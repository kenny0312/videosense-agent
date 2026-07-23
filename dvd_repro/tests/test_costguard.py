# 费用闸门离线单测:不花一分钱,注入假成本验证三闸+暂停单+令牌续跑。
import json
import os

import pytest

from dvd_repro import config
from dvd_repro.costguard import BudgetGuard, BudgetPause


def _mk(tmp_path, run_id="r1", token=None):
    return BudgetGuard(run_id=run_id, state_dir=str(tmp_path), approve_token=token)


def test_single_call_gate(tmp_path):
    g = _mk(tmp_path)
    with pytest.raises(BudgetPause) as e:
        g.charge(0.51, note="误喂整条视频")
    paused = json.load(open(e.value.paused_path, encoding="utf-8"))
    assert paused["reason"] == "单次调用超闸" and paused["last_call_usd"] == 0.51


def test_run_gate_cumulative(tmp_path):
    g = _mk(tmp_path)
    for _ in range(12):                       # 12 × 0.42 = 5.04 > 5.00
        try:
            g.charge(0.42)
        except BudgetPause as e:
            paused = json.load(open(e.paused_path, encoding="utf-8"))
            assert paused["reason"] == "单场运行超闸"
            assert paused["run_usd"] > config.GUARD_RUN_USD
            return
    pytest.fail("单场闸没触发")


def test_total_gate_across_runs(tmp_path):
    # 每场 10×$0.49=$4.9(单次/单场闸都不触),9 场后累计 $44.1;第十场跨过 $45 触总闸
    for i in range(9):
        g = _mk(tmp_path, run_id=f"run{i}")
        for _ in range(10):
            g.charge(0.49)
    g = _mk(tmp_path, run_id="run9")
    with pytest.raises(BudgetPause) as e:
        for _ in range(10):
            g.charge(0.49)                    # 第 2 笔时累计 45.08 > 45
    paused = json.load(open(e.value.paused_path, encoding="utf-8"))
    assert paused["reason"] == "项目总额超闸"


def test_pause_blocks_restart_and_token_resumes(tmp_path):
    g = _mk(tmp_path)
    with pytest.raises(BudgetPause):
        g.charge(0.9, note="超单次闸")
    # 没带令牌重启 → 拒绝
    with pytest.raises(BudgetPause):
        _mk(tmp_path)
    # 拿令牌重启 → 放行,账目保留(不清零)
    token = json.load(open(os.path.join(tmp_path, "PAUSED.json"), encoding="utf-8"))["resume_token"]
    g2 = _mk(tmp_path, token=token)
    assert g2.spent_run() == pytest.approx(0.9)
    assert not os.path.exists(os.path.join(tmp_path, "PAUSED.json"))       # 暂停单已归档
    assert os.path.exists(os.path.join(tmp_path, "PAUSED.json.resolved"))


def test_normal_flow_no_pause(tmp_path):
    g = _mk(tmp_path)
    for _ in range(50):
        g.charge(0.01, note="正常 caption")
    assert g.spent_run() == pytest.approx(0.5)
    assert not os.path.exists(os.path.join(tmp_path, "PAUSED.json"))
