"""从生产失败里挖新题（题库要从真实失败里长出来，才代表真实分布）。

两种用法：
1. 有 GCP 凭证：直接查 BigQuery 里的使用日志，捞出错/走满步数/花费异常的请求。
     python -m evals.mine_failures --bq 你的项目.日志数据集.usage_audit
2. 没凭证/离线：喂一份导出的日志 jsonl（每行一个请求记录）。
     python -m evals.mine_failures --file logs.jsonl

产出：候选清单 + 每条一份"半成品题"（题目/建议维度/待人工补金标），
写到 evals/candidates.jsonl(刻意放在 tasks/ 外 —— 半成品金标是 TODO,load_tasks 也会跳过
candidates* 文件名,双保险防止未审题混进正式题库)。人工补金标后再挪进 evals/tasks/。
"""
from __future__ import annotations

import argparse
import json
import sys

# 什么样的请求值得变成题（人话：出错的、绕圈的、异常烧钱的、用户重问的）
_WORTH = (
    ("status_error", lambda r: str(r.get("status", "")).lower() == "error", "请求出错"),
    ("max_steps", lambda r: str(r.get("terminated_reason", "")) == "max_steps", "绕圈到步数上限"),
    ("expensive", lambda r: float(r.get("cost_usd", 0) or 0) > 0.10, "单次花费异常（>$0.10）"),
    ("many_analyze", lambda r: int(r.get("analyze_calls", 0) or 0) >= 5, "看画面调用异常多（≥5 次）"),
)


def _to_candidate(r: dict, why: str) -> dict:
    return {
        "id": f"mined-{r.get('request_id', '?')[:12]}",
        "dims": ["TODO-标维度"],
        "kind": "single",
        "pinned": False,
        "user_query": r.get("query", "(日志里没存问题原文)"),
        "evaluation_criteria": {"output_checks": {"TODO": "人工补金标：期望什么行为/答案"}},
        "reward_basis": ["TODO"],
        "grounding_note": f"来自生产失败（{why}）；trace 摘要：{str(r.get('trace_summary', ''))[:200]}",
    }


def mine_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        for _key, pred, why in _WORTH:
            try:
                if pred(r):
                    out.append(_to_candidate(r, why))
                    break
            except Exception:
                continue
    return out


def _from_bigquery(table: str, days: int) -> list[dict]:
    from google.cloud import bigquery

    client = bigquery.Client()
    sql = f"""
      SELECT * FROM `{table}`
      WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        AND (LOWER(CAST(status AS STRING)) = 'error'
             OR terminated_reason = 'max_steps'
             OR SAFE_CAST(cost_usd AS FLOAT64) > 0.10)
      ORDER BY timestamp DESC LIMIT 200
    """
    return [dict(row) for row in client.query(sql).result()]


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="从生产失败挖新题")
    ap.add_argument("--bq", help="BigQuery 表名：项目.数据集.usage_audit")
    ap.add_argument("--file", help="离线：日志导出 jsonl")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out", default="evals/candidates.jsonl")   # GD-0:移出 tasks/,防自动入题库
    args = ap.parse_args(argv)

    if args.bq:
        rows = _from_bigquery(args.bq, args.days)
    elif args.file:
        rows = [json.loads(l) for l in open(args.file, encoding="utf-8") if l.strip()]
    else:
        print(__doc__)
        return 2

    cands = mine_rows(rows)
    with open(args.out, "w", encoding="utf-8") as fh:
        for c in cands:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"扫了 {len(rows)} 条日志，挖出 {len(cands)} 条候选 → {args.out}")
    print("下一步：人工过目、把 TODO 补成真金标，再挪进 evals/tasks/gen/（记得跑 evals check）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
