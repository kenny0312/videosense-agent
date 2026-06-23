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

from pipeline import mcp_client
from pipeline.dag_schema import DAG
from pipeline.node_executor import NodeResult, execute_node
from pipeline.node_specs import catalog_for_planner
from pipeline.planner import Planner
from pipeline.router import Router, should_refuse, SMALLTALK_REPLY
from pipeline.trace import Trace
from sandbox.client import SandboxClient

if TYPE_CHECKING:                       # 仅类型提示;运行期不 import session(不在 import 期碰 STORE)
    from pipeline.session import Session

log = logging.getLogger("pipeline.orchestrator")


def _result(ok: bool, *, trace: Trace, dag: DAG | None = None,
            answer: Any = None, results: dict[str, NodeResult] | None = None,
            fail_node: str | None = None, error: str = "",
            status: str | None = None, reason: str = "",
            session_id: str | None = None, turn_type: str = "new") -> dict:
    results = results or {}
    generated_code = {nid: r.code for nid, r in results.items() if r.code}
    plot = next((r.artifact for r in results.values() if r.artifact), {})
    return {
        "ok": ok,
        "status": status or ("ok" if ok else "error"),   # ok | refused | error
        "reason": reason,
        "answer": answer,
        "dag": dag.model_dump() if dag else None,
        "generated_code": generated_code,
        "plot": plot,
        "fail_node": fail_node,
        "error": error,
        "session_id": session_id,                         # 多轮:回传给客户端,下一轮带上
        "turn_type": turn_type,                           # new | followup | meta
        "trace": trace.as_list(),
        "trace_summary": trace.summary_line(),
    }


def _explain_meta(session: "Session", resolved_ids: list[str]) -> str:
    """meta 轮:纯 Python 模板说明上一轮"用了什么方法"(只描述配方,不编造为什么、不调模型)。"""
    lines = ["我来说说上一轮是怎么算出来的:"]
    for aid in resolved_ids:
        a = session.get_artifact(aid)
        if not a:
            continue
        recipe = a.recipe or {}
        if recipe.get("type") == "sql":
            lines.append(f"· 『{a.label}』直接用这条 SQL 查的 ——\n    {recipe.get('sql', '')}")
        elif recipe.get("type") == "dag":
            if recipe.get("truncated"):
                chain = recipe.get("chain", "")
            else:
                nodes = (recipe.get("dag") or {}).get("nodes", [])
                chain = " → ".join(f"{n['id']}:{n['tool']}" for n in nodes)
            lines.append(f"· 『{a.label}』走了这条步骤链 —— {chain}")
        if a.n:
            lines.append(f"    (结果共 {a.n} 条;预览 {json.dumps(a.preview, ensure_ascii=False)})")
    lines.append("想知道某一步更细的逻辑就告诉我哪一步,我可以展开。")
    return "\n".join(lines)


def run_query(nl: str, *, quiet_trace: bool = False,
              planner: Planner | None = None,
              session: "Session | None" = None) -> dict:
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
                 conf=f"{verdict.confidence:.2f}")
    except Exception as e:
        # Router 自身出错 → fail-open:照常往下规划,不因 router 崩了卡住
        rstep.fail(error=repr(e))

    sid = session.session_id if session else None
    ttype = getattr(verdict, "turn_type", "new") if verdict else "new"
    intent = getattr(verdict, "intent", "other") if verdict else "other"

    if verdict is not None and verdict.decision == "smalltalk":
        _remember("smalltalk", SMALLTALK_REPLY)
        return _result(True, trace=trace, status="smalltalk", answer=SMALLTALK_REPLY,
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

    # 已解析的上一轮结果(配方+预览)打包给 Planner —— 复用策略=重算
    context = session.planner_context(resolved_ids) if (session and resolved_ids) else None

    # ── Stage 4: 规划 ──
    step = trace.step("Plan DAG")
    try:
        planner = planner or Planner(schema=schema)   # 复用 router 取到的 schema(None 则 Planner 自取)
        dag = planner.plan(nl, context=context) if context else planner.plan(nl)
        step.ok(nodes=len(dag.nodes),
                tools=",".join(n.tool for n in dag.nodes))
    except Exception as e:
        step.fail(error=repr(e))
        _remember("error")
        return _result(False, trace=trace, error=f"planning failed: {e!r}",
                       session_id=sid, turn_type=ttype)

    # ── 拓扑执行 ──
    order = dag.topo_order()
    results: dict[str, NodeResult] = {}

    for node in order:
        upstream = {dep: results[dep].value for dep in node.depends_on
                    if dep in results}
        res = execute_node(node, upstream, sandbox, trace, schema=planner.schema)
        results[node.id] = res
        if not res.ok:
            _remember("error")
            return _result(False, trace=trace, dag=dag, results=results,
                           fail_node=node.id,
                           error=f"node {node.id} ({node.tool}) failed: {res.stderr[:300]}",
                           session_id=sid, turn_type=ttype)

    # ── 汇总:最终节点的值即答案 ──
    final = order[-1]
    answer = results[final.id].value

    # 成功 → 把结果登记为可指代的 artifact(复用策略=重算:存配方),再记这一轮
    if session is not None:
        artifact_ids = None
        try:
            node_values = {nid: r.value for nid, r in results.items()}
            art = session.register_artifact(dag, node_values, nl, intent)
            artifact_ids = [art.id]
        except Exception as e:
            log.warning("session.register_artifact 失败(fail-open): %r", e)
        _remember("ok", answer, artifact_ids=artifact_ids)

    return _result(True, trace=trace, dag=dag, results=results, answer=answer,
                   session_id=sid, turn_type=ttype)
