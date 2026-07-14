"""时间戳独立验尺:跨模型(2.5-pro)+ 默认分辨率 + 窗口判定。
    python -m perception.audit_spans   # 抽 60 条 machine facts,产出 span_audit_report.json

与重抽仪器(flash+LOW)不同源,消自证循环;模型只报"我在第 X 秒看到/听到它",
span 对错由数值规则判(X ∈ [start-5, end+5]),消手工分类。n=60,门槛 <5%(≤2 错)。
"""
import json
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
OUT = "span_audit_report.json"

import pipeline.config  # noqa: E402
from pipeline.semantic_index import _execute  # noqa: E402

rng = random.Random(99)
rows = _execute(
    "SELECT f.video_id, f.predicate, f.start_ts, f.end_ts, m.gcs_uri "
    "FROM video_facts f JOIN video_metadata m USING (video_id) "
    "WHERE f.matched = true AND f.verified = 'machine' AND m.gcs_uri LIKE 'gs://%%'", ())
rng.shuffle(rows)
seen, sample = set(), []
for r in rows:                                   # 一视频最多一条,摊开覆盖
    if r[0] in seen:
        continue
    seen.add(r[0])
    sample.append(r)
    if len(sample) >= 60:
        break
print(f"独立验尺样本 {len(sample)} 条(machine 池 {len(rows)})", flush=True)

from google.genai import types  # noqa: E402
from pipeline.genai_client import get_client  # noqa: E402

# 直接验"存储的窗口":重复事件两模型各选一个合法时刻会被误判,所以不比时刻,验窗口
P = ('Watch the video, then focus on the time window {a}s to {b}s. '
     'Is "{pred}" visible or clearly audible WITHIN that window? '
     'Reply STRICT JSON: {"in_window": true|false, "evidence": "<one sentence about '
     'what happens in that window>"}.')

ok = span_bad = absent = err = 0
results = []
for i, (vid, pred, s0, e0, uri) in enumerate(sample, 1):
    mime = "video/quicktime" if uri.lower().endswith(".mov") else "video/mp4"
    verdict = {"found": None}
    for attempt in (0, 1):
        try:
            resp = get_client().models.generate_content(
                model="gemini-2.5-pro",
                contents=[types.Part(file_data=types.FileData(file_uri=uri, mime_type=mime)),
                          P.replace("{pred}", pred)
                           .replace("{a}", str(round(max(float(s0) - 2, 0), 1)))
                           .replace("{b}", str(round(float(e0) + 2, 1)))],
                config=types.GenerateContentConfig(
                    temperature=0.0, max_output_tokens=2048,
                    response_mime_type="application/json"))
            verdict = json.loads(resp.text)
            if isinstance(verdict, list):                      # pro 偶尔套一层数组
                verdict = verdict[0] if verdict and isinstance(verdict[0], dict) else {}
            break
        except Exception as e:                                 # noqa: BLE001
            if "429" in str(e):
                time.sleep(30)
                continue
            verdict = {"found": None, "err": str(e)[:60]}
            break
    tag = "?"
    if verdict.get("in_window") is True:
        ok += 1
        tag = "√"
    elif verdict.get("in_window") is False:
        span_bad += 1
        tag = "×span"
    else:
        err += 1
        tag = "?err"
    results.append({"video_id": vid, "predicate": pred, "span": [s0, e0],
                    "verdict": verdict, "tag": tag})
    print(f"[{i}/{len(sample)}] {tag} {vid} '{pred}' span={s0}-{e0}", flush=True)

judged = ok + span_bad
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)
print(f"\n== 独立验尺(2.5-pro/默认分辨率/数值判定)==")
print(f"span 判定 {judged} 条:对 {ok} / 错位 {span_bad} = "
      f"{span_bad / max(judged, 1):.1%} 错位率(门槛 <5%)")
print(f"仪器错误: {err}")
print(f"明细 → {OUT}")
