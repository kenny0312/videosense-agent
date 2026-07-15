"""套件分层单测：闭眼全过进回归、空白bug拖挂留能力、n_for 回归题降到 1 次。"""
from evals import tag_suite
from evals.runner import n_for


def test_classify_saturated_to_regression():
    results = [
        {"id": "easy", "status": "ok", "successes": 3, "n": 3, "kind": "single"},
        {"id": "flaky", "status": "ok", "successes": 1, "n": 3, "kind": "single"},
        {"id": "hard", "status": "ok", "successes": 0, "n": 3, "kind": "single",
         "first_fail": {"answer": "答错了但有内容"}},
        {"id": "infra", "status": "infra_error", "successes": 0, "n": 1},
    ]
    c = tag_suite.classify(results)
    assert c["regression"] == ["easy"]
    assert set(c["capability"]) == {"flaky", "hard", "infra"}   # 环境故障也留能力套件


def test_blank_hit_stays_capability():
    """空白答案 bug 拖挂的题不进回归——它其实 agent 会做。"""
    results = [
        {"id": "blank-multi", "status": "ok", "successes": 1, "n": 2, "kind": "multi",
         "first_fail": {"turns": [{"who": "agent", "text": ""}, {"who": "agent", "text": "有内容"}]}},
    ]
    c = tag_suite.classify(results)
    assert "blank-multi" in c["capability"]
    assert "blank-multi" in c["blank_hit"]


def test_n_for_regression_runs_once():
    reg = {"sat-1"}
    assert n_for({"id": "sat-1", "pinned": False}, None, reg) == 1     # 回归题便宜跑
    assert n_for({"id": "other", "pinned": False}, None, reg) == 3     # 能力普通题 3 次
    assert n_for({"id": "sat-1", "pinned": True}, None, reg) == 5      # 必过题永远 5 次，不因套件降级
    assert n_for({"id": "sat-1", "pinned": False}, 1, reg) == 1        # 显式 --n 照旧
