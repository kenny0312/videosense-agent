"""
会话记忆 / 跨轮 artifact 的轻量单元测试 —— 纯离线,不依赖 GCP / DB。
    python -m pipeline.test_session

覆盖(M7b:catalog 纯 handle,无 recipe):artifact kind 推断、预览封顶、视图非对称、
id 递增、容量淘汰、resolve_references 丢幻觉 id、store 幂等、history 记录、持久化往返。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline import session as S
from pipeline.session import Session, SessionStore


# 旧 DAG fixture(节点列表)—— 仍用作 _reg 的输入形状,不再解析成 DAG。
_SQL1 = [{"id": "n1", "tool": "sql_query",
          "inputs": {"sql": "SELECT id, predicate FROM video_facts"}, "depends_on": []}]
_SQL_OLS = [
    {"id": "n1", "tool": "sql_query", "inputs": {"sql": "SELECT x, y FROM t"}, "depends_on": []},
    {"id": "n2", "tool": "ols_regress", "inputs": {"y": "y", "x": ["x"]}, "depends_on": ["n1"]},
]
_SQL_PLOT = [
    {"id": "n1", "tool": "sql_query", "inputs": {"sql": "SELECT start, conf FROM t"}, "depends_on": []},
    {"id": "n2", "tool": "plot",
     "inputs": {"kind": "scatter", "x": "start", "y": "conf", "title": "X"}, "depends_on": ["n1"]},
]


def _reg(s, nodes, node_values, question, intent, **kw):
    """把旧 (节点列表, node_values) fixture 适配到 M7b 的 register_artifact(final/preview)。
    final = 最后一个节点;preview_value:plot-final 取上游非 plot 节点(plot 自身只有 {n_points}),
    否则 = final 值。语义与旧 register 的 topo/plot-preview 完全一致。"""
    final = nodes[-1]
    final_tool = final["tool"]
    final_value = node_values.get(final["id"])
    preview_value = final_value
    if final_tool == "plot":
        for nd in reversed(nodes[:-1]):
            if nd["tool"] != "plot":
                preview_value = node_values.get(nd["id"])
                break
    return s.register_artifact(final_tool=final_tool, final_value=final_value,
                               preview_value=preview_value, question=question,
                               intent=intent, **kw)


# ── artifact kind 推断 ─────────────────────────────────────
def test_sql_artifact_is_table_kind():
    s = Session("t")
    art = _reg(s, _SQL1, {"n1": [{"id": 1, "predicate": "skiing"}]},
               "Find skiing videos", "retrieve")
    assert art.kind == "table"
    assert not hasattr(art, "recipe")            # M7b:不再有 recipe 字段


def test_ols_artifact_is_scalar_kind():
    s = Session("t")
    art = _reg(s, _SQL_OLS, {"n1": [{"x": 1, "y": 2}], "n2": {"r_squared": 0.9}},
               "regress y on x", "analyze")
    assert art.kind == "scalar"                  # ols_regress final → 单 dict


def test_plot_final_uses_upstream_preview():
    s = Session("t")
    rows = [{"start": 1, "conf": 0.9}, {"start": 2, "conf": 0.8}]
    art = _reg(s, _SQL_PLOT, {"n1": rows, "n2": {"n_points": 2}},
               "plot start vs conf", "visualize")
    assert art.kind == "plot"
    # 预览来自上游数据节点 n1,而非 plot 节点的 {n_points}
    assert "start" in art.preview[0] and "conf" in art.preview[0]
    assert all("n_points" not in row for row in art.preview)
    assert art.n == 2


# ── 预览封顶 ───────────────────────────────────────────────
def test_preview_caps_rows_cols_cells():
    s = Session("t")
    wide = [{f"c{i}": ("x" * 200) for i in range(12)} for _ in range(50)]
    art = _reg(s, _SQL1, {"n1": wide}, "big", "retrieve")
    assert art.n == 50                              # 真实行数保留
    assert len(art.preview) <= S.PREVIEW_ROWS       # 行封顶
    for row in art.preview:
        assert len(row) <= S.PREVIEW_COLS           # 列封顶
        for v in row.values():
            assert len(v) <= S.PREVIEW_CELL         # 每格封顶


# ── 视图 ───────────────────────────────────────────────────
def test_catalog_view_keys():
    s = Session("t")
    _reg(s, _SQL1, {"n1": [{"id": 1}]}, "q", "retrieve")
    view = s.catalog_view()
    assert set(view[0].keys()) == {"id", "turn", "kind", "label", "preview", "n"}
    assert "recipe" not in view[0]                  # 回归护栏:视图绝不含内部值/已废弃字段


def test_empty_catalog_view_is_empty():
    assert Session("t").catalog_view() == []        # turn1 → Router have_memory=false


# ── id 递增 / 容量淘汰 ─────────────────────────────────────
def test_artifact_ids_monotonic():
    s = Session("t")
    ids = [_reg(s, _SQL1, {"n1": [{"id": 1}]}, "q", "retrieve").id for _ in range(3)]
    assert ids == ["a1", "a2", "a3"]


def test_caps_evict_oldest():
    s = Session("t")
    for _ in range(S.MAX_ARTIFACTS + 3):
        _reg(s, _SQL1, {"n1": [{"id": 1}]}, "q", "retrieve")
    assert len(s.catalog) == S.MAX_ARTIFACTS
    assert s.catalog[0].id == "a4"                  # a1..a3 被淘汰
    assert s.catalog[-1].id == f"a{S.MAX_ARTIFACTS + 3}"


# ── resolve_references ────────────────────────────────────
def test_resolve_references_drops_unknown_id():
    s = Session("t")
    _reg(s, _SQL1, {"n1": [{"id": 1}]}, "q", "retrieve")   # a1
    v = types.SimpleNamespace(references=[{"resolved_to": "a1"},
                                          {"resolved_to": "a9"},   # 幻觉
                                          {"resolved_to": "a1"}])  # 重复
    assert s.resolve_references(v) == ["a1"]


def test_resolve_references_empty_when_no_catalog():
    v = types.SimpleNamespace(references=[{"resolved_to": "a1"}])
    assert Session("t").resolve_references(v) == []


# ── history / store ───────────────────────────────────────
def test_record_turn_pulls_verdict_fields():
    s = Session("t")
    v = types.SimpleNamespace(turn_type="followup", intent="visualize")
    t = s.record_turn("plot those", v, "ok", {"n_points": 3})
    assert t.turn == 1 and t.turn_type == "followup" and t.intent == "visualize"
    assert s.history_view()[0]["question"] == "plot those"


def test_store_get_or_create_idempotent():
    st = SessionStore()                             # 无 path → 纯内存
    a = st.get_or_create("x")
    b = st.get_or_create("x")
    assert a is b
    assert st.reset("x") is not a                   # reset 换新对象


# ── 滚动摘要 / 视图截断 / 指代冻结 ─────────────────────────
def test_rolling_on_eviction():
    s = Session("t")
    for i in range(S.MAX_TURNS + 3):
        s.record_turn(f"q{i}", None, "ok", f"ans{i}")
    assert len(s.history) == S.MAX_TURNS
    assert [t.turn for t in s.rolling] == [1, 2, 3]          # 最老 3 条转入 rolling(整条)
    assert s.history[-1].turn == S.MAX_TURNS + 3            # 轮号全局单调
    v = s.history_view()
    assert v[0]["turn"] == 1 and v[0]["answer_summary"] == ""           # 最老 = terse
    assert v[-1]["turn"] == S.MAX_TURNS + 3 and v[-1]["answer_summary"] # 最近 = full
    assert [e["turn"] for e in v] == list(range(1, S.MAX_TURNS + 4))    # 连续单调无洞


def test_history_view_terse_then_full():
    s = Session("t")
    for i in range(8):
        s.record_turn(f"q{i}", None, "ok", f"ans{i}")
    v = s.history_view()
    assert len(v) == 8
    assert v[0]["answer_summary"] == "" and v[-1]["answer_summary"]     # 老 terse / 近 full
    assert [e["turn"] for e in v] == list(range(1, 9))


def test_referenced_ids_recorded():
    s = Session("t")
    t = s.record_turn("plot those", None, "ok", None, referenced_ids=["a1"])
    assert t.referenced_artifact_ids == ["a1"]
    assert s.history_view()[0]["used"] == ["a1"]            # 冻结指代进了视图


def test_catalog_view_trims_newest_first():
    s = Session("t")
    for _ in range(S.CATALOG_VIEW_MAX + 3):
        _reg(s, _SQL1, {"n1": [{"id": 1}]}, "q", "retrieve")
    v = s.catalog_view()
    assert len(v) == S.CATALOG_VIEW_MAX
    assert v[0]["id"] == f"a{S.CATALOG_VIEW_MAX + 3}"       # newest-first
    assert v[-1]["id"] == "a4"                              # 视图里最老的可见项


# ── 持久化:save → 新 store load(模拟重启续上)───────────
def test_persist_roundtrip():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "s.sqlite")
        st = SessionStore(path=p)
        s = st.get_or_create("x")
        _reg(s, _SQL1, {"n1": [{"id": 1, "predicate": "ski"}]}, "find ski", "retrieve")
        s.record_turn("find ski", None, "ok", [{"id": 1}], artifact_ids=["a1"])
        st.save(s)

        st2 = SessionStore(path=p)                  # 模拟重启:空缓存,从盘恢复
        s2 = st2.get_or_create("x")
        assert s2.catalog and s2.catalog[0].id == "a1" and s2.catalog[0].kind == "table"
        assert s2.history and s2.history[0].artifact_ids == ["a1"]
        assert s2._seq == 1 and s2._turn_no == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_from_dict_tolerates_legacy_recipe_field():
    """M7b 前的 blob 里 catalog 项带 recipe;from_dict 应丢弃未知字段而非抛错。"""
    s = Session("x")
    _reg(s, _SQL1, {"n1": [{"id": 1}]}, "q", "retrieve")
    blob = s.to_dict()
    blob["catalog"][0]["recipe"] = {"type": "sql", "sql": "SELECT 1"}   # 注入旧字段
    s2 = Session.from_dict(blob)
    assert s2.catalog[0].id == "a1" and not hasattr(s2.catalog[0], "recipe")


def test_inmemory_store_save_noop():
    st = SessionStore()                             # path=None
    s = st.get_or_create("x")
    s.record_turn("q", None, "ok", None)
    st.save(s)                                      # 不应抛错、不应建文件
    assert st.get_or_create("x") is s


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
