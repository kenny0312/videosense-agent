"""
单节点执行器 —— DAG 里一个节点的执行单元。

路由:
    数据获取类(sql_query / threshold_sweep)
        → 主进程经 MCP 执行(持有凭证、可信),不进沙箱
    数据科学类(merge_asof / interpolate / ols_regress / plot / ...)
        → Code Generator 生成 Python → 注入上游数据 → Stage 5 沙箱执行
          → 失败把 stderr 回喂重写(Stage 6 自愈),最多 CODE_MAX_RETRIES 次

自愈作用在**单个节点**上:n3 失败只重试 n3,上游 n1/n2 的结果不丢。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pipeline import mcp_client
from pipeline.code_generator import CodeGenerator
from pipeline.sql_fixer import SqlFixer
from pipeline.dag_schema import Node
from pipeline.node_specs import needs_sandbox
from pipeline.trace import Trace
from sandbox.client import SandboxClient

log = logging.getLogger("pipeline.node_executor")

CODE_MAX_RETRIES = 3   # 沙箱节点:首发 + 至多 3 次自愈
SQL_MAX_RETRIES = 2    # sql_query 节点:首发 + 至多 2 次自愈(对称沙箱节点)


@dataclass
class NodeResult:
    node_id: str
    tool: str
    ok: bool
    value: Any = None              # 解析后的结果(list[dict] / dict)
    code: str = ""                 # 生成的 Python(仅 sandbox 节点)
    attempts: int = 0
    stderr: str = ""
    artifact: dict = field(default_factory=dict)   # 如 plot 的 png_base64


# ── 上游数据注入 ──────────────────────────────

def _inject(code: str, node: Node, upstream: dict[str, Any]) -> str:
    header = "import json\n"
    header += f"inputs = json.loads({json.dumps(node.inputs, ensure_ascii=False)!r})\n"
    for nid, val in upstream.items():
        header += f"data_{nid} = json.loads({json.dumps(val, ensure_ascii=False, default=str)!r})\n"
    return header + "\n" + code


def _parse_stdout(stdout: str) -> Any:
    """节点代码约定 print(json.dumps(...));从 stdout 末尾找可解析的 JSON。"""
    s = stdout.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line and line[0] in "[{":
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"_raw_stdout": s}   # 兜底:解析不出 JSON 就原样带回


# ── 数据获取类(MCP)──────────────────────────

def _run_sql_query(node: Node, schema: dict, trace: Trace) -> NodeResult:
    """sql_query 自愈执行(结构对称 _run_sandbox_node):
    查库失败 → 把 DB 报错回喂 SqlFixer 重写 SQL → 重试,至多 SQL_MAX_RETRIES 次。"""
    sql = node.inputs.get("sql", "")
    fixer: SqlFixer | None = None
    last_err = ""

    for attempt in range(SQL_MAX_RETRIES + 1):
        step = trace.step(f"[{node.id}/sql_query] MCP query (try {attempt + 1})")
        try:
            rows = mcp_client.query_db(sql)
            step.ok(rows=len(rows) if isinstance(rows, list) else 1)
            return NodeResult(node.id, node.tool, ok=True, value=rows, attempts=attempt + 1)
        except Exception as e:
            last_err = str(e)
            will_retry = attempt < SQL_MAX_RETRIES
            step.fail(error=last_err[:160], will_retry=will_retry)
            if not will_retry:
                break
            # 自愈:把 DB 报错回喂,重写 SQL
            rstep = trace.step(f"[{node.id}/sql_query] repair (try {attempt + 1})")
            try:
                fixer = fixer or SqlFixer()
                sql = fixer.repair(sql, last_err, schema or {})
                rstep.ok(sql_len=len(sql))
            except Exception as ge:
                rstep.fail(error=repr(ge))
                return NodeResult(node.id, node.tool, ok=False,
                                  stderr=f"sql repair failed: {ge!r}", attempts=attempt + 1)

    return NodeResult(node.id, node.tool, ok=False, stderr=last_err,
                      attempts=SQL_MAX_RETRIES + 1)


def _run_threshold_sweep(node: Node) -> NodeResult:
    """Stage 9 动态探针:主进程当 MCP 代理,逐阈值代入模板查询并汇总。"""
    template = node.inputs.get("sql_template", "")
    thresholds = node.inputs.get("thresholds", [0.5, 0.6, 0.7, 0.8, 0.9])
    out = []
    for t in thresholds:
        sql = template.replace("{threshold}", str(t))
        rows = mcp_client.query_db(sql)
        # 约定模板返回单行单聚合列;取首行首个数值列
        agg = {}
        if rows:
            agg = {k: v for k, v in rows[0].items()}
        out.append({"threshold": t, **agg})
    return NodeResult(node.id, node.tool, ok=True, value=out, attempts=1)


# ── 沙箱类(CodeGen + Stage 5/6)───────────────

def _run_sandbox_node(node: Node, upstream: dict[str, Any],
                      sandbox: SandboxClient, trace: Trace) -> NodeResult:
    gen = CodeGenerator()
    code = ""
    last = None

    for attempt in range(CODE_MAX_RETRIES + 1):
        step = trace.step(f"[{node.id}/{node.tool}] gen code (try {attempt + 1})")
        try:
            code = gen.generate(node, upstream) if attempt == 0 else gen.repair(
                last.stderr, last.exit_code
            )
            step.ok(code_len=len(code))
        except Exception as e:
            step.fail(error=repr(e))
            return NodeResult(node.id, node.tool, ok=False, code=code,
                              attempts=attempt, stderr=repr(e))

        step = trace.step(f"[{node.id}/{node.tool}] sandbox exec (try {attempt + 1})")
        last = sandbox.execute(_inject(code, node, upstream), timeout=30)

        if last.ok:
            value = _parse_stdout(last.stdout)
            step.ok(stdout_chars=len(last.stdout), elapsed_s=f"{last.elapsed_seconds:.2f}")
            artifact = {}
            if isinstance(value, dict):
                for key in ("svg", "png_base64"):
                    if key in value:
                        artifact[key] = value.pop(key)
            return NodeResult(node.id, node.tool, ok=True, value=value,
                              code=code, attempts=attempt + 1, artifact=artifact)

        will_retry = attempt < CODE_MAX_RETRIES
        step.fail(error=f"exit={last.exit_code}", will_retry=will_retry,
                  policy_violation=last.policy_violation)
        if not will_retry:
            return NodeResult(node.id, node.tool, ok=False, code=code,
                              attempts=attempt + 1, stderr=last.stderr)

    return NodeResult(node.id, node.tool, ok=False, code=code, stderr="unreachable")


# ── 统一入口 ──────────────────────────────────

def execute_node(node: Node, upstream: dict[str, Any],
                 sandbox: SandboxClient, trace: Trace,
                 schema: dict | None = None) -> NodeResult:
    # sql_query:自管 trace + 自愈(对称 _run_sandbox_node)
    if node.tool == "sql_query":
        return _run_sql_query(node, schema or {}, trace)

    # 其它数据节点(threshold_sweep):主进程经 MCP,单次执行
    if not needs_sandbox(node.tool):
        step = trace.step(f"[{node.id}/{node.tool}] MCP query")
        try:
            if node.tool == "threshold_sweep":
                res = _run_threshold_sweep(node)
            else:
                raise ValueError(f"未知数据工具: {node.tool}")
            step.ok(rows=len(res.value) if isinstance(res.value, list) else 1)
            return res
        except Exception as e:
            step.fail(error=str(e)[:160])
            return NodeResult(node.id, node.tool, ok=False, stderr=str(e))

    return _run_sandbox_node(node, upstream, sandbox, trace)
