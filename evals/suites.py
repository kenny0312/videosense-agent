"""套件分层：把题分成【回归套件】和【能力套件】，别在简单题上浪费钱。

- 能力套件：当前 agent 会挂或时好时坏的题——这才是"agent 到底行不行"的信号，认真跑（n=3/5）。
- 回归套件：当前 agent 闭眼全过的题——它们没区分度了，但留着当【防退步安全网】
  （以后改 prompt 改坏了、模型降级了，靠它们报警）。便宜跑（n=1），只在大改动前跑。

怎么分：不把 suite 硬编进每道题（饱和度会变，那样很脆）。而是从最近一次真跑的成绩
自动算——闭眼全过(successes==n)的进回归，其余进能力——存进 evals/suites.json。
题库或 agent 变了，重跑一次 `python -m evals.tag_suite` 重新分层即可。

没有 suites.json 时（全新环境/还没跑过基线），默认全部当能力套件——宁可全跑，不漏测。
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
SUITES_PATH = os.path.join(_HERE, "suites.json")


def load_regression_ids() -> set:
    """回归套件的题 id 集合。文件不存在则空集（=全部当能力套件）。"""
    if not os.path.exists(SUITES_PATH):
        return set()
    try:
        return set(json.load(open(SUITES_PATH, encoding="utf-8")).get("regression", []))
    except (OSError, ValueError):
        return set()


def suite_of(task_id: str, regression_ids: set | None = None) -> str:
    reg = load_regression_ids() if regression_ids is None else regression_ids
    return "regression" if task_id in reg else "capability"


def filter_by_suite(tasks: list, which: str) -> list:
    """which = all / regression / capability。"""
    if which == "all":
        return tasks
    reg = load_regression_ids()
    if which == "regression":
        return [t for t in tasks if t.get("id") in reg]
    return [t for t in tasks if t.get("id") not in reg]      # capability
