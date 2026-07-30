"""Phase 1 裁决:把三臂跑批结果按冻结判据算成裁决表(docs/longhorizon-multiagent-plan.md §3.5)。

判据(跑前冻结,不许事后改):
  R1 成本双门:C 的 T2 单题 $ 中位 > 2×B 或 P90 > 3×B → 不值
  R2 质量闸:C−B 在 T2 均值差 < +0.10 → 不值
  B5 cap-受限保护:C 臂熔断触发率 > 30% → 判"cap-受限不可判",不得写成"深度2不值"
  R3 算力混淆:C 在 T1、T2 以相近幅度同时赢 → 先跑 B+ 再下结论
  R4 阴性对照:T1 上 |C−B| > 0.10 只作触发 R3 的信号,不独立裁决
建门槛(全满足才建 Phase 2):T2 上 C−B ≥ +0.10 且 C≥B 的题 ≥6/9 且成本双门达标 且熔断率 ≤30%
用法:python -m evals.longhorizon_verdict --run main-v2 --split holdout
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return s[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="main-v2")
    ap.add_argument("--split", default="holdout")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from evals import longhorizon_score as S

    bank = json.loads((ROOT / "evals" / f"longhorizon_bank.{a.split}.json").read_text(encoding="utf-8"))
    vocab = bank["meta"]["category_vocab"]
    items = {i["id"]: i for i in bank["items"]}
    rows = [json.loads(l) for l in
            (ROOT / "evals" / "runs" / f"gate-{a.run}.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]

    # 每(题,臂)取 rep 均值
    per = defaultdict(list)
    for r in rows:
        it = items.get(r["id"])
        if not it:
            continue
        sc = S.score_item(it, r.get("answer") or "", vocab, judge=None,
                          surfaced=r.get("surfaced"))
        # 探针档走 probe_score,返回的是 score(可能是 None=拒判)而非 composite ——
        # 一律读 composite 会把探针全记 0 分(自查逮出)。
        raw = sc.get("composite") if it["tier"] != "PROBE" else sc.get("score")
        per[(r["id"], r["arm"])].append({
            "score": 0.0 if raw is None else raw, "f1": sc.get("set_f1", 0.0),
            "needs_review": bool(sc.get("needs_review")),
            "fabricated": sc.get("fabricated_ids", 0),
            "cost": r["cost_usd"], "wall": r["wall_s"], "spawned": bool(r.get("spawned")),
            "guard": bool(r.get("guard_trip")), "quota": bool(r.get("quota_hit")),
            "term": r["terminated"], "tier": it["tier"],
            "analyzed": (r.get("tools") or []).count("analyze_video"),
            "err": bool(r.get("error")),
        })

    def avg(xs, k):
        v = [x[k] for x in xs]
        return sum(v) / len(v) if v else 0.0

    tiers = defaultdict(lambda: defaultdict(list))
    for (qid, arm), runs in per.items():
        tier = runs[0]["tier"]
        tiers[tier][arm].append({
            "id": qid, "score": avg(runs, "score"), "f1": avg(runs, "f1"),
            "cost": avg(runs, "cost"), "wall": avg(runs, "wall"),
            "spawned": any(x["spawned"] for x in runs),
            "guard": any(x["guard"] for x in runs),
            "err": any(x["err"] for x in runs),
            "analyzed": avg(runs, "analyzed"),
        })

    print(f"# Phase 1 裁决表({a.run} / {a.split} split)\n")
    print("| 档 | 臂 | 题数 | 质量均分 | set_F1 | 单题$中位 | $P90 | 秒中位 | 拆分率 | 熔断率 | 看视频均次 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    summary = {}
    for tier in ("T1", "T2", "PROBE"):
        for arm in ("A", "B", "C"):
            xs = tiers.get(tier, {}).get(arm, [])
            if not xs:
                continue
            costs = [x["cost"] for x in xs]
            summary[(tier, arm)] = {
                "score": sum(x["score"] for x in xs) / len(xs),
                "f1": sum(x["f1"] for x in xs) / len(xs),
                "med": statistics.median(costs), "p90": _pct(costs, 0.9),
                "wall": statistics.median([x["wall"] for x in xs]),
                "spawn_rate": sum(1 for x in xs if x["spawned"]) / len(xs),
                "guard_rate": sum(1 for x in xs if x["guard"]) / len(xs),
                "analyzed": sum(x["analyzed"] for x in xs) / len(xs),
                "n": len(xs),
            }
            s = summary[(tier, arm)]
            print(f"| {tier} | {arm} | {s['n']} | {s['score']:.3f} | {s['f1']:.3f} | "
                  f"${s['med']:.4f} | ${s['p90']:.4f} | {s['wall']:.0f}s | "
                  f"{s['spawn_rate']:.0%} | {s['guard_rate']:.0%} | {s['analyzed']:.1f} |")

    print("\n## 冻结判据判定\n")
    t2b, t2c = summary.get(("T2", "B")), summary.get(("T2", "C"))
    t1b, t1c = summary.get(("T1", "B")), summary.get(("T1", "C"))
    verdicts = []
    if not (t2b and t2c):
        print("- 数据不足,无法判定")
        return
    dq = t2c["score"] - t2b["score"]
    dq1 = (t1c["score"] - t1b["score"]) if (t1b and t1c) else 0.0
    cost_med_ok = t2c["med"] <= 2 * t2b["med"]
    cost_p90_ok = t2c["p90"] <= 3 * t2b["p90"]
    guard_ok = t2c["guard_rate"] <= 0.30
    win = sum(1 for c in tiers["T2"]["C"]
              if c["score"] >= next((b["score"] for b in tiers["T2"]["B"] if b["id"] == c["id"]), 0))

    print(f"- **R2 质量闸**:T2 上 C−B = **{dq:+.3f}**(门槛 +0.10)→ "
          f"{'过' if dq >= 0.10 else '**不过**'}")
    print(f"- **R1 成本双门**:中位 C ${t2c['med']:.4f} vs 2×B ${2*t2b['med']:.4f} → "
          f"{'过' if cost_med_ok else '不过'};P90 C ${t2c['p90']:.4f} vs 3×B "
          f"${3*t2b['p90']:.4f} → {'过' if cost_p90_ok else '不过'}")
    print(f"- **B5 cap-受限保护**:C 臂熔断率 {t2c['guard_rate']:.0%} → "
          f"{'正常可判' if guard_ok else '**cap-受限不可判**'}")
    print(f"- **R4 阴性对照**:T1 上 C−B = {dq1:+.3f}(|Δ|>0.10 才作 R3 信号)")
    print(f"- **R3 算力混淆**:{'触发(T1/T2 同幅赢)' if dq >= 0.10 and abs(dq1) >= 0.10 else '未触发'}")
    print(f"- C≥B 的题:{win}/{len(tiers['T2']['C'])}(门槛 ≥6/9)")
    print(f"- **拆分率**:B 臂 {t2b['spawn_rate']:.0%} / C 臂 {t2c['spawn_rate']:.0%}")
    # 探针(弃权/零编造)单列 —— 它不进 R1-R4,但属红线
    pr = [(qid, arm, runs) for (qid, arm), runs in per.items()
          if items[qid]["tier"] == "PROBE"]
    if pr:
        print("\n### 诚实探针(空集题:精确判空=1,编造任何 id=0)")
        for qid, arm, runs in sorted(pr, key=lambda x: x[1]):
            r0 = runs[0]
            print(f"- {arm} 臂 {qid}:得分 {r0['score']:.1f}"
                  f"{'(拒判,需人工复核)' if r0.get('needs_review') else ''}"
                  f" 编造 {r0.get('fabricated', 0)} 条")

    build = (dq >= 0.10 and cost_med_ok and cost_p90_ok and guard_ok
             and win >= 6 and t2c["spawn_rate"] > 0)
    print(f"\n## 裁决:**{'建 Phase 2' if build else '不建'}**")
    if not guard_ok:
        print("  → 但按 B5 条款,这是【cap-受限不可判】,不得写成『深度 2 不值』")
    elif t2c["spawn_rate"] == 0:
        print("  → 关键限定:**C 臂拆分率为 0**,深度 2 的机制根本没被触发 ——")
        print("     本裁决的准确表述是『在当前任务分布下,主脑不选择拆分』,")
        print("     而非『拆分了但没用』。措辞必须带 flash orchestrator 条件。")


if __name__ == "__main__":
    main()
