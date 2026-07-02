"""
编排核心(Stage 10)—— 把 Planner → CodeGen → Sandbox/MCP 串成一条流水线。

    run_query(nl):
        1. Planner.plan(nl)            自然语言 → DAG(已校验)
        2. dag.topo_order()            拓扑排序
        3. for node in order:          逐节点执行
              upstream = {dep: 上游结果}
              execute_node(node, upstream, ...)   数据节点走 MCP / 科学节点走沙箱+自愈
              失败 → 中止,返回到此为止的 trace
        4. 汇总:最终答案 + 每节点生成的代码 + 图表产物

返回结构对齐大纲 Stage 10 的交付物:
    answer / dag / generated_code / plot(png_base64) / trace
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from pipeline import config, mcp_client, usage
from pipeline.dag_schema import DAG
from pipeline.node_executor import NodeResult
from pipeline.node_specs import catalog_for_planner
from pipeline.router import Router, should_refuse, SMALLTALK_REPLY
from pipeline.skills import loader as skills
from pipeline.skills.handlers import HANDLERS, smalltalk_reply
from pipeline.trace import Trace
from sandbox.client import SandboxClient

if TYPE_CHECKING:                       # 仅类型提示;运行期不 import session(不在 import 期碰 STORE)
    from pipeline.session import Session

log = logging.getLogger("pipeline.orchestrator")


def _result(ok: bool, *, trace: Trace, dag: DAG | None = None,
            answer: Any = None, results: dict[str, NodeResult] | None = None,
            fail_node: str | None = None, error: str = "",
            status: str | None = None, reason: str = "",
            session_id: str | None = None, turn_type: str = "new",
            loop_meta: dict | None = None, context: dict | None = None) -> dict:
    results = results or {}
    generated_code = {nid: r.code for nid, r in results.items() if r.code}
    plot = next((r.artifact for r in results.values() if r.artifact), {})
    videos = next((r.videos for r in results.values() if r.videos), [])
    table = next((r.table for r in results.values() if r.table), {})
    return {
        "ok": ok,
        "status": status or ("ok" if ok else "error"),   # ok | refused | error
        "reason": reason,
        "answer": answer,
        "dag": dag.model_dump() if dag else None,
        "generated_code": generated_code,
        "plot": plot,
        "videos": videos,                                 # show_video:前端内嵌 <video> 播放
        "table": table,                                   # show_table:前端渲染成表格(原始行,不经大脑复述)
        "fail_node": fail_node,
        "error": error,
        "session_id": session_id,                         # 多轮:回传给客户端,下一轮带上
        "turn_type": turn_type,                           # new | followup | meta
        "trace": trace.as_list(),
        "trace_summary": trace.summary_line(),
        "loop": loop_meta,                                # M6:loop 审计指标(steps/terminated/tool_calls);dag 路径为 None
        "context": context,                               # 前端 context 监控环:{replay_tokens, budget}(仅 loop 路径)
        "usage": usage.summarize(),                       # 本轮 LLM token 总计 + 估算成本(含自愈重试)
    }


def run_query(nl: str, *, quiet_trace: bool = False,
              session: "Session | None" = None,
              owner: str = "anon", on_step=None, pro_video: bool = False) -> dict:
    usage.reset_usage()                  # 清空本轮 token 累加器(每请求一次)
    # Pro 模式:本请求的 analyze_video 用 pro 模型;否则默认 flash。每请求开头都设(跨请求不串)。
    from perception import analyze_video_contextual as _AVC
    _AVC.MODEL_OVERRIDE.set(_AVC.PRO_MODEL if pro_video else None)
    trace = Trace(quiet=quiet_trace)
    sandbox = SandboxClient()

    resolved_ids: list = []          # 兼容自定义 skill handler 形参;指代解析已下放给 loop

    # ── schema(loop 与 Router 都要;loop-primary 下 loop 仍需 schema)──
    schema = None
    try:
        schema = mcp_client.get_schema()
    except Exception as e:
        trace.step("Schema").fail(error=repr(e))

    sid = session.session_id if session else None

    # ── transcript 回放:两条路都要(loop 复用 + 派生 turn_type)。只建一次。──
    from pipeline import loop_memory
    from pipeline.transcript_store import STORE as TX_STORE
    replay_ctx = None
    if session is not None:
        try:
            replay_ctx = loop_memory.build_loop_context(
                TX_STORE, owner, sid, summarize=loop_memory.make_llm_summarizer())
        except Exception as e:
            log.warning("build_loop_context 失败(fail-open): %r", e)

    # ── 路由层(设计 one-loop-router-demote.md)─────────────────────────────
    # 默认【单 loop 主路】(USE_ROUTER_GATE=0):不调 Router,loop 带【完整回放】自己判 闲聊/超范围拒/
    # clarify —— 省每轮一次 flash,且根治"context-blind 前置门把只有结合上文才看得懂的短回复误杀"。
    # USE_ROUTER_GATE=1:保留旧 Router 终结门(回退开关;PR#48 后 smalltalk/refuse 仅在无回放时终结)。
    verdict = None
    route = ""
    if config.USE_ROUTER_GATE:
        rstep = trace.step("Route")
        try:
            verdict = Router().judge(nl, schema=schema, tools=catalog_for_planner())
            rstep.ok(decision=verdict.decision, intent=verdict.intent,
                     route=verdict.route or "-", conf=f"{verdict.confidence:.2f}")
        except Exception as e:
            rstep.fail(error=repr(e))                       # fail-open → 照常进 loop
        route = getattr(verdict, "route", "") if verdict else ""
        if verdict is not None and verdict.decision == "smalltalk" and not replay_ctx:
            ans = smalltalk_reply(nl) or SMALLTALK_REPLY
            return _result(True, trace=trace, status="smalltalk", answer=ans,
                           session_id=sid, turn_type=getattr(verdict, "turn_type", "new"))
        if verdict is not None and should_refuse(verdict) and not replay_ctx:
            return _result(False, trace=trace, status="refused", reason=verdict.reason,
                           session_id=sid, turn_type=getattr(verdict, "turn_type", "new"))

    # turn_type:有 verdict 用它;loop-primary 据回放廉价派生(有上文=followup,否则 new),零模型调用。
    ttype = (getattr(verdict, "turn_type", "new") if verdict is not None
             else ("followup" if replay_ctx else "new"))

    context = None                                          # 仅供自定义 skill handler(现无在用)

    # ── 按 route 分派 workflow(打地基)──
    # 现阶段四个大类(retrieval/aggregate/analyze/visualize)的 handler 都是 "planner",
    # 直接落到下面的 Planner→DAG 主链路。某个大类要走【自定义 workflow】时:
    #   ① skills/<name>.md 写 `handler: <key>`;② skills/handlers.py 的 HANDLERS 注册 <key>。
    # 这里见到非 "planner" 的 handler 就按表分派,无需改动本函数的其它判断。
    handler_key = skills.handler_for(route)
    custom = HANDLERS.get(handler_key) if handler_key != "planner" else None
    if custom is not None:
        hstep = trace.step(f"Skill:{handler_key}")
        try:
            ans = custom(nl, verdict=verdict, session=session, context=context,
                         schema=schema, resolved_ids=resolved_ids)
            hstep.ok(route=route)
        except Exception as e:
            hstep.fail(error=repr(e))
            return _result(False, trace=trace, error=f"skill {handler_key} failed: {e!r}",
                           session_id=sid, turn_type=ttype)
        return _result(True, trace=trace, status="ok", answer=ans,
                       session_id=sid, turn_type=ttype)

    # ── M7b:probe-and-step 主循环是【唯一】执行路径(旧 Planner→DAG 已删;dev CLI main.py 仍保留)──
    # 回放(replay_ctx)已在上方 smalltalk/refuse 门【之前】建好并在此复用 —— 不二次 build(省一次 LLM 摘要)。
    # 方案 A:Router 只管分流 + 标 turn_type,指代/复用/重算/offer 解析全交给 loop 用回放自己做。
    from pipeline import loop_driver
    from pipeline.transcript_store import gcs_blob_put
    lstep = trace.step("Loop")
    # U3 自我认知:把会话累计 usage / 模型档位 / 窗口等真实数字注入 loop(元问题不再拒答/编数)。
    rt_facts = loop_driver.runtime_facts_line(
        getattr(session, "usage_cum", None) if session is not None else None)
    # L2 用户记忆:跨会话偏好/事实(owner 作用域;无记忆 = 空串不占 token;fail-open)。
    if config.USE_USER_MEMORY:
        try:
            from pipeline import user_memory
            mem = user_memory.render_section(owner)
            if mem:
                rt_facts = rt_facts + "\n\n" + mem
        except Exception as e:
            log.warning("用户记忆加载失败(fail-open): %r", e)
    try:
        lo = loop_driver.run_query_loop(nl, schema=schema, replay_context=replay_ctx,
                                        sandbox=sandbox, trace=trace, session_id=sid,
                                        on_step=on_step, runtime_facts=rt_facts, owner=owner)
        lstep.ok(steps=lo.steps, terminated=lo.terminated)
    except Exception as e:
        lstep.fail(error=repr(e))
        return _result(False, trace=trace, error=f"loop failed: {e!r}",
                       session_id=sid, turn_type=ttype)
    if lo.answer is None:
        return _result(False, trace=trace, error=f"loop 未收敛({lo.terminated})",
                       session_id=sid, turn_type=ttype)
    # 记忆简化:不再登记 catalog/值仓 —— 唯一记忆 = transcript。推进轮号后把这一轮落 transcript
    # (CC 式耐久记忆;owner 作用域,大本体溢出 GCS)。
    if session is not None:
        try:
            turn_no = session.next_turn()
            loop_memory.record_loop_turn(TX_STORE, owner, sid, turn_no, nl,
                                         lo.trace, lo.results, lo.answer, blob_put=gcs_blob_put)
        except Exception as e:
            log.warning("record_loop_turn 失败(fail-open): %r", e)
        try:
            session.add_usage(usage.summarize())   # U3:会话累计 usage(API 层 save 时随 blob 落盘)
        except Exception as e:
            log.warning("usage 累计失败(fail-open): %r", e)
    replay_tok = (len(replay_ctx) // 3) if replay_ctx else 0   # 与 loop_memory._est_tokens 同口径
    return _result(True, trace=trace, results=lo.results, answer=lo.answer,
                   session_id=sid, turn_type=ttype, loop_meta=loop_driver.loop_metrics(lo),
                   context={"replay_tokens": replay_tok, "budget": config.LOOP_CONTEXT_TOKEN_BUDGET})
