"""
RedisSessionStore 单元测试(极薄壳)—— 用 fakeredis,纯离线、不连真 Redis / GCP / DB。
    python -m pipeline.test_redis_session

覆盖:save→新 store load 续上轮号、缺失键→新会话、reset 删键、TTL(ex)生效/不设、
无 L0 缓存(每次新对象)、Redis 异常 fail-open、脏 blob→新会话、后端工厂按 env 选型、
跨副本后写覆盖(LWW)、TTL 滑动刷新、Upstash REST 契约、API 层每会话锁。
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

try:
    import fakeredis
except ImportError:                                 # 干净检出 / 仅装运行期依赖时优雅跳过,不崩
    fakeredis = None

from pipeline import session as S
from pipeline.session import (BaseSessionStore, RedisSessionStore, Session,
                              SessionStore, _build_redis_client, _make_store)


def _client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _seed(store: BaseSessionStore, sid: str = "x", turns: int = 2) -> Session:
    s = store.get_or_create(sid)
    for _ in range(turns):
        s.next_turn()
    store.save(s)
    return s


# ── 持久化:save → 另一个 store 实例(共享同一 redis)load 续上 ──────────
def test_redis_roundtrip_shared_client():
    c = _client()                                   # 模拟"同一个 Redis 服务"
    _seed(RedisSessionStore(client=c))              # _turn_no → 2
    other = RedisSessionStore(client=c)             # 模拟另一副本:独立 store 对象,共享 redis
    s2 = other.get_or_create("x")
    assert s2.session_id == "x" and s2._turn_no == 2
    assert s2.next_turn() == 3                       # 续上单调


def test_redis_missing_key_returns_fresh():
    st = RedisSessionStore(client=_client())
    s = st.get_or_create("nope")
    assert s.session_id == "nope" and s._turn_no == 0


def test_redis_reset_deletes_key():
    c = _client()
    st = RedisSessionStore(client=c)
    _seed(st)
    assert c.get(st._key("x"))                       # 存在
    fresh = st.reset("x")
    assert fresh._turn_no == 0
    assert c.get(st._key("x")) is None               # 键已删


# ── TTL 交给 Redis ─────────────────────────────────────────
def test_redis_ttl_applied():
    c = _client()
    st = RedisSessionStore(client=c, ttl_seconds=100)
    _seed(st)
    ttl = c.ttl(st._key("x"))
    assert 0 < ttl <= 100, ttl                       # 设了过期


def test_redis_no_ttl_means_persist():
    c = _client()
    st = RedisSessionStore(client=c, ttl_seconds=0)
    _seed(st)
    assert c.ttl(st._key("x")) == -1                 # -1 = 永不过期


# ── 无 L0 缓存:每次 get_or_create 都是新对象(对比 SQLite 版的 is 缓存)──
def test_redis_has_no_l0_cache():
    st = RedisSessionStore(client=_client())
    _seed(st)
    a = st.get_or_create("x")
    b = st.get_or_create("x")
    assert a is not b                                # 每次从 Redis 重读,非同一对象
    assert a.to_dict() == b.to_dict()               # 但内容等价
    # 对照:SQLite/内存版保留 L0 缓存,返回同一对象
    mem = SessionStore()
    assert mem.get_or_create("x") is mem.get_or_create("x")


# ── Redis 异常 → fail-open,不拖垮主请求 ────────────────────
class _Broken:
    def get(self, *a, **k): raise RuntimeError("redis down")
    def set(self, *a, **k): raise RuntimeError("redis down")
    def delete(self, *a, **k): raise RuntimeError("redis down")


def test_redis_fail_open_on_errors():
    st = RedisSessionStore(client=_Broken())
    s = st.get_or_create("x")                        # 读失败 → 退化为新会话,不抛
    assert s._turn_no == 0
    s.next_turn()
    st.save(s)                                       # 写失败 → 不抛
    assert st.reset("x")._turn_no == 0               # 删失败 → 仍返回新会话


def test_redis_bad_blob_returns_fresh():
    c = _client()
    st = RedisSessionStore(client=c)
    c.set(st._key("x"), "{not json")                 # 脏数据
    s = st.get_or_create("x")
    assert s.session_id == "x" and s._turn_no == 0   # 反序列化失败 → 新会话


def test_redis_from_dict_tolerates_legacy_blob():
    """Redis 里残留升级前的 blob(带 history/catalog)→ 仍能加载,只取 _turn_no。"""
    c = _client()
    st = RedisSessionStore(client=c)
    import json
    c.set(st._key("x"), json.dumps({"session_id": "x", "_turn_no": 7,
                                    "history": [{"turn": 1}], "catalog": [{"id": "a1"}]}))
    s = st.get_or_create("x")
    assert s._turn_no == 7 and not hasattr(s, "catalog")


# ── 后端工厂按 env 选型 ────────────────────────────────────
def _save_cfg():
    return (S.config.SESSION_BACKEND, S.config.REDIS_URL,
            S.config.UPSTASH_REDIS_REST_URL, S.config.UPSTASH_REDIS_REST_TOKEN)


def _restore_cfg(saved):
    (S.config.SESSION_BACKEND, S.config.REDIS_URL,
     S.config.UPSTASH_REDIS_REST_URL, S.config.UPSTASH_REDIS_REST_TOKEN) = saved


def test_factory_sqlite_default():
    saved = _save_cfg()
    try:
        S.config.SESSION_BACKEND = "sqlite"
        assert isinstance(_make_store(), SessionStore)
    finally:
        _restore_cfg(saved)


def test_factory_redis_via_tcp_url():
    saved = _save_cfg()
    try:
        S.config.SESSION_BACKEND = "redis"
        S.config.REDIS_URL = "redis://localhost:6379/0"   # from_url 惰性,不立刻连
        S.config.UPSTASH_REDIS_REST_URL = S.config.UPSTASH_REDIS_REST_TOKEN = ""
        store = _make_store()
        assert isinstance(store, RedisSessionStore)
    finally:
        _restore_cfg(saved)


def test_factory_redis_via_upstash_rest():
    saved = _save_cfg()
    try:
        S.config.SESSION_BACKEND = "redis"
        S.config.REDIS_URL = ""                            # 无 TCP → 落到 REST 分支
        S.config.UPSTASH_REDIS_REST_URL = "https://x.upstash.io"
        S.config.UPSTASH_REDIS_REST_TOKEN = "tok"          # 构造惰性,不发请求
        store = _make_store()
        assert isinstance(store, RedisSessionStore)
        assert type(store._r).__module__.startswith("upstash_redis")
    finally:
        _restore_cfg(saved)


def test_build_redis_client_requires_creds():
    saved = _save_cfg()
    try:
        S.config.REDIS_URL = ""
        S.config.UPSTASH_REDIS_REST_URL = S.config.UPSTASH_REDIS_REST_TOKEN = ""
        try:
            _build_redis_client()                          # 两种凭据都没有 → 报错
            raised = False
        except ValueError:
            raised = True
        assert raised
    finally:
        _restore_cfg(saved)


def test_construct_redis_requires_url():
    try:
        RedisSessionStore(url="")                    # 无 url、无 client → 报错
        raised = False
    except ValueError:
        raised = True
    assert raised


# ── 跨副本并发:文档化的"后写覆盖"(API 层锁只防同进程,跨副本仍 LWW)──────
def test_redis_last_write_wins_across_instances():
    c = _client()                                    # 同一 Redis
    a, b = RedisSessionStore(client=c), RedisSessionStore(client=c)   # 两副本
    sa = a.get_or_create("s"); sa.next_turn()                        # _turn_no → 1
    sb = b.get_or_create("s"); sb.next_turn(); sb.next_turn()        # _turn_no → 2
    a.save(sa); b.save(sb)                            # B 后写
    final = RedisSessionStore(client=c).get_or_create("s")
    assert final._turn_no == 2                        # A 整轮被覆盖(已知/已文档化)


# ── fail-open:save 失败时保留旧值、不抛 ──────────────────────
class _SetBroken:
    def __init__(self, c): self._c = c
    def get(self, *a, **k): return self._c.get(*a, **k)
    def set(self, *a, **k): raise RuntimeError("set down")
    def delete(self, *a, **k): return self._c.delete(*a, **k)


def test_redis_fail_open_on_save_keeps_stale():
    c = _client()
    ok = RedisSessionStore(client=c)
    s = ok.get_or_create("s"); s.next_turn(); ok.save(s)             # _turn_no=1 落盘
    broken = RedisSessionStore(client=_SetBroken(c))
    s2 = broken.get_or_create("s"); s2.next_turn()                  # _turn_no=2(内存)
    broken.save(s2)                                  # set 抛 → fail-open,不抛
    final = RedisSessionStore(client=c).get_or_create("s")
    assert final._turn_no == 1                        # _turn_no=2 未落盘,仍是旧值


# ── TTL 在每次 save 重新计(滑动刷新)────────────────────────
def test_redis_ttl_rearmed_on_resave():
    c = _client()
    st = RedisSessionStore(client=c, ttl_seconds=100)
    s = _seed(st)
    c.expire(st._key("x"), 5)                        # 模拟 TTL 快到期
    assert c.ttl(st._key("x")) <= 5
    st.save(s)                                       # 再存 → 重新计 TTL
    assert c.ttl(st._key("x")) > 5


# ── 行为级验证 Upstash REST 契约(值恒 str / delete(*keys) / set(ex=))──────
class _FakeUpstashREST:
    def __init__(self): self._d = {}
    def get(self, k):
        v = self._d.get(k)
        assert v is None or isinstance(v, str)       # REST 永远返回 str / None
        return v
    def set(self, k, v, ex=None):
        assert isinstance(v, str)                    # 只写 str
        self._d[k] = v
        return True
    def delete(self, *keys):
        return sum(1 for k in keys if self._d.pop(k, None) is not None)


def test_redis_works_with_upstash_rest_contract():
    st = RedisSessionStore(client=_FakeUpstashREST())
    _seed(st)                                         # _turn_no → 2
    s2 = st.get_or_create("x")                        # 无 L0 缓存 → 真从 fake 读回
    assert s2._turn_no == 2
    st.reset("x")
    assert st.get_or_create("x")._turn_no == 0        # reset 后空


# ── API 层每会话锁:同 id 同一把锁、不同 id 不同锁 ──────────
def test_api_session_lock_per_id():
    from api.server import _session_lock
    assert _session_lock("a") is _session_lock("a")
    assert _session_lock("a") is not _session_lock("b")


def main() -> int:
    if fakeredis is None:
        print("  SKIP  fakeredis 未安装(pip install -r requirements-dev.txt);跳过 Redis 测试")
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
