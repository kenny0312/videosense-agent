"""
结构化 trace 事件 —— 实时打印 + 收集成可序列化 list(供 SSE / API 返回前端)。

用法(与升级前完全兼容):
    trace = Trace()
    step = trace.step("Planning DAG")
    dag = planner.plan(q)
    step.ok(nodes=len(dag.nodes))                    # 成功
    step.fail(error="...", will_retry=True)          # 失败/重试

长程引擎新增(P0-2 / T-1 / T-2,docs/longhorizon-master-plan.md):
    树结构:每个 step 带 span_id / parent_id / depth / component / t_start / t_end,
            于是"钱和时间花在树的哪个枝上"可回答(升级前是扁平列表,子 agent 还裸共享父对象)。
    因由码:失败必打枚举 cause(见 CAUSES),禁自由文本 —— 测试挂了能直接指认【哪一层设计】出问题。
            忘了打 → 自动标 cause_missing;打了表外的 → 标 cause_unknown。静默失败不再可能。
    子 span:sub = trace.child(parent_span_id=s.span_id, component="exec", depth=1)
            子 agent 用 sub 记步,步进同一条 steps 列表但带上父子关系(线程安全)。
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["running", "ok", "error", "retry", "softfail", "refused"]

GLYPH = {"ok": "[+]", "error": "[x]", "retry": "[~]", "running": "[ ]",
         "softfail": "[!]",      # 软失败:护栏拦下但流程继续(如熔断后教收口)
         "refused": "[/]"}       # 拒绝执行:结构性不允许(如并行调用的第二个工具)

# ── T-2 因由码:设计层 → 允许的因由码 ─────────────────────────────
# 一条失败路径必打其一。加新码就在这里加 —— 枚举集中一处,triage 才能按层聚合。
COMPONENTS = ("main", "gate", "plan", "exec", "agg", "guard", "sub", "relay")
CAUSES: dict[str, frozenset] = {
    "main":  frozenset({"MAIN_UNEXPECTED", "MAIN_TOOL_ERROR"}),
    "gate":  frozenset({"GATE_MISFIRE", "GATE_MISS"}),
    "plan":  frozenset({"PLAN_EMPTY", "PLAN_OVERSPLIT", "PLAN_BAD_TASK"}),
    "exec":  frozenset({"EXEC_TOOL_ERROR", "EXEC_NO_EVIDENCE", "EXEC_TRAP",
                        "EXEC_NOT_CONVERGED"}),
    "agg":   frozenset({"AGG_TRUNCATED", "AGG_EVIDENCE_FAIL"}),
    "guard": frozenset({"GUARD_BUDGET", "GUARD_WALL", "GUARD_NODE_CAP",
                        "GUARD_CANCELLED"}),
    "sub":   frozenset({"SUB_CLAIM_RACE", "SUB_ZOMBIE_DISCARD", "SUB_ENQUEUE_FAIL",
                        "SUB_LEASE_TIMEOUT", "SUB_REPLAN_REFUSED"}),
    "relay": frozenset({"RELAY_DUP_SUPPRESSED", "RELAY_REPORT_MISS"}),
}
ALL_CAUSES = frozenset().union(*CAUSES.values())

# span_id 用模块级计数器,不用 len(steps) —— 并行子 agent 下 len 会撞号(红队 A5)。
_span_seq = itertools.count(1)
_seq_lock = threading.Lock()


def _next_span_id() -> str:
    with _seq_lock:
        return f"s{next(_span_seq)}"


@dataclass
class TraceStep:
    name: str
    status: Status = "running"
    elapsed_ms: int = 0
    meta: dict = field(default_factory=dict)
    error: str | None = None
    # ── T-1 树结构与关联键 ──
    span_id: str = ""
    parent_id: str | None = None
    depth: int = 0
    component: str = "main"
    cause: str | None = None            # 失败时必填(枚举,见 CAUSES)
    t_start: float = 0.0                # epoch 秒(跨进程可对齐)
    t_end: float = 0.0
    tok: dict = field(default_factory=dict)     # {in,out,thought,cached}
    cost_usd: float = 0.0

    _t0: float = field(default=0.0, repr=False, compare=False)
    _trace: Any = field(default=None, repr=False, compare=False)

    # ── 收尾三态 ──
    def ok(self, **meta_kw):
        self.meta.update(meta_kw)
        self._end("ok")

    def fail(self, error: str = "", will_retry: bool = False, cause: str | None = None,
             **meta_kw):
        self.meta.update(meta_kw)
        if error:
            self.error = error
        self._set_cause(cause)
        self._end("retry" if will_retry else "error")

    def soft(self, cause: str, error: str = "", **meta_kw):
        """软失败:护栏拦下但流程继续(熔断/取消/拒绝并行调用)。cause 必填。"""
        self.meta.update(meta_kw)
        if error:
            self.error = error
        self._set_cause(cause)
        self._end("softfail")

    def refuse(self, cause: str, **meta_kw):
        """结构性拒绝执行(不是出错,是不允许)。"""
        self.meta.update(meta_kw)
        self._set_cause(cause)
        self._end("refused")

    # ── 记账 ──
    def bill(self, tok: dict | None = None, cost_usd: float = 0.0):
        """把本 span 的 token 账与成本【累加】上来(可多次调:一个 span 内自愈重试会多次计费)。

        审查 MED:早期版本是覆盖赋值 —— 第二次 bill 会吞掉第一次的钱,
        只传 tok 的 bill 还会把已记成本清零;span 级金额是按枝熔断与对照表的输入,少记即失真。
        NaN 防呆:NaN 一旦进账,`nan > cap` 恒为 False = 永久关掉闸门,且会写出非法 JSON。
        """
        import math
        if tok:
            for k in ("in", "out", "thought", "tool", "cached"):
                v = tok.get(k, 0) or 0
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = 0
                if v:
                    self.tok[k] = int(self.tok.get(k, 0)) + v
        try:
            add = float(cost_usd or 0.0)
        except (TypeError, ValueError):
            add = 0.0
        if not math.isfinite(add):
            add = 0.0
            self.meta["cost_nonfinite"] = True      # 留痕:上游算出了 NaN/inf,是它的 bug
        if add:
            self.cost_usd = round(self.cost_usd + add, 6)
        return self

    # ── 内部 ──
    def _set_cause(self, cause: str | None):
        """因由码纪律:非 main 层失败必须给码;给了表外码也留痕。fail-open,绝不抛。"""
        if cause:
            self.cause = cause
            if cause not in ALL_CAUSES:
                self.meta["cause_unknown"] = True
            elif cause not in CAUSES.get(self.component, frozenset()):
                self.meta["cause_component_mismatch"] = self.component
        elif self.component != "main":
            self.meta["cause_missing"] = True

    def _end(self, status: Status):
        self.elapsed_ms = int((time.perf_counter() - self._t0) * 1000)
        self.t_end = time.time()
        self.status = status
        if self._trace is not None:
            self._trace._print_done(self)

    def public(self) -> dict:
        # 显式构造,不用 asdict():它会递归深拷贝,而 _trace 里现在有 threading.Lock
        # (不可深拷贝)。顺带省掉对 meta 的无谓深拷贝。
        return {"name": self.name, "status": self.status, "elapsed_ms": self.elapsed_ms,
                "meta": dict(self.meta), "error": self.error,
                "span_id": self.span_id, "parent_id": self.parent_id, "depth": self.depth,
                "component": self.component, "cause": self.cause,
                "t_start": self.t_start, "t_end": self.t_end,
                "tok": dict(self.tok), "cost_usd": self.cost_usd}


class Trace:
    """一次请求(或一个任务波次)的 span 收集器。

    child() 派生的子 Trace 与父共享同一 steps 列表(所以最终仍是一份可序列化的全树),
    但自带 component/depth/parent_span —— 子 agent 用它记步,不再裸共享父对象。
    """

    def __init__(self, quiet: bool = False, *, component: str = "main", depth: int = 0,
                 parent_span: str | None = None, _steps: list | None = None,
                 _lock: threading.Lock | None = None, _t0: float | None = None):
        self.steps: list[TraceStep] = [] if _steps is None else _steps
        self.quiet = quiet
        self.component = component
        self.depth = depth
        self.parent_span = parent_span
        self._append_lock = _lock or threading.Lock()
        self._global_t0 = time.perf_counter() if _t0 is None else _t0

    def child(self, parent_span_id: str | None = None, component: str = "exec",
              depth: int | None = None) -> "Trace":
        """派生子 Trace(共享 steps 与锁)。子 agent / 子任务用它记步。"""
        return Trace(quiet=self.quiet, component=component,
                     depth=self.depth + 1 if depth is None else depth,
                     parent_span=parent_span_id or self.parent_span,
                     _steps=self.steps, _lock=self._append_lock, _t0=self._global_t0)

    def step(self, name: str, *, component: str | None = None,
             parent_id: str | None = None, **meta) -> TraceStep:
        s = TraceStep(name=name, status="running", meta=dict(meta),
                      span_id=_next_span_id(),
                      parent_id=parent_id if parent_id is not None else self.parent_span,
                      depth=self.depth,
                      component=component or self.component,
                      t_start=time.time())
        s._t0 = time.perf_counter()
        s._trace = self
        with self._append_lock:          # 并行子 agent 同时 append
            self.steps.append(s)
        return s

    def _print_done(self, s: TraceStep):
        if self.quiet:
            return
        suffix = ""
        if s.meta:
            kv = ", ".join(f"{k}={v}" for k, v in s.meta.items())
            suffix = f"  ({kv})"
        err_suffix = f"  -> {s.error}" if s.error else ""
        cause = f" <{s.cause}>" if s.cause else ""
        money = f" ${s.cost_usd:.4f}" if s.cost_usd else ""
        indent = "  " * (s.depth + 1)
        print(f"{indent}{GLYPH.get(s.status, '[?]')} {s.name}{cause}  "
              f"{s.elapsed_ms}ms{money}{suffix}{err_suffix}", flush=True)

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self._global_t0) * 1000)

    def as_list(self) -> list[dict]:
        return [s.public() for s in self.steps]

    # ── 汇总:给验收与 triage 用 ──
    def total_cost(self) -> float:
        return round(sum(s.cost_usd for s in self.steps), 6)

    def failures(self) -> list[TraceStep]:
        return [s for s in self.steps if s.status in ("error", "softfail", "refused")]

    def cause_counts(self) -> dict:
        """{因由码: 次数} —— triage 的最小内核(scripts/trace_report.py 复用)。"""
        out: dict[str, int] = {}
        for s in self.failures():
            key = s.cause or f"({s.component}:NO_CAUSE)"
            out[key] = out.get(key, 0) + 1
        return out

    def summary_line(self) -> str:
        n = len(self.steps)
        ok = sum(1 for s in self.steps if s.status == "ok")
        money = f", ${self.total_cost():.4f}" if self.total_cost() else ""
        depth = max((s.depth for s in self.steps), default=0)
        return (f"trace: {ok}/{n} steps ok, depth {depth}, total {self.total_ms}ms{money}")


# ── T-1 落盘:线上 trace 只活在内存环里(loop_console._RING),进程一死即无。
# 测试/跑批必须留下可事后诊断的凭证 —— 这一个函数就是 scripts/trace_report.py 的数据源。
def dump_trace(trace: "Trace", path: str, **meta) -> str:
    """原子写一份 trace JSON(tmp+replace,崩在写一半不留坏文件)。返回落盘路径。"""
    import json
    import os
    payload = {**meta, "steps": trace.as_list(),
               "total_ms": trace.total_ms, "total_cost_usd": trace.total_cost(),
               "cause_counts": trace.cause_counts()}
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)
    return path
