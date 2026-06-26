"""
跨轮 artifact【值复用】的轻量单元测试 —— 纯离线,不依赖 GCP / Redis / DB / 沙箱。
    python -m pipeline.test_artifact_value_reuse

覆盖:
  · 值仓 put/get 往返 + 容量封顶(超 256KB 跳过、不可序列化跳过)+ LRU 条数封顶/delete
  · register_artifact 给可复用 artifact 存值并置 has_value;sql-only/超封顶 → 不存、标志 false
  · plot-final 存的是上游 x/y 数据(非 plot 节点的 {n_points})
  · load_artifact 节点经 execute_node 从(注入的假)值仓取回值;取不到 → 节点失败
  · 跨会话隔离:sessA 存的值 sessB 取不到(键含 session_id)
  · planner_context 暴露 value_cached(实查活仓:值不在场 → False)
  · 重启/跨副本:has_value 持久化但活仓空 → planner_context 报 value_cached=False
  · 端到端:turn1 存可复用值 → turn2 单 load_artifact 节点复用,值相等且未重算
  · Artifact 的值字段经 to_dict/from_dict 往返保真
"""
from __future__ import annotations

import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline import artifact_value_store as AVS
from pipeline.artifact_value_store import (InMemoryArtifactValueStore, make_key)
from pipeline.dag_schema import parse_dag
from pipeline.node_executor import execute_node, NodeResult
from pipeline.session import Session
from pipeline.test_session import _SQL1, _SQL_OLS, _SQL_PLOT, _dag   # 复用 DAG fixture,不重复造


# ── 值仓:put/get 往返 + 封顶 ──────────────────────────────
def test_value_store_put_get_roundtrip():
    st = InMemoryArtifactValueStore()
    val = [{"coef": 1.5, "r2": 0.91}]
    assert st.put("s::a1", val) is True
    assert st.get("s::a1") == val


def test_value_store_get_missing_is_none():
    assert InMemoryArtifactValueStore().get("nope") is None


def test_value_store_skips_oversized():
    st = InMemoryArtifactValueStore(max_bytes=100)   # 故意调小封顶便于触发
    big = [{"x": "y" * 500}]
    assert st.put("s::a1", big) is False             # 超封顶 → 跳过
    assert st.get("s::a1") is None                   # 没存进去


def test_value_store_under_cap_stored():
    st = InMemoryArtifactValueStore(max_bytes=10_000)
    small = [{"x": 1}]
    assert st.put("s::a1", small) is True
    assert st.get("s::a1") == small


def test_value_store_default_str_fallback_serializes_size():
    # 大小估算用 json.dumps(default=str)(与全仓 blob 序列化一致):set 等非原生类型
    # 经 default=str 仍可估算大小 → 不因"奇怪类型"误跳过;存的仍是原对象,get 原样取回。
    st = InMemoryArtifactValueStore()
    assert st.put("s::a1", {1, 2, 3}) is True
    assert st.get("s::a1") == {1, 2, 3}


def test_value_store_lru_evicts_oldest_over_cap():
    # 简易 LRU:超过 max_entries 条 → 淘汰最旧;get 命中会刷新"最近用",免被先淘汰。
    st = InMemoryArtifactValueStore(max_entries=2)
    st.put("k1", [1])
    st.put("k2", [2])
    st.get("k1")                                      # k1 命中 → 变最近;k2 变最旧
    st.put("k3", [3])                                 # 超 2 条 → 淘汰最旧的 k2
    assert st.get("k2") is None
    assert st.get("k1") == [1]
    assert st.get("k3") == [3]


def test_value_store_delete_removes_entry():
    st = InMemoryArtifactValueStore()
    st.put("k1", [1])
    st.delete("k1")
    assert st.get("k1") is None
    st.delete("nope")                                 # 删不存在的键 → 不抛错


def test_make_key_combines_session_and_artifact():
    assert make_key("sess", "a1") == "sess::a1"


# ── register_artifact:可复用类存值 + 置标志 ───────────────
def test_register_stores_value_for_reusable_ols():
    st = InMemoryArtifactValueStore()
    s = Session("sess")
    final_val = {"r_squared": 0.9, "params": {"x": 1.2}}
    art = s.register_artifact(_dag(_SQL_OLS), {"n1": [{"x": 1, "y": 2}], "n2": final_val},
                              "regress y on x", "analyze", value_store=st)
    assert art.has_value is True
    assert art.value_key == make_key("sess", art.id)
    assert st.get(art.value_key) == final_val        # 存的是最终节点(n2/ols)的真实值


def test_register_skips_value_for_sql_only():
    st = InMemoryArtifactValueStore()
    s = Session("sess")
    art = s.register_artifact(_dag(_SQL1), {"n1": [{"id": 1, "predicate": "ski"}]},
                              "find ski", "retrieve", value_store=st)
    assert art.has_value is False                    # 纯 sql_query → 重算,不存值
    assert art.value_key is None
    assert st.get(make_key("sess", art.id)) is None


def test_register_no_store_means_no_value():
    s = Session("sess")
    art = s.register_artifact(_dag(_SQL_OLS), {"n1": [{"x": 1, "y": 2}], "n2": {"r_squared": 0.9}},
                              "regress", "analyze")   # 不传 value_store → 行为同从前
    assert art.has_value is False and art.value_key is None


def test_register_oversized_value_flag_false():
    st = InMemoryArtifactValueStore(max_bytes=50)     # 极小封顶,逼 ols 结果超限
    s = Session("sess")
    huge = {"params": {f"x{i}": i for i in range(200)}}
    art = s.register_artifact(_dag(_SQL_OLS), {"n1": [{"x": 1, "y": 2}], "n2": huge},
                              "regress", "analyze", value_store=st)
    assert art.has_value is False                    # 超封顶 → put 返回 False → 不置标志
    assert art.value_key is None
    assert st.get(make_key("sess", art.id)) is None


def test_register_plot_final_stores_upstream_xy_not_n_points():
    # plot-final:final 节点(plot)的 value 只有 {n_points};存进值仓的必须是【上游 x/y 数据】
    # (preview_node 所依据的同一节点),否则下一轮 load_artifact 拿到 {n_points} 无法 re-plot。
    # 与 test_session.test_plot_final_uses_dag_and_upstream_preview 对称(那条验 preview)。
    st = InMemoryArtifactValueStore()
    s = Session("sess")
    rows = [{"start": 1, "conf": 0.9}, {"start": 2, "conf": 0.8}]
    art = s.register_artifact(_dag(_SQL_PLOT), {"n1": rows, "n2": {"n_points": 2}},
                              "plot start vs conf", "visualize", value_store=st)
    assert art.has_value is True                      # plot 不在 DATA_TOOLS → 可复用,存值
    stored = st.get(art.value_key)
    assert stored == rows                             # 存的是上游 x/y 数据,原样
    # 绝不是 plot 节点的 {n_points}
    assert stored != {"n_points": 2}
    assert all("n_points" not in row for row in stored)
    assert "start" in stored[0] and "conf" in stored[0]


# ── load_artifact 节点:从值仓取回 ─────────────────────────
class _FakeTrace:
    """execute_node 只调 trace.step(...).ok()/.fail();给个最小桩,免依赖真 Trace。"""
    class _Step:
        def ok(self, **k): pass
        def fail(self, **k): pass
    def step(self, *a, **k): return self._Step()


def _load_node(aid="a1"):
    return parse_dag({"nodes": [{"id": "n1", "tool": "load_artifact",
                                 "inputs": {"artifact_id": aid}, "depends_on": []}]}).nodes[0]


def test_load_artifact_node_returns_stored_value():
    st = InMemoryArtifactValueStore()
    cached = [{"t": 0, "hr": 80}, {"t": 1, "hr": 82}]
    st.put(make_key("sess", "a1"), cached)
    res = execute_node(_load_node("a1"), {}, sandbox=None, trace=_FakeTrace(),
                       session_id="sess", value_store=st)
    assert res.ok is True
    assert res.value == cached                        # 原样取回,未重算、未进沙箱
    assert res.tool == "load_artifact"


def test_load_artifact_node_fails_when_missing():
    st = InMemoryArtifactValueStore()                 # 空仓
    res = execute_node(_load_node("a1"), {}, sandbox=None, trace=_FakeTrace(),
                       session_id="sess", value_store=st)
    assert res.ok is False                            # 取不到 → 节点失败(上层退回重算)
    assert "a1" in res.stderr


def test_load_artifact_node_fails_without_session():
    st = InMemoryArtifactValueStore()
    st.put(make_key("sess", "a1"), [{"x": 1}])
    res = execute_node(_load_node("a1"), {}, sandbox=None, trace=_FakeTrace(),
                       session_id=None, value_store=st)
    assert res.ok is False                            # 无 session_id → 无法定位键


def test_load_artifact_cross_session_isolation():
    # 键 = session_id::artifact_id:sessA 存的 a1,sessB 用同一 artifact_id 取【取不到】(隔离);
    # 同一 sessA 取则命中。防跨会话串值。
    st = InMemoryArtifactValueStore()
    valA = [{"sessA_only": True}]
    st.put(make_key("sessA", "a1"), valA)

    # 反例:sessB 看不到 sessA 的 a1
    miss = execute_node(_load_node("a1"), {}, sandbox=None, trace=_FakeTrace(),
                        session_id="sessB", value_store=st)
    assert miss.ok is False
    assert "a1" in miss.stderr

    # 正例:sessA 自己能取回
    hit = execute_node(_load_node("a1"), {}, sandbox=None, trace=_FakeTrace(),
                       session_id="sessA", value_store=st)
    assert hit.ok is True
    assert hit.value == valA


# ── planner_context 暴露 value_cached(实查活仓)─────────────
def test_planner_context_exposes_value_cached():
    st = InMemoryArtifactValueStore()
    s = Session("sess")
    # a1:sql-only(不缓存) ；a2:ols(缓存)
    s.register_artifact(_dag(_SQL1), {"n1": [{"id": 1}]}, "q1", "retrieve", value_store=st)
    s.register_artifact(_dag(_SQL_OLS), {"n1": [{"x": 1, "y": 2}], "n2": {"r_squared": 0.9}},
                        "q2", "analyze", value_store=st)
    ctx = s.planner_context(["a1", "a2"], value_store=st)
    by_id = {a["id"]: a for a in ctx["resolved_artifacts"]}
    assert by_id["a1"]["value_cached"] is False      # sql-only 没存值
    assert by_id["a2"]["value_cached"] is True        # ols 存了值,活仓里在场


def test_planner_context_value_cached_false_without_store():
    # 不传 value_store → 保守起见 value_cached 一律 False(没有活仓可证明值在场)。
    st = InMemoryArtifactValueStore()
    s = Session("sess")
    s.register_artifact(_dag(_SQL_OLS), {"n1": [{"x": 1, "y": 2}], "n2": {"r_squared": 0.9}},
                        "q2", "analyze", value_store=st)
    ctx = s.planner_context(["a1"])                   # 不传 value_store
    assert ctx["resolved_artifacts"][0]["value_cached"] is False


def test_planner_context_value_cached_false_after_restart_crossreplica():
    # 重启/跨副本:has_value 进了 session blob 并往返存活,但活仓是另一个(空)实例 —— 值没了。
    # value_cached 必须实查活仓 → 报 False(不向 planner 谎称缓存在),从而 planner 走重算,
    # 绝不发出注定取不到的 load_artifact。锁死 fix #2。
    persist_store = InMemoryArtifactValueStore()
    s = Session("sess")
    s.register_artifact(_dag(_SQL_OLS),
                        {"n1": [{"x": 1, "y": 2}], "n2": {"r_squared": 0.9}},
                        "regress", "analyze", value_store=persist_store)

    # 会话 blob 往返(模拟重启后从盘/Redis 恢复):has_value/value_key 仍在
    s2 = Session.from_dict(s.to_dict())
    a = s2.catalog[0]
    assert a.has_value is True and a.value_key is not None     # 标志被持久化了

    # 但新副本的值仓是【全新空仓】—— 值不在场
    fresh_store = InMemoryArtifactValueStore()
    ctx = s2.planner_context([a.id], value_store=fresh_store)
    assert ctx["resolved_artifacts"][0]["value_cached"] is False  # 值没了 → 不暴露缓存


# ── 端到端(orchestrator 级,桩同 test_multiturn)──────────
def test_e2e_turn2_reuses_value_no_recompute():
    """turn1 把一个可复用 artifact 的值存进【共享 VALUE_STORE】;turn2 的 planner 只回一个
    load_artifact 节点 → 结果值 == turn1 的值,且 trace 里【没有】sql/ols 等重算步骤。
    用真 execute_node 跑 load_artifact(它从 orch.VALUE_STORE 取值),只桩 Router/Planner。"""
    import pipeline.orchestrator as orch
    from pipeline.artifact_value_store import InMemoryArtifactValueStore as _Mem
    from pipeline.router import RouterVerdict

    saved = (orch.mcp_client, orch.Router, orch.VALUE_STORE)
    orch.mcp_client = types.SimpleNamespace(
        get_schema=lambda: {"t": [{"column": "x", "type": "int"}]})
    shared_store = _Mem()
    orch.VALUE_STORE = shared_store                  # turn1 存 / turn2 取,同一仓

    s = Session("t")
    final_val = {"r_squared": 0.91, "params": {"x": 1.2}}

    # ── turn1:跑出一个 ols(可复用)结果,值进共享仓 ──
    v1 = RouterVerdict(decision="answer", turn_type="new", intent="analyze")

    class _Router1:
        def judge(self, q, **kw): return v1
    orch.Router = _Router1

    orig_exec = orch.execute_node

    def fake_exec_turn1(node, upstream, sandbox, trace, schema=None, **kwargs):
        # 桩掉 turn1 的 sql/ols:n1 出数据,n2 出 ols 结果
        val = [{"x": 1, "y": 2}] if node.tool == "sql_query" else final_val
        return NodeResult(node.id, node.tool, ok=True, value=val, attempts=1)
    orch.execute_node = fake_exec_turn1

    class _Planner1:
        schema = {"t": [{"column": "x"}]}
        def __init__(self, *a, **k): pass
        def plan(self, nl, *, context=None):
            return _dag(_SQL_OLS)
    try:
        r1 = orch.run_query("regress y on x", planner=_Planner1(), session=s, quiet_trace=True)
        assert r1["status"] == "ok", r1
        a1 = s.catalog[0]
        assert a1.has_value is True                  # 可复用 → 值进了共享仓
        assert shared_store.get(a1.value_key) == final_val

        # ── turn2:planner 只回一个 load_artifact;用【真】execute_node 取值 ──
        orch.execute_node = orig_exec                # turn2 不再桩 execute_node
        v2 = RouterVerdict(decision="answer", turn_type="followup", intent="visualize",
                           references=[{"resolved_to": a1.id, "text": "that", "resolvable": True}])

        class _Router2:
            def judge(self, q, **kw): return v2
        orch.Router = _Router2

        class _Planner2:
            schema = {"t": [{"column": "x"}]}
            def __init__(self, *a, **k): pass
            def plan(self, nl, *, context=None):
                # planner 选了复用:单个 load_artifact 节点
                return parse_dag({"nodes": [{"id": "n1", "tool": "load_artifact",
                    "inputs": {"artifact_id": a1.id}, "depends_on": []}]})

        r2 = orch.run_query("re-present that", planner=_Planner2(), session=s, quiet_trace=True)
        assert r2["status"] == "ok", r2
        assert r2["answer"] == final_val             # 复用回 turn1 算出的同一份值
        # 没有重算:trace 里不应出现 sql_query / ols_regress 步骤
        trace_text = " ".join(step.get("name", "") for step in r2["trace"])
        assert "sql_query" not in trace_text, trace_text
        assert "ols_regress" not in trace_text, trace_text
        assert "load_artifact" in trace_text         # 确实走了载入
    finally:
        orch.execute_node = orig_exec
        orch.mcp_client, orch.Router, orch.VALUE_STORE = saved


# ── 序列化:has_value/value_key 经 to_dict/from_dict 往返 ───
def test_value_fields_survive_roundtrip():
    st = InMemoryArtifactValueStore()
    s = Session("sess")
    # 用 sentinel:一个超出 preview 每格字符封顶(PREVIEW_CELL)的长串 —— 它只会完整出现在
    # 值仓里,绝不应原样进 session blob(blob 里的 preview 会把它截断)。
    sentinel = "SENTINEL_" + ("Z" * 500)
    s.register_artifact(_dag(_SQL_OLS),
                        {"n1": [{"x": 1, "y": 2}], "n2": {"r_squared": 0.9, "note": sentinel}},
                        "regress", "analyze", value_store=st)
    s2 = Session.from_dict(s.to_dict())
    a = s2.catalog[0]
    assert a.has_value is True
    assert a.value_key == make_key("sess", a.id)
    # 值仓里有完整 sentinel
    assert sentinel in str(st.get(a.value_key))
    # 但 session blob 只存指针,不存完整结果值 —— 完整 sentinel 不应出现在 blob 里
    blob = s.to_dict()
    assert "has_value" in blob["catalog"][0] and "value_key" in blob["catalog"][0]
    assert sentinel not in str(blob)                   # 完整值没被塞进 session blob


def test_module_default_store_is_inmemory():
    assert isinstance(AVS.VALUE_STORE, InMemoryArtifactValueStore)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
