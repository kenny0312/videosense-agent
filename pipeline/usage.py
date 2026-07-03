"""
每请求 LLM token 记账(使用审计用)。

run_query 期间,各模型调用点(loop 大脑 / analyze_video / code_generator / sql_fixer /
web_search / 子 agent 等)在 generate_content 之后调一次 add_usage(resp, model),把 resp.usage_metadata
累加进一个 contextvar。orchestrator 在 run_query 开头 reset_usage()、收尾时
summarize() 取回扁平总计 + 估算成本,塞进返回的 result。

刻意做成最薄的一层:无类、无外部依赖、全程 fail-open —— 拿不到 usage_metadata
绝不抛错(与代码库一贯的 fail-open 风格一致)。用 contextvar 而非普通全局,
因为 FastAPI 并发跑请求,普通全局会在并发查询间串味。
"""
from __future__ import annotations

import contextvars
import threading

_USAGE: contextvars.ContextVar = contextvars.ContextVar("llm_usage", default=None)
# M4.3:并行 analyze worker 经 copy_context 共享同一 usage dict(按引用),
# 增量(d["in"] += …)是读-改-写,需互斥防丢更新。串行下无竞争、开销可忽略。
_LOCK = threading.Lock()

# 估算单价(USD / 1M tokens)。仅用于"谁烧得多"的相对归因;绝对花费以 GCP 账单为准
# (上下文缓存、计费四舍五入、赠金都会偏差)。按需更新:
#   https://cloud.google.com/vertex-ai/generative-ai/pricing
_PRICE = {
    # cached = 隐式缓存命中部分的单价(L3:静态前缀字节稳定后自动命中,已实测;
    # 2.5 系 = 75% 折扣,3.5-flash 官方 cached 价 $0.15)
    "gemini-2.5-pro":   {"in": 1.25, "out": 10.0, "cached": 0.3125},
    "gemini-2.5-flash": {"in": 0.30, "out": 2.50, "cached": 0.075},
    "gemini-3.5-flash": {"in": 1.50, "out": 9.00, "cached": 0.15},   # U5:global 端点价
}


def reset_usage() -> None:
    """每个请求(run_query)开头调一次,清空累加器。"""
    _USAGE.set({})


def add_usage(resp, model: str) -> None:
    """在每个 generate_content 之后调用;fail-open。

    放在底层 _call/repair/_gen/judge 里 → 自愈重试的 token 也自动算进去。
    """
    u = _USAGE.get()
    if u is None:                       # 没 reset(如单测直接调 Planner)→ 静默跳过
        return
    m = getattr(resp, "usage_metadata", None)
    if not m:
        return
    with _LOCK:                         # 并行 worker 共享同一 dict → 增量需互斥
        d = u.setdefault(model, {"in": 0, "out": 0, "total": 0, "calls": 0, "cached": 0})
        d["in"]    += getattr(m, "prompt_token_count", 0) or 0
        d["out"]   += getattr(m, "candidates_token_count", 0) or 0
        d["total"] += getattr(m, "total_token_count", 0) or 0
        d["calls"] += 1
        # L3:隐式缓存命中的输入 tokens(是 prompt_token_count 的子集,便宜 ~10x;两 SDK 同字段名)
        d["cached"] += getattr(m, "cached_content_token_count", 0) or 0


def get_usage() -> dict:
    """取回 {model: {in,out,total,calls}}(未 reset 时为空 dict)。"""
    return _USAGE.get() or {}


def summarize(usage: dict | None = None) -> dict:
    """{model:{in,out,total,calls}} → 扁平总计 + 按模型单价估算的成本。"""
    usage = usage if usage is not None else get_usage()
    tin   = sum(d["in"]    for d in usage.values())
    tout  = sum(d["out"]   for d in usage.values())
    ttot  = sum(d["total"] for d in usage.values())
    calls = sum(d["calls"] for d in usage.values())
    tcach = sum(d.get("cached", 0) for d in usage.values())
    cost = 0.0
    for model, d in usage.items():
        p = _PRICE.get(model)
        if not p:
            continue
        cached = min(d.get("cached", 0), d["in"])     # cached ⊂ in;防脏数据把成本算成负
        cost += ((d["in"] - cached) / 1e6 * p["in"]
                 + cached / 1e6 * p.get("cached", p["in"])
                 + d["out"] / 1e6 * p["out"])
    return {
        "tokens_in":    tin,
        "tokens_out":   tout,
        "tokens_total": ttot,
        "tokens_cached": tcach,          # L3:命中隐式缓存的输入(已按折扣价计入 cost)
        "llm_calls":    calls,
        "cost_usd":     round(cost, 6),
        "by_model":     usage,
    }
