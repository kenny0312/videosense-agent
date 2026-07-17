"""M1 存量迁移:视频档案 v2(设计 docs/design/video-metadata-v2.md §7)。

四个阶段,全部幂等、可断点重跑:
  A. video_metadata 加列 source/ingested_at + 按 id 前缀回填
  B. 标题回填:title=id 的空壳,从 caption 批量生成人类标题(flash,20个/次)
  C. transcript 穿外衣:裸段前置「[标题 | 活动词] 」重嵌(已穿的跳过)
  D. video 粗向量:每个有 caption 的视频造一条 vid:{id} 合成摘要行

    python -m perception.setup_metadata_v2          # 默认全阶段
    python -m perception.setup_metadata_v2 C D      # 只跑指定阶段
"""
from __future__ import annotations

import json
import sys

from pipeline.embeddings import embed_texts, vec_literal
from pipeline.enrichment import transcript_prefix
from pipeline.semantic_index import _execute, index_entry

BATCH = 20


def _log(msg):
    print(msg, flush=True)


def stage_a():
    _log("[A] 加列 + 回填 source/ingested_at …")
    _execute("ALTER TABLE video_metadata ADD COLUMN IF NOT EXISTS source TEXT", ())
    _execute("ALTER TABLE video_metadata ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMP", ())
    # 新插入行自动带真值(审计 B2:没有 DEFAULT 的话,列从合并那天起就开始烂)
    _execute("ALTER TABLE video_metadata ALTER COLUMN ingested_at SET DEFAULT CURRENT_TIMESTAMP", ())
    # source 按 id 形状回填:v_px* = pexels;up_* = 用户上传;v_* = activitynet;其余 = seed(早期私人素材)
    _execute("UPDATE video_metadata SET source = CASE "
             "WHEN video_id LIKE 'v\_px%%' THEN 'pexels' "
             "WHEN video_id LIKE 'up\_%%' THEN 'user_upload' "
             "WHEN video_id LIKE 'v\_%%' THEN 'activitynet' "
             "ELSE 'seed' END WHERE source IS NULL", ())
    # 存量的 ingested_at 没有真值,统一记迁移日(诚实近似,新入库由链路写真值)
    _execute("UPDATE video_metadata SET ingested_at = CURRENT_TIMESTAMP WHERE ingested_at IS NULL", ())
    n = _execute("SELECT COUNT(*) FROM video_metadata WHERE source IS NOT NULL", ())
    _log(f"    source 已覆盖 {n[0][0]} 行")


def stage_b():
    _log("[B] 标题回填(title=id 的空壳)…")
    rows = _execute(
        "SELECT m.video_id, ce.snippet FROM video_metadata m "
        "JOIN content_embeddings ce ON ce.content_key = 'cap:' || m.video_id "
        "WHERE m.title = m.video_id", ())
    _log(f"    待回填 {len(rows)} 个")
    from google.genai import types
    from pipeline.genai_client import get_client
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        caps = [{"id": r[0], "caption": r[1][:300]} for r in chunk]
        try:
            resp = get_client().models.generate_content(
                model="gemini-2.5-flash",
                contents=("For each item, write a short human-readable video title (5-10 words, "
                          "same language as the caption). Return STRICT JSON array of "
                          '{"id":..., "title":...}, same length/order:\n'
                          + json.dumps(caps, ensure_ascii=False)),
                config=types.GenerateContentConfig(
                    temperature=0.0, max_output_tokens=4096,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=0)))
            out = json.loads(resp.text)
            for item in out:
                vid, title = str(item.get("id", "")), str(item.get("title", "")).strip()
                if vid and title and any(vid == r[0] for r in chunk):
                    _execute("UPDATE video_metadata SET title = %s "
                             "WHERE video_id = %s AND title = video_id", (title[:120], vid))
                    done += 1
        except Exception as e:                                 # noqa: BLE001 —— 单批失败重跑可补
            _log(f"    批 {i // BATCH} 失败(重跑可补): {str(e)[:80]}")
        _log(f"    进度 {min(i + BATCH, len(rows))}/{len(rows)}")
    _log(f"    回填 {done} 个标题")


def _ctx_map():
    """video_id → (title, activities)。"""
    rows = _execute("SELECT m.video_id, m.title, d.all_activities FROM video_metadata m "
                    "LEFT JOIN video_discovery d USING (video_id)", ())
    out = {}
    for vid, title, acts in rows:
        if isinstance(acts, str):
            try:
                acts = json.loads(acts)
            except Exception:
                acts = []
        out[vid] = (title if title != vid else "", list(acts or []))
    return out


def stage_c():
    _log("[C] transcript 穿外衣重嵌 …")
    ctx = _ctx_map()
    rows = _execute("SELECT content_key, video_id, snippet, start_ts, end_ts "
                    "FROM content_embeddings WHERE source = 'transcript' "
                    "AND snippet NOT LIKE '[%%'", ())          # 幂等:已穿衣(以[开头)跳过
    _log(f"    待穿衣 {len(rows)} 段")
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        entries = []
        for key, vid, text, s, e in chunk:
            title, acts = ctx.get(vid, ("", []))
            pre = transcript_prefix(title, acts)
            if not pre:                                        # 没有任何上下文可穿 → 保持原样
                continue
            entries.append((vid, (key, pre + text, s, e)))    # text 本就≤500,前缀不挤占(审计 B1)
        if not entries:
            continue
        vecs = embed_texts([e[1][1] for e in entries])
        if not vecs:
            _log(f"    批 {i // BATCH} embed 失败(重跑可补)")
            continue
        for (vid, entry), vec in zip(entries, vecs):
            if index_entry(vid, "transcript", entry, vec_literal(vec)):
                done += 1
        _log(f"    进度 {min(i + BATCH, len(rows))}/{len(rows)}")
    _log(f"    重嵌 {done} 段")


def stage_d():
    _log("[D] video 粗向量 …")
    ctx = _ctx_map()
    rows = _execute(
        "SELECT ce.video_id, ce.snippet FROM content_embeddings ce "
        "WHERE ce.source = 'caption' AND NOT EXISTS "
        "(SELECT 1 FROM content_embeddings v WHERE v.content_key = 'vid:' || ce.video_id)", ())
    _log(f"    待造 {len(rows)} 条")
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        entries = []
        for vid, cap in chunk:
            title, acts = ctx.get(vid, ("", []))
            doc = " ".join(x for x in (f"{title}." if title else "", cap,
                                       f"Activities: {', '.join(acts)}." if acts else "") if x)
            entries.append((vid, (f"vid:{vid}", doc[:700], None, None)))
        vecs = embed_texts([e[1][1] for e in entries])
        if not vecs:
            _log(f"    批 {i // BATCH} embed 失败(重跑可补)")
            continue
        for (vid, entry), vec in zip(entries, vecs):
            if index_entry(vid, "video", entry, vec_literal(vec)):
                done += 1
        _log(f"    进度 {min(i + BATCH, len(rows))}/{len(rows)}")
    _log(f"    新建 {done} 条粗向量")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    stages = [s.upper() for s in sys.argv[1:]] or ["A", "B", "C", "D"]
    for s in stages:
        {"A": stage_a, "B": stage_b, "C": stage_c, "D": stage_d}[s]()
    _log("M1 迁移完成 ✅")


if __name__ == "__main__":
    main()
