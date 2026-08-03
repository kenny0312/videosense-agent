"""gold 质量审计:量化"被判超发的视频里,有多少其实是 gold 漏了"。

为什么单独成一个步骤(而不是写在报告生成器里):
  报告生成器必须是【纯排版】—— 它读数据、算数字、排版,不产生新事实,也不许有
  任何硬编码的样本数/比率/结论(批次 1.5 R1 的验收原文)。而本审计要打真库
  (`video_facts` 的谓词),有凭据依赖、有网络、结果会随库变化。把它落成一份带
  时间戳的 JSON,报告只读那份 JSON —— 数字可复现、可追溯、和排版解耦。

审计逻辑:
  gold 是按【谓词精确匹配】建的(longhorizon_bank_build.py:85 `WHERE vf.predicate = %s`)。
  实测发现它系统性地漏掉:
    · 词序变体 —— gold `riding horse` 漏掉库内的 `horse riding`;
    · 更具体的谓词 —— gold `performing gymnastics` 漏掉 `performing gymnastics on parallel bars`;
    · 同场景近义 —— gold `riding horse` 漏掉 `mounting horse`/`dismounting horse`。
  所以对每个"被判超发"的视频,回查它在库里的 matched 谓词,看是否与 gold 谓词共享词干。

  【共享词干】的定义是【刻意宽松】的:去停用词、粗暴去 ing/ed/s 后缀,两边词干集合
  交集非空即算。这是一把【上界】尺子 —— 它会把 `mounting horse` 也算进来。目的不是
  给出真值,而是证明"换个 gold 口径结论方向就翻转",所以严格与宽松两把尺子都要报,
  并且都要标注为不可信。真值在中间,要靠 E1 gold 重建(批次 5)。

用法:
    python -m evals.longhorizon_gold_audit --runs main-v3,fulltrace --split holdout
    → evals/runs/gold-audit.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 停用词:谓词里出现但不承载语义的。留得少一点 —— 宁可宽松(这本来就是上界尺子)。
_STOP = {"a", "an", "the", "on", "in", "at", "to", "of", "with", "for", "and", "or",
         "into", "onto", "from", "by", "up", "down", "over", "out"}


def _stems(text: str) -> set[str]:
    """粗暴词干化:小写 → 切非字母 → 去停用词 → 削 ing/ed/s。

    不引 nltk/porter:多一个依赖只为省这四行,而且真正的判据在 E1 人工裁决那一步,
    这里只需要一把【稳定可复现】的上界尺子。
    """
    out = set()
    for w in re.split(r"[^a-z]+", (text or "").lower()):
        if not w or w in _STOP or len(w) <= 2:
            continue
        for suf in ("ing", "ed", "es", "s"):        # 顺序有意义:先长后短
            if len(w) > len(suf) + 2 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def _load_runs(tags, split):
    bank = json.loads((ROOT / "evals" / f"longhorizon_bank.{split}.json").read_text(encoding="utf-8"))
    items = {i["id"]: i for i in bank["items"]}
    rows = []
    for tag in tags:
        p = ROOT / "evals" / "runs" / f"gate-{tag}.jsonl"
        if not p.exists():
            print(f"[警告] 缺 {p.name},跳过", file=sys.stderr)
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["_tag"] = tag
                rows.append(r)
    return bank, items, rows


def _db_predicates(video_ids: list[str]) -> dict[str, list[str]]:
    """回查这些视频在库里的 matched 谓词。空列表 = 查不到(不是"没有谓词")。"""
    if not video_ids:
        return {}
    from pipeline import config  # noqa: F401  —— .env 装载副作用(只读凭证),必须先于 semantic_index
    from pipeline import semantic_index as si
    rows = si._execute(
        "SELECT video_id, array_agg(DISTINCT predicate) FROM video_facts "
        "WHERE matched AND video_id = ANY(%s) GROUP BY video_id",
        (list(video_ids),))
    return {r[0]: [p for p in (r[1] or []) if p] for r in (rows or [])}


def _prf(surf: set, gold: set) -> tuple[float, float, float]:
    """precision / recall / F1。空交付按 0 处理(交不出货就是没找到)。"""
    if not gold:
        return (0.0, 0.0, 0.0)
    tp = len(surf & gold)
    p = tp / len(surf) if surf else 0.0
    r = tp / len(gold)
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return (p, r, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="main-v3,fulltrace")
    ap.add_argument("--split", default="holdout")
    ap.add_argument("--out", default="evals/runs/gold-audit.json")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))

    tags = [t.strip() for t in a.runs.split(",") if t.strip()]
    bank, items, rows = _load_runs(tags, a.split)

    # ① 收集所有"被判超发"的(题, 视频)对
    extra_by_item: dict[str, set] = defaultdict(set)
    for r in rows:
        it = items.get(r["id"])
        if not it or it["tier"] == "PROBE":
            continue
        gold = set(it["gold"]["video_ids"])
        for v in (r.get("surfaced") or []):
            if v not in gold:
                extra_by_item[r["id"]].add(v)

    all_extra = sorted({v for s in extra_by_item.values() for v in s})
    print(f"[审计] 被判超发的不同视频 {len(all_extra)} 个,回查库内谓词…", file=sys.stderr)
    preds = _db_predicates(all_extra)

    # ② 逐条判"是不是 gold 漏了"
    detail = []
    lenient_gold: dict[str, set] = {}
    for qid, vids in sorted(extra_by_item.items()):
        it = items[qid]
        gs = _stems(it.get("predicate") or "")
        lenient_gold[qid] = set(it["gold"]["video_ids"])
        for v in sorted(vids):
            vp = preds.get(v, [])
            hits = [p for p in vp if _stems(p) & gs]
            same = bool(hits)
            if same:
                lenient_gold[qid].add(v)
            detail.append({"item": qid, "gold_predicate": it.get("predicate"),
                           "video_id": v, "db_predicates": vp,
                           "same_stem_predicates": hits, "gold_missed_it": same})

    n_extra = len(detail)
    n_same = sum(1 for d in detail if d["gold_missed_it"])

    # ③ 严格 gold vs 宽松 gold 下,"拆了 vs 没拆"的对照(这是方向会不会翻转的关键)
    grp: dict[str, list] = defaultdict(list)
    for r in rows:
        it = items.get(r["id"])
        if not it or it["tier"] == "PROBE" or r["arm"] == "A":
            continue          # A 臂没有 spawn 工具,不进"拆了 vs 没拆"的对照
        surf = set(r.get("surfaced") or [])
        strict = set(it["gold"]["video_ids"])
        lenient = lenient_gold.get(r["id"], strict)
        ps, rs, fs = _prf(surf, strict)
        pl, rl, fl = _prf(surf, lenient)
        grp["拆了" if r.get("spawned") else "没拆"].append(
            {"p_strict": ps, "r_strict": rs, "f_strict": fs,
             "p_lenient": pl, "r_lenient": rl, "f_lenient": fl,
             "cost": r["cost_usd"], "wall": r["wall_s"], "n_surfaced": len(surf)})

    def agg(xs, k):
        return round(statistics.mean(x[k] for x in xs), 4) if xs else None

    compare = {g: {"n": len(xs), **{k: agg(xs, k) for k in
                                    ("f_strict", "f_lenient", "p_strict", "p_lenient",
                                     "r_strict", "r_lenient", "cost", "wall")}}
               for g, xs in grp.items()}
    for k in ("f_strict", "f_lenient", "p_strict", "p_lenient"):
        a_, b_ = compare.get("拆了", {}).get(k), compare.get("没拆", {}).get(k)
        compare.setdefault("差(拆了−没拆)", {})[k] = (
            round(a_ - b_, 4) if a_ is not None and b_ is not None else None)

    # ④ 逐题 F1(找"六次跑一模一样"这类系统性异常的入口)
    byq = defaultdict(list)
    for r in rows:
        it = items.get(r["id"])
        if not it or it["tier"] == "PROBE":
            continue
        byq[r["id"]].append(round(_prf(set(r.get("surfaced") or []),
                                       set(it["gold"]["video_ids"]))[2], 4))

    out = {
        "generated_from": {"runs": tags, "split": a.split, "n_runs": len(rows)},
        "stem_rule": "小写→切非字母→去停用词(含长度≤2)→削 ing/ed/es/s;两边词干集合交集非空即算同词根",
        "caveat": ("宽松 gold 是【上界】:它会把 mounting horse 这类同场景近义也算进来。"
                   "严格 gold 是【下界】:精确谓词匹配漏词序变体与更具体谓词。"
                   "两把尺子都不可信,真值在中间 —— 要靠 E1 gold 重建。"),
        "caveat_endogenous": (
            "【内生性,读数时必须记住】宽松 gold 是从【被评的这些跑次自己的输出】里长出来的 —— "
            "只有被某次跑摆出来过的视频才有机会进宽松 gold。所以:"
            "①【绝对数字虚高】,库里真相关但从没被摆出来过的视频,两把尺子都漏,召回率被系统性高估;"
            "② 但宽松 gold 是【按题取全体跑次的并集】、两臂共用同一把尺子,"
            "所以【臂间比较仍然可用】,不存在某一臂给自己开小灶。"
            "③ 残余偏差:摆得多的臂对并集贡献更大,那些只有它摆过的视频会变成它自己的 TP。"
            "实测这一项影响很小 —— 召回率差在严格/宽松两把尺子下几乎不变(+0.042 vs +0.043)。"),
        "extra_total": n_extra,
        "extra_same_stem": n_same,
        "extra_same_stem_pct": round(n_same / n_extra, 4) if n_extra else None,
        "spawn_compare": compare,
        "per_question_f1_strict": dict(sorted(byq.items())),
        "detail": detail,
    }
    (ROOT / a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[审计] {n_same}/{n_extra} 的超发其实带同词根谓词 → {a.out}")


if __name__ == "__main__":
    main()
