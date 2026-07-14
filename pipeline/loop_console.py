"""Loop Console 数据环:最近 N 次请求的"大脑决策全息"(内存态,进程内,重启即清)。

给 /console 开发者页面供数据:每次问答记一条 —— 问题、prompt 各节的体积、
大脑每一步选了什么工具/进出/耗时、扇出的子 agent、答案与清洗命中、终止原因。
纯旁路:record() 全程 fail-open,绝不影响作答;不落盘(含内部数据,只活在受
Basic 鉴权保护的进程内存里)。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque

try:
    KEEP = int(os.environ.get("LOOP_CONSOLE_KEEP", "50"))
except ValueError:
    KEEP = 50
_RING: deque = deque(maxlen=KEEP)
_LOCK = threading.Lock()          # 读写都过锁:迭代 deque 时另一线程 appendleft 会 RuntimeError


def _cap(v, n=160) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def record(*, query: str, owner: str, lo, ledger: dict,
           runtime_facts: "str | None" = None, replay_chars: int = 0,
           system_chars: int = 0, schema_chars: int = 0, total_ms: float = 0.0) -> None:
    """收一条请求全息。fail-open:任何异常吞掉(旁路观测不许伤主流程)。
    注意:环是【每个 worker/实例各一份】,多 worker 部署时列表只反映本实例。"""
    try:
        from pipeline import lessons
        from pipeline.dag_schema import ALL_TOOLS
        steps = []
        for s in (lo.trace or []):
            er = ledger.get(s.get("cid"))
            tool = s.get("tool")
            if tool not in ALL_TOOLS:
                tool = "unknown_tool"   # 工具名是模型可控输出(校验前已入 trace)——白名单归一,XSS 载荷不入环
            inputs = s.get("inputs")    # trace 的真实键名是 inputs(审查抓的:读 args 恒空)
            try:
                args = json.dumps(inputs, ensure_ascii=False, default=str) if inputs else ""
            except Exception:
                args = str(inputs)
            ok = bool(s.get("ok"))
            # 成功步给 preview;失败步给 stderr(失败原因在 ExecResult.stderr,不在 trace)
            out = ((getattr(er, "preview", None) if ok
                    else (getattr(er, "stderr", "") or getattr(er, "preview", None)))
                   if er else "")
            steps.append({
                "tool": tool, "ok": ok,
                "ms": round(float(s.get("ms", 0.0)), 1),
                "args": _cap(args, 200),
                "rows": getattr(er, "n", None) if er else None,
                "out": _cap(out, 240),
                "cache_hit": bool(s.get("cache_hit")),
                "sub": tool == "spawn_agents",       # 扇出步(子 agent 明细在 out 里)
            })
        rec = ({
            "id": uuid.uuid4().hex[:10],
            "ts": time.strftime("%m-%d %H:%M:%S"),
            "owner": owner,
            "query": _cap(query, 300),
            "prompt": {                                       # 这一轮大脑"脑子里装了什么"
                "system_chars": system_chars,                  # 宪法+教训+数据事实(字节稳定段)
                "schema_chars": schema_chars,
                "runtime_facts": _cap(runtime_facts, 500),     # 自我认知一行(每轮变)
                "replay_chars": replay_chars,                  # 多轮回放段
                "lessons_count": len(lessons.LESSONS),
            },
            "steps": steps,
            "answer": _cap(lo.answer, 500),                    # 已过清洗(run_query_loop 收口后)
            "terminated": lo.terminated,
            "n_steps": lo.steps,
            "scrub_hits": getattr(lo, "id_scrub_hits", 0),
            "tool_ms": round(sum(getattr(lo, "step_walls", None) or []), 1),
            "total_ms": round(float(total_ms), 1),   # 请求真墙钟(含 LLM 生成;tool_ms 只是工具段)
        })
        with _LOCK:
            _RING.appendleft(rec)
    except Exception:
        pass


def list_traces() -> list:
    """轻量列表(左栏):不含步骤明细。"""
    with _LOCK:
        snap = list(_RING)
    return [{k: t[k] for k in ("id", "ts", "owner", "query", "n_steps", "terminated", "total_ms")}
            for t in snap]


def get_trace(tid: str) -> "dict | None":
    with _LOCK:
        snap = list(_RING)
    for t in snap:
        if t["id"] == tid:
            return t
    return None
