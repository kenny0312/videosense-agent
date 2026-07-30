"""step4 并发测试用的小工人:被 subprocess 拉起,对同一个账本狂记账。

用法: python _budget_worker.py <state_dir> <run_id> <次数>
三道闸全部调到天上去 —— 这个工人只负责制造并发写,不该被闸门打断。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dvd_repro import budget, config           # noqa: E402

config.GUARD_SINGLE_CALL_USD = 1e9
config.GUARD_RUN_USD = 1e9
config.GUARD_TOTAL_USD = 1e9

state_dir, run_id, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
g = budget.BudgetGuard(run_id, state_dir=state_dir)
for _ in range(n):
    g.charge(0.01, note="并发压测")
