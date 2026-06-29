"""共享:按 config 里的凭据建一个 Redis 客户端。

session 仓(RedisSessionStore)与 transcript 存储(RedisGcsTranscriptStore)都用它 —— 抽到这里
避免重复,也避免循环引用(本模块只依赖 config)。

两种客户端(redis-py TCP / upstash-redis REST)都暴露同样的 get/set(ex=)/delete,对上层等价。
惰性导入:只装你实际用到的那个库即可。
"""
from __future__ import annotations

from typing import Any

from pipeline import config


def build_redis_client() -> Any:
    """TCP(redis-py,REDIS_URL)优先,否则 Upstash REST(upstash-redis)。两者都没配 → 报错。"""
    if config.REDIS_URL:
        import redis                                # TCP RESP 协议
        return redis.from_url(config.REDIS_URL, decode_responses=True)
    if config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN:
        from upstash_redis import Redis             # HTTP REST(serverless 友好)
        return Redis(url=config.UPSTASH_REDIS_REST_URL, token=config.UPSTASH_REDIS_REST_TOKEN)
    raise ValueError(
        "需要 REDIS_URL 或 UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN")
