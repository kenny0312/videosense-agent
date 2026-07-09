"""评测跑批器：跑每道题 n 次 -> 判分 -> 给结论 -> 归档 + 报告 + 仪表盘。

命令行：
    python -m evals.runner                 # 脚本车道（免费）：验证评测机器本身
    python -m evals.runner --compare       # 脚本车道演示"变差·打回"
    python -m evals.runner --live           # 真跑（真 Gemini，花 token；默认每题 3 次）
    python -m evals.runner --list          # 只看数据集统计

几个口径（人话）：
- 每道题的记录带全信息：题目原文、期望、答案、工具链、花费 —— 看失败不用翻别的文件。
- "环境故障"（沙箱没起、断网这类）单独记，不算进通过率 —— 别把机器的锅扣在模型头上。
- 和上一次真跑比结论时，只看"变了的题"（新挂/新过），并算一下这种变化
  碰运气也会出现的概率（p 值）——p 小才敢说是真变化。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter

from evals import report, scorers
from evals.world import LiveWorld, ScriptedWorld, live_preflight

_GUARD = 0.15        # 某个方面掉超过 15 个百分点，值得在原因里点名
_P_REAL = 0.05       # 变化碰运气出现的概率低于 5%，才敢说"真变了"


# ── 读题 ────────────────────────────────────────────────────────────
def _strip_jsonc(text: str) -> str:
    """去掉整行 // 注释（本项目 .jsonc 只用整行注释）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))


def load_tasks(path: str) -> list[dict]:
    """加载题目：目录则递归读 *.jsonc / *.json（单对象）+ *.jsonl（一行一题）。
    GD-0 防脚枪：文件名以 candidates 开头的一律跳过 —— 那是挖题脚本的【半成品】
    （金标还是 TODO），人工补完金标挪正式文件后才算题。"""
    if os.path.isdir(path):
        files = []
        for root, _dirs, fnames in os.walk(path):
            files += [os.path.join(root, fn) for fn in fnames
                      if fn.endswith((".jsonc", ".json", ".jsonl"))
                      and not fn.startswith("candidates")]
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


def filter_tasks(tasks: list[dict], ids: "str | list[str] | None") -> list[dict]:
    """GD-0 子集选择（GEPA 候选评估只重跑受影响题）。ids = 逗号分隔串或列表;
    支持前缀通配 'retrieval-*'。没匹配到的 id 报错(防手滑静默跑空)。"""
    if not ids:
        return tasks
    want = [s.strip() for s in (ids.split(",") if isinstance(ids, str) else ids) if s.strip()]
    out, matched = [], set()
    for t in tasks:
        for w in want:
            if t["id"] == w or (w.endswith("*") and t["id"].startswith(w[:-1])):
                out.append(t)
                matched.add(w)
                break
    missing = [w for w in want if w not in matched]
    if missing:
        raise SystemExit(f"--ids 没匹配到任何题：{', '.join(missing)}")
    return out


# ── 判分分发 ─────────────────────────────────────────────────────────
def _score_one_check(name: str, cfg: dict, res, aliases: dict | None):
    """跑一项检查。res 只需要有 answer/trace/ledger 三样。"""
    if name == "honesty":
        return scorers.refusal_ok(res.answer, cfg)
    if name == "retrieval":
        # 查全看"交付面"（答案 + show_* 侧信道，含结果行）；
        # 数"甩了多少无关视频"只看 agent 主动亮出的部分（结果回显不算它甩的）
        return scorers.retrieval_score(scorers.surface_blob(res), cfg, aliases,
                                       own_blob=scorers.surface_blob_own(res))
    if name == "timestamp":
        return scorers.timestamp_iou(res.answer, cfg)
    if name == "count":
        return scorers.answer_count(res.answer, cfg)
    if name == "entity_match":
        return scorers.entity_match(res.answer, cfg)
    if name == "no_id_leak":
        return scorers.no_id_leak(res.answer, cfg)
    if name == "identity":
        return scorers.no_provider_leak(res.answer, cfg)
    if name == "safety":
        return scorers.refusal_ok(res.answer, {"expect_refusal": cfg.get("expect_refusal", True)})
    return None


def dispatch_scorers(task: dict, res, aliases: dict | None = None) -> dict:
    """把一道题适用的判分器都跑一遍，输出 {判分器名 -> 分}。
    带 "turn" 字段的检查属于多轮某一轮，这里跳过（score_multi 负责）。"""
    ec = task["evaluation_criteria"]
    aliases = aliases if aliases is not None else _titles()
    s: dict = {}
    if ec.get("required_actions") is not None:
        s["required_actions"] = scorers.toolseq_match(res.trace, ec["required_actions"])
    if ec.get("no_call_expected"):
        # "别去干那件不该干的事"：只读地瞥一眼库（sql/语义/看画面）不算，
        # 真去交付/联网/写东西（show_*/web_search/python/plot/记忆/子agent）才算越界。
        overreach = {"show_video", "show_table", "web_search", "plot", "python",
                     "spawn_agents", "update_memory"}
        did = any(st.get("tool") in overreach for st in res.trace or [])
        s["no_call"] = 0.0 if did else 1.0
    if ec.get("forbidden_actions"):
        # 用户明说别做的事（比如"别放视频"）——做了任何一条就 0 分
        hit_any = any(scorers.toolseq_match(res.trace, [req]) == 1.0
                      for req in ec["forbidden_actions"])
        s["no_forbidden"] = 0.0 if hit_any else 1.0
    for name, cfg in ec.get("output_checks", {}).items():
        if isinstance(cfg, dict) and cfg.get("turn"):
            continue
        v = _score_one_check(name, cfg, res, aliases)
        if v is not None:
            s[name] = v
    return s


# ── 别名表 ───────────────────────────────────────────────────────────
_MUTATING_ACTIONS = {"upload_video", "enrich_video", "paste_image"}


def _has_user_actions(task: dict) -> bool:
    """脚本里带【改共享状态】动作（上传/入库/贴图）的题。动作字段兼容 tool/type 两种写法。"""
    for step in task.get("user", {}).get("script", []) or []:
        act = step.get("action") or {}
        if (act.get("tool") or act.get("type")) in _MUTATING_ACTIONS:
            return True
    return "state_assertions" in (task.get("reward_basis") or [])


def _titles() -> dict:
    """每个视频的可区分别名：英文标题 + 时长数字（验指代解析用）。"""
    from repl._mock_db import VIDEOS

    return {v[0]: [v[1], str(int(v[3]))] for v in VIDEOS}


def _task_aliases(task: dict) -> dict:
    """这道题的"视频别名表"：假片库的 标题+时长，再加上用户中途上传的视频。"""
    aliases = dict(_titles())
    for step in task.get("user", {}).get("script", []) or []:
        act = step.get("action") or {}
        vid = act.get("video_id")
        if vid:
            aliases[vid] = [act.get("title", "")] + list(act.get("activities", []) or [])
    return aliases


# ── 多轮判分 ─────────────────────────────────────────────────────────
def score_multi(task: dict, turns, world_state: dict | None = None) -> dict:
    """多轮判分（纯函数，离线可测）。turns = TurnRecord 列表（who/text/trace/ledger）。
    - 不带 turn 字段的检查：判整场工具链 + 最后一轮的交付面。
    - 带 turn 字段的检查：判对应那一轮（比如"第 1 轮该说没有滑冰"）。
    - state_assertions：判用户动作改的共享状态真落了没。"""
    from types import SimpleNamespace

    aliases = _task_aliases(task)
    agents = [t for t in turns if t.who == "agent"]
    turn_objs = [SimpleNamespace(answer=t.text, trace=t.trace, ledger=t.ledger) for t in agents]
    blobs = [scorers.surface_blob(o) for o in turn_objs]
    all_trace = [step for t in agents for step in t.trace]
    final = SimpleNamespace(answer=agents[-1].text if agents else "",
                            trace=all_trace,
                            ledger={k: v for t in agents for k, v in (t.ledger or {}).items()})
    ec = task["evaluation_criteria"]
    s = dispatch_scorers(task, final, aliases=aliases)
    for name, cfg in ec.get("output_checks", {}).items():          # 带 turn 的检查：判那一轮
        if isinstance(cfg, dict) and cfg.get("turn"):
            idx = int(cfg["turn"]) - 1
            res = turn_objs[idx] if 0 <= idx < len(turn_objs) else SimpleNamespace(answer="", trace=[], ledger={})
            v = _score_one_check(name, cfg, res, aliases)
            if v is not None:
                s[name] = min(s.get(name, 1.0), v)                 # 同名检查每一轮都要过
    if ec.get("jga_slots"):
        # 指代解析（video_ids）额外看工具调用参数：去查了那条视频=解析对了，
        # 不逼 agent 在答案里念 id/标题（产品规则本来就不让 id 进答案文本）
        resolve = [scorers.resolve_blob(o) for o in turn_objs]
        s["jga"] = scorers.score_jga(blobs, ec["jga_slots"], titles=aliases,
                                     resolve_blobs=resolve)
    if ec.get("state_assertions"):
        s["state_assertions"] = scorers.score_state_assertions(ec["state_assertions"], world_state or {})
    return s


# ── 跑一道题（含丰富记录）────────────────────────────────────────────
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _tools_of(trace, ledger=None, limit: int = 160, out_limit: int = 240) -> list:
    """工具链落盘：工具名+参数（截断）+【每步成败与世界返回了什么】（GD-0：反思器要能看到
    "SQL 明明返回了 v009 但 agent 没用"这类事实，只有名字和参数不够）。fail-open：没 ledger 就略。"""
    led = ledger or {}
    out = []
    for st in trace or []:
        args = json.dumps(st.get("inputs", {}), ensure_ascii=False, default=str)
        row = {"tool": st.get("tool"), "args": args[:limit], "ok": st.get("ok")}
        er = led.get(st.get("cid"))
        if er is not None:
            try:
                row["out"] = (json.dumps(getattr(er, "preview", None), ensure_ascii=False,
                                         default=str)[:out_limit]
                              if getattr(er, "ok", False)
                              else str(getattr(er, "stderr", ""))[:out_limit])
            except Exception:
                pass
        out.append(row)
    return out


def _make_record(task, n, successes, per_dim, last, first_fail, cost) -> dict:
    """一道题的完整记录：看这一条就能下钻，不用翻别的文件。"""
    return {
        "id": task["id"],
        "pinned": task.get("pinned", False),
        "dims": task.get("dims", []),
        "kind": task.get("kind", "single"),
        "status": "ok",
        "n": n,
        "successes": successes,
        "passed": successes == n,
        "pass_k": {str(k): scorers.passk(successes, n, k) for k in (1, 3, 5)},
        "scores": {d: _mean(v) for d, v in per_dim.items()},
        "question": task.get("user_query") or " / ".join(
            s.get("utterance", "") for s in task.get("user", {}).get("script", []) or []),
        "expect": task.get("evaluation_criteria", {}),
        "grounding_note": task.get("grounding_note", ""),
        "answer": (last or {}).get("answer"),
        "tools": (last or {}).get("tools", []),
        "turns": (last or {}).get("turns"),  # 多轮题的逐轮明细（单轮题为 None）
        "first_fail": first_fail,          # 第一次挂掉的那回：答案+工具链（最该看的样本）
        "cost": cost,
    }


def _infra_record(task, n, err) -> dict:
    """环境故障（沙箱没起/断网等）：单独记，不算进通过率。"""
    return {
        "id": task["id"], "pinned": task.get("pinned", False), "dims": task.get("dims", []),
        "kind": task.get("kind", "single"), "status": "infra_error",
        "n": n or 1, "successes": 0, "passed": False,
        "pass_k": {"1": 0.0, "3": None, "5": None}, "scores": {},
        "question": task.get("user_query", ""), "expect": {}, "grounding_note": "",
        "answer": f"[环境故障] {err}", "tools": [], "first_fail": None,
        "cost": {"llm_calls": 0, "analyze_calls": 0, "wall_ms": 0, "tokens": 0, "cost_usd": 0.0},
    }


def _is_infra_error(e: Exception) -> bool:
    s = str(e)
    return (isinstance(e, (ConnectionError, ConnectionRefusedError))
            or "urlopen" in s or "WinError 10061" in s or "Connection refused" in s
            or "getaddrinfo" in s)


def run_case(task: dict, script=None, tool_results=None, n: int | None = None,
             live: bool = False, owner: str = "eval") -> dict:
    n = n or task.get("n_rollouts", 3)
    successes = 0
    per_dim: dict = {}
    last = None
    first_fail = None
    cost = {"llm_calls": 0, "analyze_calls": 0, "wall_ms": 0, "tokens": 0, "cost_usd": 0.0}
    steps = task.get("max_steps", 16)
    for _ in range(n):
        t0 = time.perf_counter()
        if live:                                # Mode B：真 Gemini
            from pipeline import usage as _usage
            _usage.reset_usage()                # GD-0：按 rollout 记 token/$（GEPA 预算控制用）
            res = LiveWorld(owner=owner).run(task["user_query"], max_steps=steps)
            _u = _usage.summarize()
            cost["tokens"] += _u.get("tokens_total", 0) or 0
            cost["cost_usd"] = round(cost["cost_usd"] + (_u.get("cost_usd", 0) or 0), 6)
        else:                                   # Mode A：脚本车道
            res = ScriptedWorld(script, tool_results or {}).run(task["user_query"], max_steps=steps)
        cost["wall_ms"] += round((time.perf_counter() - t0) * 1000)
        cost["llm_calls"] += getattr(res, "llm_calls", 0)
        cost["analyze_calls"] += sum(1 for st in res.trace if st.get("tool") == "analyze_video")
        sc = dispatch_scorers(task, res)
        ok = scorers.case_pass(sc, task["reward_basis"])
        successes += int(ok)
        led = getattr(res, "ledger", None)
        if not ok and first_fail is None:
            first_fail = {"answer": res.answer, "tools": _tools_of(res.trace, led), "scores": sc}
        for d, v in sc.items():
            per_dim.setdefault(d, []).append(v)
        last = {"answer": res.answer, "tools": _tools_of(res.trace, led)}
    return _make_record(task, n, successes, per_dim, last, first_fail, cost)


def run_case_multi(task: dict, n: int | None = None, owner: str = "eval") -> dict:
    from evals.session import DualControlSession

    n = n or task.get("n_rollouts", 3)
    successes = 0
    per_dim: dict = {}
    last = None
    first_fail = None
    cost = {"llm_calls": 0, "analyze_calls": 0, "wall_ms": 0, "tokens": 0, "cost_usd": 0.0}
    for _ in range(n):
        t0 = time.perf_counter()
        from pipeline import usage as _usage
        _usage.reset_usage()                     # GD-0：按 rollout 记 token/$
        out = DualControlSession(task, owner=owner).run()
        _u = _usage.summarize()
        cost["tokens"] += _u.get("tokens_total", 0) or 0
        cost["cost_usd"] = round(cost["cost_usd"] + (_u.get("cost_usd", 0) or 0), 6)
        cost["wall_ms"] += round((time.perf_counter() - t0) * 1000)
        agents = [t for t in out["turns"] if t.who == "agent"]
        cost["llm_calls"] += sum(getattr(t, "llm_calls", 0) for t in agents)
        cost["analyze_calls"] += sum(1 for t in agents for st in t.trace
                                     if st.get("tool") == "analyze_video")
        sc = score_multi(task, out["turns"], world_state=out.get("world_state"))
        ok = scorers.case_pass(sc, task["reward_basis"])
        successes += int(ok)
        snapshot = {"answer": agents[-1].text if agents else None,
                    "tools": [x for t in agents
                              for x in _tools_of(t.trace, getattr(t, "ledger", None))],
                    # 逐轮明细：多轮题失败归因的第一现场（谁在第几轮说了什么、调了什么）
                    "turns": [{"who": t.who, "text": (t.text or "")[:400],
                               "tools": _tools_of(getattr(t, "trace", None) or [],
                                                  getattr(t, "ledger", None), limit=110)[:8]}
                              for t in out["turns"]]}
        if not ok and first_fail is None:
            first_fail = {**snapshot, "scores": sc}
        for d, v in sc.items():
            per_dim.setdefault(d, []).append(v)
        last = snapshot
    rec = _make_record(task, n, successes, per_dim, last, first_fail, cost)
    return rec


def n_for(task: dict, n: int | None) -> int:
    """一道题该跑几次：显式 --n 全体照办（冒烟用）；
    默认档：普通题 3 次、必过题 5 次（红线要建立在更多证据上）。"""
    if n:
        return n
    return 5 if task.get("pinned") else 3


def run_suite(tasks: list[dict], policies: dict | None = None, tool_results: dict | None = None,
              live: bool = False, n: int | None = None) -> list[dict]:
    if live:                                  # Mode B：真 Gemini。单轮 + 多轮；带世界动作的看接没接
        todo, skipped = split_live_tasks(tasks)
        out = []
        for i, (t, is_multi) in enumerate(todo, 1):
            tn = n_for(t, n)
            try:
                r = run_case_multi(t, n=tn) if is_multi else run_case(t, live=True, n=tn)
            except Exception as e:            # 单题崩溃不拖垮整场
                if _is_infra_error(e):
                    r = _infra_record(t, tn, e)
                else:
                    r = _infra_record(t, tn, e)
                    r["status"] = "crash"     # 代码崩溃：计分且算没过（必过题崩=失守）
                    r["answer"] = f"[代码崩溃] {e}"
            out.append(r)
            tag = "多轮" if is_multi else "单轮"
            mark = {"ok": "过" if r["passed"] else "没过", "infra_error": "环境故障",
                    "crash": "崩溃"}[r["status"]] if r["status"] != "ok" else ("过" if r["passed"] else "没过")
            print(f"[{i}/{len(todo)}] ({tag}) {t['id']:<36} {mark}", flush=True)
        if skipped:
            print(f"跳过 {len(skipped)} 道要改共享状态才能判分的题（世界动作待接线）：")
            print("  " + "、".join(skipped), flush=True)
        return out
    tool_results = tool_results or {}         # 脚本车道：只跑有 fixture 策略的题
    out = []
    for t in tasks:
        if policies is not None and t["id"] not in policies:
            continue
        out.append(run_case(t, policies[t["id"]], tool_results.get(t["id"], {}), n=n_for(t, n)))
    return out


def split_live_tasks(tasks: list[dict]):
    """真跑车道分拣。世界动作（上传/入库/贴图）已接进假后端，全部题都能跑。"""
    singles = [(t, False) for t in tasks if t.get("kind") != "multi"]
    multis = [(t, True) for t in tasks if t.get("kind") == "multi"]
    return singles + multis, []


# ── 结论 ────────────────────────────────────────────────────────────
def _scored(results):
    """真正计分的题。环境故障（沙箱没起/断网）不算——那是机器的锅；
    但【代码崩溃】算没过——必过题崩了同样是失守，不能从门禁里漏掉。"""
    return [r for r in results if r.get("status", "ok") != "infra_error"]


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


def baseline_drop_reason(prev: dict | None, n_label, scorer_fp: str) -> str | None:
    """上一次跑还能不能当对比基线。尺子变了硬比会把"尺子的变化"错算成"agent 的变化"
    （历史教训：修判分器带来的 +6 个百分点，差点被当成 agent 变好）。"""
    if not prev:
        return None
    pm = prev.get("meta") or {}
    if str(pm.get("n")) != str(n_label):
        return f"每题次数档位不同（上次 {pm.get('n')}，这次 {n_label}）"
    if pm.get("scorer_fp") and pm.get("scorer_fp") != scorer_fp:
        return "判分器或题库动过（尺子指纹不同）"
    return None


def classify(cur: list[dict], base: list[dict] | None = None) -> dict:
    """给整场下结论。有上一次记录时按"同一道题"配对比较，只看变了的题；
    并算 p 值（这种变化碰运气也会出现的概率），p 小才说真变了。"""
    scored = _scored(cur)
    infra = [r["id"] for r in cur if r.get("status") == "infra_error"]
    reasons = []
    if infra:
        reasons.append(f"环境故障 {len(infra)} 题未计分（{'、'.join(infra[:4])}…）"
                       if len(infra) > 4 else f"环境故障 {len(infra)} 题未计分：" + "、".join(infra))

    if base is None:
        pinned_fail = [r["id"] for r in scored if r["pinned"] and not r["passed"]]
        fails = sum(1 for r in scored if not r["passed"])
        if pinned_fail:
            reasons.insert(0, "必过题没过：" + "、".join(pinned_fail))
            return {"label": "变差 · 打回", "kind": "bad", "reasons": reasons}
        if fails == 0:
            return {"label": "全部通过 · 建立基线", "kind": "ok", "reasons": reasons}
        reasons.insert(0, f"没过 {fails}/{len(scored)} 题（首次记录，无对比基准）")
        return {"label": "已出分 · 建立基线", "kind": "neutral", "reasons": reasons}

    base_scored = {r["id"]: r for r in _scored(base)}
    paired = [(r, base_scored[r["id"]]) for r in scored if r["id"] in base_scored]
    new_fail = [r["id"] for r, b in paired if b["passed"] and not r["passed"]]
    new_pass = [r["id"] for r, b in paired if not b["passed"] and r["passed"]]
    pinned_new_fail = [r["id"] for r, b in paired
                       if r["pinned"] and b["passed"] and not r["passed"]]
    p = scorers.flip_significance(len(new_fail), len(new_pass))

    if new_fail:
        reasons.append(f"新挂 {len(new_fail)} 题：" + "、".join(new_fail[:6]))
    if new_pass:
        reasons.append(f"新过 {len(new_pass)} 题：" + "、".join(new_pass[:6]))
    if new_fail or new_pass:
        reasons.append(f"这种变化碰运气也会出现的概率 p≈{p:.2f}"
                       + ("（够小，算真变化）" if p < _P_REAL else "（不够小，先当噪声看）"))
    for d in _all_dims(scored):                        # 各方面大变化：写进原因供参考
        cv, bv = _dim_mean(scored, d), _dim_mean(list(base_scored.values()), d)
        if cv is not None and bv is not None and abs(cv - bv) >= _GUARD:
            sign = "+" if cv > bv else "-"
            reasons.append(f"方面「{report.DIM_LABEL.get(d, d)}」{sign}{round(abs(cv - bv) * 100)} 个百分点")

    if pinned_new_fail:
        reasons.insert(0, "必过题失守：" + "、".join(pinned_new_fail))
        return {"label": "变差 · 打回", "kind": "bad", "reasons": reasons}
    if p < _P_REAL and len(new_fail) > len(new_pass):
        return {"label": "变差 · 打回", "kind": "bad", "reasons": reasons}
    if p < _P_REAL and len(new_pass) > len(new_fail):
        return {"label": "变好", "kind": "ok", "reasons": reasons}
    if new_fail and new_pass:
        return {"label": "有得有失 · 待人看", "kind": "warn", "reasons": reasons}
    return {"label": "没明显变化", "kind": "neutral", "reasons": reasons}


# ── 跑批身份证（模型/代码/判分器版本，归因用）─────────────────────────
def run_meta(mode: str, n, tasks_dir: str, skipped=None) -> dict:
    def _git(*args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            return ""

    fp = hashlib.sha1()
    for root, _d, files in os.walk(tasks_dir):
        for fn in sorted(files):
            if fn.endswith((".jsonl", ".jsonc", ".json")):
                with open(os.path.join(root, fn), "rb") as fh:
                    fp.update(fh.read())
    here = os.path.dirname(os.path.abspath(__file__))
    for fn in ("scorers.py", "runner.py"):
        with open(os.path.join(here, fn), "rb") as fh:
            fp.update(fh.read())
    try:
        from pipeline import config
        model = getattr(config, "LOOP_MODEL", "?")
    except Exception:
        model = "?"
    return {
        "model": model if mode == "live" else "(脚本，不调模型)",
        "commit": _git("rev-parse", "--short", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "n": n,
        "scorer_fp": fp.hexdigest()[:10],       # 判分器+题库指纹：变了说明尺子/题动过
        "skipped": skipped or [],
    }


# ── 命令行输出 ───────────────────────────────────────────────────────
def print_summary(cur, base, verdict):
    print("\n道题结果：")
    basemap = {r["id"]: r for r in (base or [])}
    for r in cur:
        if r.get("status") == "infra_error":
            print(f"  [环境] {r['id']:<28} 未计分")
            continue
        mark = "过 " if r["passed"] else "没过"
        p3 = r["pass_k"].get("3")
        p3s = "-" if p3 is None else f"{p3:.2f}"
        pin = "[必过]" if r["pinned"] else "     "
        flip = ""
        b = basemap.get(r["id"])
        if b and b.get("passed") and not r["passed"]:
            flip = "  <- 由过变没过"
        print(f"  {pin} {r['id']:<28} {mark}  连过3次:{p3s}{flip}")
    scored = _scored(cur)
    lo, hi = scorers.wilson(sum(1 for r in scored if r["passed"]), len(scored) or 1)
    print(f"\n通过率（只算真计分的题）：{sum(1 for r in scored if r['passed'])}/{len(scored)}"
          f"，波动区间约 {round(lo * 100)}%~{round(hi * 100)}%")
    print(f"结论：{verdict['label']}")
    for why in verdict["reasons"]:
        print(f"   · {why}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:                                   # Windows 控制台默认编码打不了中文 -> 切 utf-8
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="VS 评测跑批器")
    ap.add_argument("tasks_dir", nargs="?", default="evals/tasks")
    ap.add_argument("--out", default="evals/report.html")
    ap.add_argument("--compare", action="store_true", help="脚本车道：好策略(旧版) vs 回归策略(新版)")
    ap.add_argument("--live", action="store_true", help="真跑：真 Gemini 进循环（要 GCP 凭证 + 花 token）")
    ap.add_argument("--n", type=int, default=None,
                    help="每题跑几次。默认档：普通题 3 次、必过题 5 次（全过才算过，压掉单次运气）；"
                         "显式给 --n 则全体照办（省钱冒烟用 --n 1）")
    ap.add_argument("--list", action="store_true", help="只列数据集统计，不跑")
    ap.add_argument("--ids", default=None,
                    help="GD-0：只跑这些题（逗号分隔 id，支持前缀通配 retrieval-*）——GEPA 候选评估用")
    ap.add_argument("--split", default=None, choices=("train", "val", "sealed"),
                    help="GD-1：只跑某一堂（train=反思器可见 / val=候选选优 / sealed=最终门；"
                         "清单见 evals/split_manifest.json）。子集跑同样不写仪表盘")
    # GD-0 政策：语义检索【默认开】对齐生产（生产 USE_SEMANTIC_SEARCH 默认 1;之前 eval 默认关
    # → L11 等语义教训在 eval 里是死段,优化的和上线的不是同一个 prompt）。embed 失败自动回退关。
    ap.add_argument("--semantic", action=argparse.BooleanOptionalAction, default=True,
                    help="真跑时打开语义检索（默认开,对齐生产;--no-semantic 关）")
    args = ap.parse_args(argv)

    if args.semantic:
        os.environ["EVAL_SEMANTIC"] = "1"       # 假世界 install 时会据此建内存语义索引

    tasks = filter_tasks(load_tasks(args.tasks_dir), args.ids)
    if args.split:                              # GD-1:按堂过滤(train/val/sealed)
        from evals.split_tool import MANIFEST_PATH
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            _splits = json.load(f).get("splits", {})
        tasks = [t for t in tasks if _splits.get(t["id"]) == args.split]
        if not tasks:
            print(f"--split {args.split} 下没有题(清单没覆盖?先跑 python -m evals.split_tool)")
            return 2

    if args.list:
        by_dim = Counter(d for t in tasks for d in t.get("dims", ["?"]))
        single = sum(1 for t in tasks if t.get("kind") != "multi")
        pinned = sum(1 for t in tasks if t.get("pinned"))
        _todo, skipped = split_live_tasks(tasks)
        print(f"数据集：{len(tasks)} 道题（单轮 {single}，多轮 {len(tasks) - single}，必过题 {pinned}；"
              f"真跑可跑 {len(_todo)}，待接世界动作 {len(skipped)}）")
        for d, c in sorted(by_dim.items(), key=lambda x: -x[1]):
            print(f"  {d:<16} {c}")
        return 0

    from evals import dashboard

    if args.live:
        msg = live_preflight()
        if msg:
            print(msg)
            return 2
        n_label = args.n or "普通3·必过5"
        _todo, skipped = split_live_tasks(tasks)
        meta = run_meta("live", n_label, args.tasks_dir, skipped)
        cur = run_suite(tasks, live=True, n=args.n)
        prev = dashboard.latest_run("live")            # 上一次真跑当对比基准
        drop_why = baseline_drop_reason(prev, n_label, meta.get("scorer_fp"))
        if drop_why:
            prev = None
        base_results = prev.get("results") if prev else None
        v = classify(cur, base_results)
        if drop_why:
            v["reasons"].insert(0, f"没和上次比：{drop_why}——尺子不同的分数不可跨比，本次重新建基线")
        html = report.render(cur, v, baseline=base_results, title="评测报告 · 真跑（真 Gemini）",
                             meta=meta)
        cur_print, base_print, mode = cur, base_results, "live"
    else:
        from evals.fixtures.policies import GOOD, REGRESSED, TOOL_RESULTS

        base = run_suite(tasks, GOOD, TOOL_RESULTS, n=args.n)
        meta = run_meta("scripted", args.n or "普通3·必过5", args.tasks_dir)
        if args.compare:
            cur = run_suite(tasks, REGRESSED, TOOL_RESULTS, n=args.n)
            v = classify(cur, base)
            html = report.render(cur, v, baseline=base, title="评测报告 · 演示（脚本车道）", meta=meta)
            cur_print, base_print, mode = cur, base, "compare"
        else:
            cur = base
            v = classify(base)
            html = report.render(base, v, baseline=None, title="评测报告 · 脚本车道", meta=meta)
            cur_print, base_print, mode = base, None, "scripted"

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    results_path = args.out.rsplit(".", 1)[0] + ".results.jsonl"   # 每题完整明细
    with open(results_path, "w", encoding="utf-8") as fh:
        for r in cur_print:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    if args.live:
        # AI 裁判（对过表才有的参考分，永远不碰门禁）：配了 key 就顺手判一遍
        from evals import judge as judge_mod
        if judge_mod.available():
            judge_mod.judge_results(results_path)
            js = judge_mod.sidecar_summary(results_path)
            if js:
                meta["judge"] = js
    print_summary(cur_print, base_print, v)
    if args.ids or args.split:
        # GD-0/1：子集跑（GEPA 候选评估/按堂跑/手工复测）不进仪表盘历史 —— 基线不被局部样本污染
        print(f"\n报告已生成：{args.out}（每题明细：{results_path}）")
        print("（--ids/--split 子集跑：不写入仪表盘/简报，基线不受影响）")
        return 0
    dashboard.save_run(cur_print, v, mode, meta=meta)
    dash = dashboard.rebuild()
    # 顺手落一份"给大模型看的分析简报"（仪表盘上也有下载按钮，内容一样）
    from evals.briefing import write_briefing
    brief = write_briefing(dashboard.load_runs()[-1])
    print(f"\n报告已生成：{args.out}（每题明细：{results_path}）")
    print(f"仪表盘已更新：{dash}  ← 浏览器打开看历史/趋势/失败下钻，不用 push GitHub")
    print(f"分析简报：{brief}  ← 扔给 Claude 等大模型，让它分析哪里出了问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
