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


# (旧 _extract/_blocked_fallback 的空响应测试已删:该职责由 _blocked_text 承担,
#  等价覆盖见 tests/pipeline/test_e_batch_guards.py 的 blocked_* 四连测。)


def test_converges_on_text():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "SELECT 1"}, [])], None),
        ([], "答案在此"),
    ])
    r = run_loop("q", conv, make_exec(), max_steps=8)
    assert r.terminated == "text" and r.answer == "答案在此"
    assert r.steps == 1 and len(r.ledger) == 1 and r.llm_calls == 2


def test_handle_resolution_passes_upstream_in_order():
    # 控制流验证:多个上游按顺序解析进 upstream(与具体工具无关,用 python 逃生舱当载体)
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "a"}, []), Call("sql_query", {"sql": "b"}, [])], None),
        ([Call("python", {"instruction": "combine"}, ["c0_0", "c0_1"])], None),
        ([], "merged"),
    ])
    ex = make_exec(values={"sql_query": [{"x": 1}], "python": [{"m": 1}]})
    r = run_loop("q", conv, ex, max_steps=8)
    assert r.answer == "merged"
    step = [c for c in ex.seen if c["name"] == "python"][0]
    assert step["uses"] == ["c0_0", "c0_1"]                 # 顺序保留
    assert set(step["upstream"]) == {"c0_0", "c0_1"}        # upstream 由 ledger 解析得到


def test_max_steps_termination():
    """A1:步数耗尽是【交付点】不是故障 —— 绝不回 answer=None。
    回 None 会被 orchestrator 归到"可能是临时的服务波动,请再发一次",于是归因是假的、
    整份账本被丢掉、还等于劝用户把刚烧掉的这些步全额重烧一遍。"""
    conv = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None)] * 10)
    r = run_loop("q", conv, make_exec(), max_steps=3)
    assert r.terminated == "max_steps" and r.steps == 3        # 归因没被文案掩盖
    assert r.answer == ld.MAX_STEPS_ANSWER
    assert "服务波动" not in r.answer and "再发一次" not in r.answer
    assert r.ledger                                            # 整份账本留下来,交给上层交付


# ── A2:result_id 带请求短前缀 + 上游句柄失效硬失败 ──────────────
def test_result_id_carries_request_prefix():
    """跨轮撞号是静默错数据的源头:上一轮的 c0_0 与本轮的 c0_0 长得一模一样,
    模型从回放里抄一个旧 id 过来,以前会稳稳落在本轮某个【不相干】的结果上。"""
    conv = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None), ([], "done")])
    r = run_loop("q", conv, make_exec(), max_steps=4, req_short="ab12cd34")
    assert [s["cid"] for s in r.trace] == ["r_ab12cd34_c0_0"]
    assert list(r.ledger) == ["r_ab12cd34_c0_0"]
    # 不给前缀(离线纯控制流的默认)→ 与升级前逐字节一致
    conv2 = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None), ([], "done")])
    assert [s["cid"] for s in run_loop("q", conv2, make_exec(), max_steps=4).trace] == ["c0_0"]


def test_stale_result_id_hard_fails_instead_of_silent_drop():
    """A2:引用了不在本轮账本里的 result_id → 硬失败 + 明确文案,那一步【根本不执行】。
    旧写法静默丢弃句柄、工具照跑 —— "按上一轮那批视频回答"悄悄变成"对着空数据回答"。"""
    conv = ScriptedConv([
        ([Call("show_video", {}, ["r_deadbeef_c0_0"])], None),
        ([], "收口"),
    ])
    ex = make_exec()
    r = run_loop("q", conv, ex, max_steps=4, req_short="ab12cd34")
    assert ex.seen == []                                    # 没进执行器:不白花钱、不出假结果
    bad = r.ledger["r_ab12cd34_c0_0"]
    assert bad.ok is False
    assert "已失效" in bad.stderr and "r_deadbeef_c0_0" in bad.stderr
    fed = conv.sent[-1]                                     # 走既有错误回灌路径,原文喂回大脑
    assert fed[0][0] == "show_video" and "已失效" in fed[0][1]["error"]


def test_trace_carries_explicit_turn_and_console_reads_it():
    """A2:轮号写成显式 turn 字段;Loop Console 不再靠反解析 cid 字符串倒推
    —— id 格式一变(加了请求前缀)旧写法整列静默错成第 0 轮。"""
    from pipeline import loop_console as lc
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "a"}, [])], "先查库"),
        ([Call("sql_query", {"sql": "b"}, [])], "再查一遍"),
        ([], "答案"),
    ])
    r = run_loop("q", conv, make_exec(), max_steps=6, req_short="ab12cd34")
    assert [s["turn"] for s in r.trace] == [0, 1]

    class LO:                                               # run_query_loop 的最小替身
        answer, steps, terminated = r.answer, r.steps, r.terminated
        trace, step_walls, id_scrub_hits, turns = r.trace, r.step_walls, 0, r.turns
    lc._RING.clear()
    lc.record(query="q", owner="t", lo=LO, ledger=r.ledger)
    full = lc.get_trace(lc.list_traces()[0]["id"])
    assert [s["turn"] for s in full["steps"]] == [0, 1]      # 前缀 id 下轮号依然对
    by_i = {t["i"]: t for t in full["turns"]}                # 决策对话流没被压进第 0 轮
    assert by_i[0]["brain"] == "先查库" and '"sql": "a"' in by_i[0]["steps"][0]["args"]
    assert by_i[1]["brain"] == "再查一遍" and '"sql": "b"' in by_i[1]["steps"][0]["args"]


# ── A5:成功的重复调用 —— 只记录/提醒,不终止 ──────────────────
def test_repeated_success_warns_but_never_terminates():
    """失败重复才终止(seen);成功重复以前一个数字都不留(`if res.ok:` 分支完全不动 seen)。
    补上观测半边,但【不】接进 repeat_limit 的终止逻辑 —— 那会误杀合法的重复查询。"""
    conv = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None)] * 5 + [([], "答案")])
    ex = make_exec()
    r = run_loop("q", conv, ex, max_steps=8, repeat_limit=2)
    assert r.terminated == "text" and r.answer == "答案"           # 没被误杀
    assert sum(1 for c in ex.seen if c["name"] == "sql_query") == 5  # 5 次都真跑了
    nudges = [t["nudge"] for t in r.turns if t.get("nudge")]
    assert any("完全相同的参数" in n for n in nudges)               # 提醒记下来了
    notices = [resp for msg in conv.sent if isinstance(msg, list)
               for _n, resp in msg if "_system_notice" in resp]
    assert notices and "完全相同的参数" in notices[0]["_system_notice"]   # 也回喂给了大脑


def test_loop_metrics_counts_successful_repeats():
    lo = ld.LoopOutcome(answer="x", steps=2, terminated="text", final_tool="sql_query",
                        final_value=None, preview_value=None, results={},
                        trace=[{"tool": "sql_query", "inputs": {"sql": "a"}, "uses": [], "ok": True},
                               {"tool": "sql_query", "inputs": {"sql": "a"}, "uses": [], "ok": True},
                               {"tool": "sql_query", "inputs": {"sql": "b"}, "uses": [], "ok": True},
                               {"tool": "sql_query", "inputs": {"sql": "a"}, "uses": [], "ok": False}])
    assert ld.loop_metrics(lo)["repeat_ok_calls"] == 1        # 只数成功那一对,失败的归 seen


def test_repeat_failure_termination():
    """同一签名连续失败到上限 → terminated="repeat",且交【诚实的部分收口文案】不是 None。

    这条原来断言 `answer is None`,锁的正是要治的那个病:回 None 会掉进 orchestrator 的
    "瞬时波动"网 —— 谎报原因、丢掉整份 ledger、劝用户把刚烧掉的步数全额重烧。
    A4 把 analyze 失败从"假成功"改成 ok=False 之后,最贵工具的最常见失败模式
    (坏 JSON / 429 / GCS 权限)正好接到了这条绳子上,这个坑就从理论变成了常见路径。
    """
    conv = ScriptedConv([([Call("sql_query", {"sql": "bad"}, [])], None)] * 10)
    ex = make_exec(fail={"sql_query"})
    r = run_loop("q", conv, ex, max_steps=8, repeat_limit=2)
    assert r.terminated == "repeat"
    assert r.answer == ld.REPEAT_ANSWER, "repeat 仍在回 None —— 会被当成瞬时波动"
    assert "服务波动" not in (r.answer or ""), "别把'那条路不通'谎报成'服务抖动'"
    assert r.ledger, "已经买到的东西必须还在 ledger 里,交付得出去"
    # 重复上限=2:执行了 2 次后第 3 次循环前被拦
    assert sum(1 for c in ex.seen if c["name"] == "sql_query") == 2


def test_repeat_is_partial_delivery_not_a_transient_blip():
    """orchestrator 侧:repeat 必须与 max_steps 一样走【部分交付】,而不是"服务波动"。

    但 can_continue 只给 max_steps —— 那边是预算用完(缩小范围接着问有意义),
    这边是那条路不通(原样再跑还是同样结果),给"可以继续"等于劝用户再烧一遍钱。
    """
    assert "repeat" in ld.PARTIAL_TERMINATIONS and "max_steps" in ld.PARTIAL_TERMINATIONS


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
    plot = next(d for d in decls if d["name"] == "plot")
    assert "data_result_id" in plot["parameters"]["required"]   # 必填句柄注入
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
    from pipeline import config, analyze_cache, mcp_client as mc
    from pipeline.agentops import usage
    from pipeline.agentops.trace import Trace
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

    def fake_analyze(req, gcs, **kw):
        m = avc.MODEL_OVERRIDE.get()                         # worker 上下文里读模型
        with lk:
            seen_models.append(m)
        usage.add_usage(_Resp(), m or "gemini-2.5-flash")    # 模拟 _gemini_generate 的上报
        time.sleep(0.01)
        return avc.AnalyzeOutcome(
            result=avc.AnalyzeResult(answer="ok", enough="yes", confidence=0.8), attempts=1)

    monkeypatch.setattr(avc, "analyze_with_outcome", fake_analyze)
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
    from pipeline.agentops.trace import Trace
    import perception.analyze_video_contextual as avc

    monkeypatch.setattr(config, "MAX_VIDEOS_PER_REQUEST", 1)
    monkeypatch.setattr(config, "MAX_ANALYZE_PARALLEL", 1)
    monkeypatch.setattr(mc, "query_db", lambda sql: [{"gcs_uri": "gs://b/v.mp4"}])
    analyze_cache.clear()
    avc.MODEL_OVERRIDE.set(None)
    calls = {"n": 0}

    class _R:
        def model_dump(self): return {"answer": "ok", "enough": "yes", "confidence": 0.8}
    def fake(req, gcs, **kw):
        calls["n"] += 1
        return avc.AnalyzeOutcome(result=_R(), attempts=1)
    monkeypatch.setattr(avc, "analyze_with_outcome", fake)
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


def test_runtime_facts_image_directive():
    from pipeline.loop_driver import runtime_facts_line
    with_img = runtime_facts_line(None, nl="what is this?", has_image=True)
    assert "附了图片" in with_img and "别把它当成" not in with_img[:0]   # 有图 → 注入指令
    assert "超范围请求拒掉" in with_img
    assert "附了图片" not in runtime_facts_line(None, nl="hi", has_image=False)  # 无图 → 不注入


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
    monkeypatch.setattr(ld, "GeminiConversation", lambda m, d, s, image=None: picked.setdefault("legacy", m))
    monkeypatch.setattr(ld, "GenAIConversation", lambda m, d, s, image=None: picked.setdefault("genai", m))
    monkeypatch.setattr(ld, "OpenAICompatConversation", lambda m, d, s, image=None: picked.setdefault("oai", m))
    ld.make_conversation("gemini-2.5-flash", [], "s")     # 回滚/阶段A 可选:旧 SDK(image 也直通)
    ld.make_conversation("gemini-3.5-flash", [], "s")     # 默认:genai
    ld.make_conversation("gemini-4-flash", [], "s")       # 未来 gemini 代际也走 genai
    ld.make_conversation("qwen3.7-plus", [], "s")         # 阶段B:非 gemini → OpenAI 兼容端点
    assert picked == {"legacy": "gemini-2.5-flash", "genai": "gemini-3.5-flash", "oai": "qwen3.7-plus"}


def test_price_table_covers_35flash():
    from pipeline.agentops import usage as u
    s = u.summarize({"gemini-3.5-flash": {"in": 1_000_000, "out": 100_000,
                                          "total": 1_100_000, "calls": 2}})
    assert abs(s["cost_usd"] - (1.50 + 0.90)) < 1e-9      # $1.5/M in + $9/M out
    assert s["tokens_cached"] == 0                        # 无 cached 键 → 兼容旧形状


def test_cached_tokens_discounted():
    """L3:命中隐式缓存的输入按折扣价计;cached ⊂ in,防负数。"""
    from pipeline.agentops import usage as u
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


def test_runway_warning_fires_once_before_wall():
    """Phase 1 试跑实测的真缺陷:大脑第 1 步判"我做得完",逐个 analyze 烧光 16 步零答案。
    剩几步时必须提醒一次(要么并行拆、要么收口标未核查),且只提醒一次。"""
    from pipeline import loop_driver as LD

    class _Conv:
        last_thoughts = ""
        def __init__(self): self.sent = []
        def send(self, msg):
            self.sent.append(msg)
            return [LD.Call("analyze_video", {"video_id": f"v{len(self.sent)}"}, [])], None

    def _exec(*a, **k):
        return LD.ExecResult(ok=True, value={"answer": "看了"}, preview=[], n=1)
    conv = _Conv()
    r = LD.run_loop("看这一堆视频", conv, _exec, max_steps=10)
    assert r.terminated == "max_steps"
    hits = [m for m in conv.sent if "跑道" in str(m) or "还能再做" in str(m)]
    assert len(hits) == 1, f"提醒应恰好一次,实际 {len(hits)}"
    # 提醒落在最后 RUNWAY_WARN_LEFT 步内
    idx = next(i for i, m in enumerate(conv.sent) if "还能再做" in str(m))
    assert 10 - idx <= LD.RUNWAY_WARN_LEFT + 1
    nudges = [t for t in r.turns if "还能再做" in str(t.get("nudge", ""))]
    assert len(nudges) == 1                       # trace 上也留痕


def test_runway_warning_can_be_disabled(monkeypatch):
    """开关为 0 时行为与升级前逐字节一致(不变量①)。"""
    from pipeline import loop_driver as LD
    monkeypatch.setattr(LD, "RUNWAY_WARN_LEFT", 0)

    class _Conv:
        last_thoughts = ""
        def __init__(self): self.sent = []
        def send(self, msg):
            self.sent.append(msg)
            return [LD.Call("sql_query", {"sql": "s"}, [])], None
    conv = _Conv()
    LD.run_loop("q", conv, lambda *a, **k: LD.ExecResult(ok=True, value={}, preview=[], n=1),
                max_steps=6)
    assert not any("还能再做" in str(m) for m in conv.sent)


# ── C4:回灌 notice 钩子(大脑对自己的资源状态一无所知,只有撞墙那一刻才知道)────────
def _notices(conv):
    """从回喂给大脑的 function_response 里把【C4 写的那几行】捞出来。

    _system_notice 是共享载体:跑道提醒、护栏信封也写它,而且 _attach_envelope 现在是
    【合并】不是覆盖,所以一格里可能叠着好几条(换行分隔)。按行拆开、剔掉跑道提醒
    (那是另一条既有机制,不归 C4 管)——顺带也验证了合并确实没把别人挤掉。"""
    out = []
    for msg in conv.sent:
        if not isinstance(msg, list):
            continue
        for _n, resp in msg:
            for line in str(resp.get("_system_notice") or "").split("\n"):
                if line.strip() and "还能再做" not in line:
                    out.append(line)
    return out


def _quota_exec(quota, *, attempts=1, cache_hit=False, value=None):
    """带【共享配额账】的 stub 执行器 —— 形状与 _make_executor 挂 analyze_quota 的做法一致。"""
    def execute(cid, name, inputs, upstream, uses):
        if name == "analyze_video":
            if not cache_hit:
                quota["analyzed"] += 1
            return ExecResult(ok=True, value={"video_id": "v", "answer": "ok"},
                              preview=[{"answer": "ok"}], n=1,
                              attempts=attempts, cache_hit=cache_hit)
        val = value if value is not None else [{"v": 1}]
        return ExecResult(ok=True, value=val, preview=[{"v": 1}], n=1)
    execute.analyze_quota = quota
    return execute


def test_quota_balance_is_fed_back_before_the_wall_not_at_it(monkeypatch):
    """C4①:配额余额必须在【撞墙之前】就到大脑手里。

    治的病(实测):子 agent 把全树 12 个 analyze 配额吃光,主脑下一步才收到"已达本请求
    视频分析上限",只能退回拿检索片段的文字当证据。全代码库 quota["analyzed"] 只在
    【拦截那一刻】被读 —— 没有任何地方把余额告诉大脑,看不见余额就做不好资源决策。
    """
    monkeypatch.setattr(ld.config, "MAX_VIDEOS_PER_REQUEST", 12)
    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": "v1", "question": "q"}, [])], None),
        ([], "答案"),
    ])
    quota = {"analyzed": 0}
    run_loop("q", conv, _quota_exec(quota, attempts=3), max_steps=4)
    n = _notices(conv)
    assert n, "回灌链上一条 notice 都没有 —— 大脑还是看不见余额"
    assert "1/12" in n[0] and "还剩 11 个" in n[0]         # 余额,不是"已经撞墙了"
    assert "实发 3 次模型调用" in n[0]                     # attempts:重试藏在工具内部,必须说
    # 【明令砍掉】预留估价:那是熔断的内部记账口径,按定义不等于真实花费。
    assert "est_usd" not in n[0] and "actual_usd" not in n[0]


def test_quota_note_says_used_up_and_never_repeats_the_same_number(monkeypatch):
    """余额见底要说死【已用完】;而数字没变的步不复述 —— 回灌进 prompt 是要花钱的,
    同一个数每步念一遍是纯浪费(历史里那条还在,大脑读得到)。"""
    monkeypatch.setattr(ld.config, "MAX_VIDEOS_PER_REQUEST", 1)
    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": "v1", "question": "q"}, [])], None),
        ([Call("sql_query", {"sql": "a"}, [])], None),
        ([Call("sql_query", {"sql": "b"}, [])], None),
        ([], "答案"),
    ])
    run_loop("q", conv, _quota_exec({"analyzed": 0}), max_steps=6)
    n = _notices(conv)
    assert len(n) == 1 and "1/1" in n[0] and "【已用完】" in n[0]   # 只说一次,且说死


def test_no_analyze_no_note_at_all():
    """整轮一次 analyze 都没有(绝大多数请求)→ 一个字都不该多花。"""
    conv = ScriptedConv([([Call("sql_query", {"sql": "a"}, [])], None), ([], "答案")])
    run_loop("q", conv, _quota_exec({"analyzed": 0}), max_steps=4)
    assert _notices(conv) == []


def test_cache_hit_is_reported_as_free(monkeypatch):
    """命中缓存 = 一次模型都没发、也不占配额。不说的话大脑会以为自己刚烧了一格。"""
    monkeypatch.setattr(ld.config, "MAX_VIDEOS_PER_REQUEST", 12)
    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": "v1", "question": "q"}, [])], None),
        ([], "答案"),
    ])
    run_loop("q", conv, _quota_exec({"analyzed": 0}, attempts=0, cache_hit=True), max_steps=4)
    n = _notices(conv)
    assert n and "0/12" in n[0] and "1 次命中缓存(命中不占配额)" in n[0]


def test_repeat_at_two_is_reported_even_when_repeat_limit_is_larger():
    """C4②:A5 的提醒阈值跟着 repeat_limit 走 —— 配大了(比如 5)就在 2/3/4 次时
    整整齐齐地一声不吭。任务书要求 ≥2 必须让大脑知道;补位用的是【同一个】
    success_seen 计数和【同一段】_repeat_note 文案,不另起一套,也不会说两遍。"""
    conv = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None)] * 3 + [([], "答案")])
    r = run_loop("q", conv, _quota_exec({"analyzed": 0}), max_steps=6, repeat_limit=5)
    n = _notices(conv)
    assert any("第 2 次" in x for x in n)                 # ≥2 就说了(A5 自己要到第 5 次才响)
    assert r.terminated == "text"                         # 只提醒,绝不终止
    assert sum(x.count("第 2 次") for x in n) == 1         # 同一次不说两遍


def test_quota_blocked_call_does_not_trigger_a_second_bill(monkeypatch):
    """被配额闸拦下的那次 analyze 连视频都没看,不算一次 analyze —— 它自己带回的闸门信封
    已经把话说尽了("已达上限、别再调"),再触发一条余额播报是把同一件事收两遍钱。"""
    monkeypatch.setattr(ld.config, "MAX_VIDEOS_PER_REQUEST", 12)
    note = "已达本请求视频分析上限,这个【没分析】。" + "补" * 60   # 远超默认 80 字/格

    def execute(cid, name, inputs, upstream, uses):
        return ld._soft_note(note)
    execute.analyze_quota = {"analyzed": 0}
    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": "v1", "question": "q"}, [])], None),
        ([], "答案"),
    ])
    run_loop("q", conv, execute, max_steps=6)
    assert not any("配额已用" in x for x in _notices(conv))
    # 但信封本身一个字都没少(C5 的规则在这条链上照样成立)
    wire = [m for m in conv.sent if isinstance(m, list)]
    _name, payload = wire[0][-1]
    assert len(str(payload["preview"][0]["answer"])) == len(note)


def test_truncation_note_is_relayed_only_when_the_preview_swallowed_it():
    """C4③:B4 薄壳的 `_note`(那句"别把带回的行数当总数")。

    sql_query 走 _preview_sql 时它已经【单独成格、原文】进了 preview,那就不再往
    _system_notice 里说第二遍 —— 回灌是要花 token 的,说两遍等于白花一遍。
    换成走默认 80 字/格的预览,note 必被腰斩成半句 —— 那种情况才由这条回灌兜底。
    判据看的是"这段字在不在 preview 里",【不认工具名】,所以以后哪个工具开始返薄壳
    都自动接住。
    """
    from pipeline.node_executor import _truncated_shell
    shell = _truncated_shell([{"id": 1}], {"returned": 1, "total_seen": 2001,
                                           "reason": "row_cap"})
    note = shell["_note"]

    def _exec_with(preview):
        def execute(cid, name, inputs, upstream, uses):
            return ExecResult(ok=True, value=shell, preview=preview, n=1)
        return execute

    script = [([Call("sql_query", {"sql": "a"}, [])], None), ([], "答案")]
    # ① 原文已经在 preview 里(_preview_sql 的形状)→ 不重复
    conv = ScriptedConv(list(script))
    run_loop("q", conv, _exec_with([{"id": "1"}, {"_note": note}]), max_steps=6)
    assert not any("结果被截断" in x for x in _notices(conv))
    # ② 预览把它腰斩了(默认 80 字/格的形状)→ 原文补进指令通道
    conv2 = ScriptedConv(list(script))
    run_loop("q", conv2, _exec_with([{"_note": note[:80]}]), max_steps=6)
    relayed = [x for x in _notices(conv2) if "结果被截断" in x]
    assert relayed and note in relayed[0]          # 复用薄壳自己的措辞,且一字不改


def test_context_note_never_talks_over_the_cost_guard(monkeypatch):
    """共享载体 _system_notice 的优先级:护栏最高(那是钱的事)。触闸后护栏信封是
    【收口指令】("别再调工具"),这时候再补一句"还剩 N 个配额"就是在拆护栏的台 ——
    所以触闸后 C4 一个字都不加;而护栏信封本身必须排在最前面、且不许顶掉别人写的。"""
    from pipeline.agentops.treeguard import TreeGuard
    monkeypatch.setattr(ld.config, "MAX_VIDEOS_PER_REQUEST", 12)

    # ① 触闸后不再回灌余额
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    g._trip("budget", "测试触闸")
    conv = ScriptedConv([
        ([Call("analyze_video", {"video_id": "v1", "question": "q"}, [])], None),
        ([], "收口"),
    ])
    run_loop("q", conv, _quota_exec({"analyzed": 0}), max_steps=4, guard=g)
    assert not any("配额已用" in x for x in _notices(conv))

    # ② 合并而不是顶掉,且后写的(= 钱)排在最前
    merged = ld._attach_envelope([("t", {"_system_notice": "先写的"})], "[系统·成本护栏] 停")
    got = merged[0][1]["_system_notice"]
    assert got.startswith("[系统·成本护栏] 停")     # 钱的事排最前
    assert "先写的" in got                          # 先写的没被无声顶掉
