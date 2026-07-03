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
              owner: str = "anon", on_step=None, pro_video: bool = False,
              image: "tuple[bytes, str] | None" = None) -> dict:
    usage.reset_usage()                  # 清空本轮 token 累加器(每请求一次)
    # Pro 模式:本请求的 analyze_video 用 pro 模型;否则默认 flash。每请求开头都设(跨请求不串)。
    from perception import analyze_video_contextual as _AVC
    _AVC.MODEL_OVERRIDE.set(_AVC.PRO_MODEL if pro_video else None)
    trace = Trace(quiet=quiet_trace)
    sandbox = SandboxClient()

    # ── schema(loop 需要)──
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

    # turn_type:据回放廉价派生(有上文=followup,否则 new),零模型调用。
    # (V1-C 清理:旧 Router 终结门与 skills 分派已删 —— one-loop 稳定数周、gate 一直为 0、
    #  自定义 handler 从未注册;历史见 docs/design/one-loop-router-demote.md 与 git。)
    ttype = "followup" if replay_ctx else "new"

    # ── probe-and-step 主循环是【唯一】执行路径(dev CLI main.py 仍保留)──
    from pipeline import loop_driver
    from pipeline.transcript_store import gcs_blob_put
    lstep = trace.step("Loop")
    # U3 自我认知:把会话累计 usage / 模型档位 / 窗口等真实数字注入 loop(元问题不再拒答/编数)。
    rt_facts = loop_driver.runtime_facts_line(
        getattr(session, "usage_cum", None) if session is not None else None, nl=nl)
    # L2 用户记忆:跨会话偏好/事实(owner 作用域;无记忆 = 空串不占 token;fail-open)。
    if config.USE_USER_MEMORY:
        try:
            from pipeline import user_memory
            mem = user_memory.render_section(owner)
            if mem:
                rt_facts = rt_facts + "\n\n" + mem
        except Exception as e:
            log.warning("用户记忆加载失败(fail-open): %r", e)
    # 瞬时失败(重试后仍抖 / 未收敛)→ 给【优雅的重试提示】而非原始崩溃卡片。
    # (Pandora 对照测的镜像教训:别把抖动伪装成"库空"的假结果,也别把它甩成 error;诚实说"这次没成,再试一次"。)
    _RETRY_MSG = "抱歉,这次没能完成 —— 可能是临时的服务波动。请再发一次,或把问题说得更具体一点。"
    try:
        lo = loop_driver.run_query_loop(nl, schema=schema, replay_context=replay_ctx,
                                        sandbox=sandbox, trace=trace, session_id=sid,
                                        on_step=on_step, runtime_facts=rt_facts, owner=owner,
                                        image=image)
        lstep.ok(steps=lo.steps, terminated=lo.terminated)
    except Exception as e:
        lstep.fail(error=repr(e))
        log.warning("loop 抛错(优雅降级为重试提示): %r", e)
        return _result(True, trace=trace, status="ok", answer=_RETRY_MSG,
                       session_id=sid, turn_type=ttype)
    if lo.answer is None:
        log.warning("loop 未收敛(%s)→ 重试提示", lo.terminated)
        return _result(True, trace=trace, status="ok", answer=_RETRY_MSG,
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
