"""S-2(任务底座):四端点 + 存取层(离线,零 DB —— _execute/队列全打桩)。

验收(任务书):owner 隔离;USE_TASKS=0 全 404;双击只建一个任务;enqueue 故障不留幽灵。
"""
import base64
import json

import pytest

from pipeline import config, task_store, taskstate as TS


# ── 存取层(fake _execute 钉 SQL 参数)──
def test_create_task_idempotent_shapes(monkeypatch):
    calls = []

    def fake_execute(sql, params):
        calls.append((" ".join(sql.split()), params))
        if "INSERT INTO agent_tasks" in sql:
            return []                                    # 模拟撞幂等索引
        if "SELECT task_id FROM agent_tasks" in sql:
            return [("tk_existing",)]
        return []
    monkeypatch.setattr(task_store, "_execute", fake_execute)
    tid, created = task_store.create_task("kenny", "找出所有滑雪视频做个报告", 0.5)
    assert tid == "tk_existing" and created is False     # 幂等命中 → 回已有任务
    ins = calls[0][0]
    assert "ON CONFLICT (owner, goal_hash)" in ins and "DO NOTHING" in ins
    for st in TS.ACTIVE:                                 # 幂等索引口径与状态机一致
        assert f"'{st}'" in ins


def test_create_task_fresh(monkeypatch):
    monkeypatch.setattr(task_store, "_execute",
                        lambda sql, params: [(params["task_id"],)] if "INSERT INTO agent_tasks" in sql else [])
    tid, created = task_store.create_task("kenny", "goal", 0.5)
    assert created is True and tid.startswith("tk_")


def test_add_event_rejects_unknown_kind(monkeypatch):
    monkeypatch.setattr(task_store, "_execute", lambda *a: [])
    with pytest.raises(ValueError, match="未知事件"):
        task_store.add_event("t", "not_a_kind", {})
    task_store.add_event("t", "user_note", {"note": "x"})  # 合法的照常


def test_goal_hash_scoped_by_owner():
    assert task_store.goal_hash("a", "同一目标") != task_store.goal_hash("b", "同一目标")
    assert task_store.goal_hash("a", " 同一目标 ") == task_store.goal_hash("a", "同一目标")


# ── 端点(TestClient;store/queue 打桩)──
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEYS", "kenny:pw,guest:gg")
    import importlib
    import api.server as srv
    importlib.reload(srv)
    monkeypatch.setattr(srv.config, "USE_TASKS", True)
    from fastapi.testclient import TestClient
    return TestClient(srv.app), srv


def _auth(user="kenny", pw="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_use_tasks_off_all_404(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_KEYS", "kenny:pw")
    import importlib
    import api.server as srv
    importlib.reload(srv)
    monkeypatch.setattr(srv.config, "USE_TASKS", False)
    from fastapi.testclient import TestClient
    c = TestClient(srv.app)
    assert c.post("/v1/tasks", json={"goal": "x"}, headers=_auth()).status_code == 404
    assert c.get("/v1/tasks/tk_1", headers=_auth()).status_code == 404
    assert c.post("/v1/tasks/tk_1/notes", json={"note": "x"}, headers=_auth()).status_code == 404
    assert c.post("/v1/tasks/tk_1/cancel", headers=_auth()).status_code == 404


def test_guest_403_on_all_four(client):
    c, srv = client
    g = _auth("guest", "gg")
    assert c.post("/v1/tasks", json={"goal": "x"}, headers=g).status_code == 403
    assert c.get("/v1/tasks/tk_1", headers=g).status_code == 403
    assert c.post("/v1/tasks/tk_1/notes", json={"note": "x"}, headers=g).status_code == 403
    assert c.post("/v1/tasks/tk_1/cancel", headers=g).status_code == 403


def test_create_enqueues_wave0_and_caps_budget(client, monkeypatch):
    c, srv = client
    from pipeline import task_queue
    seen = {}
    monkeypatch.setattr(task_store, "create_task",
                        lambda owner, goal, cap: seen.update(owner=owner, cap=cap) or ("tk_1", True))
    monkeypatch.setattr(task_queue, "enqueue_advance",
                        lambda tid, w: seen.update(enq=(tid, w)))
    r = c.post("/v1/tasks", json={"goal": "把滑雪视频都整理一遍", "budget_cap": 99.0},
               headers=_auth())
    assert r.status_code == 200 and r.json()["created"] is True
    assert seen["enq"] == ("tk_1", 0)                    # 投的是第 0 波(规划波)
    assert seen["cap"] <= min(srv.config.TASK_MAX_CAP_USD, srv.config.RL_TASK_DAILY_COST_USD)


def test_create_idempotent_double_click(client, monkeypatch):
    c, _ = client
    from pipeline import task_queue
    enq = []
    monkeypatch.setattr(task_store, "create_task", lambda *a: ("tk_dup", False))
    monkeypatch.setattr(task_store, "status_of", lambda tid: ("running", 1))   # 正常在跑,非幽灵
    monkeypatch.setattr(task_queue, "enqueue_advance", lambda *a: enq.append(a))
    r = c.post("/v1/tasks", json={"goal": "双击目标"}, headers=_auth())
    assert r.status_code == 200 and r.json()["created"] is False
    assert enq == []                                     # 幂等命中不再投波


def test_create_enqueue_failure_leaves_no_ghost(client, monkeypatch):
    """验收:enqueue 故障 fail-closed —— paused_error + enqueue_failed 事件 + 503,
    不留 running/pending 幽灵。"""
    c, _ = client
    from pipeline import task_queue
    ops = []
    monkeypatch.setattr(task_store, "create_task", lambda *a: ("tk_boom", True))
    monkeypatch.setattr(task_store, "set_status",
                        lambda tid, st: ops.append(("status", tid, st)) or True)
    monkeypatch.setattr(task_store, "add_event",
                        lambda tid, kind, payload=None: ops.append(("event", tid, kind)))
    monkeypatch.setattr(task_queue, "enqueue_advance",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("queue down")))
    r = c.post("/v1/tasks", json={"goal": "x"}, headers=_auth())
    assert r.status_code == 503
    assert ("status", "tk_boom", "paused_error") in ops
    assert ("event", "tk_boom", "enqueue_failed") in ops


def test_get_view_owner_isolation(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(task_store, "get_view",
                        lambda owner, tid, events_limit=10:
                        {"task_id": tid, "status": "running"} if owner == "kenny" else None)
    assert c.get("/v1/tasks/tk_1", headers=_auth()).status_code == 200
    # 同一条数据换个 owner 查 → 404(防枚举,不是 403)
    monkeypatch.setattr(task_store, "get_view", lambda *a, **k: None)
    assert c.get("/v1/tasks/tk_1", headers=_auth()).status_code == 404


def test_notes_and_cancel_owner_checked(client, monkeypatch):
    c, _ = client
    events = []
    monkeypatch.setattr(task_store, "owner_of", lambda tid: "别人")
    assert c.post("/v1/tasks/tk_1/notes", json={"note": "hi"},
                  headers=_auth()).status_code == 404
    assert c.post("/v1/tasks/tk_1/cancel", headers=_auth()).status_code == 404
    monkeypatch.setattr(task_store, "owner_of", lambda tid: "kenny")
    monkeypatch.setattr(task_store, "add_event",
                        lambda tid, kind, payload=None: events.append(kind))
    monkeypatch.setattr(task_store, "set_status", lambda tid, st: True)
    assert c.post("/v1/tasks/tk_1/notes", json={"note": "改一下方向"},
                  headers=_auth()).status_code == 200
    r = c.post("/v1/tasks/tk_1/cancel", headers=_auth())
    assert r.status_code == 200 and r.json()["changed"] is True
    assert events == ["user_note", "cancelled"]


def test_cancel_idempotent_on_terminal(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(task_store, "owner_of", lambda tid: "kenny")
    monkeypatch.setattr(task_store, "set_status", lambda tid, st: False)   # 已终态 → 0 行
    r = c.post("/v1/tasks/tk_1/cancel", headers=_auth())
    assert r.status_code == 200 and r.json()["changed"] is False           # 幂等,不报错


# ── S-2 review 钉子 ──
def test_create_rejects_nan_and_zero_budget(client, monkeypatch):
    """review-HIGH:裸 NaN 过 json/pydantic 默认校验,min(nan,x)=nan 穿透夹紧入库 →
    预算闸对该任务失明 + 任务页序列化 500;显式 0 被旧写法 or 静默换成默认再开跑烧钱。
    两者都必须 422。"""
    c, _ = client
    from pipeline import task_queue
    monkeypatch.setattr(task_store, "create_task", lambda *a: ("tk_x", True))
    monkeypatch.setattr(task_queue, "enqueue_advance", lambda *a: None)
    r = c.post("/v1/tasks", content='{"goal": "x", "budget_cap": NaN}',
               headers={**_auth(), "Content-Type": "application/json"})
    assert r.status_code == 422
    assert c.post("/v1/tasks", json={"goal": "x", "budget_cap": 0},
                  headers=_auth()).status_code == 422
    assert c.post("/v1/tasks", json={"goal": "x", "budget_cap": -1},
                  headers=_auth()).status_code == 422
    assert c.post("/v1/tasks", json={"goal": "x", "budget_cap": None},
                  headers=_auth()).status_code == 200        # 未填 → 默认,照常


def test_create_ratelimit_uses_no_session_bucket(client, monkeypatch):
    """review 实测的投毒 DoS:常量 sid 是全用户共享的伪会话桶,一个登录用户把自己会话
    取同名烧到 $0.75 就能让全站立项 429 一天。立项必须传 sid=None。"""
    c, srv = client
    from pipeline.agentops import ratelimit
    from pipeline import task_queue
    seen = {}
    monkeypatch.setattr(ratelimit, "precheck",
                        lambda owner, ip, sid: seen.update(sid=sid) or None)
    monkeypatch.setattr(task_store, "create_task", lambda *a: ("tk_x", True))
    monkeypatch.setattr(task_queue, "enqueue_advance", lambda *a: None)
    assert c.post("/v1/tasks", json={"goal": "x"}, headers=_auth()).status_code == 200
    assert seen["sid"] is None


def test_idempotent_hit_repairs_pending_ghost(client, monkeypatch):
    """review 确认的 pending 幽灵(commit→enqueue 窗口进程死 / _execute 盲重试翻转 created):
    幂等命中的行若还停在 pending = 第一波从没投出去 → 补投一次(幂等,重复无害)。"""
    c, _ = client
    from pipeline import task_queue
    enq = []
    monkeypatch.setattr(task_store, "create_task", lambda *a: ("tk_ghost", False))
    monkeypatch.setattr(task_store, "status_of", lambda tid: ("pending", 0))
    monkeypatch.setattr(task_queue, "enqueue_advance", lambda tid, w: enq.append((tid, w)))
    r = c.post("/v1/tasks", json={"goal": "x"}, headers=_auth())
    assert r.status_code == 200 and r.json()["created"] is False
    assert enq == [("tk_ghost", 0)]                          # 幽灵被补投救活
    # 命中的是 running(正常在跑)→ 不补投
    enq.clear()
    monkeypatch.setattr(task_store, "status_of", lambda tid: ("running", 3))
    assert c.post("/v1/tasks", json={"goal": "x"}, headers=_auth()).status_code == 200
    assert enq == []
