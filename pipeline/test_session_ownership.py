"""
会话按 owner(认证身份)归属 / IDOR 隔离的离线单测 —— 不连网。
    python -m pipeline.test_session_ownership

覆盖:_scoped 规则 + 净化、SQLite/Redis 两版的跨 owner 隔离、anon 默认向后兼容、reset 按 owner 隔离。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

try:
    import fakeredis
except ImportError:
    fakeredis = None

from pipeline.session import RedisSessionStore, Session, SessionStore, _scoped


# ── _scoped ───────────────────────────────────────────────
def test_scoped_basic():
    assert _scoped("alice", "s") == "alice:s"
    assert _scoped("", "s") == "anon:s"
    assert _scoped(None, "s") == "anon:s"


def test_scoped_sanitizes_colon_owner():
    k = _scoped("a:b", "s")                       # owner 含分隔符 → 哈希,防越界/碰撞
    assert k.startswith("u_") and k.endswith(":s")


# ── SQLite 版 ─────────────────────────────────────────────
def _sqlite(d):
    return SessionStore(path=os.path.join(d, "s.sqlite"))


def test_sqlite_cross_owner_isolation():
    d = tempfile.mkdtemp()
    try:
        st = _sqlite(d)
        a = st.get_or_create("s", owner="alice")
        a.record_turn("alice secret q", None, "ok", "alice answer")
        st.save(a, owner="alice")
        st2 = _sqlite(d)                          # 空缓存,模拟另一进程/重启
        bob = st2.get_or_create("s", owner="bob")  # bob 拿同一 sid
        assert bob.history == []                   # 读不到 alice 的(IDOR 关上)
        alice = st2.get_or_create("s", owner="alice")
        assert alice.history and alice.history[0].question == "alice secret q"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sqlite_anon_default_backcompat():
    d = tempfile.mkdtemp()
    try:
        st = _sqlite(d)
        s = st.get_or_create("x")                 # owner 默认 anon
        s.record_turn("q", None, "ok", "a")
        st.save(s)                                 # save 默认 anon
        st2 = _sqlite(d)
        assert st2.get_or_create("x").history                 # anon 续得上
        assert st2.get_or_create("x", owner="anon").history   # 显式 anon = 同一条
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sqlite_reset_is_owner_scoped():
    d = tempfile.mkdtemp()
    try:
        st = _sqlite(d)
        a = st.get_or_create("s", owner="alice"); a.record_turn("qa", None, "ok", "a"); st.save(a, owner="alice")
        b = st.get_or_create("s", owner="bob"); b.record_turn("qb", None, "ok", "b"); st.save(b, owner="bob")
        st.reset("s", owner="bob")
        st2 = _sqlite(d)
        assert st2.get_or_create("s", owner="bob").history == []      # bob 的被清
        assert st2.get_or_create("s", owner="alice").history          # alice 的还在
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── Redis 版 ──────────────────────────────────────────────
def _client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_redis_cross_owner_isolation():
    c = _client()
    st = RedisSessionStore(client=c)
    sa = st.get_or_create("s", owner="alice"); sa.record_turn("alice q", None, "ok", "a"); st.save(sa, owner="alice")
    bob = st.get_or_create("s", owner="bob")
    assert bob.history == []                       # bob 读不到 alice
    assert st.get_or_create("s", owner="alice").history[0].question == "alice q"
    assert c.get("vs:session:alice:s") and c.get("vs:session:bob:s") is None   # key 真带归属


def test_redis_anon_default_key():
    c = _client()
    st = RedisSessionStore(client=c)
    s = st.get_or_create("x"); s.record_turn("q", None, "ok", "a"); st.save(s)   # 默认 anon
    assert c.get("vs:session:anon:x")
    assert st._key("x") == "vs:session:anon:x"


def test_redis_reset_owner_scoped():
    c = _client()
    st = RedisSessionStore(client=c)
    sa = st.get_or_create("s", owner="alice"); sa.record_turn("q", None, "ok", "a"); st.save(sa, owner="alice")
    sb = st.get_or_create("s", owner="bob"); sb.record_turn("q", None, "ok", "a"); st.save(sb, owner="bob")
    st.reset("s", owner="bob")
    assert c.get("vs:session:bob:s") is None and c.get("vs:session:alice:s")


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    if fakeredis is None:
        fns = [f for f in fns if "redis" not in f.__name__]
        print("  (fakeredis 缺失 → 跳过 redis 用例)")
    failed = 0
    for t in fns:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
