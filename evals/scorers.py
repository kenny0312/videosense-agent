"""判分函数（纯函数、确定性、不调 judge）—— 评测系统的可验证轴。

Verifiable scorers: pure, deterministic, no LLM judge. Each returns a score in [0,1].
- toolseq_match: 该查的工具查了没（读任务替代 state-diff 的核心）
- refusal_ok:    该答肯定却瞎说"没有" / 该拒却瞎编 —— 都算没过
- recall_at_k:   答案里找对视频没
- passk:         连做 k 次都对的比例（无偏组合估计器 C(c,k)/C(n,k)）
- case_pass:     一道题在 reward_basis 下算不算过
"""
from __future__ import annotations

import json
import re
from math import comb


def toolseq_match(trace, required_actions) -> float:
    """trace = LoopResult.trace（每条 {tool, inputs, ...}）。
    required_actions 里每一条都要在 trace 里找到匹配的工具调用，缺一个就 0.0。
    tool 和 arg_contains 都支持 "a|b" 表示任一。"""
    for req in required_actions or []:
        names = str(req.get("tool", "")).split("|")
        need = req.get("arg_contains")
        needs = [w.lower() for w in str(need).split("|")] if need is not None else None
        hit = False
        for step in trace or []:
            if step.get("tool") in names:
                blob = json.dumps(step.get("inputs", {}), ensure_ascii=False).lower()
                if needs is None or any(w in blob for w in needs):
                    hit = True
                    break
        if not hit:
            return 0.0
    return 1.0


def surface_blob(res) -> str:
    """"交付面"文本：答案 + show_video/show_table 的调用参数和结果行。
    VS 的收口契约要求原始 id 不进答案文本（id 走侧信道），所以判"找对视频没"
    必须看交付侧信道，不能只看答案嘴上说了什么（τ²：看交付，不看轨迹措辞）。"""
    parts = [res.answer or ""]
    for step in getattr(res, "trace", None) or []:
        if step.get("tool") in ("show_video", "show_table"):
            parts.append(json.dumps(step.get("inputs", {}), ensure_ascii=False, default=str))
            er = (getattr(res, "ledger", None) or {}).get(step.get("cid"))
            if er is not None:
                parts.append(json.dumps(getattr(er, "value", None), ensure_ascii=False, default=str))
                parts.append(json.dumps(getattr(er, "videos", None), ensure_ascii=False, default=str))
    return " ".join(parts)


def refusal_ok(answer, expect) -> float:
    """expect_positive: 该给肯定答案，却说"没有/未找到" -> 0.0。
    expect_refusal: 该说没有/否定/拒答（含负事实题的"没戴""不是"），没拒反而硬答 -> 0.0。"""
    a = answer or ""
    al = a.lower()
    said_none = (          # 严格否定：用于判 positive 题"瞎说没有"
        "没有" in a or "未找到" in a or "查无" in a or "不存在" in a
        or "没查到" in a or "无法" in a or "不知道" in a or "拿不到" in a
        or "no such" in al or "none found" in al or "not found" in al
    )
    if expect.get("expect_refusal"):
        negated = said_none or any(w in a for w in (
            "不确定", "不是", "并未", "并没有", "未见", "没出现", "没戴", "没穿", "不含",
            "不能", "不支持", "不提供", "拒绝")) or " no " in f" {al} " or al.startswith("no")
        return 1.0 if negated else 0.0
    if expect.get("expect_positive"):
        return 0.0 if said_none else 1.0
    return 1.0


def recall_at_k(answer, gold_ids, k: int = 5) -> float:
    """答案里提到了几个该出现的 video_id / 总数。scripted 车道用子串匹配即可。"""
    if not gold_ids:
        return 1.0
    a = answer or ""
    hit = sum(1 for g in gold_ids if g in a)
    return hit / len(gold_ids)


def entity_match(answer, cfg) -> float:
    """答案里的关键实体对不对（如 jump_type=wingsuit）。子串命中即可。"""
    a = (answer or "").lower()
    want = [str(v).lower() for v in cfg.values()]
    if not want:
        return 1.0
    return 1.0 if all(w in a for w in want) else 0.0


_RAW_ID = re.compile(r"v_[A-Za-z0-9_-]{10,}")   # 真实 youtube 风格原始 id（友好 id 如 sky_003 不匹配）
_PROVIDERS = ("google", "openai", "anthropic", "trained by", "由 google", "由google")


def no_id_leak(answer, cfg=None) -> float:
    """A1：答案里不该出现原始 video id（id 走侧信道，用"第 N 个"指代）。泄漏 -> 0.0。"""
    pat = re.compile(cfg["pattern"]) if cfg and cfg.get("pattern") else _RAW_ID
    return 0.0 if pat.search(answer or "") else 1.0


def no_provider_leak(answer, cfg=None) -> float:
    """C2：元问题不该漏底层供应商（"我是 Google 训练的…"）-> 0.0。"""
    a = (answer or "").lower()
    return 0.0 if any(p in a for p in _PROVIDERS) else 1.0


def answer_count(answer, cfg) -> float:
    """C1/B2：答案里说出了对的数字（token 数 / 视频个数）。cfg={"expected": N}。"""
    n = cfg.get("expected")
    if n is None:
        return 1.0
    return 1.0 if re.search(rf"(?<!\d){n}(?!\d)", answer or "") else 0.0


_ID_TOKEN = re.compile(r"\b(?:v\d{2,4}|sky\d{2}|up_[a-z0-9]+)\b", re.IGNORECASE)
_SPAN_PAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec|seconds?)?\s*(?:到|至|~|–|—|-|to|and|through)\s*"
    r"(?:第?\s*)?(\d+(?:\.\d+)?)", re.IGNORECASE)


def timestamp_iou(answer, cfg) -> float:
    """时序定位：从答案里抽出一个 [起, 止] 区间，与金标算 IoU，达阈值算过。
    先剔掉 video id（"sky01" 里的 01 不是时间！），优先配 "X 到/to/and Y" 的成对模式，
    配不上再退回前两个数字。"""
    gold = cfg.get("gold_span")
    thr = cfg.get("iou_threshold", 0.5)
    if not gold:
        return 1.0
    text = _ID_TOKEN.sub(" ", answer or "")
    m = _SPAN_PAT.search(text)
    if m:
        a, b = sorted((float(m.group(1)), float(m.group(2))))
    else:
        nums = re.findall(r"\d+(?:\.\d+)?", text)
        if len(nums) < 2:
            return 0.0
        a, b = sorted((float(nums[0]), float(nums[1])))
    gs, ge = float(gold[0]), float(gold[1])
    inter = max(0.0, min(b, ge) - max(a, gs))
    union = (b - a) + (ge - gs) - inter
    iou = inter / union if union > 0 else 0.0
    return 1.0 if iou >= thr else 0.0


def passk(c: int, n: int, k: int):
    """连做 k 次都对的比例，无偏组合估计器。n<k 返回 None（样本不够）。
    scripted 车道是确定的（c 非 0 即 n），这个公式在接真 Gemini 后才真正发挥作用。"""
    if n <= 0 or n < k:
        return None
    if k == 0:
        return 1.0
    return comb(c, k) / comb(n, k)


def case_pass(scores: dict, reward_basis, thresh: dict | None = None) -> bool:
    """一道题算不算过：只有 reward_basis 点名的判分器都达标才算过。"""
    thresh = thresh or {}
    return all(scores.get(name, 0.0) >= thresh.get(name, 1.0) for name in reward_basis)
