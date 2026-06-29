"""
多轮编排分支的轻量测试 —— 全用桩,不依赖 GCP / DB。
    python -m pipeline.test_multiturn

记忆简化后(transcript 是唯一记忆,catalog/session-history 已删):
  followup → 一律进 loop(指代由 loop 用 transcript 回放自解析,不在编排层前置拒)
  meta     → 一律进 loop(由 loop 据回放解释"怎么算的",不再走模板早返回)
  new + 成功 → 进 loop、出答案、推进轮号、写 transcript(record_loop_turn)
  无 session → 向后兼容(到达 loop、session_id=None)
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
from pipeline.router import RouterVerdict
from pipeline.session import Session


# ── 桩:替换 orch.mcp_client / orch.Router ─────────────────
def _stub_router(verdict):
    saved = (orch.mcp_client, orch.Router)
    orch.mcp_client = types.SimpleNamespace(
        get_schema=lambda: {"video_facts": [{"column": "id", "type": "int"}]})

    class FakeRouter:
        def judge(self, q, **kw):
            return verdict
    orch.Router = FakeRouter
    return saved


def _restore_router(saved):
    orch.mcp_client, orch.Router = saved


_REPLAY_SENTINEL = "# 多轮上下文\n## 第1轮\n用户:之前的问题"


def _stub_loop(run):
    """把 loop 入口换成 run,并让记忆侧 inert(离线)。build_loop_context 返回一个【哨兵字符串】
    (不是 None)→ 可断言它【真被透传】进 run_query_loop 的 replay_context,而不止"被调用"。"""
    calls = {"replay": 0, "passed_ctx": "UNSET"}
    saved = (loop_driver.run_query_loop,
             loop_memory.build_loop_context, loop_memory.record_loop_turn)

    def _wrapped_run(nl, **kw):
        calls["passed_ctx"] = kw.get("replay_context")     # 捕获编排层实际传进来的回放
        return run(nl, **kw)
    loop_driver.run_query_loop = _wrapped_run

    def _fake_ctx(*a, **k):
        calls["replay"] += 1
        return _REPLAY_SENTINEL
    loop_memory.build_loop_context = _fake_ctx
    loop_memory.record_loop_turn = lambda *a, **k: None
    return saved, calls


def _restore_loop(saved):
    (loop_driver.run_query_loop,
     loop_memory.build_loop_context, loop_memory.record_loop_turn) = saved


# ── followup:进 loop,且取了回放(不再前置拒)───────────────
def test_followup_reaches_loop_and_replays():
    s = Session("t")  # 无需预置任何 catalog —— 记忆全在 transcript
    v = RouterVerdict(decision="answer", turn_type="followup", intent="visualize")
    saved = _stub_router(v)

    def boom(*a, **k):
        raise RuntimeError("REACHED_LOOP")
    sl, calls = _stub_loop(boom)
    try:
        r = orch.run_query("plot those", session=s)
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
        assert r["turn_type"] == "followup"
        assert calls["replay"] == 1                       # followup → 取了 transcript 回放
        assert calls["passed_ctx"] == _REPLAY_SENTINEL    # 且回放【真透传】进了 loop 的 replay_context
    finally:
        _restore_loop(sl); _restore_router(saved)


# ── meta:进 loop(不再模板早返回),且取了回放 ─────────────
def test_meta_reaches_loop_and_replays():
    s = Session("t")
    v = RouterVerdict(decision="answer", turn_type="meta", intent="meta")
    saved = _stub_router(v)

    def boom(*a, **k):
        raise RuntimeError("REACHED_LOOP")
    sl, calls = _stub_loop(boom)
    try:
        r = orch.run_query("how did you get that", session=s)
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
        assert r["turn_type"] == "meta"
        assert calls["replay"] == 1                       # meta → 也取回放(由 loop 解释)
        assert calls["passed_ctx"] == _REPLAY_SENTINEL    # 回放透传进 loop
    finally:
        _restore_loop(sl); _restore_router(saved)


# ── new + 成功:进 loop、出答案、推进轮号 ───────────────────
def test_new_success_reaches_loop_and_advances_turn():
    s = Session("t")
    v = RouterVerdict(decision="answer", turn_type="new", intent="retrieve")
    saved = _stub_router(v)

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
        r = orch.run_query("find skiing", session=s)
        assert r["status"] == "ok", r
        assert r["answer"] == "共 1 条 skiing 视频"
        assert r["session_id"] == "t" and r["turn_type"] == "new"
        assert s._turn_no == 1                           # 轮号推进了(供 record_loop_turn)
        assert calls["replay"] == 0                      # new 轮不取回放
        assert calls["passed_ctx"] is None               # 且传给 loop 的 replay_context 为 None
    finally:
        _restore_loop(sl); _restore_router(saved)


def test_no_session_backcompat():
    v = RouterVerdict(decision="answer", turn_type="new", intent="retrieve")
    saved = _stub_router(v)

    def boom(*a, **k):
        raise RuntimeError("REACHED")
    sl, calls = _stub_loop(boom)
    try:
        r = orch.run_query("how many videos")   # 无 session
        assert r["status"] == "error" and "REACHED" in r["error"], r
        assert r["session_id"] is None and r["turn_type"] == "new"
    finally:
        _restore_loop(sl); _restore_router(saved)


# ── 回放真正进了 loop 的 system prompt(_loop_system 拼接)─────────────
def test_loop_system_splices_replay_context():
    from pipeline.loop_driver import _loop_system
    schema = {"video_facts": [{"column": "id"}]}
    s_none = _loop_system(schema, None)
    s_ctx = _loop_system(schema, _REPLAY_SENTINEL)
    assert _REPLAY_SENTINEL not in s_none                 # 无回放 → system 不含它
    assert _REPLAY_SENTINEL in s_ctx                      # 有回放 → 拼进 system prompt
    assert s_ctx.startswith(s_none)                       # 规则+schema 段不变,回放追加在尾部


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
