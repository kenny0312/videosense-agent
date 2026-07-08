"""跑批器 + 结论单测。会 import pipeline.loop_driver（离线，仍不联网/不碰 DB）。

核心断言：跳伞题里"没查 skydive_segments 就答否定" => 没过。
"""
import os

import evals as _evals_pkg
from evals import report, runner
from evals.fixtures.policies import GOOD, REGRESSED, TOOL_RESULTS

TASKS_DIR = os.path.join(os.path.dirname(_evals_pkg.__file__), "tasks")


def _task(tid):
    return next(t for t in runner.load_tasks(TASKS_DIR) if t["id"] == tid)


def test_good_skydive_passes():
    t = _task("skydive-honesty-01")
    r = runner.run_case(t, GOOD[t["id"]], TOOL_RESULTS[t["id"]], n=5)
    assert r["passed"]
    assert r["scores"]["required_actions"] == 1.0
    assert r["scores"]["honesty"] == 1.0
    assert r["pass_k"]["3"] == 1.0


def test_bad_skydive_fails_the_key_assertion():
    """没查跳伞库就说'没有' => 工具没用对 + 诚实没过 => 整题没过。"""
    t = _task("skydive-honesty-01")
    r = runner.run_case(t, REGRESSED[t["id"]], TOOL_RESULTS[t["id"]], n=5)
    assert not r["passed"]
    assert r["scores"]["required_actions"] == 0.0
    assert r["scores"]["honesty"] == 0.0


def test_suite_baseline_all_pass():
    tasks = runner.load_tasks(TASKS_DIR)
    base = runner.run_suite(tasks, GOOD, TOOL_RESULTS)
    assert runner.classify(base)["kind"] == "ok"


def test_compare_flags_regression_and_blocks():
    tasks = runner.load_tasks(TASKS_DIR)
    base = runner.run_suite(tasks, GOOD, TOOL_RESULTS)
    cur = runner.run_suite(tasks, REGRESSED, TOOL_RESULTS)
    v = runner.classify(cur, base)
    assert v["kind"] == "bad"                              # 必过题失守 -> 打回
    assert any("skydive" in why for why in v["reasons"])


def test_report_renders_html():
    tasks = runner.load_tasks(TASKS_DIR)
    base = runner.run_suite(tasks, GOOD, TOOL_RESULTS)
    html = report.render(base, runner.classify(base))
    assert "<html" in html and "整体通过率" in html


def test_score_multi_offline():
    """多轮判分纯函数：用假 TurnRecord 验证 jga + required_actions 走整场轨迹。"""
    from types import SimpleNamespace as NS

    task = {
        "id": "x", "reward_basis": ["jga", "required_actions"],
        "evaluation_criteria": {
            "required_actions": [{"tool": "sql_query"}],
            "jga_slots": [{"turn": 1, "video_ids": ["v006"]},
                          {"turn": 2, "answer_contains": "60"}],
        },
    }
    turns = [
        NS(who="user_sim", text="有没有做饭的视频", trace=[], ledger={}),
        NS(who="agent", text="有：v006 烤饼干。", trace=[{"tool": "sql_query", "inputs": {"sql": "..."}}], ledger={}),
        NS(who="user_sim", text="第一个多长", trace=[], ledger={}),
        NS(who="agent", text="60 秒。", trace=[], ledger={}),
    ]
    s = runner.score_multi(task, turns)
    assert s["jga"] == 1.0 and s["required_actions"] == 1.0
    turns[3] = NS(who="agent", text="不知道多长", trace=[], ledger={})
    assert runner.score_multi(task, turns)["jga"] == 0.0


def test_dashboard_save_and_rebuild(tmp_path, monkeypatch):
    from evals import dashboard

    monkeypatch.setattr(dashboard, "RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(dashboard, "DASH_PATH", str(tmp_path / "dashboard.html"))
    tasks = runner.load_tasks(TASKS_DIR)
    base = runner.run_suite(tasks, GOOD, TOOL_RESULTS)
    dashboard.save_run(base, runner.classify(base), "scripted")
    path = dashboard.rebuild()
    text = open(path, encoding="utf-8").read()
    assert "评测仪表盘" in text and "历史" in text
