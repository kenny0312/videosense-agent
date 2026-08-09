"""P0-7:T2 定位 gold 的【库外】预标(docs/longhorizon-multiagent-plan.md §3.2,红队 C4)。

为什么要库外:T2 题问"这个动作在第几秒到第几秒"。库里 video_facts 已经有 start_ts,
直接拿它当 gold 等于允许被测 agent 查库抄答案 —— 测的就不是"看视频的能力"。
所以 gold 必须由【独立的一次观看】产生,且:
  · 不用 flash 考 flash(同族相关误差)→ 用 pro 预标;
  · 双次独立标注交叉,分歧大的条目降级(只进集合判分,不进定位判分);
  · 每条记置信度,低置信同样降级。
产出写回 evals/longhorizon_bank.{split}.json 的 gold_localization
(status: labeled / low_confidence),judge/scorer 据此决定算不算定位分。

跑法(花钱,pro 档):
  python -m evals.longhorizon_prelabel --split dev --limit 3 --budget 1.0   # 试
  python -m evals.longhorizon_prelabel --split holdout --budget 3.0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROMPT = (
    "你在给一个视频理解基准做【标准答案】。请只依据你【实际看到的画面】回答,不要猜。\n"
    "问题:视频里【{pred_zh}】这个动作,最清晰的一次发生在哪个时间段?\n"
    "只输出一个 JSON:{{\"present\": true/false, \"start_ts\": 秒(数字), \"end_ts\": 秒(数字), "
    "\"confidence\": \"high\"|\"medium\"|\"low\", \"evidence\": \"你看到了什么(一句话)\"}}\n"
    "· 视频里确实没有这个动作 → present=false,时间给 null;\n"
    "· 画质太差/看不清 → confidence=\"low\";\n"
    "· 时间段取【动作本身】的起止,别把前后铺垫算进去;\n"
    "· start_ts / end_ts 必须是【从视频开头算起的总秒数】,纯数字。"
    "3 分 37 秒要写 217,【不要】写 \"3:37\"、不要写 3.37、不要写分钟 —— "
    "实测两次标注一个给 217、一个给 2.17,描述的是同一个画面却被判成分歧。"
)

PRED_ZH = {
    "falling": "有人摔倒", "celebrating": "有人在庆祝", "playing tennis": "有人在打网球",
    "diving": "有人跳水", "playing dodgeball": "有人在玩躲避球",
    "performing gymnastics": "有人在做体操(翻腾/平衡动作)",
    "rock climbing": "有人在攀岩", "dribbling basketball": "有人在运篮球",
    "playing water polo": "有人在打水球",
}


_LABEL_RETRIES = 2       # 首发 + 至多 2 次重试(与 analyze 那条路同口径)


def _label_once(video_id: str, predicate: str, model: str) -> dict:
    """看一遍视频出一次标注。异常 → {'error': ...}(不炸整批)。

    【为什么直接调生成层,不走 analyze_with_outcome】:预标有它【自己的】输出契约
    ({present, start_ts, end_ts, confidence, evidence}),而通用 analyze 用
    `AnalyzeResult` 校验 —— 那个信封 `answer` 是硬要求、`evidence_ts` 只有单个时刻,
    装不下起止对。走它必然 ValidationError。
    实测证据:本脚本的 6 道题至今全是 `pending_prelabel` —— 它从来没成功跑过。
    A4 之前这个失败被伪装成"看过了、结论是看不清"的信封,所以一直没人发现。
    """
    from pipeline.node_executor import _resolve_gcs
    from perception.analyze_video_contextual import MODEL_OVERRIDE, _gemini_generate
    gcs = _resolve_gcs(video_id)
    if not gcs:
        return {"error": "no gcs_uri"}
    q = PROMPT.format(pred_zh=PRED_ZH.get(predicate, predicate))
    tok = MODEL_OVERRIDE.set(model)          # 预标走 pro(红队 C4:别用 flash 考 flash)
    last = ""
    try:
        for _ in range(_LABEL_RETRIES + 1):
            try:
                raw = _gemini_generate(gcs, q, None)
            except Exception as e:
                last = repr(e)[:200]
                continue
            m = re.search(r"\{.*\}", str(raw or ""), re.S)
            if not m:
                last = f"no json: {str(raw)[:160]}"
                continue
            try:
                d = json.loads(m.group(0))
            except Exception as e:
                last = f"bad json: {e}"
                continue
            if isinstance(d, dict):
                # 时间戳归一:模型有时会写 "3:37" 或 3.37 来表示 3 分 37 秒。
                # 实测踩过 —— 两次标注一个给 217、一个给 2.17,描述的是同一个画面,
                # 却被 IoU 判成"分歧"。复用 analyze 那边久经考验的 _to_seconds
                # (认 mm:ss / hh:mm:ss / 数字),别在这儿另写一套。
                from perception.analyze_video_contextual import _to_seconds
                for k in ("start_ts", "end_ts"):
                    if d.get(k) is not None:
                        d[k] = _to_seconds(d[k])
                return d
            last = f"not an object: {str(d)[:120]}"
    finally:
        MODEL_OVERRIDE.reset(tok)
    return {"error": last or "unknown"}


def _merge(a: dict, b: dict) -> dict:
    """两次独立标注交叉:都说有、时段重叠够(IoU≥0.3 或起点差≤10s)→ labeled(取均值);
    否则 low_confidence(只进集合判分,不进定位判分)。"""
    if a.get("error") or b.get("error"):
        return {"status": "low_confidence", "why": "标注失败", "a": a, "b": b}
    if not (a.get("present") and b.get("present")):
        return {"status": "low_confidence", "why": "至少一次判定为不存在", "a": a, "b": b}
    try:
        as_, ae = float(a["start_ts"]), float(a["end_ts"])
        bs, be = float(b["start_ts"]), float(b["end_ts"])
    except (TypeError, ValueError, KeyError):
        return {"status": "low_confidence", "why": "时间字段缺失/非法", "a": a, "b": b}
    inter = max(0.0, min(ae, be) - max(as_, bs))
    union = max(ae, be) - min(as_, bs)
    iou = inter / union if union > 0 else 0.0
    agree = iou >= 0.3 or abs(as_ - bs) <= 10.0
    lowconf = "low" in (str(a.get("confidence")) + str(b.get("confidence"))).lower()
    if not agree or lowconf:
        return {"status": "low_confidence",
                "why": f"两次标注分歧(IoU={iou:.2f})" if not agree else "自评置信度低",
                "a": a, "b": b}
    return {"status": "labeled", "start_ts": round((as_ + bs) / 2, 1),
            "end_ts": round((ae + be) / 2, 1), "iou": round(iou, 2),
            "evidence": a.get("evidence"), "a": a, "b": b}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("dev", "holdout"))
    ap.add_argument("--limit", type=int, default=None, help="只标前 N 道 T2 题")
    ap.add_argument("--vids-per-item", type=int, default=2, help="每题标几个视频")
    ap.add_argument("--budget", type=float, default=3.0)
    ap.add_argument("--model", default=None, help="预标模型(默认 pro,红队 C4:别用 flash 考 flash)")
    a = ap.parse_args()

    from pipeline import config
    from pipeline.agentops import usage
    model = a.model or "gemini-2.5-pro"
    path = ROOT / "evals" / f"longhorizon_bank.{a.split}.json"
    bank = json.loads(path.read_text(encoding="utf-8"))
    items = [i for i in bank["items"] if i["tier"] == "T2"]
    if a.limit:
        items = items[:a.limit]

    usage.reset_usage()
    spent = 0.0
    print(f"[prelabel] split={a.split} 题数={len(items)} 每题 {a.vids_per_item} 视频 "
          f"×2 次独立标注 模型={model} 预算 ${a.budget:.2f}", flush=True)
    for it in items:
        spans = {}
        for vid in it["gold"]["video_ids"][:a.vids_per_item]:
            if spent >= a.budget:
                print(f"  [停] 预算 ${a.budget:.2f} 用尽", flush=True)
                break
            t0 = time.perf_counter()
            r1 = _label_once(vid, it["predicate"], model)
            r2 = _label_once(vid, it["predicate"], model)
            merged = _merge(r1, r2)
            spans[vid] = merged
            spent = float(usage.summarize().get("cost_usd") or 0.0)
            print(f"  {it['id'][:26]:26s} {vid[:14]:14s} {merged['status']:15s} "
                  f"{time.perf_counter()-t0:.0f}s 累计 ${spent:.3f}", flush=True)
        ok = {v: s for v, s in spans.items() if s["status"] == "labeled"}
        it["gold_localization"] = {
            "status": "labeled" if ok else "low_confidence",
            "labeler": f"{model} ×2 交叉", "spans": {v: {"start_ts": s["start_ts"],
                                                        "end_ts": s["end_ts"]} for v, s in ok.items()},
            "raw": spans,
        }
        if spent >= a.budget:
            break
    # 【写回就要重新冻结】。冻结元数据(frozen_at + items_sha256)是"内容有没有被悄悄改过"
    # 的唯一凭据,而本脚本就是【合法地】改 items 的那一方。不更新哈希会留下一个永久失配:
    # 之后任何一次真正的意外漂移都被这条既有失配盖住,冻结纪律就废了
    # (实测踩过:B0-5 跑完 test_freeze_metadata_has_teeth 直接红)。
    import datetime
    import hashlib
    bank["meta"]["items_sha256"] = hashlib.sha256(
        json.dumps(bank["items"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    bank["meta"]["frozen_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    bank["meta"]["last_prelabel"] = {"at": bank["meta"]["frozen_at"], "model": model,
                                     "spent_usd": round(spent, 4)}
    path.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")
    n_lab = sum(1 for i in bank["items"]
                if i.get("gold_localization", {}).get("status") == "labeled")
    print(f"\n[prelabel] 完成:{n_lab} 题拿到可判定位 gold;总花费 ${spent:.3f}", flush=True)
    print(f"  已写回 {path.name}(重跑 build 脚本会报库漂移 —— 那是预期,gold 本来就该变)")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
