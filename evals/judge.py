"""AI 裁判（只做参考分，不进门禁）：给题里 nl_assertions 写的开放式判据打分。

为什么用别家模型当裁判：被考的是 Gemini，让 Gemini 判自己会偏心 ——
所以裁判固定用 Claude（跨家族）。为什么只做参考：裁判还没和人工标注对过表
（对表达标前别拿它挡合并，见设计文档 A3）。

    python -m evals.judge evals/report_live.results.jsonl     # 给最近一次真跑补裁判分
没配 ANTHROPIC_API_KEY 时会礼貌跳过，不报错。
"""
from __future__ import annotations

import json
import os
import sys

JUDGE_MODEL = "claude-haiku-4-5-20251001"   # 固定住：换裁判=换尺子，要重新对表


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def judge_one(question: str, answer: str, assertions: list[str]) -> dict:
    """让裁判逐条判：这条判据答案做到了没。返回 {做到几条, 总条数, 逐条意见}。"""
    import anthropic

    rubric = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(assertions))
    prompt = (
        "你是评卷人。下面是用户的问题、助手的回答、和几条评卷判据。\n"
        "对每条判据：先用一句话说理由，再给结论 PASS 或 FAIL。最后一行输出 JSON：\n"
        '{"verdicts": [true/false, ...]}（按判据顺序）。\n\n'
        f"【问题】{question}\n\n【回答】{answer}\n\n【判据】\n{rubric}"
    )
    resp = anthropic.Anthropic().messages.create(
        model=JUDGE_MODEL, max_tokens=600,
        messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text if resp.content else ""
    verdicts = []
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                verdicts = [bool(x) for x in json.loads(line).get("verdicts", [])]
            except Exception:
                pass
            break
    return {"passed": sum(verdicts), "total": len(assertions),
            "verdicts": verdicts, "notes": text[:800]}


def judge_results(results_path: str) -> int:
    """给一份结果明细里带 nl_assertions 的题补裁判分，写成 <原名>.judge.jsonl。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not available():
        print("没配 ANTHROPIC_API_KEY —— 跳过 AI 裁判（它只是参考分，不影响门禁）。")
        return 0
    rows = [json.loads(l) for l in open(results_path, encoding="utf-8") if l.strip()]
    out_path = results_path.rsplit(".", 1)[0] + ".judge.jsonl"
    n = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in rows:
            asserts = (r.get("expect") or {}).get("nl_assertions") or []
            if not asserts or not r.get("answer"):
                continue
            v = judge_one(r.get("question", ""), r["answer"], asserts)
            fh.write(json.dumps({"id": r["id"], **v}, ensure_ascii=False) + "\n")
            n += 1
            print(f"[{r['id']}] 裁判：做到 {v['passed']}/{v['total']} 条")
    print(f"共判 {n} 题，明细：{out_path}（参考分，不进门禁）")
    return 0


if __name__ == "__main__":
    raise SystemExit(judge_results(sys.argv[1] if len(sys.argv) > 1 else "evals/report_live.results.jsonl"))
