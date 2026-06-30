"""analyze_video 内容缓存 —— 视频离线投递后内容【静态】,同一(视频+问题+上下文+细则+模型+时段)
重复分析没意义 → 缓存 AnalyzeResult,重复不重看,省一次 Gemini 多模态调用(成本 + 10–40s 延迟)。

两层(ANALYZE_CACHE_BACKEND):
  · memory(默认):只用【进程内 L1 LRU】(有界 + 锁,fail-open;重启清空 → prompt/模型一变 redeploy 即失效)。
  · redis:L1 之上加一层【共享 L2 Redis】—— Cloud Run 多副本【跨实例命中】(进程内各存各的,L2 才共享)。
            L1 miss 时查 L2,命中回填 L1;put 同写两层。Redis 任何异常一律 fail-open(退化成 memory,绝不卡)。
  · off:关闭。
键含【实际生效模型】(Pro/Flash)与【time_range】→ 不串味;失败信封不缓存(见 node_executor)。
"""
from __future__ import annotations

import collections
import hashlib
import json
import threading

from pipeline import config

_LOCK = threading.Lock()
_LRU: "collections.OrderedDict[str, dict]" = collections.OrderedDict()

# 惰性 Redis 客户端:建一次;失败后置 None 不再重试(整段 fail-open)。测试可直接覆盖 _REDIS。
_REDIS = None
_REDIS_TRIED = False
_REDIS_LOCK = threading.Lock()


def make_key(video_id: str, *, question: str, context, rubric, time_range, model: str) -> str:
    """稳定缓存键:av:{video_id}:{md5(规范化参数)}。参数 sort_keys → 顺序无关。"""
    payload = json.dumps(
        {"q": question, "ctx": context, "rubric": rubric, "tr": time_range, "m": model},
        sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()
    return f"av:{video_id}:{digest}"


# ── L2 Redis(惰性、fail-open)────────────────────────────────
def _redis():
    global _REDIS, _REDIS_TRIED
    if _REDIS_TRIED:
        return _REDIS
    with _REDIS_LOCK:
        if not _REDIS_TRIED:
            try:
                from pipeline.redis_client import build_redis_client
                _REDIS = build_redis_client()
            except Exception:
                _REDIS = None
            _REDIS_TRIED = True
    return _REDIS


# ── L1 进程内 LRU ────────────────────────────────────────────
def _l1_get(key: str):
    try:
        with _LOCK:
            if key in _LRU:
                _LRU.move_to_end(key)                    # LRU:命中移到尾 = 最近用
                return dict(_LRU[key])
    except Exception:
        pass
    return None


def _l1_put(key: str, value: dict):
    try:
        with _LOCK:
            _LRU[key] = dict(value)
            _LRU.move_to_end(key)
            while len(_LRU) > config.ANALYZE_CACHE_MAX:
                _LRU.popitem(last=False)                 # 淘汰最久未用
    except Exception:
        pass


# ── 对外 ─────────────────────────────────────────────────────
def get(key: str) -> dict | None:
    """L1 命中直接返回;memory 模式仅 L1;redis 模式 L1 miss 再查 L2,命中回填 L1。全程 fail-open。"""
    if config.ANALYZE_CACHE_BACKEND == "off":
        return None
    hit = _l1_get(key)
    if hit is not None:
        return hit
    if config.ANALYZE_CACHE_BACKEND == "redis":
        try:
            r = _redis()
            if r is not None:
                raw = r.get(key)
                if raw:
                    val = json.loads(raw)
                    _l1_put(key, val)                    # 回填 L1(下次本进程直接命中)
                    return dict(val)
        except Exception:
            pass                                         # Redis 挂掉 → 退化成 L1-only
    return None


def put(key: str, value: dict) -> None:
    """写 L1;redis 模式同写 L2(带 TTL)。关闭/异常 → 静默跳过(fail-open)。"""
    if config.ANALYZE_CACHE_BACKEND == "off":
        return
    _l1_put(key, value)
    if config.ANALYZE_CACHE_BACKEND == "redis":
        try:
            r = _redis()
            if r is not None:
                r.set(key, json.dumps(value, ensure_ascii=False),
                      ex=config.ANALYZE_CACHE_TTL_SECONDS)
        except Exception:
            pass


def clear() -> None:
    """清空 L1(测试用);不动共享 L2。顺带重置 Redis 客户端缓存,便于测试重建/注入。"""
    global _REDIS, _REDIS_TRIED
    with _LOCK:
        _LRU.clear()
    with _REDIS_LOCK:
        _REDIS, _REDIS_TRIED = None, False


def size() -> int:
    with _LOCK:
        return len(_LRU)
