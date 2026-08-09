"""C6 棘轮:系统提示与工具声明的字符预算,只许【主动评审后】增长。

## 为什么这个值得一条测试

prompt 不是一次性成本 —— 它随【每一步】重发。一个 16 步的请求要付 16 次;
开了子 agent 就是 (1 + K) 份。所以往声明里多写 200 字,不是"多 200 字",
是"每次请求多 3200+ 字"。而写文档的人拿不到这个反馈:加一句话很轻,账单在别处。

棘轮的作用不是禁止增长,是让增长【必须经过一次显式的抬闸】—— 改上限的那一行
diff 会出现在 review 里,而"顺手多写两句"不会。

## 这些数字从哪来

上限取自任务书 v1.1 §8-C6。实测当时的真实占用(2026-08-02):
    _LOOP_SYSTEM        4850 / 5350
    13 个工具描述合计    5305 / 5700
    最大单工具 spawn     1181 / 1300
也就是各留了约 10% 余量。**余量是给"把话说清楚"用的,不是给"再塞一个功能"用的** ——
本仓刚吃过反面教训:spawn_agents 的描述曾经 922 字全是刹车、正面引导 110 字,
拆分率恒 0;重写成"方向盘"之后拆分率 0→67%。那次是把字用对了,不是用多了。
"""
from __future__ import annotations

import os

import pytest

# 上限。改这里 = 主动评审后的决定;别为了让测试变绿而调大它。
LOOP_SYSTEM_MAX = 5350
ALL_TOOL_DESC_MAX = 5700
SINGLE_TOOL_DESC_MAX = 1300


def _decls():
    """按【全部工具都开着】取声明 —— 棘轮要量最坏情况,不是当前开关下的子集。"""
    os.environ["USE_SUBAGENTS"] = "1"
    from pipeline.node_specs import build_function_declarations
    return build_function_declarations()


def test_loop_system_within_budget():
    from pipeline import loop_driver as ld

    n = len(ld._LOOP_SYSTEM)
    assert n <= LOOP_SYSTEM_MAX, (
        f"_LOOP_SYSTEM 涨到 {n} 字符,超了 {LOOP_SYSTEM_MAX} 的预算。"
        "它随每一步重发 —— 16 步的请求要付 16 次。确实需要就抬 LOOP_SYSTEM_MAX,"
        "但那一行 diff 要能在 review 里被看见。")


def test_all_tool_descriptions_within_budget():
    total = sum(len(d.get("description") or "") for d in _decls())
    assert total <= ALL_TOOL_DESC_MAX, (
        f"工具描述合计 {total} 字符,超了 {ALL_TOOL_DESC_MAX}。"
        "所有声明【每一步】都全量重发,加一个工具是给每次请求都加钱。")


def test_no_single_tool_description_hogs_the_budget():
    """单工具上限单独存在:总预算够时,一个工具也不该吃掉一半。

    描述太长不只是钱的问题 —— 大脑要在十几个声明里挑一个,某一条特别长会挤掉
    其它工具被读到的机会(实测过反面:spawn 的描述堆满刹车条款,结果它一次都没被用)。
    """
    over = [(d["name"], len(d.get("description") or "")) for d in _decls()
            if len(d.get("description") or "") > SINGLE_TOOL_DESC_MAX]
    assert not over, (
        f"这些工具的描述超了单条 {SINGLE_TOOL_DESC_MAX} 字符的预算:{over}。"
        "先想想能不能删 —— 判据写不清楚往往是因为判据本身没想清楚。")


def test_budget_has_actual_headroom_not_just_a_ceiling():
    """反向锁:上限不能被偷偷抬到远高于实际占用。

    一个永远够用的上限等于没有上限。这条要求余量在合理区间(实际占用 ≥ 上限的 60%),
    上限抬得太高时它会红 —— 提醒抬闸的人顺手把它调回贴近实际的水平。
    """
    from pipeline import loop_driver as ld

    lens = [len(d.get("description") or "") for d in _decls()]
    checks = [
        ("_LOOP_SYSTEM", len(ld._LOOP_SYSTEM), LOOP_SYSTEM_MAX),
        ("工具描述合计", sum(lens), ALL_TOOL_DESC_MAX),
        # 单工具那条也要查 —— 少了它,把 SINGLE_TOOL_DESC_MAX 抬到 3000
        # 全套件照样绿(实测:变异存活)。三个上限一个都不能漏。
        ("最长的单个工具描述", max(lens), SINGLE_TOOL_DESC_MAX),
    ]
    for name, actual, cap in checks:
        assert actual >= cap * 0.6, (
            f"{name} 实际 {actual}、上限 {cap} —— 余量 {1 - actual / cap:.0%},上限形同虚设。"
            "把上限调到贴近实际(留 10~15% 就够),棘轮才有意义。")


@pytest.mark.parametrize("field", ["description", "parameters"])
def test_every_declared_tool_has_the_basics(field):
    """顺带钉住声明的完整性 —— 少了描述的工具等于没有判据,模型只能靠名字猜。"""
    missing = [d["name"] for d in _decls() if not d.get(field)]
    assert not missing, f"这些工具缺 {field}:{missing}"
