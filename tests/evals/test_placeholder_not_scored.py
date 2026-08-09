"""A1 连带回归:系统占位文案绝不许进判分器。

背景(对抗审查发现):A1 让 run_loop 在 max_steps 返回一段兜底文案而不再是 None。
那段文案开头是「这次**没能**给出最终结论」,而 `evals/scorers._NEG_WORDS` 里就有「没能」——
于是 `expect_refusal` / `expect_honest_disclaimer` 这类题会把它判成"agent 诚实地说了没有",
**白拿 1.0**。实测受影响 42 道题,全在 reward_basis 里,而且偏差单向只抬分,
抬得最狠的正是"该说没有"那一类 —— 尺子被我们自己的兜底文案骗过去了。

这是 eval 保真问题,不是产品问题:文案对用户是对的,只是它不该被当成 agent 的回答去判分。
"""
from __future__ import annotations

import pytest

from evals import scorers
from pipeline.loop_driver import MAX_STEPS_ANSWER


def test_placeholder_text_would_fool_the_refusal_scorer():
    """先把【危险本身】钉住:这段文案确实命中 _NEG_WORDS。

    这条不是在测我们的修复,是在测"为什么需要这个修复" —— 哪天有人把文案改得不含
    否定词、以为可以撤掉下面的置空,这条会提醒他真正的问题是【口径】不是【措辞】。
    """
    hits = [w for w in scorers._NEG_WORDS if w in MAX_STEPS_ANSWER]
    assert hits, ("占位文案不再命中 _NEG_WORDS 了 —— 但别据此撤掉置空:"
                  "换个措辞只会把偏差挪到 expect_positive 那一支,口径问题还在")


def _lr(answer, terminated):
    from pipeline.loop_driver import LoopResult
    return LoopResult(answer=answer, steps=1, terminated=terminated, trace=[],
                      ledger={}, llm_calls=1)


@pytest.mark.parametrize("terminated", ["max_steps", "repeat", "tree_guard"])
def test_scripted_world_blanks_non_text_answers(monkeypatch, terminated):
    """ScriptedWorld:非 text 终止 → answer 置空,判分器看不到占位文案。"""
    from evals import world as W
    monkeypatch.setattr(W, "run_loop", lambda *a, **k: _lr(MAX_STEPS_ANSWER, terminated))
    w = W.ScriptedWorld(script=[], tool_results={})
    assert w.run("q").answer == "", f"{terminated} 的占位文案漏进判分了"


def test_scripted_world_keeps_real_answers(monkeypatch):
    """反向锁:正常收口的答案一个字都不许动。"""
    from evals import world as W
    monkeypatch.setattr(W, "run_loop", lambda *a, **k: _lr("库里有 3 个滑雪视频", "text"))
    assert W.ScriptedWorld(script=[], tool_results={}).run("q").answer == "库里有 3 个滑雪视频"
