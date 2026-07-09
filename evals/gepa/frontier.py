"""Pareto 前沿:分数矩阵 → 谁称王几题 → 加权抽父本(§4 ①)。

为什么不选平均分最高的独苗:论文消融明确 —— 独苗早熟收敛;
在至少一道题上并列最优的候选都保留繁殖权,按称王题数加权,
偏科生手里的独门解法基因才传得下去。
"""
from __future__ import annotations

EPS = 1e-9


def pareto_wins(matrix: dict) -> dict:
    """{cid: 称王题数}。称王 = 在该题上与全场最高分并列(浮点容差)。
    None(环境故障)不参与该题比较。之后剔除被完全支配的候选
    (每道共有题都 ≤ 某人且总分 < 那人 → 出局)。"""
    tasks: set = set()
    for row in matrix.values():
        tasks |= set(row)
    wins = {cid: 0 for cid in matrix}
    for t in tasks:
        best, kings = None, []
        for cid, row in matrix.items():
            v = row.get(t)
            if v is None:
                continue
            if best is None or v > best + EPS:
                best, kings = v, [cid]
            elif abs(v - best) <= EPS:
                kings.append(cid)
        for cid in kings:
            wins[cid] += 1
    front = {cid: w for cid, w in wins.items() if w > 0}
    for cid in list(front):
        if _dominated(cid, front, matrix):
            front.pop(cid)
    return front


def _dominated(cid: str, front: dict, matrix: dict) -> bool:
    mine = matrix[cid]
    for other in front:
        if other == cid:
            continue
        theirs = matrix[other]
        common = [t for t in mine if t in theirs
                  and mine[t] is not None and theirs[t] is not None]
        if not common:
            continue
        if (all(theirs[t] >= mine[t] - EPS for t in common)
                and sum(theirs[t] for t in common) > sum(mine[t] for t in common) + EPS):
            return True
    return False


def sample_parent(front: dict, rng) -> str:
    """按称王题数加权抽样(rng 由 evolve 用 run_id 播种,可复现)。"""
    cids = sorted(front)
    weights = [front[c] for c in cids]
    total = sum(weights)
    x = rng.random() * total
    acc = 0.0
    for c, w in zip(cids, weights):
        acc += w
        if x <= acc:
            return c
    return cids[-1]
