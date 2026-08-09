"""P0-6:裸 depth-2(USE_DEPTH2,默认关)。

只做深度穿透,不带 DAG/蒸馏/分层(红队 C2:实验测单变量)。四条验收(任务书):
① depth-2 子 agent 的白名单不含 spawn_agents(拿不到第三层);
② 未成功执行满 2 次工具就 spawn → 教育信封(红队 D1:代码计数,不信文本);
③ 全树节点数在并发下不超 MAX_TREE_NODES;
④ 关开关时全部路径与现状逐字节一致(单层多次 spawn 照旧允许、无节点账)。
离线、零 API。
"""
import threading
from contextvars import copy_context

import pytest

from pipeline import config, loop_driver, subagents


def _at(depth, fn, *a, ok_tools=None, **k):
    """在指定树深度(+可选枝计数)的独立上下文里跑 fn —— 模拟"某枝里发起 spawn"。"""
    def run():
        subagents._TREE_DEPTH.set(depth)
        if ok_tools is not None:
            subagents._BRANCH.set({"ok_tools": ok_tools})
        return fn(*a, **k)
    return copy_context().run(run)


def _root_ex():
    """带节点账的根闭包替身(生产里由 _make_executor 挂)。"""
    def ex(*a, **k):
        return None
    ex.tree_guard = None
    ex.tree_nodes = {"nodes": 1, "lock": threading.Lock()}
    return ex


# ── 白名单(验收①)──
def test_allowed_no_spawn_anywhere_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "USE_DEPTH2", False)
    assert "spawn_agents" not in subagents._allowed(0)
    assert "spawn_agents" not in subagents._allowed(1)
    assert subagents._allowed(0) == subagents._SUBAGENT_ALLOWED   # 与升级前同一张表


def test_allowed_spawn_only_for_depth1_agents(monkeypatch):
    """开了也只有主 loop(depth0)拆出的 depth-1 拿得到;depth-1 拆出的 depth-2 永远拿不到。"""
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    assert "spawn_agents" in subagents._allowed(0)
    assert "spawn_agents" not in subagents._allowed(1)
    assert "spawn_agents" not in subagents._allowed(2)


def test_clean_tasks_strips_spawn_for_second_layer(monkeypatch):
    """depth-1 spawner 请求给孩子 spawn_agents → 被剥,退回默认子集(没有第三层)。"""
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    t = [{"instruction": "deep", "tools": ["spawn_agents"]}]
    cleaned0, _ = subagents._clean_tasks(t, 6, depth=0)
    assert cleaned0[0]["tools"] == ["spawn_agents"]              # 主 loop 拆的可以带
    cleaned1, _ = subagents._clean_tasks(t, 6, depth=1)
    assert cleaned1[0]["tools"] == list(subagents._SUBAGENT_DEFAULT)   # 二层剥掉


# ── 深度硬闸 ──
def _fan(depth, ok_tools, execute, tasks=None, monkeypatch=None):
    return _at(depth, subagents.run_fanout,
               tasks or [{"instruction": "t"}],
               ok_tools=ok_tools, sandbox=None, trace=None, schema=None, execute=execute)


def test_depth2_hard_stop_is_atomic(monkeypatch):
    """depth≥2 发起 spawn → 直接 ValueError(原子强制),连教育机会都不给。"""
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    with pytest.raises(ValueError, match="最大深度"):
        _fan(2, 99, _root_ex())


def test_depth1_spawn_blocked_when_flag_off(monkeypatch):
    """双保险(白名单已挡,这里兜直连/回放):关着时 depth-1 spawn 必拒。"""
    monkeypatch.setattr(config, "USE_DEPTH2", False)
    with pytest.raises(ValueError, match="USE_DEPTH2"):
        _fan(1, 99, _root_ex())


def test_try_yourself_gate_needs_two_real_tool_successes(monkeypatch):
    """验收②(红队 D1):没干满 2 次活就想拆 → 教育信封;干满了 → 放行。"""
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    monkeypatch.setattr(subagents, "_run_one",
                        lambda task, **kw: {"instruction": task["instruction"], "output": "ok"})
    for n in (0, 1):
        with pytest.raises(ValueError, match="先自己动手"):
            _fan(1, n, _root_ex())
    out = _fan(1, 2, _root_ex())                                 # 2 次 → 放行
    assert out[0]["output"] == "ok"


def test_l2_fanout_capped_at_three(monkeypatch):
    """depth-1 再拆的扇出顶 = SUBAGENT_L2_FANOUT(默认 3),不吃主层的 6。"""
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    ran = []
    monkeypatch.setattr(subagents, "_run_one",
                        lambda task, **kw: ran.append(1) or {"instruction": "i", "output": "ok"})
    tasks = [{"instruction": f"t{i}"} for i in range(6)]
    out = _fan(1, 2, _root_ex(), tasks=tasks)
    assert len(ran) == 3
    assert "只跑了前 3 个" in out[-1]["output"]                  # 截断有说明


# ── 全树节点账(验收③)──
def test_tree_node_cap_holds_under_concurrency(monkeypatch):
    """并发 spawn 共享同一本节点账(挂在根闭包上):锁内查-截-记,总数绝不超 13。

    review 变异实测:3 个裸线程无交错压力时,把查-截挪到锁外(经典 TOCTOU)20 连跑照样全绿
    —— 假保护。这里按复现配方加压:Barrier 对齐 4 线程 + setswitchinterval(1e-6) + 多轮,
    变异版实测 ~2%/轮冲破上限(200 轮检出率 ≈98.7%),原版 0 冲破。"""
    import sys
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 6)
    monkeypatch.setattr(subagents, "_run_one",
                        lambda task, **kw: {"instruction": task["instruction"], "output": "ok"})
    old_si = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                                  # 逼出线程交错
    try:
        for _ in range(200):
            ex = _root_ex()
            start = threading.Barrier(4)

            def one_spawn(tag):
                try:
                    start.wait()
                    _fan(0, None, ex,
                         tasks=[{"instruction": f"{tag}-{i}"} for i in range(6)])
                except ValueError:
                    pass                                         # 满了被拒是正常结局
            ths = [threading.Thread(target=one_spawn, args=(f"w{j}",)) for j in range(4)]
            [t.start() for t in ths]
            [t.join() for t in ths]
            assert ex.tree_nodes["nodes"] <= config.MAX_TREE_NODES, \
                f"节点账冲破上限:{ex.tree_nodes['nodes']} > {config.MAX_TREE_NODES}"
    finally:
        sys.setswitchinterval(old_si)


def test_tree_node_cap_third_wave_rejected(monkeypatch):
    """串行三波 6+6+…:第三波 room=0 → 拒绝并教育(不能再拆)。"""
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 6)
    monkeypatch.setattr(subagents, "_run_one",
                        lambda task, **kw: {"instruction": "i", "output": "ok"})
    ex = _root_ex()
    _fan(0, None, ex, tasks=[{"instruction": f"a{i}"} for i in range(6)])   # 7
    _fan(0, None, ex, tasks=[{"instruction": f"b{i}"} for i in range(6)])   # 13
    with pytest.raises(ValueError, match="上限"):
        _fan(0, None, ex, tasks=[{"instruction": "c0"}])


def test_flag_off_no_node_cap_many_waves(monkeypatch):
    """验收④:关着时单层多次 spawn 照旧(升级前就允许),没有节点账、永不拒。"""
    monkeypatch.setattr(config, "USE_DEPTH2", False)
    monkeypatch.setattr(config, "SUBAGENT_MAX_FANOUT", 6)
    n = []
    monkeypatch.setattr(subagents, "_run_one",
                        lambda task, **kw: n.append(1) or {"instruction": "i", "output": "ok"})
    ex = _root_ex()
    for _ in range(4):                                           # 4×6=24 > 13 也没人拦
        _fan(0, None, ex, tasks=[{"instruction": f"t{i}"} for i in range(6)])
    assert len(n) == 24
    assert ex.tree_nodes["nodes"] == 1                           # 账本没动


# ── 深度传播与泄漏 ──
def test_single_task_does_not_leak_depth_into_caller(monkeypatch):
    """单任务路径也要 copy_context:_run_one 的 set 不许漏进调用方(否则下次 spawn 深度全错)。"""
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    monkeypatch.setattr(loop_driver, "run_loop",
                        lambda *a, **k: loop_driver.LoopResult("done", 1, "text", [], {}, 1, [], []))
    assert subagents._TREE_DEPTH.get() == 0
    out = subagents.run_fanout([{"instruction": "solo"}],
                               sandbox=None, trace=None, schema=None, execute=_root_ex())
    assert out[0]["output"] == "done"
    assert subagents._TREE_DEPTH.get() == 0                      # 没被污染


def test_full_chain_two_layers_then_wall(monkeypatch):
    """整链集成(假大脑):主脑拆 L1 → L1 干满 2 次活后拆 L2 → L2 再想拆被最大深度拒。
    同时钉:每层看到的深度、以及 L2 白名单里确实没有 spawn。"""
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    root = _root_ex()
    events = []

    def fake_run_loop(instr, conv, ex, **kw):
        d = subagents._TREE_DEPTH.get()
        b = subagents._BRANCH.get()
        b["ok_tools"] = 2                                        # 模拟:这层先干满 2 次活
        try:
            subagents.run_fanout([{"instruction": f"L{d + 1}"}],
                                 sandbox=None, trace=None, schema=None, execute=root)
            events.append(("spawned", d))
        except ValueError as e:
            events.append(("blocked", d, "最大深度" in str(e)))
        return loop_driver.LoopResult(f"depth{d} done", 1, "text", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)
    subagents.run_fanout([{"instruction": "L1"}],
                         sandbox=None, trace=None, schema=None, execute=root)
    assert ("spawned", 1) in events                              # L1 成功拆出 L2
    assert ("blocked", 2, True) in events                        # L2 被最大深度墙拦下
    assert root.tree_nodes["nodes"] == 3                         # 主脑 + L1 + L2,账实相符


def test_branch_counter_counts_only_real_successes(monkeypatch):
    """计数壳:闸门信封(gate=blocked)与 spawn 自身不算"自己动过手";
    但 analyze 的【真成功】结果哪怕 enough="no"("视频里没有狗"是合法定论)也必须算 ——
    review 确认:按 enough 判会把真干过活的枝误判成没干活,否定结论型子任务永远拆不了。"""
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    monkeypatch.setattr(loop_driver, "make_conversation", lambda *a, **k: object())
    counted = {}

    def fake_run_loop(instr, conv, ex, **kw):
        ex("c1", "sql_query", {}, {}, [])                        # 真成功 → +1
        ex("c2", "analyze_video", {}, {}, [])                    # 闸门信封(_soft_note 形状)→ 不算
        ex("c3", "sql_query", {}, {}, [])                        # 失败(ok=False)→ 不算
        ex("c4", "analyze_video", {}, {}, [])                    # 真成功但 enough=no → 【要算】
        counted["ok_tools"] = subagents._BRANCH.get()["ok_tools"]
        return loop_driver.LoopResult("done", 1, "text", [], {}, 1, [], [])
    monkeypatch.setattr(loop_driver, "run_loop", fake_run_loop)

    def base(cid, name, inputs, upstream, uses):
        if cid == "c1":
            return loop_driver.ExecResult(ok=True, value={"rows": 1}, preview=[], n=1)
        if cid == "c2":
            return loop_driver._soft_note("已达上限,这个【没执行】")   # 生产同款信封
        if cid == "c4":
            return loop_driver.ExecResult(ok=True, value={
                "video_id": "v1", "answer": "视频里没有狗", "enough": "no",
                "confidence": "high"}, preview=[], n=1)
        return loop_driver.ExecResult(ok=False, stderr="boom")
    base.tree_guard = None
    base.tree_nodes = {"nodes": 1, "lock": threading.Lock()}
    subagents.run_fanout([{"instruction": "t"}],
                         sandbox=None, trace=None, schema=None, execute=base)
    assert counted["ok_tools"] == 2                              # c1 + c4


def test_spawn_only_toolset_gets_default_merged(monkeypatch):
    """review 确认的死枝:主脑给 tools=["spawn_agents"] → 该枝唯一工具不计 ok_tools,
    "先自己试"闸构造上永不满足 → 烧满步数零产出。必须并入默认子集让它有活可干。"""
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    seen = {}
    monkeypatch.setattr(loop_driver, "make_conversation",
                        lambda model, decls, system, **k: seen.update(
                            tools={d["name"] for d in decls}) or object())
    monkeypatch.setattr(loop_driver, "run_loop",
                        lambda *a, **k: loop_driver.LoopResult("done", 1, "text", [], {}, 1, [], []))
    subagents.run_fanout([{"instruction": "t", "tools": ["spawn_agents"]}],
                         sandbox=None, trace=None, schema=None, execute=_root_ex())
    assert "spawn_agents" in seen["tools"]                       # 再拆权保留
    assert seen["tools"] & set(subagents._SUBAGENT_DEFAULT)      # 但也有能干活的工具


def test_depth1_system_prompt_mentions_last_resort(monkeypatch):
    """握有 spawn 的 depth-1 子 agent,system 里要有"最后手段/先自己做"的教育。"""
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    monkeypatch.setattr(config, "USE_DEPTH2", True)
    seen = {}
    monkeypatch.setattr(loop_driver, "make_conversation",
                        lambda model, decls, system, **k: seen.update(
                            system=system, tools={d["name"] for d in decls}) or object())
    monkeypatch.setattr(loop_driver, "run_loop",
                        lambda *a, **k: loop_driver.LoopResult("done", 1, "text", [], {}, 1, [], []))
    subagents.run_fanout([{"instruction": "t", "tools": ["sql_query", "spawn_agents"]}],
                         sandbox=None, trace=None, schema=None, execute=_root_ex())
    assert "spawn_agents" in seen["tools"]                       # depth-1 真拿到了
    assert "最后手段" in seen["system"]
