"""批次 1 · B1/B2/B5 服务端半边 —— 有界读取 / 客户端超时顺序 / 伪造 SQLSTATE。

覆盖任务书 §5-B5 里属于服务端的那些项:
    <cap / =cap 不误报 / cap+1 报截断且 rows 恰好 cap /
    字节 cap / 单行超限 → rows=[] / CJK 按 UTF-8 字节(不是字符数) /
    空结果仍保留 columns / COUNT(*) 单行单列不被误判 /
    mock 三种 SQLSTATE 都能被 `.pgcode` 读到

外加两条"别把安全项做成展示项"的契约锁:
    · USE_BOUNDED_SQL 关时 wire 与今天逐字节等价,但行数上界照样勒着
    · 开关开/关返回【完全相同的行集】
"""
from __future__ import annotations

import asyncio
import json

import pytest

from pipeline import config, mcp_client
from pipeline.sql_bounds import (
    _ENVELOPE_OVERHEAD, build_envelope, fetch_bounded, needs_envelope,
)


# ── 假游标:只实现 DB-API 里 fetch_bounded 该用的那两样 ──────────────
class FakeCursor:
    """description + fetchmany。fetchall 故意做成炸弹 —— 有界读取绝不该调它。"""

    def __init__(self, rows, columns=None):
        self._rows = list(rows)
        self._i = 0
        cols = columns if columns is not None else (list(rows[0].keys()) if rows else [])
        self.description = tuple((c, None, None, None, None, None, None) for c in cols)
        self.fetch_calls = 0

    def fetchmany(self, n):
        self.fetch_calls += 1
        chunk = self._rows[self._i:self._i + n]
        self._i += len(chunk)
        return chunk

    def fetchall(self):
        raise AssertionError("fetch_bounded 绝不该调用 fetchall()")

    @property
    def consumed(self) -> int:
        return self._i


def _rows(n, key="a"):
    return [{key: i} for i in range(n)]


# ══════════════════════════════════════════════════════════════
#  B1 ① 行数上界
# ══════════════════════════════════════════════════════════════

def test_under_cap_no_truncation():
    res = fetch_bounded(FakeCursor(_rows(10)), max_rows=2000)
    assert res.truncated is False
    assert res.reason is None
    assert res.returned == 10
    assert res.rows == _rows(10)


def test_exactly_cap_is_not_reported_as_truncated():
    """=cap 不误报 —— 差一错在这里最贵:每次刚好取满都谎报"还有更多",
    大脑会去翻不存在的下一页。"""
    res = fetch_bounded(FakeCursor(_rows(5)), max_rows=5)
    assert res.truncated is False
    assert res.reason is None
    assert res.returned == 5
    assert res.total_seen == 5


def test_cap_plus_one_truncates_and_keeps_exactly_cap_rows():
    cur = FakeCursor(_rows(8))
    res = fetch_bounded(cur, max_rows=5, batch=2)
    assert res.truncated is True
    assert res.reason == "row_cap"
    assert res.returned == 5
    assert res.rows == _rows(5)              # 第 6 行【绝不进 rows】
    assert res.total_seen == 6               # 至少 cap+1
    # 确认截断后没有把游标读干 —— 读干就等于没有上界
    assert cur.consumed == 6, cur.consumed


def test_bounded_read_never_calls_fetchall():
    # FakeCursor.fetchall 是炸弹;这条能过就说明分批读没退化成 fetchall()
    fetch_bounded(FakeCursor(_rows(300)), max_rows=2000, batch=128)


# ══════════════════════════════════════════════════════════════
#  B1 ② 字节上界
# ══════════════════════════════════════════════════════════════

def _budget_for(payload_bytes: int) -> int:
    """把"我想给行留 payload_bytes 字节"换算成 max_bytes(引擎恒定扣信封开销)。"""
    return _ENVELOPE_OVERHEAD + payload_bytes


def test_byte_cap_stops_mid_stream():
    # 每行 {"a": "xxxx..."} 约 60 字节;给 200 字节预算 → 只塞得下头几行
    rows = [{"a": "x" * 50} for _ in range(20)]
    res = fetch_bounded(FakeCursor(rows), max_rows=2000, max_bytes=_budget_for(200))
    assert res.truncated is True
    assert res.reason == "byte_cap"
    assert 0 < res.returned < 20
    assert res.total_seen == res.returned + 1     # 溢出的那行被扫到但没进 rows
    # 真实出口字节确实没越界
    assert len(json.dumps(res.rows, ensure_ascii=False).encode("utf-8")) <= 200


def test_single_row_too_large_returns_no_rows():
    """单行就超限 → rows=[],不返回半行。
    半行 JSON 既解析不了,截一半的 dict 又会让大脑以为那就是全部字段。"""
    res = fetch_bounded(FakeCursor([{"a": "x" * 5000}]),
                        max_rows=2000, max_bytes=_budget_for(200))
    assert res.truncated is True
    assert res.reason == "single_row_too_large"
    assert res.rows == []
    assert res.returned == 0
    assert res.total_seen == 1


def test_cjk_counted_as_utf8_bytes_not_characters():
    """CJK 必须按 UTF-8 字节算。

    这一行:JSON 是 `{"t": "汉…汉"}` = 7 + 100*3 + 2 = 309 字节,
    但【字符数】只有 109。预算给 200:
        按字节算 → 309 > 200 → 单行超限(正确)
        按字符算 → 109 <= 200 → 会被放行(错误,而且是静默把 3 倍体积
                                       灌进大脑 context 的那种错)
    """
    row = {"t": "汉" * 100}
    assert len(json.dumps(row, ensure_ascii=False)) == 109             # 字符数
    assert len(json.dumps(row, ensure_ascii=False).encode("utf-8")) == 309  # 字节数

    res = fetch_bounded(FakeCursor([row]), max_rows=2000, max_bytes=_budget_for(200))
    assert res.truncated is True
    assert res.reason == "single_row_too_large"

    # 反向:同样 200 字节预算下,50 个汉字(159 字节)必须【放得进】——
    # 否则这条测试用"永远拒绝"也能蒙混过关。
    small = {"t": "汉" * 50}
    assert len(json.dumps(small, ensure_ascii=False).encode("utf-8")) == 159
    ok = fetch_bounded(FakeCursor([small]), max_rows=2000, max_bytes=_budget_for(200))
    assert ok.truncated is False
    assert ok.rows == [small]


# ══════════════════════════════════════════════════════════════
#  B1 ③ 列名走 cursor.description
# ══════════════════════════════════════════════════════════════

def test_empty_result_still_reports_columns():
    """零行也要能回列名。

    从行 dict 反推的话零行 == 零列,大脑分不清
    "这张表没有匹配的行"(该收工)和"我不知道有哪些列"(该去 get_schema)。
    """
    res = fetch_bounded(FakeCursor([], columns=["video_id", "title"]))
    assert res.rows == []
    assert res.columns == ["video_id", "title"]
    assert res.truncated is False


def test_count_star_single_row_single_col_not_misjudged():
    res = fetch_bounded(FakeCursor([{"n": 1300}]), max_rows=2000)
    assert res.truncated is False
    assert res.reason is None
    assert res.returned == 1
    assert res.columns == ["n"]
    assert res.rows == [{"n": 1300}]


# ══════════════════════════════════════════════════════════════
#  B5 mock 伪造 SQLSTATE(上游靠 .pgcode 分类)
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("sql, expected", [
    ("SELECT FROM video_metadata",          "42601"),   # 语法错
    ("SELECT nosuchcol FROM video_metadata", "42703"),  # 列不存在
    ("SELECT * FROM nosuchtable",           "42P01"),   # 表不存在
])
def test_mock_fakes_sqlstate_readable_via_pgcode(sql, expected):
    from repl._mock_db import mock_cursor
    with pytest.raises(Exception) as ei:
        mock_cursor(sql)
    # 上游写的就是这一行 —— 字段名换了就对接不上
    assert getattr(ei.value, "pgcode", None) == expected, ei.value
    # 原始报错文本必须留着:SqlFixer 还要拿它去改 SQL
    assert str(ei.value)


def test_mock_run_sql_accepts_meta_and_fills_on_truncation(monkeypatch):
    """evals/world.py 把 mcp_client.query_db 直接换成 mock_run_sql,
    所以签名必须对得上(上游一传 meta= 就 TypeError 的话,整条路当场断)。"""
    from repl._mock_db import mock_run_sql
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    monkeypatch.setattr(config, "SQL_MAX_ROWS", 3)

    meta: dict = {}
    rows = mock_run_sql("SELECT id FROM video_fact_instances", meta)
    assert len(rows) == 3
    assert meta["truncated"] is True
    assert meta["reason"] == "row_cap"
    assert meta["returned"] == 3
    assert meta["total_seen"] == 4


def test_mock_run_sql_leaves_meta_untouched_when_not_truncated(monkeypatch):
    from repl._mock_db import mock_run_sql
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    meta: dict = {}
    rows = mock_run_sql("SELECT COUNT(*) AS n FROM video_metadata", meta)
    assert rows and rows[0]["n"] > 0
    assert "truncated" not in meta, meta      # 没截断就不动截断四键


# ══════════════════════════════════════════════════════════════
#  wire 三形状 + USE_BOUNDED_SQL 只管展示
# ══════════════════════════════════════════════════════════════

def _call(tool, args):
    import mcp_server.server as S
    blocks = asyncio.run(S.call_tool(tool, args))
    return json.loads(blocks[0].text)


@pytest.fixture
def mock_server(monkeypatch):
    import mcp_server.server as S
    monkeypatch.setattr(S, "USE_MOCK_DB", True)
    return S


def test_wire_flag_off_is_bare_array_even_when_truncated(mock_server, monkeypatch):
    """开关关 → wire 与今天逐字节等价(裸数组),但【上界照样生效】。
    §12:回滚只回滚展示,不回滚安全。"""
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", False)
    monkeypatch.setattr(config, "SQL_MAX_ROWS", 4)
    data = _call("query_db", {"sql": "SELECT id FROM video_fact_instances"})
    assert isinstance(data, list)             # 裸数组,没有信封
    assert len(data) == 4                     # 但确实被截断了(1300 → 4)


def test_wire_is_byte_identical_to_legacy_when_not_truncated(mock_server, monkeypatch):
    """"没截断的路必须与今天逐字节等价" —— 这条按字面验:
    拿改之前那行代码(`json.dumps([dict(r) for r in cur.fetchall()], ...)`)
    自己算一遍,和现在服务端真正吐出的文本比【字符串相等】,不是比解析后的对象。
    键序、分隔符空格、default=str 的落地形式,任何一样漂了都会红。
    """
    import mcp_server.server as S
    from repl._mock_db import mock_cursor

    sql = ("SELECT vm.video_id, vm.title, vm.duration_sec, vf.predicate, vf.confidence "
           "FROM video_metadata vm JOIN video_facts vf ON vm.video_id = vf.video_id "
           "ORDER BY vm.video_id, vf.predicate LIMIT 25")

    legacy = json.dumps([dict(r) for r in mock_cursor(sql).fetchall()],
                        ensure_ascii=False, default=str)

    for flag in (False, True):        # 未截断时开关开也不该动 wire
        monkeypatch.setattr(config, "USE_BOUNDED_SQL", flag)
        actual = asyncio.run(S.call_tool("query_db", {"sql": sql}))[0].text
        assert actual == legacy, f"USE_BOUNDED_SQL={flag} 时 wire 变了"


def test_wire_flag_on_truncated_emits_envelope(mock_server, monkeypatch):
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    monkeypatch.setattr(config, "SQL_MAX_ROWS", 4)
    data = _call("query_db", {"sql": "SELECT id FROM video_fact_instances"})
    assert isinstance(data, dict)
    assert len(data["rows"]) == 4
    assert data["truncated"] is True
    assert data["returned"] == 4
    assert data["total_seen"] == 5
    assert data["reason"] == "row_cap"


def test_flag_does_not_change_the_row_set(mock_server, monkeypatch):
    """开关是展示开关,不是安全开关:同一条 SQL 在开/关两态下行集必须一致。
    (信封开销恒定扣除就是为了这条 —— 否则开关会偷偷改变返回多少行。)"""
    monkeypatch.setattr(config, "SQL_MAX_ROWS", 7)
    sql = {"sql": "SELECT id FROM video_fact_instances"}

    monkeypatch.setattr(config, "USE_BOUNDED_SQL", False)
    off = _call("query_db", sql)
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    on = _call("query_db", sql)

    assert off == on["rows"]


def test_wire_untruncated_nonempty_stays_bare_array(mock_server, monkeypatch):
    """非空且没截断 —— 哪怕开关开着也不动 wire(列名从行 key 就看得出来)。"""
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    data = _call("query_db", {"sql": "SELECT COUNT(*) AS n FROM video_metadata"})
    assert isinstance(data, list)
    assert data[0]["n"] > 0


def test_wire_empty_result_carries_columns_when_flag_on(mock_server, monkeypatch):
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    data = _call("query_db", {
        "sql": "SELECT video_id, title FROM video_metadata WHERE video_id = 'zzz'"})
    assert data["rows"] == []
    assert data["columns"] == ["video_id", "title"]
    assert "truncated" not in data            # 零行不是截断


def test_wire_error_shape_unchanged_plus_pgcode(mock_server, monkeypatch):
    monkeypatch.setattr(config, "USE_BOUNDED_SQL", True)
    data = _call("query_db", {"sql": "SELECT * FROM nosuchtable"})
    assert set(data) <= {"error", "pgcode"}
    assert "no such table" in data["error"]
    assert data["pgcode"] == "42P01"


def test_wire_non_readonly_still_refused(mock_server):
    data = _call("query_db", {"sql": "DELETE FROM video_metadata"})
    assert "error" in data and "只读" in data["error"]


# ══════════════════════════════════════════════════════════════
#  真 PG 路的护栏(没有真库,锁住"到底发了哪几条语句")
# ══════════════════════════════════════════════════════════════

class RecordingCursor:
    def __init__(self):
        self.sql: list[str] = []
        self.description = ()

    def execute(self, s, *a):
        self.sql.append(s)

    def fetchmany(self, n):
        return []


def test_apply_timeouts_issues_both_set_local(monkeypatch):
    """B2 的整条顺序依赖都压在这两句上 —— GUC 名字打错、单位写错、
    或者哪天被人删掉一句,客户端 15s 超时就会变成"客户端先放弃"的悬挂查询。
    没有真库可连,至少把发出去的语句文本锁死。"""
    import mcp_server.server as S
    monkeypatch.setattr(config, "SQL_STATEMENT_TIMEOUT_MS", 10000)
    monkeypatch.setattr(config, "SQL_LOCK_TIMEOUT_MS", 2000)

    cur = RecordingCursor()
    S._apply_timeouts(cur)
    assert cur.sql == ["SET LOCAL statement_timeout = 10000",
                       "SET LOCAL lock_timeout = 2000"]


def test_timeouts_are_set_local_not_session():
    """SET(不带 LOCAL)会把超时泄漏给这条连接后续的所有查询。"""
    import mcp_server.server as S
    cur = RecordingCursor()
    S._apply_timeouts(cur)
    assert all(s.startswith("SET LOCAL ") for s in cur.sql), cur.sql


def test_begin_readonly_marks_connection_readonly():
    import mcp_server.server as S

    class FakeConn:
        def __init__(self):
            self.kw = None

        def set_session(self, **kw):
            self.kw = kw

    conn = FakeConn()
    S._begin_readonly(conn)
    assert conn.kw == {"readonly": True}


def test_error_payload_keeps_message_and_adds_pgcode():
    import mcp_server.server as S

    class Boom(Exception):
        pgcode = "57014"      # query_canceled(statement_timeout)

    assert S._error_payload(Boom("canceling statement due to statement timeout")) == {
        "error": "canceling statement due to statement timeout", "pgcode": "57014"}
    # 没有 SQLSTATE 时形状与今天完全一样,不多这个键
    assert S._error_payload(ValueError("plain")) == {"error": "plain"}


# ══════════════════════════════════════════════════════════════
#  mcp_client 归一化:三形状 → (list, meta)
# ══════════════════════════════════════════════════════════════

def test_unwrap_bare_array_leaves_meta_untouched():
    meta: dict = {}
    assert mcp_client._unwrap([{"a": 1}], meta) == [{"a": 1}]
    assert meta == {}


def test_unwrap_envelope_fills_meta():
    meta: dict = {}
    rows = mcp_client._unwrap(
        {"rows": [{"a": 1}], "columns": ["a"], "truncated": True,
         "total_seen": 2001, "returned": 1, "reason": "row_cap"}, meta)
    assert rows == [{"a": 1}]
    assert meta["truncated"] is True
    assert meta["total_seen"] == 2001
    assert meta["returned"] == 1
    assert meta["reason"] == "row_cap"
    assert meta["columns"] == ["a"]


def test_unwrap_empty_envelope_gives_columns_but_no_truncation_keys():
    meta: dict = {}
    assert mcp_client._unwrap({"rows": [], "columns": ["video_id"]}, meta) == []
    assert meta == {"columns": ["video_id"]}


def test_unwrap_tolerates_meta_none():
    assert mcp_client._unwrap({"rows": [{"a": 1}], "truncated": True}, None) == [{"a": 1}]


def test_db_error_carries_pgcode_but_same_message():
    err = mcp_client._db_error("boom", {"error": "boom", "pgcode": "57014"})
    assert str(err) == "boom"                       # 消息文本与今天一致
    assert getattr(err, "pgcode", None) == "57014"  # 上游按这一行分类
    plain = mcp_client._db_error("boom", {"error": "boom"})
    assert getattr(plain, "pgcode", None) is None


# ══════════════════════════════════════════════════════════════
#  B2 客户端超时顺序
# ══════════════════════════════════════════════════════════════

def test_client_timeout_outlives_server_statement_timeout():
    """服务端必须【先】放弃,否则会留下悬挂查询(客户端不等了、SQL 还在跑,
    连接不归还、锁不释放,上游一重试就滚成 N 条并发慢查询)。"""
    assert config.MCP_CALL_TIMEOUT_S >= config.SQL_STATEMENT_TIMEOUT_MS / 1000.0 + 5.0
    assert config.MCP_CALL_TIMEOUT_S < 60      # 确实比改之前的 60s 收紧了


def test_client_timeout_floor_cannot_be_configured_away():
    """把 env 设成荒唐的小值也不能让客户端比服务端先放弃。

    跑在子进程里:这条要验的是 config 【import 时】算出来的下界,
    而 reload(pipeline.config) 会把中央配置在整个测试进程里换一遍 ——
    为了一条断言去动全局状态,不划算。
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ,
               PYTHONUTF8="1",
               MCP_CALL_TIMEOUT_S="3",          # 荒唐的小值
               SQL_STATEMENT_TIMEOUT_MS="10000")
    out = subprocess.run(
        [sys.executable, "-c",
         "from pipeline import config; print(config.MCP_CALL_TIMEOUT_S)"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert float(out.stdout.strip()) == 15.0, out.stdout


# ══════════════════════════════════════════════════════════════
#  信封形状本身
# ══════════════════════════════════════════════════════════════

def test_needs_envelope_matrix():
    full = fetch_bounded(FakeCursor(_rows(3)))
    empty = fetch_bounded(FakeCursor([], columns=["a"]))
    cut = fetch_bounded(FakeCursor(_rows(9)), max_rows=2)

    assert needs_envelope(full, report=True) is False    # 非空未截断 → 裸数组
    assert needs_envelope(empty, report=True) is True    # 零行 → 要带列名
    assert needs_envelope(cut, report=True) is True      # 截断 → 要报
    for res in (full, empty, cut):
        assert needs_envelope(res, report=False) is False  # 开关关 → 永远裸数组


def test_build_envelope_omits_truncation_keys_when_not_truncated():
    env = build_envelope(fetch_bounded(FakeCursor([], columns=["a"])))
    assert env == {"rows": [], "columns": ["a"]}
