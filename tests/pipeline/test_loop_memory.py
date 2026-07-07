"""M5:loop_memory 记录 / 回放 / 压缩 离线单测(InMemory store + stub summarizer)。"""
from pipeline import loop_memory as lm
from pipeline.transcript_store import InMemoryTranscriptStore


class _Res:
    def __init__(self, ok, value=None, stderr=""):
        self.ok, self.value, self.stderr = ok, value, stderr


def test_record_then_build_context_owner_scoped():
    st = InMemoryTranscriptStore()
    trace = [{"cid": "c0_0", "tool": "sql_query", "inputs": {"sql": "SELECT count(*)"},
              "uses": [], "ok": True}]
    ledger = {"c0_0": _Res(True, [{"cnt": 16}])}
    lm.record_loop_turn(st, "alice", "s1", 1, "有多少视频?", trace, ledger, "16 条视频")
    ctx = lm.build_loop_context(st, "alice", "s1")
    assert ctx and "16 条视频" in ctx and "有多少视频" in ctx and "sql_query" in ctx
    assert lm.build_loop_context(st, "bob", "s1") is None          # 跨 owner 看不到


def test_empty_session_returns_none():
    assert lm.build_loop_context(InMemoryTranscriptStore(), "a", "s") is None


def test_failed_tool_result_recorded_as_error():
    st = InMemoryTranscriptStore()
    trace = [{"cid": "c0_0", "tool": "sql_query", "inputs": {"sql": "bad"}, "uses": [], "ok": False}]
    ledger = {"c0_0": _Res(False, stderr="syntax error near bad")}
    lm.record_loop_turn(st, "a", "s", 1, "q", trace, ledger, "出错了")
    ctx = lm.build_loop_context(st, "a", "s")
    assert "失败" in ctx and "syntax error" in ctx


def test_compaction_triggers_over_budget():
    st = InMemoryTranscriptStore()
    calls = []

    def summ(text):
        calls.append(text)
        return "【摘要】早前问了若干问题"

    for t in range(1, 11):
        lm.record_loop_turn(st, "a", "s", t, "x" * 300, [], {}, "y" * 300)
    ctx = lm.build_loop_context(st, "a", "s", keep=2, token_budget=50, summarize=summ)
    assert calls                                                   # 摘要被调用
    assert "更早对话摘要" in ctx and "【摘要】" in ctx and "最近对话" in ctx
    assert "## 第10轮" in ctx and "## 第9轮" in ctx                 # 最近 2 轮原文


def test_compaction_failopen_when_summarizer_raises():
    st = InMemoryTranscriptStore()
    for t in range(1, 8):
        lm.record_loop_turn(st, "a", "s", t, "x" * 300, [], {}, "y" * 300)
    ctx = lm.build_loop_context(st, "a", "s", keep=2, token_budget=50,
                                summarize=lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert ctx is not None                                         # fail-open,不抛


def test_no_compaction_under_budget():
    st = InMemoryTranscriptStore()
    seen = []
    lm.record_loop_turn(st, "a", "s", 1, "短问", [], {}, "短答")
    ctx = lm.build_loop_context(st, "a", "s", token_budget=100000, summarize=lambda t: seen.append(t))
    assert not seen and "更早对话摘要" not in ctx
