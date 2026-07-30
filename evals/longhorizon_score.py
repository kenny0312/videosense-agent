"""P0-7:gate 实验判分器 —— 确定性部分(docs/longhorizon-multiagent-plan.md §3.4)。

尺子(三臂同一):
  · 答案契约:结构化 JSON {video_ids, count, per_video:{category, start_ts, end_ts, evidence}};
    解析失败走同一 regex 兜底(只捞 video_ids,细分项记 parse_failure —— 按臂单列,红队 F2);
  · 集合 = F1(确定性);归类 = 受控词表准确率(确定性);
  · 【无排序轴】:任务书原案"时间线"在现库不可判(511/514 同秒批量入库),换"时长排序"后
    review 又实测 496/514 duration_sec 为 NULL → gold 全退化成 id 字典序(真排时长反被扣分,
    方向性反奖励)—— 两个候选轴都死,整轴砍掉,T1 确定性 = 0.6 F1 + 0.4 归类;
  · T2 定位 = ±10s 或 IoU≥0.3 vs 【库外 gold】(确定性;gold 预标未完成时【拒算】不给 0 ——
    把"不可判"伪装成 0 分会污染 C−B 差);
  · 空集探针 = 精确判空 + 零编造(契约里列出任何 video_id 直接 0);答案没走 JSON 契约
    且带弃权语时【拒判】(needs_review)—— regex 兜底分不清"列举的"与"排查后排除的" id,
    把证据链式诚实弃权判成编造是往探针轴里灌反向噪声(review 实测确认);
  · 合成:T1 = 0.8×确定性(0.6 F1 + 0.4 归类)+ 0.2×judge;
        T2 = 0.5×F1 + 0.3×定位 + 0.2×judge。judge 分是 live 步骤,传 None 时合成分
        只报确定性部分并置 judge_pending=True(不许静默当 0 或当满分)。
离线纯函数,零 API 零 DB;单测在 tests/evals/test_longhorizon_score.py。
"""
from __future__ import annotations

import json
import re
from typing import Any

_VID_RE = re.compile(r"v_[A-Za-z0-9_-]{6,}")


_MAX_IDS = 200          # 病态重复的长输出会把 Kendall O(n²) 拖死(实测 2 万 id 12.9s)→ 截断


def _brace_candidates(text: str) -> list:
    """收集全部顶层 {...} 平衡块 —— 【字符串感知】:evidence 自由文本里出现 '}' 时,
    朴素深度计数会在字符串内提前归零把合法契约块腰斩(review 实测确认)。"""
    out, stack, in_str, esc = [], [], False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if not stack:                       # 顶层块闭合
                out.append(text[start:i + 1])
    return out


def parse_answer(text: str) -> dict:
    """答案文本 → {video_ids, count, per_video, parse_failure}。
    JSON 契约:按长度降序逐候选试 loads(只认最大块会被更长的非法块遮蔽,review 实测);
    失败走 regex 兜底(三臂同一条 regex,红队判分对称关)。两路都去重+截断
    (JSON 路不去重会让重复 id 在 Kendall 里当逆序对扣分 —— 对格式合规者的反向惩罚)。"""
    text = text or ""
    for cand in sorted(_brace_candidates(text), key=len, reverse=True):
        try:
            d = json.loads(cand)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("video_ids"), list):
            per = d.get("per_video") if isinstance(d.get("per_video"), dict) else {}
            ids = list(dict.fromkeys(str(v) for v in d["video_ids"]))[:_MAX_IDS]
            return {"video_ids": ids, "count": d.get("count"), "per_video": per,
                    "parse_failure": False}
    return {"video_ids": list(dict.fromkeys(_VID_RE.findall(text)))[:_MAX_IDS],
            "count": None, "per_video": {}, "parse_failure": True}


def set_f1(pred_ids: list, gold_ids: list) -> float:
    p, g = set(pred_ids), set(gold_ids)
    if not p and not g:
        return 1.0
    if not p or not g:
        return 0.0
    tp = len(p & g)
    prec, rec = tp / len(p), tp / len(g)
    return 0.0 if tp == 0 else 2 * prec * rec / (prec + rec)


def category_accuracy(per_video: dict, gold: dict, vocab: list) -> float:
    """受控词表归类:对每个 gold 视频,预测 category ∈ 该视频的 gold 类目集 → 对。
    预测缺失/不在词表 → 错。分母 = gold 视频数(漏答也是错,防"少答少错")。"""
    gold_pv = gold.get("per_video") or {}
    if not gold_pv:
        return 1.0
    vs = set(vocab)
    ok = 0
    for vid, ginfo in gold_pv.items():
        got = per_video.get(vid) or {}
        cat = str(got.get("category") or "").strip().lower()
        if cat and cat in {v.lower() for v in vs} \
                and cat in {c.lower() for c in (ginfo.get("categories") or [])}:
            ok += 1
    return ok / len(gold_pv)


def localization_score(per_video: dict, gold_loc: dict) -> "float | None":
    """T2 定位:每个有 gold 时段的视频,|Δstart|≤10s 或 IoU≥0.3 → 1。
    gold 预标未完成(status != labeled)→ 返回 None =【拒算】,调用方必须区别对待。"""
    if (gold_loc or {}).get("status") != "labeled":
        return None
    spans = gold_loc.get("spans") or {}
    if not spans:
        return None
    ok = 0
    for vid, g in spans.items():
        got = per_video.get(vid) or {}
        try:
            ps, pe = float(got.get("start_ts")), float(got.get("end_ts"))
            gs, ge = float(g["start_ts"]), float(g["end_ts"])
        except (TypeError, ValueError, KeyError):
            continue
        inter = max(0.0, min(pe, ge) - max(ps, gs))
        union = max(pe, ge) - min(ps, gs)
        iou = inter / union if union > 0 else 0.0
        if abs(ps - gs) <= 10.0 or iou >= 0.3:
            ok += 1
    return ok / len(spans)


_ABSTAIN_MARKS = ("没有", "不存在", "未找到", "找不到", "库里没",
                  "no video", "not found", "none of", "no such")


def probe_score(pred_ids: list, answer_text: str, parse_failure: bool = False) -> dict:
    """空集探针:精确判空 = 1;契约里列出任何 video_id = 0(编造)。
    【拒判】(score=None, needs_review=True):没走 JSON 契约 + 带弃权语 + regex 捞到 id ——
    兜底分不清"我找到 v_x"与"我排查过 v_x,不是"(review 实测:证据链式诚实弃权被判 0,
    探针 0/1 二值,单次误判翻转整轴)。探针只有 2 道,人工复核成本 ≈ 0。"""
    fabricated = len(pred_ids)
    abstained = any(k in (answer_text or "").lower() for k in _ABSTAIN_MARKS)
    if parse_failure and abstained and fabricated:
        return {"score": None, "needs_review": True,
                "fabricated_ids": fabricated, "abstain_language": abstained}
    return {"score": 1.0 if fabricated == 0 else 0.0, "needs_review": False,
            "fabricated_ids": fabricated, "abstain_language": abstained}


def score_item(item: dict, answer_text: str, vocab: list,
               judge: "float | None" = None) -> dict:
    """单题总入口。返回各分项 + composite;judge=None → judge_pending=True,
    composite 只含确定性部分(权重不重排 —— 缺项就是缺项,不许偷偷归一化成满分)。"""
    parsed = parse_answer(answer_text)
    gold = item["gold"]
    out: dict[str, Any] = {"parse_failure": parsed["parse_failure"],
                           "judge_pending": judge is None}
    if item["tier"] == "PROBE":
        out.update(probe_score(parsed["video_ids"], answer_text,
                               parse_failure=parsed["parse_failure"]))
        return out
    f1 = set_f1(parsed["video_ids"], gold["video_ids"])
    out["set_f1"] = f1
    if item["tier"] == "T1":
        cat = category_accuracy(parsed["per_video"], gold, vocab)
        out["category_acc"] = cat
        det = 0.6 * f1 + 0.4 * cat
        out["deterministic"] = det
        out["composite"] = 0.8 * det + (0.2 * judge if judge is not None else 0.0)
    else:                                                   # T2
        loc = localization_score(parsed["per_video"], item.get("gold_localization") or {})
        out["localization"] = loc                           # None = gold 未预标,拒算
        out["composite"] = (0.5 * f1
                            + (0.3 * loc if loc is not None else 0.0)
                            + (0.2 * judge if judge is not None else 0.0))
        out["localization_pending"] = loc is None
    return out
