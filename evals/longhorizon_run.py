"""P0-7 / Phase 1:gate 实验三臂跑机(真库、真模型、真钱)。

为什么不复用 evals/runner.py:那套的 LiveWorld 强制 mock DB(world.py 里 REPL_USE_MOCK_DB=1),
而本实验的题库(longhorizon_bank)是从【真库 514 条】建的 gold —— 必须打真库。

三臂(docs/longhorizon-multiagent-plan.md §3.1,单变量):
  A 单脑   USE_SUBAGENTS=0
  B 一层   USE_SUBAGENTS=1(FANOUT=6, MAX_STEPS=4)
  C 裸下钻 B + USE_DEPTH2=1              ← 唯一差异
控制变量(§3.3):同模型/同工具白名单(web_search 关)/同 DB 快照/同答案契约/同熔断
($0.80 per-tree)与墙钟(900s)/同 MAX_VIDEOS_PER_REQUEST/USE_IN_VIDEO_SEARCH 三臂同开。
缓存命名空间按 arm×rep 隔离(红队 C3:否则先跑的臂给后跑的臂喂暖缓存,成本不是全口径)。

停机三闸(§3.7,写死在代码里,不靠自觉):
  闸1 dry-run:单题成本中位 > 2× 预估 或 报错率 > 20% 或 任一臂配额触发 → 停;
  闸2 半程:花到 HALF_STOP_USD 时完成率 < 50% → 停;
  闸3 硬顶:HARD_CAP_USD → 立即停,按已有数据出结论。
【每题跑完立刻落盘】(记忆教训:结果跑完才落盘 = 被杀就白花钱)。

跑法:
  python -m evals.longhorizon_run --split dev --arms A,B,C --n 1 --dry-run     # 试跑
  python -m evals.longhorizon_run --split holdout --arms A,B,C --n 2 --budget 42
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "evals" / "runs"

ARMS = {
    "A": {"USE_SUBAGENTS": "0", "USE_DEPTH2": "0"},
    "B": {"USE_SUBAGENTS": "1", "USE_DEPTH2": "0"},
    "C": {"USE_SUBAGENTS": "1", "USE_DEPTH2": "1"},
}
# 三臂共同的控制变量(每题跑前重设,防上一题的残留)
COMMON_ENV = {
    "USE_WEB_SEARCH": "0",
    "USE_IN_VIDEO_SEARCH": "1",          # 三臂同开(§3.3)
    "USE_SEMANTIC_SEARCH": "1",
    "MAX_TREE_COST_USD": "0.80",         # per-tree 熔断
    "MAX_TREE_WALL_S": "900",
    "SUBAGENT_MAX_FANOUT": "6",
    "SUBAGENT_MAX_STEPS": "4",
    "USE_SELF_CHECK_CRITIC": "0",
    "USE_TASKS": "0",                    # 本实验测树引擎,不测任务底座
    "LOOP_THOUGHTS": "1",
}

ANSWER_CONTRACT = (
    "\n\n【答案格式(务必遵守)】最后用一个 JSON 代码块给出结构化结果:"
    '{"video_ids": ["..."], "count": 数量, '
    '"per_video": {"video_id": {"category": "大类", "start_ts": 秒, "end_ts": 秒, '
    '"evidence": "画面证据一句话"}}}。'
    "T1 类问题 per_video 只需 category;找不到就给空数组并说明。JSON 之外可以正常写说明文字。"
)


def _load_bank(split: str) -> dict:
    p = ROOT / "evals" / f"longhorizon_bank.{split}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _set_env(arm: str, item: dict, rep: int):
    """按臂设置环境 + 缓存命名空间隔离(红队 C3:三臂全冷,成本才叫全口径)。"""
    for k, v in COMMON_ENV.items():
        os.environ[k] = v
    for k, v in ARMS[arm].items():
        os.environ[k] = v
    os.environ["ANALYZE_CACHE_NS"] = f"gate-{arm}-r{rep}"
    # T2 防 SQL 捷径:实验快照对本题谓词做 ts 掩码(env 供 pipeline 侧读;
    # 若 pipeline 未实现掩码则由 gold 的 ts_mask 标记提示人工核对,不静默当已掩码)
    os.environ["GATE_TS_MASK_PREDICATE"] = item.get("predicate", "") if item.get("ts_mask") else ""


def _reload_config():
    """env 改了要让 config 重新读(模块级常量)。"""
    import importlib
    from pipeline import config
    importlib.reload(config)
    for mod in ("pipeline.loop_driver", "pipeline.node_executor", "pipeline.subagents"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


def run_one(item: dict, arm: str, rep: int, owner: str = "gate-eval") -> dict:
    """跑一题一臂一次。返回 {answer, cost_usd, wall_s, llm_calls, terminated, ...}。"""
    _set_env(arm, item, rep)
    _reload_config()
    from pipeline import loop_driver
    from pipeline.agentops import trace as T
    from pipeline.agentops import usage
    from pipeline.mcp_client import get_schema

    usage.reset_usage()
    t0 = time.perf_counter()
    trace = T.Trace(quiet=True)
    rec = {"id": item["id"], "tier": item["tier"], "arm": arm, "rep": rep}
    try:
        schema = get_schema()
        lo = loop_driver.run_query_loop(
            item["question"] + ANSWER_CONTRACT, schema=schema, replay_context=None,
            sandbox=None, trace=trace, session_id=None, owner=owner)
        rec["answer"] = lo.answer or ""
        rec["terminated"] = lo.terminated
        rec["steps"] = lo.steps
        rec["tools"] = [s.get("tool") for s in (lo.trace or [])]
        # 大脑原话(思考摘要):验尸时要能看出"它为什么这么决定"—— 尤其"为什么不拆"
        rec["turns"] = [{"step": t.get("step"), "brain": (t.get("brain") or "")[:600]}
                        for t in (getattr(lo, "turns", None) or [])][:6]
        rec["spawned"] = "spawn_agents" in (rec["tools"] or [])
    except Exception as e:
        rec["answer"] = ""
        rec["error"] = repr(e)[:300]
        rec["terminated"] = "error"
    u = usage.summarize()
    rec["cost_usd"] = round(float(u.get("cost_usd") or 0.0), 6)
    rec["tokens"] = u.get("tokens_total", 0)
    rec["llm_calls"] = u.get("calls", 0)
    rec["wall_s"] = round(time.perf_counter() - t0, 1)
    rec["quota_hit"] = "已达本请求视频分析上限" in (rec.get("answer") or "")
    rec["guard_trip"] = "触发成本护栏" in (rec.get("answer") or "")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=("dev", "holdout"))
    ap.add_argument("--arms", default="A,B,C")
    ap.add_argument("--n", type=int, default=1, help="每题每臂 rep 数")
    ap.add_argument("--ids", default=None, help="只跑这些题(逗号分隔)")
    ap.add_argument("--budget", type=float, default=42.0, help="本次预算(硬顶)")
    ap.add_argument("--half-stop", type=float, default=None, help="半程闸金额")
    ap.add_argument("--dry-run", action="store_true", help="试跑:每臂只跑前 N 题并出闸1 判定")
    ap.add_argument("--dry-n", type=int, default=3)
    ap.add_argument("--tag", default=None, help="输出文件名标签")
    a = ap.parse_args()

    bank = _load_bank(a.split)
    items = [i for i in bank["items"] if i["tier"] != "PROBE" or not a.dry_run]
    if a.ids:
        want = set(a.ids.split(","))
        items = [i for i in items if i["id"] in want]
    if a.dry_run:
        items = items[:a.dry_n]
    arms = [x.strip() for x in a.arms.split(",") if x.strip() in ARMS]
    half_stop = a.half_stop if a.half_stop is not None else a.budget * 0.6

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = a.tag or (("dry-" if a.dry_run else "") + a.split)
    out_path = OUT_DIR / f"gate-{tag}.jsonl"
    fh = out_path.open("a", encoding="utf-8")           # 【每题落盘】,被杀也不白花

    spent = 0.0
    done = 0
    total = len(items) * len(arms) * a.n
    errors = 0
    per_item_cost = []
    print(f"[gate] split={a.split} arms={arms} n={a.n} 题数={len(items)} 计划 {total} 次 "
          f"预算 ${a.budget:.2f}(半程闸 ${half_stop:.2f})", flush=True)
    stopped = None
    for rep in range(1, a.n + 1):
        for item in items:
            for arm in arms:
                if spent >= a.budget:                    # 闸3 硬顶
                    stopped = f"硬顶 ${a.budget:.2f}"
                    break
                rec = run_one(item, arm, rep)
                spent += rec["cost_usd"]
                done += 1
                per_item_cost.append(rec["cost_usd"])
                if rec.get("error"):
                    errors += 1
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  [{done}/{total}] {item['id'][:28]:28s} arm={arm} "
                      f"${rec['cost_usd']:.4f} {rec['wall_s']:.0f}s "
                      f"{'ERR' if rec.get('error') else rec['terminated']}"
                      f"{' QUOTA' if rec['quota_hit'] else ''}"
                      f"{' GUARD' if rec['guard_trip'] else ''} 累计 ${spent:.3f}", flush=True)
                # 闸2 半程
                if spent >= half_stop and done / max(1, total) < 0.5:
                    stopped = f"半程闸:花到 ${spent:.2f} 完成率仅 {done/total:.0%}"
                    break
            if stopped:
                break
        if stopped:
            break
    fh.close()

    med = statistics.median(per_item_cost) if per_item_cost else 0.0
    summary = {"split": a.split, "arms": arms, "n": a.n, "planned": total, "done": done,
               "spent_usd": round(spent, 4), "median_cost": round(med, 4),
               "error_rate": round(errors / max(1, done), 3), "stopped": stopped,
               "out": str(out_path)}
    print("\n[gate] " + json.dumps(summary, ensure_ascii=False), flush=True)
    if a.dry_run:                                        # 闸1 判定
        verdict = []
        if med > 2 * (float(os.environ.get("GATE_EXPECT_COST", "0.25"))):
            verdict.append(f"单题成本中位 ${med:.3f} > 2× 预估 → 停下重定参数")
        if summary["error_rate"] > 0.2:
            verdict.append(f"报错率 {summary['error_rate']:.0%} > 20% → 停下修")
        if any(json.loads(l).get("quota_hit") for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()):
            verdict.append("有臂触发 analyze 配额 → 停下重定参数(红队 C1)")
        print("[闸1] " + (";".join(verdict) if verdict else "通过,可进主跑"), flush=True)
    (OUT_DIR / f"gate-{tag}.summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
