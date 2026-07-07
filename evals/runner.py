"""评测跑批器：跑每道题 n 次 -> 出"连做 k 次的比例" + 各方面分 -> 给结论 -> 生成报告。

命令行：
    python -m evals.runner                 # 跑 evals/tasks 里的题（好策略），生成首次基线报告
    python -m evals.runner --compare       # 好策略(旧版) vs 回归策略(新版)，生成对比报告（演示跳伞题失守）
    python -m evals.runner <目录> --out <文件>

脚本车道：不调 Gemini、不联网、不碰 DB。结论四种：变好 / 变差·打回 / 有得有失·待人看 / 没明显变化。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from evals import report, scorers
from evals.world import LiveWorld, ScriptedWorld, live_preflight

_GUARD = 0.15  # 某个方面掉超过 15 个百分点，算"变差"


# ── 读题 ────────────────────────────────────────────────────────────
def _strip_jsonc(text: str) -> str:
    """去掉整行 // 注释（本项目 .jsonc 只用整行注释）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))


def load_tasks(path: str) -> list[dict]:
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.jsonc")) + glob.glob(os.path.join(path, "*.json")))
    else:
        files = [path]
    tasks = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            tasks.append(json.loads(_strip_jsonc(fh.read())))
    return sorted(tasks, key=lambda t: t["id"])


# ── 判分分发 ─────────────────────────────────────────────────────────
def dispatch_scorers(task: dict, res) -> dict:
    ec = task["evaluation_criteria"]
    s: dict = {}
    if ec.get("required_actions") is not None:
        s["required_actions"] = scorers.toolseq_match(res.trace, ec["required_actions"])
    for name, cfg in ec.get("output_checks", {}).items():
        key = f"output_checks.{name}"
        if name == "honesty":
            s[key] = scorers.refusal_ok(res.answer, cfg)
        elif name == "retrieval":
            s[key] = scorers.recall_at_k(res.answer, cfg.get("must_surface_video_ids", []), cfg.get("k", 5))
        elif name == "entity_match":
            s[key] = scorers.entity_match(res.answer, cfg)
        elif name == "no_id_leak":
            s[key] = scorers.no_id_leak(res.answer, cfg)
        elif name == "identity":
            s[key] = scorers.no_provider_leak(res.answer, cfg)
        elif name == "count":
            s[key] = scorers.answer_count(res.answer, cfg)
    return s


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
    }


def run_suite(tasks: list[dict], policies: dict | None = None, tool_results: dict | None = None,
              live: bool = False, n: int | None = None) -> list[dict]:
    if live:
        return [run_case(t, live=True, n=n) for t in tasks]
    tool_results = tool_results or {}
    return [run_case(t, policies[t["id"]], tool_results.get(t["id"], {}), n=n) for t in tasks]


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
    args = ap.parse_args(argv)

    tasks = load_tasks(args.tasks_dir)

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
    print_summary(cur_print, base_print, v)
    print(f"\n报告已生成：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
