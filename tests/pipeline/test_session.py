"""
会话极薄壳的轻量单元测试 —— 纯离线,不依赖 GCP / DB。
    python -m pipeline.test_session

记忆简化后 Session 只剩:session_id + 单调轮号(next_turn)+ 持久化往返。
历史/指代/产物全在 transcript(见 test_loop_memory),这里不再测 catalog/history/resolve。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline.session import Session, SessionStore


# ── 轮号:单调推进 ─────────────────────────────────────────
def test_next_turn_monotonic():
    s = Session("t")
    assert s._turn_no == 0
    assert [s.next_turn() for _ in range(3)] == [1, 2, 3]
    assert s._turn_no == 3


# ── 序列化往返 ─────────────────────────────────────────────
def test_to_from_dict_roundtrip():
    s = Session("x")
    s.next_turn(); s.next_turn()
    d = s.to_dict()
    assert d == {"session_id": "x", "_turn_no": 2, "usage_cum": {}}
    s2 = Session.from_dict(d)
    assert s2.session_id == "x" and s2._turn_no == 2 and s2.usage_cum == {}


# ── U3:会话累计 usage(自我认知注入的数据源)────────────────
def test_add_usage_accumulates_and_snapshots_last():
    s = Session("u")
    s.add_usage({"tokens_total": 4000, "cost_usd": 0.001, "llm_calls": 2})
    s.add_usage({"tokens_total": 6000, "cost_usd": 0.002, "llm_calls": 3})
    c = s.usage_cum
    assert c["tokens_total"] == 10000 and c["llm_calls"] == 5 and c["turns"] == 2
    assert abs(c["cost_usd"] - 0.003) < 1e-9
    assert c["last"] == {"tokens_total": 6000, "cost_usd": 0.002}   # 上一轮快照,非累计


def test_add_usage_malformed_failopen():
    """形状不对(None/缺键/字符串数字)不抛错:缺键当 0,烂输入整体跳过。"""
    s = Session("u")
    s.add_usage({})                                   # 缺键 → 全 0,但算一轮
    s.add_usage({"tokens_total": "not-a-number"})     # 烂值 → 跳过
    assert s.usage_cum["turns"] == 1 and s.usage_cum["tokens_total"] == 0


def test_usage_cum_roundtrip_and_legacy_default():
    s = Session("u")
    s.add_usage({"tokens_total": 100, "cost_usd": 0.0001, "llm_calls": 1})
    s2 = Session.from_dict(s.to_dict())
    assert s2.usage_cum["tokens_total"] == 100 and s2.usage_cum["turns"] == 1
    legacy = Session.from_dict({"session_id": "old", "_turn_no": 3})   # 旧 blob 无 usage_cum
    assert legacy.usage_cum == {}


def test_from_dict_tolerates_legacy_blob():
    """升级前的 blob 带 history/rolling/catalog/_seq;from_dict 应忽略而非抛错,只取 _turn_no。"""
    legacy = {"session_id": "x", "_turn_no": 5, "_seq": 3,
              "history": [{"turn": 1, "question": "q"}],
              "rolling": [], "catalog": [{"id": "a1", "recipe": {"sql": "SELECT 1"}}]}
    s = Session.from_dict(legacy)
    assert s.session_id == "x" and s._turn_no == 5
    assert not hasattr(s, "catalog") and not hasattr(s, "history")


# ── store:幂等 / reset ────────────────────────────────────
def test_store_get_or_create_idempotent():
    st = SessionStore()                             # 无 path → 纯内存
    a = st.get_or_create("x")
    b = st.get_or_create("x")
    assert a is b
    assert st.reset("x") is not a                   # reset 换新对象


def test_inmemory_store_save_noop():
    st = SessionStore()                             # path=None
    s = st.get_or_create("x")
    s.next_turn()
    st.save(s)                                      # 不应抛错、不应建文件
    assert st.get_or_create("x") is s


# ── 持久化:save → 新 store load(模拟重启续上轮号)──────────
def test_persist_roundtrip():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "s.sqlite")
        st = SessionStore(path=p)
        s = st.get_or_create("x")
        s.next_turn(); s.next_turn(); s.next_turn()  # _turn_no → 3
        st.save(s)

        st2 = SessionStore(path=p)                   # 模拟重启:空缓存,从盘恢复
        s2 = st2.get_or_create("x")
        assert s2._turn_no == 3                       # 轮号续上
        assert s2.next_turn() == 4                    # 继续单调
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ── owner 作用域:别人的 sid 落不到你的命名空间 ────────────
def test_owner_scoped_isolation():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "s.sqlite")
        st = SessionStore(path=p)
        sa = st.get_or_create("sid", owner="alice"); sa.next_turn(); st.save(sa, owner="alice")
        sb = st.get_or_create("sid", owner="bob")    # 同 sid、不同 owner → 全新会话
        assert sb._turn_no == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
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
