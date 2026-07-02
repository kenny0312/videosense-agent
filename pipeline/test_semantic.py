"""V1 语义索引:snippet 构造 / 向量字面量 / embed 通道(stub)的离线单测。
    python -m pytest pipeline/test_semantic.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline import embeddings as emb
from pipeline.semantic_index import (
    SEARCH_SQL, UPSERT_SQL, analyze_snippet, fact_snippet, skydive_snippet, upsert_params)


# ── snippet 构造 ───────────────────────────────────────────
def test_fact_snippet_combines_predicate_and_rationale():
    key, snip, s, e = fact_snippet({"video_id": "v1", "predicate": "skydiving",
                                    "rationale": "a wingsuit jump", "start_ts": 2.0, "end_ts": 17.0})
    assert key == "fact:v1:skydiving" and snip == "skydiving: a wingsuit jump" and (s, e) == (2.0, 17.0)


def test_fact_snippet_skips_category_provenance_and_empty():
    assert fact_snippet({"video_id": "v", "predicate": "skydiving",
                         "rationale": "category: derived from predicates: x"}) is None
    assert fact_snippet({"video_id": "v", "predicate": "p", "rationale": ""}) is None


def test_skydive_snippet():
    assert skydive_snippet({"video_id": "s1", "summary": "", "freefall_start_ts": 1}) is None
    key, snip, s, e = skydive_snippet({"video_id": "s1", "summary": "Wingsuit flight over coast.",
                                       "freefall_start_ts": 2.0, "freefall_end_ts": 46.0})
    assert key == "skydive:s1" and (s, e) == (2.0, 46.0)


def test_analyze_snippet_skips_failure_and_parses_evidence():
    assert analyze_snippet("v", {"answer": "[分析失败][boom]"}, "k") is None
    key, snip, s, e = analyze_snippet(
        "v", {"answer": "两人贴地飞行,非常精彩", "evidence_ts": [12.0, 3.5, "bad"]}, "av:v:abc")
    assert key == "av:v:abc" and s == 3.5 and e == 12.0        # 数值取 min/max,烂值跳过
    assert analyze_snippet("v", {"answer": "ok", "evidence_ts": "not-a-list"}, "k")[2] is None


def test_upsert_params_order_matches_sql():
    entry = ("fact:v:p", "p: r", 1.0, 2.0)
    params = upsert_params(entry, "v", "fact", "[0.1,0.2]")
    assert params == ("v", "fact", "p: r", 1.0, 2.0, "[0.1,0.2]", "fact:v:p")
    assert UPSERT_SQL.count("%s") == len(params)
    assert SEARCH_SQL.count("%s") == 3                          # 查询向量×2 + LIMIT


# ── 向量字面量 / embed 通道 ────────────────────────────────
def test_vec_literal_format():
    lit = emb.vec_literal([0.1, -2.5, 1e-7])
    assert lit.startswith("[") and lit.endswith("]") and lit.count(",") == 2
    assert "e" not in lit.split(",")[0]                         # 常规小数不走科学计数


def _stub_client(dim=768, fail=False):
    class _Emb:
        def __init__(self, v): self.values = v
    class _R:
        def __init__(self, n): self.embeddings = [_Emb([0.0] * dim) for _ in range(n)]
    class _Models:
        def embed_content(self, *, model, contents, config):
            if fail:
                raise RuntimeError("quota")
            return _R(len(contents))
    class _C:
        models = _Models()
    return _C()


# ── S3:工具执行 / 声明门控 / 写钩子 fail-open ────────────────
def test_semantic_search_declaration_gated(monkeypatch):
    from pipeline import config
    from pipeline import loop_driver as ld
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", False)
    assert "semantic_search" not in [d["name"] for d in ld.loop_function_declarations()]
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    assert "semantic_search" in [d["name"] for d in ld.loop_function_declarations()]


def test_run_semantic_search(monkeypatch):
    import pytest
    from pipeline import config, embeddings as e, semantic_index as si
    from pipeline import node_executor as ne
    from pipeline.dag_schema import Node
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(e, "embed_query", lambda q: [0.0] * 768)
    seen = {}
    monkeypatch.setattr(si, "search", lambda lit, k: seen.setdefault("k", k) and [] or
                        [{"n": 1, "video_id": "v1", "source": "fact", "snippet": "s",
                          "start_ts": 1.0, "end_ts": 2.0, "score": 0.9, "label": "s"}])
    r = ne._run_semantic_search(Node(id="s1", tool="semantic_search",
                                     inputs={"query": "falling", "k": 99}))
    assert r.ok and isinstance(r.value, list) and r.value[0]["video_id"] == "v1"
    assert seen["k"] == 20                                     # k 被夹在 [1,20]
    with pytest.raises(ValueError):
        ne._run_semantic_search(Node(id="s2", tool="semantic_search", inputs={}))
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", False)
    with pytest.raises(ValueError):
        ne._run_semantic_search(Node(id="s3", tool="semantic_search", inputs={"query": "x"}))


def test_index_analyze_hook_failopen(monkeypatch):
    """写钩子内部任何失败(embed 挂)都不许外溢 —— analyze 主流程不受影响。"""
    from pipeline import config, embeddings as e
    from pipeline import node_executor as ne
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(e, "embed_texts", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    ne._index_analyze_result("v1", {"answer": "好"}, "av:v1:k")   # 不抛即过
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", False)
    ne._index_analyze_result("v1", {"answer": "好"}, "av:v1:k")   # 关着 → 直接跳过


def test_embed_texts_stub_and_failopen(monkeypatch):
    from pipeline import genai_client
    monkeypatch.setattr(genai_client, "_CLIENT", _stub_client())
    out = emb.embed_texts(["a", "b", "c"])
    assert len(out) == 3 and all(len(v) == emb.EMBED_DIM for v in out)
    assert emb.embed_texts([]) == []
    monkeypatch.setattr(genai_client, "_CLIENT", _stub_client(dim=42))
    assert emb.embed_texts(["a"]) is None                       # 维度异常 → None
    monkeypatch.setattr(genai_client, "_CLIENT", _stub_client(fail=True))
    assert emb.embed_texts(["a"]) is None                       # API 失败 → None(fail-open)
    assert emb.embed_query("x") is None
