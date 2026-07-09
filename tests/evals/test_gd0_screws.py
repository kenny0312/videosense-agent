"""GD-0(GEPA 就绪修螺丝)测试:子集选择 / candidates 防吞 / trace 补全 /
per-task 反馈函数 / prompt 刷新钩子 / 成本字段。"""
import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from evals.runner import load_tasks, filter_tasks, _tools_of
from evals.briefing import task_feedback


# ── S1:--ids 子集选择 ─────────────────────────────────────────
def _tasks():
    return [{"id": "retrieval-park-25"}, {"id": "retrieval-cooking-03"}, {"id": "count-falling-26"}]


def test_filter_tasks_by_exact_id():
    out = filter_tasks(_tasks(), "count-falling-26")
    assert [t["id"] for t in out] == ["count-falling-26"]


def test_filter_tasks_prefix_wildcard_and_list():
    out = filter_tasks(_tasks(), "retrieval-*")
    assert [t["id"] for t in out] == ["retrieval-park-25", "retrieval-cooking-03"]
    out2 = filter_tasks(_tasks(), ["count-falling-26", "retrieval-park-25"])
    assert len(out2) == 2


def test_filter_tasks_none_passthrough_and_missing_raises():
    assert filter_tasks(_tasks(), None) == _tasks()
    with pytest.raises(SystemExit):
        filter_tasks(_tasks(), "no-such-task")          # 手滑不静默跑空


# ── S7:candidates 半成品防吞 ─────────────────────────────────
def test_load_tasks_skips_candidates_files():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "real.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "t1"}) + "\n")
        with open(os.path.join(d, "candidates.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "mined-TODO"}) + "\n")
        ids = [t["id"] for t in load_tasks(d)]
        assert ids == ["t1"]                             # 半成品不入题库


# ── S3:trace 补全(每步 ok + 世界返回)────────────────────────
def test_tools_of_includes_ok_and_output():
    trace = [{"cid": "c0_0", "tool": "sql_query", "inputs": {"sql": "SELECT 1"}, "ok": True},
             {"cid": "c0_1", "tool": "sql_query", "inputs": {"sql": "bad"}, "ok": False}]
    ledger = {"c0_0": SimpleNamespace(ok=True, preview=[{"video_id": "v009"}], stderr=""),
              "c0_1": SimpleNamespace(ok=False, preview=None, stderr="syntax error near bad")}
    out = _tools_of(trace, ledger)
    assert out[0]["ok"] is True and "v009" in out[0]["out"]      # 反思器能看到世界返回了 v009
    assert out[1]["ok"] is False and "syntax error" in out[1]["out"]


def test_tools_of_failopen_without_ledger():
    trace = [{"cid": "c0_0", "tool": "sql_query", "inputs": {}, "ok": True}]
    out = _tools_of(trace, None)
    assert out[0]["tool"] == "sql_query" and "out" not in out[0]  # 没 ledger 不崩、字段缺省


# ── S2:per-task 反馈函数(GEPA 的"梯度")────────────────────
def test_task_feedback_contains_all_sections():
    rec = {"id": "retrieval-park-25", "pinned": True,
           "scores": {"required_actions": 1.0, "retrieval": 0.5},
           "question": "找一下在公园里拍的视频",
           "expect": {"output_checks": {"retrieval": {"must_surface_video_ids": ["v009", "v012"]}}},
           "grounding_note": "v009 标题 Skateboard Tricks Park;v012 Walking Dog in Park",
           "first_fail": {"answer": "找到 1 个", "tools": [
               {"tool": "sql_query", "args": "{\"sql\": \"...park...\"}", "ok": True,
                "out": "[{\"video_id\": \"v012\"}]"}]}}
    txt = task_feedback(rec)
    for piece in ("retrieval-park-25", "必过", "找一下在公园里拍的视频", "找到 1 个",
                  "must_surface_video_ids", "Skateboard Tricks Park", "sql_query", "⇒"):
        assert piece in txt, piece
    assert "栽在" in txt                                  # 挂的尺子被点名


# ── S4:prompt 刷新钩子(同进程候选评估)────────────────────────
def test_refresh_loop_system_picks_up_lessons_change(monkeypatch):
    from pipeline import loop_driver, lessons
    old = loop_driver._LOOP_SYSTEM
    monkeypatch.setattr(lessons, "render", lambda: "- (L99) GEPA 候选试验条目")
    loop_driver.refresh_loop_system()
    try:
        assert "GEPA 候选试验条目" in loop_driver._LOOP_SYSTEM
        assert loop_driver._LOOP_SYSTEM != old
    finally:
        monkeypatch.undo()
        loop_driver.refresh_loop_system()                 # 还原,不污染后续测试
    assert loop_driver._LOOP_SYSTEM == old


# ── S5:runtime_facts 对齐(eval prompt 含「运行时状态」节)──────
def test_liveworld_system_includes_runtime_facts(monkeypatch):
    from pipeline import loop_driver
    captured = {}

    def cap_conv(model, decls, system, image=None):
        captured["system"] = system
        raise RuntimeError("stop-after-capture")          # 只验 prompt,不真跑
    monkeypatch.setattr(loop_driver, "make_conversation", cap_conv)
    from evals.world import LiveWorld
    with pytest.raises(RuntimeError):
        LiveWorld(owner="t").run("有几个滑雪视频?")
    assert "运行时状态" in captured["system"]             # 生产必有的节,eval 不再缺
    assert "中文" in captured["system"] or "语言" in captured["system"]  # 语言指令激活
