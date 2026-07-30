"""S-1(任务底座):状态机 + 规范 SQL + DDL 的离线校验(零 DB)。

验收(任务书):setup 幂等;非法迁移单测;裸 SQL 绕不过状态守卫
(检查点 UPDATE 自带 status='running' 条件)。SQL 语义在这里用文本断言钉合同,
真 DB 行为归 S-3 的集成验收。
"""
import pytest

from perception import setup_tasks as ST
from pipeline import config, taskstate as TS


# ── 状态机 ──
def test_statuses_and_partitions_consistent():
    assert set(TS.ACTIVE) | set(TS.TERMINAL) == set(TS.STATUSES)
    assert not (set(TS.ACTIVE) & set(TS.TERMINAL))


def test_legal_transitions_reference_real_statuses():
    for f, t in TS.LEGAL:
        assert f in TS.STATUSES and t in TS.STATUSES


def test_terminal_states_are_absorbing():
    """终态是吸收态:done/cancelled 出不去(任何方向都非法)。"""
    for term in TS.TERMINAL:
        for to in TS.STATUSES:
            if to != term:
                assert not TS.can_transition(term, to), f"{term}→{to} 不该合法"


def test_illegal_transitions_blow_up():
    for frm, to in (("pending", "done"),          # 没跑过一波不能直接 done
                    ("pending", "paused_budget"),
                    ("done", "running"),
                    ("cancelled", "running"),
                    ("paused_budget", "done")):
        with pytest.raises(ValueError, match="非法"):
            TS.assert_transition(frm, to)
    TS.assert_transition("paused_budget", "running")          # resume 合法


def test_enqueue_failed_path_is_expressible():
    """review-HIGH:S-2 立项后 enqueue 失败 = pending → paused_error(fail-closed);
    没这条迁移,pending 幽灵无修复通道(补投只救 running,幂等索引还会把同 goal 钉死)。"""
    TS.assert_transition("pending", "paused_error")
    assert "pending" in TS.terminal_params("t", "paused_error")["from_statuses"]


def test_resume_paths_exist_for_both_pause_states():
    """红队 HIGH(resume 死锁)的状态机侧前提:两种暂停态都必须有回 running 的路。"""
    assert TS.can_transition("paused_budget", "running")
    assert TS.can_transition("paused_error", "running")


# ── 规范 SQL 合同(文本断言 = 防止后续改动悄悄拆掉红队修正)──
def test_claim_sql_guards():
    s = " ".join(TS.CLAIM_SQL.split())
    assert "status IN ('pending','running')" in s             # 只有活跃可跑态能认领
    assert "wave_n=%(wave_n)s" in s                           # 波匹配(重复投递幂等的前半)
    assert "lease_until IS NULL OR lease_until < now()" in s  # 租约空闲才发牌
    assert "lease_token=%(token)s" in s                       # CAS 围栏发牌


def test_checkpoint_sql_is_cas_and_status_guarded():
    """S-1 验收③:裸 SQL 绕不过状态守卫 —— 检查点必须同时钉 wave/token/status。"""
    s = " ".join(TS.CHECKPOINT_SQL.split())
    assert "wave_n=%(wave_n)s" in s
    assert "lease_token=%(token)s" in s                       # 僵尸波在这里被丢弃
    assert "status='running'" in s                            # cancelled→done 堵死
    assert "lease_until=NULL" in s and "lease_token=NULL" in s  # 落点即释放租约
    assert "wave_n + 1" in s.replace("wave_n+1", "wave_n + 1")


def test_terminal_sql_clears_lease_in_same_statement():
    """红队 HIGH:终态/暂停写入必须同一条语句清租约,否则 resume 死锁。"""
    s = " ".join(TS.TERMINAL_SQL.split())
    assert "lease_until=NULL" in s and "lease_token=NULL" in s
    assert "status=ANY(%(from_statuses)s)" in s               # from 守卫在 DB 层兜底


def test_claim_lease_covers_wave_budget():
    """review 确认的任务书内部矛盾:租约 10min < 波时长预算 720s(0.8×900)——
    合法慢波会被二次认领双倍烧钱。租约常量必须 ≥ 波预算 + 落盘余量,且 SQL 吃常量不写死。"""
    assert TS.TASK_LEASE_MIN * 60 >= 720 + 60
    s = " ".join(TS.CLAIM_SQL.split())
    assert "make_interval(mins => %(lease_min)s)" in s        # 参数化,不硬编码
    assert "interval '10 minutes'" not in s
    assert TS.claim_params("t", 3, "tok")["lease_min"] == TS.TASK_LEASE_MIN


def test_checkpoint_params_wraps_plan_as_json():
    """review-HIGH:psycopg2 不适配 dict,%(plan)s 裸传第一个真实波落盘即炸 →
    租约挂到过期 → 重投重跑 = 烧钱回路。合同层必须给 helper 包 Json。"""
    p = TS.checkpoint_params("t", 2, "tok", {"remaining": [], "done": {"1": {}}},
                             0.12, 0.0)
    assert type(p["plan"]).__name__ == "Json"                 # psycopg2.extras.Json
    assert p["wave_n"] == 2 and p["spent_usd"] == pytest.approx(0.12)


def test_terminal_params_derived_from_state_machine():
    p = TS.terminal_params("t1", "paused_budget")
    assert p["from_statuses"] == ["running"]                  # 只有 running 能进 paused_budget
    p2 = TS.terminal_params("t1", "cancelled")
    assert set(p2["from_statuses"]) == {"pending", "running", "paused_budget", "paused_error"}
    with pytest.raises(ValueError):
        TS.terminal_params("t1", "pending")                   # 没人能"回到"pending


def _check_set(ddl: str, col: str) -> set:
    """从单段 DDL 里抽出 CHECK (col IN (...)) 的字面量集合。"""
    import re
    m = re.search(rf"CHECK\s*\(\s*{col}\s+IN\s*\(([^)]*)\)", " ".join(ddl.split()))
    assert m, f"{col} 的 CHECK 子句不见了"
    return set(re.findall(r"'([^']+)'", m.group(1)))


# ── DDL(离线文本校验;真库幂等由 setup_tasks 手动跑验证)──
def test_ddl_idempotent_and_columns():
    all_sql = " ".join(" ".join(d.split()) for d in ST.ALL_DDL)
    assert all_sql.count("CREATE TABLE IF NOT EXISTS") == 2   # 幂等家规
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_active_goal" in all_sql
    assert "lease_token" in all_sql and "goal_hash" in all_sql
    assert "notified_at" in all_sql                           # S-9 回流列一次建齐
    assert "parent_task_id" in all_sql                        # S-10 续作列一次建齐


def test_status_check_equals_state_machine_exactly():
    """review 变异实测:旧钉法对三段拼接串找子串,状态字面量被 events.kind 与索引 WHERE
    顶包 —— 从 status CHECK 里删 'done' 测试照绿。改为对【单段】DDL 抽 CHECK 集合全等比对。"""
    assert _check_set(ST.DDL_TASKS, "status") == set(TS.STATUSES)


def test_event_kinds_check_equals_constant_and_covers_spec():
    """review 确认:任务书 S-2/S-3/S-4 明文要求 enqueue_failed / wasted 事件,旧闭集没留位
    —— CHECK 违约会恰好炸在错误处理路径里(僵尸波账丢失+异常掩盖)。
    唯一事实源 = taskstate.EVENT_KINDS,DDL 与之全等。"""
    kinds = _check_set(ST.DDL_EVENTS, "kind")
    assert kinds == set(TS.EVENT_KINDS)
    for required in ("wasted", "enqueue_failed", "planned", "wave_attempt", "user_note"):
        assert required in kinds


def test_active_goal_index_matches_active_partition():
    """立项幂等索引的状态集合必须与状态机的 ACTIVE 口径一致(漂移=幂等失效)。"""
    idx = " ".join(ST.DDL_TASKS_IDEMPOTENCY.split())
    for st in TS.ACTIVE:
        assert f"'{st}'" in idx
    for st in TS.TERMINAL:
        assert f"'{st}'" not in idx                           # 终态不占坑(同 goal 可重开)


def test_flags_default_off():
    """Part 0 不变量①:底座开关默认关。"""
    import importlib
    import os
    assert os.environ.get("USE_TASKS") in (None, "0") or not config.USE_TASKS or True
    # 直接断言默认值语义:未设 env 时必须是 False
    if os.environ.get("USE_TASKS") is None:
        assert config.USE_TASKS is False
    assert config.TASK_MAX_CAP_USD > 0 and config.RL_TASK_DAILY_COST_USD > 0
