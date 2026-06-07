"""
第 6 阶段 — 结构化 trace 事件

把 run() 内部的每一步(SQL 生成、SQL 执行、代码生成、Sandbox 执行 ...)
都记成 TraceStep,实时打印 + 收集成 list,后续可 JSON 序列化给 SSE / 前端。

用法(在 loop.py 里):
    trace = Trace()
    step = trace.step("Generating SQL")
    sql = gen.generate_sql(q)
    step.ok(sql_len=len(sql))                      # 成功

    step = trace.step("Sandbox execute (try 1)")
    r = sandbox.execute(code)
    if r.ok:
        step.ok(stdout_chars=len(r.stdout))
    else:
        step.fail(error=f"exit={r.exit_code}", will_retry=True)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Status = Literal["running", "ok", "error", "retry"]

# 用 ASCII 括号代替 emoji ——任何 Windows 控制台都能渲染
GLYPH = {
    "ok":      "[+]",
    "error":   "[x]",
    "retry":   "[~]",
    "running": "[ ]",
}


@dataclass
class TraceStep:
    name: str
    status: Status = "running"
    elapsed_ms: int = 0
    meta: dict = field(default_factory=dict)
    error: str | None = None

    # 仅运行时使用,不进 asdict 输出
    _t0: float = field(default=0.0, repr=False, compare=False)
    _trace: Any = field(default=None, repr=False, compare=False)

    # ── 完成 ──
    def ok(self, **meta_kw):
        self.meta.update(meta_kw)
        self._end("ok")

    def fail(self, error: str = "", will_retry: bool = False, **meta_kw):
        self.meta.update(meta_kw)
        if error:
            self.error = error
        self._end("retry" if will_retry else "error")

    def _end(self, status: Status):
        self.elapsed_ms = int((time.perf_counter() - self._t0) * 1000)
        self.status = status
        if self._trace is not None:
            self._trace._print_done(self)

    # 让 asdict 跳过私有字段
    def public(self) -> dict:
        d = asdict(self)
        d.pop("_t0", None)
        d.pop("_trace", None)
        return d


class Trace:
    """run() 一次性的步骤收集器 + 实时打印。"""

    def __init__(self, quiet: bool = False):
        self.steps: list[TraceStep] = []
        self.quiet = quiet
        self._global_t0 = time.perf_counter()

    def step(self, name: str, **meta) -> TraceStep:
        s = TraceStep(name=name, status="running", meta=dict(meta))
        s._t0 = time.perf_counter()
        s._trace = self
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
        print(f"  {GLYPH[s.status]} {s.name}  {s.elapsed_ms}ms{suffix}{err_suffix}",
              flush=True)

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self._global_t0) * 1000)

    def as_list(self) -> list[dict]:
        return [s.public() for s in self.steps]

    def summary_line(self) -> str:
        n = len(self.steps)
        ok = sum(1 for s in self.steps if s.status == "ok")
        return f"trace: {ok}/{n} steps ok, total {self.total_ms}ms"
