"""批⑤出题防线的单测：会冤枉人的两类出题陷阱必须被校验器当场抓住。"""
from evals.validate_tasks import _lint_traps


def test_lint_catches_jga_slot_on_action_turn():
    """jga 考点设在上传/入库/贴图那一轮 = 结构性不可通过，必须报错。"""
    bad = {
        "id": "x", "reward_basis": ["jga"],
        "evaluation_criteria": {"jga_slots": [{"turn": 1, "video_ids": ["up_a"]}]},
        "user": {"script": [{"turn": 1, "utterance": "传个视频",
                             "action": {"tool": "upload_video", "video_id": "up_a"}}]},
    }
    assert any("动作宣布轮" in msg for _, msg in _lint_traps(bad))
    # 考点在实质回答轮就没问题
    bad["evaluation_criteria"]["jga_slots"][0]["turn"] = 2
    assert not _lint_traps(bad)


def test_lint_catches_table_task_with_count_proxy():
    """批⑥：列清单/表格题拿 count 当完整性代理必须报错（必过题 23 栽过）。"""
    bad = {"id": "x", "reward_basis": ["required_actions", "count"],
           "user_query": "把库里所有视频列个清单表格给我，带标题和时长", "evaluation_criteria": {}}
    assert any("代理" in msg for _, msg in _lint_traps(bad))
    ok = dict(bad, reward_basis=["required_actions", "retrieval"])
    assert not _lint_traps(ok)


def test_lint_catches_pure_count_with_retrieval():
    """纯计数题（只问几个）把 retrieval 计分 = 冤枉裸报数的完整回答，必须报错。"""
    bad = {"id": "x", "reward_basis": ["count", "retrieval"],
           "user_query": "有几个视频里出现了摔倒的镜头？", "evaluation_criteria": {}}
    assert any("纯计数" in msg for _, msg in _lint_traps(bad))
    # 题面明示要点名（"都是哪几个"）就允许 retrieval 计分
    ok = dict(bad, user_query="有几个摔倒的视频？都是哪几个？")
    assert not _lint_traps(ok)
    # 推荐/挑选类的"几个"本来就是交付意图，不误伤
    ok2 = dict(bad, user_query="给我推荐几个刺激的极限运动视频")
    assert not _lint_traps(ok2)
