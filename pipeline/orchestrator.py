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
from typing import Any

from pipeline import mcp_client
from pipeline.dag_schema import DAG
from pipeline.node_executor import NodeResult, execute_node
from pipeline.node_specs import catalog_for_planner
from pipeline.planner import Planner
from pipeline.router import Router, should_refuse, SMALLTALK_REPLY
from pipeline.trace import Trace
from sandbox.client import SandboxClient

log = logging.getLogger("pipeline.orchestrator")


def _result(ok: bool, *, trace: Trace, dag: DAG | None = None,
            answer: Any = None, results: dict[str, NodeResult] | None = None,
            fail_node: str | None = None, error: str = "",
            status: str | None = None, reason: str = "") -> dict:
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
        "trace": trace.as_list(),
        "trace_summary": trace.summary_line(),
    }


def run_query(nl: str, *, quiet_trace: bool = False,
              planner: Planner | None = None) -> dict:
    trace = Trace(quiet=quiet_trace)
    sandbox = SandboxClient()

    # ── 前置 Router:判可答性 + 意图(不可答则拒,跳过昂贵 planner)──
    rstep = trace.step("Route")
    schema = None
    verdict = None
    try:
        schema = mcp_client.get_schema()
        verdict = Router().judge(nl, schema=schema, tools=catalog_for_planner())
        rstep.ok(decision=verdict.decision, intent=verdict.intent,
                 conf=f"{verdict.confidence:.2f}")
    except Exception as e:
        # Router 自身出错 → fail-open:照常往下规划,不因 router 崩了卡住
        rstep.fail(error=repr(e))
    if verdict is not None and verdict.decision == "smalltalk":
        return _result(True, trace=trace, status="smalltalk", answer=SMALLTALK_REPLY)
    if verdict is not None and should_refuse(verdict):
        return _result(False, trace=trace, status="refused", reason=verdict.reason)

    # ── Stage 4: 规划 ──
    step = trace.step("Plan DAG")
    try:
        planner = planner or Planner(schema=schema)   # 复用 router 取到的 schema(None 则 Planner 自取)
        dag = planner.plan(nl)
        step.ok(nodes=len(dag.nodes),
                tools=",".join(n.tool for n in dag.nodes))
    except Exception as e:
        step.fail(error=repr(e))
        return _result(False, trace=trace, error=f"planning failed: {e!r}")

    # ── 拓扑执行 ──
    order = dag.topo_order()
    results: dict[str, NodeResult] = {}

    for node in order:
        upstream = {dep: results[dep].value for dep in node.depends_on
                    if dep in results}
        res = execute_node(node, upstream, sandbox, trace, schema=planner.schema)
        results[node.id] = res
        if not res.ok:
            return _result(False, trace=trace, dag=dag, results=results,
                           fail_node=node.id,
                           error=f"node {node.id} ({node.tool}) failed: {res.stderr[:300]}")

    # ── 汇总:最终节点的值即答案 ──
    final = order[-1]
    return _result(True, trace=trace, dag=dag, results=results,
                   answer=results[final.id].value)
