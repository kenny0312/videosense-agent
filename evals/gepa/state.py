"""簿记:候选谱系 + 分数矩阵 + 病历 + 偷看/花费台账,落盘可续跑。

state.json 一份全量(原子写);events.jsonl 追加流水(出生/淘汰/评估/闸门判决),
两者都进 runs/<运行id>/,gitignored —— 进 git 的只有最终 report.md 里的结论摘要。
"""
from __future__ import annotations

import json
import os
import time

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")


class RunState:
    """一轮进化的全部家当。

    candidates: {cid: {parent, gen, overrides, rationale, cites}}
    matrix:     {cid: {task_id: score∈[0,1] | None(环境故障)}}   —— val 记分板
    scores_all: {cid: {task_id: score}}   —— 全部评估的分(train/minibatch 也记,
                准入小考拿它做父子对照;val 记分板只认 matrix)
    meds:       {cid: {task_id: 病历文本}}                        —— 反思器燃料
    passed:     {cid: {task_id: bool}}                            —— 必过门用
    peeks / spent_usd / gen:台账
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = os.path.join(RUNS_DIR, run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.candidates: dict = {}
        self.matrix: dict = {}
        self.scores_all: dict = {}
        self.meds: dict = {}
        self.passed: dict = {}
        self.peeks = 0
        self.spent_usd = 0.0
        self.gen = 0

    # ── 持久化 ──────────────────────────────────────────
    def save(self) -> None:
        tmp = os.path.join(self.dir, "state.json.tmp")
        data = {k: getattr(self, k) for k in
                ("run_id", "candidates", "matrix", "scores_all", "meds", "passed",
                 "peeks", "spent_usd", "gen")}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.dir, "state.json"))

    @classmethod
    def load(cls, run_id: str) -> "RunState":
        st = cls(run_id)
        with open(os.path.join(st.dir, "state.json"), encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            setattr(st, k, v)
        return st

    def journal(self, kind: str, **kw) -> None:
        row = {"t": time.strftime("%H:%M:%S"), "kind": kind, **kw}
        with open(os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ── 记账 ────────────────────────────────────────────
    def add_candidate(self, cid: str, parent: "str | None", overrides: dict,
                      rationale: str = "", cites: list | None = None) -> None:
        self.candidates[cid] = {"parent": parent, "gen": self.gen,
                                "overrides": overrides, "rationale": rationale,
                                "cites": cites or []}
        self.journal("birth", cid=cid, parent=parent, rationale=rationale[:200])

    def record_one(self, cid: str, r: dict, is_val: bool,
                   basis_of: "dict[str, list] | None" = None) -> float:
        """记一条 runner 记录,返回它的花费。口径(对审计):
        infra_error(断网/429 类环境故障)→ 分数 None,一切统计剔除 —— 机器的锅;
        crash(判分器/代码异常)→ 按 0 分计入 —— 崩溃是失败,不许靠崩溃退出比较。"""
        from evals.briefing import task_feedback
        cost = (r.get("cost") or {}).get("cost_usd", 0.0) or 0.0
        score = (None if r.get("status") == "infra_error"
                 else _task_score(r, (basis_of or {}).get(r["id"])))
        self.scores_all.setdefault(cid, {})[r["id"]] = score
        if is_val:
            self.matrix.setdefault(cid, {})[r["id"]] = score
            self.passed.setdefault(cid, {})[r["id"]] = bool(r.get("passed"))
        if score is not None and score < 1.0:
            self.meds.setdefault(cid, {})[r["id"]] = task_feedback(r)
        self.spent_usd = round(self.spent_usd + cost, 4)
        return cost

    def record_eval(self, cid: str, records: list[dict], is_val: bool,
                    basis_of: "dict[str, list] | None" = None) -> float:
        """批量版(测试/简单路径用);evolve 走 record_one 逐题记账+熔断。"""
        cost = sum(self.record_one(cid, r, is_val, basis_of) for r in records)
        if is_val:
            self.peeks += 1
        return cost

    def val_mean(self, cid: str) -> "float | None":
        row = [v for v in self.matrix.get(cid, {}).values() if v is not None]
        return round(sum(row) / len(row), 4) if row else None

    def lineage(self, cid: str) -> list[str]:
        chain = [cid]
        while self.candidates.get(chain[-1], {}).get("parent"):
            chain.append(self.candidates[chain[-1]]["parent"])
        return list(reversed(chain))


def _task_score(r: dict, basis: "list | None" = None) -> float:
    """一道题的连续分 = 它自己声明的尺子(reward_basis)的平均分。
    与 case_pass(全达标才算过)是同一批尺子的两种口径:矩阵要梯度,必过门要硬杠。
    没传 basis 时退回"所有已算尺子的平均"(仅测试路径)。"""
    basis = basis or list(r.get("scores", {}).keys())
    vals = [r.get("scores", {}).get(b, 0.0) for b in basis] or [0.0]
    return round(sum(vals) / len(vals), 4)


def new_run_id() -> str:
    return time.strftime("g%m%d-%H%M%S")
