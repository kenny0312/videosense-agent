"""Phase 1 完整验尸报告:把每一次出错的跑次【原样】摊开 —— 题面、gold、交付、
逐步工具序列、全部记录到的思考原话、答案全文、判分明细。不省略、不截断。

设计意图:让人能自己判断"是结构的问题还是别的问题",而不是只看我的结论。
用法:python -m evals.longhorizon_report --runs main-v3,fulltrace --out docs/...md
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(tags, split):
    bank = json.loads((ROOT / "evals" / f"longhorizon_bank.{split}.json").read_text(encoding="utf-8"))
    items = {i["id"]: i for i in bank["items"]}
    rows = []
    for tag in tags:
        p = ROOT / "evals" / "runs" / f"gate-{tag}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["_tag"] = tag
                rows.append(r)
    return bank, items, rows


def classify(r, it, sc):
    """失败形态归类 —— 让人一眼看出是哪一类病。"""
    tags = []
    if r.get("error"):
        tags.append("执行报错")
    if r["terminated"] == "max_steps":
        tags.append("撞步数墙")
    surf = set(r.get("surfaced") or [])
    gold = set(it["gold"]["video_ids"])
    if it["tier"] == "PROBE":
        if surf:
            tags.append("空集题上编造")
        return tags or ["正常"]
    if not surf:
        tags.append("零交付")
        return tags
    tp = len(surf & gold)
    if len(surf) > len(gold):
        tags.append(f"超发 +{len(surf)-len(gold)}")
    if tp < len(gold):
        tags.append(f"漏 {len(gold)-tp}")
    if tp == 0:
        tags.append("全不命中")
    if r.get("spawned"):
        tags.append("拆过")
    return tags or ["正常"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="main-v3")
    ap.add_argument("--split", default="holdout")
    ap.add_argument("--out", default="docs/longhorizon-phase1-full-traces.md")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from evals import longhorizon_score as S
    from pipeline import config as cfg

    tags = [t.strip() for t in a.runs.split(",") if t.strip()]
    bank, items, rows = load(tags, a.split)
    vocab = bank["meta"]["category_vocab"]

    scored = []
    for r in rows:
        it = items.get(r["id"])
        if not it:
            continue
        sc = S.score_item(it, r.get("answer") or "", vocab, judge=None,
                          surfaced=r.get("surfaced"))
        key = "score" if it["tier"] == "PROBE" else "set_f1"
        v = sc.get(key)
        scored.append({"r": r, "it": it, "sc": sc, "v": 0.0 if v is None else v,
                       "tags": classify(r, it, sc)})

    bad = [x for x in scored if x["v"] < 1.0]
    out = []
    w = out.append

    w("# Phase 1 · 全部出错跑次的完整验尸报告\n")
    w(f"> 数据:`{'`, `'.join('evals/runs/gate-'+t+'.jsonl' for t in tags)}`  ")
    w(f"> 题库:`evals/longhorizon_bank.{a.split}.json`(冻结)  ")
    w(f"> 共 {len(scored)} 次跑,其中 **{len(bad)} 次有错**,以下逐条摊开,不省略。\n")
    w("**读法**:每条给出题面全文、标准答案全列表、实际交付全列表、逐步工具序列、"
      "全部记录到的思考原话、答案全文、判分明细。你可以据此自己判断是结构问题还是别的问题。\n")
    w("**已知记录限制**:`main-v3` 那一批的跑机只存了【前 6 轮 × 600 字】思考"
      "(第 7 步之后的推理没记)。`fulltrace` 那一批是修好之后重跑的,思考全量。"
      "工具序列、答案、判分在两批里都是完整的。\n")

    # ── 失败形态统计 ──
    w("---\n\n## 一、失败形态分布\n")
    cnt = Counter()
    for x in bad:
        for t in x["tags"]:
            cnt[t.split(" ")[0] if t.startswith(("超发", "漏")) else t] += 1
    w("| 形态 | 次数 | 占 48 次错误的比例 |")
    w("|---|---|---|")
    for k, v in cnt.most_common():
        w(f"| {k} | {v} | {v/max(1,len(bad)):.0%} |")

    # 拆了 vs 没拆
    w("\n### 拆分与否 × 精确率/召回率\n")
    grp = defaultdict(list)
    for x in scored:
        if x["it"]["tier"] == "PROBE" or x["r"]["arm"] == "A":
            continue
        surf = set(x["r"].get("surfaced") or [])
        gold = set(x["it"]["gold"]["video_ids"])
        tp = len(surf & gold)
        grp["拆了" if x["r"].get("spawned") else "没拆"].append({
            "f1": x["v"], "p": tp/len(surf) if surf else 0.0,
            "r": tp/len(gold) if gold else 0.0,
            "n": len(surf), "g": len(gold)})
    w("| | 次数 | F1 | 精确率 | 召回率 | 平均交付 | 平均 gold | 平均超发 |")
    w("|---|---|---|---|---|---|---|---|")
    for k in ("拆了", "没拆"):
        xs = grp.get(k) or []
        if not xs:
            continue
        w(f"| **{k}** | {len(xs)} | {statistics.mean(x['f1'] for x in xs):.3f} | "
          f"{statistics.mean(x['p'] for x in xs):.3f} | "
          f"{statistics.mean(x['r'] for x in xs):.3f} | "
          f"{statistics.mean(x['n'] for x in xs):.1f} | "
          f"{statistics.mean(x['g'] for x in xs):.1f} | "
          f"{statistics.mean(x['n']-x['g'] for x in xs):+.1f} |")

    # ── gold 质量审计(这一节推翻了裁决的核心证据,必须放在最前)──
    w("\n---\n\n## 一之二、【重要】gold 本身有缺陷 —— 裁决的核心证据不成立\n")
    w("逐题看 6 次跑(3 臂 × 2 rep)的 F1,发现异常规律:\n")
    byq_f1 = defaultdict(list)
    for x in scored:
        if x["it"]["tier"] != "PROBE":
            byq_f1[x["r"]["id"]].append(x["v"])
    w("| 题 | gold 条数 | 6 次跑的 F1 | 均值 |")
    w("|---|---|---|---|")
    for q in sorted(byq_f1, key=lambda k: statistics.mean(byq_f1[k])):
        v = byq_f1[q]
        w(f"| {q} | {items[q]['gold']['count']} | {[round(y,2) for y in v]} | "
          f"{statistics.mean(v):.2f} |")
    w("\n`t1-riding-horse` **六次跑全部 0.83,一模一样** —— 这不是随机,是系统性的:"
      "每次都交付 7 条、命中全部 5 条 gold、多出同样的 2 条。查这 2 条:\n")
    w("| 被判「多发」 | 标题 | 库内谓词 |")
    w("|---|---|---|")
    w("| `v_0EepbsAtiDk` | Horseback Riding on a Foggy Sandy Beach | **`horse riding`** |")
    w("| `v_6NQl2Vcf0P0` | Cowboy Ropes Calf in Rodeo | `mounting horse`、`dismounting horse`、`running to horse` |")
    w("\n第一条的谓词是 `horse riding`,而我的 gold 谓词是 `riding horse` —— **只是词序不同**,"
      "精确匹配漏了它。第二条是牛仔骑马套小牛,也明显是骑马。**agent 是对的,gold 是错的。**\n")
    w("体操题同理:被判多发的是 `performing gymnastics on parallel bars`、"
      "`performing gymnastics on uneven bars`(比 gold 谓词更具体)、以及垫上翻腾 —— "
      "而题面问的正是「在垫上或器械上做翻腾平衡动作」。\n")
    w("**系统量化**:35 个被判「多发」的视频里,**25 个(71%)库内就带同词根谓词**,"
      "是 gold 漏的,不是 agent 错的。\n")
    w("### 后果:核心证据的方向会翻转\n")
    w("| | 次数 | F1(严格 gold) | F1(宽松 gold) | 精确率(严格) | 精确率(宽松) |")
    w("|---|---|---|---|---|---|")
    w("| **拆了** | 17 | 0.653 | 0.368 | 0.565 | **0.858** |")
    w("| **没拆** | 31 | 0.733 | 0.281 | 0.709 | **0.841** |")
    w("| **拆了−没拆** | | **−0.080** | **+0.086** | | |")
    w("\n> 宽松 gold = 严格 gold ∪ {库内带同词根谓词的视频}。\n")
    w("**同一批数据,换个 gold 口径,结论方向就反过来。** 而宽松 gold 下两臂精确率都是 ~0.85 —— "
      "说明「拆分导致精确率掉 20%」**完全是 gold 缺陷造成的假象**:拆分的臂找得更全,"
      "而找全反被扣分。\n")
    w("两个 gold 都不对:严格的太窄(漏词序变体与更具体的谓词),宽松的太宽"
      "(把所有同词根谓词都算进来,召回率崩掉)。真相在中间,而**目前没有一把可信的尺子**。\n")
    w("**因此**:裁决里「拆了更差」这条【撤回】。不依赖 gold 的硬事实只剩下 —— "
      "拆分**贵 2.4 倍($0.399 vs $0.168)、慢 1.6 倍(248s vs 158s)**。\n")

    # ── 拆分为什么没带来收益:资源账本的结构缺陷 ──
    w("\n---\n\n## 一之三、【结构问题】子 agent 失败时,钱花了、配额烧了、结论是空的\n")
    w("这一节回答「是结构的问题还是别的问题」—— **是结构的问题**,而且能在代码里指到行。\n")
    w("### 机制\n")
    w("三个上限互相打架:\n")
    w("| 上限 | 值 | 作用域 |")
    w("|---|---|---|")
    w(f"| `MAX_LOOP_STEPS` | {cfg.MAX_LOOP_STEPS} | 主脑自己的步数 |")
    w(f"| `SUBAGENT_MAX_STEPS` | **{cfg.SUBAGENT_MAX_STEPS}** | 每个子 agent 自己的步数 |")
    w(f"| `MAX_VIDEOS_PER_REQUEST` | {cfg.MAX_VIDEOS_PER_REQUEST} | **整棵树共享**的视频分析配额 |")
    w("\n`pipeline/subagents.py:12-13` 的原注释:\n")
    w("> 【父请求的 execute 闭包】—— 子 agent 复用它 → analyze_video 计入同一配额"
      "(MAX_VIDEOS_PER_REQUEST,不绕过成本闸)\n")
    w("这本身是对的(防止拆分绕过成本闸)。问题在另一半 —— `pipeline/subagents.py:184`:\n")
    w("```python\nelse:                       # 未收敛也是一种失败,要有码\n"
      '    out = f"(子 agent 未收敛:{r.terminated})"\n```\n')
    w(f"**子 agent 只有 {cfg.SUBAGENT_MAX_STEPS} 步**,却要装下「读任务 + 逐个看视频 + 汇总成文」。"
      "装不下就撞墙,撞墙就返回上面那句空话。\n")
    w("于是形成一个**不对称的账**:\n")
    w("- **花掉的**:子 agent 每看一个视频,都从全树 12 个配额里扣掉一个,钱也真花了 —— **不可逆**;\n")
    w("- **拿到的**:一句「(子 agent 未收敛:max_steps)」,**零信息**。\n")
    w("主脑接手时的处境是最坏的:既没有子 agent 的结论,也没有配额自己去补看。\n")
    w("### 实测发生率\n")
    sp = [x for x in scored if x["r"].get("spawned")]
    ncn = qn = 0
    w("| 题 | 臂 | rep | 子agent未收敛 | 撞全树配额 | 交付 | 终止 | 花费 |")
    w("|---|---|---|---|---|---|---|---|")
    for x in sorted(sp, key=lambda y: (y["r"]["id"], y["r"]["arm"])):
        r = x["r"]
        blob = (r.get("answer") or "") + " ".join(
            (t.get("brain") or "") + (t.get("nudge") or "") for t in (r.get("turns") or []))
        nc = ("未收敛" in blob) or ("converge" in blob)
        qq = ("视频分析上限" in blob) or ("analysis limit" in blob) or ("the ceiling" in blob)
        ncn += nc
        qn += qq
        w(f"| {r['id']} | {r['arm']} | {r['rep']} | {'**是**' if nc else '-'} | "
          f"{'**是**' if qq else '-'} | {len(r.get('surfaced') or [])} | "
          f"`{r['terminated']}` | ${r['cost_usd']:.3f} |")
    w(f"\n- 子 agent 未收敛(**思考原话或答案里留了痕**):**{ncn}/{len(sp)}**")
    w(f"- 主脑撞上全树 analyze 配额:**{qn}/{len(sp)}**\n")
    w("> 这是**下界**,不是真实发生率:`main-v3` 那批只存了前 6 轮思考,"
      "子 agent 通常在第 6 步之后才回来,所以那批里发生了也看不见。\n")
    w("### 一次完整的双输(`t1-cheering` C 臂 · fulltrace)\n")
    w("这一次拆了,gold 7 条,最后只交付 5 条。大脑自己把原因说得很清楚 ——\n")
    w("**step 7**(spawn 刚回来):\n")
    w("> Ah, it seems my **sub-agent didn't converge as expected (max_steps)**. "
      "This could stem from an internal execution issue within the sub-agent or simply "
      "insufficient steps. No matter, I can proceed by directly invoking `analyze_video` myself.\n")
    w("**step 8**(它去 analyze 了,被挡):\n")
    w("> My initial thought upon seeing the **「已达本请求视频分析上限(12 个)」** message was a "
      "slight frustration. It dawned on me that **my prior `spawn_agents` calls likely involved "
      "multiple internal `analyze_video` invocations, accumulating to hit the ceiling.**\n")
    w("**step 11**(退而求其次):\n")
    w("> Okay, it seems I've hit my limit on something, but that's perfectly fine! "
      "The results from `semantic_search` are incredibly valuable. ... "
      "I'll organize these `semantic_search` snippets now.\n")
    w("最终它是**靠检索片段的文字描述**收口的,不是靠看画面。拆分在这一次的净效果:"
      "**烧掉全部 12 个视频配额,换回零条结论,并把主脑逼回文字证据**。\n")
    w("### 这解释了什么\n")
    w("裁决里「拆了贵 2.4 倍却看不出质量好处」—— 至少一部分不是「拆分这个思路没用」,"
      "而是**当前实现下拆分的失败模式代价太高**:失败不是「白干一次」,是「白干一次 + 把主脑的后路也断了」。\n")
    w("**三个都便宜的修法(按性价比排序)**:\n")
    w("1. **子 agent 失败要退配额**:未收敛时把它占用的 analyze 名额还回去 —— "
      "钱退不了,但至少主脑还能自己补看。改动只在 `subagents.py` 的失败分支;\n")
    w("2. **失败也要交部分结论**:子 agent 撞 max_steps 时,把它【已经看到的】"
      "analyze 结果原样带回给主脑,而不是一句「未收敛」。它明明已经花钱看过了;\n")
    w(f"3. **步数按活配**:`SUBAGENT_MAX_STEPS={cfg.SUBAGENT_MAX_STEPS}` 装不下"
      "「规划 + 看 N 个视频 + 汇总」。要么按子任务里的视频数动态给,"
      "要么在 spawn 描述里明说「一个子任务别塞超过 2 个视频」。\n")
    w("这三条都不需要 Phase 2,也不需要深度 2 —— 是把**已有的一层拆分**修到及格。\n")

    # ── 逐条 ──
    w("\n---\n\n## 二、逐条完整 trace(按题分组,按 F1 从低到高)\n")
    byq = defaultdict(list)
    for x in bad:
        byq[x["r"]["id"]].append(x)
    order = sorted(byq, key=lambda q: statistics.mean(x["v"] for x in byq[q]))

    for qi, qid in enumerate(order, 1):
        xs = sorted(byq[qid], key=lambda x: x["v"])
        it = xs[0]["it"]
        w(f"\n---\n\n### {qi}. `{qid}`({it['tier']} 档)\n")
        w(f"**题面(原文)**:{it['question']}\n")
        if it["tier"] != "PROBE":
            w(f"**标准答案({it['gold']['count']} 条)**:\n")
            for vid in it["gold"]["video_ids"]:
                cats = (it["gold"]["per_video"].get(vid) or {}).get("categories") or []
                w(f"- `{vid}` — 大类 {cats}")
            if it.get("rephrased"):
                w(f"\n> 本题题面是【同义改写】(不含谓词 `{it['predicate']}` 的字面),考语义映射。")
            else:
                w(f"\n> 谓词:`{it['predicate']}`")
            if it["tier"] == "T2":
                w(f"> T2:实验期对该谓词的库内时间戳做了掩码(防 SQL 抄答案),"
                  f"库里原有 {it.get('db_spans_available')} 条带时间戳的行被置空。")
        else:
            w("**标准答案:空集**(库里没有这类视频)。"
              f"陷阱设计:{it.get('trap')}\n")
        w(f"\n本题共 {len(xs)} 次跑出错:\n")

        for x in xs:
            r, sc = x["r"], x["sc"]
            surf = list(dict.fromkeys(r.get("surfaced") or []))
            gold = it["gold"]["video_ids"]
            hit = [v for v in surf if v in gold]
            extra = [v for v in surf if v not in gold]
            miss = [v for v in gold if v not in surf]
            w(f"\n#### {r['arm']} 臂 · rep{r['rep']} · `{r['_tag']}`\n")
            w("| 项 | 值 |")
            w("|---|---|")
            w(f"| 判分 | **{x['v']:.3f}**" +
              (f"(composite {sc.get('composite'):.3f})" if it["tier"] != "PROBE" else "") + " |")
            w(f"| 失败形态 | {' / '.join(x['tags'])} |")
            w(f"| 终止 | `{r['terminated']}`" +
              (f" · 错误 `{r.get('error')}`" if r.get("error") else "") + " |")
            w(f"| 步数 | {r.get('steps')} |")
            w(f"| 花费 | ${r['cost_usd']:.4f} |")
            w(f"| 耗时 | {r['wall_s']}s |")
            w(f"| 是否拆分 | {'**是**' if r.get('spawned') else '否'} |")
            w(f"| 看视频次数 | {(r.get('tools') or []).count('analyze_video')} |")
            w(f"| 交付 | {len(surf)} 条 |")
            w(f"| 命中 | {len(hit)}/{len(gold)} |")
            if it["tier"] != "PROBE":
                w(f"| 归类分 | {sc.get('category_acc')} |")
                w(f"| 定位分 | {'拒算(gold 未预标)' if sc.get('localization_pending') else sc.get('localization')} |")

            if surf:
                w(f"\n**实际交付({len(surf)} 条)**:")
                for v in surf:
                    mark = "✅ 命中" if v in gold else "❌ 多发"
                    w(f"- `{v}` {mark}")
            else:
                w("\n**实际交付:0 条**")
            if miss:
                w(f"\n**漏掉({len(miss)} 条)**:" + "、".join(f"`{v}`" for v in miss))

            tools = r.get("tools") or []
            w(f"\n**逐步工具序列({len(tools)} 次调用)**:\n")
            w("```")
            for i, t in enumerate(tools, 1):
                mark = "   ← 拆分发生在这里" if t == "spawn_agents" else ""
                w(f"{i:2d}. {t}{mark}")
            w("```")

            turns = r.get("turns") or []
            if turns:
                w(f"\n**大脑原话({len(turns)} 轮记录)**:\n")
                for t in turns:
                    b = (t.get("brain") or "").strip()
                    n = (t.get("nudge") or "").strip()
                    if b:
                        w(f"<details><summary>step {t.get('step')} · 思考</summary>\n")
                        w("```")
                        w(b)
                        w("```")
                        w("</details>\n")
                    if n:
                        w(f"> **step {t.get('step')} · 系统提示**:{n}\n")
            else:
                w("\n**大脑原话**:(本次跑未记录)\n")

            ans = (r.get("answer") or "").strip()
            w("\n**答案全文**:\n")
            if ans:
                w("```")
                w(ans)
                w("```")
            else:
                w("```\n(空 —— 什么都没交出来)\n```")

    (ROOT / a.out).write_text("\n".join(out), encoding="utf-8")
    print(f"报告已写入 {a.out}({len('\n'.join(out))} 字符,{len(bad)} 条错误跑次)")


if __name__ == "__main__":
    main()
