"""
前置 Router 的轻量单元测试 —— 不依赖 GCP / DB(全用桩)。
    python -m pipeline.test_router

覆盖:
  parse_verdict : 合法解析 / 畸形 fail-open / 缺字段默认
  should_refuse : 高置信拒 / 低置信放行 / answer 放行
  orchestrator  : refuse/smalltalk → 前置返回,【不进 loop】;
                  answer / 低置信refuse → 越过 Router 到达 loop(M7b 起唯一执行路径)
"""
from __future__ import annotations

import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pipeline.loop_driver as loop_driver
import pipeline.orchestrator as orch
from pipeline.router import RouterVerdict, parse_verdict, should_refuse


# ── parse_verdict ─────────────────────────────────────────
def test_parse_good():
    v = parse_verdict({"decision": "refuse", "confidence": 0.8, "reason": "x", "intent": "meta"})
    assert v.decision == "refuse" and v.confidence == 0.8 and v.intent == "meta"


def test_parse_malformed_failopen():
    for bad in [None, "not json", 123, {"decision": "banana"}]:
        v = parse_verdict(bad)
        assert v.decision == "answer", bad   # 畸形 → fail-open


def test_parse_missing_defaults():
    v = parse_verdict({})
    assert v.decision == "answer" and v.turn_type == "new"


def test_parse_bad_turn_type_failopen():
    v = parse_verdict({"decision": "answer", "turn_type": "banana"})
    assert v.turn_type == "new"   # 未知轮型 → 归 new


def test_parse_smalltalk_ok():
    v = parse_verdict({"decision": "smalltalk", "confidence": 0.9})
    assert v.decision == "smalltalk"   # 不被强制改成 answer


# ── should_refuse ─────────────────────────────────────────
def test_should_refuse():
    assert should_refuse(RouterVerdict(decision="refuse", confidence=0.9))
    assert not should_refuse(RouterVerdict(decision="refuse", confidence=0.3))   # 低置信放行
    assert not should_refuse(RouterVerdict(decision="answer", confidence=0.99))


# ── orchestrator 分支(桩,无 GCP)──────────────────────────
def _stub_orch(verdict):
    orig = (orch.mcp_client, orch.Router)
    orch.mcp_client = types.SimpleNamespace(
        get_schema=lambda: {"video_metadata": [{"column": "id", "type": "int"}]})

    class FakeRouter:
        def judge(self, q, **kw):
            return verdict
    orch.Router = FakeRouter
    return orig


def _restore_orch(orig):
    orch.mcp_client, orch.Router = orig


def _stub_loop(run):
    """把 loop 入口换成 run(可抛异常以证明"到达了 loop")。无 session 时记忆侧不触发。"""
    saved = loop_driver.run_query_loop
    loop_driver.run_query_loop = run
    return saved


def _restore_loop(saved):
    loop_driver.run_query_loop = saved


def test_orch_refuses_and_skips_loop():
    orig = _stub_orch(RouterVerdict(decision="refuse", confidence=0.9, reason="测试拒答"))

    def boom(*a, **k):
        raise AssertionError("refuse 时不应进入 loop")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("what is the first video above")
        assert r["status"] == "refused", r["status"]
        assert r["reason"] == "测试拒答", r["reason"]
        assert r["ok"] is False
    finally:
        _restore_loop(sl); _restore_orch(orig)


def test_orch_answer_reaches_loop():
    orig = _stub_orch(RouterVerdict(decision="answer", confidence=0.95))

    def boom(*a, **k):
        raise RuntimeError("REACHED_LOOP")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("How many videos")
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r
    finally:
        _restore_loop(sl); _restore_orch(orig)


def test_orch_smalltalk():
    from pipeline.router import SMALLTALK_REPLY
    orig = _stub_orch(RouterVerdict(decision="smalltalk", confidence=0.95))
    orig_st = orch.smalltalk_reply

    def boom(*a, **k):
        raise AssertionError("smalltalk 时不应进入 loop")
    sl = _stub_loop(boom)
    try:
        # 生成失败(返回 None)→ 回退固定俏皮回复
        orch.smalltalk_reply = lambda q: None
        r = orch.run_query("who are you")
        assert r["status"] == "smalltalk", r["status"]
        assert r["answer"] == SMALLTALK_REPLY and "Kenny Qiu" in r["answer"]
        # 生成成功 → 用生成的【可变】回复(不再被锁死成一句)
        orch.smalltalk_reply = lambda q: "嗨,我能帮你分析视频~"
        r2 = orch.run_query("hi there")
        assert r2["status"] == "smalltalk" and r2["answer"] == "嗨,我能帮你分析视频~", r2["answer"]
    finally:
        orch.smalltalk_reply = orig_st
        _restore_loop(sl); _restore_orch(orig)


def test_orch_lowconf_refuse_failopen():
    orig = _stub_orch(RouterVerdict(decision="refuse", confidence=0.3, reason="低置信"))

    def boom(*a, **k):
        raise RuntimeError("REACHED_LOOP")
    sl = _stub_loop(boom)
    try:
        r = orch.run_query("something")
        assert r["status"] == "error" and "REACHED_LOOP" in r["error"], r   # 没拒,放行到 loop
    finally:
        _restore_loop(sl); _restore_orch(orig)


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
