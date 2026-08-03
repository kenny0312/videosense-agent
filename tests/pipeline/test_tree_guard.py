"""P0-3:per-tree 美元熔断 + 墙钟(双挂点)。

治的病:一次请求里一棵 agent 树把钱烧穿(DVD 实测 Trap 税 12× 成本方差)。
红队 B1:只挂工具闸挡不住"进入 Trap 循环只思考不调工具"—— 那条路径永远不经过 execute,
所以必须同时挂 run_loop 的每步 generate 前。
红队 B3(review 变异验证后重修):钱是调用结束后才落账的,放行必须【预留】(admit/settle),
否则 K 个并行 worker 在"在飞"窗口互相看不见,超冲 = K × 单次成本 —— 光加锁防不住。
本文件按 review 的变异结论逐条钉死:每个测试都要求"删掉对应生产逻辑就必挂"。
离线、零 API。
"""
import threading

import pytest

from pipeline import config, loop_driver
from pipeline.agentops import trace as T
from pipeline.agentops import usage
from pipeline.agentops.treeguard import GUARD_NOTE_PREFIX, TreeGuard


@pytest.fixture(autouse=True)
def _fresh():
    usage.reset_usage()
    yield
    usage.reset_usage()


def _spend(dollars: float, model="gemini-2.5-flash"):
    """往 usage 里塞出指定金额(用 in token 反推,精确可控)。"""
    p = usage._PRICE[model]
    tin = int(dollars / p["in"] * 1e6)

    class _UM:
        prompt_token_count = tin
        candidates_token_count = 0
        total_token_count = tin
        cached_content_token_count = 0

    class _R:
        usage_metadata = _UM()
    usage.add_usage(_R(), model)


# ── guard 本体 ──
def test_disabled_by_default_is_a_noop():
    """Part 0 不变量①:开关全关(cap=0)时 admit/settle/reconcile 恒放行,行为与升级前等价。"""
    g = TreeGuard(cost_cap=0, wall_cap_s=0)
    assert not g.enabled
    _spend(99.0)
    assert g.admit() is None and g.tripped is None
    g.settle()
    g.reconcile()
    assert g.tripped is None


def test_budget_gate_is_predictive_not_after_the_fact():
    """判据是"预估后比":spent + 在飞 + 本次估价 > cap 即拦。事后比会让最后一次调用总能越线。"""
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.06)                       # 已花 0.06,+0.05 预估 = 0.11 > 0.10 → 拦
    env = g.admit()
    assert env and g.tripped == "budget"
    assert "没执行" in env and "【未核查】" in env      # 软收口信封:教收口 + 强制弃权标注


def test_budget_gate_lets_cheap_calls_through():
    g = TreeGuard(cost_cap=1.0, call_estimate=0.05)
    _spend(0.10)
    assert g.admit() is None and g.tripped is None


def test_admit_reserves_inflight_cost():
    """红队 B3 核心:admit 放行即预留 —— 不 settle 的连续放行,光靠 pending 就能触闸
    (钱一分没落账也一样)。删掉预留逻辑本测试必挂。"""
    g = TreeGuard(cost_cap=0.20, call_estimate=0.05)
    results = [g.admit() for _ in range(5)]
    assert results[:4] == [None] * 4                   # 0+0.15+0.05=0.20 不>0.20,第 4 个仍放行
    assert results[4] is not None and g.tripped == "budget"   # 0+0.20+0.05 > 0.20 → 拦


def test_settle_releases_reservation():
    """admit/settle 配对使用时,串行调用永不因 pending 累积误触。"""
    g = TreeGuard(cost_cap=0.20, call_estimate=0.05)
    for _ in range(10):
        assert g.admit() is None
        g.settle()
    assert g.tripped is None


def test_wall_gate_trips_on_time():
    g = TreeGuard(cost_cap=0, wall_cap_s=0.01)
    import time as _t
    _t.sleep(0.02)
    assert g.admit() and g.tripped == "wall"


def test_wasted_usd_is_live_not_frozen():
    """review 确认的真 bug:触闸后模型只 generate 不调工具(最常见宽限形态)时没人再调闸,
    存量式 wasted 字段永远停在 0 → 披露系统性错报。现算:不需要任何后续调用就看得见。"""
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.20)
    g.admit()
    assert g.tripped == "budget"
    _spend(0.30)                       # 触闸后又烧了(宽限 generate / 已在飞的并行调用)
    assert g.wasted_usd == pytest.approx(0.30, rel=0.02)      # 无需再调闸
    note = g.final_note()
    assert "触发成本护栏" in note and "$0.3" in note           # 浪费金额出现在用户可见披露里


def test_concurrent_under_cap_admits_are_bounded():
    """红队 B3 主形状(review 变异验证:旧测试删锁全绿):spent 逼近但未到 cap 时,
    K 个并行 admit 不能全放行 —— 预留让在飞成本互相可见,放行数被 (cap-spent)/est 钉死。"""
    from contextvars import copy_context
    g = TreeGuard(cost_cap=1.0, call_estimate=0.05)
    _spend(0.79)
    results = []
    lock = threading.Lock()

    def w():
        r = g.admit()
        with lock:
            results.append(r)
    ths = [threading.Thread(target=copy_context().run, args=(w,)) for _ in range(12)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    admitted = sum(1 for r in results if r is None)
    assert admitted == 4                # 0.79+p+0.05≤1.0 只容 p∈{0,.05,.10,.15} 四个
    assert g.tripped == "budget"        # 第 5 个起触闸,且只 trip 一次


def test_blind_thread_cannot_erase_already_spent_money():
    """自测逮出的真 bug:usage 是 contextvar,忘了 copy_context 的裸线程读到 $0 → 闸失明。
    高水位让 guard 只记得钱不忘钱:失明线程至多不推进账目,绝不放行。"""
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.20)
    assert g.spent() == pytest.approx(0.20, rel=0.02)     # 主线程看得见
    out = []
    t = threading.Thread(target=lambda: out.append(g.admit()))   # 裸线程:usage 为空
    t.start(); t.join()
    assert out[0] is not None                            # 仍然被拦(读到高水位)


def test_trip_writes_a_guard_span_with_cause_once():
    """触闸要在 trace 上留一个 guard 层 span(带 GUARD_BUDGET),且只留一次。"""
    tr = T.Trace(quiet=True)
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05, trace=tr)
    _spend(0.20)
    g.admit(); g.admit(); g.admit()
    spans = [s for s in tr.as_list() if s["component"] == "guard"]
    assert len(spans) == 1
    assert spans[0]["cause"] == "GUARD_BUDGET" and spans[0]["status"] == "softfail"
    assert spans[0]["meta"]["cost_cap"] == 0.10


def test_snapshot_shape():
    g = TreeGuard(cost_cap=0.5, wall_cap_s=100)
    s = g.snapshot()
    assert set(s) == {"spent_usd", "cost_cap", "elapsed_s", "wall_cap_s", "tripped",
                      "wasted_usd"}


def test_final_note_wall_shows_seconds_not_dollar_cap():
    """review 确认:wall-only(生产推荐姿势)触闸时旧文案渲染『上限 $0.00』——
    把"关"状态的美元线当被突破的上限展示,真正触线的秒数反而不说。"""
    g = TreeGuard(cost_cap=0, wall_cap_s=0.01)
    import time as _t
    _t.sleep(0.02)
    g.admit()
    note = g.final_note()
    assert g.tripped == "wall" and "墙钟" in note
    assert "上限 $0.00" not in note


def test_final_note_claims_marks_only_when_envelope_fed():
    """review 确认:收口处【首次】发现超支时模型从没见过信封、答案里没有【未核查】标注,
    note 却声称"已在上文标注"= 对完整答案附假陈述。声称必须跟随信封是否真喂过。"""
    g1 = TreeGuard(cost_cap=0.10)
    _spend(0.20)
    g1.reconcile()                                       # 收口补记账:不产生信封
    assert g1.tripped == "budget"
    assert "已在上文标注" not in g1.final_note()

    g2 = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.20)
    assert g2.admit()                                    # 信封确实喂出去了
    assert "已在上文标注" in g2.final_note()
    assert "已在上文标注" not in g2.final_note(mark_claim=False)   # 硬终止路径显式否掉


# ── 挂点①:工具闸(_make_executor)──
def _executor(guard):
    return loop_driver._make_executor(sandbox=None, trace=T.Trace(quiet=True), schema={},
                                      session_id=None, owner="t", guard=guard)


def test_tool_gate_blocks_and_returns_soft_envelope(monkeypatch):
    """工具闸:触闸后工具【不执行】,回软失败信封(ok=True,让大脑读到并收口)。
    preview 断言是 bug③ 的钉子(review 变异验证:回喂大脑的是 preview 不是 value,
    旧断言只查 value,preview 腰斩照样全绿)—— 信封必须【全文】抵达大脑。"""
    called = []
    monkeypatch.setattr(loop_driver, "execute_node",
                        lambda *a, **k: called.append(1))
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.20)
    ex = _executor(g)
    res = ex("c1", "sql_query", {"sql": "select 1"}, {}, [])
    assert res.ok and "没执行" in res.value["answer"] and res.value["enough"] == "no"
    assert "【未核查】" in str(res.preview)      # 真正进 prompt 的通道没被 _preview 截断
    assert not called                       # 真的没执行


def test_tool_gate_lets_show_tools_through_after_trip(monkeypatch):
    """交付类 show_* 不烧 LLM 钱,触闸后仍须能把已有结果交付给用户。"""
    seen = []

    class _NR:
        ok, value, stderr, code, artifact, videos, table, stat, cache_hit = \
            True, {"shown": 1}, "", None, {}, [{"video_id": "v"}], {}, {}, False
        attempts = 1
    monkeypatch.setattr(loop_driver, "execute_node",
                        lambda node, *a, **k: seen.append(node.tool) or _NR())
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.20)
    assert g.admit()                        # 先真的触闸
    ex = _executor(g)
    ex("c1", "show_video", {"video_ids": ["v"]}, {}, [])
    assert seen == ["show_video"]


def test_tool_gate_settles_reservation_even_on_exception(monkeypatch):
    """预留必须在 finally 里释放:工具炸了不释放 → pending 越积越多,闸门越收越紧直到误触。"""
    def _boom(*a, **k):
        raise RuntimeError("tool exploded")
    monkeypatch.setattr(loop_driver, "execute_node", _boom)
    g = TreeGuard(cost_cap=1.0, call_estimate=0.05)
    ex = _executor(g)
    with pytest.raises(RuntimeError):
        ex("c1", "sql_query", {"sql": "select 1"}, {}, [])
    assert g._pending == pytest.approx(0.0)              # 异常路径也释放了


def test_executor_exposes_guard_for_subagents():
    """子 agent 靠 execute.tree_guard 取到同一本账(不改 run_fanout 签名)。"""
    g = TreeGuard(cost_cap=0.5)
    assert _executor(g).tree_guard is g


# ── 挂点②:每步 generate 闸(run_loop)——红队 B1 的洞 ──
def _cheap_exec(*a, **k):
    """便宜工具:执行本身几乎不花钱 —— 于是钱全烧在 generate 上(红队 B1 的真实形状:
    每轮把越长的历史重发一遍,输入 token 线性涨,而工具闸看每次工具都便宜、一路放行)。"""
    return loop_driver.ExecResult(ok=True, value={"rows": 1}, preview=[["1"]], n=1)


def test_generate_gate_catches_burn_that_tool_gate_cannot_see():
    """核心(红队 B1):工具便宜、generate 烧钱时,只挂工具闸的实现永远拦不住。
    这里【不经过 _make_executor】(直接给 execute),所以只有挂点② 在起作用。"""
    class _Conv:
        last_thoughts = "thinking"
        def __init__(self): self.sent = []
        def send(self, msg):
            self.sent.append(msg)
            _spend(0.30)                    # 每轮 generate 烧 $0.30(历史越长越贵)
            return [loop_driver.Call("sql_query", {"sql": "select 1"}, [])], None
    g = TreeGuard(cost_cap=0.50, call_estimate=0.0)
    conv = _Conv()
    r = loop_driver.run_loop("q", conv, _cheap_exec, max_steps=20, guard=g)
    assert g.tripped == "budget"
    assert len(conv.sent) <= 2 + loop_driver._TRIP_GRACE_STEPS + 1   # 早停,没烧满 20 步
    assert any("成本护栏" in str(m) for m in conv.sent)               # 收口指令喂进去了


def test_soft_landing_success_path_delivers_partial_answer():
    """P0-3 的主打设计路径(review 确认此前零覆盖):触闸 → 信封喂回 → 模型宽限内改口
    用已有证据收口 → 答案照常交付(terminated=text)+ 【未核查】标注 + 现算的透明尾注。"""
    class _Conv:
        last_thoughts = ""
        def __init__(self): self.sent = []
        def send(self, msg):
            self.sent.append(msg)
            _spend(0.40)
            if "成本护栏" in str(msg):                   # 信封到了 → 听话收口
                return [], "部分结论:A 已核实;B【未核查】"
            return [loop_driver.Call("sql_query", {"sql": "s"}, [])], None
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    conv = _Conv()
    r = loop_driver.run_loop("q", conv, _cheap_exec, max_steps=10, guard=g)
    assert r.terminated == "text"                        # 软收口 = 正常交付,不是异常
    assert r.answer.startswith("部分结论")
    assert "【未核查】" in r.answer
    assert "本次触发成本护栏" in r.answer and "已在上文标注" in r.answer
    assert len(conv.sent) == 2                           # 触闸后一轮就收口,没耗宽限


def test_note_appended_even_when_budget_blown_on_final_generate():
    """自测逮出的真 bug:预算正好在最后一次 generate 上烧穿时,前置检查还没看到这笔钱
    → 收口处必须补记一次账(reconcile),否则用户看不到"钱为何停"。
    此时模型从没见过信封 → 不得声称"已在上文标注"(review 确认的假陈述)。"""
    class _Conv:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.40)
            return [], "我的结论是 X"
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Conv(), _cheap_exec, max_steps=5, guard=g)
    assert r.answer.startswith("我的结论是 X")        # 答案照常交付(不 kill)
    assert "触发成本护栏" in r.answer                 # 且对用户透明
    assert "已在上文标注" not in r.answer             # 信封没喂过,不许撒谎


def test_wall_trip_through_run_loop_shows_seconds():
    """review 确认漏测:wall 闸从未穿过 run_loop。生产推荐姿势正是 wall-only。"""
    class _Conv:
        last_thoughts = ""
        def send(self, msg):
            import time as _t
            _t.sleep(0.03)
            if "成本护栏" in str(msg):
                return [], "已就绪的部分:X;其余【未核查】"
            return [loop_driver.Call("sql_query", {"sql": "s"}, [])], None
    g = TreeGuard(cost_cap=0, wall_cap_s=0.02)
    r = loop_driver.run_loop("q", _Conv(), _cheap_exec, max_steps=10, guard=g)
    assert g.tripped == "wall" and r.terminated == "text"
    assert "墙钟" in r.answer and "上限 $0.00" not in r.answer


def test_critic_skipped_after_trip():
    """review 确认:critic 的"继续做到位"与信封"不要再调工具"打架,能把已产出的部分答案
    逼进硬终止(钱全浪费)。触闸态下部分答案+弃权标注就是合格交付 → critic 必须让路。"""
    critic_calls = []

    def critic(q, a):
        critic_calls.append(1)
        return False, "还缺 X,请继续查"

    class _Conv:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.40)                                 # 最后一次 generate 烧穿 cap
            return [], "部分结论【未核查】"
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Conv(), _cheap_exec, max_steps=5, guard=g, critic=critic)
    assert critic_calls == []                            # reconcile 先记账 → critic 被跳过
    assert r.terminated == "text" and "触发成本护栏" in r.answer


def test_trip_grace_is_bounded():
    """触闸后模型仍硬调工具 → 宽限用尽强制终止(terminated=tree_guard),不烧光剩余步数。"""
    class _Stubborn:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.40)
            return [loop_driver.Call("sql_query", {"sql": "select 1"}, [])], None
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Stubborn(), _cheap_exec, max_steps=20, guard=g)
    assert r.terminated == "tree_guard"
    assert r.steps <= 2 + loop_driver._TRIP_GRACE_STEPS


def test_guard_none_keeps_loop_behaviour_identical():
    """guard=None(默认)→ 循环行为与升级前完全一致(Part 0 不变量①)。"""
    class _Conv:
        last_thoughts = ""
        def send(self, msg):
            return [], "answer"
    r = loop_driver.run_loop("q", _Conv(), lambda *a, **k: None, max_steps=3)
    assert r.answer == "answer" and r.terminated == "text" and r.steps == 0


# ── review 逮出的真 bug,各钉一颗钉子 ──
def test_envelope_merges_into_tool_results_not_replaces_them():
    """bug①:早先触闸时 msg 被整句顶掉 —— 上一步工具结果全丢(删掉了"用已有证据收口"
    里的证据),且 function_call 轮缺配对的 function_response → Gemini 400 硬崩。"""
    prev = [("sql_query", {"result_id": "c0_0", "preview": [{"n": "42"}], "n": 1})]
    out = loop_driver._attach_envelope(prev, "[系统·成本护栏] 停")
    assert isinstance(out, list) and len(out) == 1
    name, result = out[0]
    assert name == "sql_query" and result["result_id"] == "c0_0"   # 证据还在,配对还在
    assert result["_system_notice"] == "[系统·成本护栏] 停"

    assert loop_driver._attach_envelope("你好", "E").startswith("你好")
    assert "E" in loop_driver._attach_envelope("你好", "E")


def test_hard_stop_answer_is_never_none():
    """bug②:硬终止若回 answer=None,orchestrator 会当"瞬时波动"提示用户【再发一次】——
    把熔断刚省下的钱请回来重烧。必须交一句诚实的说明 + 现算的花费披露。"""
    class _Stubborn:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.40)
            return [loop_driver.Call("sql_query", {"sql": "select 1"}, [])], None
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Stubborn(), _cheap_exec, max_steps=20, guard=g)
    assert r.terminated == "tree_guard"
    assert r.answer and "成本护栏" in r.answer          # 不是 None、不是空串
    assert "本次触发成本护栏" in r.answer               # final_note 也带上(花了多少、上限多少)
    assert "已在上文标注" not in r.answer               # 占位答案没有标注可言,不许撒谎


def test_soft_envelope_survives_preview_uncut():
    """bug③(构造器单元):_preview 把字段截到 80 字符 → 收口指令被腰斩。
    真实调用点的钉子在 test_tool_gate_blocks_and_returns_soft_envelope 的 preview 断言。"""
    long_note = "x" * 79 + "【未核查】必须出现在被大脑读到的部分"
    res = loop_driver._soft_note(long_note)
    assert res.ok and res.value["answer"] == long_note
    assert "未核查" in str(res.preview)                 # preview(真正进 prompt 的)没截


def test_tool_gate_trip_first_still_yields_note_on_hard_stop(monkeypatch):
    """bug④(review 变异验证后重写:旧版预先 _spend 让 generate 闸先触,测的是错误路径,
    删掉工具闸触发后的宽限文案逻辑 20 测全绿):钱烧在 send 内、generate 闸(预估 $0.05)
    放行后才越线 → 【工具闸】先触;模型硬调工具到宽限用尽 → 硬终止答案必须带现算的护栏
    说明。走真实工具闸(_make_executor),工具绝不真执行。"""
    def _never(*a, **k):
        raise AssertionError("触闸后工具不该真执行")
    monkeypatch.setattr(loop_driver, "execute_node", _never)

    class _Stubborn:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.20)                # 钱在 generate 里烧穿(闸前预估时还看不见)
            return [loop_driver.Call("sql_query", {"sql": "select 1"}, [])], None
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    ex = _executor(g)
    r = loop_driver.run_loop("q", _Stubborn(), ex, max_steps=20, guard=g)
    assert g.tripped == "budget"
    assert r.terminated == "tree_guard"
    assert "本次触发成本护栏" in r.answer               # 硬终止也有现算披露
    assert "额外发生 $0.4" in r.answer                  # 宽限期烧的 2 轮 generate 进了账


# ── 接线(review 变异验证:删掉 run_loop 的 guard=guard / 打错 getattr 名,全套件全绿)──
def test_run_query_loop_wires_same_guard_to_both_hooks(monkeypatch):
    """一次请求 = 一棵树 = 一本账:run_query_loop 必须把【同一个】guard 同时喂给
    工具闸(execute.tree_guard)和每步 generate 闸(run_loop 的 guard=)。"""
    captured = {}

    def fake_run_loop(nl, conv, execute, **kw):
        captured["guard"] = kw.get("guard")
        captured["execute"] = execute
        return loop_driver.LoopResult("ok", 0, "text", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    loop_driver.run_query_loop("q", schema={}, replay_context=None, sandbox=None,
                               trace=T.Trace(quiet=True), session_id=None)
    assert isinstance(captured["guard"], TreeGuard)
    assert getattr(captured["execute"], "tree_guard", None) is captured["guard"]


def test_subagent_run_one_passes_parent_guard_to_its_loop(monkeypatch):
    """子 agent 的 mini-loop 必须拿到父 execute 上的同一个 guard(getattr 名打错即挂)。"""
    from pipeline import subagents
    captured = {}

    def fake_run_loop(instr, conv, ex, **kw):
        captured["guard"] = kw.get("guard")
        return loop_driver.LoopResult("done", 1, "text", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    g = TreeGuard(cost_cap=0.5)

    def ex(*a, **k):
        return None
    ex.tree_guard = g
    subagents._run_one({"instruction": "查一下", "video_ids": [], "tools": ["sql_query"]},
                       execute=ex, sandbox=None, trace=None, schema=None,
                       session_id=None, owner="t", model="m", max_steps=3)
    assert captured["guard"] is g


# ── 子 agent 与护栏话术的边界(review 确认)──
def test_subagent_tree_guard_termination_is_not_a_result(monkeypatch):
    """子 agent 被硬终止:①面向最终用户的占位话术不得当"子任务结论"回流主脑综合;
    ②span 不得记 ok(静默失败正是因由码制度要消灭的)。"""
    from pipeline import subagents

    def fake_run_loop(instr, conv, ex, **kw):
        return loop_driver.LoopResult(loop_driver._GUARD_STOP_ANSWER + "\n\n(系统) …",
                                      3, "tree_guard", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    tr = T.Trace(quiet=True)
    out = subagents._run_one({"instruction": "查一下", "video_ids": [], "tools": ["sql_query"]},
                             execute=None, sandbox=None, trace=tr, schema=None,
                             session_id=None, owner="t", model="m", max_steps=3)
    assert "无结论" in out["output"]
    assert "调高" not in out["output"]                   # 用户话术没回流
    spans = tr.as_list()
    assert spans and spans[0]["status"] == "softfail"
    assert spans[0]["cause"] == "EXEC_NOT_CONVERGED"


def test_subagent_strips_guard_note_from_soft_landing_answer(monkeypatch):
    """子 agent 软收口:剥掉 (系统) 记账行 —— 触闸时刻的旧账回流只会跟主回答的现算账
    互相矛盾;换成中性说明,已核实的部分结论保留。"""
    from pipeline import subagents
    ans = ("结论:A 已核实;B【未核查】"
           + GUARD_NOTE_PREFIX + "(budget):累计 $0.5000 / 上限 $0.40,触闸后额外发生 $0.1000;"
           "未核查的部分已在上文标注。")

    def fake_run_loop(instr, conv, ex, **kw):
        return loop_driver.LoopResult(ans, 2, "text", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    out = subagents._run_one({"instruction": "查一下", "video_ids": [], "tools": ["sql_query"]},
                             execute=None, sandbox=None, trace=None, schema=None,
                             session_id=None, owner="t", model="m", max_steps=3)
    assert out["output"].startswith("结论:A 已核实")
    assert "本次触发成本护栏" not in out["output"]       # 记账行剥掉了
    assert "提前收口" in out["output"]                   # 换成中性说明


# ── round2 钉子(第二轮 review:2 条变异存活的假保护 + 5 条新缺陷,各钉一颗)──
def test_generate_gate_settles_reservation_each_step():
    """round2-HIGH(变异存活):挂点② 的 settle 此前零钉子 —— 删掉后健康请求每步泄漏
    $0.05 幽灵预留,约 (cap/est) 步后闸门在几乎没花钱的请求上误触。est>0 跑多步:
    不误触、循环后 pending 归零。"""
    class _Conv:
        last_thoughts = ""
        def __init__(self): self.n = 0
        def send(self, msg):
            self.n += 1
            if self.n >= 6:
                return [], "done"
            return [loop_driver.Call("sql_query", {"sql": f"s{self.n}"}, [])], None
    g = TreeGuard(cost_cap=0.20, call_estimate=0.05)     # 不释放的话第 5 次 admit 必误触
    r = loop_driver.run_loop("q", _Conv(), _cheap_exec, max_steps=10, guard=g)
    assert g.tripped is None and r.answer == "done"      # 健康请求不被幽灵预留误杀
    assert g._pending == pytest.approx(0.0)              # 每步都释放干净


def test_summarize_safe_under_concurrent_new_model_insertion():
    """round2(变异存活):summarize 锁内拷贝此前零钉子。A 线程狂插【新】模型键,
    B 线程狂 summarize —— 旧无锁写法会 RuntimeError(dict changed size during iteration),
    再被 treeguard.spent() 吞成闸门盲区。"""
    from contextvars import copy_context

    def _raw_spend(model):
        class _UM:
            prompt_token_count = 10
            candidates_token_count = 0
            total_token_count = 10
            cached_content_token_count = 0

        class _R:
            usage_metadata = _UM()
        usage.add_usage(_R(), model)

    errors = []
    done = threading.Event()
    start = threading.Barrier(2)                         # 两线程同时起跑,保证真重叠
    def writer():
        start.wait()
        for i in range(4000):                            # 每次都是新键 → 持续触发 dict 扩容
            _raw_spend(f"race-model-{i}")
        done.set()

    def reader():
        start.wait()
        try:
            while not done.is_set():
                usage.summarize()
        except Exception as e:                           # 旧写法在这里炸 RuntimeError
            errors.append(e)
            done.set()
    tw = threading.Thread(target=copy_context().run, args=(writer,))
    tr_ = threading.Thread(target=copy_context().run, args=(reader,))
    tw.start(); tr_.start()
    tw.join(); tr_.join()
    assert errors == []


def test_trip_near_max_steps_still_reports_guard_not_retry_bait():
    """round2(实测复现):触闸落在最后几步内 → 宽限没用尽 for 就耗完 → 旧代码从 max_steps
    出口漏出 answer=None,orchestrator 劝用户"再发一次"重烧,披露全丢。"""
    class _Stubborn:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.40)
            return [loop_driver.Call("sql_query", {"sql": "select 1"}, [])], None
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Stubborn(), _cheap_exec, max_steps=3, guard=g)
    assert r.terminated == "tree_guard"                  # 不是 "max_steps"
    assert r.answer and "本次触发成本护栏" in r.answer


def test_trip_then_repeat_exit_still_reports_guard():
    """round2(实测复现):触闸前同签名已失败两次,宽限期内模型再发同一调用 → 旧代码走
    repeat 出口回 answer=None,同样变成"请再发一次"。"""
    def _fail_exec(*a, **k):
        return loop_driver.ExecResult(ok=False, stderr="boom")

    class _Conv:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.20)
            return [loop_driver.Call("sql_query", {"sql": "same"}, [])], None
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Conv(), _fail_exec, max_steps=10, guard=g)
    assert r.terminated == "tree_guard"                  # 不是 "repeat"
    assert r.answer and "本次触发成本护栏" in r.answer


def test_trip_with_empty_final_generation_goes_hard_stop_not_bare_note():
    """round2(实测复现):触闸 + 最终生成为空 → 旧代码交付"只有一行记账"的答案,
    还声称"已在上文标注"(上文为空),且 answer 非空绕过 orchestrator 空答网。"""
    class _Conv:
        last_thoughts = ""
        def __init__(self): self.n = 0
        def send(self, msg):
            self.n += 1
            _spend(0.40)
            if self.n == 1:
                return [loop_driver.Call("sql_query", {"sql": "s"}, [])], None
            return [], ""                                # 触闸后连着空生成
    g = TreeGuard(cost_cap=0.30, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Conv(), _cheap_exec, max_steps=10, guard=g)
    assert r.terminated == "tree_guard"
    assert "没有可交付的结论" in r.answer                # 诚实说明,不是裸记账行
    assert "已在上文标注" not in r.answer


def test_critic_cost_after_reconcile_still_disclosed():
    """round2(实测复现):critic 自己的 LLM 调用在闸外落账;satisfied 终路若不再 reconcile,
    最后那笔 critic 钱可以无披露越线(tripped=None → final_note 空串)。"""
    def critic(q, a):
        _spend(0.10)                                     # critic 调用的钱(闸外)
        return True, ""

    class _Conv:
        last_thoughts = ""
        def send(self, msg):
            _spend(0.75)
            return [], "结论"
    g = TreeGuard(cost_cap=0.80, call_estimate=0.0)
    r = loop_driver.run_loop("q", _Conv(), _cheap_exec, max_steps=5, guard=g, critic=critic)
    assert g.tripped == "budget"                         # $0.85 > $0.80,critic 后补记账逮住
    assert "本次触发成本护栏" in r.answer


def test_grace_step_feeds_envelope_when_trip_happened_elsewhere():
    """round2(实测复现):触闸发生在子 agent/工具执行里(本 conversation 没见过信封)→
    主 loop 第一宽限步必须补喂收口指令,否则主脑不知道要标【未核查】,
    final_note 的"已标注"声称对主答案就是假的。"""
    g = TreeGuard(cost_cap=0.30, call_estimate=0.05)
    _spend(0.40)
    assert g.admit()                                     # 模拟:触闸发生在别处(信封喂给了那边)

    class _Conv:
        last_thoughts = ""
        def __init__(self): self.sent = []
        def send(self, msg):
            self.sent.append(msg)
            return [], "综合结论;X【未核查】"
    conv = _Conv()
    r = loop_driver.run_loop("q", conv, _cheap_exec, max_steps=5, guard=g)
    assert "成本护栏" in str(conv.sent[0])               # 第一步就把收口指令带进主对话
    assert "本次触发成本护栏" in r.answer


def test_subagent_strip_does_not_fabricate_mark_claim(monkeypatch):
    """round2(实测复现):子 agent 从没收到过信封(收口 reconcile 才首次触闸)时,
    其 note 是无声称版 —— 剥离替换文案不得凭空写"未核查处已标注"(假陈述换路复活)。"""
    from pipeline import subagents
    ans = ("结论 B(未经核实的推断)"
           + GUARD_NOTE_PREFIX + "(budget):累计 $0.5000 / 上限 $0.40,触闸后额外发生 $0.0000。")

    def fake_run_loop(instr, conv, ex, **kw):
        return loop_driver.LoopResult(ans, 2, "text", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    out = subagents._run_one({"instruction": "查一下", "video_ids": [], "tools": ["sql_query"]},
                             execute=None, sandbox=None, trace=None, schema=None,
                             session_id=None, owner="t", model="m", max_steps=3)
    assert out["output"].startswith("结论 B")
    assert "已标注" not in out["output"]                 # 原 note 没声称 → 替换文案也不许声称
    assert "未经完整核查" in out["output"]               # 反而要提醒主脑谨慎


def test_analyze_reservation_scales_with_model_tier(monkeypatch):
    """Phase 1 主跑实测的误触:analyze 一律按 pro 悲观估价($0.30)预留,而实跑是 flash
    (单次约 $0.015)—— 一步内并行 3 个 analyze 就把 $0.80 的闸在实花 $0.13 时顶掉,
    而且专挑"看视频多"的路径罚。预留必须跟着【实际生效的档位】走。"""
    from perception import analyze_video_contextual as AV
    monkeypatch.setattr(config, "TREE_CALL_ESTIMATE_USD", 0.05)
    monkeypatch.setattr(config, "TREE_ANALYZE_ESTIMATE_USD", 0.30)
    tok = AV.MODEL_OVERRIDE.set("gemini-2.5-flash")
    try:
        assert loop_driver._is_pro_analyze() is False
    finally:
        AV.MODEL_OVERRIDE.reset(tok)
    tok = AV.MODEL_OVERRIDE.set("gemini-2.5-pro")
    try:
        assert loop_driver._is_pro_analyze() is True
    finally:
        AV.MODEL_OVERRIDE.reset(tok)

    # flash 档:一步内 3 个并行 analyze 不该顶掉 $0.80 的闸(实花远低于预留)
    seen = []
    monkeypatch.setattr(loop_driver, "execute_node",
                        lambda node, *a, **k: seen.append(node.tool) or _NRok())
    g = TreeGuard(cost_cap=0.80, call_estimate=0.05)
    _spend(0.13)
    ex = _executor(g)
    tok = AV.MODEL_OVERRIDE.set("gemini-2.5-flash")
    try:
        for i in range(3):
            ex(f"c{i}", "analyze_video", {"video_id": f"v{i}"}, {}, [])
    finally:
        AV.MODEL_OVERRIDE.reset(tok)
    assert g.tripped is None, "flash 档三个并行 analyze 不该触闸"
    assert len(seen) == 3


class _NRok:
    ok, value, stderr, code, artifact, videos, table, stat, cache_hit = \
        True, {"answer": "看了", "enough": "yes"}, "", None, {}, [], {}, {}, False
    attempts = 1


# ── A7+ B 方案下沉:admit/settle 进 analyze 的重试循环 ────────────────────────
class _FakeTrace:
    class _S:
        def ok(self, **k): pass
        def fail(self, **k): pass
        def soft(self, *a, **k): pass
        def bill(self, **k): pass
    def step(self, *a, **k): return self._S()


def _analyze_node(vid="v1"):
    from pipeline.dag_schema import Node
    return Node(id="c0", tool="analyze_video", inputs={"video_id": vid, "question": "q"})


def _count_admits(g, monkeypatch):
    """把 guard.admit/settle 包一层计数(仍走真实实现)。返回计数字典。"""
    n = {"admit": 0, "settle": 0}
    real_admit, real_settle = g.admit, g.settle

    def counting_admit(**kw):
        n["admit"] += 1
        return real_admit(**kw)

    def counting_settle(*a, **kw):
        n["settle"] += 1
        return real_settle(*a, **kw)
    monkeypatch.setattr(g, "admit", counting_admit)
    monkeypatch.setattr(g, "settle", counting_settle)
    return n


def _inject_failing_generate(monkeypatch):
    """注入一个必失败的 generate,并统计【真实 LLM 调用次数】。"""
    import perception.analyze_video_contextual as AV
    from pipeline import analyze_cache, mcp_client as mc
    analyze_cache.clear()
    monkeypatch.setattr(mc, "query_db", lambda sql: [{"gcs_uri": "gs://b/v.mp4"}])
    calls = {"n": 0}

    def boom(gcs_uri, prompt, time_range=None):
        calls["n"] += 1
        raise RuntimeError("API down")
    monkeypatch.setattr(AV, "_gemini_generate", boom)
    return calls


def test_admit_count_equals_real_llm_calls_in_analyze_retries(monkeypatch):
    """A7+ 验收:注入必失败的 generate → 断言 TreeGuard.admit 的【调用笔数】==【真实 LLM
    调用次数】。下沉之前是 1 vs 3(admit 每工具调用记一笔,而 3 次重试藏在 analyze 函数
    内部、对 guard 完全不可见)—— 记账错 3 倍。

    刻意【不】断言 usage.summarize()["cost_usd"]:那个今天跑就是绿的(每次 generate 都
    add_usage 事后落账),断言对象写错这条就白做。
    """
    import perception.analyze_video_contextual as AV
    from pipeline import node_executor as ne
    calls = _inject_failing_generate(monkeypatch)
    g = TreeGuard(cost_cap=10.0, call_estimate=0.05)      # cap 给足,不让它中途触闸
    n = _count_admits(g, monkeypatch)

    res = ne.execute_node(_analyze_node(), {}, None, _FakeTrace(), guard=g)

    assert calls["n"] == AV.RETRY_LIMIT + 1               # 真实发了 3 次 LLM 调用
    assert n["admit"] == calls["n"], f"记账 {n['admit']} 笔 vs 真实 {calls['n']} 次调用"
    assert n["settle"] == calls["n"]                      # 配对释放
    assert g._pending == 0.0                              # 在飞预留没泄漏
    assert not res.ok and res.attempts == calls["n"]      # A4:失败就是失败,attempts 说真话


def test_no_guard_param_keeps_old_behaviour(monkeypatch):
    """不传 guard(= 今天 loop_driver 的调用形状)时一笔都不记,行为与下沉前逐字节一致。"""
    import perception.analyze_video_contextual as AV
    from pipeline import node_executor as ne
    calls = _inject_failing_generate(monkeypatch)
    g = TreeGuard(cost_cap=10.0, call_estimate=0.05)
    n = _count_admits(g, monkeypatch)

    res = ne.execute_node(_analyze_node(), {}, None, _FakeTrace())   # 不传 guard

    assert calls["n"] == AV.RETRY_LIMIT + 1
    assert n["admit"] == 0 and n["settle"] == 0
    assert not res.ok


def test_analyze_estimate_follows_model_tier_not_retry_count(monkeypatch):
    """预留口径不许因为下沉而变大:仍按【实际生效档位】二选一,且【不乘 RETRY_LIMIT 系数】。
    一律按 pro 估价会让一步内并行 3 个 analyze 在实花 $0.13 时顶掉 $0.80 的闸(20 倍高估)。"""
    import perception.analyze_video_contextual as AV
    from pipeline import node_executor as ne
    monkeypatch.setattr(config, "TREE_CALL_ESTIMATE_USD", 0.05)
    monkeypatch.setattr(config, "TREE_ANALYZE_ESTIMATE_USD", 0.30)
    assert ne._analyze_estimate("gemini-2.5-flash") == 0.05
    assert ne._analyze_estimate("gemini-2.5-pro") == 0.30
    # 关键:单次预留 == 单次估价,没有被 RETRY_LIMIT+1 放大
    assert ne._analyze_estimate("gemini-2.5-pro") == config.TREE_ANALYZE_ESTIMATE_USD

    calls = _inject_failing_generate(monkeypatch)
    seen = []
    g = TreeGuard(cost_cap=10.0, call_estimate=0.05)
    real_admit = g.admit
    monkeypatch.setattr(g, "admit",
                        lambda **kw: (seen.append(kw.get("estimate")), real_admit(**kw))[1])
    tok = AV.MODEL_OVERRIDE.set("gemini-2.5-pro")
    try:
        ne.execute_node(_analyze_node(), {}, None, _FakeTrace(), guard=g)
    finally:
        AV.MODEL_OVERRIDE.reset(tok)
    assert calls["n"] == AV.RETRY_LIMIT + 1
    assert seen == [0.30] * calls["n"]                    # 每笔都是【单次】pro 估价


def test_guard_block_stops_analyze_without_spending(monkeypatch):
    """已超支时 analyze 一次 LLM 都不发,并把护栏信封原文交回(大脑要读得到收口指令)。"""
    import perception.analyze_video_contextual as AV
    from pipeline import node_executor as ne
    calls = _inject_failing_generate(monkeypatch)
    g = TreeGuard(cost_cap=0.10, call_estimate=0.05)
    _spend(0.50)                                          # 已经超支 → 第一次 admit 必拦

    res = ne.execute_node(_analyze_node(), {}, None, _FakeTrace(), guard=g)

    assert calls["n"] == 0                                # 一分钱没花
    assert not res.ok and res.error_code == AV.ERROR_GUARD_BLOCKED
    assert res.attempts == 0
    assert "成本护栏" in res.stderr and res.stderr.index("成本护栏") < 20   # 信封在最前,别被截尾切掉


# ── A4:失败不写成功缓存、不进语义索引 ──────────────────────────────────
def test_failed_analyze_never_reaches_cache_or_semantic_index(monkeypatch):
    """假成功最毒的一处:失败信封曾被 _index_analyze_result 写进 content_embeddings ——
    等于把"看不清"当证据永久存进生产库,以后每次检索都召回一条假证据。"""
    import perception.analyze_video_contextual as AV
    from pipeline import analyze_cache, node_executor as ne
    calls = _inject_failing_generate(monkeypatch)
    indexed = []
    monkeypatch.setattr(ne, "_index_analyze_result",
                        lambda vid, dump, key: indexed.append(vid))

    res = ne.execute_node(_analyze_node(), {}, None, _FakeTrace())

    assert calls["n"] == AV.RETRY_LIMIT + 1
    assert indexed == [], "失败结果绝不能进语义索引"
    assert analyze_cache.size() == 0, "失败结果绝不能写成功缓存"
    assert not res.ok and res.error_code == AV.ERROR_ANALYZE_FAILED
    assert "没有被分析过" in res.stderr          # 下游不许再当成"看过了、结论是看不清"


# ── A7+ 启动校验:配置别把 analyze 配死 ─────────────────────────────────
def test_startup_check_rejects_cap_below_one_analyze():
    """cap < 单次预留 → pro 档 analyze 一次都进不来,且第一次拦下就触闸整棵树 → 拒绝启动。"""
    with pytest.raises(ValueError) as e:
        config._validate_tree_guard_budget(0.20, 0.30, 3)
    assert "MAX_TREE_COST_USD" in str(e.value) and "0.9" in str(e.value)


def test_startup_check_warns_when_parallel_burst_would_trip():
    """够单次但不够满并行 → 不拒绝启动,但必须告警(以前是【静默】拦死,没有任何提示)。"""
    w = config._validate_tree_guard_budget(0.80, 0.30, 3)
    assert w and "0.9" in w and "MAX_ANALYZE_PARALLEL" in w


def test_startup_check_silent_when_satisfied_or_disabled():
    assert config._validate_tree_guard_budget(0.90, 0.30, 3) == ""
    assert config._validate_tree_guard_budget(0, 0.30, 3) == ""      # 0 = 熔断关,不受约束
