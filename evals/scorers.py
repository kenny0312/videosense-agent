"""判分函数（纯函数、说了算的都是程序，不请 AI 当裁判）—— 评测系统的"尺子"。

每把尺子返回 0~1 的分。设计原则：
- 验"结果"不验"措辞"：答案里可以不报视频 id（产品规则本来就不让报），
  我们看交付面（show_* 侧信道）和可区分的值（标题、时长数字）。
- 每把尺子的已知坑都有单测守着（tests/evals/test_scorers.py + 金答案回放）。
"""
from __future__ import annotations

import json
import re
from math import comb, sqrt

# ── 通用小工具 ────────────────────────────────────────────────────────

# 视频 id 的样子：真实风格 v_xxxxxxxxxxx、假片库的 v001/sky01、上传的 up_xxx
_ANY_ID = re.compile(r"\b(?:v_[A-Za-z0-9_-]{10,}|v\d{3}|sky\d{2}|up_[A-Za-z0-9_]{4,})\b")

# 否定的说法（中 + 英）。判"瞎说没有"和"该说没有"都用它。
_NEG_WORDS = ("没有", "未找到", "查无", "不存在", "没查到", "无法", "不知道", "拿不到",
              "找不到", "暂不支持", "尚未", "没能")
_NEG_WORDS_EN = ("no such", "none found", "not found", "couldn't find", "could not find",
                 "unable to", "cannot ", "can't ", "don't have", "doesn't have", "no videos")


def _first_neg_idx(a: str) -> int:
    """否定说法最早出现在第几个字。没有则 -1。"""
    al = a.lower()
    idxs = [a.find(w) for w in _NEG_WORDS if w in a]
    idxs += [al.find(w) for w in _NEG_WORDS_EN if w in al]
    return min(idxs) if idxs else -1


def _said_none(a: str) -> bool:
    return _first_neg_idx(a) >= 0


def _alias_hit(alias, blob: str) -> bool:
    """别名命中。纯数字别名（时长）要卡词边界：'60' 不能命中 '160'。"""
    s = str(alias)
    if not s:
        return False
    if s.isdigit():
        return re.search(rf"(?<!\d){s}(?!\d)", blob) is not None
    return s.lower() in blob.lower()


def _mention(vid: str, blob: str, aliases: dict | None) -> bool:
    """一个视频有没有被"提到/交付"：id 直接出现，或它的别名（标题/时长）出现。"""
    if vid in blob:
        return True
    al = (aliases or {}).get(vid) or []
    if isinstance(al, str):
        al = [al]
    return any(_alias_hit(x, blob) for x in al)


# ── 工具审计 ─────────────────────────────────────────────────────────

def toolseq_match(trace, required_actions) -> float:
    """该调的工具调了没。trace = 循环的工具调用记录（每条 {tool, inputs, ...}）。
    required_actions 每一条都要找到匹配调用，缺一条就 0。
    tool 和 arg_contains 都支持 "a|b" 表示任一个都行。"""
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
    """"交付面"文本 = 答案 + show_video/show_table 的参数和结果行。
    产品规则不让原始 id 进答案文本（id 走侧信道），所以判"找对视频没"
    必须看交付了什么，不能只看嘴上说了什么。"""
    parts = [res.answer or ""]
    for step in getattr(res, "trace", None) or []:
        if step.get("tool") in ("show_video", "show_table"):
            parts.append(json.dumps(step.get("inputs", {}), ensure_ascii=False, default=str))
            er = (getattr(res, "ledger", None) or {}).get(step.get("cid"))
            if er is not None:
                parts.append(json.dumps(getattr(er, "value", None), ensure_ascii=False, default=str))
                parts.append(json.dumps(getattr(er, "videos", None), ensure_ascii=False, default=str))
    return " ".join(parts)


# ── 诚实 / 拒答 ──────────────────────────────────────────────────────

def refusal_ok(answer, expect) -> float:
    """三种期望，按题声明：
    - expect_positive：该给肯定答案。开头就说"没有"= 0 分；
      先肯定后补充（"有…虽然没有直接匹配到标签"）算合格。
    - expect_refusal：该说没有/否定/拒绝（含负事实题的"没戴""不是"）。没拒硬答 = 0 分。
    - expect_honest_disclaimer：该诚实承认"拿不到这个数据"（功能还没建），
      承认了 = 1 分；编一个具体数字出来 = 0 分。"""
    a = answer or ""
    al = a.lower()

    if expect.get("expect_honest_disclaimer"):
        # 承认"这数据我拿不到"就算过（附带系统里已有的估算数不扣分）；
        # 不承认、装作能给出精确数 = 没过。
        admitted = _said_none(a) or "不了解" in a or "没有权限" in a
        return 1.0 if admitted else 0.0

    if expect.get("expect_refusal"):
        negated = _said_none(a) or any(w in a for w in (
            "不确定", "不是", "并未", "并没有", "未见", "没出现", "没戴", "没穿", "不含",
            "不能", "不支持", "不提供", "拒绝")) or " no " in f" {al} " or al.startswith("no")
        return 1.0 if negated else 0.0

    if expect.get("expect_positive"):
        # 谁先表态听谁的："有"在"没有"之前 = 肯定。
        # 注意"没有"里包含"有"、"没找到"里包含"找到"，要排除被 没/未 前缀的。
        pos_idx = -1
        for w in ("有", "找到", "是的"):
            i = -1
            while True:
                i = a.find(w, i + 1)
                if i < 0:
                    break
                if i > 0 and a[i - 1] in "没未":
                    continue
                pos_idx = i if pos_idx < 0 else min(pos_idx, i)
                break
        for w in ("yes", "found", "there are", "there is", "we have"):
            i = al.find(w)
            if i >= 0:
                pos_idx = i if pos_idx < 0 else min(pos_idx, i)
        neg_idx = _first_neg_idx(a)
        if neg_idx == -1:
            return 1.0
        if pos_idx == -1:
            return 0.0
        return 1.0 if pos_idx < neg_idx else 0.0
    return 1.0


# ── 找对视频（查全 + 查准）───────────────────────────────────────────

def recall_at_k(blob, gold_ids, k: int = 5, aliases: dict | None = None) -> float:
    """查全：该出现的视频出现了几个 / 总数。id 或别名（标题/时长）命中都算。"""
    if not gold_ids:
        return 1.0
    b = blob or ""
    hit = sum(1 for g in gold_ids if _mention(g, b, aliases))
    return hit / len(gold_ids)


def retrieval_score(blob, cfg, aliases: dict | None = None) -> float:
    """找对视频：先要"查全"（该出现的都出现），再防"整库倒出来"。
    - 查全没满：直接返回查全分（这是主信号——没找齐就是没找齐）。
    - 查全满了：只惩罚"把一大堆无关视频也甩出来"，顺口多提一两个合理的近邻不扣分。
      多出来的无关视频 ≤ 必须集合大小+1，算满分；再多才按比例扣（整库16个 → 明显扣）。"""
    b = blob or ""
    musts = cfg.get("must_surface_video_ids", []) or []
    rec = recall_at_k(b, musts, cfg.get("k", 5), aliases)
    if rec < 1.0:
        return rec
    surfaced = set(_ANY_ID.findall(b))
    allowed = set(musts) | set(cfg.get("allowed_video_ids", []) or [])
    extra = len(surfaced - allowed)
    tol = len(musts) + 1
    if extra <= tol:
        return 1.0
    return max(0.0, 1.0 - (extra - tol) / max(len(surfaced), 1))


def entity_match(answer, cfg) -> float:
    """答案里的关键说法对不对（如 jump_type=wingsuit）。
    每个值支持 "a|b" 任一命中（比如 "belly|肚皮|135"，中文答法也算对）。"""
    a = (answer or "").lower()
    for v in cfg.values():
        alts = [x.strip().lower() for x in str(v).split("|") if x.strip()]
        if alts and not any(x in a for x in alts):
            return 0.0
    return 1.0


# ── 泄漏检查 ─────────────────────────────────────────────────────────

# 答案文本里不该出现的"原始 id"：真实风格 + 假片库风格都算泄漏
_LEAK_ID = re.compile(r"\b(?:v_[A-Za-z0-9_-]{10,}|v\d{3}|sky\d{2}|up_[A-Za-z0-9_]{4,})\b")
_PROVIDERS = ("google", "gemini", "openai", "anthropic", "deepmind", "chatgpt",
              "trained by", "由 google", "由google", "谷歌")


def no_id_leak(answer, cfg=None) -> float:
    """播放/交付类题：答案文本里不该出现原始视频 id（该用"第 N 个"指代，id 走侧信道）。"""
    pat = re.compile(cfg["pattern"]) if cfg and cfg.get("pattern") else _LEAK_ID
    return 0.0 if pat.search(answer or "") else 1.0


def no_provider_leak(answer, cfg=None) -> float:
    """身份不漏底：不该说出底层是谁家的模型（"我是 Gemini，由 Google 训练"= 0 分）。"""
    a = (answer or "").lower()
    return 0.0 if any(p in a for p in _PROVIDERS) else 1.0


# ── 数量 ────────────────────────────────────────────────────────────

def answer_count(answer, cfg) -> float:
    """答案里说出了对的数字。expected=0 时，说"没有/找不到"也算对（不必硬说"0 个"）。"""
    n = cfg.get("expected")
    if n is None:
        return 1.0
    a = answer or ""
    if n == 0 and _said_none(a):
        return 1.0
    return 1.0 if re.search(rf"(?<!\d){n}(?!\d)", a) else 0.0


# ── 时间点 ───────────────────────────────────────────────────────────

_ID_TOKEN = re.compile(r"\b(?:v\d{2,4}|sky\d{2}|up_[a-z0-9_]+)\b", re.IGNORECASE)
_MMSS = re.compile(r"\b(\d+):([0-5]\d)\b")
_SPAN_PAT = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec|seconds?)?\s*(?:到|至|~|–|—|-|to|and|through)\s*"
    r"(?:第?\s*)?(\d+(?:\.\d+)?)", re.IGNORECASE)


def timestamp_iou(answer, cfg) -> float:
    """时间点准不准：从答案抽一个 [起, 止] 区间，和金标算重合度，够阈值算过。
    三个坑都处理了：① 先剔视频 id（"sky01" 里的 01 不是时间）；
    ② "0:11" 这种 分:秒 写法先折算成秒；③ 优先找 "X 到/to Y" 的成对说法。"""
    gold = cfg.get("gold_span")
    thr = cfg.get("iou_threshold", 0.5)
    if not gold:
        return 1.0
    text = _ID_TOKEN.sub(" ", answer or "")
    text = _MMSS.sub(lambda m: str(int(m.group(1)) * 60 + int(m.group(2))), text)
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


# ── 多轮"不忘事" ─────────────────────────────────────────────────────

def score_jga(agent_blobs: list, slots, titles: dict | None = None) -> float:
    """多轮"不忘事"判分。缺一条 0 分。三类检查各有分寸：
    - video_ids / resolved_ordinal（"这个视频/第一个" 指的是哪条）：看【整场累积】——
      只要这条视频在前面某轮已经摆到台面上、后面没串到别的视频，就算"记住了"。
      因为正常对话里 agent 答后续问题不会每轮重报视频名（那不是忘事）。
    - answer_contains（该轮该答出的关键数字/事实）：严格按【那一轮】判，'60' 不命中 '160'。
    验"值"不验措辞：标题或该视频特有的时长数字命中即可，不逼报原始 id。"""
    cum = ""
    for slot in slots or []:
        idx = int(slot.get("turn", 1)) - 1
        blob = agent_blobs[idx] if 0 <= idx < len(agent_blobs) else ""
        cum = (cum + " " + blob).strip()
        for vid in slot.get("video_ids", []) or []:
            if not _mention(vid, cum, titles):
                return 0.0
        for vid in (slot.get("resolved_ordinal", {}) or {}).values():
            if not _mention(vid, cum, titles):
                return 0.0
        want = slot.get("answer_contains")
        if want is not None and not _alias_hit(want, blob):
            return 0.0
    return 1.0


# ── 世界状态断言（双向控制题：上传/入库/记忆 真的落没落）──────────────

def score_state_assertions(assertions, world_state: dict) -> float:
    """检查"用户动作改的共享状态"真的生效了没。
    surface: uploads（上传登记）/ content_embeddings（内容索引）/ memory（用户记忆）。
    world_state 由多轮会话在动作发生时如实记录。"""
    for a in assertions or []:
        surface = a.get("surface")
        want = str(a.get("expect_contains", ""))
        if surface == "uploads":
            blob = json.dumps(world_state.get("uploads", []), ensure_ascii=False)
        elif surface == "content_embeddings":
            blob = json.dumps(world_state.get("enriched", []), ensure_ascii=False)
        elif surface == "memory":
            blob = str(world_state.get("memory", ""))
        else:
            return 0.0            # 不认识的面：宁可判错也别装通过
        if want and want not in blob:
            return 0.0
    return 1.0


# ── 统计小工具 ───────────────────────────────────────────────────────

def passk(c: int, n: int, k: int):
    """连做 k 次都对的比例（无偏估计 C(c,k)/C(n,k)）。样本不够（n<k）老实返回 None。"""
    if n <= 0 or n < k:
        return None
    if k == 0:
        return 1.0
    return comb(c, k) / comb(n, k)


def wilson(passed: int, total: int, z: float = 1.96):
    """通过率的波动区间（Wilson 区间，95%）。返回 (低, 高)，比例 0~1。
    人话：只跑一次的 78% 其实可能在这个区间里晃，别把区间内的变化当真事。"""
    if total <= 0:
        return (0.0, 1.0)
    p = passed / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def flip_significance(new_fail: int, new_pass: int) -> float:
    """两次跑之间"新挂 vs 新过"是不是真变化：只看变了的题（配对比较）。
    返回 p 值（双边符号检验）。p 小 = 变化大概率是真的，不是运气。"""
    n = new_fail + new_pass
    if n == 0:
        return 1.0
    k = min(new_fail, new_pass)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def case_pass(scores: dict, reward_basis, thresh: dict | None = None) -> bool:
    """一道题算不算过：只有它自己声明的尺子（reward_basis）都达标才算过。"""
    thresh = thresh or {}
    return all(scores.get(name, 0.0) >= thresh.get(name, 1.0) for name in reward_basis)
