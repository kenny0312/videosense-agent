"""M4.1:analyze_video 内容缓存 —— 视频离线投递后内容【静态】,同一(视频+问题+上下文+细则+模型)
重复分析没意义 → 缓存 AnalyzeResult,重复不重看,省一次 Gemini 多模态调用(成本 + 10–40s 延迟)。

本期最简实现 = 【进程内 LRU】(有界 OrderedDict + 锁,fail-open):
  · 零基建、线程安全(为 M4.3 并行铺路)、重启自动清空(prompt/模型一变,redeploy 即失效,天然防陈旧);
  · 跨副本不共享 —— 那是后续 Redis 层的事(设计 §4.3 / 开放问题),本期不做。
键含【实际生效模型】(Pro/Flash)→ 不串味。命中【不另消耗】Gemini,但配额计数仍在 _make_executor
(命中省的是真实调用;"命中不占配额"的覆盖优化属 M4.4)。任何异常一律 fail-open(绝不卡主链路)。
"""
from __future__ import annotations

import collections
import hashlib
import json
import threading

from pipeline import config

_LOCK = threading.Lock()
_LRU: "collections.OrderedDict[str, dict]" = collections.OrderedDict()


def make_key(video_id: str, *, question: str, context, rubric, time_range, model: str) -> str:
    """稳定缓存键:av:{video_id}:{md5(规范化参数)}。参数 sort_keys → 顺序无关。"""
    payload = json.dumps(
        {"q": question, "ctx": context, "rubric": rubric, "tr": time_range, "m": model},
        sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()
    return f"av:{video_id}:{digest}"


def get(key: str) -> dict | None:
    """命中返回缓存的 AnalyzeResult dump(并标记为最近用);未命中/关闭/异常 → None(fail-open)。"""
    if config.ANALYZE_CACHE_BACKEND == "off":
        return None
    try:
        with _LOCK:
            if key not in _LRU:
                return None
            _LRU.move_to_end(key)                    # LRU:命中移到尾 = 最近使用
            return dict(_LRU[key])                    # 拷贝,调用方改不脏缓存
    except Exception:
        return None


def put(key: str, value: dict) -> None:
    """写入缓存并按 LRU 淘汰最久未用;关闭/异常 → 静默跳过(fail-open)。"""
    if config.ANALYZE_CACHE_BACKEND == "off":
        return
    try:
        with _LOCK:
            _LRU[key] = dict(value)
            _LRU.move_to_end(key)
            while len(_LRU) > config.ANALYZE_CACHE_MAX:
                _LRU.popitem(last=False)             # 从头淘汰 = 最久未用
    except Exception:
        pass


def clear() -> None:
    """清空(测试用)。"""
    with _LOCK:
        _LRU.clear()


def size() -> int:
    with _LOCK:
        return len(_LRU)
