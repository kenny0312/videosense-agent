"""
RedisSessionStore 单元测试 —— 用 fakeredis,纯离线、不连真 Redis / GCP / DB。
    python -m pipeline.test_redis_session

覆盖:save→新 store load 续聊、缺失键→新会话、reset 删键、TTL(ex)生效/不设、
无 L0 缓存(每次新对象)、Redis 异常 fail-open、后端工厂按 SESSION_BACKEND 选型。
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
from pipeline.test_session import _SQL1, _reg   # 复用 fixture + register 适配器


def _client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _seed(store: BaseSessionStore, sid: str = "x") -> Session:
    s = store.get_or_create(sid)
    _reg(s, _SQL1, {"n1": [{"id": 1, "predicate": "ski"}]}, "find ski", "retrieve")
    s.record_turn("find ski", None, "ok", [{"id": 1}], artifact_ids=["a1"])
    store.save(s)
    return s


# ── 持久化:save → 另一个 store 实例(共享同一 redis)load 续聊 ──────────
def test_redis_roundtrip_shared_client():
    c = _client()                                   # 模拟"同一个 Redis 服务"
    _seed(RedisSessionStore(client=c))
    other = RedisSessionStore(client=c)             # 模拟另一副本:独立 store 对象,共享 redis
    s2 = other.get_or_create("x")
    assert s2.catalog and s2.catalog[0].id == "a1"
    assert s2.catalog[0].kind == "table"
    assert s2.history and s2.history[0].artifact_ids == ["a1"]
    assert s2._seq == 1 and s2._turn_no == 1


def test_redis_missing_key_returns_fresh():
    st = RedisSessionStore(client=_client())
    s = st.get_or_create("nope")
    assert s.session_id == "nope"
    assert s.history == [] and s.catalog == [] and s._turn_no == 0


def test_redis_reset_deletes_key():
    c = _client()
    st = RedisSessionStore(client=c)
    _seed(st)
    assert c.get(st._key("x"))                       # 存在
    fresh = st.reset("x")
    assert fresh.history == [] and fresh.catalog == []
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
    assert s.history == [] and s._turn_no == 0
    s.record_turn("q", None, "ok", None)
    st.save(s)                                       # 写失败 → 不抛
    assert st.reset("x").history == []               # 删失败 → 仍返回新会话


def test_redis_bad_blob_returns_fresh():
    c = _client()
    st = RedisSessionStore(client=c)
    c.set(st._key("x"), "{not json")                 # 脏数据
    s = st.get_or_create("x")
    assert s.session_id == "x" and s.history == []   # 反序列化失败 → 新会话


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
    sa = a.get_or_create("s"); sa.record_turn("A", None, "ok", None)
    sb = b.get_or_create("s"); sb.record_turn("B", None, "ok", None)
    a.save(sa); b.save(sb)                           # B 后写
    final = RedisSessionStore(client=c).get_or_create("s")
    assert [t.question for t in final.history] == ["B"]   # A 整轮被覆盖(已知/已文档化)
    assert final._turn_no == 1


# ── fail-open:save 失败时保留旧值、不抛(此前只测了全坏 client)────────────
class _SetBroken:
    def __init__(self, c): self._c = c
    def get(self, *a, **k): return self._c.get(*a, **k)
    def set(self, *a, **k): raise RuntimeError("set down")
    def delete(self, *a, **k): return self._c.delete(*a, **k)


def test_redis_fail_open_on_save_keeps_stale():
    c = _client()
    ok = RedisSessionStore(client=c)
    s = ok.get_or_create("s"); s.record_turn("v1", None, "ok", None); ok.save(s)   # v1 落盘
    broken = RedisSessionStore(client=_SetBroken(c))
    s2 = broken.get_or_create("s"); s2.record_turn("v2", None, "ok", None)
    broken.save(s2)                                  # set 抛 → fail-open,不抛
    final = RedisSessionStore(client=c).get_or_create("s")
    assert [t.question for t in final.history] == ["v1"]   # v2 未落盘,仍是旧值


# ── unicode / emoji 往返(锁住 ensure_ascii=False + decode 契约)────────────
def test_redis_unicode_roundtrip():
    c = _client()
    st = RedisSessionStore(client=c)
    s = st.get_or_create("s")
    q = "找出所有滑雪视频 🎿❄️"
    s.record_turn(q, None, "ok", {"说明": "结果 ✓", "条数": 3})
    st.save(s)
    s2 = RedisSessionStore(client=c).get_or_create("s")
    assert s2.history[0].question == q
    assert "✓" in s2.history[0].answer_summary and "说明" in s2.history[0].answer_summary


# ── TTL 在每次 save 重新计(滑动刷新)────────────────────────
def test_redis_ttl_rearmed_on_resave():
    c = _client()
    st = RedisSessionStore(client=c, ttl_seconds=100)
    s = _seed(st)
    c.expire(st._key("x"), 5)                        # 模拟 TTL 快到期
    assert c.ttl(st._key("x")) <= 5
    st.save(s)                                       # 再存 → 重新计 TTL
    assert c.ttl(st._key("x")) > 5


# ── 行为级验证 Upstash REST 契约(此前只 isinstance/模块名)──────────────
class _FakeUpstashREST:
    """模拟 upstash_redis.Redis 表面:值恒为 str、delete(*keys)、set(ex=...)。"""
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
    _seed(st)
    s2 = st.get_or_create("x")                       # 无 L0 缓存 → 真从 fake 读回
    assert s2.catalog and s2.catalog[0].id == "a1"
    assert s2.catalog[0].kind == "table"
    st.reset("x")
    assert st.get_or_create("x").history == []       # reset 后空


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
