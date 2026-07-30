"""P0-1 账本修复:思考 token 入账 + 价目表外模型 fail-loud。

长程引擎的一切美元闸门都读 summarize()["cost_usd"] —— 这层记漏一分,
熔断就对应地失明一分。两个病(思考 token 不计价、表外模型静默记 $0)在
DVD 复现里各自造成过一次"闸门形同虚设",故用单测钉死。离线、零 API。
"""
import pytest

from pipeline.agentops import usage


class _UM:
    """假 usage_metadata。thoughts 可省(模拟不吐该字段的 SDK 版本)。"""
    def __init__(self, pin, out, total, cached=0, thoughts=None):
        self.prompt_token_count = pin
        self.candidates_token_count = out
        self.total_token_count = total
        self.cached_content_token_count = cached
        if thoughts is not None:
            self.thoughts_token_count = thoughts


class _Resp:
    def __init__(self, um):
        self.usage_metadata = um


@pytest.fixture(autouse=True)
def _fresh():
    usage.reset_usage()
    yield
    usage.reset_usage()


def test_thinking_tokens_billed_at_output_price_explicit_field():
    """SDK 给了 thoughts_token_count → 按 out 价计入。"""
    usage.add_usage(_Resp(_UM(1000, 200, 1700, thoughts=500)), "gemini-3.5-flash")
    s = usage.summarize()
    assert s["tokens_thought"] == 500
    p = usage._PRICE["gemini-3.5-flash"]
    expect = 1000 / 1e6 * p["in"] + (200 + 500) / 1e6 * p["out"]
    assert s["cost_usd"] == pytest.approx(round(expect, 6))


def test_thinking_tokens_derived_when_field_missing():
    """SDK 不吐 thoughts 字段 → 从 total−in−out 推(total 是权威口径)。"""
    usage.add_usage(_Resp(_UM(1000, 200, 1700)), "gemini-3.5-flash")   # 无 thoughts
    s = usage.summarize()
    assert s["tokens_thought"] == 500          # 1700-1000-200
    p = usage._PRICE["gemini-3.5-flash"]
    assert s["cost_usd"] == pytest.approx(
        round(1000 / 1e6 * p["in"] + 700 / 1e6 * p["out"], 6))


def test_thinking_never_undercounted_takes_max_of_two_sources():
    """两来源不一致时取较大者 —— 少记 = 闸门失明,是这条修复要治的病。"""
    usage.add_usage(_Resp(_UM(100, 10, 900, thoughts=5)), "gemini-2.5-flash")
    assert usage.summarize()["tokens_thought"] == 790     # max(5, 900-100-10)


def test_no_thinking_means_no_extra_charge():
    """total == in+out(无思考)→ 与升级前计价一致,不虚增。"""
    usage.add_usage(_Resp(_UM(1000, 200, 1200)), "gemini-2.5-flash")
    p = usage._PRICE["gemini-2.5-flash"]
    s = usage.summarize()
    assert s["tokens_thought"] == 0
    assert s["cost_usd"] == pytest.approx(
        round(1000 / 1e6 * p["in"] + 200 / 1e6 * p["out"], 6))


def test_unpriced_model_uses_most_expensive_price_and_is_reported():
    """表外模型:不再静默记 $0,按表内最贵单价估价 + 报名字(红队 B2)。"""
    usage.add_usage(_Resp(_UM(1_000_000, 0, 1_000_000)), "some-new-model-v9")
    s = usage.summarize()
    assert s["unpriced_models"] == ["some-new-model-v9"]
    assert s["cost_usd"] == pytest.approx(usage._MAX_PRICE["in"])   # 1M in × 最贵 in 价
    assert s["cost_usd"] > 0                                        # 绝不为 0


def test_priced_model_reports_no_unpriced():
    usage.add_usage(_Resp(_UM(100, 10, 110)), "gemini-2.5-flash")
    assert usage.summarize()["unpriced_models"] == []


def test_models_prefix_is_normalized_not_treated_as_unknown():
    """"models/gemini-2.5-flash" 是同一个模型,不该被当表外 —— 只剥前缀,不猜别名。"""
    usage.add_usage(_Resp(_UM(1000, 100, 1100)), "models/gemini-2.5-flash")
    s = usage.summarize()
    assert s["unpriced_models"] == []
    assert "gemini-2.5-flash" in s["by_model"]


def test_cached_discount_still_applies_with_thinking():
    """缓存折扣与思考计价互不干扰(cached ⊂ in)。"""
    usage.add_usage(_Resp(_UM(1000, 100, 1300, cached=800, thoughts=200)), "gemini-3.5-flash")
    p = usage._PRICE["gemini-3.5-flash"]
    expect = (200 / 1e6 * p["in"] + 800 / 1e6 * p["cached"] + 300 / 1e6 * p["out"])
    assert usage.summarize()["cost_usd"] == pytest.approx(round(expect, 6))


def test_dirty_cached_larger_than_in_does_not_go_negative():
    usage.add_usage(_Resp(_UM(100, 10, 110, cached=99999)), "gemini-2.5-flash")
    assert usage.summarize()["cost_usd"] > 0


def test_add_usage_without_reset_is_silent_noop():
    """未 reset(单测直连场景)→ 静默跳过,不抛(fail-open 家规)。"""
    usage._USAGE.set(None)
    usage.add_usage(_Resp(_UM(1, 1, 2)), "gemini-2.5-flash")   # 不抛即通过
    assert usage.get_usage() == {}


# ── 审查修正后的关键口径 ──
def test_tool_use_prompt_not_billed_as_thinking():
    """审查 HIGH:grounding 把搜索结果回灌的 token 落在 tool_use_prompt 上,含在 total、
    不含在 prompt。不单独扣掉它,derived 会把这批【输入】按 out 价计 → 成本虚高 ~5.5×。"""
    um = _UM(1000, 200, 6200)           # total 多出 5000
    um.tool_use_prompt_token_count = 5000
    usage.add_usage(_Resp(um), "gemini-2.5-flash")
    s = usage.summarize()
    assert s["tokens_thought"] == 0      # 5000 全归 tool,不是思考
    assert s["tokens_tool"] == 5000
    p = usage._PRICE["gemini-2.5-flash"]
    expect = (1000 + 5000) / 1e6 * p["in"] + 200 / 1e6 * p["out"]   # 工具提示按 in 价
    assert s["cost_usd"] == pytest.approx(round(expect, 6))


def test_thinking_computed_per_call_not_on_sums():
    """审查 HIGH:在累加和上取 max 是错的 —— 某次 total 缺失(=0)会把它的 in+out
    从别人的 total 里减掉,已推出的思考量凭空消失,累计成本甚至能随消耗下降。"""
    usage.add_usage(_Resp(_UM(1000, 100, 1600)), "gemini-2.5-flash")   # 思考 500
    after_first = usage.summarize()
    usage.add_usage(_Resp(_UM(800, 50, 0)), "gemini-2.5-flash")        # total 缺失(通道漏吐)
    after_second = usage.summarize()
    assert after_second["tokens_thought"] == 500        # 第一次的 500 必须还在
    assert after_second["cost_usd"] >= after_first["cost_usd"]   # 成本单调不减
