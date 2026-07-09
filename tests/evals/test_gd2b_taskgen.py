"""GD-2b 测试:世界 C/D 隔离 / 生成器确定性 / 四向翻转结构 / 生成族同堂。"""
import json

import pytest


@pytest.fixture()
def _world(monkeypatch):
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


def test_world_c_d_seeds_and_isolation(_world):
    mock = _world("C")
    assert _count(mock, "SELECT COUNT(*) n FROM video_metadata") == 20
    assert _count(mock, "SELECT COUNT(*) n FROM skydive_segments") == 0
    assert _count(mock, "SELECT COUNT(DISTINCT video_id) n FROM video_facts "
                        "WHERE predicate='tai chi' AND matched=1") == 1          # c017
    mock = _world("D")
    assert _count(mock, "SELECT COUNT(*) n FROM video_metadata") == 20
    assert _count(mock, "SELECT COUNT(DISTINCT video_id) n FROM video_facts "
                        "WHERE predicate='beekeeping' AND matched=1") == 1       # d004
    mock = _world("A")
    assert _count(mock, "SELECT COUNT(*) n FROM video_metadata") == 16           # A 原样冻结


def test_taskgen_deterministic():
    """同种子两次生成必须逐字节一致 —— 金标可重跑可 diff 的根基。"""
    from evals.task_gen import gen_world, gen_flips
    assert gen_world("C") == gen_world("C")
    assert gen_flips() == gen_flips()


def test_flip_families_one_positive_three_negative():
    """每个翻转实体:4 个世界同一句问话,恰好 1 正 3 负;金标随世界翻。"""
    from evals.task_gen import gen_flips, FLIP_ENTITIES
    flips = gen_flips()
    assert len(flips) == len(FLIP_ENTITIES) * 4
    by_fam: dict = {}
    for t in flips:
        by_fam.setdefault(t["family"], []).append(t)
    for fam, ts in by_fam.items():
        assert len(ts) == 4, fam
        pos = [t for t in ts
               if t["evaluation_criteria"]["output_checks"]["honesty"]["expect_positive"]]
        assert len(pos) == 1, fam
        assert len({t["user_query"] for t in ts}) == 1, fam            # 四题同一句
        assert pos[0]["evaluation_criteria"]["output_checks"]["retrieval"][
            "must_surface_video_ids"], fam                             # 正例必须点名金标


def test_generated_families_never_cross_splits():
    """同族(含四向翻转的 4 题)必须同堂 —— 否则训练堂泄漏另一堂的答案。"""
    from evals.split_tool import MANIFEST_PATH
    m = json.load(open(MANIFEST_PATH, encoding="utf-8"))
    sp, fams = m["splits"], m["families"]
    fam_split: dict = {}
    for tid, f in fams.items():
        if not tid.startswith("gen-"):
            continue
        assert fam_split.setdefault(f, sp[tid]) == sp[tid], (tid, f)


def test_titles_alias_covers_four_worlds():
    from evals.runner import _titles
    t = _titles()
    for vid in ("v001", "b001", "c001", "d001"):
        assert vid in t, vid
