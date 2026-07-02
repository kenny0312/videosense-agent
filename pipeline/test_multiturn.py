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


def _boom(*a, **k):
    raise RuntimeError("REACHED_LOOP")


# ── 有会话:回放建好并透传;turn_type 派生 followup ───────────────
def test_session_turn_reaches_loop_with_replay():
    s = Session("t")
    m = _stub_mcp()
    sl, calls = _stub_loop(_boom)
    try:
        r = orch.run_query("plot those", session=s)
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
        assert r["turn_type"] == "followup"               # 有回放 → followup(零模型调用派生)
        assert calls["replay"] == 1
        assert calls["passed_ctx"] == _REPLAY_SENTINEL    # 回放【真透传】进 loop
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 首轮(有会话但回放为空)→ turn_type=new,仍进 loop ─────────────
def test_first_turn_derives_new():
    s = Session("t")
    m = _stub_mcp()
    sl, calls = _stub_loop(_boom, replay=None)            # 空会话 → 无回放
    try:
        r = orch.run_query("你好", session=s)              # 闲聊也进 loop(无前置门)
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
        assert r["turn_type"] == "new"
        assert calls["passed_ctx"] is None
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 无 session:向后兼容(不建回放、session_id=None)─────────────
def test_no_session_backcompat():
    m = _stub_mcp()
    sl, calls = _stub_loop(_boom)
    try:
        r = orch.run_query("how many videos")
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
        assert r["session_id"] is None and r["turn_type"] == "new"
        assert calls["replay"] == 0 and calls["passed_ctx"] is None
    finally:
        _restore_loop(sl); orch.mcp_client = m


# ── 短语境回复(ok / 我想看):无前置门可误杀,直接带回放进 loop ────
def test_context_dependent_shorts_reach_loop():
    m = _stub_mcp()
    for q in ("ok", "我想看"):
        s = Session("t")
        sl, calls = _stub_loop(_boom)
        try:
            r = orch.run_query(q, session=s)
            assert r["status"] == "error" and "REACHED_LOOP" in r["error"], (q, r)
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
