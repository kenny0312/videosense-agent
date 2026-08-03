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
    # v3 主跑用的是 4。实测那批 14 个子 agent 里 4~5 步的 10 个无一收敛 → 基线提到 6
    # (见 config.SUBAGENT_MAX_STEPS 注释)。【重测时这里跟着改,与 v3 数据不可直接比】。
    "SUBAGENT_MAX_STEPS": "6",
    "USE_SELF_CHECK_CRITIC": "0",
    "USE_TASKS": "0",                    # 本实验测树引擎,不测任务底座
    "LOOP_THOUGHTS": "1",
}

# 答案契约按【产品自己的交付方式】设计,不跟它对着干:VS 的规则是"绝不把内部 id 抄给
# 用户看"(scrub_ids 会把答案里的 id 洗成"第 N 个"),交付视频靠 show_video。
# 试跑实测:要求"输出 video_ids 的 JSON"会被这条规则洗掉 → 每一臂 set_f1 恒为 0,
# 量的是"洗得干不干净"。所以判分口径 = show_video 这个【交付动作】的台账。
ANSWER_CONTRACT = (
    "\n\n【交付要求(务必遵守)】把你【最终认定符合条件】的视频,用 show_video 一次性摆出来"
    "(data_result_id 指向你的检索结果,或直接给 video_ids)—— 这是交付动作,只摆你确认的,"
    "别把探查过程中看过的候选都摆上。然后用文字说明:总共几个、每个属于哪个大类"
    "(受控词表),T2 类问题还要说每个视频里目标动作的时间段与画面证据。"
    "一个都没有就明确说没有,别硬凑。"
    # 【受控词表在库里,不在这段话里】。原来只说"受控词表"却从不告诉它是哪 26 个,
    # 而判分要求预测值同时命中词表【和】该视频的 gold 类目集 —— 等于在考一套没公布的闭集,
    # T1 那 40% 权重再怎么修取数侧也接近 0。
    # 但也【不能】把 26 个词贴进来:那会把任务从"找出分类法"变成"抄清单"。
    # 实测 `SELECT label FROM categories` 精确返回那 26 个(与题库词表 26/26 命中),
    # 所以只指路 —— 考的是"知道去查",这是真能力。
    "\n【大类取值】必须来自库里的受控词表:`SELECT label FROM categories`(26 个),"
    "别自造词、别用谓词当大类。"
)

# B0-4:大类与时间段【也要走交付台账】,不能只落在自然语言里 —— 判分从 videos[] 侧信道取。
# 这一段只在 show_video 真的声明了 items 参数时才追加(见 answer_contract()):
# 产品侧还没合进来时多说一句 items,只会让模型发出一个被 schema 拒收的参数,
# 把好端端的跑次变成 terminated=error —— 那正是 B0-3 刚修掉的那种"数据"。
_ITEMS_CLAUSE = (
    "调 show_video 时用 items 参数逐个交付:items=[{\"video_id\": ..., \"category\": <受控词表里的大类>,"
    " \"start_ts\": <目标动作起始秒>, \"end_ts\": <结束秒>}, ...]。"
    "大类必须来自受控词表;T2 类问题的 start_ts/end_ts 必须是你【看画面】定出来的目标动作时段,"
    "不是整段视频的 0 到结尾。文字说明照旧写,items 是给交付台账用的,两者都要。"
)


def answer_contract() -> str:
    """契约随【工具的真实签名】走 —— 有 items 就用,没有就退回今天的行为。

    为什么要探一下而不是写死:B0-4 的产品侧改动(`show_video(items=...)`)与本文件
    分属两个代理,合入时序不保证。写死会在合入之前把每一次跑都推成参数校验失败。
    """
    try:
        from pipeline.node_specs import SPECS
        props = ((SPECS["show_video"].parameters or {}).get("properties") or {})
    except Exception:                                    # 探不到就当没有,绝不因探测本身崩掉跑机
        props = {}
    return ANSWER_CONTRACT + (("\n" + _ITEMS_CLAUSE) if "items" in props else "")


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
    # T2 防 SQL 捷径:把本题谓词的时间戳在【返回给 agent 的行上】置空(mcp_client._mask_ts),
    # 逼它真去看视频。试跑实测:不掩码时大脑 12 次 SQL、一个视频不看就把时间戳抄出来了 ——
    # 号称考感知的题变成考 SQL。gold 走库外预标,不受掩码影响。
    os.environ["GATE_TS_MASK_PREDICATE"] = item.get("predicate", "") if item.get("ts_mask") else ""


def _reload_config():
    """env 改了要让 config 重新读(模块级常量)。"""
    import importlib
    from pipeline import config
    importlib.reload(config)
    for mod in ("pipeline.loop_driver", "pipeline.node_executor", "pipeline.subagents"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])


def _surfaced_video_ids(lo) -> list:
    """判分口径 = 【交付动作】摆出来的视频,不是"agent 碰过的一切"。
    实测:把所有工具结果里的 video_id 都算上,一道 gold=3 的题会抓到 37 条(SQL 探查的
    中间候选全进来了),precision 崩掉。既定口径(evals/scorers)是"答案 + show_* 参数",
    这里对应 show_video 的 videos 侧信道 —— 那才是 agent 主动摆给用户看的。"""
    out, seen = [], set()

    def add(v):
        v = str(v or "")
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    for cid, er in (getattr(lo, "results", None) or {}).items():
        if not getattr(er, "ok", False):
            continue
        for v in (getattr(er, "videos", None) or []):        # show_video 的交付侧信道
            if isinstance(v, dict):
                add(v.get("video_id"))
    return out


_LEDGER_KEYS = ("video_id", "title", "category", "start_ts", "end_ts")


def _surfaced_meta(lo) -> list:
    """B0-4:交付台账的【全字段】版本 —— 归类与定位的取数来源。

    只是把同一条侧信道(`NodeResult.videos`)里已经有的字段原样抄下来,**不改 rows/preview
    形状**(v2.2 R4 禁改区边界),也不新造侧信道。`category` 是产品侧 `show_video(items=...)`
    随后新增的字段;它还没合进来时这里读到 None —— 有就用、没有就退回今天的行为。

    【按出现顺序原样留档,不去重】:同一视频被摆两次(先交付、后补时段)是真实发生的事,
    合并规则属于判分口径,放在 `longhorizon_score.per_video_from_ledger` 里一处实现,
    跑机只负责如实记账。所以本字段长度可能 > `surfaced`(那个是去重后的 id 列表)。
    """
    out = []
    for cid, er in (getattr(lo, "results", None) or {}).items():
        if not getattr(er, "ok", False):
            continue
        for v in (getattr(er, "videos", None) or []):
            if isinstance(v, dict) and v.get("video_id"):
                row = {k: v.get(k) for k in _LEDGER_KEYS}
                row["video_id"] = str(row["video_id"])
                out.append(row)
    return out


_SPAWN_LOG: list = []          # 本次 run_one 内 spawn_agents 的【真实参数 + 真实返回】


def _install_spawn_capture():
    """录下主脑到底【怎么分的活】。

    原来的跑机只存工具名(`["...","spawn_agents",...]`),验尸时看得见"拆了",
    看不见"拆成了什么" —— 每段 instruction 是主脑现场写的自由文本,那才是拆分质量
    的全部内容。这里包一层 run_fanout,把入参(tasks)和出参(各子 agent 的结论)原样留档。

    必须在 `_reload_config()` 【之后】装:那个函数会 reload pipeline.subagents,把补丁刷掉。
    node_executor 是函数内 `from pipeline import subagents` 再 `subagents.run_fanout(...)`,
    所以改模块属性能生效。深度 2 的嵌套 spawn 也会走这里,自然一并录到。
    """
    from pipeline import subagents
    if getattr(subagents.run_fanout, "_captured", False):
        return
    orig = subagents.run_fanout

    def wrapped(tasks, **kw):
        rec = {"tasks": tasks, "results": None, "error": None}
        _SPAWN_LOG.append(rec)                   # 先登记:抛异常的那次也要留痕
        out = orig(tasks, **kw)
        rec["results"] = out
        return out

    wrapped._captured = True
    subagents.run_fanout = wrapped


def run_one(item: dict, arm: str, rep: int, owner: str = "gate-eval") -> dict:
    """跑一题一臂一次。返回 {answer, cost_usd, wall_s, llm_calls, terminated, ...}。"""
    _set_env(arm, item, rep)
    _reload_config()
    _SPAWN_LOG.clear()
    _install_spawn_capture()
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
            item["question"] + answer_contract(), schema=schema, replay_context=None,
            sandbox=None, trace=trace, session_id=None, owner=owner)
        rec["answer"] = lo.answer or ""
        rec["terminated"] = lo.terminated
        rec["steps"] = lo.steps
        rec["tools"] = [s.get("tool") for s in (lo.trace or [])]
        # 大脑原话(思考摘要):验尸时要能看出"它为什么这么决定"。
        # 【全量记录,不截断】—— 第一版只存前 6 轮 × 600 字,结果验尸时发现关键决策
        # (拆分发生在第 5 步、跑道提醒发在第 12 步)全在截断之外,看不到。
        rec["turns"] = [{"step": t.get("step"), "brain": t.get("brain") or "",
                         "nudge": t.get("nudge") or ""}
                        for t in (getattr(lo, "turns", None) or [])]
        rec["spawned"] = "spawn_agents" in (rec["tools"] or [])
        # 【判分必须从工具结果取 video_id,不能从答案文本抠】:VS 的 scrub_ids 按产品规则
        # 把答案里的内部 id 全洗成"第 N 个"(绝不把 id 抄给用户看)—— 试跑实测,不这么做
        # 每一臂的 set_f1 恒为 0,整个实验测的是"洗得干不干净"。
        rec["surfaced"] = _surfaced_video_ids(lo)
        # B0-4:归类/定位也从台账取(答案 JSON 那条路结构上就不存在 —— 契约要的是自然语言)。
        rec["surfaced_meta"] = _surfaced_meta(lo)
    except Exception as e:
        rec["answer"] = ""
        rec["error"] = repr(e)[:300]
        rec["terminated"] = "error"
    # 拆分的【真实分活内容】—— 主脑写给每个子 agent 的 instruction 原文 + 各自交回的结论。
    # default=str 兜住模型可能塞进来的非 JSON 类型(坏输入本身就是要看的证据)。
    rec["spawn_calls"] = json.loads(json.dumps(_SPAWN_LOG, ensure_ascii=False, default=str))
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
    ap.add_argument("--rep-start", type=int, default=1,
                    help="从第几个 rep 开始(补跑用;rep 号进缓存命名空间,保证与前一轮全冷)")
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
    for rep in range(a.rep_start, a.rep_start + a.n):
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
