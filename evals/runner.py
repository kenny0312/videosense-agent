"""评测跑批器：跑每道题 n 次 -> 出"连做 k 次的比例" + 各方面分 -> 给结论 -> 生成报告。

命令行：
    python -m evals.runner                 # 跑 evals/tasks 里的题（好策略），生成首次基线报告
    python -m evals.runner --compare       # 好策略(旧版) vs 回归策略(新版)，生成对比报告（演示跳伞题失守）
    python -m evals.runner <目录> --out <文件>

脚本车道：不调 Gemini、不联网、不碰 DB。结论四种：变好 / 变差·打回 / 有得有失·待人看 / 没明显变化。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from evals import report, scorers
from evals.world import LiveWorld, ScriptedWorld, live_preflight

_GUARD = 0.15  # 某个方面掉超过 15 个百分点，算"变差"


# ── 读题 ────────────────────────────────────────────────────────────
def _strip_jsonc(text: str) -> str:
    """去掉整行 // 注释（本项目 .jsonc 只用整行注释）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))


def load_tasks(path: str) -> list[dict]:
    """加载题目：目录则递归读 *.jsonc / *.json（单对象，去整行注释）+ *.jsonl（一行一题）。"""
    if os.path.isdir(path):
        files = []
        for root, _dirs, fnames in os.walk(path):
            files += [os.path.join(root, fn) for fn in fnames
                      if fn.endswith((".jsonc", ".json", ".jsonl"))]
    else:
        files = [path]
    tasks = []
    for f in sorted(files):
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        if f.endswith(".jsonl"):
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("//"):
                    tasks.append(json.loads(line))
        else:
            tasks.append(json.loads(_strip_jsonc(text)))
    return sorted(tasks, key=lambda t: t["id"])


# ── 判分分发 ─────────────────────────────────────────────────────────
def dispatch_scorers(task: dict, res) -> dict:
    """把一道题适用的判分器都跑一遍，输出 {判分器名 -> 分}（短名，与 reward_basis 对齐）。"""
    ec = task["evaluation_criteria"]
    s: dict = {}
    if ec.get("required_actions") is not None:
        s["required_actions"] = scorers.toolseq_match(res.trace, ec["required_actions"])
    if ec.get("no_call_expected"):
        s["no_call"] = 1.0 if not res.trace else 0.0
    for name, cfg in ec.get("output_checks", {}).items():
        if name == "honesty":
            s["honesty"] = scorers.refusal_ok(res.answer, cfg)
        elif name == "retrieval":
            # 看"交付面"（答案 + show_* 侧信道），不是只看答案文本 —— 收口契约本来就要求 id 不进文本
            s["retrieval"] = scorers.recall_at_k(scorers.surface_blob(res), cfg.get("must_surface_video_ids", []), cfg.get("k", 5))
        elif name == "timestamp":
            s["timestamp"] = scorers.timestamp_iou(res.answer, cfg)
        elif name == "count":
            s["count"] = scorers.answer_count(res.answer, cfg)
        elif name == "entity_match":
            s["entity_match"] = scorers.entity_match(res.answer, cfg)
        elif name == "no_id_leak":
            s["no_id_leak"] = scorers.no_id_leak(res.answer, cfg)
        elif name == "identity":
            s["identity"] = scorers.no_provider_leak(res.answer, cfg)
        elif name == "safety":
            s["safety"] = scorers.refusal_ok(res.answer, {"expect_refusal": cfg.get("expect_refusal", True)})
    # state_assertions / jga_slots 需要 world/session（Mode B / dual-control 才有），单轮跳过
    return s


# ── 多轮（真 Gemini + 脚本用户，经 DualControlSession）────────────────
_MUTATING_ACTIONS = {"upload_video", "enrich_video", "paste_image"}


def _has_user_actions(task: dict) -> bool:
    """要【改共享状态】才能判分的题（计分含 state_assertions，或脚本带上传/入库/贴图动作）——
    世界动作还没接真执行器，先跳过。say/correct 只是说话，照跑。"""
    if "state_assertions" in (task.get("reward_basis") or []):
        return True
    return any((step.get("action") or {}).get("type") in _MUTATING_ACTIONS
               for step in task.get("user", {}).get("script", []) or [])


def _titles() -> dict:
    """每个视频的可区分别名：英文标题 + 时长数字（验指代解析用，见 scorers.score_jga）。"""
    from repl._mock_db import VIDEOS

    return {v[0]: [v[1], str(int(v[3]))] for v in VIDEOS}


def score_multi(task: dict, turns) -> dict:
    """多轮判分（纯函数，离线可测）。turns = TurnRecord 列表（who/text/trace/ledger）。"""
    from types import SimpleNamespace

    agents = [t for t in turns if t.who == "agent"]
    blobs = [scorers.surface_blob(SimpleNamespace(answer=t.text, trace=t.trace, ledger=t.ledger))
             for t in agents]
    all_trace = [step for t in agents for step in t.trace]
    final = SimpleNamespace(answer=agents[-1].text if agents else "",
                            trace=all_trace,
                            ledger={k: v for t in agents for k, v in (t.ledger or {}).items()})
    ec = task["evaluation_criteria"]
    s = dispatch_scorers(task, final)              # required_actions/output_checks 作用于整场+最终答案
    if ec.get("jga_slots"):
        s["jga"] = scorers.score_jga(blobs, ec["jga_slots"], titles=_titles())
    return s


def run_case_multi(task: dict, n: int | None = None, owner: str = "eval") -> dict:
    from evals.session import DualControlSession

    n = n or task.get("n_rollouts", 5)
    successes = 0
    per_dim: dict = {}
    last_turns = None
    for _ in range(n):
        out = DualControlSession(task, owner=owner).run()
        sc = score_multi(task, out["turns"])
        if scorers.case_pass(sc, task["reward_basis"]):
            successes += 1
        for d, v in sc.items():
            per_dim.setdefault(d, []).append(v)
        last_turns = out["turns"]
    agents = [t for t in (last_turns or []) if t.who == "agent"]
    return {
        "id": task["id"], "pinned": task.get("pinned", False), "dims": task.get("dims", []),
        "n": n, "successes": successes, "passed": successes == n,
        "pass_k": {k: scorers.passk(successes, n, k) for k in (1, 3, 5)},
        "scores": {d: _mean(v) for d, v in per_dim.items()},
        "answer": agents[-1].text if agents else None,
        "tools": [st.get("tool") for t in agents for st in t.trace],
        "kind": "multi",
    }


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def run_case(task: dict, script=None, tool_results=None, n: int | None = None,
             live: bool = False, owner: str = "eval") -> dict:
    n = n or task.get("n_rollouts", 5)
    successes = 0
    per_dim: dict = {}
    last = None
    steps = task.get("max_steps", 16)
    for _ in range(n):
        if live:                                # Mode B：真 Gemini
            res = LiveWorld(owner=owner).run(task["user_query"], max_steps=steps)
        else:                                   # Mode A：脚本车道
            res = ScriptedWorld(script, tool_results or {}).run(task["user_query"], max_steps=steps)
        sc = dispatch_scorers(task, res)
        if scorers.case_pass(sc, task["reward_basis"]):
            successes += 1
        for d, v in sc.items():
            per_dim.setdefault(d, []).append(v)
        last = res
    return {
        "id": task["id"],
        "pinned": task.get("pinned", False),
        "dims": task.get("dims", []),
        "n": n,
        "successes": successes,
        "passed": successes == n,
        "pass_k": {k: scorers.passk(successes, n, k) for k in (1, 3, 5)},
        "scores": {d: _mean(v) for d, v in per_dim.items()},
        "answer": last.answer if last else None,
        "tools": [t.get("tool") for t in (last.trace if last else [])],   # 最后一次的工具链（归因用）
    }


def run_suite(tasks: list[dict], policies: dict | None = None, tool_results: dict | None = None,
              live: bool = False, n: int | None = None) -> list[dict]:
    if live:                                  # Mode B：真 Gemini。单轮 + 脚本多轮；带用户动作的先跳过
        singles = [t for t in tasks if t.get("kind") != "multi"]
        multis = [t for t in tasks if t.get("kind") == "multi" and not _has_user_actions(t)]
        skipped = [t["id"] for t in tasks if t.get("kind") == "multi" and _has_user_actions(t)]
        todo = [(t, False) for t in singles] + [(t, True) for t in multis]
        out = []
        for i, (t, is_multi) in enumerate(todo, 1):
            try:
                r = run_case_multi(t, n=n) if is_multi else run_case(t, live=True, n=n)
            except Exception as e:            # 单题崩溃不拖垮整场：记为没过 + 错误信息
                r = {"id": t["id"], "pinned": t.get("pinned", False), "dims": t.get("dims", []),
                     "n": n or 1, "successes": 0, "passed": False,
                     "pass_k": {1: 0.0, 3: None, 5: None}, "scores": {}, "answer": f"[error] {e}"}
            out.append(r)
            tag = "多轮" if is_multi else "单轮"
            print(f"[{i}/{len(todo)}] ({tag}) {t['id']:<34} {'过' if r['passed'] else '没过'}", flush=True)
        if skipped:
            print(f"跳过 {len(skipped)} 道要改共享状态才能判分的双向控制题（上传/入库/贴图，待接真执行器）：")
            print("  " + "、".join(skipped), flush=True)
        return out
    tool_results = tool_results or {}         # 脚本车道：只跑有 fixture 策略的题（其余留给 Mode B）
    out = []
    for t in tasks:
        if policies is not None and t["id"] not in policies:
            continue
        out.append(run_case(t, policies[t["id"]], tool_results.get(t["id"], {}), n=n))
    return out


# ── 结论 ────────────────────────────────────────────────────────────
def _all_dims(results):
    dims = []
    for r in results:
        for d in r["scores"]:
            if d not in dims:
                dims.append(d)
    return dims


def _dim_mean(results, dim):
    vals = [r["scores"][dim] for r in results if dim in r["scores"]]
    return _mean(vals) if vals else None


def classify(cur: list[dict], base: list[dict] | None = None) -> dict:
    if base is None:
        pinned_fail = [r["id"] for r in cur if r["pinned"] and not r["passed"]]
        if pinned_fail:
            return {"label": "变差 · 打回", "kind": "bad",
                    "reasons": ["必过题没过：" + "、".join(pinned_fail)]}
        return {"label": "全部通过 · 建立基线", "kind": "ok", "reasons": []}

    basemap = {r["id"]: r for r in base}
    flips = [r["id"] for r in cur
             if r["pinned"] and basemap.get(r["id"], {}).get("passed") and not r["passed"]]
    regressed, improved = [], []
    for d in _all_dims(cur):
        cv, bv = _dim_mean(cur, d), _dim_mean(base, d)
        if cv is None or bv is None:
            continue
        delta = cv - bv
        if delta <= -_GUARD:
            regressed.append(d)
        elif delta >= _GUARD:
            improved.append(d)

    reasons = []
    if flips:
        reasons.append("必过题失守：" + "、".join(flips))
    if regressed:
        reasons.append("变差的方面：" + "、".join(report.DIM_LABEL.get(d, d) for d in regressed))
    if improved:
        reasons.append("变好的方面：" + "、".join(report.DIM_LABEL.get(d, d) for d in improved))

    if flips:
        return {"label": "变差 · 打回", "kind": "bad", "reasons": reasons}
    if regressed and improved:
        return {"label": "有得有失 · 待人看", "kind": "warn", "reasons": reasons}
    if regressed:
        return {"label": "变差 · 打回", "kind": "bad", "reasons": reasons}
    if improved:
        return {"label": "变好", "kind": "ok", "reasons": reasons}
    return {"label": "没明显变化", "kind": "neutral", "reasons": []}


# ── 命令行输出 ───────────────────────────────────────────────────────
def print_summary(cur, base, verdict):
    print("\n道题结果：")
    basemap = {r["id"]: r for r in (base or [])}
    for r in cur:
        mark = "过 " if r["passed"] else "没过"
        p3 = r["pass_k"].get(3)
        p3s = "-" if p3 is None else f"{p3:.2f}"
        pin = "[必过]" if r["pinned"] else "     "
        flip = ""
        b = basemap.get(r["id"])
        if b and b["passed"] and not r["passed"]:
            flip = "  <- 由过变没过"
        print(f"  {pin} {r['id']:<26} {mark}  连过3次:{p3s}{flip}")
    print(f"\n结论：{verdict['label']}")
    for why in verdict["reasons"]:
        print(f"   · {why}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:                                   # Windows 控制台默认 cp1252，中文输出会崩 -> 切 utf-8
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="VS 评测跑批器")
    ap.add_argument("tasks_dir", nargs="?", default="evals/tasks")
    ap.add_argument("--out", default="evals/report.html")
    ap.add_argument("--compare", action="store_true", help="脚本车道：好策略(旧版) vs 回归策略(新版)")
    ap.add_argument("--live", action="store_true", help="Mode B：真 Gemini 进循环（要 GCP 凭证 + 花 token）")
    ap.add_argument("--n", type=int, default=None, help="覆盖每题跑几次（真跑先 --n 1 冒烟）")
    ap.add_argument("--list", action="store_true", help="只列出数据集（按维度统计），不跑")
    args = ap.parse_args(argv)

    tasks = load_tasks(args.tasks_dir)

    if args.list:
        by_dim = Counter(d for t in tasks for d in t.get("dims", ["?"]))
        single = sum(1 for t in tasks if t.get("kind") != "multi")
        pinned = sum(1 for t in tasks if t.get("pinned"))
        print(f"数据集：{len(tasks)} 道题（单轮 {single}，多轮 {len(tasks) - single}，必过题 {pinned}）")
        for d, c in sorted(by_dim.items(), key=lambda x: -x[1]):
            print(f"  {d:<16} {c}")
        return 0

    if args.live:
        msg = live_preflight()
        if msg:
            print(msg)
            return 2
        cur = run_suite(tasks, live=True, n=args.n)
        v = classify(cur)
        html = report.render(cur, v, baseline=None, title="评测报告 · 真 Gemini（Mode B）")
        cur_print, base_print = cur, None
    else:
        from evals.fixtures.policies import GOOD, REGRESSED, TOOL_RESULTS

        base = run_suite(tasks, GOOD, TOOL_RESULTS, n=args.n)
        if args.compare:
            cur = run_suite(tasks, REGRESSED, TOOL_RESULTS, n=args.n)
            v = classify(cur, base)
            html = report.render(cur, v, baseline=base, title="评测报告 · 新版 vs 旧版（演示：跳伞题回归）")
            cur_print, base_print = cur, base
        else:
            cur = base
            v = classify(base)
            html = report.render(base, v, baseline=None, title="评测报告 · 首次基线")
            cur_print, base_print = base, None

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    results_path = args.out.rsplit(".", 1)[0] + ".results.jsonl"   # 每题结果落盘，便于归因
    with open(results_path, "w", encoding="utf-8") as fh:
        for r in cur_print:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    print_summary(cur_print, base_print, v)
    from evals import dashboard                                     # 归档本次运行 + 重建本地仪表盘
    mode = "live" if args.live else ("compare" if args.compare else "scripted")
    dashboard.save_run(cur_print, v, mode)
    dash = dashboard.rebuild()
    print(f"\n报告已生成：{args.out}（每题明细：{results_path}）")
    print(f"仪表盘已更新：{dash}  ← 浏览器打开这个看历史/趋势，不用 push GitHub")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
