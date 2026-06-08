"""
结构化 trace 事件 —— 实时打印 + 收集成可序列化 list(供 SSE / API 返回前端)。

用法:
    trace = Trace()
    step = trace.step("Planning DAG")
    dag = planner.plan(q)
    step.ok(nodes=len(dag.nodes))          # 成功
    step.fail(error="...", will_retry=True) # 失败/重试
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Status = Literal["running", "ok", "error", "retry"]

GLYPH = {"ok": "[+]", "error": "[x]", "retry": "[~]", "running": "[ ]"}


@dataclass
class TraceStep:
    name: str
    status: Status = "running"
    elapsed_ms: int = 0
    meta: dict = field(default_factory=dict)
    error: str | None = None

    _t0: float = field(default=0.0, repr=False, compare=False)
    _trace: Any = field(default=None, repr=False, compare=False)

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

    def public(self) -> dict:
        d = asdict(self)
        d.pop("_t0", None)
        d.pop("_trace", None)
        return d


class Trace:
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
        print(f"  {GLYPH[s.status]} {s.name}  {s.elapsed_ms}ms{suffix}{err_suffix}", flush=True)

    @property
    def total_ms(self) -> int:
        return int((time.perf_counter() - self._global_t0) * 1000)

    def as_list(self) -> list[dict]:
        return [s.public() for s in self.steps]

    def summary_line(self) -> str:
        n = len(self.steps)
        ok = sum(1 for s in self.steps if s.status == "ok")
        return f"trace: {ok}/{n} steps ok, total {self.total_ms}ms"
