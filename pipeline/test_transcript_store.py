"""M4:transcript 存储 + 确定性写入器 离线单测(InMemory + append_event 路由)。

不碰 GCS/Redis —— 真实后端复用已验过的 redis_client + GCS 模式,这里验:owner 作用域、
append/tail、大/非 JSON 本体溢出到 blob_put、小本体内联。
"""
import pytest

from pipeline import transcript_store as ts
from pipeline.transcript_store import (InMemoryTranscriptStore, _scoped, append_event)


def test_owner_scoping_and_idor():
    assert _scoped("alice", "s1") == "alice:s1"
    assert _scoped("", "s1") == "anon:s1"
    h = _scoped("a:b", "s1")                       # owner 含 ':' → 哈希兜底
    assert h.startswith("u_") and h.endswith(":s1") and ":b:" not in h
    assert _scoped("alice", "s1") != _scoped("bob", "s1")   # 跨 owner 同 sid 不撞


def test_append_and_tail_order():
    st = InMemoryTranscriptStore()
    for i in range(5):
        append_event(st, "alice", "s1", {"type": "user", "seq": i, "text": f"m{i}"})
    tail = st.tail(_scoped("alice", "s1"), 3)
    assert [l["seq"] for l in tail] == [2, 3, 4]
    assert len(st.all(_scoped("alice", "s1"))) == 5


def test_small_tool_result_stays_inline():
    st = InMemoryTranscriptStore()
    calls = []
    bp = lambda o, s, e, v: (calls.append(v) or "gs://x")
    line = append_event(st, "a", "s", {"type": "tool_result", "event_id": "c0", "value": [{"x": 1}]},
                        blob_put=bp, overflow_bytes=10000)
    assert "value" in line and "result_ref" not in line and not calls


def test_big_tool_result_overflows_to_blob():
    st = InMemoryTranscriptStore()
    calls = []

    def bp(o, s, e, v):
        calls.append((e, len(v)))
        return f"gs://b/{e}"

    big = [{"k": "x" * 200} for _ in range(50)]
    line = append_event(st, "a", "s", {"type": "tool_result", "event_id": "c0", "value": big},
                        blob_put=bp, overflow_bytes=1024)
    assert line["result_ref"] == "gs://b/c0"
    assert "value" not in line                     # 本体不进行
    assert line["n"] == 50 and len(line["preview"]) == 3
    assert calls and calls[0][0] == "c0"
    # 落盘的行也不含完整本体
    stored = st.tail(_scoped("a", "s"), 1)[0]
    assert "value" not in stored and stored["result_ref"] == "gs://b/c0"


def test_non_json_value_overflows_even_if_small():
    st = InMemoryTranscriptStore()
    seen = []
    bp = lambda o, s, e, v: (seen.append(e) or "gs://x")
    line = append_event(st, "a", "s", {"type": "tool_result", "event_id": "c1", "value": {1, 2, 3}},
                        blob_put=bp, overflow_bytes=100000)
    assert "value" not in line and line["result_ref"] == "gs://x" and seen == ["c1"]


def test_non_tool_result_never_overflows():
    st = InMemoryTranscriptStore()
    big_text = "x" * 50000
    line = append_event(st, "a", "s", {"type": "model", "text": big_text}, overflow_bytes=1024)
    assert line["text"] == big_text and "result_ref" not in line   # 非 tool_result 不溢出


def test_factory_default_inmemory(monkeypatch):
    monkeypatch.setattr(ts.config, "SESSION_BACKEND", "sqlite")
    assert isinstance(ts.make_transcript_store(), InMemoryTranscriptStore)


# ── GCS 回读【门控】:热尾不足时才碰 GCS(短会话别每轮白跑 GCS)──────────────
import json

from pipeline.transcript_store import RedisGcsTranscriptStore


class _FakeRedis:
    def __init__(self, items): self._items = items           # items = JSON 字符串列表
    def lrange(self, key, start, end):                        # 模拟 lrange(key, -n, -1)
        return self._items[start:] if start < 0 else self._items[start:end + 1]


def _redis_store(n_redis: int, hot: int = 500) -> RedisGcsTranscriptStore:
    st = RedisGcsTranscriptStore.__new__(RedisGcsTranscriptStore)   # 跳过 __init__(免建真 Redis)
    st._r = _FakeRedis([json.dumps({"turn": i}) for i in range(n_redis)])
    st._hot = hot
    st._ttl = 0
    return st


def test_tail_no_gcs_when_hot_has_full_history():
    st = _redis_store(5)                                      # 5 < hot(500) → Redis 即全量
    hits = {"n": 0}
    st._tail_from_gcs = lambda k, n: hits.__setitem__("n", hits["n"] + 1) or []
    evs = st.tail("k", 1000)
    assert len(evs) == 5 and hits["n"] == 0                   # 完全没碰 GCS


def test_tail_reads_gcs_when_hot_empty():
    st = _redis_store(0)                                      # 过 TTL / 空
    st._tail_from_gcs = lambda k, n: [{"turn": 1}, {"turn": 2}]
    assert len(st.tail("k", 1000)) == 2                       # 回读 GCS


def test_tail_reads_gcs_when_hot_trimmed_to_window():
    st = _redis_store(500, hot=500)                           # 顶到 HOT_WINDOW → 被 LTRIM
    st._tail_from_gcs = lambda k, n: [{"turn": i} for i in range(800)]
    assert len(st.tail("k", 1000)) == 800                     # 取 GCS 更全的(更老的只在 GCS)


# ── U4-D2:耐久层序号 —— 跨进程单调,绝不复用旧名覆盖历史 ─────────
class _FakeSeqRedis:
    """极简 fake:只实现 _next_seq_name 用到的 incr/set。"""
    def __init__(self, fail=False):
        self.kv, self.fail = {}, fail

    def incr(self, k):
        if self.fail:
            raise ConnectionError("redis down")
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    def set(self, k, v):
        self.kv[k] = int(v)


def _mk_store(monkeypatch, fake):
    from pipeline import redis_client as rc
    monkeypatch.setattr(rc, "build_redis_client", lambda: fake)
    return ts.RedisGcsTranscriptStore()


def test_seq_monotonic_across_instances(monkeypatch):
    """两个 store 实例(= 两个进程/一次重启)共享 Redis 计数 → 序号连续,不回卷不覆盖。"""
    fake = _FakeSeqRedis()
    a = _mk_store(monkeypatch, fake)
    monkeypatch.setattr(a, "_gcs_next_seq", lambda key: 1)    # 无旧历史
    assert a._next_seq_name("o:s") == "000000001"
    assert a._next_seq_name("o:s") == "000000002"
    b = _mk_store(monkeypatch, fake)                    # "重启后的新进程"
    assert b._next_seq_name("o:s") == "000000003"       # 旧实现这里会回到 1 → 覆盖


def test_seq_aligns_to_legacy_gcs_max_not_count(monkeypatch):
    """计数器是新的、GCS 已有旧历史(可能带空洞)→ 对齐到【最大旧序号+1】,首写不覆盖。
    (review 修:按 count 对齐会被空洞骗到复用旧名 —— 如旧名 {1,2,5} count=3 → 下一个写 4、
    再下一个撞 5;按 max 对齐直接从 6 开始。)"""
    st = _mk_store(monkeypatch, _FakeSeqRedis())
    monkeypatch.setattr(st, "_gcs_next_seq", lambda key: 6)   # 旧历史 max=5(哪怕只有 3 个对象)
    assert st._next_seq_name("o:s") == "000000006"
    assert st._next_seq_name("o:s") == "000000007"      # 之后正常递增


def test_seq_probe_failure_never_risks_seq1(monkeypatch):
    """首用时 GCS 列举失败(探不出旧历史)→ 本次退时间戳名 + 回退计数下次再探,
    【绝不】冒险用序号 1(可能覆盖探不到的旧历史)。探测恢复后正常对齐。"""
    fake = _FakeSeqRedis()
    st = _mk_store(monkeypatch, fake)
    monkeypatch.setattr(st, "_gcs_next_seq", lambda key: None)   # 探测失败
    n1 = st._next_seq_name("o:s")
    assert n1.startswith("t")
    monkeypatch.setattr(st, "_gcs_next_seq", lambda key: 4)     # 恢复:旧 max=3
    assert st._next_seq_name("o:s") == "000000004"              # 计数被回退过 → 重新对齐


def test_seq_fallback_never_reuses_names(monkeypatch):
    """Redis 不可用 → 时间戳命名:排在 9 位数字之后,且两次调用不同名(绝不覆盖)。"""
    st = _mk_store(monkeypatch, _FakeSeqRedis(fail=True))
    n1, n2 = st._next_seq_name("o:s"), st._next_seq_name("o:s")
    assert n1.startswith("t") and n2.startswith("t") and n1 != n2
    assert n1 > "999999999"                             # 字典序在所有数字名之后
