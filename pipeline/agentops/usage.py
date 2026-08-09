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
    # 阶段B 候选(DashScope 国际站 2026-07 牌价,基础档;cached 按 ~80% 折扣估)
    "qwen3.7-plus":     {"in": 0.40, "out": 1.60, "cached": 0.08},
    "qwen3.6-flash":    {"in": 0.25, "out": 1.50, "cached": 0.05},
}

# P0-1(长程引擎前置):价目表外的模型不再静默跳过计价 —— 那会让任何美元闸门失明
# (红队 B2:SUBAGENT_MODEL/OAI 兼容通道可填任意模型名,填了就等于把熔断关掉)。
# 处置=按表内【最贵】单价估价(宁可高估提前触闸)+ 在 summarize 里列出 unpriced_models 供告警。
# 刻意不做前缀匹配:"gemini-2.5-flash-thinking-exp" 猜成 2.5-flash 是静默的错答案,
# 而按最贵估价 + 报出名字是响亮的近似 —— 后者可被发现、可被修表。
_MAX_PRICE = {
    "in":     max(p["in"] for p in _PRICE.values()),
    "out":    max(p["out"] for p in _PRICE.values()),
    "cached": max(p.get("cached", p["in"]) for p in _PRICE.values()),
}


def _norm_model(model: str) -> str:
    """"models/gemini-2.5-flash" → "gemini-2.5-flash"(SDK 有时带前缀)。仅剥前缀,不猜别名。"""
    m = str(model or "")
    return m[len("models/"):] if m.startswith("models/") else m


def _thinking_tokens(d: dict) -> int:
    """本模型累计的思考 token(官方按 output 价计费)。

    正确口径由 add_usage 在【每一次调用】上算好并累加进 d["thought"] —— 审查 HIGH:
    在累加后的和上取 max 是错的(某次 total 缺失=0 时,它的 in+out 会从别人的 total
    里被减掉,已推出的思考量凭空消失,累计成本甚至能随消耗下降)。
    旧 dict(跨版本共享 contextvar)无 thought 键时才退回和级推导。
    """
    if "thought" in d:
        return int(d["thought"])
    derived = (int(d.get("total", 0)) - int(d.get("in", 0)) - int(d.get("out", 0))
               - int(d.get("tool", 0)))
    return max(derived, 0)


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
    key = _norm_model(model)
    # ── 本次调用的五个量(必须在单次上算思考量,不能在累加和上算,见 _thinking_tokens)──
    c_in    = getattr(m, "prompt_token_count", 0) or 0
    c_out   = getattr(m, "candidates_token_count", 0) or 0
    c_total = getattr(m, "total_token_count", 0) or 0
    c_cache = getattr(m, "cached_content_token_count", 0) or 0
    # 工具用提示 token(grounding/web_search 把搜索结果回灌的那批):独立字段,含在 total、
    # 不含在 prompt。审查 HIGH:不单独扣掉它,derived 会把这批【输入】当思考按 out 价计,
    # 单次 grounded 调用成本虚高 ~5.5×。
    c_tool  = getattr(m, "tool_use_prompt_token_count", 0) or 0
    # 思考量 = 显式字段与"总量减去其余三项"取较大者(任一缺失时另一个仍成立);绝不少记。
    c_think = max(getattr(m, "thoughts_token_count", 0) or 0,
                  c_total - c_in - c_out - c_tool, 0)
    with _LOCK:                         # 并行 worker 共享同一 dict → 增量需互斥
        d = u.setdefault(key, {"in": 0, "out": 0, "total": 0, "calls": 0,
                               "cached": 0, "thought": 0, "tool": 0})
        for k in ("thought", "tool"):   # 兼容旧 dict(跨版本共享同一 contextvar 时)
            d.setdefault(k, 0)
        d["in"]      += c_in
        d["out"]     += c_out
        d["total"]   += c_total
        d["cached"]  += c_cache         # L3:隐式缓存命中的输入(prompt 的子集,便宜 ~10x)
        d["thought"] += c_think         # P0-1:按 out 价计价
        d["tool"]    += c_tool          # 按 in 价计价
        d["calls"]   += 1


def get_usage() -> dict:
    """取回 {model: {in,out,total,calls}}(未 reset 时为空 dict)。"""
    return _USAGE.get() or {}


def summarize(usage: dict | None = None) -> dict:
    """{model:{in,out,total,calls}} → 扁平总计 + 按模型单价估算的成本。

    并发安全:P0-3 让本函数进了热路径(treeguard 每次放行判定都读),而并行 worker 的
    add_usage 可能同时 setdefault 一个【新】模型键(analyze/子 agent 模型首调即插键)——
    无锁遍历活 dict 会 RuntimeError(dictionary changed size during iteration),被
    treeguard.spent() 吞掉后,闸门恰在新钱落账的那一刻读到陈旧值多放行一次(review 确认)。
    锁内浅拷贝一次,开销可忽略。"""
    if usage is None:
        with _LOCK:
            usage = {k: dict(v) for k, v in get_usage().items()}
    tin   = sum(d["in"]    for d in usage.values())
    tout  = sum(d["out"]   for d in usage.values())
    ttot  = sum(d["total"] for d in usage.values())
    calls = sum(d["calls"] for d in usage.values())
    tcach = sum(d.get("cached", 0) for d in usage.values())
    tthk  = sum(_thinking_tokens(d) for d in usage.values())
    ttool = sum(int(d.get("tool", 0)) for d in usage.values())
    cost, unpriced = 0.0, []
    for model, d in usage.items():
        p = _PRICE.get(_norm_model(model))
        if not p:                                     # P0-1:不再 continue(那等于记 $0)
            unpriced.append(model)
            p = _MAX_PRICE                            # 按最贵估价,宁可早触闸
        cached = min(d.get("cached", 0), d["in"])     # cached ⊂ in;防脏数据把成本算成负
        cost += ((d["in"] - cached) / 1e6 * p["in"]
                 + cached / 1e6 * p.get("cached", p["in"])
                 + int(d.get("tool", 0)) / 1e6 * p["in"]     # 工具用提示 = 输入价
                 # 思考 token 与输出同价(官方口径)—— 漏记它 = 熔断对 agent 深跑失明
                 + (d["out"] + _thinking_tokens(d)) / 1e6 * p["out"])
    return {
        "tokens_in":    tin,
        "tokens_out":   tout,
        "tokens_total": ttot,
        "tokens_cached": tcach,          # L3:命中隐式缓存的输入(已按折扣价计入 cost)
        "tokens_thought": tthk,          # P0-1:思考 token(已按 out 价计入 cost)
        "tokens_tool": ttool,            # P0-1:工具用提示 token(grounding 回灌,按 in 价)
        "llm_calls":    calls,
        "cost_usd":     round(cost, 6),
        "unpriced_models": sorted(set(unpriced)),   # 非空 = 计价用了兜底价,调用方应告警
        "by_model":     usage,
    }
