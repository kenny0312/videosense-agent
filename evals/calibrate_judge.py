"""AI 裁判的"对表"装置：裁判判一遍、人判一遍、算一致率——合格才准上岗。

为什么要这步：裁判(judge.py)判 PASS 的，人是不是也判 PASS？没验证过就让它出现在
报告里，等于用一把没校准的尺子唬人。对表合格(κ≥0.7)后，它才有资格以
"参考分"身份进报告(仍然永远不碰门禁)。

三步用法(按顺序)：
    set ANTHROPIC_API_KEY=...                        # 只有第 1 步要 key
    python -m evals.calibrate_judge collect          # ① 收集：裁判把历史真跑的答案判一遍
    python -m evals.calibrate_judge label            # ② 标注：你逐条判"做到/没做到"(~10分钟)
    python -m evals.calibrate_judge score            # ③ 算分：一致率 + Cohen's κ + 上岗结论

中间成果都在 evals/judge_calibration.jsonl，每行一条：
    {key, id, source, question, answer, assertion, judge, human}
label 支持中断续标(标一条存一条)；collect 重跑不会冲掉已有的人工标注。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
CAL_PATH = os.path.join(_HERE, "judge_calibration.jsonl")

# 答案全文的来源(优先)；归档 runs 里答案截断到 500 字，判据判断可能失真，不用
_FULL_RESULT_FILES = ("report_live.results.jsonl", "report_live2.results.jsonl")


def _nl_tasks() -> dict:
    """带 nl_assertions 的题：id -> (question, assertions)。"""
    out = {}
    for f in glob.glob(os.path.join(_HERE, "tasks", "gen", "*.jsonl")):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            asserts = (t.get("evaluation_criteria") or {}).get("nl_assertions")
            if asserts:
                q = t.get("user_query") or " / ".join(
                    s.get("utterance", "") for s in t.get("user", {}).get("script", []) or [])
                out[t["id"]] = (q, asserts)
    return out


def _gather_items() -> list:
    """从历史真跑结果里凑对表样本：每条 = 一道题的一份真实答案 × 一条判据。
    同题同答案去重(重复样本会虚高一致率)。归档 runs 里答案截到 500 字，
    只收看起来是完整的（<480 字），被截断的答案判不准，不要。"""
    tasks = _nl_tasks()
    sources = []                                    # (来源名, 记录列表)
    for name in _FULL_RESULT_FILES:
        path = os.path.join(_HERE, name)
        if os.path.exists(path):
            sources.append((name, [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]))
    for path in sorted(glob.glob(os.path.join(_HERE, "runs", "run-*-live.json"))):
        d = json.load(open(path, encoding="utf-8"))
        rows = [r for r in d.get("results", []) if len(r.get("answer") or "") < 480]
        sources.append((os.path.basename(path), rows))
    items, seen = [], set()
    for name, rows in sources:
        for r in rows:
            tid, ans = r.get("id"), r.get("answer") or ""
            if tid not in tasks or not ans.strip():
                continue
            digest = hashlib.sha1(ans.encode("utf-8")).hexdigest()[:10]
            if (tid, digest) in seen:
                continue
            seen.add((tid, digest))
            q, asserts = tasks[tid]
            for i, a in enumerate(asserts):
                items.append({"key": f"{tid}#{digest}#{i}", "id": tid, "source": name,
                              "question": q, "answer": ans, "assertion": a,
                              "judge": None, "human": None})
    return items


def _load() -> list:
    if not os.path.exists(CAL_PATH):
        return []
    return [json.loads(l) for l in open(CAL_PATH, encoding="utf-8") if l.strip()]


def _save(items: list):
    with open(CAL_PATH, "w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")


def collect() -> int:
    """① 裁判把样本判一遍。保留已有的人工标注(按 key 合并)。"""
    from evals import judge

    if not judge.available():
        print("没配 ANTHROPIC_API_KEY —— 裁判没法跑。set ANTHROPIC_API_KEY=... 后重试。")
        return 1
    old = {it["key"]: it for it in _load()}
    items = _gather_items()
    print(f"样本：{len(items)} 条（题 × 真实答案 × 判据，已去重）")
    for it in items:
        prev = old.get(it["key"]) or {}
        it["human"] = prev.get("human")                 # 人工标注永远不丢
        if prev.get("judge") is not None:
            it["judge"] = prev["judge"]                 # 判过的不重判(省钱+稳定)
            continue
        v = judge.judge_one(it["question"], it["answer"], [it["assertion"]])
        it["judge"] = bool(v["verdicts"][0]) if v["verdicts"] else None
        print(f"  [{it['id']}] 裁判：{'做到' if it['judge'] else '没做到'}")
    _save(items)
    n_j = sum(1 for it in items if it["judge"] is not None)
    print(f"裁判判决 {n_j}/{len(items)} 条已存 {CAL_PATH}")
    print("下一步：python -m evals.calibrate_judge label  （你来当人工基准）")
    return 0


def label() -> int:
    """② 交互式人工标注。你只看 问题+答案+判据，别看裁判怎么判的(防被带节奏)。"""
    items = _load()
    if not items:
        print("还没有样本 —— 先跑 collect。")
        return 1
    todo = [it for it in items if it["human"] is None]
    print(f"待标 {len(todo)} 条（已标 {len(items) - len(todo)}）。"
          "y=做到 n=没做到 s=跳过 q=退出（随时退，标一条存一条）\n")
    for it in todo:
        print("─" * 60)
        print(f"题目：{it['question']}")
        print(f"答案：{it['answer'][:600]}")
        print(f"判据：{it['assertion']}")
        while True:
            c = input("这条判据做到了吗? [y/n/s/q] ").strip().lower()
            if c in ("y", "n", "s", "q"):
                break
        if c == "q":
            break
        if c == "s":
            continue
        it["human"] = (c == "y")
        _save(items)
    done = sum(1 for it in items if it["human"] is not None)
    print(f"\n已标 {done}/{len(items)}。够 20 条就可以：python -m evals.calibrate_judge score")
    return 0


def _kappa(pairs: list) -> float:
    """Cohen's κ：扣掉"瞎蒙也会一致"的部分后，还剩多少真一致。"""
    n = len(pairs)
    po = sum(1 for j, h in pairs if j == h) / n
    pj = sum(1 for j, _ in pairs if j) / n
    ph = sum(1 for _, h in pairs if h) / n
    pe = pj * ph + (1 - pj) * (1 - ph)
    return 1.0 if pe == 1.0 else (po - pe) / (1 - pe)


def score() -> int:
    """③ 一致率 + κ + 上岗结论。"""
    items = _load()
    pairs = [(it["judge"], it["human"]) for it in items
             if it["judge"] is not None and it["human"] is not None]
    if len(pairs) < 10:
        print(f"两边都有判决的只有 {len(pairs)} 条，少于 10 条没法下结论 —— 先补 collect/label。")
        return 1
    po = sum(1 for j, h in pairs if j == h) / len(pairs)
    k = _kappa(pairs)
    print(f"样本 {len(pairs)} 条 ｜ 原始一致率 {po:.0%} ｜ Cohen's κ = {k:.2f}")
    print("（κ 的意思：把'瞎蒙也会撞对'的部分扣掉后剩下的真一致。1=完全一致，0=跟瞎蒙一样）\n")
    diffs = [it for it in items if it["judge"] is not None and it["human"] is not None
             and it["judge"] != it["human"]]
    if diffs:
        print("分歧清单（值得逐条看看是谁错了）：")
        for it in diffs:
            print(f"  [{it['id']}] 裁判={'做到' if it['judge'] else '没做到'} "
                  f"你={'做到' if it['human'] else '没做到'} ｜ 判据：{it['assertion'][:60]}")
        print()
    if k >= 0.7:
        print("结论：κ≥0.7，一致性够高 —— 裁判可以上岗，以【参考分】身份进报告（仍不碰门禁）。")
    elif k >= 0.4:
        print("结论：κ 在 0.4~0.7，勉强及格但别急 —— 先看分歧清单：如果错的多是裁判，"
              "改它的提示词再对一轮；如果错的多是判据写得歧义，改判据。")
    else:
        print("结论：κ<0.4，和瞎蒙差不多 —— 继续坐板凳，别进报告。")
    print("\n注意：样本 <30 条时 κ 本身波动也大，结论当方向看，别当精确数。")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    cmd = (argv or sys.argv[1:] or ["help"])[0]
    if cmd == "collect":
        return collect()
    if cmd == "label":
        return label()
    if cmd == "score":
        return score()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
