"""
多轮编排分支的轻量测试 —— 全用桩,不依赖 GCP / DB。
    python -m pipeline.test_multiturn

覆盖 orchestrator 的多轮路径(M7b:执行路径=loop;上一轮上下文走 transcript 回放):
  followup 解析成功 → 进入 loop(turn_type=followup)
  followup 解析不到真实结果 → 降级诚实拒答(不进 loop)
  meta 有 prior → 纯模板用 handle 说明上一轮产出(status=ok,不进 loop)
  meta 无 prior → 拒答
  成功轮 → catalog 登记 1 个 artifact(纯 handle,无 recipe)+ history 记一轮
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


def _stub_loop(run):
    """把 loop 入口换成 run(返回 LoopOutcome 或抛异常),并让记忆侧 inert(离线、不打 LLM/网络)。"""
    saved = (loop_driver.run_query_loop,
             loop_memory.build_loop_context, loop_memory.record_loop_turn)
    loop_driver.run_query_loop = run
    loop_memory.build_loop_context = lambda *a, **k: None
    loop_memory.record_loop_turn = lambda *a, **k: None
    return saved


def _restore_loop(saved):
    (loop_driver.run_query_loop,
     loop_memory.build_loop_context, loop_memory.record_loop_turn) = saved


def _seed(session):
    """给会话预置一个可指代的 artifact(a1,纯 handle)。"""
    return session.register_artifact(
        final_tool="sql_query", final_value=[{"id": 1, "predicate": "skiing"}],
        preview_value=[{"id": 1, "predicate": "skiing"}],
        question="find skiing videos", intent="retrieve")


# ── followup ──────────────────────────────────────────────
def test_followup_resolved_reaches_loop():
    s = Session("t"); _seed(s)
    v = RouterVerdict(decision="answer", turn_type="followup", intent="visualize",
                      references=[{"resolved_to": "a1", "text": "those", "resolvable": True}])
    saved = _stub_router(v)

    def boom(*a, **k):
        raise RuntimeError("REACHED_LOOP")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("plot those", session=s)
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
        assert r["turn_type"] == "followup"
    finally:
        _restore_loop(sl); _restore_router(saved)


def test_followup_unresolvable_refuses():
    s = Session("t")  # catalog 空
    v = RouterVerdict(decision="answer", turn_type="followup", intent="visualize",
                      references=[{"resolved_to": "a1"}])   # a1 不存在(幻觉)
    saved = _stub_router(v)

    def boom(*a, **k):
        raise AssertionError("不可解析的 followup 不应进入 loop")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("plot those", session=s)
        assert r["status"] == "refused", r
        assert r["turn_type"] == "followup"
    finally:
        _restore_loop(sl); _restore_router(saved)


# ── meta ──────────────────────────────────────────────────
def test_meta_with_prior_answers_not_refuse():
    s = Session("t"); _seed(s)
    v = RouterVerdict(decision="answer", turn_type="meta", intent="meta",
                      references=[{"resolved_to": "a1"}])
    saved = _stub_router(v)

    def boom(*a, **k):
        raise AssertionError("meta 轮不应进入 loop")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("how did you get that", session=s)
        assert r["status"] == "ok", r
        assert "skiing" in r["answer"], r["answer"]       # 用 handle 预览描述了上一轮产出
        assert r["turn_type"] == "meta"
    finally:
        _restore_loop(sl); _restore_router(saved)


def test_meta_no_prior_refuses():
    s = Session("t")  # 无任何上一轮结果
    v = RouterVerdict(decision="answer", turn_type="meta", intent="meta",
                      references=[{"resolved_to": "a1"}])
    saved = _stub_router(v)
    try:
        r = orch.run_query("how did you decide", session=s)
        assert r["status"] == "refused", r
        assert r["turn_type"] == "meta"
    finally:
        _restore_router(saved)


# ── 成功登记 / 向后兼容 ───────────────────────────────────
def test_success_registers_artifact():
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
    sl = _stub_loop(fake_loop)
    try:
        r = orch.run_query("find skiing", session=s)
        assert r["status"] == "ok", r
        assert len(s.catalog) == 1, s.catalog
        assert not hasattr(s.catalog[0], "recipe"), "M7b:artifact 不应再带 recipe"
        assert s.history[-1].status == "ok" and s.history[-1].artifact_ids == ["a1"]
        assert r["session_id"] == "t"
    finally:
        _restore_loop(sl); _restore_router(saved)


def test_no_session_backcompat():
    v = RouterVerdict(decision="answer", turn_type="new", intent="retrieve")
    saved = _stub_router(v)

    def boom(*a, **k):
        raise RuntimeError("REACHED")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("how many videos")   # 无 session
        assert r["status"] == "error" and "REACHED" in r["error"], r
        assert r["session_id"] is None and r["turn_type"] == "new"
    finally:
        _restore_loop(sl); _restore_router(saved)


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
