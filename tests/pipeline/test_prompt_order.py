"""A3:系统 prompt 的【前缀顺序不变量】—— 固定大块在前、易变段在后。

为什么值得一条专门的测试:隐式缓存按前缀【逐字符从头比】,第一个不同的字符之后的内容
全部作废。schema 是几 KB 的共享固定块(同一批子 agent 拿的是同一份,跨请求也基本不变);
video_ids / replay / runtime_facts 是每次都不同的易变段。易变段排在前面 = 后面那一大块
永远进不了命中区,白丢一次命中 —— 而且这种"顺序错了"的 bug 不会有任何症状,
只会安静地多花钱,所以必须由测试而不是靠人记性来守。

两处都验:
  · 主 loop  —— pipeline.loop_driver._loop_system(本文件【只读它】)
  · 子 agent —— pipeline.subagents._run_one 里拼的那段 system
谁把顺序改回去,这里就红。
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
