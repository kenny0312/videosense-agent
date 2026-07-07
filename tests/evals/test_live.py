"""Mode B（真 Gemini）冒烟测试 —— 默认跳过（要 GCP 凭证 + 花 token）。

设 `RUN_LIVE_EVAL=1` 且配好 GCP_PROJECT/ADC 才真跑，避免默认测试套件产生花费。
"""
import os

import pytest

RUN_LIVE = os.environ.get("RUN_LIVE_EVAL") == "1"


@pytest.mark.skipif(not RUN_LIVE, reason="Mode B 要 GCP 凭证 + 花 token；设 RUN_LIVE_EVAL=1 才跑")
def test_live_skydive_smoke():
    from evals import runner
    from evals.world import live_preflight

    import evals as _evals_pkg

    assert live_preflight() is None, "GCP 凭证没配好"
    t = next(x for x in runner.load_tasks(os.path.join(os.path.dirname(_evals_pkg.__file__), "tasks"))
             if x["id"] == "skydive-honesty-01")
    r = runner.run_case(t, live=True, n=1)
    assert r["answer"] is not None


def test_live_preflight_gives_helpful_message_when_no_creds():
    """没配凭证时预检应给出清晰的"怎么配"说明（本环境正是如此）。"""
    from evals.world import live_preflight

    msg = live_preflight()
    if msg is not None:                    # 配好了凭证则为 None，跳过断言
        assert "GCP_PROJECT" in msg and "REPL_USE_MOCK_DB" in msg
