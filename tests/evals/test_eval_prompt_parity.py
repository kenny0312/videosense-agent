"""评测的 prompt 不许比生产少一节 —— 这个坑已经长过两次了。

第一次(GD-0):生产每请求注入 `runtime_facts`(模型档/语言指令),eval 传 None,
于是语言指令那一段在 eval 里成了死代码 —— 考的不是同一个系统。修完在
`evals/world.py` 留了注释。

第二次(C1):生产开始注入 `library_state`(库存快照),eval 又没跟上。
**同一个形状、同一个位置、同一份注释底下**。

所以这次不靠"记得手动对齐"。`_loop_system()` 的每一个注入段参数,
两个 eval 入口都必须显式传 —— 下次再加一节而 eval 没跟上,这里立刻红。

为什么用静态源码检查而不是跑一遍比对:eval 的两个入口要真库/真模型/假世界安装,
在单测里跑不起来;而"有没有把参数传下去"是纯语法事实,读源码就够,且零依赖。
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVAL_CALLERS = ("evals/world.py", "evals/session.py")


def _injection_params() -> list[str]:
    """`_loop_system()` 里【每一个会往 prompt 里加一段】的参数。

    判据取"有默认值的可选参":schema / replay_context 是位置必填,其余
    (runtime_facts / task_notice / library_state / user_memory)每一个都对应
    prompt 里的一节 —— 少传一个就少一节。
    """
    from pipeline import loop_driver

    sig = inspect.signature(loop_driver._loop_system)
    return [n for n, p in sig.parameters.items()
            if p.default is not inspect.Parameter.empty]


def _kwargs_passed_at(path: str) -> set[str]:
    """源码里所有 `_loop_system(...)` 调用【显式传了】哪些关键字参数。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    got: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "_loop_system":
            continue
        got |= {kw.arg for kw in node.keywords if kw.arg}
        # 位置参数也算数:_loop_system(schema, None, rt) 里的 rt 就是 runtime_facts
        from pipeline import loop_driver
        names = list(inspect.signature(loop_driver._loop_system).parameters)
        for i, _ in enumerate(node.args):
            if i < len(names):
                got.add(names[i])
    return got


@pytest.mark.parametrize("path", EVAL_CALLERS)
def test_eval_passes_every_prompt_section(path):
    want = set(_injection_params())
    got = _kwargs_passed_at(path)
    assert got, f"{path} 里没找到 _loop_system(...) 调用 —— 这条测试的前提要重看"
    missing = want - got
    assert not missing, (
        f"{path} 调 _loop_system 时漏了这些注入段:{sorted(missing)}。\n"
        "评测的 prompt 会比生产少这几节 —— 那就不是在量同一个系统了。\n"
        "(这个坑长过两次:GD-0 的 runtime_facts、C1 的 library_state,"
        "同一个位置同一份注释底下。所以现在有这条测试。)")


def test_the_check_actually_has_something_to_check():
    """反向锁:注入段列表不能是空的,否则上面那条恒真、等于没测。"""
    want = _injection_params()
    assert len(want) >= 4, f"注入段只识别出 {want} —— 判据可能失效了,上面那条就是空转的"
    assert "library_state" in want and "runtime_facts" in want
