"""三道闸:minibatch 准入 / sign-test 显著性 / 预算台账(§4.5)。"""
from __future__ import annotations

import math

EPS = 1e-9


def sign_test(child: dict, parent: dict) -> dict:
    """按题配对比较两行分数(只比双方都有分的题;平局丢弃)。
    p = 单边二项检验:若两者其实一样好(每题赢率 0.5),赢出这么多题纯靠运气的概率。
    p < 0.05 才允许下"真变好了"的结论(§4.5 纪律1)。"""
    wins = losses = 0
    for t, cv in child.items():
        pv = parent.get(t)
        if cv is None or pv is None:
            continue
        if cv > pv + EPS:
            wins += 1
        elif cv < pv - EPS:
            losses += 1
    n = wins + losses
    p = (sum(math.comb(n, k) for k in range(wins, n + 1)) / 2 ** n) if n else 1.0
    return {"wins": wins, "losses": losses, "n": n, "p": round(p, 5),
            "significant": n > 0 and p < 0.05}


def minibatch_pass(child_scores: dict, parent_scores: dict, margin: float = 0.25) -> bool:
    """准入小考(省钱闸):同一批题上,子代总分必须高出父本 margin 以上。
    margin 治均值回归假阳(审计 m6):小考题偏选父本的低分题,零效应子代重掷
    也倾向反弹,"严格大于"闸形同抛硬币 —— 要求平均每题 +margin/k 的真起色。"""
    common = [t for t in child_scores
              if t in parent_scores
              and child_scores[t] is not None and parent_scores[t] is not None]
    if not common:
        return False
    return (sum(child_scores[t] for t in common)
            > sum(parent_scores[t] for t in common) + margin)


class Ledger:
    """预算台账:进化阶段只准花到 budget - reserve,预留的 reserve 保证
    赢家重考 + 终门一定跑得起(§4.5:纪律花在防假提升上,不是多跑几代)。
    unit() 给出实测单题成本(样本不足时用保守估计),预检批次用。"""

    FALLBACK_UNIT = 0.022

    def __init__(self, budget_usd: float, reserve_usd: float):
        self.budget = budget_usd
        self.reserve = reserve_usd
        self.spent = 0.0            # 总账(含 --resume 继承的旧账)
        self.spent_local = 0.0      # 本进程增量(单价分子;继承旧账会虚高 3-7 倍,两轮实跑都栽过)
        self.rollouts = 0           # 本进程 rollout 数(单价分母)

    def add(self, cost: float, rollouts: int = 0) -> None:
        self.spent = round(self.spent + (cost or 0.0), 4)
        self.spent_local = round(self.spent_local + (cost or 0.0), 4)
        self.rollouts += rollouts

    def unit(self) -> float:
        if self.rollouts >= 50:
            return max(self.spent_local / self.rollouts, 0.005)   # 进程内实测均价
        return self.FALLBACK_UNIT

    def evolution_open(self) -> bool:
        return self.spent < self.budget - self.reserve

    def exhausted(self) -> bool:
        return self.spent >= self.budget
