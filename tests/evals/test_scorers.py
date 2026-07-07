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


def test_refusal_ok_hedged_positive():
    # 真跑抓的冤枉：先肯定后补充"没有直接匹配到标签"的 hedged 回答是合格肯定
    ok = {"expect_positive": True}
    assert scorers.refusal_ok("库里有做饭相关的视频。虽然没有直接匹配到受控标签，但找到了 2 个", ok) == 1.0
    assert scorers.refusal_ok("库里目前没有专门的健身视频。不过有一些跑步元素", ok) == 0.0  # 先否定=没过
    assert scorers.refusal_ok("抱歉，没有翼装视频", ok) == 0.0                                # "没有"里的"有"不算


def test_timestamp_iou_ignores_video_ids():
    # "sky01" 里的 01 不是时间 —— 真跑抓出来的抽取 bug，防回归
    cfg = {"gold_span": [11, 62], "iou_threshold": 0.5}
    assert scorers.timestamp_iou("In sky01, freefall happens from 11.0 seconds to 62.0 seconds.", cfg) == 1.0
    assert scorers.timestamp_iou("视频 sky01 的自由落体在 11 秒到 62 秒之间", cfg) == 1.0
    assert scorers.timestamp_iou("between 62.0 and 65.0 seconds", {"gold_span": [62, 65]}) == 1.0
    assert scorers.timestamp_iou("大概在 100 到 120 秒", cfg) == 0.0


def test_toolseq_arg_contains_supports_pipe():
    trace = [{"tool": "update_memory", "inputs": {"text": "用户不喜欢 makeup 类视频"}}]
    req = [{"tool": "update_memory", "arg_contains": "化妆|makeup"}]
    assert scorers.toolseq_match(trace, req) == 1.0


def test_surface_blob_covers_sidechannel():
    # 收口契约：id 走 show_video 侧信道不进答案文本 —— retrieval 必须看交付面
    class R:
        answer = "为你播放第 1 个和第 2 个做饭视频。"
        trace = [{"cid": "c0_0", "tool": "show_video", "inputs": {"video_ids": ["v006", "v007"]}}]
        ledger = {}
    blob = scorers.surface_blob(R())
    assert scorers.recall_at_k(blob, ["v006", "v007"]) == 1.0
    assert scorers.recall_at_k(R.answer, ["v006", "v007"]) == 0.0   # 只看文本就会冤枉


def test_score_jga_multi_turn_slots():
    titles = {"v006": "Baking Chocolate Chip Cookies", "v007": "Grill Cooking BBQ Ribs"}
    blobs = [
        "找到做饭视频：v006（烤饼干）、v007（烤肉）。",
        "第一个是烤饼干（Baking Chocolate Chip Cookies），时长 60 秒。",
    ]
    slots = [
        {"turn": 1, "video_ids": ["v006", "v007"]},
        {"turn": 2, "resolved_ordinal": {"第一个": "v006"}, "answer_contains": "60"},
    ]
    assert scorers.score_jga(blobs, slots, titles) == 1.0
    bad = [{"turn": 2, "resolved_ordinal": {"第一个": "v007"}}]      # 指代解析错 -> 没过
    assert scorers.score_jga(blobs, bad, titles) == 0.0
    assert scorers.score_jga(["只有一轮"], slots, titles) == 0.0     # 缺轮 -> 没过


def test_case_pass_respects_reward_basis():
    good = {"required_actions": 1.0, "output_checks.honesty": 1.0, "output_checks.retrieval": 1.0}
    assert scorers.case_pass(good, list(good))
    bad = dict(good, required_actions=0.0)
    assert not scorers.case_pass(bad, list(good))
    assert scorers.case_pass({"a": 1.0, "b": 0.0}, ["a"])   # 不在 basis 里的判分器不计入
