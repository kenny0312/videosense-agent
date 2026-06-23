"""
多轮编排分支的轻量测试 —— 全用桩,不依赖 GCP / DB。
    python -m pipeline.test_multiturn

覆盖 orchestrator 的多轮路径:
  followup 解析成功 → Planner 收到含配方的 context
  followup 解析不到真实结果 → 降级诚实拒答(不构造 Planner)
  meta 有 prior → 纯模板方法说明(status=ok,不构造 Planner)
  meta 无 prior → 拒答
  成功轮 → catalog 登记 1 个 artifact(配方=sql)+ history 记一轮
  无 session → 向后兼容(到达 planner、session_id=None)
"""
from __future__ import annotations

import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pipeline.orchestrator as orch
from pipeline.dag_schema import parse_dag
from pipeline.node_executor import NodeResult
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


def _seed(session, sql="SELECT id, predicate FROM video_facts WHERE predicate ILIKE '%ski%'"):
    """给会话预置一个可指代的 artifact(a1,配方=sql)。"""
    dag = parse_dag({"nodes": [{"id": "n1", "tool": "sql_query",
                                "inputs": {"sql": sql}, "depends_on": []}]})
    return session.register_artifact(dag, {"n1": [{"id": 1, "predicate": "skiing"}]},
                                     "find skiing videos", "retrieve")


# ── followup ──────────────────────────────────────────────
def test_followup_passes_context_to_planner():
    s = Session("t"); _seed(s)
    v = RouterVerdict(decision="answer", turn_type="followup", intent="visualize",
                      references=[{"resolved_to": "a1", "text": "those", "resolvable": True}])
    saved = _stub_router(v)

    class CtxPlanner:
        schema = {}
        def __init__(self, *a, **k): pass
        def plan(self, nl, *, context=None):
            assert context and context["resolved_artifacts"][0]["id"] == "a1", context
            assert context["resolved_artifacts"][0]["recipe"]["type"] == "sql"
            raise RuntimeError("REACHED_WITH_CONTEXT")
    try:
        r = orch.run_query("plot those", planner=CtxPlanner(), session=s)
        assert r["status"] == "error" and "REACHED_WITH_CONTEXT" in r["error"], r
        assert r["turn_type"] == "followup"
    finally:
        _restore_router(saved)


def test_followup_unresolvable_refuses():
    s = Session("t")  # catalog 空
    v = RouterVerdict(decision="answer", turn_type="followup", intent="visualize",
                      references=[{"resolved_to": "a1"}])   # a1 不存在(幻觉)
    saved = _stub_router(v)

    class BoomPlanner:
        def __init__(self, *a, **k):
            raise AssertionError("不可解析的 followup 不应构造 Planner")
    orig = orch.Planner; orch.Planner = BoomPlanner
    try:
        r = orch.run_query("plot those", session=s)
        assert r["status"] == "refused", r
        assert r["turn_type"] == "followup"
    finally:
        orch.Planner = orig; _restore_router(saved)


# ── meta ──────────────────────────────────────────────────
def test_meta_with_prior_answers_not_refuse():
    s = Session("t"); _seed(s)
    v = RouterVerdict(decision="answer", turn_type="meta", intent="meta",
                      references=[{"resolved_to": "a1"}])
    saved = _stub_router(v)

    class BoomPlanner:
        def __init__(self, *a, **k):
            raise AssertionError("meta 轮不应构造 Planner")
    orig = orch.Planner; orch.Planner = BoomPlanner
    try:
        r = orch.run_query("how did you get that", session=s)
        assert r["status"] == "ok", r
        assert "SELECT" in r["answer"], r["answer"]       # 展示了上一轮 SQL
        assert r["turn_type"] == "meta"
    finally:
        orch.Planner = orig; _restore_router(saved)


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
    orig_exec = orch.execute_node

    def fake_exec(node, upstream, sandbox, trace, schema=None):
        return NodeResult(node.id, node.tool, ok=True,
                          value=[{"id": 1, "predicate": "skiing"}], attempts=1)
    orch.execute_node = fake_exec

    class OkPlanner:
        schema = {"video_facts": [{"column": "id"}]}
        def __init__(self, *a, **k): pass
        def plan(self, nl, *, context=None):
            return parse_dag({"nodes": [{"id": "n1", "tool": "sql_query",
                "inputs": {"sql": "SELECT id FROM video_facts"}, "depends_on": []}]})
    try:
        r = orch.run_query("find skiing", planner=OkPlanner(), session=s)
        assert r["status"] == "ok", r
        assert len(s.catalog) == 1 and s.catalog[0].recipe["type"] == "sql", s.catalog
        assert s.history[-1].status == "ok" and s.history[-1].artifact_ids == ["a1"]
        assert r["session_id"] == "t"
    finally:
        orch.execute_node = orig_exec; _restore_router(saved)


def test_no_session_backcompat():
    v = RouterVerdict(decision="answer", turn_type="new", intent="retrieve")
    saved = _stub_router(v)

    class ReachedPlanner:
        def __init__(self, *a, **k): pass
        def plan(self, nl, *, context=None):
            raise RuntimeError("REACHED")
    try:
        r = orch.run_query("how many videos", planner=ReachedPlanner())   # 无 session
        assert r["status"] == "error" and "REACHED" in r["error"], r
        assert r["session_id"] is None and r["turn_type"] == "new"
    finally:
        _restore_router(saved)


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
