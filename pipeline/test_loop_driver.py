"""M3:loop 驱动器【控制流】离线单测(注入 stub conversation + stub execute)。

不调 Gemini、不碰 DB/沙箱 —— live 路径已由 M2 spike(spikes/loop_spike.py)验过。
这里只验:收敛、句柄→upstream 解析、max_steps、重复失败终止、声明叠加、合成 DAG。
"""
import threading
import time

from pipeline import loop_driver as ld
from pipeline.loop_driver import Call, ExecResult, run_loop


class ScriptedConv:
    """按脚本依次返回 (calls, text);忽略发来的 msg。"""
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return self.script.pop(0)


def make_exec(values=None, fail=()):
    seen = []

    def execute(cid, name, inputs, upstream, uses):
        seen.append({"cid": cid, "name": name, "inputs": inputs,
                     "uses": list(uses), "upstream": dict(upstream)})
        if name in fail:
            return ExecResult(ok=False, stderr="boom")
        val = (values or {}).get(name, [{"v": 1}])
        return ExecResult(ok=True, value=val, preview=val[:1], n=len(val))

    execute.seen = seen
    return execute


def test_converges_on_text():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "SELECT 1"}, [])], None),
        ([], "答案在此"),
    ])
    r = run_loop("q", conv, make_exec(), max_steps=8)
    assert r.terminated == "text" and r.answer == "答案在此"
    assert r.steps == 1 and len(r.ledger) == 1 and r.llm_calls == 2


def test_handle_resolution_passes_upstream_in_order():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "video"}, []), Call("sql_query", {"sql": "sensor"}, [])], None),
        ([Call("merge_asof", {"left_on": "ts", "right_on": "t", "tolerance_ms": 500},
               ["c0_0", "c0_1"])], None),
        ([], "merged"),
    ])
    ex = make_exec(values={"sql_query": [{"x": 1}], "merge_asof": [{"m": 1}]})
    r = run_loop("q", conv, ex, max_steps=8)
    assert r.answer == "merged"
    merge = [c for c in ex.seen if c["name"] == "merge_asof"][0]
    assert merge["uses"] == ["c0_0", "c0_1"]                 # 顺序保留(左、右)
    assert set(merge["upstream"]) == {"c0_0", "c0_1"}        # upstream 由 ledger 解析得到


def test_max_steps_termination():
    conv = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None)] * 10)
    r = run_loop("q", conv, make_exec(), max_steps=3)
    assert r.terminated == "max_steps" and r.answer is None and r.steps == 3


def test_repeat_failure_termination():
    conv = ScriptedConv([([Call("sql_query", {"sql": "bad"}, [])], None)] * 10)
    ex = make_exec(fail={"sql_query"})
    r = run_loop("q", conv, ex, max_steps=8, repeat_limit=2)
    assert r.terminated == "repeat" and r.answer is None
    # 重复上限=2:执行了 2 次后第 3 次循环前被拦
    assert sum(1 for c in ex.seen if c["name"] == "sql_query") == 2


def test_failed_step_feeds_error_not_crash():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "bad"}, [])], None),     # 失败一次
        ([Call("sql_query", {"sql": "good"}, [])], None),    # 模型改正(不同参数 → 不算重复)
        ([], "好了"),
    ])
    ex = make_exec(fail={})                                   # 都成功;上面靠不同参数区分
    # 让第一次失败:用一个按 inputs 决定成败的执行器
    def exec2(cid, name, inputs, upstream, uses):
        ex.seen.append({"cid": cid, "name": name, "inputs": inputs})
        if inputs.get("sql") == "bad":
            return ExecResult(ok=False, stderr="syntax error")
        return ExecResult(ok=True, value=[{"v": 1}], preview=[{"v": 1}], n=1)
    r = run_loop("q", conv, exec2, max_steps=8)
    assert r.answer == "好了" and r.terminated == "text"
    assert r.trace[0]["ok"] is False and r.trace[1]["ok"] is True


def test_declarations_have_handles_without_mutating_specs():
    decls = ld.loop_function_declarations()
    merge = next(d for d in decls if d["name"] == "merge_asof")
    assert "left_result_id" in merge["parameters"]["properties"]
    assert "right_result_id" in merge["parameters"]["required"]
    plot = next(d for d in decls if d["name"] == "plot")
    assert "data_result_id" in plot["parameters"]["required"]
    show = next(d for d in decls if d["name"] == "show_video")
    assert "data_result_id" in show["parameters"]["properties"]
    assert "data_result_id" not in show["parameters"]["required"]   # show_video 句柄可选
    py = next(d for d in decls if d["name"] == "python")
    assert "data_result_id" in py["parameters"]["properties"]
    assert "data_result_id" not in py["parameters"]["required"]     # python 逃生舱句柄可选(可独立写代码)
    # SPECS 未被污染
    from pipeline.node_specs import SPECS
    assert "data_result_id" not in SPECS["plot"].parameters["properties"]


def test_loop_metrics():                                     # M6 审计指标
    lo = ld.LoopOutcome(answer="x", steps=3, terminated="text", final_tool="sql_query",
                        final_value=None, preview_value=None,
                        results={}, trace=[{"tool": "sql_query"}, {"tool": "plot"},
                                           {"tool": "sql_query"}])
    m = ld.loop_metrics(lo)
    assert m["steps"] == 3 and m["terminated"] == "text"
    assert m["tool_calls"] == {"sql_query": 2, "plot": 1}
    assert m["analyze_calls"] == 0 and m["analyze_cache_hits"] == 0   # M4.2 新增字段


def test_loop_metrics_parallel_speedup():                    # M4.2:并行加速比 = Σtool_ms / 墙钟
    lo = ld.LoopOutcome(answer="x", steps=1, terminated="text", final_tool="analyze_video",
                        final_value=None, preview_value=None, results={},
                        trace=[{"tool": "analyze_video", "ms": 300.0, "cache_hit": False},
                               {"tool": "analyze_video", "ms": 300.0, "cache_hit": True}],
                        step_walls=[320.0])                  # 两个各 300ms 的 analyze 并发 → 墙钟 ~320ms
    m = ld.loop_metrics(lo)
    assert m["analyze_calls"] == 2 and m["analyze_cache_hits"] == 1
    assert m["tool_ms"] == 600.0 and m["wall_ms"] == 320.0
    assert m["parallel_speedup"] == round(600.0 / 320.0, 2)


def test_on_step_callback_emits_events():                    # M6b:SSE 流式回调
    events = []
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "x"}, [])], None),
        ([], "done"),
    ])
    run_loop("q", conv, make_exec(), max_steps=8, on_step=events.append)
    assert [e["type"] for e in events] == ["step", "answer"]
    assert events[0]["tools"][0]["tool"] == "sql_query" and events[0]["tools"][0]["ok"] is True
    assert events[1]["text"] == "done"


# ── M4.3:并行 analyze_video ───────────────────────
def test_parallel_analyze_overlaps_and_keeps_cid_order(monkeypatch):
    """同一步 4 个 analyze 并发执行(墙钟 << 串行和),回收仍按 cid 顺序(确定性)。"""
    monkeypatch.setattr(ld.config, "MAX_ANALYZE_PARALLEL", 4)

    def execute(cid, name, inputs, upstream, uses):
        idx = int(cid.split("_")[1])
        time.sleep(0.03 * (4 - idx))                        # 后发 cid 睡更短 → 先完成
        return ExecResult(ok=True, value=[{"cid": cid}], preview=[{"cid": cid}], n=1, ms=1.0)

    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": f"v{i}", "question": f"q{i}"}, []) for i in range(4)], None),
        ([], "done"),
    ])
    r = run_loop("q", conv, execute, max_steps=4)
    assert r.answer == "done"
    assert [s["cid"] for s in r.trace] == ["c0_0", "c0_1", "c0_2", "c0_3"]   # 回收按 cid 序,不随完成序
    assert len(r.step_walls) == 1 and r.step_walls[0] < 200                  # 并行(串行约 300ms)


def test_parallel_serial_fallback_when_cap_is_one(monkeypatch):
    """MAX_ANALYZE_PARALLEL=1 → 退回串行(秒级回退开关),结果仍正确。"""
    monkeypatch.setattr(ld.config, "MAX_ANALYZE_PARALLEL", 1)
    order = []

    def execute(cid, name, inputs, upstream, uses):
        order.append(cid)
        return ExecResult(ok=True, value=[{"cid": cid}], preview=[{"cid": cid}], n=1, ms=1.0)

    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": f"v{i}", "question": f"q{i}"}, []) for i in range(3)], None),
        ([], "done"),
    ])
    r = run_loop("q", conv, execute, max_steps=4)
    assert r.answer == "done" and order == ["c0_0", "c0_1", "c0_2"]


def test_parallel_quota_exact_and_model_and_usage(monkeypatch):
    """真 _make_executor:6 个并发 analyze、配额=3 → 恰好 3 个真分析(不漏/不超);
    每个 worker 都读到主线程设的 Pro(没降级);3 次 usage 都合回主 context(没丢)。"""
    from pipeline import config, analyze_cache, usage, mcp_client as mc
    from pipeline.trace import Trace
    import perception.analyze_video_contextual as avc

    monkeypatch.setattr(config, "MAX_VIDEOS_PER_REQUEST", 3)
    monkeypatch.setattr(config, "MAX_ANALYZE_PARALLEL", 6)
    monkeypatch.setattr(mc, "query_db", lambda sql: [{"gcs_uri": "gs://b/v.mp4"}])
    analyze_cache.clear()
    usage.reset_usage()
    avc.MODEL_OVERRIDE.set("gemini-2.5-pro")                 # 主线程设 Pro

    seen_models, lk = [], threading.Lock()

    class _Meta:
        prompt_token_count, candidates_token_count, total_token_count = 10, 5, 15

    class _Resp:
        usage_metadata = _Meta()

    def fake_analyze(req, gcs):
        m = avc.MODEL_OVERRIDE.get()                         # worker 上下文里读模型
        with lk:
            seen_models.append(m)
        usage.add_usage(_Resp(), m or "gemini-2.5-flash")    # 模拟 _gemini_generate 的上报
        time.sleep(0.01)
        return avc.AnalyzeResult(answer="ok", enough="yes", confidence=0.8)

    monkeypatch.setattr(avc, "analyze", fake_analyze)
    try:
        conv = ScriptedConv([
            ([Call("analyze_video", {"video_id": f"v{i}", "question": f"q{i}"}, []) for i in range(6)], None),
            ([], "done"),
        ])
        execute = ld._make_executor(sandbox=None, trace=Trace(quiet=True), schema={}, session_id=None)
        run_loop("q", conv, execute, max_steps=4)
        assert len(seen_models) == 3                         # 配额精确:恰好 3 个真分析
        assert all(m == "gemini-2.5-pro" for m in seen_models)   # Pro 传进每个 worker(没降级)
        s = usage.summarize()
        assert s["by_model"].get("gemini-2.5-pro", {}).get("calls") == 3   # 3 次 usage 都合回(没丢)
        assert s["tokens_total"] == 45                       # 3 × 15
    finally:
        avc.MODEL_OVERRIDE.set(None)
        analyze_cache.clear()


def test_cache_hit_does_not_consume_quota(monkeypatch):
    """配额=1,同一视频分析两次:第一次真调(吃掉配额),第二次命中缓存=免费,不该被上限挡。"""
    from pipeline import config, analyze_cache, mcp_client as mc
    from pipeline.trace import Trace
    import perception.analyze_video_contextual as avc

    monkeypatch.setattr(config, "MAX_VIDEOS_PER_REQUEST", 1)
    monkeypatch.setattr(config, "MAX_ANALYZE_PARALLEL", 1)
    monkeypatch.setattr(mc, "query_db", lambda sql: [{"gcs_uri": "gs://b/v.mp4"}])
    analyze_cache.clear()
    avc.MODEL_OVERRIDE.set(None)
    calls = {"n": 0}

    class _R:
        def model_dump(self): return {"answer": "ok", "enough": "yes", "confidence": 0.8}
    def fake(req, gcs):
        calls["n"] += 1
        return _R()
    monkeypatch.setattr(avc, "analyze", fake)
    try:
        conv = ScriptedConv([
            ([Call("analyze_video", {"video_id": "vid_1", "question": "q"}, [])], None),
            ([Call("analyze_video", {"video_id": "vid_1", "question": "q"}, [])], None),  # 同视频 → 命中缓存
            ([], "done"),
        ])
        execute = ld._make_executor(sandbox=None, trace=Trace(quiet=True), schema={}, session_id=None)
        r = run_loop("q", conv, execute, max_steps=4)
        assert r.answer == "done"
        assert calls["n"] == 1                            # 只真分析了一次
        assert all(s["ok"] for s in r.trace)
        assert r.ledger["c1_0"].value.get("answer") == "ok"   # 第二步=缓存结果,不是"已达上限"note
        assert r.ledger["c1_0"].cache_hit is True
    finally:
        avc.MODEL_OVERRIDE.set(None)
        analyze_cache.clear()


# ── 自检 B:收口前的 critic 回路 ───────────────────────
def test_self_check_satisfied_returns_immediately():
    conv = ScriptedConv([([], "答案")])
    r = run_loop("q", conv, make_exec(), critic=lambda nl, a: (True, ""), max_critic=1)
    assert r.answer == "答案" and r.steps == 0


def test_self_check_not_satisfied_continues_once():
    sent = []

    class Conv:
        def __init__(self): self.n = 0
        def send(self, msg):
            sent.append(msg); self.n += 1
            return ([], "初版答案") if self.n == 1 else ([], "改进版答案")
    seen = []
    def crit(nl, ans):
        seen.append(ans)
        return (False, "还差 X") if len(seen) == 1 else (True, "")
    r = run_loop("q", Conv(), make_exec(), critic=crit, max_critic=1)
    assert r.answer == "改进版答案"                         # 介入后的改进版被采纳
    assert seen == ["初版答案"]                             # critic 只介入一次(cap=1),改进版不再复检
    assert "[自检]" in sent[1] and "还差 X" in sent[1]      # hint 被喂回


def test_self_check_max_critic_caps():
    class Conv:
        def __init__(self): self.n = 0
        def send(self, msg):
            self.n += 1
            return [], f"答案{self.n}"
    # critic 永远不满足,但 max_critic=1 → 只介入一次,第二次收敛直接返回
    r = run_loop("q", Conv(), make_exec(), critic=lambda nl, a: (False, "还不行"), max_critic=1)
    assert r.answer == "答案2"


# ── 瞬时错误重试(Pandora 对照测暴露:一次 API 抖动不该硬崩)──
def test_send_retry_recovers_from_transient():
    calls = {"n": 0}
    class _Transient(Exception):
        code = 503
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Transient("service unavailable")
        return "ok"
    import pipeline.loop_driver as m
    # 免真 sleep
    _sleep = m.time.sleep; m.time.sleep = lambda s: None
    try:
        assert m._send_with_retry(flaky, attempts=3) == "ok" and calls["n"] == 3
    finally:
        m.time.sleep = _sleep


def test_send_retry_reraises_deterministic():
    class _Bad(Exception):
        code = 400
    def bad():
        raise _Bad("invalid argument")
    import pytest
    import pipeline.loop_driver as m
    with pytest.raises(_Bad):
        m._send_with_retry(bad, attempts=3)          # 400 不是瞬时 → 立即上抛,不重试


def test_detect_lang_directive():
    from pipeline.loop_driver import _detect_lang, runtime_facts_line
    assert _detect_lang("How many videos are there?") == "en"
    assert _detect_lang("有几个视频") == "zh"
    assert _detect_lang("show me v_-02DygXbn6w") == "en"      # 混 id 仍算英文
    assert _detect_lang("看 v_-02DygXbn6w") == "zh"           # 有中文 → 中文
    assert _detect_lang("") == ""
    assert "English" in runtime_facts_line(None, nl="find falling videos")
    assert "中文" in runtime_facts_line(None, nl="找摔倒的视频")


def test_is_transient_classification():
    from pipeline.loop_driver import _is_transient
    class E(Exception):
        def __init__(self, code=None): self.code = code
    assert _is_transient(E(429)) and _is_transient(E(503))
    assert not _is_transient(E(400)) and not _is_transient(E(None))
    assert _is_transient(type("ServerError", (Exception,), {})())         # 按类名
    assert _is_transient(type("DeadlineExceeded", (Exception,), {})())


def test_self_check_critic_exception_failopen():
    def boom(nl, ans):
        raise RuntimeError("critic down")
    r = run_loop("q", ScriptedConv([([], "答案")]), make_exec(), critic=boom, max_critic=1)
    assert r.answer == "答案"                              # critic 抛错 → 视为满足,直接返回


# ── U3:运行时状态(自我认知注入)───────────────────────
def test_runtime_facts_first_turn():
    s = ld.runtime_facts_line(None)
    assert "# 运行时状态" in s and "第一轮" in s
    assert "万 token" in s                                 # 窗口以真实 config 值渲染


def test_runtime_facts_with_cum():
    cum = {"turns": 2, "tokens_total": 10000, "cost_usd": 0.003, "llm_calls": 5,
           "last": {"tokens_total": 6000, "cost_usd": 0.002}}
    s = ld.runtime_facts_line(cum)
    assert "2 轮" in s and "10,000" in s and "$0.0030" in s
    assert "上一轮 6,000" in s and "$0.0020" in s
    assert "不含正在进行的这一轮" in s                     # 诚实边界:本轮未计入


def test_loop_system_injects_runtime_facts():
    marker = "# 运行时状态\nRT_MARKER_XYZ"
    assert "RT_MARKER_XYZ" in ld._loop_system({"t": []}, None, marker)
    assert "RT_MARKER_XYZ" not in ld._loop_system({"t": []}, None, None)


# ── U5:后端工厂(gemini-3.x → google-genai;1.x/2.x → 旧 vertexai SDK)──
def test_make_conversation_backend_choice(monkeypatch):
    picked = {}
    monkeypatch.setattr(ld, "GeminiConversation", lambda m, d, s: picked.setdefault("legacy", m))
    monkeypatch.setattr(ld, "GenAIConversation", lambda m, d, s, image=None: picked.setdefault("genai", m))
    ld.make_conversation("gemini-2.5-flash", [], "s")     # 回滚路径:旧 SDK
    ld.make_conversation("gemini-3.5-flash", [], "s")     # 默认:genai
    ld.make_conversation("gemini-4-flash", [], "s")       # 未来代际也走 genai(负向匹配 1.x/2.x)
    assert picked == {"legacy": "gemini-2.5-flash", "genai": "gemini-3.5-flash"}


def test_price_table_covers_35flash():
    from pipeline import usage as u
    s = u.summarize({"gemini-3.5-flash": {"in": 1_000_000, "out": 100_000,
                                          "total": 1_100_000, "calls": 2}})
    assert abs(s["cost_usd"] - (1.50 + 0.90)) < 1e-9      # $1.5/M in + $9/M out
    assert s["tokens_cached"] == 0                        # 无 cached 键 → 兼容旧形状


def test_cached_tokens_discounted():
    """L3:命中隐式缓存的输入按折扣价计;cached ⊂ in,防负数。"""
    from pipeline import usage as u
    s = u.summarize({"gemini-3.5-flash": {"in": 1_000_000, "out": 0, "total": 1_000_000,
                                          "calls": 1, "cached": 600_000}})
    expect = 0.4 * 1.50 + 0.6 * 0.15                      # 40% 全价 + 60% 缓存价
    assert abs(s["cost_usd"] - expect) < 1e-9
    assert s["tokens_cached"] == 600_000
    dirty = u.summarize({"gemini-3.5-flash": {"in": 100, "out": 0, "total": 100,
                                              "calls": 1, "cached": 999}})
    assert dirty["cost_usd"] >= 0                          # 脏数据 cached>in 不产生负成本


# ── U6:web_search(声明门控 + 结果解析,全离线 stub)──────────
def test_web_search_declaration_gated(monkeypatch):
    from pipeline import config as cfg
    monkeypatch.setattr(cfg, "USE_WEB_SEARCH", False)
    assert "web_search" not in [d["name"] for d in ld.loop_function_declarations()]
    monkeypatch.setattr(cfg, "USE_WEB_SEARCH", True)
    assert "web_search" in [d["name"] for d in ld.loop_function_declarations()]


def _fake_grounding_client(text="ans", uri="https://s", title="src"):
    class _Web:
        pass
    web = _Web(); web.uri, web.title = uri, title
    class _Chunk:
        pass
    ch = _Chunk(); ch.web = web
    class _GM:
        pass
    gm = _GM(); gm.grounding_chunks = [ch]
    class _Cand:
        pass
    cand = _Cand(); cand.grounding_metadata = gm
    class _Resp:
        pass
    resp = _Resp(); resp.text, resp.candidates, resp.usage_metadata = text, [cand], None
    class _Models:
        def generate_content(self, **kw):
            return resp
    class _Client:
        models = _Models()
    return _Client()


def test_run_web_search_parses_answer_and_sources(monkeypatch):
    from pipeline import config as cfg, genai_client
    from pipeline import node_executor as ne
    from pipeline.dag_schema import Node
    monkeypatch.setattr(cfg, "USE_WEB_SEARCH", True)
    monkeypatch.setattr(genai_client, "_CLIENT", _fake_grounding_client())
    r = ne._run_web_search(Node(id="w1", tool="web_search", inputs={"query": "世界纪录"}))
    assert r.ok and r.value["answer"] == "ans"
    assert r.value["sources"] == [{"title": "src", "url": "https://s"}]


def test_run_web_search_gated_and_requires_query(monkeypatch):
    import pytest
    from pipeline import config as cfg
    from pipeline import node_executor as ne
    from pipeline.dag_schema import Node
    monkeypatch.setattr(cfg, "USE_WEB_SEARCH", False)
    with pytest.raises(ValueError):
        ne._run_web_search(Node(id="w1", tool="web_search", inputs={"query": "x"}))
    monkeypatch.setattr(cfg, "USE_WEB_SEARCH", True)
    with pytest.raises(ValueError):
        ne._run_web_search(Node(id="w1", tool="web_search", inputs={}))


# ── U1-T2:查询桥 —— 词表进 prompt + 护栏 ───────────────────
def test_loop_system_contains_category_vocab_and_guards():
    from pipeline.taxonomy_seed import CATEGORIES
    s = ld._LOOP_SYSTEM
    assert "大类词表" in s
    for c in ("skydiving", "cooking & food", "winter sports"):
        assert c in s                                      # 词表真的注入了
    assert str(len(CATEGORIES)) in s                       # 数量与 seed 同步
    assert "不许下「没有」的结论" in s                      # 存在性护栏
    assert "COUNT(DISTINCT video_id)" in s                 # 去重护栏
    assert "%skiing%/%snowboarding%" not in s              # 诱发重复计数的旧示例已移除


# ── 粘贴图片:多模态首轮把图附在用户消息里 ──
def test_genai_conversation_attaches_image_first_turn(monkeypatch):
    import pipeline.loop_driver as m
    sent = {}
    class _FakeChat:
        def send_message(self, payload):
            sent.setdefault("payloads", []).append(payload)
            class _R:
                candidates = []; usage_metadata = None
            return _R()
    class _FakeClient:
        def chats(self): pass
    # 直接构造 GenAIConversation 但注入 fake chat
    conv = object.__new__(m.GenAIConversation)
    from google.genai import types
    conv._types = types; conv._chat = _FakeChat(); conv._model_name = "x"; conv.tokens = 0
    conv._pending_image = (b"\x89PNG\r\n", "image/png")
    conv.send("what is this?")                    # 首轮:图 + 文本
    conv.send("follow up")                         # 次轮:纯文本,不再带图
    p1, p2 = sent["payloads"]
    assert isinstance(p1, list) and len(p1) == 2   # [image_part, text]
    assert p2 == "follow up"                        # 图只附一次
