"""V1.5 富化:解析纯函数 + enrich 主流程(stub)+ /v1/enrich 端点校验的离线单测。
    python -m pytest tests/pipeline/test_enrichment.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline.enrichment import MAX_SNIPPET, entries_from_enrichment


# ── 解析:caption 恒有;有话才有 transcript 段;烂段跳过 ────────────
def test_entries_full():
    data = {"caption": "A spin class instructor coaches riders.", "has_speech": True,
            "language": "en",
            "segments": [{"start_s": 0.0, "end_s": 9.0, "text": "Hey guys, it's Brooke."},
                         {"start_s": 9.0, "end_s": 12.3, "text": "Stay strong."}]}
    out = entries_from_enrichment("v1", data)
    assert [s for s, _ in out] == ["caption", "video", "transcript", "transcript"]  # v2:+video 粗向量行
    key, snip, s0, e0 = out[2][1]                             # v2:out[1] 是 vid: 粗向量行
    assert key == "tr:v1:0" and snip == "Hey guys, it's Brooke." and (s0, e0) == (0.0, 9.0)
    assert out[0][1][0] == "cap:v1" and out[1][1][0] == "vid:v1"


def test_entries_no_speech_keeps_caption_only():
    out = entries_from_enrichment("v2", {"caption": "Wingsuit flight.", "has_speech": False,
                                         "segments": [{"start_s": 0, "end_s": 1, "text": "ghost"}]})
    assert len(out) == 2 and out[0][0] == "caption" and out[1][0] == "video"  # v2:无话仍有 caption+video


def test_entries_skips_junk_and_caps_length():
    data = {"caption": "c" * 9999, "has_speech": True,
            "segments": ["garbage", {"text": ""}, {"start_s": "x", "end_s": None, "text": "ok"}]}
    out = entries_from_enrichment("v3", data)
    assert out[0][1][1] == "c" * MAX_SNIPPET               # caption 截断
    assert len(out) == 3                                    # v2:caption+video+1段;烂段/空文本跳过
    assert out[1][1][2] is None and out[1][1][3] is None


def test_entries_empty():
    assert entries_from_enrichment("v", {}) == []
    assert entries_from_enrichment("v", {"caption": "  "}) == []


# ── enrich_video 主流程(全 stub:genai/embed/upsert)────────────
def test_enrich_video_stubbed(monkeypatch):
    import json
    from pipeline import enrichment as en, embeddings as emb, semantic_index as si
    from pipeline import genai_client

    class _Resp:
        text = json.dumps({"caption": "cap", "has_speech": True, "language": "en",
                           "segments": [{"start_s": 0, "end_s": 3, "text": "hello"}]})
        usage_metadata = None
    class _Models:
        def generate_content(self, **kw):
            return _Resp()
    class _C:
        models = _Models()
    monkeypatch.setattr(genai_client, "_CLIENT", _C())
    monkeypatch.setattr(emb, "embed_texts", lambda texts, **kw: [[0.0] * 768 for _ in texts])
    written = []
    monkeypatch.setattr(si, "index_entry", lambda vid, src, entry, lit: written.append((src, entry[0])) or True)
    monkeypatch.setattr(en, "embed_texts", emb.embed_texts)
    monkeypatch.setattr(en, "index_entry", si.index_entry)
    stats = en.enrich_video("v9", "gs://b/v9.mp4")
    assert stats["rows"] == 3 and stats["has_speech"] and stats["segments"] == 1  # v2:+video 行
    assert ("caption", "cap:v9") in written and ("transcript", "tr:v9:0") in written


# ── /v1/enrich 端点:非法 id / 未知视频 / 幂等 ─────────────────
def test_enrich_endpoint_validation(monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    import api.server as srv                                   # 不 reload(顺序无关);直接关掉鉴权中间件
    from pipeline import enrichment as en, node_executor as ne, config
    monkeypatch.setattr(srv, "_ACCESS_KEYS", [])               # 无鉴权 → 免 Basic 头
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(en, "already_enriched", lambda vid: vid == "done_1")
    monkeypatch.setattr(ne, "_resolve_gcs", lambda vid: "gs://b/x.mp4" if vid == "good_1" else None)
    c = TestClient(srv.app)
    assert c.post("/v1/enrich", json={"video_id": "bad' id"}).status_code == 422
    assert c.post("/v1/enrich", json={"video_id": "done_1"}).json()["status"] == "already"
    assert c.post("/v1/enrich", json={"video_id": "nope_1"}).status_code == 404
    monkeypatch.setattr(en, "enrich_video", lambda vid, gcs: {"rows": 0})
    assert c.post("/v1/enrich", json={"video_id": "good_1"}).json()["status"] == "started"
