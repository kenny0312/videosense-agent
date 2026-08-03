"""批次 1 · B3/B4 的故障注入单测(离线,不依赖 GCP / DB / MCP)。

B3 SQLSTATE 分类:
    只有 42601 / 42703 / 42P01(SQL 真写错了)进 SqlFixer;
    57014(超时被取消)/ 55P03(拿不到锁)/ 任何拿不到 pgcode 的传输层错 → 【不改 SQL】,
    直接失败并如实回喂 —— 否则一次 statement timeout 就变成
    "超时 → 叫 LLM 改 SQL → 再超时" 的烧钱循环。
    回喂文案还必须让大脑分得清两类,否则烧钱循环只是从代码搬进了模型脑子里。

B4 截断薄壳:
    薄壳【只在真截断时】才套(没截断保持裸 list,下游零迁移);
    show_table 截断时说"至少 N 条"而不是假陈述"共 N 条";
    薄壳的 `_note` 在 loop 预览里【单独成格】,不被 80 字/格挤掉。
"""
from __future__ import annotations

import sys
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pipeline.node_executor as ne
from pipeline import loop_driver
from pipeline.agentops.trace import Trace
from pipeline.dag_schema import Node

SCHEMA = {"video_metadata": [{"column": "id", "type": "int"}]}


def _node(sql: str = "SELECT * FROM video_metadata") -> Node:
    return Node(id="n1", tool="sql_query", inputs={"sql": sql})


def _pgerr(msg: str, code: "str | None") -> RuntimeError:
    """psycopg2 的真实形状:异常对象上挂 .pgcode。code=None → 传输层错(连码都没有)。"""
    e = RuntimeError(msg)
    if code is not None:
        e.pgcode = code
    return e


class _CountingFixer:
    """SqlFixer 替身:被叫到几次就是"进了几次自愈"。"""
    calls = 0

    def repair(self, bad_sql, err, schema):
        type(self).calls += 1
        return bad_sql + " /*fixed*/"


def _run_with(query_fn, fixer=None):
    """把 mcp_client / SqlFixer 换成替身跑一次 _run_sql_query,跑完复原。"""
    saved = (ne.mcp_client, ne.SqlFixer)
    ne.mcp_client = types.SimpleNamespace(query_db=query_fn)
    ne.SqlFixer = fixer or _CountingFixer
    try:
        return ne._run_sql_query(_node(), SCHEMA, Trace(quiet=True))
    finally:
        ne.mcp_client, ne.SqlFixer = saved


# ── B3:哪些码进 SqlFixer ───────────────────────────────────

def test_repairable_sqlstates_do_enter_fixer():
    """42601 / 42703 / 42P01 三个码【各自】都要能进 SqlFixer 并跑满重试预算。"""
    for code in ("42601", "42703", "42P01"):
        _CountingFixer.calls = 0
        calls = {"n": 0}

        def q(sql, _c=code, meta=None):
            calls["n"] += 1
            raise _pgerr("bad sql", _c)

        res = _run_with(q)
        assert not res.ok
        assert _CountingFixer.calls == ne.SQL_MAX_RETRIES, (code, _CountingFixer.calls)
        assert calls["n"] == ne.SQL_MAX_RETRIES + 1, (code, calls["n"])
        assert res.attempts == ne.SQL_MAX_RETRIES + 1, (code, res.attempts)
        assert "SQL 写错了" in res.stderr, (code, res.stderr)


def test_timeout_does_not_enter_fixer():
    """57014 = statement timeout / query canceled。【绝不能】叫 LLM 改 SQL 再重发。"""
    _CountingFixer.calls = 0
    calls = {"n": 0}

    def q(sql, meta=None):
        calls["n"] += 1
        raise _pgerr("canceling statement due to statement timeout", "57014")

    res = _run_with(q)
    assert not res.ok
    assert _CountingFixer.calls == 0, "超时进了 SqlFixer = 烧钱循环还在"
    assert calls["n"] == 1, f"超时后又重发了查询:{calls['n']} 次"
    assert res.attempts == 1, res.attempts
    # 回喂文案要让大脑分得清:这不是 SQL 写错了,别原样重发
    assert "这不是 SQL 写错了" in res.stderr, res.stderr
    assert "57014" in res.stderr, res.stderr


def test_lock_not_available_does_not_enter_fixer():
    """55P03 = lock not available。同样不是 SQL 写错了。"""
    _CountingFixer.calls = 0

    def q(sql, meta=None):
        raise _pgerr("could not obtain lock on relation", "55P03")

    res = _run_with(q)
    assert not res.ok
    assert _CountingFixer.calls == 0
    assert "这不是 SQL 写错了" in res.stderr, res.stderr


def test_transport_error_without_pgcode_does_not_enter_fixer():
    """连接断 / JSON 解析失败:拿不到 pgcode → 不改 SQL,直接失败。"""
    _CountingFixer.calls = 0
    calls = {"n": 0}

    def q(sql, meta=None):
        calls["n"] += 1
        raise _pgerr("server closed the connection unexpectedly", None)

    res = _run_with(q)
    assert not res.ok
    assert _CountingFixer.calls == 0, "传输层错进了 SqlFixer"
    assert calls["n"] == 1, calls["n"]
    assert "这不是 SQL 写错了" in res.stderr, res.stderr
    assert "SQLSTATE" in res.stderr, res.stderr      # 明说"连 SQLSTATE 都没给出"


def test_repairable_sqlstates_is_a_module_constant():
    """可自愈码集中在一个模块级常量里,不散在 if 里(加减码只改一处)。"""
    assert ne._REPAIRABLE_SQLSTATES == frozenset({"42601", "42703", "42P01"})
    assert "57014" not in ne._REPAIRABLE_SQLSTATES
    assert "55P03" not in ne._REPAIRABLE_SQLSTATES


# ── B4:薄壳只在截断时出现 ──────────────────────────────────

def _q_plain(rows):
    def q(sql, meta=None):        # 有 meta 形参,但【不填】= 没截断(契约:没截断不动那个 dict)
        return list(rows)
    return q


def _q_truncated(rows, *, total_seen, reason="row_cap"):
    def q(sql, meta=None):
        if meta is not None:
            meta["truncated"] = True
            meta["total_seen"] = total_seen
            meta["returned"] = len(rows)
            meta["reason"] = reason
        return list(rows)
    return q


def test_shell_absent_when_not_truncated():
    """没截断 → value 仍是【裸 list】,一个字段都不加(否则每个下游消费者都要改)。"""
    rows = [{"id": 1}, {"id": 2}]
    res = _run_with(_q_plain(rows))
    assert res.ok
    assert res.value == rows
    assert isinstance(res.value, list), type(res.value)


def test_shell_present_when_truncated():
    rows = [{"id": i} for i in range(5)]
    res = _run_with(_q_truncated(rows, total_seen=2001))
    assert res.ok
    assert isinstance(res.value, dict), type(res.value)
    assert res.value["rows"] == rows
    assert res.value["_truncated"] is True
    assert res.value["_total"] == 2001
    assert res.value["_returned"] == 5
    assert res.value["_reason"] == "row_cap"
    assert "至少" in res.value["_note"] and "COUNT(*)" in res.value["_note"]


def test_doubles_must_match_production_signature():
    """替身签名必须和生产 query_db 一致 —— 对不上要【当场炸】,不许静默降级。

    合并前这里曾有个"容忍老签名"的兼容层。它撤掉了,而且撤对了:
    一个签名和真函数对不上的替身就是【坏替身】,它会让测试在一个生产上不存在的
    形状上全绿。本仓刚吃过这个亏 —— `_mask_ts` 的单测只测函数本身、从不测接线,
    于是它在 evals/world.py 那条路上被整个替换掉、失效了很久还一直绿着。

    所以这条反过来钉:老签名的替身必须让调用失败(TypeError 被 _run_sql_query
    归到"拿不到 SQLSTATE 的传输错"→ ok=False),而不是悄悄按老路走通。
    """
    import inspect

    from pipeline import mcp_client

    params = inspect.signature(mcp_client.query_db).parameters
    assert "meta" in params, "生产 query_db 没有 meta 形参了?契约变了就要同步改这里"
    assert params["meta"].default is None, "meta 必须可选,否则每个既有调用点都要改"

    def old_style(sql):           # 老签名替身:少一个形参
        return [{"id": 1}]

    res = _run_with(old_style)
    assert not res.ok, "签名对不上却跑通了 —— 说明还有一层在静默兼容,把坏替身放过去了"


def test_query_not_resent_when_signature_lacks_meta():
    """签名检查而不是 catch TypeError:query_db 【内部】抛 TypeError 时绝不能重发查询。"""
    calls = {"n": 0}

    def inner_typeerror(sql, meta=None):
        calls["n"] += 1
        raise TypeError("boom inside query_db")

    res = _run_with(inner_typeerror)
    assert not res.ok
    assert calls["n"] == 1, f"内部 TypeError 触发了重发:{calls['n']} 次"


# ── B4:show_table 措辞 ───────────────────────────────────

def _show_table(upstream_value, caption="") -> dict:
    node = Node(id="t1", tool="show_table", inputs={"caption": caption},
                depends_on=["c1"])
    return ne._run_show_table(node, {"c1": upstream_value})


def test_show_table_wording_not_truncated_is_byte_identical():
    """truncated=false → 与今天【逐字节相同】(这条是防回归的锚)。"""
    small = [{"id": i} for i in range(3)]
    assert _show_table(small).value["note"] == "📋 已为你列出 3 条"

    big = [{"id": i} for i in range(1500)]
    assert (_show_table(big).value["note"]
            == f"📋 已为你列出 1500 条(共 1500 条,展示前 {ne.SHOW_TABLE_MAX_ROWS} 条)")


def test_show_table_wording_truncated_says_at_least():
    """truncated=true → "至少 N 条(已达返回上限,未取全)"。
    说"共 N 条"是假陈述:N 只是我们取回来的,不是库里的真实总数。"""
    rows = [{"id": i} for i in range(2000)]
    shell = ne._truncated_shell(rows, {"returned": 2000, "total_seen": 2001,
                                       "reason": "row_cap"})
    note = _show_table(shell).value["note"]
    assert "至少 2001 条" in note, note
    assert "已达返回上限,未取全" in note, note
    assert "共 2000 条" not in note, note
    assert f"展示前 {ne.SHOW_TABLE_MAX_ROWS} 条" in note, note


def test_show_table_truncated_under_cap_still_says_at_least():
    """字节 cap 下 returned 可能 < 1000 —— 这时今天的写法【一个字都不会提截断】,
    大脑就会拿 300 当总数。必须仍然说"至少"。"""
    rows = [{"id": i} for i in range(300)]
    shell = ne._truncated_shell(rows, {"returned": 300, "total_seen": 301,
                                       "reason": "byte_cap"})
    res = _show_table(shell)
    assert "至少 301 条" in res.value["note"], res.value["note"]
    assert res.table["truncated"] is True
    assert res.table["rows"] and res.table["n"] == 300


def test_show_table_reads_rows_through_the_shell():
    """薄壳不能把 show_table 变成"没有可展示的表格数据"(它只认 list)。"""
    shell = ne._truncated_shell([{"id": 1}, {"id": 2}], {"returned": 2, "total_seen": 9,
                                                         "reason": "byte_cap"})
    res = _show_table(shell)
    assert res.table["n"] == 2 and res.table["columns"] == ["id"]


def test_show_stat_and_show_video_read_through_the_shell():
    shell = ne._truncated_shell([{"cnt": 7}], {"returned": 1, "total_seen": 2,
                                               "reason": "byte_cap"})
    stat = ne._run_show_stat(Node(id="s1", tool="show_stat", inputs={}, depends_on=["c1"]),
                             {"c1": shell})
    assert stat.stat["items"] == [{"label": "cnt", "value": 7}], stat.stat

    vshell = ne._truncated_shell([{"video_id": "v001"}], {"returned": 1, "total_seen": 2,
                                                          "reason": "row_cap"})
    items = ne._collect_items(Node(id="v1", tool="show_video", inputs={}, depends_on=["c1"]),
                              {"c1": vshell})
    assert [i["video_id"] for i in items] == ["v001"], items


# ── B4:上游注入形状不变 ──────────────────────────────────

def test_inject_keeps_bare_list_shape_and_adds_meta_variable():
    """代码生成的输入形状不能变:data_<id> 仍是裸行集,截断信息只是【多一个变量】。"""
    shell = ne._truncated_shell([{"id": 1}], {"returned": 1, "total_seen": 2001,
                                              "reason": "row_cap"})
    plain, meta = ne._unwrap_rows(shell)
    code = ne._inject("pass", Node(id="p1", tool="python", inputs={"instruction": "x"}),
                      {"c1": plain}, {"c1": meta})
    ns: dict = {}
    exec(code, ns)                                   # noqa: S102 —— 就是要验注入头能跑
    assert ns["data_c1"] == [{"id": 1}], ns["data_c1"]
    assert ns["data_c1_meta"] == {"truncated": True, "returned": 1, "total": 2001}


# ── B4:_note 在 loop 预览里单独成格 ────────────────────────

def test_note_gets_its_own_preview_cell_and_is_not_truncated():
    rows = [{"id": i, "title": f"t{i}"} for i in range(50)]
    shell = ne._truncated_shell(rows, {"returned": 50, "total_seen": 2001,
                                       "reason": "row_cap"})
    pv, n = loop_driver._preview_sql(shell, rows=loop_driver.SQL_PREVIEW_ROWS)
    assert n == 50, n
    assert len(pv) == loop_driver.SQL_PREVIEW_ROWS + 1, len(pv)   # 30 行预览 + 1 格 note
    assert pv[0] == {"id": "0", "title": "t0"}, pv[0]             # 行集照常预览,没被压成一格
    assert pv[-1] == {"_note": shell["_note"]}, pv[-1]            # 一字不少
    assert "…" not in pv[-1]["_note"]
    assert len(pv[-1]["_note"]) > 80                              # 确实超过默认 80 字/格


def test_plain_preview_would_have_eaten_the_note():
    """反证:这就是为什么要单独成格 —— 直接 _preview 会把 note 腰斩、把行集压成一格。"""
    rows = [{"id": i} for i in range(50)]
    shell = ne._truncated_shell(rows, {"returned": 50, "total_seen": 2001,
                                       "reason": "row_cap"})
    pv, n = loop_driver._preview(shell, rows=loop_driver.SQL_PREVIEW_ROWS)
    assert n == 1, n                                              # 整个薄壳被当成一行
    assert pv[0]["_note"].endswith("…"), pv[0]["_note"]           # note 被腰斩
    assert len(pv[0]["_note"]) == 80


def test_preview_sql_unchanged_when_not_truncated():
    rows = [{"id": i} for i in range(5)]
    assert (loop_driver._preview_sql(rows, rows=loop_driver.SQL_PREVIEW_ROWS)
            == loop_driver._preview(rows, rows=loop_driver.SQL_PREVIEW_ROWS))


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
