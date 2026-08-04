# -*- coding: utf-8 -*-
"""批 5-B 核验包跑机:任务书 §4 的三个包,一次跑完、逐条落盘。

为什么单独一个跑机而不是手工调:
  · 核验是 gold 修订的【闸门】(包 1 不过不许动 T2 gold 时段)—— 证据必须可复现、
    可归档,"我看过了"不算数;
  · 问题文本就是裁决口径的一部分,写死在这里 = 下次复核用同一把尺;
  · 每条落盘带成本,总账必须能对上任务书的 ≤$0.6 上限。

跑法:
  PYTHONUTF8=1 EVAL_READ_ONLY=1 python -m evals.longhorizon_verify_pack
输出:evals/runs/verify-pack-5B.jsonl(逐条)+ stdout 汇总。

安全:EVAL_READ_ONLY=1 挡语义索引写入;analyze 缓存键含问题文本,不会污染跑批缓存。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "runs" / "verify-pack-5B.jsonl"

# ── 核验清单(任务书 §4 原样;问题 = 裁决口径)─────────────────────────
# 每条:(包, video_id, 档, time_range, 问题)
CHECKS = [
    # 包 1 多实例仲裁(pro)—— T2 gold 时段内容的闸门
    ("P1", "v_0AbJgWxIYVI", "pro", None,
     "这段视频里【庆祝场面】(欢呼/拥抱/庆祝动作)分别出现在哪几个时间段?"
     "请逐段给出起止秒。特别核对:16-26 秒之间有没有庆祝画面?104-114 秒之间有没有?"
     "两处各自是什么内容?"),
    ("P1", "v_-NM-0NZXRNw", "pro", None,
     "请核对两个时间点:① 217-218 秒(约 3:37)处画面是什么,是否属于庆祝场面?"
     "② 296-298 秒处是不是领奖台/颁奖画面?若是,它算不算『庆祝场面』?"
     "另外整段视频里还有哪些明显的庆祝时刻,逐一给出起止秒。"),
    ("P1", "v_-jNouTszLJ0", "pro", None,
     "这段视频里【庆祝场面】出现在哪几个时间段?逐段给出起止秒。特别核对:"
     "① 62-70 秒有没有庆祝?② 96-99 秒和 101-104 秒是同一次庆祝还是两次?"
     "③ 136-139 秒有没有庆祝?"),
    ("P1", "v_-jNouTszLJ0", "pro", (100.0, 120.0),
     "只看这个片段:里面有没有庆祝场面?若有,给出(相对整段视频的)起止秒。"),
    ("P1", "v_g7l-Y_bgPkI", "pro", None,
     "0-26 秒之间画面里的人是否全程在打网球(含发球/回球/跑动)?"
     "若不是全程,打网球的动作具体是哪几段(起止秒)?23-25 秒在干什么?"),
    # 包 2 T2 变体入册(flash)—— 红队 C4:库外过眼才准进 gold
    ("P2", "v_0gw1Qq3WRbU", "flash", None,
     "这段视频的主要内容是【跳水】(从跳板/跳台/岸边跃入水中)还是【在泳池里嬉水玩耍】?"
     "给出你判断的画面依据和关键时刻的秒数。"),
    ("P2", "v_j18sB8o2IQw", "flash", None,
     "这段视频的主要内容是【跳水】(从跳板/跳台/岸边跃入水中)还是【在泳池里嬉水玩耍】?"
     "给出你判断的画面依据和关键时刻的秒数。"),
    ("P2", "v_0F8F-ON083s", "flash", None,
     "这段视频的主要内容是【跳水】(从跳板/跳台/岸边跃入水中)还是【在泳池里嬉水玩耍】?"
     "给出你判断的画面依据和关键时刻的秒数。"),
    ("P2", "v__AKzq9X1Aik", "flash", None,
     "这段视频里的人在做什么运动?有没有体操类的翻腾、倒立、支撑或平衡动作"
     "(如 L-sit、双杠撑体)?具体动作和时间段是什么?"),
    # 包 3 driving 口径裁决(flash)
    ("P3", "v_px9935090", "flash", None,
     "画面里能否辨认出【正在被驾驶的汽车】(车在动、有人在开)?"
     "画面清晰度如何(是否虚化/夜景/只有车灯)?如实描述你能看清什么、看不清什么。"),
    ("P3", "v_CbfgZlo0Ut4", "flash", None,
     "画面里有没有人【正在驾驶汽车】?是从车内拍驾驶员,还是车外拍行驶中的车?"
     "给出依据画面和秒数。"),
    ("P3", "v_-OH1BDqao9w", "flash", None,
     "画面里有没有人【正在驾驶汽车】?是从车内拍驾驶员,还是车外拍行驶中的车?"
     "给出依据画面和秒数。"),
    ("P3", "v_9pJBfTZOcxI", "flash", None,
     "画面里有没有人【正在驾驶汽车】?注意区分:开汽车、开船/摩托艇、被拖曳滑水。"
     "给出依据画面和秒数。"),
]


def _gcs(vid: str) -> "str | None":
    from pipeline import mcp_client
    rows = mcp_client.query_db(
        f"SELECT gcs_uri FROM video_metadata WHERE video_id = '{vid}' LIMIT 1")
    return rows[0].get("gcs_uri") if rows else None


def main() -> int:
    from perception import analyze_video_contextual as avc
    from pipeline.agentops import usage

    usage.reset_usage()
    done, results = 0, []
    with io.open(OUT, "a", encoding="utf-8") as f:
        for pack, vid, tier, tr, q in CHECKS:
            gcs = _gcs(vid)
            if not gcs:
                print(f"  !! {vid}: 查不到 gcs_uri,跳过")
                continue
            tok = avc.MODEL_OVERRIDE.set(avc.PRO_MODEL if tier == "pro" else None)
            t0 = time.perf_counter()
            try:
                out = avc.analyze_with_outcome(
                    avc.AnalyzeRequest(question=q, time_range=list(tr) if tr else None), gcs)
            finally:
                avc.MODEL_OVERRIDE.reset(tok)
            rec = {"pack": pack, "video_id": vid, "tier": tier, "time_range": tr,
                   "question": q, "ok": out.ok, "error_code": out.error_code,
                   "answer": out.result.answer if out.ok else out.error,
                   "confidence": out.result.confidence if out.ok else None,
                   "wall_s": round(time.perf_counter() - t0, 1),
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()                                  # 每条落盘:被杀也不丢已花的钱
            done += 1
            results.append(rec)
            print(f"  [{done}/{len(CHECKS)}] {pack} {vid} ({tier}"
                  f"{' ' + str(tr) if tr else ''}) ok={out.ok} {rec['wall_s']}s")
    s = usage.summarize()
    print(f"\n[verify-pack] 共 {done} 条,累计 ≈ ${s.get('cost_usd', 0):.4f}"
          f"({s.get('llm_calls', 0)} 次调用)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
