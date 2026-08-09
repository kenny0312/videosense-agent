# -*- coding: utf-8 -*-
"""T1 保真修复:多轮评测车道的记忆必须走【生产同款回放】,不是同一个对话对象跨轮。

生产的多轮 = 每轮新 conversation,跨轮连续性只靠 replay_context
(transcript 落盘 → loop_memory 渲染 → 超预算才压缩)。以前评测 8 轮共用一个
conversation 拿逐字原文历史 —— 量的是另一个系统,多轮分数系统性虚高。

全离线:mock DB + 桩 conversation(抓每轮的 system 串),零 API。
"""
from __future__ import annotations

import os

os.environ.setdefault("REPL_USE_MOCK_DB", "1")

import pytest


@pytest.fixture()
def offline_session(monkeypatch):
    """装假世界 + 桩 conversation。返回 (跑一把的函数, 抓到的每轮 system 列表, conv 对象列表)。"""
    from perception import analyze_video_contextual as avc
    from pipeline import analyze_cache, loop_driver

    real_generate = avc._gemini_generate
    analyze_cache.clear()
    systems, convs = [], []

    class _StubConv:
        last_thoughts = ""

        def __init__(self, answer):
            self._answer = answer

        def send(self, msg):
            return [], self._answer          # 一步文本收口

    def _fake_make_conversation(model, decls, system, image=None):
        systems.append(system)
        c = _StubConv(f"第{len(systems)}轮答案:找到了 sky01。")
        convs.append(c)
        return c

    monkeypatch.setattr(loop_driver, "make_conversation", _fake_make_conversation)

    def _run(script):
        from evals.session import DualControlSession
        return DualControlSession({"user": {"persona": "p", "goal": "g", "script": script}},
                                  owner="mt-probe").run()

    yield _run, systems, convs
    avc._gemini_generate = real_generate
    analyze_cache.clear()


def test_each_turn_gets_a_fresh_conversation(offline_session):
    """每轮必须新建 conversation —— 共用一个 = 拿逐字历史,量的不是生产系统。"""
    run, systems, convs = offline_session
    run([{"utterance": "第一问"}, {"utterance": "第二问", "done": True}])
    assert len(convs) == 2, f"两轮建了 {len(convs)} 个 conversation"
    assert convs[0] is not convs[1]


def test_turn2_memory_arrives_via_production_replay(offline_session):
    """第 2 轮的记忆必须以【生产渲染的回放节】进 system,内含第 1 轮的问与答。

    钉三样:回放节标题(生产 loop_memory 的原文)、轮次标记、上一轮答案文本 ——
    三样都来自 record_loop_turn → build_loop_context 这条生产链,不是我们自己拼的。
    """
    run, systems, convs = offline_session
    run([{"utterance": "库里有滑雪吗"}, {"utterance": "那翼装呢", "done": True}])
    # 判据用回放节的【专属头部】(build_loop_context 的原文,带 # 和后缀):
    # 宪法前缀里本来就有「多轮上下文」四个字(教大脑怎么用回放的那段),裸词会误命中。
    hdr = "# 多轮上下文(这是 follow-up"
    assert hdr not in systems[0], "第 1 轮不该有回放(生产空会话返回 None)"
    s2 = systems[1]
    assert hdr in s2, "第 2 轮没有回放节 —— 记忆机制没接上生产链"
    assert "第1轮" in s2, "回放里没有轮次标记 —— 不是 loop_memory._render_turn 渲染的"
    assert "第1轮答案" in s2, "上一轮的答案没进回放 —— agent 会忘记自己说过什么"
    assert "库里有滑雪吗" in s2, "上一轮的用户问句没进回放"


def test_runtime_facts_track_the_current_turn(offline_session):
    """语言指令与用量累计要跟着【当前轮】走:第 2 轮的 system 里必须有会话累计
    (不再是"第一轮"),语言判定基于第 2 句 —— 以前钉死在第 1 句。"""
    run, systems, _ = offline_session
    run([{"utterance": "第一问"},
         {"utterance": "please answer in English now", "done": True}])
    assert "第一轮,尚无累计" in systems[0]
    assert "本会话到上一轮为止" in systems[1], "第 2 轮没带会话累计 —— usage_cum 替身没接上"
    assert "ENTIRE final answer" in systems[1] or "English" in systems[1], \
        "第 2 轮用户切了英文,语言指令还停在第 1 句的中文判定"


def test_accumulate_usage_shape_matches_runtime_facts_contract():
    """usage_cum 替身的形状必须是 runtime_facts_line 读的那几个键(纯函数单测)。"""
    from evals.session import _accumulate_usage

    c1 = _accumulate_usage(None, {"tokens_total": 100, "cost_usd": 0.01, "llm_calls": 2})
    c2 = _accumulate_usage(c1, {"tokens_total": 50, "cost_usd": 0.02, "llm_calls": 1})
    assert c2["turns"] == 2 and c2["tokens_total"] == 150
    assert c2["cost_usd"] == pytest.approx(0.03) and c2["llm_calls"] == 3
    assert c2["last"] == {"tokens_total": 50, "cost_usd": 0.02}
