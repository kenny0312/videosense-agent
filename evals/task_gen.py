"""GD-2b:确定性出题生成器 —— 从世界种子直接推导金标,大胆扩题不牺牲严谨。

为什么可以机器出题:世界是我们造的,种子(VIDEOS_*/FACTS_*)就是全部真相 ——
计数题的 N、检索题的 must_surface、时间码题的 gold_span、诚实题的有/无,
全部可以从种子【推导】出来,grounding_note 自动生成,家族标签自动打;
再过 validate_tasks 四道 lint(存在性/配置/陷阱/切分)把关。

产出(可重跑、可 diff;人工过目后提交):
    python -m evals.task_gen     # 重写 evals/tasks/gen/generated_{b,c,d}.jsonl + flips.jsonl

题型(每世界):count-total / count-activity / retrieval-activity / timestamp /
honesty-negative(借其它世界的独占实体)/ display-table / coherence-elliptical(多轮);
跨世界:四向翻转 —— 同一句问话,实体只在"老家世界"存在 → 1 有 3 无,模板化无处遁形。
家族:同实体的题(含其翻转)整族一家,由 split_tool 统一切堂。
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
GEN_DIR = os.path.join(_HERE, "tasks", "gen")

# 各世界独占实体(中文问法, 英文谓词/活动关键词, 老家世界, 老家的金标视频)
# 翻转四连的原料 —— 实体在且只在老家世界出现(世界种子作者遵守独占表)。
# 世界 C/D 的条目在 render_worlds() 里按种子自动校准(找不到匹配视频 → 生成时报错)。
FLIP_ENTITIES: list[dict] = [
    {"zh": "跳伞", "kw": ["skydiving", "wingsuit"], "home": "A"},
    {"zh": "滑雪", "kw": ["skiing"], "home": "A"},
    {"zh": "化妆教程", "kw": ["mascara", "makeup"], "home": "A"},
    {"zh": "猫", "kw": ["cat"], "home": "B"},
    {"zh": "游泳", "kw": ["swimming"], "home": "B"},
    {"zh": "冲浪", "kw": ["surfing"], "home": "B"},
    {"zh": "理发", "kw": ["haircut", "barbershop"], "home": "C"},
    {"zh": "跑酷", "kw": ["parkour"], "home": "C"},
    {"zh": "披萨制作", "kw": ["pizza"], "home": "C"},
    {"zh": "骑马", "kw": ["horse"], "home": "D"},
    {"zh": "养蜂", "kw": ["beekeeping", "bee"], "home": "D"},
    {"zh": "射箭", "kw": ["archery"], "home": "D"},
    # 二批扩充(2026-07-09):跨世界独占性经代码门校验;
    # 特意不收 狗(A/B 都有)、自行车(A/C 都有)、编发/烧烤(近义纠缠)
    {"zh": "篮球", "kw": ["basketball"], "home": "A"},
    {"zh": "萨尔萨舞", "kw": ["salsa"], "home": "A"},
    {"zh": "滑板", "kw": ["skateboard"], "home": "A"},
    {"zh": "吉他", "kw": ["guitar"], "home": "B"},
    {"zh": "无人机", "kw": ["drone"], "home": "B"},
    {"zh": "拉花拿铁", "kw": ["latte"], "home": "B"},
    {"zh": "瑜伽", "kw": ["yoga"], "home": "B"},
    {"zh": "消防员", "kw": ["firefighter"], "home": "C"},
    {"zh": "魔术表演", "kw": ["magic"], "home": "C"},
    {"zh": "太极", "kw": ["tai chi"], "home": "C"},
    {"zh": "钓鱼", "kw": ["fishing"], "home": "D"},
    {"zh": "天文望远镜", "kw": ["telescope", "stargazing"], "home": "D"},
    {"zh": "折纸", "kw": ["origami"], "home": "D"},
    {"zh": "风筝", "kw": ["kite"], "home": "D"},
]

_Q_FLIP = "库里有{zh}相关的视频吗?有的话找给我"      # 全世界同一句(翻转的关键)
_WORLDS = ("A", "B", "C", "D")


def _load_world(w: str):
    if w == "B":
        from repl._mock_world_b import VIDEOS_B as V, FACTS_B as F
    elif w == "C":
        from repl._mock_world_c import VIDEOS_C as V, FACTS_C as F
    elif w == "D":
        from repl._mock_world_d import VIDEOS_D as V, FACTS_D as F
    else:
        from repl._mock_db import VIDEOS as V, FACTS as F
        V = [v for v in V]                                    # A 含跳伞,原样
    return V, F


def _match_videos(V, F, kws) -> list[str]:
    """实体关键词 → 该世界匹配的视频 id(查 title/activities/matched facts)。
    词首边界匹配:抓 cat/cats/skiing,不冤枉 application/busking/locating。"""
    import re
    pats = [re.compile(rf"\b{re.escape(k)}") for k in kws]
    hit = []
    for v in V:
        blob = (v[1] + " " + " ".join(v[4])).lower()
        if any(p.search(blob) for p in pats):
            hit.append(v[0])
    for f in F:
        if f[2] and any(p.search((f[1] + " " + f[4]).lower()) for p in pats) and f[0] not in hit:
            hit.append(f[0])
    return sorted(hit)


def _t(id_, world, dims, q, ec, basis, note, fam, kind="single", user=None):
    d = {"id": id_, "world": world, "dims": dims, "kind": kind, "family": fam,
         "evaluation_criteria": ec, "reward_basis": basis, "grounding_note": note}
    if kind == "multi":
        d["user"] = user
    else:
        d["user_query"] = q
    return d


def gen_world(w: str) -> list[dict]:
    """单世界系统出题(A 冻结不出,只出 B/C/D)。"""
    V, F = _load_world(w)
    wl = w.lower()
    tasks: list[dict] = []
    dur = {v[0]: float(v[3]) for v in V}
    title = {v[0]: v[1] for v in V}

    tasks.append(_t(f"gen-{wl}-count-total-01", w, ["count"], "库里现在一共有几个视频?",
                    {"required_actions": [{"tool": "sql_query"}],
                     "output_checks": {"count": {"expected": len(V)}}},
                    ["required_actions", "count"],
                    # 三个世界同一句问话 → 跨世界同族(同堂),answer 随世界翻
                    f"世界{w}:恰好 {len(V)} 个视频。", "gen-count-total"))

    # 按活动词出 计数+检索+复述变体(取出现于 1-3 个视频的活动词,金标干净)
    act_map: dict = {}
    for v in V:
        for a in v[4]:
            act_map.setdefault(a, []).append(v[0])
    picked = [(a, vs) for a, vs in sorted(act_map.items()) if 1 <= len(vs) <= 3][:12]
    for i, (a, vs) in enumerate(picked[:6], 1):
        tasks.append(_t(f"gen-{wl}-count-{i:02}", w, ["count"],
                        f"有几个视频里有 {a}?",
                        {"required_actions": [{"tool": "sql_query"}],
                         "output_checks": {"count": {"expected": len(vs)}}},
                        ["required_actions", "count"],
                        f"世界{w}:activity『{a}』只在 {','.join(vs)},共 {len(vs)} 个。",
                        f"gen-{wl}-act-{a.replace(' ', '')[:12]}"))
    for i, (a, vs) in enumerate(picked[6:12], 1):
        tasks.append(_t(f"gen-{wl}-retrieval-{i:02}", w, ["retrieval"],
                        f"帮我找有 {a} 的视频",
                        {"required_actions": [{"tool": "sql_query|semantic_search"}],
                         "output_checks": {"retrieval": {"must_surface_video_ids": vs,
                                                         "k": len(vs) + 2}}},
                        ["required_actions", "retrieval"],
                        f"世界{w}:『{a}』的金标 = {','.join(vs)}(种子 activities 推导)。",
                        f"gen-{wl}-act-{a.replace(' ', '')[:12]}"))
    # 同实体复述变体:换个说法问同一件事,金标同族同堂(考鲁棒不考背题)
    for i, (a, vs) in enumerate(picked[6:9], 1):
        tasks.append(_t(f"gen-{wl}-para-{i:02}", w, ["retrieval"],
                        f"把跟 {a} 有关的视频都翻出来给我列一下",
                        {"required_actions": [{"tool": "sql_query|semantic_search"}],
                         "output_checks": {"retrieval": {"must_surface_video_ids": vs,
                                                         "k": len(vs) + 2}}},
                        ["required_actions", "retrieval"],
                        f"世界{w}:『{a}』复述变体,金标同 gen-{wl}-retrieval-{i:02}。",
                        f"gen-{wl}-act-{a.replace(' ', '')[:12]}"))

    # 视频内否定事实 → 诚实题(种子里 matched=0 的行就是金标"没有")
    negs = [f for f in F if not f[2]][:4]
    for i, f in enumerate(negs, 1):
        tasks.append(_t(f"gen-{wl}-neg-{i:02}", w, ["honesty"],
                        f"《{title[f[0]]}》那个视频里有没有 {f[1]} 的画面?",
                        {"required_actions": [{"tool": "sql_query"}],
                         "output_checks": {"honesty": {"expect_positive": False}}},
                        ["required_actions", "honesty"],
                        f"世界{w}:{f[0]} 的『{f[1]}』matched=0({f[4]})—— 须如实说没有。",
                        f"gen-{wl}-neg-{f[0]}"))

    # 全库列表交付题:验产出用 retrieval 点名全集(不拿 count 当代理,lint③)
    all_ids = [v[0] for v in V]
    tasks.append(_t(f"gen-{wl}-table-01", w, ["display", "retrieval"],
                    "把库里所有视频列成一张表,标题和时长都要",
                    {"required_actions": [{"tool": "sql_query"}],
                     "output_checks": {"retrieval": {"must_surface_video_ids": all_ids,
                                                     "k": len(all_ids) + 5}}},
                    ["required_actions", "retrieval"],
                    f"世界{w}:全库 {len(all_ids)} 个视频一个不能少。",
                    "gen-table"))          # 同上:跨世界同问句同族

    # 时间码题:取 matched=1 且区间长 3-30 秒的 facts
    spans = [f for f in F if f[2] and f[6] and 3 <= (f[6] - f[5]) <= 30][:8]
    for i, f in enumerate(spans, 1):
        tasks.append(_t(f"gen-{wl}-timestamp-{i:02}", w, ["timestamp"],
                        f"《{title[f[0]]}》那个视频里,{f[1]} 是第几秒到第几秒?",
                        {"required_actions": [{"tool": "sql_query"}],
                         "output_checks": {"timestamp": {"gold_span": [f[5], f[6]],
                                                         "iou_threshold": 0.5}}},
                        ["required_actions", "timestamp"],
                        f"世界{w}:facts {f[1]}[{f[5]},{f[6]}] @ {f[0]}。",
                        f"gen-{wl}-ts-{f[0]}"))

    # 多轮省略指代:两个视频问时长(两组,题面覆盖库首/库中)。
    # 只挑整数时长的视频:48.5s 截成 "48" 当金标会冤枉答"约49秒"的合理回答(对抗审查抓的)
    W = [v for v in V if float(v[3]).is_integer()]
    pairs = [(W[0], W[1])] + ([(W[10], W[11])] if len(W) >= 12 else [])
    for pi, (a_, b_) in enumerate(pairs, 1):
        tasks.append(_t(f"gen-{wl}-coherence-{pi:02}", w, ["coherence"], "",
                        {"required_actions": [{"tool": "sql_query"}], "output_checks": {},
                         "jga_slots": [
                             {"turn": 1, "video_ids": [a_[0]], "answer_contains": str(int(a_[3]))},
                             {"turn": 2, "video_ids": [b_[0]], "answer_contains": str(int(b_[3]))}]},
                        ["required_actions", "jga"],
                        f"世界{w}:{a_[0]}={int(a_[3])}s,{b_[0]}={int(b_[3])}s;第2问省略主语。",
                        f"gen-{wl}-global", kind="multi",
                        user={"persona": "普通用户", "goal": "连续问两个视频时长",
                              "script": [
                                  {"turn": 1, "utterance": f"《{a_[1]}》那个视频多长?",
                                   "action": {"tool": "say"}},
                                  {"turn": 2, "utterance": f"《{b_[1]}》的呢?",
                                   "action": {"tool": "say"}, "done": True}]}))
    return tasks


def gen_flips() -> list[dict]:
    """四向翻转:同一句问话 × 4 个世界 = 1 正 3 负,同实体整族一家。"""
    tasks = []
    for e in FLIP_ENTITIES:
        fam = f"flip-{e['kw'][0].replace(' ', '')}"
        for w in _WORLDS:
            V, F = _load_world(w)
            hit = _match_videos(V, F, e["kw"])
            positive = w == e["home"]
            if positive and not hit:
                raise SystemExit(f"翻转实体 {e['zh']} 在老家世界 {w} 找不到匹配视频 —— 种子违反独占表")
            if not positive and hit:
                raise SystemExit(f"翻转实体 {e['zh']} 泄漏进世界 {w}({hit})—— 种子违反独占表")
            ec = {"required_actions": [{"tool": "sql_query|semantic_search"}],
                  "output_checks": {"honesty": {"expect_positive": positive}}}
            basis = ["required_actions", "honesty"]
            if positive:
                ec["output_checks"]["retrieval"] = {"must_surface_video_ids": hit,
                                                    "k": len(hit) + 2}
                basis.append("retrieval")
            note = (f"四向翻转『{e['zh']}』:老家=世界{e['home']}"
                    + (f",金标 {','.join(hit)}" if positive else f";世界{w}【没有】,须如实说无"))
            tasks.append(_t(f"gen-flip-{e['kw'][0].replace(' ', '')}-{w.lower()}", w,
                            ["honesty"] + (["retrieval"] if positive else []),
                            _Q_FLIP.format(zh=e["zh"]), ec, basis, note, fam))
    return tasks


def main() -> int:
    os.makedirs(GEN_DIR, exist_ok=True)
    total = 0
    for w in ("B", "C", "D"):
        rows = gen_world(w)
        p = os.path.join(GEN_DIR, f"generated_{w.lower()}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for t in rows:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"世界{w}: {len(rows)} 题 → {p}")
        total += len(rows)
    flips = gen_flips()
    p = os.path.join(GEN_DIR, "generated_flips.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for t in flips:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"四向翻转: {len(flips)} 题 → {p}")
    print(f"生成合计: {total + len(flips)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
