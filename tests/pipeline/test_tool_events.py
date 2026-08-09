"""C6 ToolEvent:现有 TraceStep 的一次【确定性投影】,给事后 triage 用。

## 为什么是"投影"而不是"新模型"

TraceStep 已经有全部字段(span_id/parent_id/depth/component/cause/t_start/t_end/tok/cost_usd)。
另立一套 schema 只会造出【第二份事实】—— 两边字段迟早漂移,而漂移的那天没人知道该信哪个。
所以这里测的第一件事就是:它必须是纯函数,同样的 trace 永远得到同样的事件。

## 三条硬性质

  ① `call_id == span_id` —— 拿事件流回查 trace 不需要第二张映射表;
  ② 只取 exec 层 —— 主循环的 generate/schema 不是"工具事件",混进来"调了几次工具"就说不清;
  ③ **永远不进 prompt** —— 它是给人和 triage 看的。混进去等于每步再付一遍观测的钱。
"""
from __future__ import annotations

import logging


from pipeline.agentops.trace import Trace, tool_events


def _trace_with(*specs) -> Trace:
    """specs = [(name, component, 结局)] —— 结局 ok/fail/soft。"""
    t = Trace(quiet=True)
    for name, comp, how in specs:
        s = t.step(name, component=comp)
        if how == "ok":
            s.ok(rows=1)
        elif how == "fail":
            s.fail(error="boom", cause="EXEC_TOOL_ERROR")
        else:
            s.soft("EXEC_NOT_CONVERGED", error="max_steps")
    return t


def test_projection_is_pure_and_deterministic():
    t = _trace_with(("[c0_0/sql_query]", "exec", "ok"),
                    ("[c0_1/analyze_video]", "exec", "fail"))
    assert tool_events(t) == tool_events(t), "同一条 trace 投影出两个结果 —— 那就不是投影了"
    assert tool_events(t) == tool_events(t.steps), "接受 Trace 与接受 steps 列表必须等价"


def test_call_id_is_the_span_id_not_a_new_one():
    """另造一套 id = 事后拿事件流回查 trace 还得维护一张映射表。"""
    t = _trace_with(("[c0_0/sql_query]", "exec", "ok"),
                    ("[c0_1/analyze_video]", "exec", "ok"))
    ev = tool_events(t)
    assert [e["call_id"] for e in ev] == [s.span_id for s in t.steps if s.component == "exec"]
    assert all(e["call_id"] for e in ev), "有事件没有 id —— 回查时对不上号"


def test_only_exec_layer_becomes_a_tool_event():
    """主循环的 generate、schema 拉取不是"工具事件"。混进来之后
    "这次请求调了几次工具"这个数就说不清了 —— 而那是 triage 的第一个问题。"""
    t = _trace_with(("Loop", "main", "ok"),
                    ("[c0_0/sql_query]", "exec", "ok"),
                    ("Schema", "main", "ok"))
    ev = tool_events(t)
    assert len(ev) == 1 and ev[0]["name"] == "[c0_0/sql_query]"
    assert len(t.steps) == 3, "前提:trace 里确实有非 exec 的 span,否则这条是空转的"


def test_failure_cause_survives_the_projection():
    """triage 的入口是因由码。投影把它丢了,事件流就只能告诉你"失败了"、不告诉你为什么。"""
    t = _trace_with(("[c0_0/analyze_video]", "exec", "fail"),
                    ("[c0_1/spawn_agents]", "exec", "soft"))
    ev = tool_events(t)
    assert [e["status"] for e in ev] == ["error", "softfail"]
    assert [e["cause"] for e in ev] == ["EXEC_TOOL_ERROR", "EXEC_NOT_CONVERGED"]


def test_cost_and_tokens_come_along():
    """事件流要能独立回答"这次请求钱花在哪一步" —— 少了这两个就得回去翻 trace。"""
    t = Trace(quiet=True)
    s = t.step("[c0_0/analyze_video]", component="exec")
    s.tok = {"in": 60000, "out": 300}
    s.cost_usd = 0.0182
    s.ok()
    ev = tool_events(t)[0]
    assert ev["tok"] == {"in": 60000, "out": 300} and ev["cost_usd"] == 0.0182
    ev["tok"]["in"] = 0                       # 投影出来的是副本
    assert t.steps[0].tok["in"] == 60000, "投影没拷贝 tok —— 消费方一改就污染了 trace 本体"


# ── 发射钩子 ────────────────────────────────────────────────────────
def test_emit_is_shadow_by_default(monkeypatch, caplog):
    """默认 shadow:算出来只进 DEBUG,不影响任何行为。"""
    from pipeline import config
    from pipeline import loop_driver as ld

    assert config.USE_TOOL_EVENT == "shadow", "默认值变了 —— 这条测试的前提要重看"
    monkeypatch.setattr(ld, "log", logging.getLogger("test.te"))
    t = _trace_with(("[c0_0/sql_query]", "exec", "ok"))
    with caplog.at_level(logging.DEBUG, logger="test.te"):
        ld._emit_tool_events(t, 0)
    recs = [r for r in caplog.records if "[tool_event]" in r.getMessage()]
    assert recs and all(r.levelno == logging.DEBUG for r in recs), (
        "shadow 模式却写了 INFO —— 默认就该是安静的")


def test_emit_can_be_turned_up(monkeypatch, caplog):
    from pipeline import config
    from pipeline import loop_driver as ld

    monkeypatch.setattr(config, "USE_TOOL_EVENT", "1")
    monkeypatch.setattr(ld, "log", logging.getLogger("test.te2"))
    t = _trace_with(("[c0_0/sql_query]", "exec", "ok"))
    with caplog.at_level(logging.DEBUG, logger="test.te2"):
        ld._emit_tool_events(t, 0)
    assert any(r.levelno == logging.INFO and "[tool_event]" in r.getMessage()
               for r in caplog.records)


def test_emit_only_covers_the_new_spans(monkeypatch, caplog):
    """水位线:一次工具调用只发它【自己】产生的 span。

    不取水位线的话,每次调用都把整条 trace 重发一遍 —— N 次调用发 N²/2 条事件,
    "完整率"这个指标当场失去意义。
    """
    from pipeline import loop_driver as ld

    monkeypatch.setattr(ld, "log", logging.getLogger("test.te3"))
    t = _trace_with(("[c0_0/sql_query]", "exec", "ok"))
    mark = len(t.steps)
    t.step("[c0_1/analyze_video]", component="exec").ok()
    with caplog.at_level(logging.DEBUG, logger="test.te3"):
        ld._emit_tool_events(t, mark)
    msgs = [r.getMessage() for r in caplog.records if "[tool_event]" in r.getMessage()]
    assert len(msgs) == 1 and "analyze_video" in msgs[0], f"发多了/发错了:{msgs}"


def test_emit_is_fail_open(monkeypatch, caplog):
    """观测绝不能拖垮请求 —— 这是本仓 T-1 就定下的规矩。"""
    from pipeline import loop_driver as ld

    monkeypatch.setattr(ld, "log", logging.getLogger("test.te4"))
    ld._emit_tool_events(object(), 0)        # 完全不是 trace,也不许抛
    ld._emit_tool_events(None, 0)

    # 上面两个都走 `getattr(...) or []` 平安落地 —— try 块【根本没抛】,
    # 所以它们验不到 fail-open(实测:把 except 改成 raise,这两行照样绿)。
    # 要真验,得让异常在 try 【内部】发生。
    class _Explodes:
        @property
        def steps(self):
            raise RuntimeError("观测层自己炸了")

    ld._emit_tool_events(_Explodes(), 0)     # 必须被吞掉:观测绝不能拖垮这次工具调用

    class _BadStep:                          # 投影时才炸(字段访问异常)
        component = "exec"

        @property
        def span_id(self):
            raise RuntimeError("字段访问炸了")

    class _T:
        steps = [_BadStep()]

    ld._emit_tool_events(_T(), 0)


def test_emit_can_be_turned_off(monkeypatch, caplog):
    from pipeline import config
    from pipeline import loop_driver as ld

    monkeypatch.setattr(config, "USE_TOOL_EVENT", "off")
    monkeypatch.setattr(ld, "log", logging.getLogger("test.te5"))
    t = _trace_with(("[c0_0/sql_query]", "exec", "ok"))
    with caplog.at_level(logging.DEBUG, logger="test.te5"):
        ld._emit_tool_events(t, 0)
    assert not [r for r in caplog.records if "[tool_event]" in r.getMessage()]


def test_tool_events_never_reach_the_prompt():
    """§12:内部观测,不进 Prompt。

    判据是静态的:prompt 装配那一层(_loop_system / _LOOP_SYSTEM / 回灌 notice)
    一个字都不许提 tool_event。它一旦进了 prompt,就是每步再付一遍观测的钱,
    而大脑对这些 id 和耗时做不出任何有用的判断。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "pipeline" / "loop_driver.py").read_text(encoding="utf-8")
    # 提取 _loop_system 函数体 + 冻结前缀构造 + 回灌 notice 构造
    for fn in ("def _loop_system(", "def _build_loop_system(", "def _context_note("):
        i = src.find(fn)
        if i < 0:
            continue
        body = src[i:src.find("\ndef ", i + 1)]
        assert "tool_event" not in body.lower(), (
            f"{fn} 里提到了 tool_event —— 观测数据混进 prompt 了")
