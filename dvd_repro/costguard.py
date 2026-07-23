"""费用三级闸门(用户制度 2026-07-18):任何 Gemini 调用后必须 charge()。

三闸(阈值在 config.py,改数字不改代码):
    ① 单次调用 > GUARD_SINGLE_CALL_USD —— 抓"误喂整条视频"级事故
    ② 单场运行累计 > GUARD_RUN_USD     —— 一次建库/一次评测烧到 $5 即停
    ③ 项目总累计 > GUARD_TOTAL_USD     —— 跨运行持久累计,防分批绕闸

触闸行为:进度已由调用方增量落盘(本模块不管业务数据,只管钱),这里做三件事:
    1) 把闸门状态持久化;2) 写 PAUSED.json(已花/卡点/恢复令牌);3) 抛 BudgetPause。
恢复:用户审查后拿 PAUSED.json 里的 resume_token,以 approve_token=... 重启同一 run_id,
从断点续跑,已花账目保留不清零(总闸继续累计)。
"""
from __future__ import annotations

import json
import os
import time
import uuid

from dvd_repro import config


class BudgetPause(RuntimeError):
    """触闸暂停。message 含人话说明;.paused_path 指向 PAUSED.json。"""
    def __init__(self, message: str, paused_path: str):
        super().__init__(message)
        self.paused_path = paused_path


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class BudgetGuard:
    """用法:
        guard = BudgetGuard(run_id="build_lvb001")          # 新跑
        guard.charge(0.012, note="clip 37 caption")          # 每次调用后
        ...触闸 → BudgetPause(进程该退出,进度已在调用方落盘)
        guard = BudgetGuard(run_id="build_lvb001", approve_token="<PAUSED里的令牌>")  # 审查后续跑
    """

    def __init__(self, run_id: str, state_dir: "str | None" = None,
                 approve_token: "str | None" = None):
        self.run_id = run_id
        self.state_dir = state_dir or config.RESULTS_DIR
        self.state_path = os.path.join(self.state_dir, "budget_state.json")
        self.paused_path = os.path.join(self.state_dir, "PAUSED.json")
        self._state = _read_json(self.state_path, {"total_usd": 0.0, "runs": {}})
        self._state["runs"].setdefault(run_id, 0.0)

        paused = _read_json(self.paused_path, None)
        if paused is not None:
            if approve_token and approve_token == paused.get("resume_token"):
                # 审查通过:归档暂停单,继续(账目保留)
                _write_json(self.paused_path + ".resolved", {**paused, "resolved_at": _now()})
                os.remove(self.paused_path)
            else:
                raise BudgetPause(
                    f"存在未审查的暂停单({paused.get('reason','?')}, 已花 run="
                    f"${paused.get('run_usd', 0):.2f} / total=${paused.get('total_usd', 0):.2f})。"
                    f"审查 {self.paused_path} 后携 approve_token 重启。", self.paused_path)

    # ── 核心:记一笔并三闸检查 ──
    def charge(self, cost_usd: float, note: str = "") -> None:
        cost_usd = max(0.0, float(cost_usd))
        self._state["runs"][self.run_id] += cost_usd
        self._state["total_usd"] += cost_usd
        _write_json(self.state_path, self._state)   # 先记账再判闸:暂停单上的数字是含本笔的真账

        if cost_usd > config.GUARD_SINGLE_CALL_USD:
            self._pause("单次调用超闸", cost_usd, note,
                        f"单次 ${cost_usd:.3f} > ${config.GUARD_SINGLE_CALL_USD:.2f}")
        if self._state["runs"][self.run_id] > config.GUARD_RUN_USD:
            self._pause("单场运行超闸", cost_usd, note,
                        f"本场累计 ${self._state['runs'][self.run_id]:.2f} > ${config.GUARD_RUN_USD:.2f}")
        if self._state["total_usd"] > config.GUARD_TOTAL_USD:
            self._pause("项目总额超闸", cost_usd, note,
                        f"项目累计 ${self._state['total_usd']:.2f} > ${config.GUARD_TOTAL_USD:.2f}")

    def _pause(self, reason: str, last_cost: float, note: str, detail: str) -> None:
        token = uuid.uuid4().hex[:12]
        _write_json(self.paused_path, {
            "reason": reason, "detail": detail, "note": note,
            "last_call_usd": round(last_cost, 4),
            "run_id": self.run_id,
            "run_usd": round(self._state["runs"][self.run_id], 4),
            "total_usd": round(self._state["total_usd"], 4),
            "resume_token": token, "paused_at": _now(),
        })
        raise BudgetPause(
            f"[费用闸门] {reason}:{detail}(最近一笔 ${last_cost:.3f} {note})。"
            f"进度已随增量落盘保留;审查 {self.paused_path} 后用 "
            f"approve_token='{token}' 续跑。", self.paused_path)

    # ── 报表 ──
    def spent_run(self) -> float:
        return self._state["runs"][self.run_id]

    def spent_total(self) -> float:
        return self._state["total_usd"]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class UsageMeter:
    """从仓库 usage 记账里读增量成本(调用前 snapshot,调用后 delta 交给 guard.charge)。
    兼容 agentops 重构前后两个路径。"""

    def __init__(self):
        try:
            from pipeline.agentops import usage as _u
        except ImportError:                      # 老布局兜底
            from pipeline import usage as _u
        self._u = _u
        # 关键:_USAGE 是 ContextVar 默认 None,不 reset 一次 add_usage 全程空转记 $0
        # (四跑事故:三条视频跑完闸门显示 $0.00)。dvd 脚本独占进程,reset 无副作用。
        try:
            self._u.reset_usage()
        except Exception:
            pass
        self._last = self._cost()

    def _cost(self) -> float:
        try:
            return float(self._u.summarize().get("cost_usd", 0.0) or 0.0)
        except Exception:
            return 0.0

    def delta(self) -> float:
        """自上次调用以来新增的成本(不回退:usage 被 reset 时归零重来)。"""
        now = self._cost()
        d = now - self._last
        if d < 0:                                # 上游 reset_usage 了
            d = now
        self._last = now
        return max(0.0, d)
