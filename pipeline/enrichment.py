"""V1.5 入库富化:转录 + caption(设计 docs/design/ingest-enrichment.md)。

一次 flash 调用(media_resolution=LOW,spike 实测转录质量不降、省 3.1×)两产出:
  · caption —— 1-2 句"这个视频在拍什么"(为检索写,与 analyze 的"答具体问题"互补)
  · transcript —— 带时间戳的语音分段(5-15s 自然停顿);无语音 → has_speech=false 只留 caption
全部进 content_embeddings(source='caption'/'transcript',V1 预留位;幂等键 cap:{vid} / tr:{vid}:{i})。
零新依赖:不装 Whisper,复用 genai client + embeddings + semantic_index。全程 fail-open。
"""
from __future__ import annotations

import json
import logging
import os

from pipeline.embeddings import embed_texts, vec_literal
from pipeline.semantic_index import index_entry, _execute

log = logging.getLogger("pipeline.enrichment")

ENRICH_MODEL = os.environ.get("ENRICH_MODEL", "gemini-2.5-flash")
MAX_SEGMENTS = 200          # 极长视频兜底
MAX_SNIPPET = 500           # 单段文本上限(embed 效率)

ENRICH_PROMPT = (
    "You are enriching a video library for retrieval. Watch this video and output STRICT JSON "
    "(no markdown):\n"
    '{"caption": "1-2 sentences describing what this video shows overall, written for search",\n'
    ' "has_speech": true|false, "language": "en|zh|...",\n'
    ' "segments": [{"start_s": 0.0, "end_s": 4.2, "text": "verbatim transcription"}]}\n'
    "Rules: transcribe speech VERBATIM in its original language, segment on natural pauses "
    "(5-15s chunks). If there is no intelligible speech (wind noise, music only), set "
    "has_speech=false with empty segments. The caption is always required."
)


def entries_from_enrichment(video_id: str, data: dict) -> list[tuple[str, tuple]]:
    """解析结果 → [(source, (content_key, snippet, start, end))](纯函数,离线可测)。"""
    out: list[tuple[str, tuple]] = []
    cap = str(data.get("caption") or "").strip()
    if cap:
        out.append(("caption", (f"cap:{video_id}", cap[:MAX_SNIPPET], None, None)))
    if data.get("has_speech"):
        for i, seg in enumerate((data.get("segments") or [])[:MAX_SEGMENTS]):
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(seg.get("start_s"))
                end = float(seg.get("end_s"))
            except (TypeError, ValueError):
                start = end = None
            out.append(("transcript", (f"tr:{video_id}:{i}", text[:MAX_SNIPPET], start, end)))
    return out


def already_enriched(video_id: str) -> bool:
    """幂等检查:有 cap:{vid} 键即视为已富化(caption 总会写)。查失败 → False(宁可重跑,upsert 幂等)。"""
    try:
        rows = _execute("SELECT 1 FROM content_embeddings WHERE content_key = %s LIMIT 1",
                        (f"cap:{video_id}",))
        return bool(rows)
    except Exception:
        return False


def enrich_video(video_id: str, gcs_uri: str) -> dict:
    """富化一个视频:flash(low res)→ 解析 → embed → upsert。返回统计;失败上抛(调用方定策略)。"""
    from google.genai import types
    from pipeline.agentops import usage
    from pipeline.genai_client import get_client

    low = gcs_uri.lower()
    mime = ("video/quicktime" if low.endswith(".mov")
            else "video/webm" if low.endswith(".webm") else "video/mp4")
    part = types.Part(file_data=types.FileData(file_uri=gcs_uri, mime_type=mime))
    resp = get_client().models.generate_content(
        model=ENRICH_MODEL, contents=[part, ENRICH_PROMPT],
        config=types.GenerateContentConfig(
            temperature=0.1, max_output_tokens=8192, response_mime_type="application/json",
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW))   # 音频不受影响,视频 token 省 3×
    usage.add_usage(resp, ENRICH_MODEL)
    data = json.loads(resp.text)
    if not isinstance(data, dict):
        raise ValueError("enrichment 返回不是 JSON object")

    entries = entries_from_enrichment(video_id, data)
    if not entries:
        return {"video_id": video_id, "has_speech": False, "rows": 0}
    vecs = embed_texts([e[1][1] for e in entries])
    written = 0
    if vecs:
        for (source, entry), vec in zip(entries, vecs):
            if index_entry(video_id, source, entry, vec_literal(vec)):
                written += 1
    return {"video_id": video_id, "has_speech": bool(data.get("has_speech")),
            "language": data.get("language"), "segments": sum(1 for s, _ in entries if s == "transcript"),
            "rows": written}
