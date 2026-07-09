"""AI 裁判（只做参考分，不进门禁）：给题里 nl_assertions 写的开放式判据打分。

为什么用别家模型当裁判：被考的是 Gemini，让 Gemini 判自己会偏心 ——
所以裁判固定用非 Gemini 家族：配了 ANTHROPIC_API_KEY 用 Claude，
配了 OPENAI_API_KEY 用 GPT（两个都配时用 Claude）。key 放仓库根的 .env
（已 gitignore，绝不进 git）。为什么只做参考：裁判还没和人工标注对过表
（对表达标前别拿它挡合并，见 calibrate_judge.py）。

    python -m evals.judge evals/report_live.results.jsonl     # 给最近一次真跑补裁判分
一个 key 都没配时会礼貌跳过，不报错。
"""
from __future__ import annotations

import json
import os
import sys

# 固定住：换裁判=换尺子，要重新对表（calibrate_judge 会记下判卷用的是哪个模型）
JUDGE_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
JUDGE_MODEL_OPENAI = "gpt-5-mini"


def _load_env_file():
    """把仓库根 .env（gitignored）里的 KEY=VALUE 载入环境。真环境变量优先。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env_file()


def available() -> str:
    """有哪个家族的裁判可用：'anthropic' / 'openai' / ''（没有，布尔判断也好使）。"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return ""


def judge_model() -> str:
    return {"anthropic": JUDGE_MODEL_ANTHROPIC, "openai": JUDGE_MODEL_OPENAI}.get(available(), "")


def _ask_anthropic(prompt: str) -> str:
    import anthropic

    resp = anthropic.Anthropic().messages.create(
        model=JUDGE_MODEL_ANTHROPIC, max_tokens=600,
        messages=[{"role": "user", "content": prompt}])
    return resp.content[0].text if resp.content else ""


def _ask_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    kwargs = dict(model=JUDGE_MODEL_OPENAI,
                  messages=[{"role": "user", "content": prompt}],
                  max_completion_tokens=1500)      # gpt-5 系会先"想"再答，额度给足免得答案被吃掉
    try:
        resp = client.chat.completions.create(reasoning_effort="low", **kwargs)
    except Exception:
        resp = client.chat.completions.create(**kwargs)   # 老版本不认 reasoning_effort 就裸跑
    return resp.choices[0].message.content or ""


def judge_one(question: str, answer: str, assertions: list[str]) -> dict:
    """让裁判逐条判：这条判据答案做到了没。返回 {做到几条, 总条数, 逐条意见, 用的哪个裁判}。"""
    rubric = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(assertions))
    prompt = (
        "你是评卷人。下面是用户的问题、助手的回答、和几条评卷判据。\n"
        "对每条判据：先用一句话说理由，再给结论 PASS 或 FAIL。最后一行输出 JSON：\n"
        '{"verdicts": [true/false, ...]}（按判据顺序）。\n\n'
        f"【问题】{question}\n\n【回答】{answer}\n\n【判据】\n{rubric}"
    )
    text = _ask_anthropic(prompt) if available() == "anthropic" else _ask_openai(prompt)
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
            "verdicts": verdicts, "notes": text[:800], "judge_model": judge_model()}


def judge_results(results_path: str) -> int:
    """给一份结果明细里带 nl_assertions 的题补裁判分，写成 <原名>.judge.jsonl。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not available():
        print("没配裁判 key（ANTHROPIC_API_KEY 或 OPENAI_API_KEY，放仓库根 .env）"
              "—— 跳过 AI 裁判（它只是参考分，不影响门禁）。")
        return 0
    print(f"裁判：{judge_model()}")
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
