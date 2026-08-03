"""P0-7:gate 实验判分器 —— 确定性部分(docs/longhorizon-multiagent-plan.md §3.4)。

尺子(三臂同一):
  · 【交付契约(B0-4 统一后)】判分输入是 **show_video 的工具台账**,不是答案文本:
    集合从 `surfaced`(摆出来的 video_id)取,归类/定位从 `surfaced_meta`
    (侧信道 `videos[]` 每条的 {video_id, category, start_ts, end_ts})取。
    为什么不是答案 JSON:`longhorizon_run.ANSWER_CONTRACT` 要的是自然语言 + show_video,
    因为 VS 的 scrub_ids 会把答案里的内部 id 洗成"第 N 个"(产品规则:绝不把 id 抄给用户)
    —— 要求输出 video_ids 的 JSON 会被洗掉,每一臂 set_f1 恒为 0。
    旧版只用 surfaced 覆盖了 video_ids、`per_video` 一个字没覆盖,后果实算过:
    72 次非 PROBE 跑 parse_failure **72/72**、36 次 T1 的 category_acc **一律 0.0**,
    T1 的 40% / T2 的 30% 权重结构性恒为零。台账缺席时才退回答案 JSON / regex 兜底。
  · 【跑次有效性(B0-3)】`terminated` 不在 `VALID_TERMINATED` 里 → 整条【拒判】,
    不参与判分(见该常量的注释:为什么是拒判而不是判 0);
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


# B0-3:只有【正常收口】的跑次才进判分。
#
# 为什么是【拒判】而不是【判 0】:"崩了"和"答错了"是两件不同的事,走完全不同的处置 ——
# 前者要修基础设施,后者要修 agent。混成一个 0 分,两边都看不见。
#
# 为什么这条是本批最要紧的一条:在空集探针(红线题)上,旧口径给崩溃跑次【满分】。
# 崩溃 → answer 长度 0、surfaced 为空 → "一个 id 都没编造" → probe_score 判 1.0。
# `evals/runs/gate-main-v3.jsonl` 里 `probe-golf` 的 B/C rep11 两行正是这样:
# terminated=error、error=AttributeError("'NoneType' object has no attribute 'execute'")、
# answer 长度 0,两行都拿了 1.0 —— B 臂在这条红线上"赢"C 臂,一半靠这次 AttributeError。
# 它没说"我不知道",它是崩了。
#
# 【为什么只把 error 判无效,max_steps / repeat / tree_guard 都照常判分】
# 第一版按任务书字面写成 `{"text"}`,实算之后改了 —— 全部 204 行的实况:
#   text 170 / max_steps 22 / error 12
#   max_steps 里【真的 show_video 摆出了视频】的有 7 行(各 2~8 条)
#   error 里交付了任何东西的:0 行
# 也就是说 max_steps 是【有产出的结局】(agent 交出了它交得出的),error 是【测量本身崩了】
# (harness 抓到异常,我们对它的能力一无所知)。把 max_steps 一起剔掉有两处代价:
#   ① 丢信号 —— 那 7 行里的交付是真实的能力数据;
#   ② 有方向 —— 系统性利好"更容易烧穿步数"的臂。实测 v2 的 T1/A 因此跳 +0.099,
#      是全表最大的 Δ,而它是排除规则的产物,不是能力变化。
# 任务书 §0 给 B0-3 的【理由】通篇讲的是崩溃那一种(terminated=error、答案长度 0,
# 却在空集探针上拿满分),它的【规则】越界到了 max_steps。这里按理由实现。
# A1 之后 max_steps / repeat 交的是【诚实的部分收口】+ 完整 ledger(见
# loop_driver.PARTIAL_TERMINATIONS),判分从台账取数,不受占位文案影响。
# 要改口径就改这一个常量;无效跑次仍按 terminated 单列上表,让口径本身可复议。
VALID_TERMINATED = frozenset({"text", "max_steps", "repeat", "tree_guard"})


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


_LEDGER_FIELDS = ("category", "start_ts", "end_ts")


def per_video_from_ledger(surfaced_meta: "list | None") -> dict:
    """B0-4:工具台账(show_video 侧信道 `videos[]`)→ 判分用的 per_video。

    形状对齐 `category_accuracy` / `localization_score` 现在吃的那个结构(扁平
    {vid: {category, start_ts, end_ts}}),**不引入新形状** —— 判分口径换了取数来源,
    没换尺子本身。

    合并规则:同一 video_id 被摆多次时按【首个非 None】逐字段合并,不是"第一条整行胜出"。
    理由:agent 完全可能先 show_video 交付视频、下一次调用才带上定位时段(或反过来先带
    时段后补大类)—— 只取第一条会把后补的定位/大类丢掉,而丢掉的那一项在 T2 里值 30% 权重。

    `category` 是产品侧 `show_video(items=...)` 随后新增的字段;它还没合进来时这里
    读到 None,归类分照旧为 0 —— **有就用、没有就退回今天的行为**,不假装有。
    """
    out: dict[str, dict] = {}
    for v in (surfaced_meta or []):
        if not isinstance(v, dict):
            continue
        vid = str(v.get("video_id") or "")
        if not vid:
            continue
        slot = out.setdefault(vid, {k: None for k in _LEDGER_FIELDS})
        for k in _LEDGER_FIELDS:
            if slot[k] is None and v.get(k) is not None:
                slot[k] = v[k]
    return out


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
    探针 0/1 二值,单次误判翻转整轴)。探针只有 2 道,人工复核成本 ≈ 0。

    B0-4 之后【拒判这条路只在台账缺席时才走得到】:有 show_video 台账时 parse_failure 恒
    False,而台账里的 id = agent【主动摆给用户看的】,与"提过一嘴"结构上就分得开 ——
    当初逼出 needs_review 的那个歧义,是 regex 兜底带来的,不是探针本身的。"""
    fabricated = len(pred_ids)
    abstained = any(k in (answer_text or "").lower() for k in _ABSTAIN_MARKS)
    if parse_failure and abstained and fabricated:
        return {"score": None, "needs_review": True,
                "fabricated_ids": fabricated, "abstain_language": abstained}
    return {"score": 1.0 if fabricated == 0 else 0.0, "needs_review": False,
            "fabricated_ids": fabricated, "abstain_language": abstained}


def score_item(item: dict, answer_text: str, vocab: list,
               judge: "float | None" = None, surfaced: "list | None" = None,
               terminated: "str | None" = None,
               surfaced_meta: "list | None" = None) -> dict:
    """单题总入口。返回各分项 + composite;judge=None → judge_pending=True,
    composite 只含确定性部分(权重不重排 —— 缺项就是缺项,不许偷偷归一化成满分)。

    `terminated`:跑次的终止形态(B0-3)。不在 `VALID_TERMINATED` 里 → 直接
    `{"score": None, "composite": None, "invalid": True, "invalid_reason": <terminated>}`,
    **不参与判分,也不判 0**。`None` = 调用方明确表示"本次调用不带跑次上下文"
    (单测 / 手工复算单条答案),按 valid 处理;**跑批判分链路必须传**
    (longhorizon_verdict / longhorizon_report 都已经传)。

    `surfaced_meta`:show_video 侧信道 `videos[]` 的原始行(B0-4),归类与定位的取数来源。
    缺席时退回答案 JSON 解析出的 per_video —— 有就用、没有就退回今天的行为。
    """
    if terminated is not None and terminated not in VALID_TERMINATED:
        return {"score": None, "composite": None, "invalid": True,
                "invalid_reason": terminated, "judge_pending": judge is None}
    parsed = parse_answer(answer_text)
    gold = item["gold"]
    out: dict[str, Any] = {"invalid": False, "judge_pending": judge is None,
                           # 诊断位:答案文本里【碰巧】带了合法 JSON 契约块吗?
                           # 永不参与判分 —— 契约本来就没要求 JSON。
                           "answer_json_contract": not parsed["parse_failure"]}
    # 集合判分口径:优先用【工具台账里真正被摆上台面的视频】。答案文本里的 id 会被
    # VS 的 scrub_ids 按产品规则洗成"第 N 个"(绝不把内部 id 抄给用户)——试跑实测,
    # 只看答案文本会让每一臂的 set_f1 恒为 0,量的是"洗得干不干净"而不是检索能力。
    # 归类/定位同理走 surfaced_meta(B0-4),不再从答案 JSON 抠 per_video。
    ledger_ids = surfaced
    if ledger_ids is None and surfaced_meta is not None:
        ledger_ids = list(dict.fromkeys(str(v.get("video_id") or "")
                                        for v in surfaced_meta if isinstance(v, dict)))
        ledger_ids = [v for v in ledger_ids if v]
    if ledger_ids is not None or surfaced_meta is not None:
        parsed = dict(parsed)
        out["scored_from"] = "tool_ledger"
    if ledger_ids is not None:
        parsed["video_ids"] = list(dict.fromkeys(str(v) for v in ledger_ids))[:_MAX_IDS]
    if surfaced_meta is not None:
        parsed["per_video"] = per_video_from_ledger(surfaced_meta)
    # parse_failure 的语义 = 【判分输入取不到】,不是"答案没写成 JSON"(B0-4)。
    # 台账在 → 判分根本不读答案 JSON,解不解得开与分数无关 → 恒 False。
    # 台账缺席 → 判分只能退回答案文本,解不开就是真的取不到 → 保留老语义。
    # 注意这不是把失败改成成功:agent 一个视频都没摆(surfaced=[])照旧是 set_f1=0,
    # 那是【交付为空】,与【尺子读不到交付】是两件事,现在终于分得开。
    out["parse_failure"] = parsed["parse_failure"] and out.get("scored_from") != "tool_ledger"
    if item["tier"] == "PROBE":
        # 传【重新定义过的】parse_failure,不是答案 JSON 那个原始标志:台账在场时
        # pred_ids 是 agent【主动摆出来的】,与"提过一嘴"结构上分得开 ——
        # 逼出 needs_review 的那个歧义是 regex 兜底带来的,这里已经没有了。
        out.update(probe_score(parsed["video_ids"], answer_text,
                               parse_failure=out["parse_failure"]))
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
