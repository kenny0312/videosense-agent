"""SA-1:子 agent 编排(spawn_agents / pipeline.subagents)离线单测。

不调 Gemini、不碰 DB/沙箱 —— monkeypatch 掉 loop_driver 的 run_loop/make_conversation/声明,
只验编排逻辑:任务归一、工具白名单交集、无递归、扇出截断、fail-open、并行、注册与开关门。
"""
import threading

import pytest

from pipeline import config, subagents
from pipeline.loop_driver import LoopResult


def _fake_lr(answer: str) -> LoopResult:
    return LoopResult(answer=answer, steps=1, terminated="text", trace=[], ledger={}, llm_calls=1)


def _stub_loop(monkeypatch, *, capture_decls=None):
    """把子 agent 用到的 loop_driver 三件套换成 stub(不触真模型/执行器)。"""
    from pipeline import loop_driver
    decl_names = ("analyze_video", "semantic_search", "sql_query", "web_search", "spawn_agents")
    monkeypatch.setattr(loop_driver, "loop_function_declarations",
                        lambda: [{"name": n} for n in decl_names])

    def fake_make_conv(model, decls, system, image=None):
        if capture_decls is not None:
            capture_decls.append([d["name"] for d in decls])
        return object()
    monkeypatch.setattr(loop_driver, "make_conversation", fake_make_conv)
    # 不 stub _make_executor:测试都显式传 execute,fallback 永不触发;e2e 需要真的执行器闭包。


def _stub_run_loop(monkeypatch, *, fail_on=None):
    from pipeline import loop_driver

    def fake_run_loop(user_query, conv, ex, *, max_steps=None, critic=None, **k):
        if fail_on is not None and fail_on in user_query:
            raise RuntimeError("boom")
        return _fake_lr(f"OUT:{user_query}")
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)


# ── _clean_tasks ────────────────────────────────────────────────
def test_clean_tasks_normalizes_and_defaults():
    cleaned, note = subagents._clean_tasks([{"instruction": "  do X  "}], 6)
    assert note == ""
    assert cleaned == [{"instruction": "do X", "video_ids": [],
                        "tools": list(subagents._SUBAGENT_DEFAULT)}]


def test_clean_tasks_tool_intersection_drops_illegal():
    # 请求越权工具(spawn_agents=递归、python=沙箱写、show_video=交付)→ 全丢,只留白名单里的
    cleaned, _ = subagents._clean_tasks(
        [{"instruction": "x", "tools": ["analyze_video", "spawn_agents", "python", "show_video"]}], 6)
    assert cleaned[0]["tools"] == ["analyze_video"]


def test_clean_tasks_empty_or_blank_raises():
    with pytest.raises(ValueError):
        subagents._clean_tasks([], 6)
    with pytest.raises(ValueError):
        subagents._clean_tasks("nope", 6)
    with pytest.raises(ValueError):
        subagents._clean_tasks([{"instruction": "   "}, {"nope": 1}], 6)


def test_clean_tasks_truncates_to_fanout_with_note():
    tasks = [{"instruction": f"T{i}"} for i in range(5)]
    cleaned, note = subagents._clean_tasks(tasks, 2)
    assert len(cleaned) == 2 and [t["instruction"] for t in cleaned] == ["T0", "T1"]
    assert "超过扇出上限 2" in note


# ── run_fanout ──────────────────────────────────────────────────
def test_run_fanout_orders_and_maps(monkeypatch):
    _stub_loop(monkeypatch)
    _stub_run_loop(monkeypatch)
    out = subagents.run_fanout([{"instruction": "A"}, {"instruction": "B"}, {"instruction": "C"}],
                               sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert [r["instruction"] for r in out] == ["A", "B", "C"]
    assert [r["output"] for r in out] == ["OUT:A", "OUT:B", "OUT:C"]


def test_run_fanout_failopen_isolates_one_bad_agent(monkeypatch):
    _stub_loop(monkeypatch)
    _stub_run_loop(monkeypatch, fail_on="B")
    out = subagents.run_fanout([{"instruction": "A"}, {"instruction": "B"}, {"instruction": "C"}],
                               sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert out[0]["output"] == "OUT:A"
    assert "出错" in out[1]["output"]          # B 崩了但被隔离
    assert out[2]["output"] == "OUT:C"          # C 照常


def test_run_fanout_truncation_appends_system_row(monkeypatch):
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 2)
    _stub_loop(monkeypatch)
    _stub_run_loop(monkeypatch)
    out = subagents.run_fanout([{"instruction": f"T{i}"} for i in range(5)],
                               sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert len(out) == 3                         # 2 个子 agent + 1 行系统提示
    assert out[-1]["instruction"] == "⚠️(系统)"
    assert "超过扇出上限" in out[-1]["output"]


def test_gated_off_tool_falls_back_to_enabled_default(monkeypatch):
    """review#1:请求的工具被 feature flag 关掉(不在 loop_function_declarations)→ 退回默认启用子集,
    decls 绝不为空(否则子 agent 无工具凭空编)。"""
    from pipeline import loop_driver
    captured: list = []
    # 模拟 USE_WEB_SEARCH=0 / USE_SEMANTIC_SEARCH=0:只有 analyze_video/sql_query 启用
    monkeypatch.setattr(loop_driver, "loop_function_declarations",
                        lambda: [{"name": n} for n in ("analyze_video", "sql_query")])

    def cap(model, decls, system, image=None):
        captured.append([d["name"] for d in decls])
        return object()
    monkeypatch.setattr(loop_driver, "make_conversation", cap)
    monkeypatch.setattr(loop_driver, "run_loop", lambda uq, c, e, **k: _fake_lr("OUT"))
    out = subagents.run_fanout([{"instruction": "x", "tools": ["web_search"]}],
                               sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert out[0]["output"] == "OUT"                     # 没有 soft-fail
    assert captured[0]                                    # decls 非空(退回启用默认)
    assert "web_search" not in captured[0]               # 被关的工具没进去
    assert set(captured[0]) <= {"analyze_video", "sql_query"}


def test_fanout_zero_or_negative_config_does_not_crash(monkeypatch):
    """review#2:SUBAGENT_MAX_FANOUT 误配 0/负 → clamp 到 1,至少跑 1 个,不 IndexError。"""
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 0)
    _stub_loop(monkeypatch)
    _stub_run_loop(monkeypatch)
    out = subagents.run_fanout([{"instruction": "A"}, {"instruction": "B"}],
                               sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert out[0]["output"] == "OUT:A"                    # 跑了(clamp 到 1)
    assert any("超过扇出上限" in r["output"] for r in out)  # 其余截断并告知


def test_subagent_never_sees_spawn_agents(monkeypatch):
    """一层、无递归:即便 task.tools 里塞了 spawn_agents,子 agent 的声明里也不含它。"""
    captured: list = []
    _stub_loop(monkeypatch, capture_decls=captured)
    _stub_run_loop(monkeypatch)
    subagents.run_fanout([{"instruction": "x", "tools": ["analyze_video", "spawn_agents"]}],
                         sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert captured == [["analyze_video"]]      # spawn_agents 被剔除


def test_run_fanout_runs_concurrently(monkeypatch):
    """真并行证明:3 个子 agent 同步在 Barrier 前汇合;若串行,首个凑不齐 → 超时破栏 → 输出含'出错'。"""
    barrier = threading.Barrier(3, timeout=5)
    _stub_loop(monkeypatch)
    from pipeline import loop_driver

    def waiting(user_query, conv, ex, *, max_steps=None, critic=None, **k):
        barrier.wait()
        return _fake_lr(f"OUT:{user_query}")
    monkeypatch.setattr(loop_driver, "run_loop", waiting)
    out = subagents.run_fanout([{"instruction": f"T{i}"} for i in range(3)],
                               sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert all(r["output"].startswith("OUT:") for r in out)   # 都过了栏 = 确实并发


def test_run_fanout_uses_parent_execute_not_fresh(monkeypatch):
    """共享成本闸:有父 execute 时,子 agent 复用它(不新建 _make_executor,不另开配额)。"""
    _stub_loop(monkeypatch)
    from pipeline import loop_driver
    made = []
    monkeypatch.setattr(loop_driver, "_make_executor",
                        lambda *a, **k: made.append(1) or (lambda *aa, **kk: None))
    seen_ex = []

    def fake_run_loop(user_query, conv, ex, *, max_steps=None, critic=None, **k):
        seen_ex.append(ex)
        return _fake_lr("OUT")
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)

    def sentinel(*a, **k):
        return None
    subagents.run_fanout([{"instruction": "A"}], sandbox=None, trace=None, execute=sentinel)
    assert seen_ex == [sentinel]                # 复用父闭包
    assert made == []                           # 没有新建执行器


# ── 注册 + 开关门(接线正确性)─────────────────────────────────
def test_spawn_agents_registered_everywhere():
    from pipeline.dag_schema import ALL_TOOLS, Node
    from pipeline import node_specs
    assert "spawn_agents" in ALL_TOOLS
    Node(id="c0", tool="spawn_agents", inputs={"tasks": []})   # ToolName Literal 接受它(无 ValidationError)
    assert "spawn_agents" in node_specs.SPECS


def test_gate_hides_or_shows_tool(monkeypatch):
    from pipeline import loop_driver
    monkeypatch.setattr(config, "USE_SUBAGENTS", False)
    assert "spawn_agents" not in [d["name"] for d in loop_driver.loop_function_declarations()]
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    assert "spawn_agents" in [d["name"] for d in loop_driver.loop_function_declarations()]


def test_handler_gate_raises_when_off(monkeypatch):
    from pipeline import node_executor
    from pipeline.dag_schema import Node
    monkeypatch.setattr(config, "USE_SUBAGENTS", False)
    node = Node(id="c0", tool="spawn_agents", inputs={"tasks": [{"instruction": "x"}]})
    with pytest.raises(ValueError):
        node_executor._run_spawn_agents(node, None, None)


def test_end_to_end_dispatch_and_preview(monkeypatch):
    """真接线(除 LLM 外):execute_node 分发 → run_fanout;父 execute 闭包 → 大格预览不砍子 agent 结论。"""
    from pipeline import loop_driver, node_executor
    from pipeline.dag_schema import Node
    from pipeline.agentops.trace import Trace
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    _stub_loop(monkeypatch)
    long_ans = "X" * 500                                 # >80,用来验预览没被砍
    monkeypatch.setattr(loop_driver, "run_loop",
                        lambda uq, c, e, **k: _fake_lr(long_ans))
    trace = Trace(quiet=True)

    # (1) 直接过 execute_node 分发
    node = Node(id="c0", tool="spawn_agents", inputs={"tasks": [{"instruction": "A"}]})
    nr = node_executor.execute_node(node, {}, None, trace, schema=None,
                                    session_id=None, owner="anon",
                                    loop_execute=lambda *a, **k: None)
    assert nr.ok and isinstance(nr.value, list)
    assert nr.value[0] == {"instruction": "A", "output": long_ans}

    # (2) 过父 execute 闭包 → loop_execute 自穿 + 大格预览(完整 500 字进得了主脑,而非砍到 ~80)
    execute = loop_driver._make_executor(None, trace, None, None, owner="anon")
    res = execute("c0_0", "spawn_agents", {"tasks": [{"instruction": "A"}]}, {}, [])
    assert res.ok and long_ans in str(res.preview)
