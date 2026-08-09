"""批 6:跑道末步工具面收窄 —— 机制,非话术。

## 为什么有这一批

nudge 文案两版、两次跑批(dp-main / 5d-newbase)各 4 个跑次:第 12 步收到
"下一步必须先摆,不摆全作废"的硬顺序,四个全部无视,接着烧 sql/semantic/analyze
到死,16+ 次 gold 交付归零。大脑在规划惯性里不执行收口指令 —— 措辞救不了。
机制:最后 N 步(config.RUNWAY_NARROW_LEFT,默认 2)发给大脑的工具声明只剩
show_video;文本收口始终可用 —— 收窄逼的是"要么摆、要么答",不是逼调用。

## 边界(每条都有测试钉着)

  · 只对声明 supports_narrowing 的 conversation 生效 —— 测试替身/旧后端零影响;
  · 子 agent 豁免(交付物是文本证据,不是 show_video);
  · 过滤出来是空的 → 回退全量声明(fail-open,绝不发零工具请求);
  · 标签行只发一次 —— 大脑得知道工具是系统收走的,不是自己看错了。
"""
from __future__ import annotations

import pytest

from pipeline import config
from pipeline import loop_driver as LD


class _NarrowAwareConv:
    """记录每步 allowed_tools 的替身(声明 supports_narrowing)。一直调 sql_query 烧到墙。"""
    supports_narrowing = True
    last_thoughts = ""

    def __init__(self):
        self.sent = []               # [(msg, allowed_tools)]

    def send(self, msg, *, allowed_tools=None):
        self.sent.append((msg, allowed_tools))
        return [LD.Call("sql_query", {"sql": "SELECT 1"}, [])], None


class _PlainConv:
    """不支持收窄的替身:send 签名【没有】allowed_tools —— 传了就 TypeError。
    这正是本测试要保证不发生的事(既有测试替身/旧后端全是这个形状)。"""
    last_thoughts = ""

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return [LD.Call("sql_query", {"sql": "SELECT 1"}, [])], None


def _ok_exec(*a, **k):
    return LD.ExecResult(ok=True, value=[{"v": 1}], preview=[{"v": 1}], n=1)


def test_last_n_steps_only_offer_delivery_tools():
    """烧到墙的跑次:最后 2 步的 generate 必须带 allowed_tools=交付工具族,之前不带。

    白名单是【族】不是单个 show_video:收窄砍的是探查工具(sql/semantic/analyze),
    不是交付通道 —— 生产的表格/统计类长请求末步同样要交付(review 抓的波及面)。
    """
    conv = _NarrowAwareConv()
    r = LD.run_loop("q", conv, _ok_exec, max_steps=8, narrow_last=2)
    assert r.terminated == "max_steps"
    narrowed = [i for i, (_, a) in enumerate(conv.sent) if a == LD.NARROW_DELIVERY_TOOLS]
    plain = [i for i, (_, a) in enumerate(conv.sent) if a is None]
    assert narrowed, "一步都没收窄 —— 机制没接上"
    assert all(i >= len(conv.sent) - 2 for i in narrowed), (
        f"收窄发生在倒数第 3 步之前:{narrowed}(总 {len(conv.sent)} 步)—— 收早了是抢大脑的活")
    assert plain and max(plain) < min(narrowed), "收窄前的步子也被收了"
    assert "show_table" in LD.NARROW_DELIVERY_TOOLS and "show_stat" in LD.NARROW_DELIVERY_TOOLS, \
        "交付族缺表格/统计 —— 生产末步的表格交付通道又被收走了"


def test_narrow_label_note_fires_exactly_once_and_in_sync():
    """机制要配标签,且标签必须与首个收窄步【同一步】生效 —— 先收工具、下一步才解释,
    大脑中间那步会懵。

    (第一版的同步断言是 `x == y or x >= 1` 的恒真式 —— review 变异实测 9/9 全绿。
    现在钉死:max_steps=8、narrow_last=2 → 首个收窄步 = 第 6 步,标签也必须在第 6 步。)
    """
    conv = _NarrowAwareConv()
    r = LD.run_loop("q", conv, _ok_exec, max_steps=8, narrow_last=2)
    notes = [t for t in r.turns if "工具面已收窄" in str(t.get("nudge", ""))]
    assert len(notes) == 1, f"标签行发了 {len(notes)} 次"
    first_narrow_idx = min(i for i, (_, a) in enumerate(conv.sent) if a is not None)
    assert first_narrow_idx == 6, f"前提:8 步窗 2,首个收窄步应是第 6 步,实际 {first_narrow_idx}"
    assert notes[0]["step"] == 6, (
        f"标签在第 {notes[0]['step']} 步、收窄在第 6 步 —— 不同步,大脑有一步看着工具消失没人解释")


def test_subagent_style_opt_out_disables_narrowing():
    """narrow_last=0 = 关(子 agent 传这个:交付物是文本证据,不是 show_video)。"""
    conv = _NarrowAwareConv()
    LD.run_loop("q", conv, _ok_exec, max_steps=6, narrow_last=0)
    assert all(a is None for _, a in conv.sent), "豁免没生效 —— 子 agent 的末步被捆了"


def test_plain_conversation_is_untouched():
    """不声明 supports_narrowing 的 conversation:send 永远按老签名调,不传新 kwarg。
    传了就是 TypeError —— 全仓的测试替身、ScriptedWorld、旧后端全是这个形状。"""
    conv = _PlainConv()
    r = LD.run_loop("q", conv, _ok_exec, max_steps=6, narrow_last=2)
    assert r.terminated == "max_steps"          # 正常跑完,没有 TypeError
    assert len(conv.sent) == 6


def test_default_comes_from_config(monkeypatch):
    """narrow_last 不传 → 用 config.RUNWAY_NARROW_LEFT;设 0 = 全局关。"""
    monkeypatch.setattr(config, "RUNWAY_NARROW_LEFT", 3)
    conv = _NarrowAwareConv()
    LD.run_loop("q", conv, _ok_exec, max_steps=8)
    assert sum(1 for _, a in conv.sent if a is not None) == 3
    monkeypatch.setattr(config, "RUNWAY_NARROW_LEFT", 0)
    conv2 = _NarrowAwareConv()
    LD.run_loop("q", conv2, _ok_exec, max_steps=8)
    assert all(a is None for _, a in conv2.sent), "RUNWAY_NARROW_LEFT=0 应与批 6 之前逐字节一致"


def test_filter_decls_is_fail_open_on_empty():
    """过滤出来是空的 → 回退全量声明。收窄是逼收口,不是发零工具请求
    (show_video 可能被上游按 owner/sandbox 过滤掉 —— 那一步宁可不收窄)。"""
    decls = [{"name": "sql_query"}, {"name": "semantic_search"}]
    assert LD._filter_decls(decls, ("show_video",)) == decls
    both = [{"name": "sql_query"}, {"name": "show_video"}]
    assert LD._filter_decls(both, ("show_video",)) == [{"name": "show_video"}]


def test_genai_backend_declares_the_capability():
    """能力声明是机制的开关面:GenAIConversation 必须带 supports_narrowing=True,
    哪天有人重构丢了它,收窄会静默失效(run_loop 只认这个属性),这里要红。"""
    assert getattr(LD.GenAIConversation, "supports_narrowing", False) is True


def test_narrowed_step_blocks_execution_of_revoked_tools():
    """执行层背书(review 的 HIGH):genai 对声明集不做硬约束 —— 模型凭规划惯性照吐
    sql_query 时,只收声明等于没收。收窄步里非交付工具的调用必须【不执行】:
    execute 闭包一次都不被调,错误回灌带收窄说明,error_code=NARROW_BLOCKED 供验尸。
    """
    calls_seen = []

    def _spy_exec(cid, name, inputs, upstream, uses):
        calls_seen.append(name)
        return LD.ExecResult(ok=True, value=[{"v": 1}], preview=[{"v": 1}], n=1)

    conv = _NarrowAwareConv()          # 无视收窄、一直吐 sql_query —— 正是要治的行为
    r = LD.run_loop("q", conv, _spy_exec, max_steps=8, narrow_last=2)
    n_narrowed = sum(1 for _, a in conv.sent if a is not None)
    assert n_narrowed >= 1
    # 收窄步的 sql_query 一次都不许真执行
    assert len(calls_seen) == len(conv.sent) - n_narrowed, (
        f"收窄步的被收工具照样执行了:执行 {len(calls_seen)} 次,"
        f"应为 {len(conv.sent) - n_narrowed}(非收窄步数)—— 声明层收窄被静默绕穿")
    blocked = [t for t in r.trace if t.get("error_code") == "NARROW_BLOCKED"]
    assert blocked, "被拦的调用在 trace 里无迹可寻 —— 验尸分不清'服软'与'硬闯被拦'"
    assert all(not t["ok"] for t in blocked)


def test_narrowed_step_still_executes_delivery_tools():
    """背书只拦探查工具:收窄步里的 show_video / show_table 必须照常执行。"""
    calls_seen = []

    def _spy_exec(cid, name, inputs, upstream, uses):
        calls_seen.append(name)
        return LD.ExecResult(ok=True, value=[{"v": 1}], preview=[{"v": 1}], n=1)

    class _Conv(_NarrowAwareConv):
        def send(self, msg, *, allowed_tools=None):
            self.sent.append((msg, allowed_tools))
            if allowed_tools is not None:
                return [LD.Call("show_video", {"video_ids": ["v001"]}, [])], None
            return [LD.Call("sql_query", {"sql": "SELECT 1"}, [])], None

    LD.run_loop("q", _Conv(), _spy_exec, max_steps=6, narrow_last=2)
    assert "show_video" in calls_seen, "收窄步的交付工具也被拦了 —— 把交付通道砍了机制就成了纯惩罚"


def test_text_collapse_still_possible_under_narrowing():
    """收窄的底线:文本收口永远可用。最后一步模型不调工具、直接给答案 → terminated=text。"""
    class _Conv(_NarrowAwareConv):
        def send(self, msg, *, allowed_tools=None):
            self.sent.append((msg, allowed_tools))
            if allowed_tools is not None:            # 被收窄的那步:选择直接收口
                return [], "最终答案:找到 2 个,均已核实。"
            return [LD.Call("sql_query", {"sql": "SELECT 1"}, [])], None

    conv = _Conv()
    r = LD.run_loop("q", conv, _ok_exec, max_steps=8, narrow_last=2)
    assert r.terminated == "text"
    assert "最终答案" in (r.answer or "")


def test_subagent_call_site_actually_passes_the_opt_out():
    """钉调用点:subagents 调 run_loop 必须显式带 narrow_last=0。

    行为半边由 test_subagent_style_opt_out_disables_narrowing 守(run_loop 尊重 0);
    这半边守"子 agent 真的传了 0"。没有它,豁免参数被谁顺手删掉时,子 agent 的
    测试全绿(替身不声明收窄能力,豁免在测试里天然无感),生产里子 agent 的
    末步却被静默捆上 —— 变异验证实测:删掉后 41 条子 agent 测试照样全绿。
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "pipeline" / "subagents.py"
           ).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "run_loop"]
    assert calls, "subagents 里没找到 run_loop 调用 —— 结构变了,本测试要跟着改"
    for c in calls:
        kw = {k.arg: k for k in c.keywords}
        assert "narrow_last" in kw, "子 agent 的 run_loop 调用没传 narrow_last —— 豁免丢了"
        v = kw["narrow_last"].value
        assert isinstance(v, ast.Constant) and v.value == 0, "豁免必须是显式的 0"
