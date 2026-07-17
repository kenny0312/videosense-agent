"""
方向1(校验+自愈对称)的轻量单元测试 —— 不依赖 GCP / DB。
    python -m pipeline.test_sql_robustness

覆盖:
  A1 validate_sql_columns : 合法通过 / 坏表名拒绝 / 别名·CTE·子查询·字符串 fail-open / 空 schema 不校验
  B  _run_sql_query 自愈  : 首错→repair→二次成功(桩) / 跑满预算干净失败(不死循环)
"""
from __future__ import annotations

import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pipeline.node_executor as ne
from pipeline.dag_schema import DAG, Node
from pipeline.agentops.trace import Trace
from pipeline.sql_validate import validate_sql_columns

SCHEMA = {
    "video_metadata": [{"column": "id", "type": "int"}],
    "video_facts": [{"column": "predicate", "type": "text"}],
}


def _dag(sql: str) -> DAG:
    return DAG(nodes=[Node(id="n1", tool="sql_query", inputs={"sql": sql})])


def _node(sql: str = "SELECT * FROM video_metadata ORDER BY likes") -> Node:
    return Node(id="n1", tool="sql_query", inputs={"sql": sql})


# ── A1: validate_sql_columns ──────────────────────────────
def test_validate_good():
    validate_sql_columns(_dag("SELECT * FROM video_metadata WHERE id > 0"), SCHEMA)


def test_validate_bad_table():
    try:
        validate_sql_columns(_dag("SELECT * FROM nonexistent_table"), SCHEMA)
    except ValueError as e:
        assert "nonexistent_table" in str(e), e
        return
    raise AssertionError("应当对未知表抛 ValueError")


def test_validate_failopen():
    # 别名 / JOIN 已知表 / 函数 / 字符串里的 from / CTE / 子查询 —— 都不该误拒
    for s in [
        "SELECT vm.id FROM video_metadata vm JOIN video_facts vf ON vm.id = vf.id",
        "SELECT count(*) FROM video_metadata WHERE predicate ILIKE '%from kitchen%'",
        "WITH t AS (SELECT id FROM video_metadata) SELECT * FROM t",
        "SELECT * FROM (SELECT id FROM video_facts) sub",
    ]:
        validate_sql_columns(_dag(s), SCHEMA)


def test_validate_empty_schema_failopen():
    validate_sql_columns(_dag("SELECT * FROM whatever_table"), {})  # 无 schema → 不校验


# ── B: _run_sql_query 自愈 ────────────────────────────────
def test_selfheal_recovers():
    calls = {"n": 0}

    def fake_query(sql):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError('column "likes" does not exist')
        return [{"id": 1}, {"id": 2}]

    class FakeFixer:
        def repair(self, bad_sql, err, schema):
            return "SELECT * FROM video_metadata ORDER BY like_count"

    saved = (ne.mcp_client, ne.SqlFixer)
    ne.mcp_client = types.SimpleNamespace(query_db=fake_query)
    ne.SqlFixer = FakeFixer
    try:
        trace = Trace(quiet=True)
        res = ne._run_sql_query(_node(), SCHEMA, trace)
        assert res.ok, res.stderr
        assert res.value == [{"id": 1}, {"id": 2}], res.value
        assert res.attempts == 2, res.attempts
        assert any("repair" in s.name for s in trace.steps), "trace 应含 repair 步"
    finally:
        ne.mcp_client, ne.SqlFixer = saved        # 复原:别把替身泄漏给后续测试


def test_selfheal_budget():
    def always_fail(sql):
        raise RuntimeError("boom")

    class FakeFixer:
        def repair(self, bad_sql, err, schema):
            return bad_sql   # 修不好,原样返回

    saved = (ne.mcp_client, ne.SqlFixer)
    ne.mcp_client = types.SimpleNamespace(query_db=always_fail)
    ne.SqlFixer = FakeFixer
    try:
        trace = Trace(quiet=True)
        res = ne._run_sql_query(_node(), SCHEMA, trace)
        assert not res.ok
        assert res.attempts == ne.SQL_MAX_RETRIES + 1, res.attempts   # 跑满,不死循环
    finally:
        ne.mcp_client, ne.SqlFixer = saved        # 复原:别把替身泄漏给后续测试


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
