"""S-1(任务底座):任务状态机 + 三条【规范 SQL】(设计 docs/longhorizon-task-substrate-plan.md §S-1/S-3)。

这里是状态机的唯一事实源:
  · LEGAL 写死全部合法迁移 —— 代码路径想做非法迁移在这里就炸(不靠自觉);
  · 三条规范 SQL 是 S-2/S-3 的既定契约,红队修正直接烙进 WHERE:
      CLAIM_SQL      认领:status 守卫 + wave 匹配 + 租约空闲,发 lease_token(CAS 围栏);
      CHECKPOINT_SQL 落检查点:status='running' + wave_n 匹配 + lease_token 匹配 ——
                     僵尸波(租约易主)在这一个 WHERE 里被丢弃(双提交/双记账/cancelled→done 全堵死);
      TERMINAL_SQL   终态/暂停:写状态的【同一条语句】清租约(lease_until/lease_token=NULL)——
                     否则 resume 死锁(红队 HIGH)。
离线纯常量+纯函数,单测在 tests/pipeline/test_taskstate.py。
"""
from __future__ import annotations

STATUSES = ("pending", "running", "paused_budget", "paused_error", "done", "cancelled")
ACTIVE = ("pending", "running", "paused_budget", "paused_error")   # 立项幂等索引同一口径
TERMINAL = ("done", "cancelled")

# 事件种类的唯一事实源(S-2/S-3 只许 import 这里;DDL 的 CHECK 与之比对有测试钉)。
# wasted = 僵尸波战果丢弃的账(S-3 步5);enqueue_failed = 立项后投递失败(S-2 fail-closed)。
EVENT_KINDS = ("planned", "wave_attempt", "wave_done", "user_note", "paused", "resumed",
               "cancelled", "done", "error", "wasted", "enqueue_failed")

# 租约时长(分钟)。红队/review 确认的合同约束:租约必须 ≥ 波时长预算(0.8×900s=720s)+
# 落盘余量,否则合法慢波(10-12min)会在跑到一半时被二次认领 → 同一波双份 LLM 花费。
TASK_LEASE_MIN = 15

# 合法迁移写死(from → to)。没有列出的组合一律非法 —— 包括任何离开终态的迁移。
LEGAL = {
    ("pending", "running"),                  # 认领第一波
    ("pending", "paused_error"),             # 立项后 enqueue 失败的 fail-closed(S-2;review 确认:
    ("pending", "cancelled"),                # 没这条,pending 幽灵无修复通道)
    ("running", "running"),                  # 波间推进(状态不变,wave_n+1)
    ("running", "paused_budget"),
    ("running", "paused_error"),
    ("running", "done"),
    ("running", "cancelled"),
    ("paused_budget", "running"),            # resume(新 cap)
    ("paused_budget", "cancelled"),
    ("paused_error", "running"),             # 重试入口
    ("paused_error", "cancelled"),
}


def can_transition(frm: str, to: str) -> bool:
    return (frm, to) in LEGAL


def assert_transition(frm: str, to: str):
    """代码路径的守卫:非法迁移立刻炸(fail-loud),不落库不吞。"""
    if not can_transition(frm, to):
        raise ValueError(f"非法任务状态迁移:{frm} → {to}")


# ── 规范 SQL(S-3 心脏的三条;参数一律 %s 占位)─────────────────────────────
# 认领(三态分流的第一步):命中 0 行时调用方必须补读该行分流(补投/503/200),
# 见任务书 S-3;这里只保证"能认领的条件"原子成立。
CLAIM_SQL = """
UPDATE agent_tasks
SET status='running', lease_until=now() + make_interval(mins => %(lease_min)s),
    lease_token=%(token)s, updated_at=now()
WHERE task_id=%(task_id)s
  AND status IN ('pending','running')
  AND wave_n=%(wave_n)s
  AND (lease_until IS NULL OR lease_until < now())
RETURNING task_id, owner, goal, plan, status, wave_n, lease_token,
          budget_cap, spent_usd, wasted_usd, precharged_usd
"""

# ── 钱账口径(review 两轮后定稿)────────────────────────────────────────────
# spent_usd  = 本任务【真实累计花费】,单调不减,含所有白花的钱 → 波开头闸只看它就够;
# wasted_usd = spent 里"白花"的那部分(仅作披露,不参与闸);
# precharged_usd = 本 attempt 未结算的悲观预估(成功时被实测替换,失败时留在 spent 里)。
#
# 记账先行(S-3 步3):本波预估悲观计入 spent —— 超时/被杀的波【不退这笔钱】,只把它
# 标记成 wasted(review-HIGH:退钱=闸对重试风暴失明,实测 cap 可被突破 N 倍;
# 规格原文就是"超时波也推高 spent,预算闸对重试风暴恢复视力")。
PRECHARGE_SQL = """
UPDATE agent_tasks
SET spent_usd = spent_usd + %(est)s,
    wasted_usd = wasted_usd + precharged_usd,
    precharged_usd = %(est)s, updated_at=now()
WHERE task_id=%(task_id)s
  AND lease_token=%(token)s
  AND status='running'
RETURNING spent_usd, wasted_usd
"""

# 结算失败 attempt(崩溃/僵尸):实测替换本次预估,且【这笔钱全记浪费】。
# 与 PRECHARGE 的区别:这里知道实测值,所以退预估换实测是准确的、不是失明。
SETTLE_FAILED_SQL = """
UPDATE agent_tasks
SET spent_usd = spent_usd - precharged_usd + %(actual)s,
    wasted_usd = wasted_usd + %(actual)s,
    precharged_usd = 0, updated_at=now()
WHERE task_id=%(task_id)s
  AND lease_token=%(token)s
RETURNING spent_usd, wasted_usd
"""


def precharge_params(task_id: str, token: str, est: float) -> dict:
    return {"task_id": task_id, "token": token, "est": float(est)}


def settle_failed_params(task_id: str, token: str, actual: float) -> dict:
    return {"task_id": task_id, "token": token, "actual": float(actual)}


# 落检查点(CAS):0 行 = 本 attempt 是僵尸 → 整波战果丢弃,只记 wasted event。
# status='running' 条件同时挡住 cancelled→done(裸 SQL 也绕不过状态守卫,S-1 验收③)。
# precharged_usd 清零 = 本 attempt 的预估已由实测结清。
CHECKPOINT_SQL = """
UPDATE agent_tasks
SET plan=%(plan)s, spent_usd=%(spent_usd)s, wasted_usd=%(wasted_usd)s,
    precharged_usd = 0,
    wave_n=wave_n + 1, lease_until=NULL, lease_token=NULL, updated_at=now()
WHERE task_id=%(task_id)s
  AND wave_n=%(wave_n)s
  AND lease_token=%(token)s
  AND status='running'
RETURNING wave_n
"""

# 终态/暂停:同一条语句写状态 + 清租约(分两条 = resume 死锁窗口)。
# from-状态守卫进 WHERE:只有合法前驱能进目标状态(与 LEGAL 同口径,DB 层兜底)。
TERMINAL_SQL = """
UPDATE agent_tasks
SET status=%(to_status)s, lease_until=NULL, lease_token=NULL, updated_at=now()
WHERE task_id=%(task_id)s
  AND status=ANY(%(from_statuses)s)
RETURNING status
"""


def claim_params(task_id: str, wave_n: int, token: str) -> dict:
    """给 CLAIM_SQL 配参(租约时长走常量,不许调用方手写)。"""
    return {"task_id": task_id, "wave_n": int(wave_n), "token": token,
            "lease_min": TASK_LEASE_MIN}


def checkpoint_params(task_id: str, wave_n: int, token: str, plan: dict,
                      spent_usd: float, wasted_usd: float) -> dict:
    """给 CHECKPOINT_SQL 配参。plan 在这里包 psycopg2 Json 适配 —— 裸传 dict 会在
    execute 时炸 ProgrammingError: can't adapt type 'dict'(review 确认:全仓无 JSONB
    写入先例,第一个真实波跑完落盘即炸 → 租约挂到过期 → 重投重跑 = 烧钱回路)。"""
    from psycopg2.extras import Json                     # 惰性:保持模块离线可测
    return {"task_id": task_id, "wave_n": int(wave_n), "token": token,
            "plan": Json(plan), "spent_usd": float(spent_usd),
            "wasted_usd": float(wasted_usd)}


# 复活(S-4 resume / paused_error 重试):新 cap + 回 running + 【同一条语句清租约】+
# 返回当前 wave_n 供投递。红队 HIGH:v1 的 resume 没投递 = 必死锁;不清租约 = 认领不了。
# 只有两种暂停态能复活(from 守卫与 LEGAL 同口径,DB 层兜底)。
RESUME_SQL = """
UPDATE agent_tasks
SET status='running', budget_cap=%(new_cap)s,
    lease_until=NULL, lease_token=NULL, updated_at=now()
WHERE task_id=%(task_id)s
  AND status IN ('paused_budget','paused_error')
RETURNING wave_n, budget_cap
"""


def resume_params(task_id: str, new_cap: float) -> dict:
    return {"task_id": task_id, "new_cap": float(new_cap)}


def terminal_params(task_id: str, to_status: str) -> dict:
    """给 TERMINAL_SQL 配参:from 集合按 LEGAL 推导(不许手写,防与状态机漂移)。"""
    froms = [f for f, t in LEGAL if t == to_status and f != t]
    if not froms:
        raise ValueError(f"没有任何合法前驱能进入 {to_status}")
    return {"task_id": task_id, "to_status": to_status, "from_statuses": froms}
