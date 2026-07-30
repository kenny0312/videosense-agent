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
INSERT INTO agent_tasks (task_id, owner, goal, goal_hash, budget_cap)
VALUES (%(task_id)s, %(owner)s, %(goal)s, %(goal_hash)s, %(budget_cap)s)
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


def goal_hash(owner: str, goal: str) -> str:
    return hashlib.sha256(f"{owner}\n{goal.strip()}".encode()).hexdigest()[:32]


def create_task(owner: str, goal: str, budget_cap: float) -> "tuple[str, bool]":
    """立项(幂等)。返回 (task_id, created);created=False = 命中已有活跃同 goal 任务
    (顺手挡前端双击)。"""
    gh = goal_hash(owner, goal)
    task_id = "tk_" + uuid.uuid4().hex[:16]
    rows = _execute(_INSERT_SQL, {"task_id": task_id, "owner": owner, "goal": goal,
                                  "goal_hash": gh, "budget_cap": float(budget_cap)})
    if rows:
        return rows[0][0], True
    got = _execute(_FIND_ACTIVE_SQL, {"owner": owner, "goal_hash": gh,
                                      "active": _ACTIVE_LIST})
    if not got:                      # 幂等索引撞了但活跃行又不见了(并发取消)→ 重试一次
        rows = _execute(_INSERT_SQL, {"task_id": task_id, "owner": owner, "goal": goal,
                                      "goal_hash": gh, "budget_cap": float(budget_cap)})
        if rows:
            return rows[0][0], True
        raise RuntimeError("任务立项冲突且回读失败(并发取消风暴),稍后重试")
    return got[0][0], False


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
