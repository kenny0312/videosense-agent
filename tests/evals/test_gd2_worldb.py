"""GD-2(世界 B)测试:种子切换隔离 / 世界A冻结 / 别名表合并 / 翻转题同堂。"""
import json
import os

import pytest


@pytest.fixture()
def _world(monkeypatch):
    """切世界并强制重灌;结束还原到 A(不污染别的测试)。"""
    import repl._mock_db as mock

    def switch(w):
        monkeypatch.setenv("MOCK_WORLD", w)
        mock._conn = None
        return mock

    yield switch
    monkeypatch.setenv("MOCK_WORLD", "A")
    mock._conn = None


def _count(mock, sql):
    return mock.mock_run_sql(sql)[0]["n"]


def test_world_b_seeds_and_isolation(_world):
    mock = _world("B")
    assert _count(mock, "SELECT COUNT(*) n FROM video_metadata") == 20
    assert _count(mock, "SELECT COUNT(*) n FROM skydive_segments") == 0          # B 无跳伞(空表诚实)
    assert _count(mock, "SELECT COUNT(DISTINCT video_id) n FROM video_facts "
                        "WHERE predicate='swimming' AND matched=1") == 2         # b001+b020
    mock = _world("A")
    assert _count(mock, "SELECT COUNT(*) n FROM video_metadata") == 16           # 世界A原样冻结
    assert _count(mock, "SELECT COUNT(*) n FROM skydive_segments") == 4


def test_world_a_golds_untouched(_world):
    """世界A的封闭世界金标(如『游泳=0』)在 A 里必须依旧成立 —— B 的加入零影响。"""
    mock = _world("A")
    assert _count(mock, "SELECT COUNT(DISTINCT video_id) n FROM video_facts "
                        "WHERE predicate='swimming' AND matched=1") == 0


def test_titles_alias_covers_both_worlds():
    from evals.runner import _titles
    t = _titles()
    assert "v001" in t and "b001" in t and "sky01" in t
    assert t["b008"][0] == "Pottery Wheel Basics"


def test_flip_tasks_same_split_as_absent_family():
    """同句翻转题必须与世界A的 honesty-absent 族同堂(否则训练题泄漏另一堂的答案)。"""
    from evals.split_tool import MANIFEST_PATH
    m = json.load(open(MANIFEST_PATH, encoding="utf-8"))
    sp = m["splits"]
    assert sp["worldb-honesty-flip-cat-01"] == sp["honesty-no-cat-25"]
    assert sp["worldb-honesty-flip-swim-02"] == sp["honesty-no-swimming-01"]


def test_worldb_tasks_all_tagged_world_b():
    from evals.runner import load_tasks
    for t in load_tasks("evals/tasks"):
        if t["id"].startswith("worldb-"):
            assert t.get("world") == "B", t["id"]
        else:
            assert t.get("world", "A") == "A", t["id"]
