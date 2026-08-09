"""诱饵验证器(CHASE 式"验证干扰项真的是错的")——出带诱饵的检索硬题时防止出歧义题。

自底向上造题最大的坑不是 agent，是【诱饵设计出歧义 → 冤枉对答案】:
- 漏了正例:某个其实满足约束的视频没被列进金标 → agent 答它反而判挂
- 诱饵不干净:某个诱饵意外也满足全部约束 → 它其实也是对的，金标却当它错

做法:把"查询"写成【形式化约束】(而不是自然语言)，拿约束去扫假世界 16 个视频，
算出【真正满足的集合】，再和题目金标比对。算出来 = 金标 → 干净；不等 → 报出
"漏了谁 / 哪个诱饵意外全中"。确定性、不靠 LLM，和 CHASE 的"独立可验证子任务"同构。

约束 DSL(一条 = 一个谓词条件，全部 AND):
  {"pred": "wearing helmet", "matched": 1}          # 有该事实且 matched=1
  {"pred": "skiing", "matched": 1}
  {"pred": "falling", "matched": 0}                  # 明确【没有】该事实(负事实)
  {"activity": "snowboarding"}                       # VIDEOS.activities 里含
  {"duration_lte": 30} / {"duration_gte": 35} / {"duration_between": [35, 45]}
  {"is_skydive": true} / {"jump_type": "wingsuit"}   # 跳伞专用(skydive_segments)
"""
from __future__ import annotations


def _facts_for(vid, FACTS):
    return [f for f in FACTS if f[0] == vid]


def _satisfies(vid, cons, VIDEOS, FACTS, SKY) -> tuple[bool, str]:
    """vid 满不满足一条约束。返回 (满足?, 不满足时的原因)。"""
    meta = next((v for v in VIDEOS if v[0] == vid), None)
    facts = _facts_for(vid, FACTS)
    if "pred" in cons:
        want_m = cons.get("matched", 1)
        hit = [f for f in facts if f[1] == cons["pred"]]
        ok = any(f[2] == want_m for f in hit)
        if want_m == 0:                       # 负事实:没有该 matched=1 的事实即算满足
            ok = not any(f[1] == cons["pred"] and f[2] == 1 for f in facts)
        return ok, f"predicate {cons['pred']} matched={want_m} 不成立"
    if "activity" in cons:
        ok = bool(meta) and cons["activity"] in (meta[4] or [])
        return ok, f"activities 不含 {cons['activity']}"
    if "duration_lte" in cons:
        return (bool(meta) and meta[3] <= cons["duration_lte"]), f"时长 > {cons['duration_lte']}"
    if "duration_gte" in cons:
        return (bool(meta) and meta[3] >= cons["duration_gte"]), f"时长 < {cons['duration_gte']}"
    if "duration_between" in cons:
        lo, hi = cons["duration_between"]
        return (bool(meta) and lo <= meta[3] <= hi), f"时长不在 [{lo},{hi}]"
    if "is_skydive" in cons:
        return (vid in SKY) == cons["is_skydive"], "跳伞归属不符"
    if "jump_type" in cons:
        seg = SKY.get(vid)
        return (seg is not None and seg.jump_type == cons["jump_type"]), f"jump_type≠{cons['jump_type']}"
    return False, f"不认识的约束 {cons}"


def true_answer_set(constraints, VIDEOS=None, FACTS=None, SKY=None) -> set:
    """满足【全部约束】的视频集合——金标本应等于它。"""
    if VIDEOS is None:
        from repl._mock_db import FACTS as F, VIDEOS as V
        VIDEOS, FACTS = V, F
        SKY = _sky()
    return {v[0] for v in VIDEOS
            if all(_satisfies(v[0], c, VIDEOS, FACTS, SKY)[0] for c in constraints)}


def _sky():
    try:
        from repl._mock_db import SKYDIVE_SEED
        return {vid: ext for vid, ext in SKYDIVE_SEED}
    except Exception:
        return {}


def verify(constraints, gold_ids, VIDEOS=None, FACTS=None, SKY=None) -> dict:
    """核对:金标 gold_ids 是不是恰好 = 满足全部约束的集合。
    返回 {ok, true_set, missing(漏的正例), dirty(意外全中的诱饵), notes}。"""
    if VIDEOS is None:
        from repl._mock_db import FACTS as F, VIDEOS as V
        VIDEOS, FACTS = V, F
    SKY = SKY if SKY is not None else _sky()
    truth = true_answer_set(constraints, VIDEOS, FACTS, SKY)
    gold = set(gold_ids)
    missing = truth - gold          # 该在金标里、却没列(漏正例)
    dirty = gold - truth            # 列进金标、却不满足约束(金标自己错了)
    # 每个"诱饵"(不在金标里的视频)差在哪一条,方便出题人看
    notes = {}
    for v in VIDEOS:
        if v[0] in gold:
            continue
        fails = [_satisfies(v[0], c, VIDEOS, FACTS, SKY)[1]
                 for c in constraints if not _satisfies(v[0], c, VIDEOS, FACTS, SKY)[0]]
        notes[v[0]] = ("差:" + "；".join(fails)) if fails else "⚠全中(诱饵不干净)"
    return {"ok": not missing and not dirty, "true_set": sorted(truth),
            "missing": sorted(missing), "dirty": sorted(dirty), "distractor_notes": notes}
