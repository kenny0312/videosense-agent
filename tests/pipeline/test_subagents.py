"""SA-1:子 agent 编排(spawn_agents / pipeline.subagents)离线单测。

不调 Gemini、不碰 DB/沙箱 —— monkeypatch 掉 loop_driver 的 run_loop/make_conversation/声明,
只验编排逻辑:任务归一、工具白名单交集、无递归、扇出截断、fail-open、并行、注册与开关门。
"""
import threading

import pytest

from pipeline import config, subagents
from pipeline.loop_driver import ExecResult, LoopResult


def _fake_lr(answer: str) -> LoopResult:
    return LoopResult(answer=answer, steps=1, terminated="text", trace=[], ledger={}, llm_calls=1)


def _analyze_value(vid: str, answer: str, enough: str = "yes") -> dict:
    """analyze_video 成功信封的真实形状(node_executor._run_analyze_video:video_id 在前)。"""
    return {"video_id": vid, "answer": answer, "enough": enough, "confidence": 0.8,
            "evidence_ts": 12.0}


def _lr_unconverged(entries, terminated: str = "max_steps", answer=None) -> LoopResult:
    """造一个"没交出结论、但 ledger 里已经有花钱买到的东西"的 LoopResult。

    entries = [(cid, tool, ok, value)] —— 按 loop_driver.run_loop 的真实记法:
    ledger 是 cid→ExecResult(不带 tool 名),tool/ok 在 trace 的同 cid 行上。
    """
    trace = [{"cid": cid, "tool": tool, "inputs": {"video_id": (value or {}).get("video_id")
                                                   if isinstance(value, dict) else None},
              "uses": [], "ok": ok, "ms": 1.0, "cache_hit": False}
             for cid, tool, ok, value in entries]
    ledger = {cid: ExecResult(ok=ok, value=value) for cid, tool, ok, value in entries}
    return LoopResult(answer=answer, steps=4, terminated=terminated, trace=trace,
                      ledger=ledger, llm_calls=4)


def _run_one_with(monkeypatch, lr: LoopResult, **task_over) -> str:
    """把 run_loop 换成"返回这个 LoopResult",跑一个子 agent,回它给主脑的 output。

    过 copy_context:_run_one 会 set 深度/枝计数,直接跑会把状态漏进测试进程的上下文
    (跑几个用例后 _TREE_DEPTH 就 ≥2,后面的 run_fanout 全被深度闸拒),production 也是这么调的。
    """
    from contextvars import copy_context
    from pipeline import loop_driver
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    monkeypatch.setattr(loop_driver, "run_loop", lambda *a, **k: lr)
    task = {"instruction": "看 A 组", "video_ids": [], "tools": ["analyze_video"], **task_over}
    return copy_context().run(
        subagents._run_one, task, execute=None, sandbox=None, trace=None, schema=None,
        session_id=None, owner="t", model="m", max_steps=4)["output"]


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

    hits = []

    def sentinel(*a, **k):
        hits.append(a)
        return None
    sentinel.tree_guard = "G"                   # 闭包上的账本要随壳透传
    subagents.run_fanout([{"instruction": "A"}], sandbox=None, trace=None, execute=sentinel)
    assert made == []                           # 没有新建执行器
    # P0-6 起 run_loop 拿到的是父闭包外的【薄计数壳】("先自己试"闸数成功工具用);
    # 复用语义不变:壳内调的就是父闭包本体,guard 账本原样透传。
    assert len(seen_ex) == 1
    assert getattr(seen_ex[0], "tree_guard", None) == "G"
    seen_ex[0]("c1", "sql_query", {}, {}, [])   # 穿透壳直达父闭包
    assert len(hits) == 1


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


# ── 残值回收:没收敛也要把【已经买到】的 analyze 结果交回主脑 ──────────────────
def test_unconverged_hands_back_paid_analyze_results(monkeypatch):
    """子 agent 撞 max_steps:此前只回一句"未收敛",ledger 里花钱买来的 analyze 结论被丢掉,
    而配额【已经实扣】(子 agent 复用父闭包)→ 主脑既没结论也没配额补看。现在必须交回。"""
    out = _run_one_with(monkeypatch, _lr_unconverged([
        ("c1", "analyze_video", True, _analyze_value("v1", "有一只狗从左侧入画")),
        ("c2", "analyze_video", True, _analyze_value("v2", "空镜头,画面里没有人", "no")),
    ]))
    assert "未收敛:max_steps" in out                    # 诚实:没说这是它的结论
    assert "未经它综合" in out
    assert "v1" in out and "有一只狗从左侧入画" in out    # 两份都回来了
    assert "v2" in out and "空镜头,画面里没有人" in out
    body = out.splitlines()[1:]
    assert len(body) == 2                                 # 不多不少 —— 没有凭空多出的"综合结论"
    assert all(ln.startswith("- video_id=") for ln in body)


def test_gate_blocked_envelope_is_never_salvaged(monkeypatch):
    """配额/熔断拦下的调用也是 ok=True(_soft_note)。把一句"已达上限"捞回去当证据是灾难:
    靠 gate="blocked" 识别,与 _run_one 计数壳同口径。用【真的】_soft_note 造,防口径漂移。"""
    from pipeline import loop_driver
    blocked = loop_driver._soft_note("已达本请求视频分析上限(12 个),这个【没分析】。").value
    out = _run_one_with(monkeypatch, _lr_unconverged([
        ("c1", "analyze_video", True, blocked),
    ]))
    assert out == "(子 agent 未收敛:max_steps)"          # 什么也没捞到 → 退回原话
    assert "上限" not in out and "没分析" not in out


def test_empty_ledger_output_is_byte_identical(monkeypatch):
    """ledger 里什么都没有 → 与残值回收上线前【逐字节】相同(max_steps 与 repeat 两条路都要)。"""
    for term in ("max_steps", "repeat"):
        out = _run_one_with(monkeypatch, _lr_unconverged([], terminated=term))
        assert out == f"(子 agent 未收敛:{term})"


def test_only_analyze_video_is_salvaged(monkeypatch):
    """只捞花钱、占配额、不可重来的 analyze_video。sql_query/semantic_search 便宜且可重跑,
    捞回来只会白撑主脑上下文。"""
    out = _run_one_with(monkeypatch, _lr_unconverged([
        ("c1", "sql_query", True, {"video_id": "v1", "answer": "一行 SQL 结果"}),
        ("c2", "semantic_search", True, {"video_id": "v2", "answer": "一条检索命中"}),
    ]))
    assert out == "(子 agent 未收敛:max_steps)"
    assert "SQL" not in out and "检索命中" not in out


def test_failed_analyze_is_not_salvaged(monkeypatch):
    """ok=False 的 analyze(解析 gcs 失败之类)没有结论可捞。"""
    out = _run_one_with(monkeypatch, _lr_unconverged([
        ("c1", "analyze_video", False, None),
    ]))
    assert out == "(子 agent 未收敛:max_steps)"


def test_salvage_truncates_overlong_conclusion(monkeypatch):
    """单条结论超长 → 截断并标注,别让一个话痨 analyze 把主脑上下文吃光。"""
    long_ans = "长" * 5000
    out = _run_one_with(monkeypatch, _lr_unconverged([
        ("c1", "analyze_video", True, _analyze_value("v1", long_ans)),
    ]))
    assert "过长已截断" in out
    assert "长" * (subagents._SALVAGE_CELL + 1) not in out    # 确实砍在额度上
    assert len(out) < subagents._SALVAGE_CELL + 300


def test_salvage_total_budget_reports_dropped_count(monkeypatch):
    """多条结论撑破总额度 → 停在额度内、剩下的报个数。总长压在主脑侧 SUBAGENT_PREVIEW_CELL
    之下,否则会被 _preview 无声截断、连截断标注都丢。"""
    from pipeline.loop_driver import SUBAGENT_PREVIEW_CELL
    out = _run_one_with(monkeypatch, _lr_unconverged([
        (f"c{i}", "analyze_video", True, _analyze_value(f"v{i}", f"{i}号结论" + "字" * 590))
        for i in range(12)
    ]))
    assert "因长度上限没带回来" in out
    assert len(out) <= SUBAGENT_PREVIEW_CELL              # 不会被主脑侧预览无声腰斩
    assert "0号结论" in out                                # 前面的照样完整回来


def test_tree_guard_termination_also_hands_back_paid_results(monkeypatch):
    """判断:护栏硬终止那条分支【也要捞】。它不回流 r.answer 的理由是"面向用户的系统话术不是
    子任务结论"(见 test_tree_guard.py 的兄弟用例)—— 那条理由约束的是话术,不是它花钱看过的
    视频结论;而且触闸时全树都停,主脑更不可能自己补看,丢掉纯亏。话术仍然不回流。"""
    from pipeline import loop_driver
    lr = _lr_unconverged([("c1", "analyze_video", True, _analyze_value("v1", "有人在跳伞"))],
                         terminated="tree_guard",
                         answer=loop_driver._GUARD_STOP_ANSWER + "\n\n(系统) 累计 $0.9")
    out = _run_one_with(monkeypatch, lr)
    assert "无结论" in out and "有人在跳伞" in out
    assert "调高" not in out and "累计 $0.9" not in out    # 用户话术/旧钱账都没回流


# ── 步数按活儿给(_steps_for)────────────────────────────────────
def test_steps_unchanged_when_no_video_ids_named():
    """钉死不变量:子任务没点名 video_ids → 恒等于 SUBAGENT_MAX_STEPS,行为逐字节不变。"""
    assert subagents._steps_for({"video_ids": []}) == config.SUBAGENT_MAX_STEPS
    assert subagents._steps_for({}) == config.SUBAGENT_MAX_STEPS


def test_steps_scale_with_named_videos_and_hit_cap():
    """N 个视频 → N 步 analyze + 1 步收口 + 1 步周转;再多也被 CAP 截住(更多步 = 更多钱)。"""
    assert subagents._steps_for({"video_ids": ["a", "b", "c"]}) == 5
    assert subagents._steps_for({"video_ids": [str(i) for i in range(20)]}) == \
        config.SUBAGENT_MAX_STEPS_CAP
    assert config.SUBAGENT_MAX_STEPS_CAP == 8              # 默认值本身也钉一下


def test_steps_cap_misconfigured_below_baseline_never_shrinks(monkeypatch):
    """CAP 误配到基线以下也不许把步数砍到基线以下(同 SUBAGENT_MAX_FANOUT 的 clamp 防呆)。"""
    monkeypatch.setattr(config, "SUBAGENT_MAX_STEPS_CAP", 1)
    assert subagents._steps_for({"video_ids": []}) == config.SUBAGENT_MAX_STEPS
    assert subagents._steps_for({"video_ids": ["a", "b", "c"]}) == config.SUBAGENT_MAX_STEPS


def test_run_fanout_gives_each_task_its_own_step_budget(monkeypatch):
    """per-task 不是全局:同一批里点名 3 个视频的那个拿 5 步,没点名的还是 4 步。"""
    _stub_loop(monkeypatch)
    from pipeline import loop_driver
    seen: dict = {}

    def fake_run_loop(user_query, conv, ex, *, max_steps=None, critic=None, **k):
        seen[user_query] = max_steps
        return _fake_lr("OUT")
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    subagents.run_fanout([{"instruction": "A"},
                          {"instruction": "B", "video_ids": ["1", "2", "3"]}],
                         sandbox=None, trace=None, execute=lambda *a, **k: None)
    assert seen == {"A": config.SUBAGENT_MAX_STEPS, "B": 5}


def test_subagent_failure_opens_a_span_with_cause(monkeypatch):
    """T-2 真注入(非人造 trace):子 agent 抛错此前只被吞成一句文本、trace 零痕迹,
    triage 会报"0 失败"。现在必须开出 exec 层 span 且带 EXEC_TOOL_ERROR。"""
    from pipeline import loop_driver, subagents
    from pipeline.agentops import trace as T
    from pipeline.agentops import trace_report as TR

    monkeypatch.setattr(loop_driver, "run_loop",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sub boom")))
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    tr = T.Trace(quiet=True)
    out = subagents.run_fanout([{"instruction": "深看 A 组"}], sandbox=None, trace=tr,
                               execute=lambda *a, **k: None)
    assert "子 agent 出错" in out[0]["output"]          # 对主脑仍是 fail-open 文本
    spans = [s for s in tr.as_list() if s["component"] == "exec"]
    assert spans and spans[0]["cause"] == "EXEC_TOOL_ERROR" and spans[0]["status"] == "error"
    assert tr.cause_counts() == {"EXEC_TOOL_ERROR": 1}
