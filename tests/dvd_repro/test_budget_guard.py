"""BudgetGuard 手搓练习的【判卷老师】。

分四步跑,一步绿了再写下一步:
    python -m pytest tests/dvd_repro -k step1 -q      # 三道闸(单进程,不落盘也能过大半)
    python -m pytest tests/dvd_repro -k step2 -q      # 落盘 + 断点续跑
    python -m pytest tests/dvd_repro -k step3 -q      # 暂停单 / 令牌 / 基线  ← 最难最值钱
    python -m pytest tests/dvd_repro -k step4 -q      # 并发(锁内重读)+ 暂停单撞车
    python -m pytest tests/dvd_repro -q               # 全绿 = 复现成功

两条"招牌题"(第一版最容易写错、review 才抓出来的):
  · step3_approving_single_gate_does_not_move_total_baseline
  · step3_approving_run_gate_does_not_widen_total
  它们盯的是同一件事:批准一道闸,不许顺手把别的闸的窗口撑大。

  step4_no_lost_update 盯的是另一件:charge 必须【锁内重读磁盘】再累加,
  拿内存快照累加的写法在单进程测试里一路绿灯,一上多进程立刻现原形。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dvd_repro import budget, config                      # noqa: E402
from dvd_repro.budget import BudgetGuard, BudgetPause      # noqa: E402

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_budget_worker.py")


# ── 夹具:把闸值调成小而整的数,测试才快且不受默认值变动影响 ──────────────
@pytest.fixture
def gcfg(monkeypatch):
    """常规三闸:单次 0.5 / 单场 2.0 / 总额 5.0"""
    monkeypatch.setattr(config, "GUARD_SINGLE_CALL_USD", 0.5)
    monkeypatch.setattr(config, "GUARD_RUN_USD", 2.0)
    monkeypatch.setattr(config, "GUARD_TOTAL_USD", 5.0)


@pytest.fixture
def gcfg_run_off(monkeypatch):
    """只留 单次 0.5 与 总额 1.0(单场闸关掉),用来单独考"批准单次闸"的副作用"""
    monkeypatch.setattr(config, "GUARD_SINGLE_CALL_USD", 0.5)
    monkeypatch.setattr(config, "GUARD_RUN_USD", 1e9)
    monkeypatch.setattr(config, "GUARD_TOTAL_USD", 1.0)


@pytest.fixture
def gcfg_single_off(monkeypatch):
    """只留 单场 1.0 与 总额 2.0(单次闸关掉),用来单独考"批准单场闸"的副作用"""
    monkeypatch.setattr(config, "GUARD_SINGLE_CALL_USD", 1e9)
    monkeypatch.setattr(config, "GUARD_RUN_USD", 1.0)
    monkeypatch.setattr(config, "GUARD_TOTAL_USD", 2.0)


# ── 小工具 ─────────────────────────────────────────────────────────
def _paused(tmp_path) -> dict:
    return json.loads((tmp_path / "PAUSED.json").read_text(encoding="utf-8"))


def _token(capsys) -> str:
    """从 stderr 里抓明文令牌(它只该出现在这儿)。"""
    err = capsys.readouterr().err
    m = re.search(r"approve_token='([0-9a-f]+)'", err)
    assert m, f"令牌没打到 stderr,或格式不是 approve_token='xxx':\n{err!r}"
    return m.group(1)


def _burn(g, each=0.1, times=100):
    """一直小额记账直到触闸,返回那个异常。"""
    with pytest.raises(BudgetPause) as ei:
        for _ in range(times):
            g.charge(each, note="压测")
    return ei.value


# ══ step1:三道闸 ═══════════════════════════════════════════════════
def test_step1_small_charges_pass(tmp_path, gcfg):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    for _ in range(5):
        g.charge(0.01, note="正常调用")
    assert g.spent_run() == pytest.approx(0.05)
    assert g.spent_total() == pytest.approx(0.05)


def test_step1_single_call_gate(tmp_path, gcfg):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    with pytest.raises(BudgetPause):
        g.charge(0.9, note="一笔离谱的账")
    assert _paused(tmp_path)["reason"] == budget.REASON_SINGLE
    # 先记账后判闸:触闸的这笔钱必须已经入账,否则续跑时账目对不上
    assert g.spent_total() == pytest.approx(0.9)


def test_step1_run_gate(tmp_path, gcfg):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    _burn(g)
    assert _paused(tmp_path)["reason"] == budget.REASON_RUN


def test_step1_total_gate_across_runs(tmp_path, gcfg):
    """每场 1.9 都不碰单场闸(2.0),但三场累计 5.7 会撞总额闸(5.0)。"""
    err = None
    for i in range(3):
        g = BudgetGuard(f"r{i}", state_dir=str(tmp_path))
        try:
            for _ in range(19):
                g.charge(0.1)
        except BudgetPause as e:
            err = e
            break
    assert err is not None, "三场共 5.7 应当撞上项目总额闸"
    assert json.loads(Path(err.paused_path).read_text(encoding="utf-8"))["reason"] == budget.REASON_TOTAL


def test_step1_negative_cost_does_not_reduce(tmp_path, gcfg):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    g.charge(0.4)
    g.charge(-10.0)                      # 负数不许倒扣账本(否则可以拿它绕闸)
    assert g.spent_total() == pytest.approx(0.4)


# ══ step2:落盘 + 断点续跑 ═══════════════════════════════════════════
def test_step2_state_survives_restart(tmp_path, gcfg):
    BudgetGuard("r1", state_dir=str(tmp_path)).charge(0.3)
    again = BudgetGuard("r1", state_dir=str(tmp_path))
    assert again.spent_run() == pytest.approx(0.3)
    assert again.spent_total() == pytest.approx(0.3)


def test_step2_runs_separate_but_total_shared(tmp_path, gcfg):
    BudgetGuard("r1", state_dir=str(tmp_path)).charge(0.3)
    b = BudgetGuard("r2", state_dir=str(tmp_path))
    b.charge(0.2)
    assert b.spent_run() == pytest.approx(0.2)
    assert b.spent_total() == pytest.approx(0.5)


def test_step2_corrupt_state_is_survivable(tmp_path, gcfg):
    (tmp_path / "budget_state.json").write_text("{半截坏掉的 JSON", encoding="utf-8")
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    g.charge(0.1)
    assert g.spent_total() == pytest.approx(0.1)


# ══ step3:暂停单 / 令牌 / 基线 ══════════════════════════════════════
def test_step3_pause_file_stores_hash_not_plaintext(tmp_path, gcfg, capsys):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    _burn(g)
    token = _token(capsys)
    raw = (tmp_path / "PAUSED.json").read_text(encoding="utf-8")
    assert token not in raw, "暂停单里不许出现明文令牌(只存 hash)"
    assert json.loads(raw)["resume_hash"] == budget._hash(token)


def test_step3_token_absent_from_exception_message(tmp_path, gcfg, capsys):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    err = _burn(g)
    token = _token(capsys)
    assert token not in str(err), \
        "异常 message 里不许带令牌 —— 否则自动化调用方 except 一把抠出来就能自解闸"


def test_step3_restart_blocked_without_token(tmp_path, gcfg, capsys):
    _burn(BudgetGuard("r1", state_dir=str(tmp_path)))
    capsys.readouterr()
    with pytest.raises(BudgetPause):
        BudgetGuard("r1", state_dir=str(tmp_path))


def test_step3_restart_blocked_with_wrong_token(tmp_path, gcfg, capsys):
    _burn(BudgetGuard("r1", state_dir=str(tmp_path)))
    capsys.readouterr()
    with pytest.raises(BudgetPause):
        BudgetGuard("r1", state_dir=str(tmp_path), approve_token="deadbeefcafe")


def test_step3_unresolved_pause_blocks_every_run(tmp_path, gcfg, capsys):
    """一张未审查的暂停单挡住【任何】run_id 启动 —— 全局刹车,有意为之。"""
    _burn(BudgetGuard("r1", state_dir=str(tmp_path)))
    capsys.readouterr()
    with pytest.raises(BudgetPause):
        BudgetGuard("完全不相干的另一场", state_dir=str(tmp_path))


def test_step3_correct_token_resumes(tmp_path, gcfg, capsys):
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    _burn(g)
    token = _token(capsys)

    resumed = BudgetGuard("r1", state_dir=str(tmp_path), approve_token=token)
    assert not (tmp_path / "PAUSED.json").exists(), "审查通过后暂停单要删掉"
    assert list(tmp_path.glob("*.resolved")), "审查过的暂停单要留痕(.resolved)"
    resumed.charge(0.1)                  # 基线已抬高,继续跑不该再触闸
    resumed.charge(0.1)


def test_step3_approving_single_gate_does_not_move_total_baseline(tmp_path, gcfg_run_off, capsys):
    """★招牌题:批准"单次调用超闸"是一次性事件,不许动任何基线。

    若实现把三道闸基线无差别重置,总额闸的窗口会被悄悄撑大 —— 最后那笔就不会触闸,本例失败。
    """
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    with pytest.raises(BudgetPause):
        g.charge(0.6)                    # 0.6 > 单次闸 0.5
    token = _token(capsys)

    g = BudgetGuard("r1", state_dir=str(tmp_path), approve_token=token)
    g.charge(0.3)                        # 累计 0.9 < 总额闸 1.0 → 不该触闸
    with pytest.raises(BudgetPause):     # 累计 1.1 > 1.0 → 必须触闸
        g.charge(0.2)
    assert _paused(tmp_path)["reason"] == budget.REASON_TOTAL


def test_step3_approving_run_gate_does_not_widen_total(tmp_path, gcfg_single_off, capsys):
    """★招牌题:批准"单场运行超闸"只抬本场基线,总额闸的窗口必须原封不动。"""
    g = BudgetGuard("r1", state_dir=str(tmp_path))
    with pytest.raises(BudgetPause):
        for _ in range(5):
            g.charge(0.25)               # 本场 1.25 > 单场闸 1.0
    assert _paused(tmp_path)["reason"] == budget.REASON_RUN
    token = _token(capsys)

    g = BudgetGuard("r1", state_dir=str(tmp_path), approve_token=token)
    with pytest.raises(BudgetPause):
        for _ in range(4):               # 总额到 2.25 > 总额闸 2.0
            g.charge(0.25)
    assert _paused(tmp_path)["reason"] == budget.REASON_TOTAL, \
        "总额基线被批准单场闸时顺手抬高了 → 总闸名存实亡"


# ══ step4:并发 ═════════════════════════════════════════════════════
def test_step4_no_lost_update_across_processes(tmp_path):
    """4 个进程 × 100 笔 × $0.01 = $4.00。少一分就是"读-改-写"丢更新。

    拿 __init__ 时的内存快照累加的写法,前面三步全绿,这里必挂。
    """
    procs = [subprocess.Popen([sys.executable, WORKER, str(tmp_path), f"w{i}", "100"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i in range(4)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, f"工人进程崩了:\n{err.decode('utf-8', 'replace')}"

    state = json.loads((tmp_path / "budget_state.json").read_text(encoding="utf-8"))
    assert state["total_usd"] == pytest.approx(4.0, abs=1e-6), \
        f"总账应为 $4.00,实际 ${state['total_usd']} —— 丢更新了"
    for i in range(4):
        assert state["runs"][f"w{i}"] == pytest.approx(1.0, abs=1e-6)


def test_step4_pause_collision_keeps_first_note(tmp_path, gcfg, capsys):
    """两个进程同时触闸:先写者的暂停单不许被覆盖(人手里那张令牌会作废),后来者另立附单。"""
    g = BudgetGuard("r1", state_dir=str(tmp_path))          # 建实例时还没有暂停单
    first = {"reason": "别人先触的闸", "resume_hash": "deadbeef"}
    (tmp_path / "PAUSED.json").write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")

    err = _burn(g)
    assert _paused(tmp_path) == first, "先写者的暂停单被覆盖了 —— 他手上的令牌当场作废"
    assert Path(err.paused_path) != (tmp_path / "PAUSED.json"), "后来者应另立附单"
    assert Path(err.paused_path).exists()
