"""A3 / C3:系统 prompt 的【前缀顺序不变量】—— 固定大块在前、易变段在后。

为什么值得一条专门的测试:隐式缓存按前缀【逐字符从头比】,第一个不同的字符之后的内容
全部作废。schema 是几 KB 的共享固定块(同一批子 agent 拿的是同一份,跨请求也基本不变);
video_ids / replay / runtime_facts 是每次都不同的易变段。易变段排在前面 = 后面那一大块
永远进不了命中区,白丢一次命中 —— 而且这种"顺序错了"的 bug 不会有任何症状,
只会安静地多花钱,所以必须由测试而不是靠人记性来守。

两处都验:
  · 主 loop  —— pipeline.loop_driver._loop_system(本文件【只读它】)
  · 子 agent —— pipeline.subagents._run_one 里拼的那段 system
谁把顺序改回去,这里就红。

C3 顺序合同(Golden Test 锁的就是这两行,两处【合在一个文件】、不写两套):

    主 loop:  _LOOP_SYSTEM → schema → library_state → user_memory → runtime_facts
              → task_notice → replay
    子 agent: static prefix → schema → guard notice → video_ids/task

上面 A3 那几条验的是"schema 在所有易变段之前"这个【性质】;下面 golden 那两条验的是
【整条序列逐位相等】—— 性质测试挡得住"把 replay 提到 schema 前"这种粗错,挡不住
"library_state 和 runtime_facts 对调"(两个都在 schema 之后,性质仍成立)。两层都要。
"""
import os
from contextvars import copy_context

import pytest

_SCHEMA_MARK = "MARK_SCHEMA_TABLE"
_DECL_NAMES = ("analyze_video", "semantic_search", "sql_query", "web_search", "spawn_agents")


# ── 主 loop:schema 必须排在所有易变段之前 ──────────────────────────────
def test_main_loop_schema_precedes_every_volatile_segment():
    """_loop_system 的三个注入段(runtime_facts / task_notice / replay_context)全是易变的:
    运行时状态每轮变、后台通知偶发、回放随会话长度变。它们必须全部在 schema 之后。"""
    from pipeline import loop_driver
    s = loop_driver._loop_system({_SCHEMA_MARK: ["a", "b"]},
                                 replay_context="MARK_REPLAY",
                                 runtime_facts="MARK_FACTS",
                                 task_notice="MARK_NOTICE")
    i_schema = s.index(_SCHEMA_MARK)
    for mark in ("MARK_FACTS", "MARK_NOTICE", "MARK_REPLAY"):
        assert mark in s, f"{mark} 没被注入,这条测试就没在验它想验的东西"
        assert i_schema < s.index(mark), f"主 loop:schema 必须排在易变段 {mark} 之前"


def test_main_loop_prefix_is_identical_up_to_schema_across_requests():
    """两次【不同】请求(不同回放/不同运行时状态)的公共前缀必须一路盖过整块 schema。
    这就是顺序换位买到的东西本身,不是顺序的间接指标。"""
    from pipeline import loop_driver
    schema = {_SCHEMA_MARK: ["c" * 300]}
    a = loop_driver._loop_system(schema, replay_context="第1轮回放", runtime_facts="facts A")
    b = loop_driver._loop_system(schema, replay_context="第2轮回放 完全不同", runtime_facts="facts B")
    common = os.path.commonprefix([a, b])
    assert _SCHEMA_MARK in common and "c" * 300 in common


# ── C3 Golden:主 loop 七段【逐位】顺序合同 ───────────────────────────
def test_main_loop_golden_segment_order():
    """合同原文:_LOOP_SYSTEM → schema → library_state → user_memory → runtime_facts
    → task_notice → replay。

    每段塞一个唯一哨兵,按它们在成品里的出现位置排序,断言排出来的次序【逐位】等于合同。
    对调任意相邻两段就红 —— 这正是 A3 那几条性质测试漏掉的那类改动。

    2026-08-03 调换 user_memory / runtime_facts:排序尺子是"越稳的越靠前",
    而 runtime_facts 里带本会话累计 token 与花费,每轮必变;它排在前面时,
    user_memory 那段每轮都注定 cache miss。下面 `..._is_cached_across_turns`
    验的就是这次调换真的买到了东西。"""
    from pipeline import loop_driver
    s = loop_driver._loop_system(
        {_SCHEMA_MARK: ["a"]},
        "MARK_G_REPLAY",
        "MARK_G_FACTS",
        task_notice="MARK_G_NOTICE",
        library_state="MARK_G_LIBSTATE",
        user_memory="MARK_G_MEMORY",
    )
    contract = ["MARK_G_LIBSTATE", "MARK_G_MEMORY", "MARK_G_FACTS",
                "MARK_G_NOTICE", "MARK_G_REPLAY"]
    for m in contract:
        assert m in s, f"{m} 没被注入,这条测试就没在验它想验的东西"
    # 静态前缀 → schema 打头(A3 本体),其后五段按合同排
    assert s.startswith(loop_driver._LOOP_SYSTEM), "静态宪法前缀必须逐字节打头"
    assert s.index(_SCHEMA_MARK) < min(s.index(m) for m in contract)
    assert sorted(contract, key=s.index) == contract, \
        "主 loop 七段顺序合同被改了(见本文件 docstring 的合同原文)"


def test_user_memory_is_cached_across_turns_because_it_precedes_runtime_facts():
    """验的是【买到的东西】本身,不是顺序这个间接指标。

    同一会话的第二轮:静态前缀、schema、库存快照、用户记忆都没变,变的只有
    运行时状态(累计 token 每轮都在涨)和回放。那么两轮的公共前缀【必须一路盖过
    整段用户记忆】—— 盖不过就说明记忆段排在易变段后面,每轮白付一次全价。

    用 800 字符的记忆段:太短的话即使排错了,commonprefix 也可能因为巧合
    看起来差不多,这条就成了空转的。
    """
    from pipeline import loop_driver
    schema = {_SCHEMA_MARK: ["a"]}
    mem = "# 用户记忆\n" + "记住我偏好中文回答。" * 80          # ≈800 字符
    lib = "# 库存快照\n{\"videos_total\":514}"
    a = loop_driver._loop_system(schema, "第1轮回放",
                                 "累计 12,345 tokens ≈ $0.0210",
                                 library_state=lib, user_memory=mem)
    b = loop_driver._loop_system(schema, "第2轮回放 完全不同",
                                 "累计 98,765 tokens ≈ $0.1730",
                                 library_state=lib, user_memory=mem)
    common = os.path.commonprefix([a, b])
    assert mem in common, (
        "两轮的公共前缀没盖住用户记忆段 —— 它排在每轮必变的运行时状态后面了,"
        "那一段每轮都是 cache miss")
    assert "累计 12,345" not in common, (
        "前提检查:两轮的运行时状态必须真的不同,否则这条测试是空转的")


def test_main_loop_new_segments_are_keyword_only():
    """C3:library_state / user_memory 必须是 keyword-only。

    位置传参一旦被允许,`_loop_system(schema, replay, rt, notice, lib, mem)` 这种调用
    就会在有人调整参数次序时静默传错段(库存快照当成用户记忆注入),而两者都是字符串,
    类型检查抓不到。同时确认前三个参【仍可位置传】—— evals/ 与既有测试是按位置调的。"""
    import inspect
    from pipeline import loop_driver
    p = inspect.signature(loop_driver._loop_system).parameters
    for name in ("library_state", "user_memory"):
        assert p[name].kind is inspect.Parameter.KEYWORD_ONLY, f"{name} 必须 keyword-only"
    for name in ("schema", "replay_context", "runtime_facts"):
        assert p[name].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


# ── 子 agent:同一套不变量 ─────────────────────────────────────────────
def _capture_subagent_system(monkeypatch, task: dict, schema: dict) -> str:
    """跑一个子 agent,把它交给 make_conversation 的 system 原样捞出来。

    不触真模型/执行器:loop_driver 的三件套全 stub(与 test_subagents.py 同法)。
    过 copy_context —— _run_one 会 set 深度/枝计数,直接跑会漏进测试进程的上下文。"""
    from pipeline import loop_driver, subagents
    from pipeline.loop_driver import LoopResult
    seen: dict = {}

    monkeypatch.setattr(loop_driver, "loop_function_declarations",
                        lambda: [{"name": n} for n in _DECL_NAMES])

    def fake_make_conv(model, decls, system, image=None):
        seen["system"] = system
        return object()

    monkeypatch.setattr(loop_driver, "make_conversation", fake_make_conv)
    monkeypatch.setattr(loop_driver, "run_loop",
                        lambda *a, **k: LoopResult(answer="ok", steps=1, terminated="text",
                                                   trace=[], ledger={}, llm_calls=1))
    copy_context().run(subagents._run_one, task, execute=None, sandbox=None, trace=None,
                       schema=schema, session_id=None, owner="t", model="m", max_steps=4)
    return seen["system"]


@pytest.mark.parametrize("tools", [["analyze_video", "sql_query"],
                                   ["sql_query", "spawn_agents"]])
def test_subagent_schema_precedes_video_ids(monkeypatch, tools):
    """A3 本体:schema JSON 必须在【只针对这些视频作答】之前。
    video_ids 是每个子 agent 都不同的易变段 —— 它排前面会把整块 schema 顶出命中区。"""
    task = {"instruction": "看 A 组", "video_ids": ["MARK_VID_ONE"], "tools": tools}
    s = _capture_subagent_system(monkeypatch, task, {_SCHEMA_MARK: ["x"]})
    assert _SCHEMA_MARK in s and "MARK_VID_ONE" in s
    assert s.index(_SCHEMA_MARK) < s.index("MARK_VID_ONE"), \
        "子 agent:schema 必须排在【只针对这些视频作答】之前"


def test_subagent_schema_precedes_toolset_hint(monkeypatch):
    """工具集提示(握有 spawn_agents 才加)也是随任务变的段 —— 同样排在 schema 之后。"""
    task = {"instruction": "看 A 组", "video_ids": [], "tools": ["sql_query", "spawn_agents"]}
    s = _capture_subagent_system(monkeypatch, task, {_SCHEMA_MARK: ["x"]})
    assert "你握有 spawn_agents" in s
    assert s.index(_SCHEMA_MARK) < s.index("你握有 spawn_agents")


def test_subagent_golden_segment_order(monkeypatch):
    """合同另一半:static prefix → schema → guard notice → video_ids/task。

    与主 loop 那条 golden 放同一个文件:顺序合同只有一份,别在两处各写一套然后祈祷
    它们不漂移(这正是 sql_bounds 抽公共模块时立的同一条规矩)。"""
    from pipeline import subagents
    task = {"instruction": "看 A 组", "video_ids": ["MARK_VID_ONE"],
            "tools": ["sql_query", "spawn_agents"]}
    s = _capture_subagent_system(monkeypatch, task, {_SCHEMA_MARK: ["x"]})
    contract = [_SCHEMA_MARK, "你握有 spawn_agents", "MARK_VID_ONE"]
    for m in contract:
        assert m in s, f"{m} 没被注入,这条测试就没在验它想验的东西"
    assert s.startswith(subagents._SUBAGENT_SYSTEM), "子 agent 静态前缀必须逐字节打头"
    assert sorted(contract, key=s.index) == contract, \
        "子 agent 四段顺序合同被改了(见本文件 docstring 的合同原文)"


def test_two_subagents_share_prefix_through_whole_schema(monkeypatch):
    """两个子 agent(不同视频、不同工具集)的公共前缀必须【一路盖过整块 schema】。
    换位前:公共前缀在 video_ids 那一行就断了,后面几 KB schema 全是各算各的。"""
    schema = {_SCHEMA_MARK: ["c" * 300]}
    a = _capture_subagent_system(
        monkeypatch, {"instruction": "A 组", "video_ids": ["v_aaa"],
                      "tools": ["analyze_video", "sql_query"]}, schema)
    b = _capture_subagent_system(
        monkeypatch, {"instruction": "B 组", "video_ids": ["v_bbb"],
                      "tools": ["sql_query", "spawn_agents"]}, schema)
    common = os.path.commonprefix([a, b])
    assert _SCHEMA_MARK in common and "c" * 300 in common, \
        "两个子 agent 的公共前缀没盖住整块 schema —— 共享固定块被易变段顶出了命中区"
