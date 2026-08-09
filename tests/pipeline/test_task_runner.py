"""S-3(任务底座):波次推进心脏(离线,零 DB 零 API —— _execute/LLM 接缝/队列全打桩)。

验收(任务书):同 (task_id,wave_n) 重复投递恰好执行一次;租约被占期间重投必须非 2xx;
僵尸双提交被 CAS 丢弃;波 event 的 $ 与 usage 对得上;enqueue 失败 fail-closed。
"""
import json

import pytest

from pipeline import config, task_runner as TR, taskstate as TS


class FakeDB:
    """极简内存 DB:按 SQL 特征分发到行为(不解析 SQL,认 taskstate 的规范语句)。"""

    def __init__(self, row=None):
        self.row = row          # dict 形式的 agent_tasks 单行
        self.events = []
        self.claims = 0

    def execute(self, sql, params):
        s = " ".join(sql.split())
        r = self.row
        if s.startswith("UPDATE agent_tasks SET status='running'"):     # CLAIM
            if (r and r["status"] in ("pending", "running") and r["wave_n"] == params["wave_n"]
                    and not r.get("lease_live")):
                self.claims += 1
                r["status"], r["lease_token"], r["lease_live"] = "running", params["token"], True
                return [(r["task_id"], r["owner"], r["goal"], json.dumps(r["plan"]),
                         r["status"], r["wave_n"], r["lease_token"], r["budget_cap"],
                         r["spent_usd"], r["wasted_usd"], r.get("precharged_usd", 0.0),
                         r.get("parent_task_id"))]
            return []
        if s.startswith("SELECT status, wave_n"):                        # REREAD
            if not r:
                return []
            return [(r["status"], r["wave_n"], bool(r.get("lease_live")),
                     json.dumps(r["plan"]))]
        if "SET plan=" in s:                                             # CHECKPOINT(CAS)
            if (r and r.get("lease_token") == params["token"] and r["wave_n"] == params["wave_n"]
                    and r["status"] == "running" and not r.get("cas_lost")):
                r["plan"] = params["plan"].adapted if hasattr(params["plan"], "adapted") else params["plan"]
                r["spent_usd"] = params["spent_usd"]
                r["wasted_usd"] = params["wasted_usd"]
                r["precharged_usd"] = 0.0
                r["wave_n"] += 1
                r["lease_token"], r["lease_live"] = None, False
                return [(r["wave_n"],)]
            return []
        if "spent_usd = spent_usd + %(est)s" in s:                       # PRECHARGE(悲观累加)
            if r and r.get("lease_token") == params["token"] and r["status"] == "running":
                prev = r.get("precharged_usd", 0.0)
                r["spent_usd"] = r["spent_usd"] + params["est"]          # 陈预估不退(留在 spent)
                r["wasted_usd"] = r.get("wasted_usd", 0.0) + prev        # 只标记成浪费
                r["precharged_usd"] = params["est"]
                return [(r["spent_usd"], r["wasted_usd"])]
            return []
        if "wasted_usd = wasted_usd + %(actual)s" in s:                  # SETTLE_FAILED
            if r and r.get("lease_token") == params["token"]:
                r["spent_usd"] = r["spent_usd"] - r.get("precharged_usd", 0.0) + params["actual"]
                r["wasted_usd"] = r.get("wasted_usd", 0.0) + params["actual"]
                r["precharged_usd"] = 0.0
                return [(r["spent_usd"], r["wasted_usd"])]
            return []
        if s.startswith("UPDATE agent_tasks SET status="):               # TERMINAL
            if r and r["status"] in params["from_statuses"]:
                r["status"] = params["to_status"]
                r["lease_token"], r["lease_live"] = None, False
                return [(r["status"],)]
            return []
        if "INSERT INTO agent_task_events" in s:
            self.events.append((params["kind"], json.loads(params["payload"])))
            return []
        if "kind='user_note'" in s:
            return []
        raise AssertionError(f"FakeDB 不认识的 SQL:{s[:80]}")


def usage_mod():
    from pipeline.agentops import usage
    return usage


def _row(**kw):
    base = dict(task_id="tk_1", owner="kenny", goal="整理滑雪视频",
                plan={"remaining": [{"id": 1, "instruction": "看 v1", "video_ids": ["v1"]}],
                      "done": {}},
                status="running", wave_n=1, lease_token=None, lease_live=False,
                budget_cap=0.5, spent_usd=0.0, wasted_usd=0.0, precharged_usd=0.0,
                parent_task_id=None)
    base.update(kw)
    return base


@pytest.fixture()
def wired(monkeypatch):
    """接线:_execute→FakeDB;LLM/波执行/队列/限流 全打桩。返回 (db, calls)。"""
    db = FakeDB(_row())
    calls = {"enq": [], "waves": [], "reset": 0, "recorded": []}
    monkeypatch.setattr(TR, "_execute", db.execute)
    import pipeline.task_store as store
    monkeypatch.setattr(store, "_execute", db.execute)
    monkeypatch.setattr(TR, "run_wave",
                        lambda task, batch: calls["waves"].append([b["id"] for b in batch])
                        or {str(b["id"]): {"answer": f"done-{b['id']}"} for b in batch})
    monkeypatch.setattr(TR, "plan_goal", lambda goal, notes, parent_ctx=None: [
        {"id": 1, "instruction": "看 v1", "video_ids": ["v1"]},
        {"id": 2, "instruction": "看 v2", "video_ids": ["v2"]}])
    monkeypatch.setattr(TR, "finalize_report", lambda goal, done: "最终报告")
    from pipeline import task_queue
    monkeypatch.setattr(task_queue, "enqueue_advance",
                        lambda tid, w: calls["enq"].append((tid, w)))
    from pipeline.agentops import ratelimit, usage
    monkeypatch.setattr(usage, "reset_usage",
                        lambda: calls.__setitem__("reset", calls["reset"] + 1))
    monkeypatch.setattr(usage, "summarize", lambda: {"cost_usd": 0.033})
    monkeypatch.setattr(ratelimit, "record",
                        lambda owner, ip, sid, cost: calls["recorded"].append((owner, sid, cost)))
    return db, calls


def test_normal_wave_advances_and_accounts(wired, monkeypatch):
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 1)    # 一波只跑一个 → 走"续投"分支
    db, calls = wired
    db.row["plan"]["remaining"].append({"id": 2, "instruction": "看 v2", "video_ids": []})
    out = TR.advance("tk_1", 1)
    assert out == {"result": "ok", "dispatch": "advanced", "next_wave": 2}
    assert calls["reset"] == 1                               # 显式 reset(不 reset 记账为零)
    assert db.row["wave_n"] == 2 and db.row["plan"]["done"]["1"]["answer"] == "done-1"
    kinds = [k for k, _ in db.events]
    assert "wave_attempt" in kinds                           # 记账先行
    att = dict(db.events)[("wave_attempt")] if False else [p for k, p in db.events if k == "wave_attempt"][0]
    assert att["est_usd"] > 0
    assert db.row["spent_usd"] == pytest.approx(0.033)       # 实测覆盖预估
    assert calls["enq"] == [("tk_1", 2)]                     # 先提交后投递
    assert calls["recorded"] == [("kenny", None, 0.033)]     # 费用回灌全站账,sid=None


def test_lease_busy_returns_retry_not_2xx(wired):
    """验收:租约被占期间重投必须非 2xx(503 → Cloud Tasks 退避跨过租约期,绝不 200 吞掉)。"""
    db, _ = wired
    db.row["lease_live"] = True
    db.row["lease_token"] = "someone-else"
    out = TR.advance("tk_1", 1)
    assert out["result"] == "retry" and out["dispatch"] == "lease_busy"


def test_broken_chain_reenqueues_current_wave(wired):
    """三态①:行波号更大且 remaining 非空、租约空闲 → 重复投递本身变成断链修复器。"""
    db, calls = wired
    db.row["wave_n"] = 3                                     # 行已推进到 w3,来的是 w1 的旧投递
    out = TR.advance("tk_1", 1)
    assert out["dispatch"] == "reenqueued"
    assert calls["enq"] == [("tk_1", 3)]                     # 补投【当前】波


def test_terminal_state_returns_ok_no_rerun(wired):
    db, calls = wired
    db.row["status"] = "cancelled"
    out = TR.advance("tk_1", 1)
    assert out == {"result": "ok", "dispatch": "terminal"}
    assert calls["waves"] == [] and calls["enq"] == []        # 终态绝不重跑


def test_wave0_plans_then_enqueues(wired):
    db, calls = wired
    db.row.update(wave_n=0, plan={"remaining": [], "done": {}})
    out = TR.advance("tk_1", 0)
    assert out["dispatch"] == "advanced"
    assert calls["waves"] == []                              # 规划波不跑子任务
    assert len(db.row["plan"]["remaining"]) == 2             # goal → 清单落盘
    assert ("planned", {"n": 2}) in db.events


def test_final_wave_finalizes_and_done(wired):
    """收口 = 独立的零 batch 轻波(review 确认:finalize 并进上一波检查点之前的话,
    收口 LLM 崩一次就把整波战果蒸发重跑重花)。两波走完:普通波 → 收口波 → done。"""
    db, calls = wired
    out1 = TR.advance("tk_1", 1)                             # 唯一子任务跑完 → 该收口了
    assert out1["dispatch"] == "advanced"
    assert db.row["plan"]["remaining"] == [] and "report" not in db.row["plan"]
    assert db.row["status"] == "running" and calls["enq"] == [("tk_1", 2)]
    out = TR.advance("tk_1", 2)                              # 收口波(零 batch)
    assert db.row["plan"]["remaining"] == []
    assert db.row["plan"]["report"] == "最终报告"            # 收口报告随检查点落盘
    assert db.row["status"] == "done"
    assert calls["enq"] == [("tk_1", 2)]                     # 收口波之后没有新投递
    assert ("done" in [k for k, _ in db.events])
    assert out["dispatch"] == "done"


def test_budget_gate_pauses_before_spending(wired):
    """S-4 波开头预算闸:悲观预估后比 → paused_budget(同语句清租约),一分钱不花。"""
    db, calls = wired
    db.row["spent_usd"] = 0.49                               # +est 必超 0.5
    out = TR.advance("tk_1", 1)
    assert out["dispatch"] == "paused_budget"
    assert db.row["status"] == "paused_budget" and db.row["lease_token"] is None
    assert calls["waves"] == [] and calls["reset"] == 0      # 真的没跑
    assert [k for k, _ in db.events] == ["paused"]


def test_zombie_cas_lost_drops_results_records_wasted(wired):
    """验收:僵尸双提交被 CAS 丢弃 —— 战果不入库,只记 wasted event。"""
    db, calls = wired
    db.row["cas_lost"] = True                                # 模拟租约易主
    out = TR.advance("tk_1", 1)
    assert out["dispatch"] == "zombie_dropped"
    assert db.row["plan"]["done"] == {}                      # 战果没进库
    wasted = [p for k, p in db.events if k == "wasted"]
    assert wasted and wasted[0]["usd"] == pytest.approx(0.033)


def test_enqueue_failure_fail_closed(wired, monkeypatch):
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 1)    # 留下一波才有"续投失败"可测
    db, calls = wired
    db.row["plan"]["remaining"].append({"id": 2, "instruction": "看 v2", "video_ids": []})
    from pipeline import task_queue
    monkeypatch.setattr(task_queue, "enqueue_advance",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("queue down")))
    out = TR.advance("tk_1", 1)
    assert out["dispatch"] == "enqueue_failed"
    assert db.row["status"] == "paused_error"                # 不留假活
    assert "enqueue_failed" in [k for k, _ in db.events]


def test_crash_anywhere_goes_paused_error(wired, monkeypatch):
    db, _ = wired
    monkeypatch.setattr(TR, "run_wave",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = TR.advance("tk_1", 1)
    assert out["dispatch"] == "crashed"
    assert db.row["status"] == "paused_error"                # 崩溃兜底 fail-closed
    assert "error" in [k for k, _ in db.events]


def test_double_delivery_executes_exactly_once(wired):
    """验收:同 (task_id,wave_n) 重复投递恰好执行一次(第二次撞三态①补投,不重跑)。"""
    db, calls = wired
    TR.advance("tk_1", 1)
    n_waves = len(calls["waves"])
    out2 = TR.advance("tk_1", 1)                             # 同波重复投递
    assert len(calls["waves"]) == n_waves                    # 没有第二次执行
    assert out2["dispatch"] in ("reenqueued", "terminal")


# ── advance 端点鉴权(K_SERVICE 禁密钥;403 fail-closed)──
def test_advance_auth_local_secret_and_cloud_lockdown(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEYS", "kenny:pw")
    import importlib
    import api.server as srv
    importlib.reload(srv)
    monkeypatch.setattr(srv.config, "USE_TASKS", True)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("TASKS_SHARED_SECRET", "s3cret")
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    from pipeline import task_runner
    monkeypatch.setattr(task_runner, "advance", lambda t, w: {"result": "ok", "dispatch": "x"})
    ok = c.post("/internal/tasks/advance", json={"task_id": "tk", "wave_n": 1},
                headers={"X-Tasks-Secret": "s3cret"})
    assert ok.status_code == 200                             # 本地共享密钥可用
    bad = c.post("/internal/tasks/advance", json={"task_id": "tk", "wave_n": 1},
                 headers={"X-Tasks-Secret": "wrong"})
    assert bad.status_code == 403
    monkeypatch.setenv("K_SERVICE", "videosense")            # 云上:密钥路径直接禁用
    cloud = c.post("/internal/tasks/advance", json={"task_id": "tk", "wave_n": 1},
                   headers={"X-Tasks-Secret": "s3cret"})
    assert cloud.status_code == 403
    monkeypatch.setattr(task_runner, "advance", lambda t, w: {"result": "retry"})
    monkeypatch.delenv("K_SERVICE", raising=False)
    r = c.post("/internal/tasks/advance", json={"task_id": "tk", "wave_n": 1},
               headers={"X-Tasks-Secret": "s3cret"})
    assert r.status_code == 503                              # RETRY → 非 2xx


# ── S-3 review 钉子 ──
def test_stale_estimate_stays_in_spent_and_marked_wasted(wired):
    """review 两轮定稿的钱账口径:被杀/超时 attempt 的预估【不退钱】,留在 spent 里
    (悲观,规格原文"超时波也推高 spent,预算闸对重试风暴恢复视力"),只标记成 wasted。
    退钱=闸对重试风暴失明,实测 cap 可被突破 N 倍。"""
    db, calls = wired
    db.row.update(spent_usd=0.16, precharged_usd=0.06)       # 上一 attempt 预扣后被杀
    TR.advance("tk_1", 1)
    # spent = 0.16(含陈预估)+ 本次实测 0.033;陈预估标记为浪费但不退
    assert db.row["spent_usd"] == pytest.approx(0.193)
    assert db.row["wasted_usd"] == pytest.approx(0.06)
    assert db.row["precharged_usd"] == 0.0                   # 本 attempt 已结清


def test_zombie_and_crash_still_record_ratelimit(wired, monkeypatch):
    """review 确认:僵尸/崩溃波的真实花费必须进全站限流账 —— 重试风暴恰是最需要
    全站熔断有视力的时刻。"""
    db, calls = wired
    db.row["cas_lost"] = True
    TR.advance("tk_1", 1)
    assert ("kenny", None, 0.033) in calls["recorded"]       # 僵尸也入账
    db2 = FakeDB(_row())
    calls["recorded"].clear()
    monkeypatch.setattr(TR, "_execute", db2.execute)
    import pipeline.task_store as store
    monkeypatch.setattr(store, "_execute", db2.execute)
    monkeypatch.setattr(TR, "run_wave",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    TR.advance("tk_1", 1)
    assert calls["recorded"] and calls["recorded"][0][2] == pytest.approx(0.033)   # 崩溃也入账
    err = [p for k, p in db2.events if k == "error"][0]
    assert err["usd_before_crash"] == pytest.approx(0.033)   # 金额进 error event


def test_preclaim_error_retries_without_touching_state(wired, monkeypatch):
    """review 确认:认领前的瞬时异常(如补读时 Neon 掐连接)只许 RETRY ——
    fail-closed 兜底若不设围栏,旁路投递的崩溃会把别人正持租在跑的波打成僵尸。"""
    db, _ = wired
    db.row["lease_live"] = True                              # 别人正持租在跑
    db.row["lease_token"] = "someone-else"
    real = TR._execute

    def flaky(sql, params):
        if sql.strip().startswith("SELECT status"):
            raise RuntimeError("Neon 掐连接")
        return real(sql, params)
    monkeypatch.setattr(TR, "_execute", flaky)
    out = TR.advance("tk_1", 1)
    assert out["result"] == "retry" and out["dispatch"] == "preclaim_error"
    assert db.row["status"] == "running"                     # 别人的波没被打成僵尸


def test_finalize_crash_only_replays_finalize_not_whole_wave(wired, monkeypatch):
    """review 确认:收口独立成波后,finalize 崩只重跑一次 LLM 调用 ——
    上一波战果已随检查点落盘,不再蒸发。"""
    db, calls = wired
    TR.advance("tk_1", 1)                                    # 普通波:战果落盘
    saved_done = dict(db.row["plan"]["done"])
    assert saved_done                                        # 战果确实在库里
    monkeypatch.setattr(TR, "finalize_report",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("LLM 拒答")))
    out = TR.advance("tk_1", 2)                              # 收口波崩
    assert out["dispatch"] == "crashed" and db.row["status"] == "paused_error"
    assert db.row["plan"]["done"] == saved_done              # 战果毫发无损(resume 只重收口)


# ── advance 端点:OIDC 钉调用者身份(review-HIGH)──
def test_oidc_pins_caller_identity(monkeypatch):
    """verify 只校验签名/exp/aud,而 SA 的 ID token audience 谁都能自选 ——
    任何 Google 账号可铸 aud=本服务的合法 token。必须钉 email == 投递 SA。"""
    monkeypatch.setenv("APP_ACCESS_KEYS", "kenny:pw")
    import importlib
    import api.server as srv
    importlib.reload(srv)
    monkeypatch.setattr(srv.config, "USE_TASKS", True)
    monkeypatch.setattr(srv.config, "TASKS_ADVANCE_URL", "https://vs.example/internal/tasks/advance")
    monkeypatch.setattr(srv.config, "TASKS_INVOKER_SA", "tasks@proj.iam.gserviceaccount.com")

    def fake_claims(claims):
        monkeypatch.setattr(srv, "_oidc_claims", lambda tok: claims)
        return srv._verify_advance_auth(None, "Bearer x", None)
    assert fake_claims({"email": "tasks@proj.iam.gserviceaccount.com",
                        "email_verified": True}) is True     # 我们的投递 SA
    assert fake_claims({"email": "attacker@evil.iam.gserviceaccount.com",
                        "email_verified": True}) is False    # 别人的 SA:aud 对也不行
    assert fake_claims({"email_verified": True}) is False    # 缺 email
    assert fake_claims({"email": "tasks@proj.iam.gserviceaccount.com"}) is False  # 缺 verified
    monkeypatch.setattr(srv.config, "TASKS_INVOKER_SA", "")  # SA 未配置 → fail-closed
    assert fake_claims({"email": "tasks@proj.iam.gserviceaccount.com",
                        "email_verified": True}) is False


# ── S-4 步内闸(取消 / 预算)──
def _gate_probe(monkeypatch, live):
    """给 _wrap_step_gate 造一个 base execute + 打桩的 live_state。返回 (gated, hits)。"""
    from pipeline import loop_driver, task_store
    hits = []

    def base(cid, name, inputs, upstream, uses):
        hits.append(name)
        return loop_driver.ExecResult(ok=True, value={"rows": 1}, preview=[], n=1)
    base.tree_guard = "G"
    base.tree_nodes = {"nodes": 1}
    monkeypatch.setattr(task_store, "live_state", lambda tid: live)
    return TR._wrap_step_gate(base, "tk_1", 0.0), hits


def test_step_gate_stops_on_cancel(monkeypatch):
    """验收:cancel ≤1 步边界生效 —— 下一次工具调用【不执行】,回软收口信封。"""
    gated, hits = _gate_probe(monkeypatch, ("cancelled", 0.1, 0.5))
    res = gated("c1", "sql_query", {}, {}, [])
    assert res.ok and "取消" in res.value["answer"] and "【未核查】" in str(res.preview)
    assert hits == []                                        # 真的没执行


def test_step_gate_stops_on_budget_overrun(monkeypatch):
    gated, hits = _gate_probe(monkeypatch, ("running", 0.61, 0.5))
    res = gated("c1", "analyze_video", {}, {}, [])
    assert res.ok and "超预算" in res.value["answer"] and hits == []


def test_step_gate_lets_show_and_healthy_through(monkeypatch):
    gated, hits = _gate_probe(monkeypatch, ("cancelled", 0.1, 0.5))
    gated("c1", "show_video", {}, {}, [])                    # 交付类放行(不烧钱)
    assert hits == ["show_video"]
    gated2, hits2 = _gate_probe(monkeypatch, ("running", 0.1, 0.5))
    gated2("c2", "sql_query", {}, {}, [])
    assert hits2 == ["sql_query"]                            # 健康任务照常


def test_step_gate_caches_and_fails_open(monkeypatch):
    """读数缓存 5s(别每次工具调用打库);查库炸了 fail-open(观测不拖垮执行)。"""
    from pipeline import loop_driver, task_store
    calls = []

    def base(cid, name, inputs, upstream, uses):
        return loop_driver.ExecResult(ok=True, value={}, preview=[], n=1)
    monkeypatch.setattr(task_store, "live_state",
                        lambda tid: calls.append(1) or ("running", 0.1, 0.5))
    gated = TR._wrap_step_gate(base, "tk_1", 0.0)
    for _ in range(5):
        gated("c", "sql_query", {}, {}, [])
    assert len(calls) == 1                                   # 5 次调用只查了 1 次库
    monkeypatch.setattr(task_store, "live_state",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("db down")))
    g2 = TR._wrap_step_gate(base, "tk_2", 0.0)
    assert g2("c", "sql_query", {}, {}, []).ok               # fail-open 照常执行


def test_step_gate_passes_closure_attrs(monkeypatch):
    """P0-6 教训:子 agent 靠 execute.tree_guard/tree_nodes 取全树账本 —— 壳必须透传。"""
    gated, _ = _gate_probe(monkeypatch, ("running", 0.1, 0.5))
    assert gated.tree_guard == "G" and gated.tree_nodes == {"nodes": 1}


# ── S-4 review 三条 HIGH 的钉子 ──
def test_crash_settles_real_cost_into_task_ledger(wired, monkeypatch):
    """review-HIGH:崩溃 attempt 的真实花费必须结进【任务账本】——
    只落 event 的话每次 resume 都是"免费重跑",实测 cap 可被突破 N 倍。"""
    db, calls = wired
    monkeypatch.setattr(usage_mod(), "summarize", lambda: {"cost_usd": 0.40})
    monkeypatch.setattr(TR, "run_wave", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    TR.advance("tk_1", 1)
    assert db.row["status"] == "paused_error"
    assert db.row["spent_usd"] == pytest.approx(0.40)         # 真花的钱进了账本
    assert db.row["wasted_usd"] == pytest.approx(0.40)        # 且全记为浪费
    assert db.row["precharged_usd"] == 0.0


def test_zombie_settles_real_cost_into_task_ledger(wired, monkeypatch):
    db, calls = wired
    db.row["cas_lost"] = True
    monkeypatch.setattr(usage_mod(), "summarize", lambda: {"cost_usd": 0.25})
    TR.advance("tk_1", 1)
    assert db.row["spent_usd"] == pytest.approx(0.25)
    assert db.row["wasted_usd"] == pytest.approx(0.25)


def test_repeated_crash_resume_cannot_burn_past_cap(wired, monkeypatch):
    """review 实测的烧钱回路:5 次"崩在末尾"+ 5 次 resume,旧实现账本恒 0.22 而真烧 $2。
    新口径下 spent 单调增,闸必须在第 N 次前把它拦住。"""
    db, calls = wired
    monkeypatch.setattr(usage_mod(), "summarize", lambda: {"cost_usd": 0.20})
    monkeypatch.setattr(TR, "run_wave", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    outs = []
    for _ in range(5):
        db.row["status"] = "running"                          # 模拟用户点 resume
        db.row["lease_live"] = False
        outs.append(TR.advance("tk_1", 1)["dispatch"])
    assert "paused_budget" in outs                            # 闸真的拦住了
    assert db.row["spent_usd"] <= 0.5 + 0.21                  # 没有 N 倍突破


def test_finalize_wave_gets_grace_budget(wired, monkeypatch):
    """review-HIGH 死锁:花满预算的任务因差几分钱收尾费永远出不了报告。
    收口波(零 batch,一次 LLM 调用)在 cap 之上有小额收尾额度。"""
    db, calls = wired
    db.row.update(wave_n=2, spent_usd=0.50,                   # 正好花满 cap
                  plan={"remaining": [], "done": {"1": {"answer": "a"}}})
    out = TR.advance("tk_1", 2)
    assert out["dispatch"] == "done"                          # 不再是 paused_budget
    assert db.row["plan"]["report"] == "最终报告"              # 战果变成了交付物


def test_normal_wave_still_gated_at_cap(wired):
    """收尾额度只给收口波:普通波该停还是停(别把 grace 变成普涨)。"""
    db, calls = wired
    db.row["spent_usd"] = 0.50
    out = TR.advance("tk_1", 1)                               # remaining 非空 = 普通波
    assert out["dispatch"] == "paused_budget"


def test_step_gate_verdict_is_sticky_across_db_failure(monkeypatch):
    """review-HIGH:判停必须粘住 —— TTL 过期时查库失败,旧实现 fail-open 把已生效的
    取消裁决重置回放行(取消是终态,没有"取消又被撤销"的语义)。"""
    from pipeline import loop_driver, task_store
    hits = []

    def base(cid, name, inputs, upstream, uses):
        hits.append(name)
        return loop_driver.ExecResult(ok=True, value={}, preview=[], n=1)
    monkeypatch.setattr(task_store, "live_state", lambda tid: ("cancelled", 0.1, 0.5))
    monkeypatch.setattr(TR, "STEP_CHECK_TTL_S", 0.0)          # TTL 立刻过期
    gated = TR._wrap_step_gate(base, "tk_1", 0.0)
    assert "取消" in gated("c1", "sql_query", {}, {}, []).value["answer"]
    monkeypatch.setattr(task_store, "live_state",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("db down")))
    res = gated("c2", "sql_query", {}, {}, [])                # 查库炸 + TTL 过期
    assert "取消" in res.value["answer"] and hits == []       # 仍然判停,工具没执行
