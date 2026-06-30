"""M4.1:analyze_video 内容缓存 —— 纯缓存单元 + _run_analyze_video 集成。

集成用例 mock 掉 gcs_uri 查询与 analyze() → 离线、不连 GCP。
"""
import pytest

from pipeline import analyze_cache, config
from pipeline.dag_schema import Node
import pipeline.node_executor as ne
import pipeline.mcp_client as mc
import perception.analyze_video_contextual as avc


@pytest.fixture(autouse=True)
def _clean():
    analyze_cache.clear()
    avc.MODEL_OVERRIDE.set(None)
    yield
    analyze_cache.clear()
    avc.MODEL_OVERRIDE.set(None)


# ── 纯缓存单元 ───────────────────────────────
def test_roundtrip():
    k = analyze_cache.make_key("v1", question="q", context=None, rubric=None, time_range=None, model="m")
    assert analyze_cache.get(k) is None
    analyze_cache.put(k, {"answer": "a"})
    assert analyze_cache.get(k) == {"answer": "a"}


def test_get_returns_copy():
    k = analyze_cache.make_key("v1", question="q", context=None, rubric=None, time_range=None, model="m")
    analyze_cache.put(k, {"answer": "a"})
    got = analyze_cache.get(k)
    got["answer"] = "mutated"                         # 改返回值不该脏缓存
    assert analyze_cache.get(k)["answer"] == "a"


def test_key_distinct_by_model():
    a = analyze_cache.make_key("v1", question="q", context=None, rubric=None, time_range=None, model="gemini-2.5-flash")
    b = analyze_cache.make_key("v1", question="q", context=None, rubric=None, time_range=None, model="gemini-2.5-pro")
    assert a != b


def test_lru_eviction(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_CACHE_MAX", 3)
    for i in range(5):
        analyze_cache.put(f"k{i}", {"i": i})
    assert analyze_cache.size() == 3
    assert analyze_cache.get("k0") is None            # 最久未用被淘汰
    assert analyze_cache.get("k4") == {"i": 4}


def test_off_backend(monkeypatch):
    monkeypatch.setattr(config, "ANALYZE_CACHE_BACKEND", "off")
    analyze_cache.put("k", {"a": 1})
    assert analyze_cache.get("k") is None


# ── 集成:_run_analyze_video ─────────────────
def _patch_gcs(monkeypatch):
    monkeypatch.setattr(mc, "query_db", lambda sql: [{"gcs_uri": "gs://b/v.mp4"}])


class _Res:
    def __init__(self, d): self._d = d
    def model_dump(self): return dict(self._d)


def test_cache_hit_skips_second_analyze(monkeypatch):
    _patch_gcs(monkeypatch)
    calls = {"n": 0}
    def fake(req, gcs):
        calls["n"] += 1
        return _Res({"answer": "great", "enough": "yes", "confidence": 0.9})
    monkeypatch.setattr(avc, "analyze", fake)
    node = Node(id="c0", tool="analyze_video", inputs={"video_id": "vid_1", "question": "how good?"})
    r1 = ne._run_analyze_video(node, {})
    r2 = ne._run_analyze_video(node, {})
    assert calls["n"] == 1                             # 第二次走缓存,不再调 analyze
    assert r1.value["answer"] == "great" and r2.value["answer"] == "great"
    assert r1.value["video_id"] == "vid_1"


def test_failure_not_cached(monkeypatch):
    _patch_gcs(monkeypatch)
    calls = {"n": 0}
    def fake(req, gcs):
        calls["n"] += 1
        return _Res({"answer": avc.FAILURE_ANSWER_PREFIX + "[boom]", "enough": "no", "confidence": 0.0})
    monkeypatch.setattr(avc, "analyze", fake)
    node = Node(id="c0", tool="analyze_video", inputs={"video_id": "vid_1", "question": "q"})
    ne._run_analyze_video(node, {})
    ne._run_analyze_video(node, {})
    assert calls["n"] == 2                             # 失败信封不缓存 → 第二次仍真调


def test_different_model_misses(monkeypatch):
    _patch_gcs(monkeypatch)
    calls = {"n": 0}
    def fake(req, gcs):
        calls["n"] += 1
        return _Res({"answer": "x", "enough": "yes", "confidence": 0.8})
    monkeypatch.setattr(avc, "analyze", fake)
    node = Node(id="c0", tool="analyze_video", inputs={"video_id": "vid_1", "question": "q"})
    avc.MODEL_OVERRIDE.set("gemini-2.5-flash")
    ne._run_analyze_video(node, {})
    avc.MODEL_OVERRIDE.set("gemini-2.5-pro")
    ne._run_analyze_video(node, {})
    assert calls["n"] == 2                             # 不同模型 → 不同键 → 各看一次


def test_time_range_parsed_and_keys_cache(monkeypatch):  # M4.5
    _patch_gcs(monkeypatch)
    analyze_cache.clear()
    seen = []
    def fake(req, gcs):
        seen.append(req.time_range)
        return _Res({"answer": "x", "enough": "yes", "confidence": 0.8})
    monkeypatch.setattr(avc, "analyze", fake)
    mk = lambda tr: Node(id="c0", tool="analyze_video",
                         inputs={"video_id": "vid_1", "question": "q", "time_range": tr})
    ne._run_analyze_video(mk([10, 20.5]), {})
    ne._run_analyze_video(mk([0, 5]), {})
    ne._run_analyze_video(mk([10, 20.5]), {})          # 同 time_range → 命中缓存,不再真调
    assert seen == [(10.0, 20.5), (0.0, 5.0)]          # 解析成 float 元组;不同段不同键、同段命中
