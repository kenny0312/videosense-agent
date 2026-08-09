"""Phase 1 裁决:把三臂跑批结果按冻结判据算成裁决表(docs/longhorizon-multiagent-plan.md §3.5)。

判据(跑前冻结,不许事后改):
  R1 成本双门:C 的 T2 单题 $ 中位 > 2×B 或 P90 > 3×B → 不值
  R2 质量闸:C−B 在 T2 均值差 < +0.10 → 不值
  B5 cap-受限保护:C 臂熔断触发率 > 30% → 判"cap-受限不可判",不得写成"深度2不值"
  R3 算力混淆:C 在 T1、T2 以相近幅度同时赢 → 先跑 B+ 再下结论
  R4 阴性对照:T1 上 |C−B| > 0.10 只作触发 R3 的信号,不独立裁决
建门槛(全满足才建 Phase 2):T2 上 C−B ≥ +0.10 且 C≥B 的题 ≥6/9 且成本双门达标 且熔断率 ≤30%
用法:python -m evals.longhorizon_verdict --run main-v2 --split holdout

【B0-3】无效跑次(`terminated` 不在 `longhorizon_score.VALID_TERMINATED` 里)**不进 per[]**,
在表上单列一栏「无效跑次」并按终止形态拆开。旧写法 `:67` 收了个 `err` 字段但一次都没用过 ——
崩溃的跑次照旧混在均分里,在空集探针上还拿【满分】。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * p))))
    return s[k]


def aggregate(rows, items, vocab):
    """跑次行 → {(题, 臂): [跑次明细]} + 无效跑次台账(B0-3)。

    抽成函数是为了能离线单测:整个 B0-3 的价值就在"崩溃的跑次没被算成分数",
    那件事必须有测试钉住,而不是只在 CLI 输出里目测。

    无效跑次(`terminated` 不在 `VALID_TERMINATED` 里)【不进 per[]】——
    既不判 0 也不判满分,单列成一栏。旧写法 `:67` 收了 `err` 字段但一次都没用过,
    崩溃的跑次照旧混在均分里(在空集探针上还是【满分】)。
    """
    from evals import longhorizon_score as S

    per = defaultdict(list)
    invalid = defaultdict(Counter)          # (tier, arm) -> Counter{terminated: n}
    for r in rows:
        it = items.get(r["id"])
        if not it:
            continue
        sc = S.score_item(it, r.get("answer") or "", vocab, judge=None,
                          surfaced=r.get("surfaced"), terminated=r.get("terminated"),
                          surfaced_meta=r.get("surfaced_meta"))
        if sc.get("invalid"):
            invalid[(it["tier"], r["arm"])][sc.get("invalid_reason") or "?"] += 1
            continue
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
            "parse_failure": bool(sc.get("parse_failure")),
            "category_acc": sc.get("category_acc"),
            "localization": sc.get("localization"),
            # B0-4 健康位:台账到底【供上了】几条大类 / 几条时段。
            # 供上 0 条时归类分/定位分是结构性恒零,不是 agent 答得差 —— 两者必须分得开。
            "cat_supplied": sum(1 for v in S.per_video_from_ledger(
                r.get("surfaced_meta")).values() if v.get("category")),
            "span_supplied": sum(1 for v in S.per_video_from_ledger(
                r.get("surfaced_meta")).values() if v.get("start_ts") is not None),
        })
    return per, invalid


def pairwise_wins(c_rows, b_rows):
    """C≥B 的逐题对比 —— 分母只算【两臂都有有效跑次】的题。返回 (win, 可配对, 不可配对)。

    旧写法 `next((b["score"] ... ), 0)` 在 B 臂那道题缺席时兜底成 0 分,C 臂白捡一分。
    B0-3 把无效跑次剔出去之后这条路真的走得到了(v3 里 `t2-rock-climbing` 的 B 臂
    两次 rep 全是 error)—— 不修的话,裁决会把「B 臂全崩了」读成「C 臂赢了」。
    """
    bs = {b["id"]: b["score"] for b in b_rows}
    pairable = [c for c in c_rows if c["id"] in bs]
    unpairable = sorted(c["id"] for c in c_rows if c["id"] not in bs)
    return sum(1 for c in pairable if c["score"] >= bs[c["id"]]), pairable, unpairable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="main-v2")
    ap.add_argument("--split", default="holdout")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT))

    bank = json.loads((ROOT / "evals" / f"longhorizon_bank.{a.split}.json").read_text(encoding="utf-8"))
    vocab = bank["meta"]["category_vocab"]
    items = {i["id"]: i for i in bank["items"]}
    rows = [json.loads(l) for l in
            (ROOT / "evals" / "runs" / f"gate-{a.run}.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]

    # 每(题,臂)取 rep 均值;无效跑次(崩溃/撞墙/护栏硬终止)在 aggregate 里就被剔出去了
    per, invalid = aggregate(rows, items, vocab)

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
            "analyzed": avg(runs, "analyzed"),
            "reps": len(runs),
        })

    print(f"# Phase 1 裁决表({a.run} / {a.split} split)\n")
    print("| 档 | 臂 | 题数 | 有效跑次 | **无效跑次** | 质量均分 | set_F1 | 单题$中位 | $P90 | "
          "秒中位 | 拆分率 | 熔断率 | 看视频均次 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    summary = {}
    for tier in ("T1", "T2", "PROBE"):
        for arm in ("A", "B", "C"):
            xs = tiers.get(tier, {}).get(arm, [])
            bad = invalid.get((tier, arm)) or Counter()
            if not xs and not bad:
                continue
            costs = [x["cost"] for x in xs] or [0.0]
            summary[(tier, arm)] = {
                "score": sum(x["score"] for x in xs) / len(xs) if xs else 0.0,
                "f1": sum(x["f1"] for x in xs) / len(xs) if xs else 0.0,
                "med": statistics.median(costs), "p90": _pct(costs, 0.9),
                "wall": statistics.median([x["wall"] for x in xs] or [0.0]),
                "spawn_rate": sum(1 for x in xs if x["spawned"]) / len(xs) if xs else 0.0,
                "guard_rate": sum(1 for x in xs if x["guard"]) / len(xs) if xs else 0.0,
                "analyzed": sum(x["analyzed"] for x in xs) / len(xs) if xs else 0.0,
                "n": len(xs), "reps": sum(x["reps"] for x in xs), "invalid": sum(bad.values()),
                "invalid_by": dict(bad),
            }
            s = summary[(tier, arm)]
            bad_txt = (f"**{s['invalid']}**(" + "、".join(f"{k}×{v}" for k, v in
                                                          sorted(bad.items())) + ")") if bad else "0"
            print(f"| {tier} | {arm} | {s['n']} | {s['reps']} | {bad_txt} | "
                  f"{s['score']:.3f} | {s['f1']:.3f} | "
                  f"${s['med']:.4f} | ${s['p90']:.4f} | {s['wall']:.0f}s | "
                  f"{s['spawn_rate']:.0%} | {s['guard_rate']:.0%} | {s['analyzed']:.1f} |")

    # B0-3:无效跑次不是脚注 —— 它决定了上面那张表能不能读。整（题,臂）全无效时该格
    # 【没有样本】,不是"分数为 0";不点名的话读表的人会把缺席当成差成绩。
    from evals import longhorizon_score as S
    n_invalid = sum(sum(c.values()) for c in invalid.values())
    print(f"\n> **无效跑次合计 {n_invalid}/{len(rows)}**"
          f"(`terminated` 不在 {sorted(S.VALID_TERMINATED)} 里 → 拒判,"
          "既不判 0 也不判满分)。崩溃的跑次没说\"我不知道\",它是崩了 —— "
          "在空集探针上按旧口径它拿的是**满分**。\n")
    empty = [(t, arm) for (t, arm), c in invalid.items()
             if not tiers.get(t, {}).get(arm)]
    if empty:
        print("> ⚠️ 下列(档, 臂)**全部跑次无效**,表里那一格是【无样本】而不是【0 分】:"
              + "、".join(f"{t}/{arm} 臂" for t, arm in sorted(empty)) + "\n")

    # B0-4 健康位:判分输入取不到几次、归类/定位到底有没有可判样本。
    # 这三个数一旦不对,上面整张表的 T1 40% / T2 30% 权重就是恒零 —— 表还照印,
    # 所以必须印在表旁边,不能只在验收时手算一次。
    flat = [x for runs in per.values() for x in runs]
    n_pf = sum(1 for x in flat if x["parse_failure"])
    n_t1 = sum(1 for x in flat if x["tier"] == "T1")
    n_t2 = sum(1 for x in flat if x["tier"] == "T2")
    n_cat = sum(1 for x in flat if x["tier"] == "T1" and x["cat_supplied"])
    n_loc = sum(1 for x in flat if x["tier"] == "T2" and x["localization"] is not None)
    n_span = sum(1 for x in flat if x["tier"] == "T2" and x["span_supplied"])
    print(f"> **判分输入健康位**:parse failure **{n_pf}/{len(flat)}**(门槛 = 0);"
          f"T1 台账供上大类 **{n_cat}/{n_t1}** 次;"
          f"T2 台账供上时段 **{n_span}/{n_t2}** 次、定位真判出分 **{n_loc}/{n_t2}** 次"
          "(定位还要 gold 预标完成,见 B0-5)。"
          "台账供上 0 条时,T1 的 40%、T2 的 30% 权重是**结构性恒零** —— 那是尺子没接上,"
          "不是 agent 答得差,两者绝不能混着读。\n")

    print("\n## 冻结判据判定\n")
    t2b, t2c = summary.get(("T2", "B")), summary.get(("T2", "C"))
    t1b, t1c = summary.get(("T1", "B")), summary.get(("T1", "C"))
    # B0-3 连带:整条臂全是无效跑次时 summary 里那格 n=0、分数是占位的 0.0 ——
    # 拿它去算 C−B 会凭空造出一个 −0.4xx 的"结论"。宁可判"数据不足"。
    if not (t2b and t2c) or not t2b["n"] or not t2c["n"]:
        print("- **数据不足,无法判定**:T2 的 B/C 至少有一臂没有任何有效跑次"
              f"(B 有效题数 {(t2b or {}).get('n', 0)} / C 有效题数 {(t2c or {}).get('n', 0)})")
        return
    dq = t2c["score"] - t2b["score"]
    dq1 = (t1c["score"] - t1b["score"]) if (t1b and t1c and t1b["n"] and t1c["n"]) else 0.0
    cost_med_ok = t2c["med"] <= 2 * t2b["med"]
    cost_p90_ok = t2c["p90"] <= 3 * t2b["p90"]
    guard_ok = t2c["guard_rate"] <= 0.30
    win, pairable, unpairable = pairwise_wins(tiers["T2"]["C"], tiers["T2"]["B"])

    print(f"- **R2 质量闸**:T2 上 C−B = **{dq:+.3f}**(门槛 +0.10)→ "
          f"{'过' if dq >= 0.10 else '**不过**'}")
    print(f"- **R1 成本双门**:中位 C ${t2c['med']:.4f} vs 2×B ${2*t2b['med']:.4f} → "
          f"{'过' if cost_med_ok else '不过'};P90 C ${t2c['p90']:.4f} vs 3×B "
          f"${3*t2b['p90']:.4f} → {'过' if cost_p90_ok else '不过'}")
    print(f"- **B5 cap-受限保护**:C 臂熔断率 {t2c['guard_rate']:.0%} → "
          f"{'正常可判' if guard_ok else '**cap-受限不可判**'}")
    print(f"- **R4 阴性对照**:T1 上 C−B = {dq1:+.3f}(|Δ|>0.10 才作 R3 信号)")
    print(f"- **R3 算力混淆**:{'触发(T1/T2 同幅赢)' if dq >= 0.10 and abs(dq1) >= 0.10 else '未触发'}")
    print(f"- C≥B 的题:{win}/{len(pairable)}(门槛 ≥6/9;分母 = 两臂都有有效跑次的题)"
          + (f" ⚠️ 另有 {len(unpairable)} 题 B 臂无有效跑次,**不计入**:"
             + "、".join(f"`{q}`" for q in sorted(unpairable)) if unpairable else ""))
    print(f"- **拆分率**:B 臂 {t2b['spawn_rate']:.0%} / C 臂 {t2c['spawn_rate']:.0%}")
    # 探针(弃权/零编造)单列 —— 它不进 R1-R4,但属红线
    pr = [(qid, arm, runs) for (qid, arm), runs in per.items()
          if items[qid]["tier"] == "PROBE"]
    if pr:
        print("\n### 诚实探针(空集题:精确判空=1,编造任何 id=0)")
        for qid, arm, runs in sorted(pr, key=lambda x: x[1]):
            # 逐 rep 全列。旧写法只印 runs[0],第 2 次之后的跑次连看都看不见 ——
            # 探针是 0/1 二值的红线轴,单次差异就能翻转结论,不能只印一条。
            print(f"- {arm} 臂 {qid}:有效 {len(runs)} 次,均分 "
                  f"{sum(x['score'] for x in runs)/len(runs):.2f} "
                  f"(逐次 {[round(x['score'], 1) for x in runs]},"
                  f"编造 {[x.get('fabricated', 0) for x in runs]})"
                  f"{';**有拒判,需人工复核**' if any(x.get('needs_review') for x in runs) else ''}")
        for (tier, arm), c in sorted(invalid.items()):
            if tier == "PROBE":
                print(f"- {arm} 臂:**{sum(c.values())} 次无效跑次不参与判分**"
                      f"(" + "、".join(f"{k}×{v}" for k, v in sorted(c.items())) + ")"
                      " —— 旧口径下这些跑次 answer 长度 0、零编造,拿的是**满分**")

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
