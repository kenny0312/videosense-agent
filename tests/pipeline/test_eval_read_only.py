"""B0-2a:评测跑不许写生产库。

实测教训(不是假想):gate 实验的 1256 行 analyze 产物永久留在生产
`content_embeddings` 里,占那张表的 **18.6%**,而生产总共只有 514 条视频。
用户 semantic_search 会命中评测垃圾 —— 其中还有"No, there is no one climbing a
rock wall"这种否定结论,作为"证据"命中一条内容完全无关的视频。

三个写入口各有各的正确处置,不是一刀切:
  · `semantic_index.index_entry`  —— 最底层,守卫在自己的 fail-open try 之【外】
  · `_run_update_memory`          —— 用户显式要求的工具调用,抛到工具层是对的
  · `_index_analyze_result`       —— 旁路,专门接住并计数(别把已花钱的结果弄丢)
"""
from __future__ import annotations

import pytest

from pipeline import config
from pipeline import eval_write_guard as G


@pytest.fixture(autouse=True)
def _clean():
    G.reset_blocked()
    yield
    G.reset_blocked()


def test_off_by_default_is_a_noop():
    """默认关 —— 生产路径逐字节不变,一个异常都不许抛。"""
    assert config.EVAL_READ_ONLY is False
    G.assert_writes_allowed("whatever")          # 不抛
    assert G.blocked_count() == 0


def test_on_raises_a_recognisable_type(monkeypatch):
    """必须是专用类型,不是裸 RuntimeError —— 旁路调用点要能【只】接住它,
    而不是顺手把真实的写库故障也一起吞掉(那就又回到静默失败)。"""
    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    with pytest.raises(G.EvalWriteBlocked) as ei:
        G.assert_writes_allowed("semantic_index.index_entry")
    assert "EVAL_READ_ONLY" in str(ei.value)
    assert G.blocked_count("semantic_index.index_entry") == 1


def test_counter_is_what_makes_it_distinguishable(monkeypatch):
    """任务书禁 fail-open 的理由是「『没写成』和『没触发』不可区分」。

    我们在旁路那条路上确实接住了异常(否则会弄丢已付费的 analyze 结果),
    可区分性就全靠这个计数器 —— 所以它本身要有测试。
    """
    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    for _ in range(3):
        with pytest.raises(G.EvalWriteBlocked):
            G.assert_writes_allowed("a")
    with pytest.raises(G.EvalWriteBlocked):
        G.assert_writes_allowed("b")
    assert G.blocked_count("a") == 3 and G.blocked_count("b") == 1
    assert G.blocked_count() == 4, "总数要能一把看出来(验收看的就是它 > 0)"


def test_index_entry_guard_sits_outside_its_own_failopen(monkeypatch):
    """`index_entry` 自己有 `except Exception: return False`。守卫必须在它【之外】——
    放进去就会被那句 fail-open 顺手吞掉,闸变摆设。"""
    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    from pipeline import semantic_index as si

    def _boom(*a, **k):
        raise AssertionError("守卫没挡住,已经去写库了")

    monkeypatch.setattr(si, "_execute", _boom)
    with pytest.raises(G.EvalWriteBlocked):
        si.index_entry("v1", "analyze", ("k", "snippet", None, None), "[0]")
    assert G.blocked_count("semantic_index.index_entry") == 1


def test_update_memory_fails_the_tool_loudly(monkeypatch):
    """用户显式要求写记忆 → 挡下时必须让工具失败,大脑才会如实告诉用户没写成。
    静默跳过会让用户以为记住了。

    前提是【没有豁免声明】—— EvalBackend.install() 会往进程环境写
    EVAL_READ_ONLY_ALLOW=update_memory(替身豁免),同套件里先跑过它的话
    这里就顺序相关;显式清掉,本测试测的是"无豁免时的默认拦截"。"""
    monkeypatch.delenv("EVAL_READ_ONLY_ALLOW", raising=False)
    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    monkeypatch.setattr(config, "USE_USER_MEMORY", True)
    from pipeline import node_executor as ne
    from pipeline.dag_schema import Node

    node = Node(id="c0_0", tool="update_memory",
                inputs={"text": "记住我喜欢滑雪", "mode": "append"}, depends_on=[])
    with pytest.raises(G.EvalWriteBlocked):
        ne._run_update_memory(node, "owner1")


def test_analyze_indexing_is_blocked_but_never_loses_the_paid_result(monkeypatch):
    """旁路那条:挡下写索引,但【绝不】把已经花钱买到的 analyze 结果一起弄丢。

    这是本文件最重要的一条 —— 任务书字面要求"抛而不是跳过",照字面写会让
    analyze 结果跟着异常一起没,那正是 A4 刚修掉的病(失败伪装 / 结果丢失)。
    """
    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    from pipeline import node_executor as ne
    # 环境解耦:embed 走真凭据,CI 上没有 → 在 embed 就断了,闸根本没被够到
    # (本地有 .env 一直是真跑,所以这两条只在 CI 红 —— "GCP_PROJECT 假失败")。
    # 桩掉向量这一环,analyze_snippet 与 index_entry 里的闸仍是真货。
    monkeypatch.setattr("pipeline.embeddings.embed_texts",
                        lambda texts, **kw: [[0.0] * 768 for _ in texts])

    # 不抛出去 = 调用方拿得到返回、analyze 结果不受影响
    ne._index_analyze_result("v1", {"answer": "看到有人滑雪", "enough": "yes"}, "av:v1:abc")
    assert G.blocked_count("semantic_index.index_entry") == 1, (
        "计数为 0 —— 要么闸没挡住,要么那条路压根没跑到,两者都不合格")


def test_production_path_untouched_when_flag_is_off(monkeypatch):
    """反向锁:开关关着时三处一个都不许拦。"""
    monkeypatch.setattr(config, "EVAL_READ_ONLY", False)
    from pipeline import semantic_index as si

    calls = {"n": 0}
    monkeypatch.setattr(si, "_execute", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    assert si.index_entry("v1", "analyze", ("k", "s", None, None), "[0]") is True
    assert calls["n"] == 1, "开关关着却没真去写库 —— 生产路径被改坏了"
    assert G.blocked_count() == 0


def test_a_real_write_failure_is_not_mislabelled_as_the_guard(monkeypatch, caplog):
    """守卫必须是【专用类型】,不能退化成裸 RuntimeError。

    退化之后,旁路那句 `except EvalWriteBlocked` 会连真实的写库故障一起接住,
    并打上"[EVAL_READ_ONLY] 拦下一次写入" —— 一条假日志:实际是数据库出问题了,
    值班却以为是闸在正常工作。静默失败换个马甲又回来了。
    """
    import logging

    from pipeline import embeddings as EMB
    from pipeline import node_executor as ne

    monkeypatch.setattr(config, "EVAL_READ_ONLY", False)      # 闸【关着】
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(ne, "log", logging.getLogger("test.ne"))

    # 注入点必须选在 `_index_analyze_result` 自己的 try 里、且在 index_entry 【之前】——
    # 从 si._execute 抛会被 index_entry 自己那句 fail-open 先吞掉,根本走不到旁路的
    # except,那样这条测试就是空转的(第一版正是这么写的,变异照样存活)。
    def _boom(*a, **k):
        raise RuntimeError("connection reset by peer")        # 真故障,不是闸
    monkeypatch.setattr(EMB, "embed_texts", _boom)

    with caplog.at_level(logging.WARNING, logger="test.ne"):
        ne._index_analyze_result("v1", {"answer": "x", "enough": "yes"}, "av:v1:k")

    text = caplog.text
    assert "EVAL_READ_ONLY" not in text, (
        "真实的写库故障被标成了闸拦截 —— 说明 EvalWriteBlocked 不是专用类型了")
    assert G.blocked_count() == 0, "真故障不该计进闸的账"


def test_guard_hit_is_logged_at_error_with_a_greppable_marker(monkeypatch, caplog):
    """旁路那条把异常接住了,所以【日志是唯一的现场痕迹】,它得够响、够好 grep。

    级别用 ERROR 而不是 WARNING:评测里出现它是意料之中,但如果【生产】日志里出现,
    说明有人把 EVAL_READ_ONLY 带上了线 —— 那要立刻看得见。
    """
    import logging

    from pipeline import node_executor as ne

    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(ne, "log", logging.getLogger("test.ne2"))
    # 环境解耦:embed 走真凭据,CI 上没有 → 在 embed 就断了,闸根本没被够到
    # (本地有 .env 一直是真跑,所以这两条只在 CI 红 —— "GCP_PROJECT 假失败")。
    # 桩掉向量这一环,analyze_snippet 与 index_entry 里的闸仍是真货。
    monkeypatch.setattr("pipeline.embeddings.embed_texts",
                        lambda texts, **kw: [[0.0] * 768 for _ in texts])

    with caplog.at_level(logging.DEBUG, logger="test.ne2"):
        ne._index_analyze_result("v9", {"answer": "x", "enough": "yes"}, "av:v9:k")

    hits = [r for r in caplog.records if "[EVAL_READ_ONLY]" in r.getMessage()]
    assert hits, "闸拦下了却没留下可 grep 的痕迹"
    assert hits[0].levelno >= logging.ERROR, (
        f"用了 {hits[0].levelname} —— 生产日志里出现这条意味着开关被带上线了,该是 ERROR")
    assert "v9" in hits[0].getMessage(), "没说是哪个视频,查起来还得翻 trace"


# ── 豁免名单(EVAL_READ_ONLY_ALLOW)────────────────────────────────
def test_allowlist_admits_named_path_only(monkeypatch):
    """闸挡的是【写生产库】,不是"写"这个动作:评测世界把记忆换成 world_state 替身后,
    update_memory 物理到不了生产 —— 由装替身的一方显式豁免;索引两路照拦。
    (多轮基线实测:不豁免时记忆题 agent 全轴满分、state_assertions 恒 0 —— 量的是闸。)"""
    from pipeline import config
    from pipeline import eval_write_guard as G

    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    monkeypatch.setenv("EVAL_READ_ONLY_ALLOW", "update_memory")
    G.reset_blocked()
    G.assert_writes_allowed("update_memory")          # 豁免:不抛、不计数
    assert G.blocked_count("update_memory") == 0
    import pytest as _pt
    with _pt.raises(G.EvalWriteBlocked):
        G.assert_writes_allowed("semantic_index.index_entry")   # 真打生产的照拦
    assert G.blocked_count("semantic_index.index_entry") == 1


def test_allowlist_defaults_to_empty(monkeypatch):
    """不声明就没有豁免 —— 默认全拦,豁免必须是显式动作。"""
    from pipeline import config
    from pipeline import eval_write_guard as G

    monkeypatch.setattr(config, "EVAL_READ_ONLY", True)
    monkeypatch.delenv("EVAL_READ_ONLY_ALLOW", raising=False)
    G.reset_blocked()
    import pytest as _pt
    with _pt.raises(G.EvalWriteBlocked):
        G.assert_writes_allowed("update_memory")
