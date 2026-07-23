"""长视频分块 enrichment(方案 §8 预言的原型:同一契约,按时间窗多次调用再合并)。

背景: pipeline.enrichment.enrich_video 对 30min+ 视频必然撞 8192 输出 token 截断
(实测《魔笛手》两次死在 ~16k 字符)。本模块把视频切成 CHUNK_S 秒的窗,每窗用
【同一个 ENRICH_PROMPT + 同一 LOW 档】调用(video_metadata 硬裁剪,Gemini 只处理该窗),
逐窗解析后合并,再走 pipeline 的纯函数入库层(entries_from_enrichment/embed/index_entry)。
只 import 不改写 —— 隔离契约内。

用法: python -m dvd_repro.enrich_chunked <video_id>
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dvd_repro.costguard import BudgetGuard, UsageMeter
from pipeline.embeddings import embed_texts, vec_literal
from pipeline.enrichment import ENRICH_MODEL, ENRICH_PROMPT, entries_from_enrichment
from pipeline.semantic_index import _execute, index_entry

CHUNK_S = 300          # 5 分钟/窗(六跑调小:8min 窗的密对白动画仍摸到 8192 截断线)

# 约束解码 schema:让解码器在语法层只能吐合法 JSON —— 治"转写引号/换行搞坏 JSON"的正药
# (五跑教训:json 模式 + 宽松解析都挡不住未转义引号)
_ENRICH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "caption": {"type": "STRING"},
        "has_speech": {"type": "BOOLEAN"},
        "language": {"type": "STRING"},
        "segments": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"start_s": {"type": "NUMBER"}, "end_s": {"type": "NUMBER"},
                           "text": {"type": "STRING"}},
            "required": ["start_s", "end_s", "text"]}},
    },
    "required": ["title", "caption", "has_speech", "segments"],
}


def _windows(duration: float) -> list[tuple[float, float]]:
    out, t = [], 0.0
    while t < duration:
        out.append((t, min(t + CHUNK_S, duration)))
        t += CHUNK_S
    return out


# 六跑取证定案:enrichment 原 prompt 的 "transcribe VERBATIM" 遇观众齐喊(No!×几千)
# 会复读到吃光输出预算 → 字符串永不闭合。dvd 侧自备变体,重复喊话只记一次。
DVD_ENRICH_PROMPT = ENRICH_PROMPT + (
    "\nIMPORTANT: if a word/phrase repeats many times (chants, crowd screaming, sirens), "
    "transcribe it ONCE followed by a note like '(crowd repeats many times)'. "
    "Never write more than 3 consecutive repetitions of anything."
)


def _salvage(text: str) -> dict:
    """输出被截断在 segments 数组中途时的残骸打捞:砍到最后一个完整段落,补闭合。"""
    idx = text.rfind('"}')
    if idx < 0 or '"segments"' not in text[:idx]:
        raise json.JSONDecodeError("salvage 无从下手", text, 0)
    return json.loads(text[:idx + 2] + "]}", strict=False)


def _loads_lenient(text: str) -> dict:
    """三段式解析 + 打捞:严格 → strict=False(放行控制符)→ 刮尾逗号 → 截断打捞。"""
    import re
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", text), strict=False)
    except json.JSONDecodeError:
        pass
    return _salvage(text)


def _call_window(gcs_uri: str, t0: float, t1: float) -> dict:
    from google.genai import types
    from pipeline.agentops import usage
    from pipeline.genai_client import get_client
    vm = types.VideoMetadata(start_offset=f"{t0:g}s", end_offset=f"{t1:g}s")
    part = types.Part(file_data=types.FileData(file_uri=gcs_uri, mime_type="video/mp4"),
                      video_metadata=vm)
    last_err = None
    for attempt in (1, 2):                       # 约束解码兜底仍坏(如撞输出上限)→ 重采一次
        resp = get_client().models.generate_content(
            model=ENRICH_MODEL, contents=[part, DVD_ENRICH_PROMPT],
            config={"temperature": 0.1, "max_output_tokens": 8192,
                    "response_mime_type": "application/json",
                    "response_schema": _ENRICH_SCHEMA,      # 约束解码:语法层保证合法 JSON
                    # 八跑定案:思考 token 吃 max_output_tokens(仓库陷阱清单老病)——
                    # 919 字符就"截断"=思考烧掉了预算大头。转写不需要思考,关。
                    "thinking_config": {"thinking_budget": 0},
                    "media_resolution": "MEDIA_RESOLUTION_LOW"})
        usage.add_usage(resp, ENRICH_MODEL)
        try:
            return _loads_lenient(resp.text)
        except json.JSONDecodeError as e:
            last_err = e
            # 取证:坏文本落盘,别再盲修(logs/enrich_bad_*.json)
            from dvd_repro import config as _C
            os.makedirs(_C.LOGS_DIR, exist_ok=True)
            bad = os.path.join(_C.LOGS_DIR,
                               f"enrich_bad_{os.path.basename(gcs_uri)}_{int(t0)}_try{attempt}.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write(resp.text or "")
            fr = ""
            try:
                fr = str(resp.candidates[0].finish_reason)
            except Exception:
                pass
            print(f"      ⚠ 窗[{t0:.0f}s] 第{attempt}次 JSON 仍坏(finish={fr}),原文已存 {bad}")
    raise last_err


def _shift(segments: list, t0: float, win_len: float) -> list:
    """窗内时间戳 → 全片绝对时间。模型对裁剪窗一般给相对时间(0 起);
    若它已给绝对时间(max end 明显超窗长)则不再平移。"""
    if not segments:
        return []
    try:
        mx = max(float(s.get("end_s") or 0) for s in segments if isinstance(s, dict))
    except ValueError:
        mx = 0
    rel = mx <= win_len * 1.25 + 10
    out = []
    for s in segments:
        if not isinstance(s, dict):
            continue
        s = dict(s)
        try:
            if rel:
                s["start_s"] = float(s.get("start_s") or 0) + t0
                s["end_s"] = float(s.get("end_s") or 0) + t0
        except (TypeError, ValueError):
            pass
        out.append(s)
    return out


def enrich_video_chunked(video_id: str, gcs_uri: str, duration: float,
                         guard: BudgetGuard, meter: UsageMeter) -> dict:
    merged = {"title": "", "caption": "", "has_speech": False, "language": None, "segments": []}
    wins = _windows(duration)
    for i, (t0, t1) in enumerate(wins):
        data = _call_window(gcs_uri, t0, t1)
        guard.charge(meter.delta(), note=f"enrich_chunk {video_id} w{i}[{t0:.0f}-{t1:.0f}s]")
        if i == 0:
            merged["title"] = str(data.get("title") or "")
            merged["caption"] = str(data.get("caption") or "")
            merged["language"] = data.get("language")
        merged["has_speech"] = merged["has_speech"] or bool(data.get("has_speech"))
        merged["segments"].extend(_shift(data.get("segments") or [], t0, t1 - t0))
        print(f"      窗 {i+1}/{len(wins)} [{t0:.0f}-{t1:.0f}s] ✓ 段数累计 {len(merged['segments'])}")

    # 上下文与标题回填(照 enrich_video 尾部逻辑,fail-open)
    context = {"title": "", "activities": []}
    try:
        row = _execute("SELECT title FROM video_metadata WHERE video_id = %s", (video_id,))
        cur_title = str(row[0][0] or "") if row else ""
        gen_title = merged["title"].strip()
        if gen_title and (not cur_title or cur_title == video_id):
            _execute("UPDATE video_metadata SET title = %s WHERE video_id = %s "
                     "AND (title IS NULL OR title = '' OR title = video_id)",
                     (gen_title[:120], video_id))
            cur_title = gen_title
        context["title"] = cur_title if cur_title != video_id else gen_title
    except Exception:
        pass

    entries = entries_from_enrichment(video_id, merged, context)
    vecs = embed_texts([e[1][1] for e in entries]) if entries else []
    written = 0
    for (source, entry), vec in zip(entries, vecs):
        if index_entry(video_id, source, entry, vec_literal(vec)):
            written += 1
    return {"video_id": video_id, "windows": len(wins), "has_speech": merged["has_speech"],
            "segments": sum(1 for s, _ in entries if s == "transcript"), "rows": written}


def main() -> int:
    vid = sys.argv[1]
    row = _execute("SELECT gcs_uri, duration_sec FROM video_metadata WHERE video_id = %s", (vid,))
    if not row:
        print(f"{vid} 不在 video_metadata")
        return 1
    gcs_uri, dur = row[0][0], float(row[0][1] or 0)
    guard = BudgetGuard(run_id=f"enrich_chunked_{vid}")
    meter = UsageMeter()
    stats = enrich_video_chunked(vid, gcs_uri, dur, guard, meter)
    print(f"✓ {stats} · 本场 ${guard.spent_run():.2f} · 项目累计 ${guard.spent_total():.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
