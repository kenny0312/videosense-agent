"""P0-2:滥用/账单护栏 —— 按【成本(美元)】+【请求速率】双口径限流。

为什么按成本、不按请求数:LLM 的风险单位是 token/美元,不是请求数 —— 一条塞满上下文的
请求可能是普通请求的 100 倍成本。所以这里限的是「这个用户/会话/全站今天烧了多少钱」,
另配一道「每分钟几次」挡住突发洪水。

四个维度(纵深防御):
  · 每 IP 每分钟请求数   —— 挡匿名洪水(Cloud Run 在 Google 代理后 → 取 XFF 最左,见 _client_ip)
  · 每用户每分钟请求数   —— 挡单账号连点
  · 每用户每日成本 $     —— 单账号当日花费顶
  · 每会话累计成本 $     —— 单条会话烧穿 → 要求开新会话
  · 全局每日成本 $       —— 全站熔断:一个人打满自己额度也波及不到别人,总账单有顶

分两拍(token/成本只有请求跑完才知道):
  precheck() 请求【前】:看「到目前为止的累计」是否已超顶,超了直接给拒绝理由;顺带 INCR 分钟计数。
  record()   请求【后】:把这次实际烧掉的成本 INCRBYFLOAT 进当日/会话桶。
跨线那一条请求会小幅超顶(与 GCP 账单 ~10min 延迟同理),可接受。

匿名/guest 走更小额度(小额度 tier)—— 面向公众时「登录当闸门,匿名给极小额度」。
anon 共享一个桶(与 uploads 配额同理),named 用户各自独立。

⚠️ fail-open:Redis 不可用 → 放行(不拿整站陪一次 Redis 抖动)。所以真正的【硬底线】是
   provider 侧的 Gemini 月度花费上限(见 docs/billing-guardrails.md);本限流器是「省得打到
   那道闸」的应用层软护栏,不是唯一防线。整体开关 USE_RATE_LIMIT;无 Redis 时本就 no-op。
"""
from __future__ import annotations

import datetime
import threading
import time

from pipeline import config

_CLIENT = None
_TRIED = False
_CLIENT_LOCK = threading.Lock()


def _redis():
    global _CLIENT, _TRIED
    if _TRIED:                                        # 已建好:无锁快路
        return _CLIENT
    with _CLIENT_LOCK:                                # 首建:互斥 + 双检,防并发重复 init / 泄漏连接
        if not _TRIED:
            try:
                from pipeline.redis_client import build_redis_client
                _CLIENT = build_redis_client()
            except Exception:
                _CLIENT = None
            _TRIED = True
    return _CLIENT


def _small_tier(owner: str) -> bool:
    """匿名 / guest* = 小额度档(与 server._is_guest 同一约定;anon 无口令时也归此档)。"""
    o = (owner or "").lower()
    return o in ("", "anon") or o.startswith("guest")


def _today() -> str:
    return datetime.date.today().isoformat()


def _to_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def precheck(owner: str, ip: str | None, session_id: str | None) -> str | None:
    """请求【前】护栏。返回 None = 放行;返回一句中文理由 = 该拒(调用方回 429)。

    分钟计数用 INCR(会把本次也算进去 → 第 N+1 次被挡);成本桶只 GET 对比(累加在 record)。
    """
    if not config.USE_RATE_LIMIT:
        return None
    r = _redis()
    if r is None:                                     # 无 Redis → fail-open(见模块注释:硬底线在 provider spend cap)
        return None

    small = _small_tier(owner)
    win = int(time.time() // 60)                      # 当前分钟窗(key 带窗号 → 自动滚动)
    try:
        # ── 分钟速率:匿名/guest 额外查每 IP(挡共享匿名桶下的单机洪水);named 只查每用户 ──
        if small and ip:
            n = r.incr(f"rl:min:ip:{ip}:{win}")
            if n == 1:
                try: r.expire(f"rl:min:ip:{ip}:{win}", 120)
                except Exception: pass
            if n > config.RL_IP_REQ_PER_MIN:
                return "请求过于频繁,请稍后再试(触发单地址每分钟上限)。"

        umin = r.incr(f"rl:min:user:{owner}:{win}")
        if umin == 1:
            try: r.expire(f"rl:min:user:{owner}:{win}", 120)
            except Exception: pass
        cap_min = config.RL_REQ_PER_MIN_GUEST if small else config.RL_REQ_PER_MIN
        if umin > cap_min:
            return "请求过于频繁,请稍后再试。"

        # ── 成本桶:一次 MGET 取回 会话 / 用户当日 / 全站当日 三个累计,少两个来回 ──
        sk = f"rl:cost:sess:{session_id}" if session_id else None
        uk = f"rl:cost:user:{owner}:{_today()}"
        gk = f"rl:cost:global:{_today()}"
        keys = [uk, gk] + ([sk] if sk else [])
        vals = r.mget(*keys)
        u_cost, g_cost = _to_float(vals[0]), _to_float(vals[1])
        s_cost = _to_float(vals[2]) if sk else 0.0

        if sk and s_cost >= config.RL_SESSION_COST_USD:
            return "本次会话已达用量上限,请开启新会话继续。"
        cap_day = config.RL_DAILY_COST_USD_GUEST if small else config.RL_DAILY_COST_USD
        if u_cost >= cap_day:
            return "今日用量已达上限,请明天再来。"
        if g_cost >= config.RL_GLOBAL_DAILY_COST_USD:
            return "服务今日总用量已达上限,请稍后再试。"
    except Exception:
        return None                                   # Redis 异常 → fail-open,不拦路
    return None


def record(owner: str, ip: str | None, session_id: str | None, cost_usd: float) -> None:
    """请求【后】记账:把本次实际成本累加进 用户当日 / 全站当日 / 会话 三个桶(fail-open)。"""
    if not config.USE_RATE_LIMIT or not cost_usd or cost_usd <= 0:
        return
    r = _redis()
    if r is None:
        return
    day_ttl = 2 * 24 * 3600                           # 当日桶:date 已在 key 里,TTL 只作清理
    try:
        _bump(r, f"rl:cost:user:{owner}:{_today()}", cost_usd, day_ttl)
        _bump(r, f"rl:cost:global:{_today()}", cost_usd, day_ttl)
        if session_id:
            _bump(r, f"rl:cost:sess:{session_id}", cost_usd, config.SESSION_TTL_SECONDS)
    except Exception:
        pass                                          # 记账失败绝不拖垮请求


def _bump(r, key: str, amount: float, ttl: int) -> None:
    n = r.incrbyfloat(key, amount)
    if abs(_to_float(n) - amount) < 1e-6:             # 首次写入(值≈本次增量)→ 设 TTL 一次
        try: r.expire(key, ttl)
        except Exception: pass


def _reset_for_test():
    global _CLIENT, _TRIED
    _CLIENT, _TRIED = None, False
