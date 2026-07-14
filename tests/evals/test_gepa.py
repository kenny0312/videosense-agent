"""GD-3(GEPA 进化器)离线单测:闸门数学 / 前沿 / 空间应用回滚 / 提案解析 / 簿记。
全部不碰模型不花钱;真跑冒烟走 evolve --dry + 小额 live(另行)。"""
import json

import pytest

from evals.gepa import frontier, gates, reflect, space, state


# ── gates ────────────────────────────────────────────────────
def test_sign_test_math():
    child = {f"t{i}": 1.0 for i in range(10)}
    parent = {f"t{i}": 0.0 for i in range(10)}
    r = gates.sign_test(child, parent)
    assert r["wins"] == 10 and r["losses"] == 0 and r["significant"]      # 10胜0负 p≈0.001
    r = gates.sign_test({"a": 1, "b": 0}, {"a": 0, "b": 1})
    assert not r["significant"]                                           # 1胜1负 = 抛硬币
    assert gates.sign_test({}, {})["p"] == 1.0                            # 无共有题 → 不显著
    tie = gates.sign_test({"a": 0.5}, {"a": 0.5})
    assert tie["n"] == 0                                                  # 平局丢弃


def test_minibatch_gate_margin():
    assert gates.minibatch_pass({"a": 1.0, "b": 0.4}, {"a": 0.5, "b": 0.4})   # +0.5 > 0.25
    assert not gates.minibatch_pass({"a": 0.6, "b": 0.4}, {"a": 0.5, "b": 0.4})  # +0.1 ≤ margin(治均值回归)
    assert not gates.minibatch_pass({"a": 0.5}, {"a": 0.5})               # 持平不算起色
    assert not gates.minibatch_pass({}, {"a": 1.0})                       # 无共有题 → 不过
    assert gates.minibatch_pass({"a": 0.6}, {"a": 0.5}, margin=0.05)      # margin 可调


def test_crash_scores_zero_not_none(tmp_path, monkeypatch):
    """崩溃是失败:crash 记 0 分计入;infra 记 None 剔除 —— 不许靠崩溃退出比较(审计 B3)。"""
    monkeypatch.setattr(state, "RUNS_DIR", str(tmp_path))
    st = state.RunState("crash-run")
    crash = _fake_rec("t1", {}, cost=0.01)
    crash["status"] = "crash"
    infra = _fake_rec("t2", {}, infra=True)
    st.record_eval("c1", [crash, infra], is_val=True, basis_of={"t1": ["count"], "t2": ["count"]})
    assert st.matrix["c1"]["t1"] == 0.0                       # 崩溃 = 0 分
    assert st.matrix["c1"]["t2"] is None                      # 环境故障 = 剔除
    assert st.val_mean("c1") == 0.0


def test_space_doc_shows_parent_text():
    doc = space.space_doc({"lessons": {"L01": "父本改过的文本"}, "tools": {}})
    assert "父本改过的文本" in doc and "lesson:L01" in doc


def test_to_overrides_new_slot_collision_bumps():
    parent = {"lessons": {"NEW1": "父本新增的"}, "tools": {}}
    child = reflect.to_overrides({"target": "lesson:NEW1", "new_text": "子代新增的"}, parent)
    assert child["lessons"]["NEW1"] == "父本新增的"            # 父本的不被覆盖
    assert child["lessons"]["NEW2"] == "子代新增的"            # 自动顺延


def test_ledger_reserve():
    led = gates.Ledger(20.0, 8.0)
    led.add(11.9)
    assert led.evolution_open()                    # 11.9 < 12
    led.add(0.2)
    assert not led.evolution_open()                # 进化停,但重考/终门的钱还在
    assert not led.exhausted()
    led.add(8.0)
    assert led.exhausted()


# ── frontier ─────────────────────────────────────────────────
def test_pareto_wins_and_dominated():
    m = {"gen0": {"t1": 1.0, "t2": 0.0, "t3": 0.5},
         "c1":   {"t1": 1.0, "t2": 1.0, "t3": 0.5},   # 全面 ≥ gen0 且总分更高 → gen0 出局
         "c2":   {"t1": 0.0, "t2": 0.0, "t3": 1.0}}   # 偏科:独门称王 t3
    front = frontier.pareto_wins(m)
    assert "gen0" not in front                        # 被 c1 支配
    assert front["c1"] == 2 and front["c2"] == 1      # c1 称王 t1,t2;c2 称王 t3
    rng = __import__("random").Random(42)
    picks = {frontier.sample_parent(front, rng) for _ in range(50)}
    assert picks == {"c1", "c2"}                      # 偏科生也有繁殖权


def test_pareto_none_scores_skipped():
    m = {"a": {"t1": None, "t2": 1.0}, "b": {"t1": 0.5, "t2": None}}
    front = frontier.pareto_wins(m)
    assert front == {"a": 1, "b": 1}                  # 环境故障的题不参与比较


# ── space ────────────────────────────────────────────────────
def test_space_validate_rejects_garbage():
    assert space.validate({"lessons": {"L99x": "文本"}})            # 不存在的教训
    assert space.validate({"tools": {"no_such_tool": "文本"}})      # 不存在的工具
    assert space.validate({"lessons": {"L01": "超" * 601}})         # 超长
    assert not space.validate({"lessons": {"L01": "改短一点的合法文本"}})


def test_space_apply_and_reset_roundtrip():
    from pipeline import lessons as _l
    from pipeline import node_specs as _ns
    from pipeline import loop_driver as _ld
    tool = next(iter(_ns.SPECS))
    old_sys = _ld._LOOP_SYSTEM
    old_l01 = next(l.text for l in _l.LESSONS if l.id == "L01")
    try:
        space.apply({"lessons": {"L01": "GEPA测试文本"}, "tools": {tool: "GEPA工具描述"}})
        assert any(l.text == "GEPA测试文本" for l in _l.LESSONS)
        assert _ns.SPECS[tool].planner_desc == "GEPA工具描述"
        assert "GEPA测试文本" in _ld._LOOP_SYSTEM                   # prompt 真重拼了
    finally:
        space.reset()
    assert next(l.text for l in _l.LESSONS if l.id == "L01") == old_l01
    assert _ld._LOOP_SYSTEM == old_sys                              # 字节级还原


def test_space_new_lesson_respects_budget():
    from pipeline import lessons as _l
    free = _l.MAX_LESSONS - len(_l.LESSONS)
    over = {"lessons": {f"NEW{i}": "x" for i in range(1, free + 2)}}
    assert space.validate(over)                                     # 超预算被拒


# ── reflect ──────────────────────────────────────────────────
def test_parse_proposal_variants():
    ok = reflect.parse_proposal(
        '```json\n{"target":"lesson:L04","new_text":"新文本","rationale":"r","cites":["t1"]}\n```')
    assert ok and ok["target"] == "lesson:L04" and ok["cites"] == ["t1"]
    assert reflect.parse_proposal('{"skip": true, "rationale": "全是环境故障"}')["skip"]
    assert reflect.parse_proposal("这不是 JSON") is None
    assert reflect.parse_proposal('{"target":"宪法","new_text":"x"}') is None   # 目标格式非法
    assert reflect.parse_proposal('{"target":"lesson:L01","new_text":""}') is None


def test_to_overrides_inherits_without_mutating_parent():
    parent = {"lessons": {"L01": "父的改动"}, "tools": {}}
    child = reflect.to_overrides({"target": "lesson:L02", "new_text": "子的改动"}, parent)
    assert child["lessons"] == {"L01": "父的改动", "L02": "子的改动"}
    assert parent["lessons"] == {"L01": "父的改动"}                 # 父本没被就地改


# ── state ────────────────────────────────────────────────────
def _fake_rec(tid, scores, passed=False, cost=0.01, infra=False):
    return {"id": tid, "status": "infra_error" if infra else "ok",
            "passed": passed, "scores": scores,
            "question": "q", "expect": {}, "grounding_note": "", "tools": [],
            "cost": {"cost_usd": cost}}


def test_state_bookkeeping(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "RUNS_DIR", str(tmp_path))
    st = state.RunState("test-run")
    st.add_candidate("gen0", None, {})
    cost = st.record_eval("gen0", [
        _fake_rec("t1", {"count": 1.0}, passed=True),
        _fake_rec("t2", {"count": 0.0, "retrieval": 1.0}),
        _fake_rec("t3", {}, infra=True),
    ], is_val=True, basis_of={"t1": ["count"], "t2": ["count", "retrieval"]})
    assert cost == pytest.approx(0.03)
    assert st.matrix["gen0"] == {"t1": 1.0, "t2": 0.5, "t3": None}
    assert st.scores_all["gen0"]["t2"] == 0.5                      # 全账本同步记
    assert "t2" in st.meds["gen0"] and "t1" not in st.meds["gen0"]  # 只有失分题进病历
    assert st.peeks == 1
    st.save()
    st2 = state.RunState.load("test-run")
    assert st2.matrix == st.matrix and st2.spent_usd == st.spent_usd


def test_state_lineage():
    st = state.RunState.__new__(state.RunState)
    st.candidates = {"gen0": {"parent": None}, "c1": {"parent": "gen0"},
                     "c2": {"parent": "c1"}}
    assert st.lineage("c2") == ["gen0", "c1", "c2"]


def test_evolve_dry_smoke(capsys):
    from evals.gepa import evolve
    assert evolve.main(["--dry"]) == 0
    out = capsys.readouterr().out
    assert "train" in out and "val" in out                          # 切分装载成功
