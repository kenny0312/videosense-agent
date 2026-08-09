"""B0-1:没有沙箱时,python/plot 不许摆在大脑面前,也不许打空对象崩掉整次请求。

实证(任务书 §4-B0-1):三份 gate jsonl 共 12 条
`AttributeError("'NoneType' object has no attribute 'execute'")`。

定性很重要,别记成"生产 bug":全仓只有两处传 sandbox=None —— evals 的跑机,
以及 task_runner(它走 run_fanout,被子 agent 的只读白名单结构性挡住)。
生产 orchestrator 永远传真沙箱。所以这是**评测阻断**,真实后果是
「这一次请求里所有已经花钱买到的证据被整体丢弃」。

两道防线各测各的:
  ① 声明过滤 —— 工具压根不出现在大脑的可选项里(治本);
  ② 执行入口软失败 —— 直连/回放/旧 trace 重放绕过声明层时的兜底(治漏)。
"""
from __future__ import annotations

import pytest

from pipeline import loop_driver as ld
from pipeline import node_executor as ne
from pipeline.dag_schema import Node


def _names(**kw):
    return [d["name"] for d in ld.loop_function_declarations(**kw)]


# ── 防线①:声明过滤 ──────────────────────────────────────────────
def test_sandbox_none_hides_python_and_plot():
    got = _names(sandbox=None)
    assert "python" not in got and "plot" not in got, "没有沙箱却把必崩的工具摆给了大脑"
    # 其余工具一个都不许被误伤
    assert "sql_query" in got and "analyze_video" in got and "show_video" in got


def test_real_sandbox_keeps_them():
    got = _names(sandbox=object())          # 任何非 None 都算"有沙箱"
    assert "python" in got and "plot" in got


def test_unspecified_is_byte_identical_to_before():
    """调用方【没说】≠ 明确说 None。

    这条是本次改动的向后兼容闸:subagents / evals.session / evals.world 都是
    `loop_function_declarations()` 裸调,它们的行为必须一个字节都不变。
    """
    assert _names() == _names(sandbox=object()), "不传 sandbox 时行为变了 —— 破坏了向后兼容"
    assert "python" in _names()


# ── 防线②:执行入口软失败 ────────────────────────────────────────
@pytest.mark.parametrize("tool", ["python", "plot"])
def test_sandbox_node_soft_fails_instead_of_crashing(tool):
    """绕过声明层直接调 → 必须是 ok=False 的软失败,不是 AttributeError。

    为什么不能让它抛:异常会一路掀掉整个 run_loop,而 ledger 里那些已经付过费的
    analyze/检索结果会跟着一起没。软失败则走既有错误回灌,大脑换条路继续做。
    """
    node = Node(id="c0_0", tool=tool, inputs={"instruction": "画个图"}, depends_on=[])
    res = ne._run_sandbox_node(node, {}, None, _Trace())
    assert res.ok is False
    assert tool in (res.stderr or "")
    assert "沙箱" in (res.stderr or ""), "错误文案没说清是环境缺失,大脑会以为是自己写错了"
    # 文案要给出【可执行的下一步】,不然大脑只会原样重试
    assert any(t in (res.stderr or "") for t in ("sql_query", "semantic_search", "analyze_video"))


def test_soft_fail_does_not_touch_the_none_sandbox():
    """反向锁:兜底必须在【碰 sandbox 之前】返回。

    如果守卫放在生成代码之后,就会先白烧一次 CodeGenerator 的 LLM 调用再崩 ——
    钱花了、结果还是失败。用一个会爆炸的假 CodeGenerator 钉住这个顺序。
    """
    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("守卫太靠后:在确认没有沙箱之前就去生成代码了")

    orig = ne.CodeGenerator
    ne.CodeGenerator = _Boom
    try:
        node = Node(id="c0_0", tool="python", inputs={"instruction": "x"}, depends_on=[])
        assert ne._run_sandbox_node(node, {}, None, _Trace()).ok is False
    finally:
        ne.CodeGenerator = orig


class _Trace:
    """最小 trace 替身:只要 step() 能用就行,不引 agentops 的真实现。"""

    def step(self, *a, **k):
        class _S:
            def ok(self, **k):
                pass

            def fail(self, **k):
                pass

            def soft(self, *a, **k):
                pass
        return _S()


def test_filter_asks_node_specs_not_a_local_list():
    """名单必须来自 node_specs.needs_sandbox 这一个源。

    在 loop_driver 里另列一份 ("python","plot") 也能让上面的用例全绿,但那样
    【新增一个沙箱工具】时过滤会漏掉它,而漏掉的后果正是本文件要治的那次崩溃。
    这条把"问单一事实源"钉住:临时把 needs_sandbox 改成"只有 sql_query 要沙箱",
    过滤结果必须跟着变 —— 本地硬编码名单做不到这一点。
    """
    from pipeline import node_specs as ns

    orig = ld.needs_sandbox
    ld.needs_sandbox = lambda t: t == "sql_query"      # 换一个"事实源"的答案
    try:
        got = _names(sandbox=None)
        assert "sql_query" not in got, (
            "loop_driver 没有走 node_specs.needs_sandbox —— 大概率本地另列了一份名单")
        assert "python" in got and "plot" in got, "同上:过滤没有跟着事实源走"
    finally:
        ld.needs_sandbox = orig
    assert ld.needs_sandbox is ns.needs_sandbox, "还原失败"
