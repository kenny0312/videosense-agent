"""
第6阶段 — Agentic REPL: 主循环(P0 强化版)

改进点(相对初版):
  1. SQL 阶段也加自愈 —— SQL 执行错可重试 1 次
  2. 全程用 Trace 记录每步:实时打印 + 收集成结构化事件
  3. code_history 上限 5 条(由 generator 内部控制),防止 prompt 膨胀

流程:
  1. user_question
  2. Gemini 写 SQL → 主进程 run_sql 拿 data
     失败 → repair_sql → 再试 1 次
  3. Gemini 写分析代码(把 data 作为 JSON 注入)→ Sandbox 执行
  4. exit_code == 0:返回 stdout;否则 stderr 喂回 Gemini 重试,最多 3 次
"""

import json
import logging

from repl.generator import CodeGenerator, run_sql
from repl.trace import Trace
from sandbox.client import SandboxClient, ExecuteResult

SQL_MAX_RETRIES  = 1   # SQL 阶段:首发 + 至多 1 次自愈 = 2 次尝试
CODE_MAX_RETRIES = 3   # 代码阶段:首发 + 至多 3 次自愈 = 4 次尝试

log = logging.getLogger("repl.loop")


def _inject_data(code: str, data: list[dict]) -> str:
    """把 data 以 JSON 字面量形式注入到代码顶部。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    header = (
        "import json\n"
        f"data = json.loads({payload!r})\n"
    )
    return header + "\n" + code


def _summarise_data(data: list[dict], max_rows: int = 3) -> str:
    """给 LLM 看的小预览,避免把全表塞进 prompt。"""
    n = len(data)
    sample = data[:max_rows]
    return (
        f"data 共 {n} 行,前 {len(sample)} 行预览:\n"
        f"{json.dumps(sample, ensure_ascii=False, default=str, indent=2)}"
    )


# ── 结果包装 ──────────────────────────────────

def _fail(trace, *, phase: str, error: str, sql: str = "",
          last_stderr: str = "", attempts: int = 0) -> dict:
    return {
        "ok": False,
        "answer": "",
        "sql": sql,
        "attempts": attempts,
        "last_stderr": last_stderr or error,
        "fail_phase": phase,
        "trace": trace.as_list(),
        "trace_summary": trace.summary_line(),
    }


def _ok(trace, *, stdout: str, sql: str, attempts: int) -> dict:
    return {
        "ok": True,
        "answer": stdout,
        "sql": sql,
        "attempts": attempts,
        "last_stderr": "",
        "fail_phase": None,
        "trace": trace.as_list(),
        "trace_summary": trace.summary_line(),
    }


# ── 主入口 ───────────────────────────────────

def run(question: str, *, quiet_trace: bool = False) -> dict:
    """
    返回 dict 字段:
        ok, answer, sql, attempts, last_stderr,
        fail_phase(失败阶段名 / None), trace(list), trace_summary
    """
    trace = Trace(quiet=quiet_trace)
    gen = CodeGenerator()
    sandbox = SandboxClient()

    # ════════════════════════════════════════
    #  SQL 阶段(带自愈)
    # ════════════════════════════════════════
    sql: str = ""
    data: list[dict] = []
    last_sql_error: str = ""

    for sql_try in range(SQL_MAX_RETRIES + 1):
        # —— 生成 / 修复 SQL ——
        step = trace.step(
            f"Generating SQL (try {sql_try + 1})" if sql_try == 0
            else f"Repairing SQL (try {sql_try + 1})"
        )
        try:
            if sql_try == 0:
                sql = gen.generate_sql(question)
            else:
                sql = gen.repair_sql(question, sql, last_sql_error)
            step.ok(sql_len=len(sql))
        except Exception as e:
            step.fail(error=repr(e))
            return _fail(trace, phase="sql_gen", error=repr(e))

        # —— 执行 SQL ——
        step = trace.step(f"Executing SQL (try {sql_try + 1})")
        try:
            data = run_sql(sql)
            step.ok(rows=len(data))
            break   # 成功 → 跳出 SQL 重试循环
        except Exception as e:
            last_sql_error = str(e)
            will_retry = sql_try < SQL_MAX_RETRIES
            step.fail(error=last_sql_error[:120], will_retry=will_retry)
            if not will_retry:
                return _fail(
                    trace, phase="sql_exec",
                    error=last_sql_error, sql=sql,
                )

    # ════════════════════════════════════════
    #  代码阶段(自愈循环)
    # ════════════════════════════════════════
    gen.reset_code_history()
    first_msg = (
        f"用户问题:{question}\n\n"
        f"{_summarise_data(data)}\n\n"
        "请写 Python 代码对 data 进行分析并 print 结果。"
    )

    last_result: ExecuteResult | None = None

    for code_try in range(CODE_MAX_RETRIES + 1):
        msg = first_msg if code_try == 0 else (
            f"上一次执行失败,报错如下,请修复后重新生成完整代码:\n\n"
            f"--- stderr ---\n{last_result.stderr}\n"
            f"--- exit_code: {last_result.exit_code} ---"
        )

        # —— 生成代码 ——
        step = trace.step(f"Generating code (try {code_try + 1})")
        try:
            code = gen.generate_code(msg)
            step.ok(code_len=len(code))
        except Exception as e:
            step.fail(error=repr(e))
            return _fail(
                trace, phase="code_gen", error=repr(e),
                sql=sql, attempts=code_try,
            )

        # —— Sandbox 执行 ——
        step = trace.step(f"Sandbox execute (try {code_try + 1})")
        full_code = _inject_data(code, data)
        last_result = sandbox.execute(full_code, timeout=30)

        if last_result.ok:
            step.ok(stdout_chars=len(last_result.stdout),
                    elapsed_s=f"{last_result.elapsed_seconds:.2f}")
            return _ok(
                trace,
                stdout=last_result.stdout, sql=sql,
                attempts=code_try + 1,
            )

        # 失败:决定是否重试
        will_retry = code_try < CODE_MAX_RETRIES
        step.fail(error=f"exit={last_result.exit_code}",
                  will_retry=will_retry,
                  policy_violation=last_result.policy_violation)
        if not will_retry:
            return _fail(
                trace, phase="code_exec",
                error="all retries exhausted",
                sql=sql, attempts=code_try + 1,
                last_stderr=last_result.stderr,
            )

    # 理论不可达
    return _fail(trace, phase="unreachable", error="impossible state")
