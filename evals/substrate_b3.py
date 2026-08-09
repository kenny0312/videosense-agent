"""B3:任务底座端到端三剧本(docs/longhorizon-master-plan.md Part E)。

三剧本(真库、真模型、真钱;inline 驱动,不依赖 Cloud Tasks):
  ① 多视频报告全程 + 六事故穿越:波中途注入六种故障(僵尸 CAS / 租约被占 / 队列挂 /
     规划崩 / 收口崩 / 取消),看任务能不能扛过去并最终交付;
  ② 完成回流 + 续作:任务做完 → 主 loop 下一轮拿到通知 → get_task_report → 用
     parent_task_id 立续作,规划波读到父报告;
  ③ 预算暂停 + resume:cap 卡到一波内触发 paused_budget → resume 提额 → 继续推进到 done。
每步落盘 evals/runs/b3-*.jsonl;双审计对齐检查(任务账本 spent 与 usage 实测)。

跑法:python -m evals.substrate_b3 --scenario 1,2,3 --budget 3.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "runs"


def _env(**kw):
    base = {"USE_TASKS": "1", "USE_TASK_TOOL": "1", "TASKS_DRIVER": "inline",
            "USE_SUBAGENTS": "1", "USE_WEB_SEARCH": "0", "USE_SELF_CHECK_CRITIC": "0",
            "MAX_TREE_COST_USD": "0.80", "MAX_TREE_WALL_S": "900",
            "SUBAGENT_MAX_FANOUT": "3", "SUBAGENT_MAX_STEPS": "3",
            "TASK_MAX_CAP_USD": "1.0", "RL_TASK_DAILY_COST_USD": "1.0"}
    base.update({k: str(v) for k, v in kw.items()})
    for k, v in base.items():
        os.environ[k] = v
    import importlib
    from pipeline import config
    importlib.reload(config)
    for m in ("pipeline.task_runner", "pipeline.task_store", "pipeline.task_queue",
              "pipeline.loop_driver", "pipeline.subagents"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])


def _drive(task_id: str, max_waves: int = 8, log=None) -> list:
    """同步推进整条波链(确定性版本:自己循环调 advance)。

    【必须掐掉自动投递】:inline 驱动会为每一波起一个后台 daemon 线程去 advance,
    那个线程先认领了租约,我这边再调同一波就撞 CAS 拿到 lease_busy —— 底座是对的
    (重复执行被正确拦住),但测试就成了双驱动、不确定。这里把 enqueue 掐成 no-op,
    让波链只由本循环推进。"""
    from pipeline import task_queue, task_runner, task_store
    task_queue.enqueue_advance = lambda *a, **k: None       # 只驱动一次,别双驱
    outs = []
    for _ in range(max_waves):
        st = task_store.status_of(task_id)
        if not st or st[0] not in ("pending", "running"):
            break
        out = task_runner.advance(task_id, st[1])
        outs.append(out)
        if log:
            log(f"    wave{st[1]} → {out.get('dispatch')}")
        if out.get("dispatch") in ("done", "paused_budget", "paused_error",
                                   "terminal", "crashed"):
            break
    return outs


def scenario_1(owner: str, log) -> dict:
    """① 多视频报告全程 + 六事故穿越。"""
    from pipeline import task_queue, task_runner, task_store, taskstate as TS
    from pipeline.semantic_index import _execute
    _env()
    rec = {"scenario": 1, "incidents": {}}
    goal = f"找出库里有人游泳的视频,逐条说说画面里在干什么,最后汇总成一份短报告 [{uuid.uuid4().hex[:6]}]"
    tid, created = task_store.create_task(owner, goal, 1.0)
    rec["task_id"], rec["created"] = tid, created
    log(f"  立项 {tid}")

    # 事故 1:第一波投递前队列挂 → fail-closed 应进 paused_error,不留幽灵
    try:
        raise RuntimeError("模拟队列不可用")
    except Exception as e:
        task_store.set_status(tid, "paused_error")
        task_store.add_event(tid, "enqueue_failed", {"error": repr(e)[:80]})
    st = task_store.status_of(tid)
    rec["incidents"]["1_enqueue_fail"] = (st[0] == "paused_error")
    log(f"    事故1 队列挂 → {st[0]}")

    # 事故 2:resume 复活(必须投递,否则死锁)
    got = task_store.resume(tid, 1.0)
    rec["incidents"]["2_resume"] = got is not None
    log(f"    事故2 resume → wave {got[0] if got else 'FAIL'}")

    # 规划波 + 若干执行波
    _drive(tid, max_waves=2, log=log)

    # 事故 3:僵尸 CAS —— 手动把 lease_token 换掉,模拟租约易主,战果应被丢弃
    _execute("UPDATE agent_tasks SET lease_token='someone-else', "
             "lease_until=now()+interval '5 min' WHERE task_id=%(t)s", {"t": tid})
    st = task_store.status_of(tid)
    out = task_runner.advance(tid, st[1])
    rec["incidents"]["3_lease_busy"] = (out.get("result") == "retry")
    log(f"    事故3 租约被占 → {out.get('dispatch')} (result={out.get('result')})")
    _execute("UPDATE agent_tasks SET lease_token=NULL, lease_until=NULL "
             "WHERE task_id=%(t)s", {"t": tid})

    # 事故 4:旧波号重复投递 → 断链修复(补投当前波),不重跑
    st = task_store.status_of(tid)
    out = task_runner.advance(tid, max(0, st[1] - 1))
    rec["incidents"]["4_stale_delivery"] = out.get("dispatch") in ("reenqueued", "terminal")
    log(f"    事故4 旧波号重投 → {out.get('dispatch')}")

    # 事故 5:规划/执行崩 → paused_error 且账目结清
    orig = task_runner.run_wave
    task_runner.run_wave = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟波内崩"))
    st = task_store.status_of(tid)
    if st and st[0] in ("pending", "running"):
        out = task_runner.advance(tid, st[1])
        rec["incidents"]["5_wave_crash"] = out.get("dispatch") == "crashed"
        log(f"    事故5 波内崩 → {out.get('dispatch')}")
    task_runner.run_wave = orig
    task_store.resume(tid, 1.0)

    # 事故 6:跑到底(或预算/步数到顶)
    outs = _drive(tid, max_waves=6, log=log)
    view = task_store.get_view(owner, tid)
    rec["final_status"] = view["status"]
    rec["cost"] = view["cost"]
    rec["progress"] = view["progress"]
    rec["has_report"] = bool((task_store.report_of(owner, tid) or {}).get("report"))
    rec["incidents"]["6_survived"] = view["status"] in ("done", "paused_budget")
    log(f"  终态 {view['status']} 花费 ${view['cost']['spent_usd']:.4f} "
        f"报告 {'有' if rec['has_report'] else '无'}")
    return rec


def scenario_2(owner: str, log) -> dict:
    """② 完成回流 + 续作。"""
    from pipeline import loop_driver, task_store
    from pipeline.dag_schema import Node
    from pipeline.node_executor import _run_get_task_report, _run_start_background_task
    _env()
    rec = {"scenario": 2}
    # 造一个已完成任务(直接落一份报告,省钱 —— 本剧本测的是回流链路不是跑波)
    goal = f"整理一份滑雪视频清单 [{uuid.uuid4().hex[:6]}]"
    tid, _ = task_store.create_task(owner, goal, 1.0)
    from pipeline.semantic_index import _execute
    import json as _j
    _execute("UPDATE agent_tasks SET status='done', plan=%(p)s, spent_usd=0.2 "
             "WHERE task_id=%(t)s",
             {"t": tid, "p": _j.dumps({"remaining": [], "done": {"1": {"answer": "子结论:v1 是滑雪"}},
                                       "report": "报告正文:库里有 3 条滑雪视频,分别是……"})})
    log(f"  预置已完成任务 {tid}")

    notice, ids = loop_driver.task_done_notice(owner)
    rec["notice_len"] = len(notice)
    rec["notice_has_task"] = tid in notice
    rec["notice_ids"] = ids
    log(f"    回流注入 {len(notice)} 字,含 task_id={rec['notice_has_task']}")

    n = Node(id="c1", tool="get_task_report", inputs={"task_id": tid}, depends_on=[])
    res = _run_get_task_report(n, owner=owner)
    rec["report_fetched"] = "报告正文" in str(res.value.get("report"))
    log(f"    get_task_report → {'拿到全文' if rec['report_fetched'] else '失败'}")

    # 续作:parent 贯通 + 规划波读到父报告
    n2 = Node(id="c2", tool="start_background_task",
              inputs={"goal": f"基于上一版再补充时间点 [{uuid.uuid4().hex[:6]}]",
                      "parent_task_id": tid}, depends_on=[])
    res2 = _run_start_background_task(n2, owner=owner)
    child = res2.value["task_id"]
    rec["child_task_id"] = child
    from pipeline import task_runner
    row = task_store.report_of(owner, tid)
    ctx = task_runner._parent_context({"owner": owner, "parent_task_id": tid})
    rec["parent_ctx_has_report"] = bool(ctx and "报告正文" in ctx)
    log(f"    续作 {child};父报告进规划上下文={rec['parent_ctx_has_report']}")
    task_store.set_status(child, "cancelled")            # 不真跑,省钱
    task_store.mark_notified(ids)
    return rec


def scenario_3(owner: str, log) -> dict:
    """③ 预算暂停 + resume。"""
    from pipeline import task_runner, task_store
    _env(TASK_MAX_CAP_USD="1.0")
    rec = {"scenario": 3}
    goal = f"逐个看几段视频再汇总 [{uuid.uuid4().hex[:6]}]"
    tid, _ = task_store.create_task(owner, goal, 0.02)     # 极小 cap:第一波就该触发
    log(f"  立项 {tid} cap=$0.02")
    _drive(tid, max_waves=3, log=log)
    st = task_store.status_of(tid)
    rec["paused_at_low_cap"] = (st[0] == "paused_budget")
    log(f"    低 cap → {st[0]}")
    got = task_store.resume(tid, 0.6)
    rec["resumed"] = got is not None
    outs = _drive(tid, max_waves=5, log=log)
    view = task_store.get_view(owner, tid)
    rec["final_status"] = view["status"]
    rec["cost"] = view["cost"]
    rec["advanced_after_resume"] = view["wave_n"] > 0
    log(f"    resume 后终态 {view['status']} wave={view['wave_n']} "
        f"花费 ${view['cost']['spent_usd']:.4f}")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="1,2,3")
    ap.add_argument("--budget", type=float, default=3.0)
    ap.add_argument("--owner", default="b3-eval")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    fh = (OUT / "b3.jsonl").open("a", encoding="utf-8")

    def log(s):
        print(s, flush=True)

    from pipeline.agentops import usage
    usage.reset_usage()
    results = []
    for s in a.scenario.split(","):
        s = s.strip()
        fn = {"1": scenario_1, "2": scenario_2, "3": scenario_3}.get(s)
        if not fn:
            continue
        print(f"\n[B3] 剧本 {s}", flush=True)
        t0 = time.perf_counter()
        try:
            rec = fn(a.owner, log)
        except Exception as e:
            rec = {"scenario": int(s), "error": repr(e)[:300]}
            print(f"  剧本崩了:{e!r}", flush=True)
        rec["wall_s"] = round(time.perf_counter() - t0, 1)
        rec["usage_cost_usd"] = round(float(usage.summarize().get("cost_usd") or 0), 4)
        results.append(rec)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        if rec["usage_cost_usd"] >= a.budget:
            print(f"[B3] 预算 ${a.budget} 用尽,停", flush=True)
            break
    fh.close()
    print("\n[B3] 汇总:" + json.dumps(results, ensure_ascii=False)[:1500], flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
