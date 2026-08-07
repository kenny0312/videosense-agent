"""评测调生产函数时不许比生产少传参 —— 这个坑长过【七次】,而前三次的补丁只堵住了一个函数。

第一次(GD-0):生产每请求注入 `runtime_facts`(模型档/语言指令),eval 传 None,
于是语言指令那一段在 eval 里成了死代码 —— 考的不是同一个系统。修完留了注释。

第二次(C1):生产开始注入 `library_state`(库存快照),eval 又没跟上。
**同一个形状、同一个位置、同一份注释底下**。于是有了本文件的第一版:
锁住 `_loop_system()` 的每一个注入段参数。

然后第三到第七次从【隔壁那个函数】长出来了,而这条测试一直是绿的:

  · `run_loop(guard=)`        没传 → per-tree 成本闸的挂点②在评测里是关的
  · `run_loop(critic=)`       没传 → "开自检 vs 不开自检"的 A/B 两臂结果逐字节相同
  · `run_loop(req_short=)`    没传 → 多轮每轮都从 c0_0 起号,合并台账时前面几轮被覆盖
  · `runtime_facts_line(has_image=)` 没传 → 图送进去了,但"你能看到它"那 164 字没进 prompt
  · `_make_executor(...)`     多轮建在循环外 → 配额与预算跨轮累积,生产是每请求重置

判据只锁一个函数,漏的就是其余每一个函数。所以现在改成【逐入口】:
对每一个生产与评测都会调的函数,断言 **评测传的 kwarg 集 ⊇ 生产传的 kwarg 集**。
生产哪天开始传一个新参而评测没跟上,不管发生在哪个入口,这里都立刻红 ——
不再是"每加一个入口就得记得再写一条测试"。

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


# 函数名 → 它定义在哪个模块(位置实参折算要查签名)。加新入口先在这里登记。
_FN_MODULE = {
    "_loop_system": "pipeline.loop_driver",
    "run_loop": "pipeline.loop_driver",
    "_make_executor": "pipeline.loop_driver",
    "runtime_facts_line": "pipeline.loop_driver",
    "build_loop_context": "pipeline.loop_memory",
    "record_loop_turn": "pipeline.loop_memory",
}


def _param_names(fn_name: str) -> list[str]:
    """被调函数的形参名(用来把位置实参也折算成参数名)。"""
    import importlib

    mod = importlib.import_module(_FN_MODULE[fn_name])
    return list(inspect.signature(getattr(mod, fn_name)).parameters)


def _kwargs_passed_at(path: str, fn_name: str = "_loop_system") -> set[str]:
    """源码里所有 `fn_name(...)` 调用一共【传到了】哪些参数(含折算过的位置实参)。

    取并集而不是逐个调用点分别看:同一个文件里可能有多条路径(单轮/多轮),
    只要有一条传了就算这个文件知道该传 —— 判据宁松不紧,避免这条测试自己变成噪音源。
    """
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    got: set[str] = set()
    names = _param_names(fn_name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if called != fn_name:
            continue
        got |= {kw.arg for kw in node.keywords if kw.arg}
        for i, _ in enumerate(node.args):        # 位置实参:_loop_system(schema, None, rt) 的 rt
            if i < len(names):
                got.add(names[i])
    return got


# ── 逐入口的 ⊇ 规则 ────────────────────────────────────────────────────
# 生产在哪个文件里调它(评测要对齐的就是那一处的传参)
PROD_SITE = {
    "_loop_system": "pipeline/loop_driver.py",        # run_query_loop 里
    "run_loop": "pipeline/loop_driver.py",            # 同上
    "_make_executor": "pipeline/loop_driver.py",      # 同上
    "runtime_facts_line": "pipeline/orchestrator.py",
    # T1 修复(多轮记忆走生产同款回放)后,多轮车道也调这两个 —— 一并纳入 ⊇ 规则。
    # 只对多轮车道生效(单轮一题一请求,没有跨轮记忆可回放),见 _SESSION_ONLY。
    "build_loop_context": "pipeline/orchestrator.py",
    "record_loop_turn": "pipeline/orchestrator.py",
}

# 只有多轮车道才该调的入口(单轮车道没有跨轮记忆,不调不算漂移)。
_SESSION_ONLY = {"build_loop_context", "record_loop_turn"}

# 允许评测不传的参 —— 每一条都要写清【为什么它不影响"考的是不是同一个系统"】。
# 加一条就是一次明知故犯,不是随手放行。
EXEMPT = {
    ("run_loop", "on_step"):
        "SSE 每步事件回调,只影响把进度推给前端这条传输链,不改变模型看到什么、也不约束它。"
        "评测没有前端可推,传了也是个空回调。",
}


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


@pytest.mark.parametrize("fn_name,prod_path", sorted(PROD_SITE.items()))
@pytest.mark.parametrize("eval_path", EVAL_CALLERS)
def test_eval_passes_at_least_what_production_passes(fn_name, prod_path, eval_path):
    """核心规则:评测传的 kwarg 集 ⊇ 生产传的 kwarg 集,逐入口。

    这条替代了"每加一个入口就再写一条测试"。它盯的不是某个具体参数,
    而是【生产开始传而评测没跟上】这个形状本身 —— 那正是长了七次的那个坑。
    """
    if fn_name in _SESSION_ONLY and eval_path != "evals/session.py":
        pytest.skip("单轮车道没有跨轮记忆,不调回放入口不算漂移")
    prod = _kwargs_passed_at(prod_path, fn_name)
    assert prod, (f"{prod_path} 里没找到 {fn_name}(...) 调用 —— "
                  "生产侧的落点挪了,PROD_SITE 要跟着改,否则这条是空转的")
    got = _kwargs_passed_at(eval_path, fn_name)
    assert got, (f"{eval_path} 里没找到 {fn_name}(...) 调用 —— "
                 "评测这条车道压根没走生产入口?那比少传参更严重")
    missing = {p for p in (prod - got) if (fn_name, p) not in EXEMPT}
    assert not missing, (
        f"{eval_path} 调 {fn_name} 时,生产传了而它没传:{sorted(missing)}。\n"
        f"(生产的落点:{prod_path};生产传的全集:{sorted(prod)})\n"
        "评测因此在量一个和生产不一样的系统。确实不该传的,写进本文件的 EXEMPT "
        "并说明【为什么它不影响考的是不是同一个系统】—— 那是一次明知故犯,不是随手放行。")


def test_every_exemption_carries_a_reason():
    """例外清单是这条规则唯一的漏气口,所以它自己也要被钉住:
    每条例外必须写明理由,且理由不能是敷衍的一句话。"""
    for key, why in EXEMPT.items():
        assert isinstance(why, str) and len(why) >= 15, (
            f"例外 {key} 没写清楚理由 —— 例外清单一松,整条规则就白建了")


def test_the_superset_rule_would_actually_catch_a_regression():
    """反向锁:确认这条规则不是恒真的。

    人为构造"生产传了 zzz_new_param 而评测没传"的情形,规则必须判它缺失。
    没有这条的话,PROD_SITE 写错文件名之类的失误会让上面那组测试静静全绿。
    """
    prod = {"guard", "critic", "zzz_new_param"}
    got = {"guard", "critic"}
    missing = {p for p in (prod - got) if ("run_loop", p) not in EXEMPT}
    assert missing == {"zzz_new_param"}, "⊇ 规则的算法本身失效了 —— 上面几条全是空转的"
