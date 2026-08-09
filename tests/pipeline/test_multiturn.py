"""
多轮编排分支的轻量测试 —— 全用桩,不依赖 GCP / DB。
    python -m pipeline.test_multiturn

单 loop 主路(V1-C 清理后唯一路径;Router/skills 已删):
  有会话 → 建 transcript 回放并【透传】给 loop;turn_type 据回放派生(有上文=followup)
  无会话 → 向后兼容(到达 loop、session_id=None、不建回放)
  闲聊/超范围/模糊 → 也进 loop(由 loop 用完整上文自判,不再有前置门)
"""
from __future__ import annotations

import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pipeline.loop_driver as loop_driver
import pipeline.loop_memory as loop_memory
import pipeline.orchestrator as orch
from pipeline.loop_driver import LoopOutcome
from pipeline.session import Session


# ── 桩:替换 orch.mcp_client / loop 入口 / 记忆侧 ─────────────────
def _stub_mcp():
    saved = orch.mcp_client
    orch.mcp_client = types.SimpleNamespace(
        get_schema=lambda: {"video_facts": [{"column": "id", "type": "int"}]})
    return saved


_REPLAY_SENTINEL = "# 多轮上下文\n## 第1轮\n用户:之前的问题"


def _stub_loop(run, replay=_REPLAY_SENTINEL):
    """把 loop 入口换成 run,并让记忆侧 inert(离线)。build_loop_context 返回哨兵字符串
    (或 None)→ 可断言它【真被透传】进 run_query_loop 的 replay_context。"""
    calls = {"replay": 0, "passed_ctx": "UNSET"}
    saved = (loop_driver.run_query_loop,
             loop_memory.build_loop_context, loop_memory.record_loop_turn)

    def _wrapped_run(nl, **kw):
        calls["passed_ctx"] = kw.get("replay_context")     # 捕获编排层实际传进来的回放
        return run(nl, **kw)
    loop_driver.run_query_loop = _wrapped_run

    def _fake_ctx(*a, **k):
        calls["replay"] += 1
        return replay
    loop_memory.build_loop_context = _fake_ctx
    loop_memory.record_loop_turn = lambda *a, **k: None
    return saved, calls


def _restore_loop(saved):
    (loop_driver.run_query_loop,
     loop_memory.build_loop_context, loop_memory.record_loop_turn) = saved


def _reached(nl, **k):
    """到达 loop 的哨兵:返回可辨识的 answer(不再靠抛异常 —— 异常现会被优雅降级)。"""
    return LoopOutcome(answer="__REACHED__", steps=1, terminated="text", final_tool="sql_query",
                       final_value=[{"x": 1}], preview_value=[{"x": 1}], results={}, trace=[])


# ── 有会话:回放建好并透传;turn_type 派生 followup ───────────────
def test_session_turn_reaches_loop_with_replay():
    s = Session("t")
    m = _stub_mcp()
    sl, calls = _stub_loop(_reached)
    try:
        r = orch.run_query("plot those", session=s)
        assert r["status"] == "ok" and r["answer"] == "__REACHED__", r
        assert r["turn_type"] == "followup"               # 有回放 → followup(零模型调用派生)
        assert calls["replay"] == 1
        assert calls["passed_ctx"] == _REPLAY_SENTINEL    # 回放【真透传】进 loop
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 首轮(有会话但回放为空)→ turn_type=new,仍进 loop ─────────────
def test_first_turn_derives_new():
    s = Session("t")
    m = _stub_mcp()
    sl, calls = _stub_loop(_reached, replay=None)         # 空会话 → 无回放
    try:
        r = orch.run_query("你好", session=s)              # 闲聊也进 loop(无前置门)
        assert r["status"] == "ok" and r["answer"] == "__REACHED__", r
        assert r["turn_type"] == "new"
        assert calls["passed_ctx"] is None
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 无 session:向后兼容(不建回放、session_id=None)─────────────
def test_no_session_backcompat():
    m = _stub_mcp()
    sl, calls = _stub_loop(_reached)
    try:
        r = orch.run_query("how many videos")
        assert r["status"] == "ok" and r["answer"] == "__REACHED__", r
        assert r["session_id"] is None and r["turn_type"] == "new"
        assert calls["replay"] == 0 and calls["passed_ctx"] is None
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 短语境回复(ok / 我想看):无前置门可误杀,直接带回放进 loop ────
def test_context_dependent_shorts_reach_loop():
    m = _stub_mcp()
    for q in ("ok", "我想看"):
        s = Session("t")
        sl, calls = _stub_loop(_reached)
        try:
            r = orch.run_query(q, session=s)
            assert r["status"] == "ok" and r["answer"] == "__REACHED__", (q, r)
            assert calls["passed_ctx"] == _REPLAY_SENTINEL
        finally:
            _restore_loop(sl)
    orch.mcp_client = m


# ── 成功轮:轮号推进 + 结果形状 ────────────────────────────────
def test_success_turn_advances_and_shapes():
    s = Session("t")
    m = _stub_mcp()

    def fake_loop(nl, **kw):
        return LoopOutcome(answer="共 1 条 skiing 视频", steps=1, terminated="text",
                           final_tool="sql_query",
                           final_value=[{"id": 1, "predicate": "skiing"}],
                           preview_value=[{"id": 1, "predicate": "skiing"}],
                           results={},
                           trace=[{"cid": "c0_0", "tool": "sql_query",
                                   "inputs": {}, "uses": [], "ok": True}])
    sl, calls = _stub_loop(fake_loop)
    try:
        r = orch.run_query("它是什么类型?", session=s)
        assert r["status"] == "ok" and r["answer"] == "共 1 条 skiing 视频"
        assert r["session_id"] == "t" and r["turn_type"] == "followup"
        assert s._turn_no == 1                            # 轮号推进(供 record_loop_turn)
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 瞬时失败 → 优雅重试提示(不甩 error 卡片;Pandora 对照测的镜像教训)──
def test_loop_exception_degrades_to_retry_message():
    s = Session("t")
    m = _stub_mcp()

    def boom(*a, **k):
        raise RuntimeError("transient blip")
    sl, calls = _stub_loop(boom)
    try:
        r = orch.run_query("how many videos", session=s)
        assert r["status"] == "ok"                        # 不是 error 卡片
        assert "服务波动" in r["answer"]                   # 优雅重试提示
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── A1:步数耗尽 = 部分交付,不是"服务波动" ────────────────────────
def _out_of_steps_outcome(videos):
    """步数耗尽的真实形状:工具已经跑完、show_video 已经把视频摆到了屏幕上,
    只差最后归纳那一步(实证 7 例 max_steps 空答里 4 例如此)。"""
    from pipeline.loop_driver import ExecResult
    cid = "r_ab12cd34_c0_0"
    ledger = {cid: ExecResult(ok=True, value={"items": videos}, n=len(videos), videos=videos)}
    trace = [{"cid": cid, "tool": "show_video", "inputs": {"video_ids": [v["video_id"] for v in videos]},
              "uses": [], "ok": True, "turn": 0, "ms": 1.0, "cache_hit": False}]
    return LoopOutcome(answer=loop_driver.MAX_STEPS_ANSWER, steps=16, terminated="max_steps",
                       final_tool="show_video", final_value=None, preview_value=None,
                       results=ledger, trace=trace)


def test_max_steps_is_partial_delivery_not_a_transient_blip():
    """步数耗尽必须【单独分流】,不许落进空答重试网。旧行为三重损害:
    ① 谎报成"临时的服务波动";② 劝用户把刚烧掉的 16 步全额重烧;
    ③ 没传 results → 整份账本被丢掉,屏幕上已经摆出来的视频凭空消失。"""
    s = Session("t")
    m = _stub_mcp()
    videos = [{"video_id": "v006", "n": 1}, {"video_id": "v007", "n": 2}]
    sl, calls = _stub_loop(lambda nl, **kw: _out_of_steps_outcome(videos))
    recorded = []
    loop_memory.record_loop_turn = lambda *a, **k: recorded.append(a)   # _restore_loop 会还原
    try:
        r = orch.run_query("把所有跳伞视频都看一遍", session=s)
        assert r["status"] == "ok"
        assert "服务波动" not in r["answer"] and "再发一次" not in r["answer"]
        assert r["videos"] == videos            # ★ 已经 show_video 摆出来的视频仍在响应里
        assert r["can_continue"] is True        # 让上层/用户决定要不要继续
        assert r["loop"]["terminated"] == "max_steps"      # 归因没被诚实文案掩盖
        assert recorded and recorded[0][-1] == loop_driver.MAX_STEPS_ANSWER   # 这一轮落了 transcript
    finally:
        _restore_loop(sl); orch.mcp_client = m


def test_max_steps_without_answer_still_gets_honest_text():
    """orchestrator 侧兜底:即便 loop 那头(旧版本/别的实现)仍回 answer=None,
    步数耗尽也不能变成"服务波动"。"""
    s = Session("t")
    m = _stub_mcp()

    def unconverged(nl, **kw):
        return LoopOutcome(answer=None, steps=16, terminated="max_steps", final_tool=None,
                           final_value=None, preview_value=None, results={}, trace=[])
    sl, calls = _stub_loop(unconverged)
    try:
        r = orch.run_query("something hard", session=s)
        assert r["status"] == "ok" and "服务波动" not in r["answer"]
        assert r["answer"] == loop_driver.MAX_STEPS_ANSWER and r["can_continue"] is True
    finally:
        _restore_loop(sl); orch.mcp_client = m


def test_empty_answer_without_max_steps_still_gets_retry_message():
    """反向锁:非 max_steps 的空答仍走原来的优雅重试提示(空答网没被 A1 拆掉)。"""
    s = Session("t")
    m = _stub_mcp()

    def blank(nl, **kw):
        return LoopOutcome(answer="   ", steps=1, terminated="text", final_tool=None,
                           final_value=None, preview_value=None, results={}, trace=[])
    sl, calls = _stub_loop(blank)
    try:
        r = orch.run_query("something", session=s)
        assert r["status"] == "ok" and "服务波动" in r["answer"]
        assert r["can_continue"] is False
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 回放真正进了 loop 的 system prompt(_loop_system 拼接)─────────────
def test_loop_system_splices_replay_context():
    from pipeline.loop_driver import _loop_system
    schema = {"video_facts": [{"column": "id"}]}
    s_none = _loop_system(schema, None)
    s_ctx = _loop_system(schema, _REPLAY_SENTINEL)
    assert _REPLAY_SENTINEL not in s_none
    assert _REPLAY_SENTINEL in s_ctx
    assert s_ctx.startswith(s_none)                       # 静态段不变,回放追加在尾部(缓存前提)


# ── Pro 模式:pro_video 透传成 analyze_video 的模型覆盖 ─────────────
def test_pro_video_sets_analyze_model_override():
    from perception import analyze_video_contextual as AVC
    s = Session("t")
    m = _stub_mcp()
    seen = {}

    def fake_loop(nl, **kw):
        seen["model"] = AVC.MODEL_OVERRIDE.get()
        return LoopOutcome(answer="ok", steps=1, terminated="text", final_tool="sql_query",
                           final_value=[{"x": 1}], preview_value=[{"x": 1}], results={}, trace=[])
    sl, calls = _stub_loop(fake_loop)
    try:
        orch.run_query("最帅的视频", session=s, pro_video=True)
        assert seen["model"] == AVC.PRO_MODEL
        orch.run_query("最帅的视频", session=s, pro_video=False)
        assert seen["model"] is None
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 复核模式:请求级 critic 透传(默认关;critic=True 本请求强制开)─────
def test_critic_request_mode_passthrough():
    s = Session("t")
    m = _stub_mcp()
    seen = {}

    def fake_loop(nl, **kw):
        seen["use_critic"] = kw.get("use_critic")
        return LoopOutcome(answer="ok", steps=1, terminated="text", final_tool="sql_query",
                           final_value=[{"x": 1}], preview_value=[{"x": 1}], results={}, trace=[])
    sl, calls = _stub_loop(fake_loop)
    try:
        orch.run_query("有几个视频", session=s, critic=True)
        assert seen["use_critic"] is True                  # 请求开 → 本请求强制开
        orch.run_query("有几个视频", session=s)
        assert seen["use_critic"] is None                  # 没开 → None(跟随全局默认)
    finally:
        _restore_loop(sl); orch.mcp_client = m


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
