"""从最近一次真跑成绩，把题重新分成回归/能力套件，写进 evals/suites.json。

    python -m evals.tag_suite            # 用最近一次真跑归档重新分层
    python -m evals.tag_suite --dry      # 只看会怎么分，不写文件

判据：闭眼全过(successes==n 且 n>=2)→ 回归套件（没区分度了，当防退步网）；
其余（会挂 / 时好时坏 / 环境故障）→ 能力套件（认真跑）。
【空白答案 bug】撞挂的题不算进回归——它们其实 agent 会做，只是被产品 bug 拖挂，
留在能力套件里，等 bug 修好再看。
"""
from __future__ import annotations

import json
import sys

from evals.suites import SUITES_PATH


def _blank_hit(rec) -> bool:
    """这道题的失败样本里有没有'空白答案'（安全拦截 bug）。"""
    ff = rec.get("first_fail") or {}
    if (ff.get("answer") or "") == "" and rec.get("kind") != "multi":
        return True
    for t in ff.get("turns") or []:
        if t.get("who") == "agent" and not (t.get("text") or "").strip():
            return True
    return False


def classify(results: list) -> dict:
    reg, cap, blank = [], [], []
    for r in results:
        if r.get("status") == "infra_error":
            cap.append(r["id"]); continue          # 环境故障没判过，留能力套件
        s, n = r.get("successes", 0), r.get("n", 1)
        if n >= 2 and s == n:
            reg.append(r["id"])
        else:
            cap.append(r["id"])
            if _blank_hit(r):
                blank.append(r["id"])
    return {"regression": sorted(reg), "capability": sorted(cap), "blank_hit": sorted(blank)}


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    argv = argv if argv is not None else sys.argv[1:]
    from evals.dashboard import latest_run

    run = latest_run("live")
    if not run:
        print("还没有真跑归档 —— 先 python -m evals.runner --live 跑一次再分层。")
        return 1
    results = run.get("results", [])
    if not any(r.get("n", 1) >= 2 for r in results):
        print("最近这次真跑是 n=1（冒烟档）——分层要 n>=2 才可信（连过几次才算稳）。先跑正式档。")
        return 1
    c = classify(results)
    print(f"依据真跑 {run.get('ts')}（{len(results)} 题）分层：")
    print(f"  回归套件（闭眼全过、防退步）：{len(c['regression'])} 题")
    print(f"  能力套件（会挂/闪烁，认真跑）：{len(c['capability'])} 题"
          f"（其中 {len(c['blank_hit'])} 题是被空白答案 bug 拖挂，非能力问题）")
    if "--dry" in argv:
        print("（--dry 只看不写）")
        return 0
    out = {"generated_from": run.get("ts"), "criterion": "successes==n & n>=2",
           "regression": c["regression"], "capability_blank_hit": c["blank_hit"]}
    with open(SUITES_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"已写 {SUITES_PATH}")
    print("以后：python -m evals.runner --live --suite capability  只认真跑有区分度的题（省钱）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
