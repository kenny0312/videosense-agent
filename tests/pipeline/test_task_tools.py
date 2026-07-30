"""S-6(主脑立项工具位)+ S-9(完成回流 + get_task_report)+ S-10(续作)。

验收(任务书):
S-6 判据不含任何美元知识;立项成功后收口不重复立项;USE_TASK_TOOL=0 工具消失。
S-9 任务完成后用户发任意消息主脑能主动提及;注入行 ≤120 字;已通报不再重复注入。
S-10 续作任务的规划波 prompt 含父报告;只能续自己的任务。
离线,零 DB 零 API。
"""
import pytest

from pipeline import config, loop_driver, node_specs, task_runner as TR, task_store
from pipeline.dag_schema import ALL_TOOLS, Node
from pipeline.node_executor import _run_get_task_report, _run_start_background_task


# ── 注册四步齐(工具存在性)──
def test_both_tools_registered_everywhere():
    for t in ("start_background_task", "get_task_report"):
        assert t in ALL_TOOLS                                  # ①dag_schema
        assert t in node_specs.SPECS                           # ②node_specs
        Node(id="c1", tool=t, inputs={}, depends_on=[])        # ③schema 认这个 tool 名
    # ④执行器分派:两个 _run_* 存在且被 dispatch 引用
    import inspect
    from pipeline import node_executor
    src = inspect.getsource(node_executor.execute_node)
    assert 'node.tool == "start_background_task"' in src
    assert 'node.tool == "get_task_report"' in src


def _decl_names(monkeypatch, **flags):
    for k, v in flags.items():
        monkeypatch.setattr(config, k, v)
    return {d["name"] for d in loop_driver.loop_function_declarations()}


def test_tools_hidden_when_flags_off(monkeypatch):
    """关掉 = 工具从声明里消失(零残留),不是看得见调了才报错。"""
    names = _decl_names(monkeypatch, USE_TASKS=False, USE_TASK_TOOL=False)
    assert "start_background_task" not in names and "get_task_report" not in names
    # USE_TASKS 开、工具位没开 → 只有读报告可见(回流通知仍需要它)
    names = _decl_names(monkeypatch, USE_TASKS=True, USE_TASK_TOOL=False)
    assert "start_background_task" not in names and "get_task_report" in names
    names = _decl_names(monkeypatch, USE_TASKS=True, USE_TASK_TOOL=True)
    assert "start_background_task" in names and "get_task_report" in names


def test_spawn_prompt_has_no_dollar_knowledge():
    """红队:单价塞 prompt 违反 keep-prompts-adaptive —— 判据只讲"装不下/用户明说",
    不讲钱。同时必须有"立项后收口别重做"的收尾纪律。"""
    d = node_specs.SPECS["start_background_task"].planner_desc
    for money in ("$", "美元", "刀", "0.0", "成本"):
        assert money not in d, f"判据里出现了美元知识:{money}"
    assert "别" in d and "重复立项" in d
    assert "能当场答完" in d                                   # 反向判据(什么时候别用)


# ── S-6 执行层 ──
def _flags(monkeypatch, tasks=True, tool=True):
    monkeypatch.setattr(config, "USE_TASKS", tasks)
    monkeypatch.setattr(config, "USE_TASK_TOOL", tool)


def test_tool_creates_and_enqueues_wave0(monkeypatch):
    _flags(monkeypatch)
    from pipeline import task_queue
    seen = {}
    monkeypatch.setattr(task_store, "create_task",
                        lambda o, g, cap, parent_task_id=None:
                        seen.update(owner=o, goal=g, cap=cap, parent=parent_task_id)
                        or ("tk_1", True))
    monkeypatch.setattr(task_queue, "enqueue_advance", lambda t, w: seen.update(enq=(t, w)))
    node = Node(id="c1", tool="start_background_task",
                inputs={"goal": "把所有滑雪视频整理成报告"}, depends_on=[])
    res = _run_start_background_task(node, owner="kenny")
    assert res.ok and res.value["created"] is True and res.value["task_id"] == "tk_1"
    assert seen["enq"] == ("tk_1", 0) and seen["owner"] == "kenny"
    assert seen["cap"] <= min(config.TASK_MAX_CAP_USD, config.RL_TASK_DAILY_COST_USD)
    assert "收口" in res.value["note"]                         # 回喂里教它收口别重做


def test_tool_idempotent_hit_tells_brain_not_to_redo(monkeypatch):
    _flags(monkeypatch)
    from pipeline import task_queue
    enq = []
    monkeypatch.setattr(task_store, "create_task", lambda *a, **k: ("tk_dup", False))
    monkeypatch.setattr(task_queue, "enqueue_advance", lambda *a: enq.append(a))
    node = Node(id="c1", tool="start_background_task", inputs={"goal": "x"}, depends_on=[])
    res = _run_start_background_task(node, owner="kenny")
    assert res.value["created"] is False and enq == []          # 不重复投波
    assert "别重复立项" in res.value["note"]


def test_tool_enqueue_failure_is_honest(monkeypatch):
    """fail-closed:投不出去 → paused_error + 软失败,且【明确禁止】主脑假装已在做。"""
    _flags(monkeypatch)
    from pipeline import task_queue
    ops = []
    monkeypatch.setattr(task_store, "create_task", lambda *a, **k: ("tk_boom", True))
    monkeypatch.setattr(task_store, "set_status", lambda t, s: ops.append(s) or True)
    monkeypatch.setattr(task_store, "add_event", lambda t, k, p=None: ops.append(k))
    monkeypatch.setattr(task_queue, "enqueue_advance",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    node = Node(id="c1", tool="start_background_task", inputs={"goal": "x"}, depends_on=[])
    with pytest.raises(ValueError, match="别假装"):
        _run_start_background_task(node, owner="kenny")
    assert "paused_error" in ops and "enqueue_failed" in ops


def test_tool_respects_flags_and_needs_goal(monkeypatch):
    _flags(monkeypatch, tasks=True, tool=False)
    node = Node(id="c1", tool="start_background_task", inputs={"goal": "x"}, depends_on=[])
    with pytest.raises(ValueError, match="USE_TASK"):
        _run_start_background_task(node, owner="kenny")
    _flags(monkeypatch)
    empty = Node(id="c1", tool="start_background_task", inputs={"goal": " "}, depends_on=[])
    with pytest.raises(ValueError, match="goal"):
        _run_start_background_task(empty, owner="kenny")


def test_tool_parent_must_be_own_task(monkeypatch):
    """S-10:只能续【自己】的任务(越权续作 = 读到别人的报告)。"""
    _flags(monkeypatch)
    monkeypatch.setattr(task_store, "owner_of", lambda t: "别人")
    node = Node(id="c1", tool="start_background_task",
                inputs={"goal": "再细化一版", "parent_task_id": "tk_other"}, depends_on=[])
    with pytest.raises(ValueError, match="不属于你"):
        _run_start_background_task(node, owner="kenny")


# ── S-9 get_task_report ──
def test_get_report_owner_scoped_and_status_aware(monkeypatch):
    _flags(monkeypatch)
    monkeypatch.setattr(task_store, "report_of", lambda o, t: None)
    node = Node(id="c1", tool="get_task_report", inputs={"task_id": "tk_x"}, depends_on=[])
    with pytest.raises(ValueError, match="查不到"):
        _run_get_task_report(node, owner="kenny")
    monkeypatch.setattr(task_store, "report_of",
                        lambda o, t: {"task_id": t, "status": "running", "goal": "g",
                                      "report": None, "done": {}, "spent_usd": 0.1})
    res = _run_get_task_report(node, owner="kenny")
    assert res.ok and "还没做完" in res.value["note"]           # 未完成 → 说进度,不编报告
    monkeypatch.setattr(task_store, "report_of",
                        lambda o, t: {"task_id": t, "status": "done", "goal": "g",
                                      "report": "全文报告", "done": {"1": {}},
                                      "spent_usd": 0.3})
    res2 = _run_get_task_report(node, owner="kenny")
    assert res2.value["report"] == "全文报告"


# ── S-9 完成回流注入 ──
def test_done_notice_within_spec_length_at_worst_case(monkeypatch):
    """规格 S-9:注入行 ≤120 字(每轮常驻税)。按【最坏情况】量:满配 NOTICE_LIMIT 条 +
    长 goal + 真实 task_id 宽度。review 实测旧模板 limit=3 时 241 字,结构上不可能达标。"""
    monkeypatch.setattr(config, "USE_TASKS", True)
    long_goal = "把库里所有滑雪视频按危险动作密度排序并逐条给证据" * 3
    monkeypatch.setattr(task_store, "unnotified_done",
                        lambda owner, limit=2: [("tk_" + "a" * 16, long_goal)] * limit)
    line, ids = loop_driver.task_done_notice("kenny")
    assert len(line) <= 120, f"注入行 {len(line)} 字,超了规格的 120"
    assert len(ids) == loop_driver.NOTICE_LIMIT
    assert "get_task_report" in line and "tk_" in line


def test_done_notice_does_not_settle_until_delivery(monkeypatch):
    """review-HIGH:旧写法在 context 组装期就销账 —— 请求崩了/空答/用户 Stop 时通知
    永久丢失且无兜底。销账必须等答案确实产出(两阶段)。"""
    monkeypatch.setattr(config, "USE_TASKS", True)
    marked = []
    monkeypatch.setattr(task_store, "unnotified_done",
                        lambda owner, limit=2: [("tk_1", "整理滑雪视频")])
    monkeypatch.setattr(task_store, "mark_notified", lambda ids: marked.extend(ids))
    line, ids = loop_driver.task_done_notice("kenny")
    assert ids == ["tk_1"] and marked == []                    # 查了但没销账
    import inspect
    src = inspect.getsource(loop_driver.run_query_loop)
    assert "mark_notified" in src                              # 销账在交付点
    assert src.index("task_done_notice") < src.index("mark_notified")
    # 且被答案非空守着
    seg = src[src.index("notice_ids and"):src.index("mark_notified")]
    assert "answer" in seg


def test_done_notice_empty_and_failopen(monkeypatch):
    monkeypatch.setattr(config, "USE_TASKS", True)
    monkeypatch.setattr(task_store, "unnotified_done", lambda owner, limit=2: [])
    assert loop_driver.task_done_notice("kenny") == ("", [])
    monkeypatch.setattr(task_store, "unnotified_done",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert loop_driver.task_done_notice("kenny") == ("", [])   # fail-open,不拖垮对话
    monkeypatch.setattr(config, "USE_TASKS", False)
    assert loop_driver.task_done_notice("kenny") == ("", [])   # 关着时零开销


def test_loop_system_carries_notice():
    s = loop_driver._loop_system({}, None, None, task_notice="# 后台任务通知(系统)\nX 完成了")
    assert "后台任务通知" in s
    assert "后台任务通知" not in loop_driver._loop_system({}, None, None)


# ── S-10 续作:父报告进规划波 ──
def test_parent_context_injected_into_planning(monkeypatch):
    monkeypatch.setattr(task_store, "report_of",
                        lambda o, t: {"goal": "上一版目标", "status": "done",
                                      "report": "上一版的报告正文",
                                      "done": {"1": {"answer": "子结论A"}},
                                      "spent_usd": 0.3})
    ctx = TR._parent_context({"owner": "kenny", "parent_task_id": "tk_parent"})
    assert "上一版的报告正文" in ctx and "子结论A" in ctx
    assert len(ctx) <= TR.PARENT_CTX_MAX
    assert TR._parent_context({"owner": "kenny", "parent_task_id": None}) is None
    monkeypatch.setattr(task_store, "report_of",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    assert TR._parent_context({"owner": "k", "parent_task_id": "tk_p"}) is None   # fail-open


def test_plan_goal_receives_parent_ctx():
    """规划波把父上下文喂给 plan_goal(trace 可证的那一环)。"""
    seen = {}

    def fake_plan(goal, notes, parent_ctx=None):
        seen["ctx"] = parent_ctx
        return [{"id": 1, "instruction": "做", "video_ids": []}]
    import pipeline.task_runner as _tr
    orig_plan, orig_parent = _tr.plan_goal, _tr._parent_context
    _tr.plan_goal = fake_plan
    _tr._parent_context = lambda task: "父报告快照"
    try:
        from tests.pipeline.test_task_runner import FakeDB, _row
        db = FakeDB(_row(wave_n=0, plan={"remaining": [], "done": {}},
                         parent_task_id="tk_parent"))
        orig_exec = _tr._execute
        _tr._execute = db.execute
        import pipeline.task_store as store
        orig_store_exec = store._execute
        store._execute = db.execute
        from pipeline import task_queue
        orig_enq = task_queue.enqueue_advance
        task_queue.enqueue_advance = lambda *a, **k: None
        try:
            _tr.advance("tk_1", 0)
        finally:
            _tr._execute, store._execute = orig_exec, orig_store_exec
            task_queue.enqueue_advance = orig_enq
    finally:
        _tr.plan_goal, _tr._parent_context = orig_plan, orig_parent
    assert seen["ctx"] == "父报告快照"


def test_goal_hash_separates_iterations():
    """续作的幂等键含 parent:同一句"再细化一版"针对不同父任务是不同的活,
    不该被活跃幂等索引互相挡掉。"""
    a = task_store.goal_hash("kenny", "再细化一版", "tk_p1")
    b = task_store.goal_hash("kenny", "再细化一版", "tk_p2")
    c = task_store.goal_hash("kenny", "再细化一版", None)
    assert len({a, b, c}) == 3


# ── S-6/S-9 review 钉子 ──
def test_guest_blocked_on_both_tool_paths(monkeypatch):
    """review-HIGH:端点的 guest 403 红线在工具路被绕过 —— 游客能立后台任务,
    且 guest 是【多人共用】身份,报告会在游客之间串号。声明层隐藏 + 执行层拒绝,两道。"""
    _flags(monkeypatch)
    names = {d["name"] for d in loop_driver.loop_function_declarations(owner="guest")}
    assert "start_background_task" not in names and "get_task_report" not in names
    names_ok = {d["name"] for d in loop_driver.loop_function_declarations(owner="kenny")}
    assert "start_background_task" in names_ok
    n1 = Node(id="c1", tool="start_background_task", inputs={"goal": "x"}, depends_on=[])
    with pytest.raises(ValueError, match="游客"):
        _run_start_background_task(n1, owner="guest2")         # guest 前缀即游客
    n2 = Node(id="c2", tool="get_task_report", inputs={"task_id": "tk_1"}, depends_on=[])
    with pytest.raises(ValueError, match="游客"):
        _run_get_task_report(n2, owner="guest")


def test_tool_goal_length_gate_matches_endpoint(monkeypatch):
    """工具路与 /v1/tasks 端点同闸(review:工具路把 goal ≤2000 的校验也绕过了)。"""
    _flags(monkeypatch)
    node = Node(id="c1", tool="start_background_task",
                inputs={"goal": "x" * 2001}, depends_on=[])
    with pytest.raises(ValueError, match="太长"):
        _run_start_background_task(node, owner="kenny")


def test_report_reaches_the_brain_uncut(monkeypatch):
    """review-HIGH(P0-3 同款坑换载体):回喂大脑的是 preview 不是 value —— 报告落进
    默认 80 字/格会被砍成半句,大脑拿残句自信作答。塑形 + 大格,全文必须真进 prompt。"""
    _flags(monkeypatch)
    from pipeline import node_executor
    long_report = "这是一份很长的报告。" * 40                    # 400 字
    monkeypatch.setattr(task_store, "report_of",
                        lambda o, t: {"task_id": t, "goal": "g", "status": "done",
                                      "report": long_report,
                                      "done": {"1": {"answer": "子结论" * 50}},
                                      "spent_usd": 0.3})
    monkeypatch.setattr(node_executor, "execute_node",
                        lambda node, *a, **k: _run_get_task_report(node, owner="kenny"))
    from pipeline.agentops import trace as T
    ex = loop_driver._make_executor(sandbox=None, trace=T.Trace(quiet=True), schema={},
                                    session_id=None, owner="kenny")
    res = ex("c1", "get_task_report", {"task_id": "tk_1"}, {}, [])
    blob = str(res.preview)
    assert long_report in blob, "报告没能完整进大脑(被 preview 截断了)"
    assert "子结论" in blob                                     # 子任务结论也在
