"""M2 时间戳批:facts 时间段"证据先行"重抽 + verified 三态回写(设计 video-metadata-v2.md §2)。

治的病:真值审计实测 ~18% 的 span 指错时刻(事件在 0:20、标在片头)—— 根因是
当年抽取只要求"给区间",模型偷懒锚在开头。本脚本把因果反过来:
  提示词强制【先说你在第 X 秒看到了什么画面,再据此给区间】,时间段是证据的推论。

更新纪律(保守,防单模型误杀):
  · 模型确认可见 → 更新 start_ts/end_ts + verified='machine'
  · 确认不了     → span 不动、保持 unverified,进复查清单(不自动翻 matched)

幂等:全部 matched facts 已 verified 的视频跳过。断点重跑安全。
    python -m perception.setup_timestamps_v2 [--limit N]
"""
from __future__ import annotations

import json
import sys

from pipeline.semantic_index import _execute

MODEL = "gemini-2.5-flash"

PROMPT = (
    "You are auditing time-spans for a video-labeling system. Watch the video. "
    "For EACH predicate below, find the single strongest moment where it is VISIBLE, "
    "and answer with EVIDENCE FIRST (visible OR clearly audible both count). "
    "Output STRICT JSON array, one item per predicate, "
    "same order:\n"
    '[{"predicate": "...", "present": true|false,\n'
    '  "evidence": "one sentence: what you SEE at that moment (required if present)",\n'
    '  "start_s": 12.0, "end_s": 18.5}]\n'
    "Rules: the span must be where your evidence happens — do NOT default to the video "
    "start. If you cannot clearly see the predicate anywhere, present=false and omit the "
    "span. Predicates:\n"
)


def parse_verdicts(text: str, predicates: list[str]) -> "list[dict]":
    """解析模型输出(纯函数,离线可测):按谓词名对齐,烂项丢弃。"""
    try:
        arr = json.loads(text)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    want = {p.lower(): p for p in predicates}
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        p = want.get(str(item.get("predicate", "")).strip().lower())
        if not p:
            continue
        if item.get("present") is True:
            try:
                s, e = float(item["start_s"]), float(item["end_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= s < e):
                continue
            out.append({"predicate": p, "present": True, "start": s, "end": e,
                        "evidence": str(item.get("evidence", ""))[:200]})
        else:
            out.append({"predicate": p, "present": False})
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    _execute("ALTER TABLE video_facts ADD COLUMN IF NOT EXISTS verified TEXT "
             "DEFAULT 'unverified'", ())
    # 待处理:还有未验 matched facts 的视频(幂等断点)
    vids = _execute(
        "SELECT f.video_id, m.gcs_uri, m.duration_sec FROM video_facts f "
        "JOIN video_metadata m USING (video_id) "
        "WHERE f.matched = true AND COALESCE(f.verified,'unverified') = 'unverified' "
        "AND m.gcs_uri LIKE 'gs://%%' "
        "GROUP BY f.video_id, m.gcs_uri, m.duration_sec ORDER BY f.video_id", ())
    if limit:
        vids = vids[:limit]
    print(f"待重抽 {len(vids)} 个视频", flush=True)

    from google.genai import types
    from pipeline.genai_client import get_client
    upd = confirmed = unconfirmed = fails = rejected_oob = 0
    for i, (vid, uri, dur) in enumerate(vids, 1):
        preds = [r[0] for r in _execute(
            "SELECT predicate FROM video_facts WHERE video_id = %s AND matched = true "
            "AND COALESCE(verified,'unverified') = 'unverified'", (vid,))]
        if not preds:
            continue
        if not dur:
            print(f"[{i}/{len(vids)}] {vid} 无时长元数据,跳过(先补 duration 再跑)", flush=True)
            continue
        mime = "video/quicktime" if uri.lower().endswith(".mov") else "video/mp4"
        try:
            resp = get_client().models.generate_content(
                model=MODEL,
                contents=[types.Part(file_data=types.FileData(file_uri=uri, mime_type=mime)),
                          PROMPT + json.dumps(preds, ensure_ascii=False)],
                config=types.GenerateContentConfig(
                    temperature=0.0, max_output_tokens=4096,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW))
            verdicts = parse_verdicts(resp.text or "", preds)
            if preds and not verdicts:
                fails += 1
                print(f"[{i}/{len(vids)}] {vid} 输出不可解析(截断/空)", flush=True)
                continue
        except Exception as e:                                 # noqa: BLE001 —— 单视频失败重跑可补
            fails += 1
            print(f"[{i}/{len(vids)}] {vid} 失败: {str(e)[:60]}", flush=True)
            continue
        for v in verdicts:
            if v["present"] and v["end"] <= float(dur) + 6:
                _execute("UPDATE video_facts SET start_ts = %s, end_ts = %s, "
                         "verified = 'machine' WHERE video_id = %s AND predicate = %s "
                         "AND matched = true", (v["start"], v["end"], vid, v["predicate"]))
                upd += 1
                confirmed += 1
            elif v["present"]:
                rejected_oob += 1                              # 越界拒写,独立口径
            else:
                # 机器查过但确认不了 → 终态(重跑不再为它重付整片钱;复查清单据此导出)
                _execute("UPDATE video_facts SET verified = 'machine_unconfirmed' "
                         "WHERE video_id = %s AND predicate = %s AND matched = true "
                         "AND COALESCE(verified,'unverified') = 'unverified'",
                         (vid, v["predicate"]))
                unconfirmed += 1
        if i % 25 == 0:
            print(f"[{i}/{len(vids)}] 已确认 {confirmed} / 未确认 {unconfirmed} / 更新 {upd}",
                  flush=True)
    print(f"\n完成:视频 {len(vids)}(失败 {fails});确认 {confirmed} 条(span 更新 {upd}),"
          f"未确认 {unconfirmed} 条(进复查清单,matched 未动)", flush=True)
    # 检索侧同步(审计 B1):检索读的是 content_embeddings 里 fact 行的 span 复制品,
    # 不同步的话"跳转到证据时刻"在产品面不生效。幂等,零模型成本。
    _execute("UPDATE content_embeddings ce SET start_ts = f.start_ts, end_ts = f.end_ts "
             "FROM video_facts f WHERE ce.source = 'fact' "
             "AND ce.content_key = 'fact:' || f.video_id || ':' || f.predicate "
             "AND f.verified = 'machine' "
             "AND (ce.start_ts IS DISTINCT FROM f.start_ts "
             "OR ce.end_ts IS DISTINCT FROM f.end_ts)", ())
    print("检索侧 fact 行 span 已同步", flush=True)
    rows = _execute("SELECT COALESCE(verified,'unverified'), COUNT(*) FROM video_facts "
                    "WHERE matched = true GROUP BY 1", ())
    print("verified 分布:", rows, flush=True)


if __name__ == "__main__":
    main()
