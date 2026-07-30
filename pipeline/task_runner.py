"""S-3(任务底座):波次推进心脏(设计 docs/longhorizon-task-substrate-plan.md §S-3)。

一波 = 一次 advance(task_id, wave_n):认领(三态分流)→ [wave0 规划] → 记账先行 →
跑波(run_fanout)→ CAS 落检查点 → 续投/收口。全部红队修正烙在流程里:
  · 认领 0 行 → 补读三态:①行波号更大且在跑 → 补投当前波(断链修复);②同波租约未过期
    → RETRY(503,让 Cloud Tasks 退避跨过租约);③非 running → OK(终态/暂停,别重跑);
  · 记账先行:本波保守预估【悲观计入 spent】—— 超时波也推高 spent,预算闸对重试风暴
    有视力;提交时实测覆盖,重试波把上波预估转 wasted;
  · usage.reset_usage() 显式调用("天然累计"是误读,不 reset 记账为零 —— 四跑事故同款);
  · CAS 落盘 0 行 = 本 attempt 是僵尸 → 整波战果丢弃只记 wasted event;
  · 先提交后投递;enqueue 失败 → paused_error(fail-closed,不留假活)。
LLM 接缝(plan_goal / finalize_report)与执行接缝(run_wave)都是模块函数,离线单测直接
monkeypatch;live 实现走 loop_driver / subagents 复用件。
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from pipeline import config, taskstate as TS
from pipeline.semantic_index import _execute

log = logging.getLogger("pipeline.task_runner")

# 组波纪律(§0 D2):每波 ≤ SUBAGENT_MAX_FANOUT 个子任务,每个子任务 ≤2 个候选视频
# (该限的是单子任务串行深度,不是扇出)。波预估 = 子任务数 × 单价悲观值。
WAVE_TASK_EST_USD = float(__import__("os").environ.get("TASK_WAVE_EST_USD", "0.06"))

_REREAD_SQL = ("SELECT status, wave_n, lease_until IS NOT NULL AND lease_until > now(), plan "
               "FROM agent_tasks WHERE task_id=%(t)s")

# advance 的返回给端点翻译成 HTTP:ok→200,retry→503(Cloud Tasks 退避重来)。
OK, RETRY = "ok", "retry"


def plan_goal(goal: str, notes: list) -> list:
    """规划波(wave 0):goal(+用户追加指示)→ remaining 子任务清单(带稳定 id)。
    live 实现:一次无状态 LLM 分解调用;单测 monkeypatch 本函数。"""
    from pipeline import loop_driver
    from google.genai import types
    from pipeline.genai_client import get_client
    note_txt = ("\n用户追加指示:" + " / ".join(notes)) if notes else ""
    prompt = (
        "把下面这个视频库分析目标拆成若干个【彼此独立、可并行】的子任务,每个子任务"
        "至多针对 2 个候选视频(或不指定视频)。只输出 JSON 数组,每项 "
        '{"id": 序号(int), "instruction": "子任务指令", "video_ids": [可选]}。'
        f"\n目标:{goal}{note_txt}")
    resp = get_client().models.generate_content(
        model=config.LOOP_MODEL, contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0))
    from pipeline.agentops import usage as _usage
    try:
        _usage.add_usage(resp, config.LOOP_MODEL)
    except Exception:
        pass
    txt = (resp.text or "").strip()
    i, j = txt.find("["), txt.rfind("]")
    items = json.loads(txt[i:j + 1]) if i != -1 and j > i else []
    out = []
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        instr = str(it.get("instruction") or "").strip()
        if not instr:
            continue
        vids = [str(v) for v in (it.get("video_ids") or [])][:2]   # 组波纪律:≤2 视频
        out.append({"id": int(it.get("id") or n), "instruction": instr, "video_ids": vids})
    if not out:
        raise ValueError("规划波产出为空:goal 无法分解")
    return out


STEP_CHECK_TTL_S = 5.0          # 步内闸的 DB 读数缓存(别每次工具调用都打库)


def _wrap_step_gate(execute, task_id: str, base_spent: float):
    """S-4 步内闸:每次工具调用前查【取消 / 预算】—— 波开头闸只在波边界,一波内跑 12 分钟
    期间用户按了取消、或实际花费超了 cap,必须在下一次工具调用前就停。
    触发 = 软失败信封(与熔断同款:教大脑就已有证据收口,别 kill 线程 = 全额浪费)。
    读数缓存 5s;查库失败 fail-open(观测绝不拖垮执行)。闭包属性必须透传 —— 子 agent
    靠 execute.tree_guard/tree_nodes 取全树账本(P0-6 教训)。"""
    from pipeline import loop_driver, task_store
    # fresh 是独立标记:不能用 "verdict is not None" 当"查过了"—— 健康任务的裁决恰是 None,
    # 那样每次工具调用都会打一次库(自测逮出:5 次调用查了 5 次)。
    state = {"at": 0.0, "fresh": False, "verdict": None}

    def _verdict():
        now = time.monotonic()
        # 判停【粘住】:一旦读到取消/暂停/超支就永不再查库、永不撤销(review-HIGH:
        # TTL 过期时若查库失败,fail-open 会把已生效的取消裁决重置回放行 —— 取消是终态,
        # 不存在"取消又被撤销"的合法语义)。fail-open 只对【还没判停过】的任务成立。
        if state["verdict"]:
            return state["verdict"]
        if state["fresh"] and now - state["at"] < STEP_CHECK_TTL_S:
            return None
        v = None
        try:
            live = task_store.live_state(task_id)
            if live:
                status, spent, cap = live
                if status in TS.TERMINAL:
                    v = f"[系统] 本任务已被{'取消' if status == 'cancelled' else '结束'}"
                elif status != "running":
                    v = f"[系统] 本任务已暂停({status})"
                elif spent > cap:
                    v = f"[系统] 本任务累计花费 ${spent:.4f} 已超预算上限 ${cap:.2f}"
        except Exception:
            log.warning("步内闸查库失败(fail-open)", exc_info=True)
            v = None
        state["at"], state["fresh"], state["verdict"] = now, True, v
        return v

    def gated(cid, name, inputs, upstream, uses):
        if not name.startswith("show_"):          # 交付类不烧钱,放行(与熔断同口径)
            v = _verdict()
            if v:
                return loop_driver._soft_note(
                    v + ":这次调用【没执行】。请立刻基于已经拿到的证据收口作答;"
                        "没查到的部分明确写【未核查】,不要推测填补。")
        return execute(cid, name, inputs, upstream, uses)
    gated.tree_guard = getattr(execute, "tree_guard", None)
    gated.tree_nodes = getattr(execute, "tree_nodes", None)
    return gated


def run_wave(task_row: dict, batch: list) -> dict:
    """跑一波:K 个子任务并行(复用 run_fanout 全套护栏)。返回 {id: {answer, ...}}。
    live 实现走 subagents;单测 monkeypatch 本函数。"""
    from pipeline import loop_driver, subagents
    from pipeline.agentops import trace as T
    from pipeline.agentops.treeguard import TreeGuard
    trace = T.Trace(quiet=True)
    # 波内 per-tree 熔断:cap = 任务剩余预算(不许一波烧穿任务 cap)。
    remaining_budget = max(0.05, float(task_row["budget_cap"]) - float(task_row["spent_usd"]))
    guard = TreeGuard(cost_cap=remaining_budget, wall_cap_s=0, trace=trace)
    execute = loop_driver._make_executor(sandbox=None, trace=trace, schema=None,
                                         session_id=None, owner=task_row["owner"],
                                         guard=guard)
    execute = _wrap_step_gate(execute, task_row["task_id"], float(task_row["spent_usd"]))
    tasks = [{"instruction": b["instruction"], "video_ids": b.get("video_ids") or []}
             for b in batch]
    results = subagents.run_fanout(tasks, sandbox=None, trace=trace, schema=None,
                                   owner=task_row["owner"], execute=execute)
    out = {}
    for b, r in zip(batch, results):
        out[str(b["id"])] = {"answer": (r or {}).get("output") or "(无产出)"}
    return out


def finalize_report(goal: str, done: dict) -> str:
    """收口:全部子任务结论 → 一份最终报告。live 一次 LLM 调用;单测 monkeypatch。"""
    from google.genai import types
    from pipeline.genai_client import get_client
    parts = "\n\n".join(f"[子任务 {k}] {v.get('answer', '')}" for k, v in sorted(done.items()))
    resp = get_client().models.generate_content(
        model=config.LOOP_MODEL,
        contents=f"综合以下子任务结论,写一份直接回答目标的报告(引用证据,别编)。\n"
                 f"目标:{goal}\n\n{parts}",
        config=types.GenerateContentConfig(temperature=0.0))
    from pipeline.agentops import usage as _usage
    try:
        _usage.add_usage(resp, config.LOOP_MODEL)
    except Exception:
        pass
    return (resp.text or "").strip() or "(收口生成为空)"


def _row_to_task(row) -> dict:
    (task_id, owner, goal, plan, status, wave_n, lease_token,
     budget_cap, spent_usd, wasted_usd, precharged_usd) = row
    plan = plan if isinstance(plan, dict) else json.loads(plan or "{}")
    return {"task_id": task_id, "owner": owner, "goal": goal, "plan": plan,
            "status": status, "wave_n": int(wave_n), "lease_token": lease_token,
            "budget_cap": float(budget_cap), "spent_usd": float(spent_usd),
            "wasted_usd": float(wasted_usd), "precharged_usd": float(precharged_usd)}


def _user_notes(task_id: str) -> list:
    rows = _execute("SELECT payload FROM agent_task_events WHERE task_id=%(t)s "
                    "AND kind='user_note' ORDER BY id", {"t": task_id})
    out = []
    for (p,) in rows:
        p = p if isinstance(p, dict) else json.loads(p or "{}")
        if p.get("note"):
            out.append(str(p["note"]))
    return out


def _record_cost(owner: str, cost: float):
    """任务花费喂进全站限流账(红队 HIGH:波次花费对全站熔断隐身)。sid 绝不许传常量。
    僵尸/崩溃路径也必须走这里 —— 重试风暴恰是最需要全站账有视力的时刻(review 确认)。"""
    from pipeline.agentops import ratelimit
    try:
        if cost > 0:
            ratelimit.record(owner, None, None, cost)
    except Exception:
        log.warning("任务波费用回灌 ratelimit 失败(fail-open)", exc_info=True)


def advance(task_id: str, wave_n: int) -> dict:
    """推进一波。返回 {"result": OK|RETRY, ...}(端点译成 200/503)。

    崩溃兜底的围栏(review 确认):fail-closed(paused_error)只许处置【本 attempt 持租
    之后】的崩溃 —— 认领前/认领失败路径的异常(如补读时 Neon 掐连接)只 RETRY,
    绝不动状态,否则旁路投递的瞬时错会把别人正持租在跑的波打成僵尸。"""
    from pipeline import task_queue, task_store
    from pipeline.agentops import usage
    t0 = time.perf_counter()
    audit = {"task_id": task_id, "wave_n": wave_n, "outcome": "?"}
    token = uuid.uuid4().hex
    claimed = False
    precharged = False
    owner = ""
    try:
        rows = _execute(TS.CLAIM_SQL, TS.claim_params(task_id, wave_n, token))
        if not rows:
            # 认领 0 行 → 补读三态分流(红队两条 HIGH 的修法)
            got = _execute(_REREAD_SQL, {"t": task_id})
            if not got:
                audit["outcome"] = "not_found"
                return {"result": OK, "dispatch": "not_found"}
            status, cur_wave, lease_live, plan = got[0]
            plan = plan if isinstance(plan, dict) else json.loads(plan or "{}")
            if status != "running":
                audit["outcome"] = "terminal_or_paused"
                return {"result": OK, "dispatch": "terminal"}       # ③ 别重跑
            # "还有活" = 清单非空【或】还没出报告(收口波也是活;review 后收口独立成波)
            has_work = bool(plan.get("remaining")) or not plan.get("report")
            if int(cur_wave) > int(wave_n) and has_work and not lease_live:
                task_queue.enqueue_advance(task_id, int(cur_wave))  # ① 断链修复:补投当前波
                audit["outcome"] = "reenqueued"
                return {"result": OK, "dispatch": "reenqueued"}
            audit["outcome"] = "lease_busy"
            return {"result": RETRY, "dispatch": "lease_busy"}      # ② 503,退避跨租约
        task = _row_to_task(rows[0])
        claimed, owner = True, task["owner"]

        plan = task["plan"]
        remaining = list(plan.get("remaining") or [])
        done = dict(plan.get("done") or {})
        # 三种波形:规划(wave0 无清单)/ 收口(清单空、有战果、无报告)/ 普通。
        planning = (wave_n == 0 and not remaining)
        finalizing = (wave_n > 0 and not remaining and done and not plan.get("report"))
        batch = [] if (planning or finalizing) else remaining[:max(1, config.SUBAGENT_MAX_FANOUT)]
        est = WAVE_TASK_EST_USD * max(1, len(batch))

        # S-4 波开头预算闸:悲观预估后比(spent 已是含全部浪费的真实累计,直接比)。
        # 收口波额外给一份小额收尾额度:已经付过钱买到的战果必须能变成交付物 ——
        # 否则花了 $2 的任务会因差 $0.06 的收尾费永远出不了报告,resume 也救不回(review-HIGH
        # 实测的死锁:cap 上界锁死在 TASK_MAX_CAP,spent 贴顶后任何 cap 都过不了闸)。
        gate_cap = task["budget_cap"] + (config.TASK_FINALIZE_GRACE_USD if finalizing else 0.0)
        if task["spent_usd"] + est > gate_cap:
            task_store.set_status(task_id, "paused_budget")
            task_store.add_event(task_id, "paused",
                                 {"why": "budget", "spent": task["spent_usd"],
                                  "cap": task["budget_cap"], "est": est})
            audit["outcome"] = "paused_budget"
            return {"result": OK, "dispatch": "paused_budget"}

        # 记账先行(规范 SQL):预估悲观入 spent;上一 attempt 未结算的预估同一条语句转
        # wasted(review-HIGH:没这步 est 永久滞留 spent、wasted_usd 变死列)。
        pc = _execute(TS.PRECHARGE_SQL, TS.precharge_params(task_id, token, est))
        if not pc:                                          # 租约已易主 → 本 attempt 出局
            audit["outcome"] = "lease_lost_precharge"
            return {"result": OK, "dispatch": "zombie_dropped"}
        spent_now, wasted_now = float(pc[0][0]), float(pc[0][1])
        base = spent_now - est                              # 结算基线(不含本波预估)
        precharged = True                                   # 之后的失败必须结算真实花费
        task_store.add_event(task_id, "wave_attempt",
                             {"wave": wave_n, "est_usd": est, "token": token[:8]})

        usage.reset_usage()                                # 显式 reset(红队 HIGH:不 reset 记账为零)
        if planning:
            remaining = plan_goal(task["goal"], _user_notes(task_id))
            task_store.add_event(task_id, "planned", {"n": len(remaining)})
            wave_results = {}
        elif finalizing:                                   # 收口 = 零 batch 轻波:崩了只重跑
            wave_results = {}                              # 这一次 LLM 调用,不吞整波战果(review 确认)
        else:
            wave_results = run_wave(task, batch)

        for k, v in wave_results.items():
            v["wave"] = wave_n
            done[k] = v
        finished_ids = set(wave_results.keys())
        new_remaining = [r for r in remaining if str(r.get("id")) not in finished_ids]
        new_plan = {"remaining": new_remaining, "done": done}
        if plan.get("report"):
            new_plan["report"] = plan["report"]
        if finalizing:
            new_plan["report"] = finalize_report(task["goal"], done)

        measured = float(usage.summarize().get("cost_usd") or 0.0)
        cp = _execute(TS.CHECKPOINT_SQL,
                      TS.checkpoint_params(task_id, wave_n, token, new_plan,
                                           base + measured, wasted_now))
        if not cp:                                          # 僵尸:租约易主/被取消 → 战果丢弃
            # 钱要结进【任务账本】(review-HIGH:只落 event 的话 resume 就是免费重跑,
            # cap 被突破 N 倍);租约已易主时这条 CAS 也会 0 行,那就由新主的账目接管。
            _execute(TS.SETTLE_FAILED_SQL,
                     TS.settle_failed_params(task_id, token, measured))
            task_store.add_event(task_id, "wasted",
                                 {"wave": wave_n, "usd": measured, "why": "cas_lost"})
            _record_cost(owner, measured)                   # 真实花费仍须进全站账
            audit["outcome"] = "zombie_dropped"
            return {"result": OK, "dispatch": "zombie_dropped"}
        _record_cost(owner, measured)

        if new_remaining or not new_plan.get("report"):     # 还有活(含"该收口了"的下一波)
            try:
                task_queue.enqueue_advance(task_id, wave_n + 1)     # 先提交后投递
            except Exception as e:
                task_store.set_status(task_id, "paused_error")      # fail-closed,不留假活
                task_store.add_event(task_id, "enqueue_failed", {"error": repr(e)[:200]})
                audit["outcome"] = "enqueue_failed"
                return {"result": OK, "dispatch": "enqueue_failed"}
            audit["outcome"] = "advanced"
            return {"result": OK, "dispatch": "advanced", "next_wave": wave_n + 1}
        task_store.set_status(task_id, "done")
        task_store.add_event(task_id, "done", {"waves": wave_n + 1,
                                               "spent_usd": base + measured})
        audit["outcome"] = "done"
        return {"result": OK, "dispatch": "done"}
    except Exception as e:
        if not claimed:                                     # 认领前的异常:不动状态,退避重来
            log.warning("advance(%s, w%s) 认领前异常 → RETRY(不动状态)", task_id, wave_n,
                        exc_info=True)
            audit["outcome"] = "preclaim_error"
            return {"result": RETRY, "dispatch": "preclaim_error"}
        log.warning("advance(%s, w%s) 崩溃 → paused_error", task_id, wave_n, exc_info=True)
        crash_cost = 0.0
        try:
            from pipeline.agentops import usage as _u
            crash_cost = float(_u.summarize().get("cost_usd") or 0.0)
        except Exception:
            pass
        try:
            from pipeline import task_store as _ts
            if precharged:                                  # 崩溃前的真实花费结进任务账本
                _execute(TS.SETTLE_FAILED_SQL,              # (review-HIGH:不结算 = 免费重跑)
                         TS.settle_failed_params(task_id, token, crash_cost))
            _ts.set_status(task_id, "paused_error")
            _ts.add_event(task_id, "error", {"wave": wave_n, "error": repr(e)[:200],
                                             "usd_before_crash": crash_cost})
            _record_cost(owner, crash_cost)                 # 也进全站账
        except Exception:
            log.error("advance 崩溃后的 fail-closed 处置也失败", exc_info=True)
        audit["outcome"] = "crashed"
        return {"result": OK, "dispatch": "crashed"}
    finally:                                                # 死掉的波也有账(审计红线)
        audit["ms"] = round((time.perf_counter() - t0) * 1000)
        log.info("task_wave_audit %s", json.dumps(audit, ensure_ascii=False))
