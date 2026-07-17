"""GEPA 总指挥:一轮进化 = gen0 基线 → 代际循环 → 冠军三连考 → 人审报告。

    python -m evals.gepa.evolve --budget-usd 20 --reserve-usd 10 --max-gens 6

数据角色(§4):train = 反思燃料 / val = Pareto 记分板 / sealed = 循环外终门。
预算语义(对抗审计后的口径):
  逐题记账 + 熔断 —— 每跑完一道题就入账,spent ≥ budget 时【题间硬停】;
  批次预检 —— 每个大批次(val 全堂/重考/终门)开跑前按实测单价估一遍,不够钱就不开跑;
  reserve 保证重考(双方全新 val×n1)+ 终门(双方 sealed×n2)一定跑得起。
判分口径:环境故障(断网/429)= None 剔除;代码崩溃 = 0 分计入(崩溃是失败)。
产出 runs/<id>/report.md —— 冠军 diff 由人审后手工落 lessons.py / node_specs.py 开 PR,
本程序绝不自动改生产文件。--resume <run_id> 按【题粒度】续跑(已记账的题不重付)。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

os.environ["REPL_USE_MOCK_DB"] = "1"     # GEPA 只在假世界考场里跑(硬设,不给真库留门)

from evals.gepa import frontier, gates, reflect, space, state

REFLECT_COST_EST = 0.05     # 反思器单次调用的保守估价(2.5-pro ~20k入1k出;不走 usage 计量,按估入账)


def _load_split_tasks():
    from evals.runner import load_tasks
    from evals.split_tool import MANIFEST_PATH
    tasks = {t["id"]: t for t in load_tasks("evals/tasks")}
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        splits = json.load(f)["splits"]
    by = {"train": [], "val": [], "sealed": []}
    for tid, sp in splits.items():
        if tid in tasks:
            by[sp].append(tasks[tid])
    for sp in by:
        by[sp].sort(key=lambda t: t["id"])
    return tasks, by


def _run_one(task: dict, n: int) -> dict:
    """真跑一道题。异常口径(对抗审计后):
    环境故障 → infra_error(分数 None,统计剔除);其它异常 → crash(0 分计入,
    判分器崩 = 这道题没过,不许靠崩溃退出比较)。两种情况都从 usage 抢救
    已烧掉的真实花费入账(runner 的局部 cost 随异常丢失,但钱已经花了)。"""
    from evals import runner as R
    try:
        if task.get("kind") == "multi":
            return R.run_case_multi(task, n=n, owner="gepa")
        return R.run_case(task, n=n, live=True, owner="gepa")
    except Exception as e:                                     # noqa: BLE001
        salvage = 0.0
        try:
            from pipeline.agentops import usage as _usage
            salvage = (_usage.summarize() or {}).get("cost_usd") or 0.0
        except Exception:
            pass
        rec = R._infra_record(task, n, e)
        if not R._is_infra_error(e):
            rec["status"] = "crash"
            rec["answer"] = f"[代码崩溃] {e}"
        rec["cost"]["cost_usd"] = round(salvage, 6)
        return rec


def _eval_batch(st: state.RunState, led: gates.Ledger, cid: str,
                task_list: list[dict], n: int, is_val: bool, basis: dict,
                label: str) -> bool:
    """跑一批题:逐题记账 + spent≥budget 题间熔断 + 每 10 题落盘 + 题粒度续跑
    (已在 scores_all[cid] 里的题直接跳过,不重付)。返回是否被熔断。串行 ——
    假世界是进程级单例(MOCK_WORLD env),并行会串世界。"""
    done = st.scores_all.get(cid, {})
    todo = [t for t in task_list if t["id"] not in done]
    for i, t in enumerate(todo):
        if led.spent >= led.budget:
            st.journal("hard_stop", cid=cid, label=label, undone=len(todo) - i,
                       spent=led.spent)
            st.spent_usd = led.spent
            st.save()
            return True
        rec = _run_one(t, n)
        led.add(st.record_one(cid, rec, is_val, basis), rollouts=n)
        st.rollouts = led.rollouts
        if (i + 1) % 10 == 0:
            st.spent_usd = led.spent
            st.save()
    if is_val:
        st.peeks += 1
    st.spent_usd = led.spent
    st.save()
    return False


def _row(st: state.RunState, cid: str) -> dict:
    return st.scores_all.get(cid, {})


def _mean(row: dict) -> "float | None":
    vals = [v for v in row.values() if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _basis_of(tasks: dict) -> dict:
    return {tid: t.get("reward_basis", []) for tid, t in tasks.items()}


def _minibatch(st: state.RunState, parent: str, cites: list, k: int, rng,
               train_ids: set) -> list[str]:
    """选准入小考的题:先取提案点名的病历题(去重),不足从父本的低分题补齐。
    只选父本【有分】且属于 train 堂的题(同批题父子对照才公平;不碰记分板)。"""
    have = {t for t, v in st.scores_all.get(parent, {}).items()
            if v is not None and t in train_ids}
    pool_cited = list(dict.fromkeys(t for t in cites if t in have))
    rest = sorted(have - set(pool_cited),
                  key=lambda t: st.scores_all[parent][t])      # 低分优先(病最重的题)
    rest, tail = rest[:k * 3], rest[k * 3:]                    # 低分带里再打散,防每代同一批
    rng.shuffle(rest)
    return (pool_cited + rest + tail)[:k]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="GEPA prompt 遗传进化")
    ap.add_argument("--budget-usd", type=float, default=20.0)
    ap.add_argument("--reserve-usd", type=float, default=10.0,
                    help="预留给重考+终门(重考=双方 val×n1≈$4.4 + 终门=双方 sealed×n2≈$5.9)")
    ap.add_argument("--max-gens", type=int, default=6)
    ap.add_argument("--val-n", type=int, default=1, help="记分板每题跑几次(val=100 后默认 1)")
    ap.add_argument("--train-n", type=int, default=1)
    ap.add_argument("--mini-k", type=int, default=8, help="准入小考题数")
    ap.add_argument("--mini-margin", type=float, default=0.25,
                    help="小考总分须高出父本的幅度(治均值回归假阳,审计 m6)")
    ap.add_argument("--reexam-n", type=int, default=1,
                    help="重考每题次数(冠军与 gen0【双方都】全新跑,对称对照)")
    ap.add_argument("--sealed-n", type=int, default=2)
    ap.add_argument("--resume", default=None, help="续跑 run_id(题粒度,已记账的不重付)")
    ap.add_argument("--dry", action="store_true", help="不真跑模型,只走通簿记(冒烟)")
    args = ap.parse_args(argv)

    tasks, by = _load_split_tasks()
    basis = _basis_of(tasks)
    st = state.RunState.load(args.resume) if args.resume else state.RunState(state.new_run_id())
    rng = random.Random(st.run_id)                  # 抽样可复现
    led = gates.Ledger(args.budget_usd, args.reserve_usd)
    led.spent = st.spent_usd                    # 旧账进总账;单价只看本进程增量
    print(f"运行 {st.run_id}:train {len(by['train'])} / val {len(by['val'])} / "
          f"sealed {len(by['sealed'])};预算 ${args.budget_usd}(预留 ${args.reserve_usd})")
    if args.dry:
        print("(--dry:只验簿记,不跑模型)"); return 0

    train_ids = {t["id"] for t in by["train"]}
    val_ids = {t["id"] for t in by["val"]}

    # ── gen0 基线:train 全堂取病历 + val 记分板 ─────────────────────
    if "gen0" not in st.candidates:
        st.add_candidate("gen0", None, {}, rationale="当前生产 prompt")
    space.reset()
    if _eval_batch(st, led, "gen0", by["train"], args.train_n, False, basis, "gen0-train") \
            or _eval_batch(st, led, "gen0", by["val"], args.val_n, True, basis, "gen0-val"):
        print("预算在 gen0 基线阶段就熔断 —— 检查单题成本后再来"); return 1
    st.journal("gen0", val_mean=st.val_mean("gen0"), spent=led.spent)
    print(f"gen0 基线:val 均分 {st.val_mean('gen0')},已花 ${led.spent}"
          f"(实测单题 ${led.unit():.4f})")

    # ── 代际循环 ────────────────────────────────────────────────
    child_seq = sum(1 for c in st.candidates if c.startswith("c"))
    # gen 计数语义 = 【真花了评估钱的子代数】;无提案/重复/非法的空转只吃 attempts
    # (首轮教训:429 空转把 gen 吃满,--resume 进门就退)。续跑时按此语义重算。
    st.gen = child_seq
    attempts, max_attempts = 0, args.max_gens * 3
    # 续跑时从谱系重建"试过的方向"(审计 m12:反思器不许失忆)
    tried_notes = [f"- {st.candidates[c].get('target', '?')}:{st.candidates[c].get('rationale', '')}"
                   for c in sorted(st.candidates) if c != "gen0"]
    seen_ov = {json.dumps(st.candidates[c]["overrides"], sort_keys=True, ensure_ascii=False)
               for c in st.candidates}
    while st.gen < args.max_gens and attempts < max_attempts:
        est_gen = (args.mini_k + len(by["val"]) * args.val_n) * led.unit() + REFLECT_COST_EST
        if led.spent + est_gen > args.budget_usd - args.reserve_usd:
            st.journal("evolution_close", gen=st.gen, spent=led.spent, est_gen=round(est_gen, 2))
            break
        attempts += 1
        front = frontier.pareto_wins(st.matrix)
        parent = frontier.sample_parent(front, rng) if front else "gen0"
        # 反思燃料严格只用 train 堂病历(§4:D_feedback=train;val 病历若进燃料,
        # 等于直接对着记分板优化 —— 自适应过拟合的正门。审计 B5/B9)
        meds = [v for t, v in st.meds.get(parent, {}).items() if t in train_ids]
        if not meds:
            st.journal("skip_gen", attempt=attempts, why=f"父本 {parent} 无 train 病历"); break
        rng.shuffle(meds)
        led.add(REFLECT_COST_EST)                   # 反思调用按估价入账(审计 m1)
        proposal = reflect.propose(
            space.space_doc(st.candidates[parent]["overrides"]),   # 父本现文本(审计 m4)
            meds[:10], "\n".join(tried_notes[-8:]))
        if not proposal or proposal.get("skip"):
            note = (proposal or {}).get("rationale", "反思器输出不可解析")
            st.journal("no_proposal", attempt=attempts, parent=parent, why=note)
            tried_notes.append(f"- (无效提案:{note[:80]})")
            continue
        overrides = reflect.to_overrides(proposal, st.candidates[parent]["overrides"])
        errs = space.validate(overrides)
        tried_notes.append(f"- {proposal['target']}:{proposal['rationale']}")
        if errs:
            st.journal("invalid", attempt=attempts, errs=errs); continue
        key = json.dumps(overrides, sort_keys=True, ensure_ascii=False)
        if key in seen_ov:                          # 重复提案不重付评估费(审计 m15)
            st.journal("duplicate", attempt=attempts, target=proposal["target"]); continue
        seen_ov.add(key)
        child_seq += 1
        st.gen += 1                                 # 只有真要花评估钱的子代才算一代
        cid = f"c{child_seq}"
        st.add_candidate(cid, parent, overrides, proposal["rationale"], proposal["cites"])
        st.candidates[cid]["target"] = proposal["target"]

        # 准入小考:同批 train 题上子代总分须高出父本 margin
        mini_ids = _minibatch(st, parent, proposal["cites"], args.mini_k, rng, train_ids)
        try:
            space.apply(overrides)
            if _eval_batch(st, led, cid, [tasks[t] for t in mini_ids], 1, False, basis,
                           f"mini-{cid}"):
                break
            child_mini = {t: st.scores_all[cid].get(t) for t in mini_ids}
            if not gates.minibatch_pass(child_mini, st.scores_all.get(parent, {}),
                                        args.mini_margin):
                st.journal("mini_fail", cid=cid, gen=st.gen, spent=led.spent)
                print(f"第{st.gen}代 {cid}({proposal['target']}):准入小考没过,弃")
                continue
            # 过闸 → val 全堂进记分板
            if _eval_batch(st, led, cid, by["val"], args.val_n, True, basis, f"val-{cid}"):
                break
        finally:
            space.reset()                           # 任何路径(含异常)都不许把变异留在进程里
        sig = gates.sign_test(st.matrix[cid], st.matrix[parent])
        st.journal("val_scored", cid=cid, gen=st.gen, val_mean=st.val_mean(cid),
                   vs_parent=sig, spent=led.spent)
        print(f"第{st.gen}代 {cid}({proposal['target']}):val {st.val_mean(cid)} "
              f"vs 父本 {st.val_mean(parent)}(胜{sig['wins']}负{sig['losses']} p={sig['p']}),"
              f"已花 ${led.spent}")

    # ── 冠军提名赛:全证据排名 → 显著性 → 对称重考 → 终门(sealed 对照 gen0)──
    # 排名用【虚拟合并行】= 原 val 行与该候选 @re 重考行的逐题平均(全部证据),
    # 治"幽灵霸榜":靠 n=1 运气登顶又被重考否决的候选,其虚拟分回落且进落选名单,
    # 不再挡住后来者。重考确认门只用双方【全新】行(对称,不掺旧运气)。
    def _virtual(cid: str) -> dict:
        base = dict(st.matrix.get(cid, {}))
        for t, v in st.scores_all.get(cid + "@re", {}).items():
            if v is None:
                continue
            b = base.get(t)
            base[t] = v if b is None else round((b + v) / 2, 4)
        return base

    g0_virtual = _virtual("gen0")
    ranked = sorted((c for c in st.matrix
                     if c != "gen0" and c not in st.disqualified
                     and _mean(_virtual(c)) is not None),
                    key=lambda c: _mean(_virtual(c)), reverse=True)
    verdict = {"champion": "gen0", "adopted": False,
               "why": "没有子代超过 gen0 —— 诚实的空结论(§4.5 纪律6)"}
    reexams = 0
    for champ in ranked:
        if reexams >= 3:
            break                                   # 重考封顶:一轮最多给 3 位候选掏重考钱
        verdict["champion"] = champ
        # 显著性预筛:合并证据或首考证据,谁硬用谁 —— 显著性不随均分单调,
        # 不显著只淘汰本人,不终止提名赛(c1 幽灵挡道、c2 没轮上的实跑教训)
        sig0 = gates.sign_test(_virtual(champ), g0_virtual)
        if not sig0["significant"]:
            sig0 = gates.sign_test(st.matrix.get(champ, {}), st.matrix.get("gen0", {}))
        if not sig0["significant"]:
            st.journal("nominee_skip", cid=champ, p=sig0["p"])
            verdict["why"] = f"候选 {champ} 对 gen0 不显著(p={sig0['p']})—— 视为平局不采纳"
            continue
        if led.spent + len(val_ids) * args.reexam_n * led.unit() > args.budget_usd:
            verdict["why"] = "预算不足以跑重考 —— 本轮结论无效,如实报告"
            break
        # 对称重考:双方都用全新 rollouts(gen0@re 已付过的题按题粒度复用)
        reexams += 1
        try:
            space.apply(st.candidates[champ]["overrides"])
            stopped = _eval_batch(st, led, champ + "@re", by["val"], args.reexam_n,
                                  False, basis, f"reexam-{champ}")
        finally:
            space.reset()
        stopped = stopped or _eval_batch(st, led, "gen0@re", by["val"], args.reexam_n,
                                         False, basis, "reexam-gen0")
        st.peeks += 1
        sig_re = gates.sign_test(_row(st, champ + "@re"), _row(st, "gen0@re"))
        st.journal("reexam", cid=champ, sig=sig_re, spent=led.spent, stopped=stopped)
        if stopped:
            verdict["why"] = "重考中预算熔断 —— 本轮结论无效,如实报告"
            break
        if not sig_re["significant"]:
            st.disqualified.append(champ)
            st.save()
            verdict["why"] = f"对称重考不显著(p={sig_re['p']})—— 首考是运气,取消资格"
            continue                                # 提名下一位
        if led.spent + 2 * len(by["sealed"]) * args.sealed_n * led.unit() > args.budget_usd:
            verdict["why"] = "预算不足以跑终门 —— 本轮结论无效,如实报告"
            break
        # 终门:双方 sealed;必过题失守(崩溃算失守,环境故障不算)即打回
        try:
            space.apply(st.candidates[champ]["overrides"])
            stopped = _eval_batch(st, led, champ + "@sealed", by["sealed"],
                                  args.sealed_n, False, basis, f"sealed-{champ}")
        finally:
            space.reset()
        stopped = stopped or _eval_batch(st, led, "gen0@sealed", by["sealed"],
                                         args.sealed_n, False, basis, "sealed-gen0")
        ch_row, g0_row = _row(st, champ + "@sealed"), _row(st, "gen0@sealed")
        pinned_ids = {t["id"] for t in by["sealed"] if t.get("pinned")}
        pinned_fail = [t for t in pinned_ids
                       if ch_row.get(t) is not None and ch_row.get(t) < 1.0]
        ch_mean, g0_mean = _mean(ch_row), _mean(g0_row)
        st.journal("sealed", cid=champ, champ_mean=ch_mean, gen0_mean=g0_mean,
                   pinned_fail=pinned_fail, spent=led.spent, stopped=stopped)
        if stopped:
            verdict["why"] = "终门中预算熔断 —— 本轮结论无效,如实报告"
        elif pinned_fail:
            verdict["why"] = f"终门必过题失守:{','.join(sorted(pinned_fail))} —— 打回"
        elif ch_mean is None or g0_mean is None or ch_mean < g0_mean - 1e-9:
            verdict["why"] = (f"sealed 对照下降(冠军 {ch_mean} vs gen0 {g0_mean})"
                              "—— 过拟合记分板,打回")
        else:
            verdict.update(adopted=True, sealed_champ=ch_mean, sealed_gen0=g0_mean,
                           why="三连考全过 —— 产出 diff 交人审")
        break
    st.spent_usd = led.spent
    st.save()
    _write_report(st, verdict, args, led)
    print(f"\n结论:{verdict['why']}\n报告:{os.path.join(st.dir, 'report.md')} "
          f"(总花费 ${led.spent},val 偷看 {st.peeks} 次)")
    return 0


def _write_report(st: state.RunState, verdict: dict, args, led) -> None:
    champ = verdict["champion"]
    L = [f"# GEPA 运行报告 {st.run_id}", "",
         f"- 结论:**{verdict['why']}**",
         f"- 总花费:${st.spent_usd} / 预算 ${args.budget_usd}"
         f"(实测单题 ${led.unit():.4f};含反思器估价 ${REFLECT_COST_EST}/次);"
         f"val 偷看 {st.peeks} 次(纪律3)",
         f"- 候选数:{len([c for c in st.candidates])};代数:{st.gen}", ""]
    L.append("## 记分板(val 均分;环境故障题剔除,崩溃题 0 分计入)")
    for c in sorted(st.matrix, key=lambda c: -(st.val_mean(c) or 0)):
        info = st.candidates.get(c, {})
        L.append(f"- {c}(父:{info.get('parent') or '-'}):{st.val_mean(c)}"
                 + (f" —— {info.get('rationale', '')}" if info.get("rationale") else ""))
    if champ != "gen0":
        L += ["", f"## 冠军 {champ} 谱系", " → ".join(st.lineage(champ)), "",
              "## 冠军 diff(人审后手工落 lessons.py / node_specs.py 开 PR)",
              space.diff_doc(st.candidates[champ]["overrides"])]
        if verdict.get("adopted"):
            L += ["", f"- sealed 对照(唯一对外口径,纪律4):冠军 {verdict['sealed_champ']}"
                  f" vs gen0 {verdict['sealed_gen0']}"]
    L += ["", "## 待办(纪律5)", "- [ ] 出 ~10 道全新加试题(新翻转变体/真实日志挖掘),冠军上线前加试"]
    with open(os.path.join(st.dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    raise SystemExit(main())
