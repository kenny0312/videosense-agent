"""S-2(任务底座):agent_tasks 的存取层(设计 docs/longhorizon-task-substrate-plan.md §S-2)。

规范 SQL/状态机来自 pipeline.taskstate(唯一事实源);连接复用 semantic_index 的
即用即连执行器(同一 Neon 库,autocommit 单语句 —— 底座的多步一致性由 CAS/幂等索引
兜,不靠事务)。owner 隔离一律在 SQL 的 WHERE 里做,不靠调用方自觉。
"""
from __future__ import annotations

import hashlib
import json
import uuid

from pipeline import taskstate as TS
from pipeline.semantic_index import _execute

_ACTIVE_LIST = list(TS.ACTIVE)

# 立项(幂等):撞 (owner, goal_hash) 活跃部分唯一索引 → 0 行,调用方回读已有任务。
_INSERT_SQL = """
INSERT INTO agent_tasks (task_id, owner, goal, goal_hash, budget_cap, parent_task_id)
VALUES (%(task_id)s, %(owner)s, %(goal)s, %(goal_hash)s, %(budget_cap)s, %(parent)s)
ON CONFLICT (owner, goal_hash)
WHERE status IN ('pending','running','paused_budget','paused_error')
DO NOTHING
RETURNING task_id
"""

_FIND_ACTIVE_SQL = """
SELECT task_id FROM agent_tasks
WHERE owner=%(owner)s AND goal_hash=%(goal_hash)s AND status=ANY(%(active)s)
ORDER BY created_at DESC LIMIT 1
"""

_GET_SQL = """
SELECT task_id, goal, status, wave_n, plan, budget_cap, spent_usd, wasted_usd,
       created_at, updated_at
FROM agent_tasks WHERE task_id=%(task_id)s AND owner=%(owner)s
"""

_EVENTS_SQL = """
SELECT kind, payload, created_at FROM agent_task_events
WHERE task_id=%(task_id)s ORDER BY id DESC LIMIT %(limit)s
"""

_EVENT_INSERT_SQL = """
INSERT INTO agent_task_events (task_id, kind, payload)
VALUES (%(task_id)s, %(kind)s, %(payload)s)
"""


def goal_hash(owner: str, goal: str, parent_task_id: "str | None" = None) -> str:
    return hashlib.sha256(
        f"{owner}\n{goal.strip()}\n{parent_task_id or ''}".encode()).hexdigest()[:32]


def create_task(owner: str, goal: str, budget_cap: float,
                parent_task_id: "str | None" = None) -> "tuple[str, bool]":
    """立项(幂等)。返回 (task_id, created);created=False = 命中已有活跃同 goal 任务
    (顺手挡前端双击)。parent_task_id(S-10)= 续作:规划波会注入父任务报告作只读快照。
    goal_hash 含 parent —— 同一句"再细化一版"针对不同父任务是不同的活,不该互相幂等挡掉。"""
    gh = goal_hash(owner, goal, parent_task_id)
    task_id = "tk_" + uuid.uuid4().hex[:16]
    args = {"task_id": task_id, "owner": owner, "goal": goal, "goal_hash": gh,
            "budget_cap": float(budget_cap), "parent": parent_task_id}
    rows = _execute(_INSERT_SQL, args)
    if rows:
        return rows[0][0], True
    got = _execute(_FIND_ACTIVE_SQL, {"owner": owner, "goal_hash": gh,
                                      "active": _ACTIVE_LIST})
    if not got:                      # 幂等索引撞了但活跃行又不见了(并发取消)→ 重试一次
        rows = _execute(_INSERT_SQL, args)
        if rows:
            return rows[0][0], True
        raise RuntimeError("任务立项冲突且回读失败(并发取消风暴),稍后重试")
    return got[0][0], False


# ── S-9 完成回流 / S-10 续作 ────────────────────────────────────────────────
# 已完成且未通报:主 loop 组装 context 时查一次,注入一行,同请求内标记 notified_at。
_UNNOTIFIED_SQL = """
SELECT task_id, goal FROM agent_tasks
WHERE owner=%(owner)s AND status='done' AND notified_at IS NULL
ORDER BY updated_at DESC LIMIT %(limit)s
"""
_MARK_NOTIFIED_SQL = ("UPDATE agent_tasks SET notified_at=now() "
                      "WHERE task_id=ANY(%(ids)s) AND notified_at IS NULL")


def unnotified_done(owner: str, limit: int = 3) -> list:
    """[(task_id, goal)...] —— 已完成还没告诉过用户的任务。"""
    return [(r[0], r[1]) for r in _execute(_UNNOTIFIED_SQL,
                                           {"owner": owner, "limit": int(limit)})]


def mark_notified(task_ids: list) -> None:
    if task_ids:
        _execute(_MARK_NOTIFIED_SQL, {"ids": list(task_ids)})


def report_of(owner: str, task_id: str) -> "dict | None":
    """get_task_report 工具与 S-10 续作共用:父任务的报告 + done 结论(只读快照)。
    owner 隔离在 SQL 里;不存在/不属于你 → None。"""
    rows = _execute("SELECT goal, status, plan, spent_usd FROM agent_tasks "
                    "WHERE task_id=%(t)s AND owner=%(o)s",
                    {"t": task_id, "o": owner})
    if not rows:
        return None
    goal, status, plan, spent = rows[0]
    plan = plan if isinstance(plan, dict) else json.loads(plan or "{}")
    return {"task_id": task_id, "goal": goal, "status": status,
            "report": plan.get("report"), "done": plan.get("done") or {},
            "spent_usd": float(spent)}


def add_event(task_id: str, kind: str, payload: dict | None = None) -> None:
    """落事件。kind 必须在 taskstate.EVENT_KINDS(代码层先炸,别等 DB CHECK)。"""
    if kind not in TS.EVENT_KINDS:
        raise ValueError(f"未知事件种类:{kind}")
    _execute(_EVENT_INSERT_SQL, {"task_id": task_id, "kind": kind,
                                 "payload": json.dumps(payload or {}, ensure_ascii=False)})


def set_status(task_id: str, to_status: str) -> bool:
    """终态/暂停迁移(同一条语句清租约,from 集合按状态机推导)。返回是否真的迁了。"""
    rows = _execute(TS.TERMINAL_SQL, TS.terminal_params(task_id, to_status))
    return bool(rows)


def resume(task_id: str, new_cap: float) -> "tuple[int, float] | None":
    """复活暂停的任务:新 cap + 回 running + 清租约。返回 (当前 wave_n, 生效 cap);
    None = 不在暂停态(幂等:重复 resume 不炸)。投递归调用方(必须投,否则死锁)。"""
    rows = _execute(TS.RESUME_SQL, TS.resume_params(task_id, new_cap))
    return (int(rows[0][0]), float(rows[0][1])) if rows else None


def live_state(task_id: str) -> "tuple[str, float, float] | None":
    """(status, spent_usd, budget_cap):步内取消/预算闸的实时读数(S-4 wrapper 用)。"""
    rows = _execute("SELECT status, spent_usd, budget_cap FROM agent_tasks "
                    "WHERE task_id=%(t)s", {"t": task_id})
    if not rows:
        return None
    return (rows[0][0], float(rows[0][1]), float(rows[0][2]))


def get_view(owner: str, task_id: str, events_limit: int = 10) -> "dict | None":
    """状态+进度(done/remaining 按 id 计数)+成本行+events 尾部。owner 不符 → None(404 口径)。"""
    rows = _execute(_GET_SQL, {"task_id": task_id, "owner": owner})
    if not rows:
        return None
    (tid, goal, status, wave_n, plan, cap, spent, wasted, created, updated) = rows[0]
    plan = plan if isinstance(plan, dict) else json.loads(plan or "{}")
    ev = _execute(_EVENTS_SQL, {"task_id": task_id, "limit": int(events_limit)})
    return {
        "task_id": tid, "goal": goal, "status": status, "wave_n": wave_n,
        "progress": {"done": len(plan.get("done") or {}),
                     "remaining": len(plan.get("remaining") or [])},
        "cost": {"spent_usd": float(spent), "budget_cap": float(cap),
                 "wasted_usd": float(wasted)},
        "created_at": str(created), "updated_at": str(updated) if updated else None,
        "events": [{"kind": k, "payload": p, "at": str(t)} for k, p, t in ev],
    }


def owner_of(task_id: str) -> "str | None":
    rows = _execute("SELECT owner FROM agent_tasks WHERE task_id=%(t)s", {"t": task_id})
    return rows[0][0] if rows else None


def status_of(task_id: str) -> "tuple[str, int] | None":
    """(status, wave_n);pending 幽灵自愈(S-2 立项幂等命中路径)用。"""
    rows = _execute("SELECT status, wave_n FROM agent_tasks WHERE task_id=%(t)s",
                    {"t": task_id})
    return (rows[0][0], int(rows[0][1])) if rows else None
