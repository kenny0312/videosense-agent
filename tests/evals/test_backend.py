"""批④离线测试：假后端上传真落库、状态断言判分、挖题、裁判优雅跳过。
不联网、不碰真服务。"""
import json
import os

os.environ["REPL_USE_MOCK_DB"] = "1"


def test_upload_lands_in_fake_db():
    """上传新视频后，假库里真能查到它 + world_state 账本记上了。"""
    from evals.world import EvalBackend
    import repl._mock_db as mock

    b = EvalBackend().install()
    b.upload("up_test_ski", title="Test Ski", activities=["skiing"], duration=42)
    rows = mock.mock_run_sql("SELECT video_id FROM video_metadata WHERE video_id='up_test_ski'")
    assert rows and rows[0]["video_id"] == "up_test_ski"
    facts = mock.mock_run_sql("SELECT predicate FROM video_facts WHERE video_id='up_test_ski'")
    assert any(f["predicate"] == "skiing" for f in facts)
    assert "up_test_ski" in b.world_state["uploads"]


def test_enrich_and_memory_recorded():
    from evals.world import EvalBackend

    b = EvalBackend().install()
    b.enrich("v006")
    assert "v006" in b.world_state["enriched"]
    from pipeline import user_memory
    user_memory.update("eval", "用户只看翼装 wingsuit")
    assert "wingsuit" in b.world_state["memory"]


def test_state_assertions_scorer():
    from evals import scorers

    ws = {"uploads": ["up_yoga_new"], "enriched": ["v006"], "memory": "只看 wingsuit"}
    assert scorers.score_state_assertions(
        [{"surface": "uploads", "expect_contains": "up_yoga_new"}], ws) == 1.0
    assert scorers.score_state_assertions(
        [{"surface": "memory", "expect_contains": "wingsuit"}], ws) == 1.0
    assert scorers.score_state_assertions(
        [{"surface": "uploads", "expect_contains": "up_missing"}], ws) == 0.0


def test_cosine_semantic_search_ranking():
    """内存语义检索排序：和 query 越像的排前面，低于阈值标 weak（离线，用手造向量）。"""
    from evals.world import build_cosine_search

    index = [
        ("v006", "baking cookies", 0, 60, [1.0, 0.0, 0.0]),
        ("v007", "grilling ribs", 0, 75, [0.0, 1.0, 0.0]),
        ("sky01", "wingsuit flight", 0, 130, [0.0, 0.0, 1.0]),
    ]
    search = build_cosine_search(index, weak_threshold=0.6)
    rows = search(json.dumps([0.9, 0.1, 0.0]), 3)     # 最像 v006
    assert rows[0]["video_id"] == "v006" and rows[0]["relevance"] == "strong"
    assert rows[-1]["relevance"] == "weak"            # 正交的那条=弱相关


def test_note_image_bytes():
    from evals.world import make_note_image

    data, mime = make_note_image("hello")
    assert mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"


def test_mine_failures_offline():
    from evals.mine_failures import mine_rows

    rows = [
        {"request_id": "a1", "status": "error", "query": "有没有游泳视频"},
        {"request_id": "b2", "terminated_reason": "max_steps", "query": "找最精彩的"},
        {"request_id": "c3", "status": "ok", "cost_usd": 0.01, "query": "正常请求"},
        {"request_id": "d4", "analyze_calls": 6, "query": "挨个看一遍"},
    ]
    cands = mine_rows(rows)
    ids = {c["id"] for c in cands}
    assert len(cands) == 3                       # 正常请求不挖
    assert "mined-a1" in ids and "mined-d4" in ids


def test_judge_skips_without_key(monkeypatch, tmp_path):
    from evals import judge

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert judge.available() is False
    p = tmp_path / "r.results.jsonl"
    p.write_text(json.dumps({"id": "x", "answer": "a",
                             "expect": {"nl_assertions": ["要有理由"]}}) + "\n", encoding="utf-8")
    assert judge.judge_results(str(p)) == 0       # 没 key 优雅返回，不报错
