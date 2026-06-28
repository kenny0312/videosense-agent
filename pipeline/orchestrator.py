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

import json
import logging
from typing import Any, TYPE_CHECKING

from pipeline import mcp_client, usage
from pipeline.artifact_value_store import VALUE_STORE
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
            loop_meta: dict | None = None) -> dict:
    results = results or {}
    generated_code = {nid: r.code for nid, r in results.items() if r.code}
    plot = next((r.artifact for r in results.values() if r.artifact), {})
    videos = next((r.videos for r in results.values() if r.videos), [])
    return {
        "ok": ok,
        "status": status or ("ok" if ok else "error"),   # ok | refused | error
        "reason": reason,
        "answer": answer,
        "dag": dag.model_dump() if dag else None,
        "generated_code": generated_code,
        "plot": plot,
        "videos": videos,                                 # show_video:前端内嵌 <video> 播放
        "fail_node": fail_node,
        "error": error,
        "session_id": session_id,                         # 多轮:回传给客户端,下一轮带上
        "turn_type": turn_type,                           # new | followup | meta
        "trace": trace.as_list(),
        "trace_summary": trace.summary_line(),
        "loop": loop_meta,                                # M6:loop 审计指标(steps/terminated/tool_calls);dag 路径为 None
        "usage": usage.summarize(),                       # 本轮 LLM token 总计 + 估算成本(含自愈重试)
    }


def _explain_meta(session: "Session", resolved_ids: list[str]) -> str:
    """meta 轮:纯 Python 模板,用 catalog 句柄(label/kind/n/预览)说明上一轮【产出了什么】。
    M7b 起 catalog 不再存 recipe/步骤链,故这里只据 handle 描述,不编造"怎么算的"、不调模型。"""
    lines = ["我来说说上一轮的结果:"]
    for aid in resolved_ids:
        a = session.get_artifact(aid)
        if not a:
            continue
        head = f"· 『{a.label}』产出了一个 {a.kind}"
        if a.n:
            head += f",共 {a.n} 条"
        lines.append(head + "。")
        if a.preview:
            lines.append(f"    预览:{json.dumps(a.preview, ensure_ascii=False)}")
    lines.append("想知道具体怎么得到的,我可以再跑一遍给你看每一步。")
    return "\n".join(lines)


def run_query(nl: str, *, quiet_trace: bool = False,
              session: "Session | None" = None,
              owner: str = "anon", on_step=None) -> dict:
    usage.reset_usage()                  # 清空本轮 token 累加器(每请求一次)
    trace = Trace(quiet=quiet_trace)
    sandbox = SandboxClient()

    # 会话视图:单轮(session=None)→ 全 None,Router/记录路径与单轮完全一致
    history_view = session.history_view() if session else None
    catalog_view = session.catalog_view() if session else None
    resolved_ids: list = []          # 本轮解析到的真实 artifact id(下面解析后赋值;用于冻结指代)

    def _remember(status: str, answer: Any = None, artifact_ids: list | None = None) -> None:
        """把这一轮记进 history(含拒答/失败轮,供后续 meta/指代);session 层出错 fail-open。"""
        if session is None:
            return
        try:
            session.record_turn(nl, verdict, status, answer,
                                 artifact_ids=artifact_ids, referenced_ids=resolved_ids)
        except Exception as e:
            log.warning("session.record_turn 失败(fail-open): %r", e)

    # ── 前置 Router:判可答性 + 意图(不可答则拒,跳过昂贵 planner)──
    rstep = trace.step("Route")
    schema = None
    verdict = None
    try:
        schema = mcp_client.get_schema()
        verdict = Router().judge(nl, schema=schema, tools=catalog_for_planner(),
                                 history=history_view, artifact_catalog=catalog_view)
        rstep.ok(decision=verdict.decision, intent=verdict.intent,
                 route=verdict.route or "-", conf=f"{verdict.confidence:.2f}")
    except Exception as e:
        # Router 自身出错 → fail-open:照常往下规划,不因 router 崩了卡住
        rstep.fail(error=repr(e))

    sid = session.session_id if session else None
    ttype = getattr(verdict, "turn_type", "new") if verdict else "new"
    intent = getattr(verdict, "intent", "other") if verdict else "other"
    route = getattr(verdict, "route", "") if verdict else ""

    if verdict is not None and verdict.decision == "smalltalk":
        # 不再回固定一句:小模型按人设生成可变回复,失败再回退到 SMALLTALK_REPLY 常量。
        ans = smalltalk_reply(nl) or SMALLTALK_REPLY
        _remember("smalltalk", ans)
        return _result(True, trace=trace, status="smalltalk", answer=ans,
                       session_id=sid, turn_type=ttype)
    if verdict is not None and should_refuse(verdict):
        _remember("refused")
        return _result(False, trace=trace, status="refused", reason=verdict.reason,
                       session_id=sid, turn_type=ttype)

    # ── 多轮:解析指代 —— 只信 catalog 真实 id(集合成员),不信模型的 resolvable ──
    resolved_ids = session.resolve_references(verdict) if (session and verdict) else []

    # meta:解释上一轮"用了什么方法"(纯模板,不再规划、不调模型)
    if ttype == "meta":
        if resolved_ids:
            ans = _explain_meta(session, resolved_ids)
            _remember("ok", ans)
            return _result(True, trace=trace, status="ok", answer=ans,
                           session_id=sid, turn_type="meta")
        _remember("refused")                          # 没有可参考的上一轮分析 → 诚实拒答
        return _result(False, trace=trace, status="refused",
                       reason="这是关于先前分析的元问题,但我没有可参考的上一轮结果。",
                       session_id=sid, turn_type="meta")

    # followup 却解析不到任何真实结果 → 诚实拒答,不瞎规划
    if ttype == "followup" and session is not None and not resolved_ids:
        _remember("refused")
        return _result(False, trace=trace, status="refused",
                       reason="你像是在指代之前的某条结果,但我没法确定具体是哪一条 —— "
                              "说得更具体些(比如含哪个活动、第几条),我就能接着算。",
                       session_id=sid, turn_type="followup")

    # M7b:planner_context 已删(loop 的上一轮上下文走 transcript 回放,见下方 build_loop_context)。
    # context 仅供【自定义 skill handler】;现阶段四大类 handler 都走 loop,无 handler 在用 → 恒 None。
    context = None

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
            _remember("error")
            return _result(False, trace=trace, error=f"skill {handler_key} failed: {e!r}",
                           session_id=sid, turn_type=ttype)
        _remember("ok", ans)
        return _result(True, trace=trace, status="ok", answer=ans,
                       session_id=sid, turn_type=ttype)

    # ── M7b:probe-and-step 主循环是【唯一】执行路径(旧 Planner→DAG 已删;dev CLI main.py 仍保留)──
    from pipeline import loop_driver, loop_memory
    from pipeline.transcript_store import STORE as TX_STORE, gcs_blob_put
    lstep = trace.step("Loop")
    # M5:follow-up/meta 的上一轮上下文来自 transcript 回放(取代 recipe);新轮无回放
    replay_ctx = None
    if session is not None and ttype in ("followup", "meta"):
        try:
            replay_ctx = loop_memory.build_loop_context(
                TX_STORE, owner, sid, summarize=loop_memory.make_llm_summarizer())
        except Exception as e:
            log.warning("build_loop_context 失败(fail-open): %r", e)
    try:
        lo = loop_driver.run_query_loop(nl, schema=schema, replay_context=replay_ctx,
                                        sandbox=sandbox, trace=trace, session_id=sid,
                                        value_store=VALUE_STORE, on_step=on_step)
        lstep.ok(steps=lo.steps, terminated=lo.terminated)
    except Exception as e:
        lstep.fail(error=repr(e))
        _remember("error")
        return _result(False, trace=trace, error=f"loop failed: {e!r}",
                       session_id=sid, turn_type=ttype)
    if lo.answer is None:
        _remember("error")
        return _result(False, trace=trace, error=f"loop 未收敛({lo.terminated})",
                       session_id=sid, turn_type=ttype)
    # 成功 → 把最终结果登记为可指代的 artifact(纯 handle;可复用类的真实值另存独立值仓)
    artifact_ids = None
    if session is not None and lo.final_tool is not None:
        try:
            art = session.register_artifact(
                final_tool=lo.final_tool, final_value=lo.final_value,
                preview_value=lo.preview_value, question=nl, intent=intent,
                value_store=VALUE_STORE)
            artifact_ids = [art.id]
        except Exception as e:
            log.warning("loop register_artifact 失败(fail-open): %r", e)
    _remember("ok", lo.answer, artifact_ids=artifact_ids)
    # M5:把这一轮记进 transcript(CC 式耐久记忆;owner 作用域,大本体溢出 GCS)
    if session is not None:
        try:
            loop_memory.record_loop_turn(TX_STORE, owner, sid, session._turn_no, nl,
                                         lo.trace, lo.results, lo.answer, blob_put=gcs_blob_put)
        except Exception as e:
            log.warning("record_loop_turn 失败(fail-open): %r", e)
    return _result(True, trace=trace, results=lo.results, answer=lo.answer,
                   session_id=sid, turn_type=ttype, loop_meta=loop_driver.loop_metrics(lo))
