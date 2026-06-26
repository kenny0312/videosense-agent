"""
RedisArtifactValueStore + 工厂的离线单测 —— fakeredis,不连网/不连真 Redis。
    python -m pipeline.test_redis_artifact_value_store

覆盖:put/get 往返、缺失→None、key 前缀、超封顶跳过、循环引用跳过、TTL(ex)生效/不设、
delete、读写 fail-open、脏 blob→None、工厂按 SESSION_BACKEND 选型 + 无凭据回退内存。
"""
from __future__ import annotations

import sys

try:
    import fakeredis
except ImportError:
    fakeredis = None

from pipeline import artifact_value_store as AVS
from pipeline.artifact_value_store import (InMemoryArtifactValueStore,
                                           RedisArtifactValueStore, _make_value_store)


def _client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


# ── 基本 ──────────────────────────────────────────────────
def test_put_get_roundtrip():
    st = RedisArtifactValueStore(_client())
    assert st.put("s::a1", {"r2": 0.9, "rows": [1, 2, 3]}) is True
    assert st.get("s::a1") == {"r2": 0.9, "rows": [1, 2, 3]}


def test_get_missing_returns_none():
    assert RedisArtifactValueStore(_client()).get("nope") is None


def test_key_is_prefixed():
    c = _client(); st = RedisArtifactValueStore(c)
    st.put("s::a1", {"x": 1})
    assert c.get("vs:artifact:s::a1") and c.get("s::a1") is None


# ── 封顶 / 不可序列化 ──────────────────────────────────────
def test_size_cap_skips():
    st = RedisArtifactValueStore(_client(), max_bytes=100)
    assert st.put("s::big", {"d": "x" * 200}) is False
    assert st.get("s::big") is None


def test_circular_ref_skips():
    st = RedisArtifactValueStore(_client())
    d: dict = {}; d["self"] = d                      # 循环引用 → json.dumps 抛 → 跳过
    assert st.put("s::x", d) is False


# ── TTL = 自动清理 ────────────────────────────────────────
def test_ttl_applied():
    c = _client(); st = RedisArtifactValueStore(c, ttl_seconds=259200)   # 3 天
    st.put("s::a1", {"x": 1})
    ttl = c.ttl("vs:artifact:s::a1")
    assert 0 < ttl <= 259200, ttl


def test_no_ttl_is_persistent():
    c = _client(); st = RedisArtifactValueStore(c, ttl_seconds=0)
    st.put("s::a1", {"x": 1})
    assert c.ttl("vs:artifact:s::a1") == -1


def test_delete_removes():
    c = _client(); st = RedisArtifactValueStore(c)
    st.put("s::a1", {"x": 1}); st.delete("s::a1")
    assert st.get("s::a1") is None


# ── fail-open ─────────────────────────────────────────────
class _Broken:
    def get(self, *a, **k): raise RuntimeError("down")
    def set(self, *a, **k): raise RuntimeError("down")
    def delete(self, *a, **k): raise RuntimeError("down")


def test_fail_open_on_errors():
    st = RedisArtifactValueStore(_Broken())
    assert st.put("k", {"x": 1}) is False
    assert st.get("k") is None
    st.delete("k")                                   # 不抛


def test_bad_blob_returns_none():
    c = _client(); st = RedisArtifactValueStore(c)
    c.set("vs:artifact:k", "{not json")
    assert st.get("k") is None


# ── 工厂 ──────────────────────────────────────────────────
def _save():
    return (AVS.config.SESSION_BACKEND, AVS.config.REDIS_URL,
            AVS.config.UPSTASH_REDIS_REST_URL, AVS.config.UPSTASH_REDIS_REST_TOKEN)


def _restore(s):
    (AVS.config.SESSION_BACKEND, AVS.config.REDIS_URL,
     AVS.config.UPSTASH_REDIS_REST_URL, AVS.config.UPSTASH_REDIS_REST_TOKEN) = s


def test_factory_sqlite_is_inmemory():
    s = _save()
    try:
        AVS.config.SESSION_BACKEND = "sqlite"
        assert isinstance(_make_value_store(), InMemoryArtifactValueStore)
    finally:
        _restore(s)


def test_factory_redis_via_url():
    s = _save()
    try:
        AVS.config.SESSION_BACKEND = "redis"
        AVS.config.REDIS_URL = "redis://localhost:6379/0"     # redis-py 始终在依赖里;from_url 惰性不连
        AVS.config.UPSTASH_REDIS_REST_URL = ""; AVS.config.UPSTASH_REDIS_REST_TOKEN = ""
        assert isinstance(_make_value_store(), RedisArtifactValueStore)   # 与环境是否装 upstash-redis 无关
    finally:
        _restore(s)


def test_factory_redis_no_creds_falls_back_to_memory():
    s = _save()
    try:
        AVS.config.SESSION_BACKEND = "redis"; AVS.config.REDIS_URL = ""
        AVS.config.UPSTASH_REDIS_REST_URL = ""; AVS.config.UPSTASH_REDIS_REST_TOKEN = ""
        assert isinstance(_make_value_store(), InMemoryArtifactValueStore)   # build 抛 → 回退
    finally:
        _restore(s)


def main() -> int:
    if fakeredis is None:
        print("  SKIP  fakeredis 未安装(pip install -r requirements-dev.txt)")
        return 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
