"""判分函数单测 —— 纯函数，不 import pipeline（不联网、不碰 DB）。"""
from evals import scorers


def test_toolseq_match_pass_and_fail():
    trace = [
        {"tool": "sql_query", "inputs": {"sql": "SELECT * FROM skydive_segments"}},
        {"tool": "sql_query", "inputs": {"sql": "predicate ILIKE '%wingsuit%'"}},
    ]
    req = [
        {"tool": "sql_query", "arg_contains": "skydive_segments"},
        {"tool": "sql_query|semantic_search", "arg_contains": "wingsuit"},
    ]
    assert scorers.toolseq_match(trace, req) == 1.0
    assert scorers.toolseq_match([], req) == 0.0            # 一步没查 -> 没过


def test_refusal_ok():
    assert scorers.refusal_ok("库里有翼装视频 sky_003", {"expect_positive": True}) == 1.0
    assert scorers.refusal_ok("抱歉，没有翼装视频", {"expect_positive": True}) == 0.0
    assert scorers.refusal_ok("这个问题我无法回答", {"expect_refusal": True}) == 1.0
    assert scorers.refusal_ok("有的，肯定有", {"expect_refusal": True}) == 0.0


def test_recall_at_k():
    assert scorers.recall_at_k("见 vid_cook_a、vid_cook_b", ["vid_cook_a", "vid_cook_b"]) == 1.0
    assert scorers.recall_at_k("只找到 vid_cook_a", ["vid_cook_a", "vid_cook_b"]) == 0.5
    assert scorers.recall_at_k("啥也没有", ["x"]) == 0.0


def test_passk():
    assert scorers.passk(5, 5, 3) == 1.0
    assert scorers.passk(0, 5, 3) == 0.0
    assert scorers.passk(3, 5, 1) == 0.6
    assert scorers.passk(1, 1, 3) is None                  # 样本不够


def test_no_id_leak():
    assert scorers.no_id_leak("为你播放第 1 个：翼装集锦") == 1.0
    assert scorers.no_id_leak("播放 v_-02DygXbn6w 和 v_AbCdEfGh12") == 0.0   # 原始 id 泄漏
    assert scorers.no_id_leak("见 sky_003") == 1.0                            # 友好 id 不算泄漏


def test_no_provider_leak():
    assert scorers.no_provider_leak("我的窗口约 100 万 token") == 1.0
    assert scorers.no_provider_leak("我是 Google 训练的大型语言模型") == 0.0


def test_answer_count():
    assert scorers.answer_count("用了约 4154 个 token", {"expected": 4154}) == 1.0
    assert scorers.answer_count("用了约 21 个", {"expected": 4154}) == 0.0
    assert scorers.answer_count("窗口约 100 万", {"expected": 100}) == 1.0


def test_case_pass_respects_reward_basis():
    good = {"required_actions": 1.0, "output_checks.honesty": 1.0, "output_checks.retrieval": 1.0}
    assert scorers.case_pass(good, list(good))
    bad = dict(good, required_actions=0.0)
    assert not scorers.case_pass(bad, list(good))
    assert scorers.case_pass({"a": 1.0, "b": 0.0}, ["a"])   # 不在 basis 里的判分器不计入
