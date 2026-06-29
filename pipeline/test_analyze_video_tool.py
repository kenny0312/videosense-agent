"""
M2 单测:analyze_video 接进 loop —— spec 登记 / node_executor 适配 / 配额护栏。
全离线:桩 mcp_client.query_db + perception.analyze,不连 GCP/网络。
    python -m pipeline.test_analyze_video_tool
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import perception.analyze_video_contextual as AVC
import pipeline.loop_driver as LD
import pipeline.node_executor as NE
from pipeline import config
from pipeline.dag_schema import ALL_TOOLS, DATA_TOOLS, Node
from pipeline.node_specs import SPECS, build_function_declarations, required_inputs


class _FakeTrace:
    class _S:
        def ok(self, **k): pass
        def fail(self, **k): pass
    def step(self, *a, **k): return self._S()


# ── 登记(node_specs / dag_schema)──────────────────
def test_registered_everywhere():
    assert "analyze_video" in SPECS
    assert "analyze_video" in DATA_TOOLS and "analyze_video" in ALL_TOOLS
    assert Node(id="n1", tool="analyze_video").tool == "analyze_video"   # ToolName 接受
    assert "analyze_video" in [d["name"] for d in build_function_declarations()]
    assert required_inputs("analyze_video") == ("question",)


# ── _run_analyze_video(桩 mcp + analyze)────────────
def _stub(query_db, analyze):
    saved = (NE.mcp_client.query_db, AVC.analyze)
    NE.mcp_client.query_db = query_db
    AVC.analyze = analyze
    return saved


def _restore(saved):
    NE.mcp_client.query_db, AVC.analyze = saved


def test_run_happy_path():
    saved = _stub(
        lambda sql: [{"gcs_uri": "gs://b/v.mp4"}],
        lambda req, gcs, **k: AVC.AnalyzeResult(answer="8/10 近地穿越", enough="yes",
                                                confidence=0.8, evidence_ts=42.0))
    try:
        node = Node(id="c0", tool="analyze_video",
                    inputs={"video_id": "GX010533", "question": "多帅?", "rubric": "近地=帅"})
        res = NE._run_analyze_video(node, {})
        assert res.ok
        assert res.value["video_id"] == "GX010533"
        assert res.value["enough"] == "yes" and res.value["evidence_ts"] == 42.0
        assert res.value["answer"].startswith("8/10")        # 结论前置
    finally:
        _restore(saved)


def test_run_missing_question():
    res = NE._run_analyze_video(Node(id="c0", tool="analyze_video", inputs={"video_id": "v1"}), {})
    assert not res.ok and "question" in res.stderr


def test_run_video_id_from_upstream():
    captured = {}

    def fake_analyze(req, gcs, **k):
        captured["gcs"] = gcs
        return AVC.AnalyzeResult(answer="ok", enough="yes")
    saved = _stub(lambda sql: [{"gcs_uri": "gs://b/up.mp4"}], fake_analyze)
    try:
        node = Node(id="c0", tool="analyze_video", inputs={"question": "在干嘛?"})
        res = NE._run_analyze_video(node, {"c_prev": [{"video_id": "v_up123"}]})
        assert res.ok and res.value["video_id"] == "v_up123"
        assert captured["gcs"] == "gs://b/up.mp4"
    finally:
        _restore(saved)


def test_run_gcs_not_found():
    saved = _stub(lambda sql: [], lambda req, gcs, **k: None)
    try:
        res = NE._run_analyze_video(Node(id="c0", tool="analyze_video",
                                         inputs={"video_id": "vX", "question": "q"}), {})
        assert not res.ok and "gcs_uri" in res.stderr
    finally:
        _restore(saved)


# ── 配额护栏(loop_driver._make_executor)────────────
def test_quota_caps_analyze_video():
    saved_exec, saved_max = LD.execute_node, config.MAX_VIDEOS_PER_REQUEST
    LD.execute_node = lambda node, upstream, sandbox, trace, **k: NE.NodeResult(
        node.id, node.tool, ok=True, value={"answer": "ok", "enough": "yes"})
    config.MAX_VIDEOS_PER_REQUEST = 2
    try:
        execute = LD._make_executor(sandbox=None, trace=_FakeTrace(),
                                    schema={}, session_id=None)
        r1 = execute("c0", "analyze_video", {"video_id": "v1", "question": "q"}, {}, [])
        r2 = execute("c1", "analyze_video", {"video_id": "v2", "question": "q"}, {}, [])
        r3 = execute("c2", "analyze_video", {"video_id": "v3", "question": "q"}, {}, [])
        assert r1.value.get("enough") == "yes" and r2.value.get("enough") == "yes"
        assert "上限" in str(r3.value) and r3.value.get("enough") == "no"   # 第 3 个超 cap=2
    finally:
        LD.execute_node, config.MAX_VIDEOS_PER_REQUEST = saved_exec, saved_max


def test_quota_does_not_affect_other_tools():
    saved_exec, saved_max = LD.execute_node, config.MAX_VIDEOS_PER_REQUEST
    LD.execute_node = lambda node, upstream, sandbox, trace, **k: NE.NodeResult(
        node.id, node.tool, ok=True, value=[{"x": 1}])
    config.MAX_VIDEOS_PER_REQUEST = 1
    try:
        execute = LD._make_executor(sandbox=None, trace=_FakeTrace(),
                                    schema={}, session_id=None)
        for i in range(4):
            r = execute(f"c{i}", "sql_query", {"sql": "SELECT 1"}, {}, [])
            assert r.ok and "上限" not in str(r.value)        # sql_query 不受配额限制
    finally:
        LD.execute_node, config.MAX_VIDEOS_PER_REQUEST = saved_exec, saved_max


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
