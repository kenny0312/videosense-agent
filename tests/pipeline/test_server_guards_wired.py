"""B1/B2 最承重的那条不变量:服务端护栏【真的被调用了】。

任务书唯一写死顺序的约束是「服务端 statement_timeout 必须先于客户端超时收紧」——
否则会造出"客户端 15s 放弃了、PG 那条 SQL 还在跑"的悬挂查询。这条不变量的四个环节里:

  ① GUC 名字与单位          —— test_bounded_sql 已锁(直接调 _apply_timeouts)
  ② SET LOCAL 而不是 SET     —— 同上
  ③ 客户端 ≥ 服务端 + 5s     —— 同上
  ④ **这两个函数有没有被真的调到** —— 【原来没人守】

第 ④ 环最承重也最容易掉:任务书 §5 已经排期了 named cursor 改造,要动的正是
`with conn.cursor(...)` 那个块;顺手把 `_begin_readonly` / `_apply_timeouts` 的调用
丢掉,测试一条都不会红,生产立刻回到"没有超时"。实测过:把四个调用点全删成 pass,
全套件 753 passed 与基线逐条一致。

所有既有 call_tool 测试都走 USE_MOCK_DB=True 的假库分支,真库那条 `else` 在整个套件里
从未被执行 —— 这条用假连接把它走一遍。
"""
from __future__ import annotations

import asyncio
import json
import sys
import types


from pipeline import config


class _FakeCursor:
    """只记 execute,不真跑。description/fetchmany 满足 fetch_bounded 的最小契约。"""

    def __init__(self, rec, rows):
        self._rec = rec
        self._rows = list(rows)
        self.description = [("id",)]

    def execute(self, sql, *a):
        self._rec["stmts"].append(sql)

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self):
        raise AssertionError("有界读取绝不该调 fetchall")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rec, rows=()):
        self._rec = rec
        self._rows = rows

    def set_session(self, **kw):
        self._rec["session"] = kw

    def cursor(self, **kw):
        return _FakeCursor(self._rec, self._rows)

    def close(self):
        self._rec["closed"] = True


def _fake_psycopg2(monkeypatch):
    """假 psycopg2:服务端只用它取 RealDictCursor 这个 kwarg。

    `import psycopg2.extras` 会绑定 `psycopg2` 这个名字再取 `.extras` 属性,
    所以父模块上必须【真的挂着】子模块 —— 只往 sys.modules 里塞两条不够。
    (这样 CI 上没装 psycopg2 也能跑这条真库路径的用例。)
    """
    parent = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    parent.extras = extras
    monkeypatch.setitem(sys.modules, "psycopg2", parent)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)


def _drive(monkeypatch, tool, args, rows=()):
    """走真库那条 else 分支跑一次 call_tool,回 (记录, 解析后的响应)。"""
    import mcp_server.server as S

    rec: dict = {"stmts": [], "session": None, "closed": False}
    monkeypatch.setattr(S, "USE_MOCK_DB", False)
    monkeypatch.setattr(S, "get_conn", lambda: _FakeConn(rec, rows))
    # psycopg2.extras 只被用来取 RealDictCursor 这个 kwarg,给个假的即可(CI 无 psycopg2 也能跑)
    _fake_psycopg2(monkeypatch)
    out = asyncio.run(S.call_tool(tool, args))
    return rec, out


def test_query_db_actually_applies_guards_in_order(monkeypatch):
    """只读事务先开、两条 SET LOCAL 再发、最后才是业务 SQL —— 顺序也要对。

    顺序有意义:SET LOCAL 必须和业务 SQL 在【同一个事务】里才生效,
    所以它得排在业务 SQL 之前、且在 psycopg2 隐式 BEGIN 之后。
    """
    rec, _ = _drive(monkeypatch, "query_db", {"sql": "SELECT 1"}, rows=[{"id": 1}])

    assert rec["session"] == {"readonly": True}, (
        "_begin_readonly 没被调用 —— sql_guard 解析走眼时就没有第二道防线了")
    assert rec["stmts"] == [
        f"SET LOCAL statement_timeout = {int(config.SQL_STATEMENT_TIMEOUT_MS)}",
        f"SET LOCAL lock_timeout = {int(config.SQL_LOCK_TIMEOUT_MS)}",
        "SELECT 1",
    ], f"护栏没被调用 / 顺序不对 / GUC 文本漂了:{rec['stmts']}"
    assert rec["closed"] is True, "连接没归还"


def test_guards_are_set_local_not_session_wide(monkeypatch):
    """SET(会话级)会把超时泄漏给连接上的后续查询 —— 连接池复用时污染别人。"""
    rec, _ = _drive(monkeypatch, "query_db", {"sql": "SELECT 1"}, rows=[])
    for s in rec["stmts"][:2]:
        assert s.startswith("SET LOCAL "), f"用了会话级 SET,会泄漏到后续查询:{s}"


def test_client_timeout_leaves_room_for_the_server(monkeypatch):
    """客户端必须比服务端多留余量,否则客户端先放弃 → 悬挂查询(PG 那边还在跑)。"""
    slack = config.MCP_CALL_TIMEOUT_S - config.SQL_STATEMENT_TIMEOUT_MS / 1000.0
    assert slack >= 5, (
        f"客户端 {config.MCP_CALL_TIMEOUT_S}s 只比服务端 "
        f"{config.SQL_STATEMENT_TIMEOUT_MS / 1000.0}s 多 {slack}s —— "
        "网络 RTT + 序列化 + stdio 往返吃掉之后,客户端会先放弃")


def test_guards_survive_a_failing_query(monkeypatch):
    """业务 SQL 抛错时连接仍要归还 —— 否则连接池会被慢慢耗干。"""
    import mcp_server.server as S

    rec: dict = {"stmts": [], "session": None, "closed": False}

    class _Boom(_FakeCursor):
        def execute(self, sql, *a):
            self._rec["stmts"].append(sql)
            if not sql.startswith("SET LOCAL"):
                raise RuntimeError("boom")

    class _Conn(_FakeConn):
        def cursor(self, **kw):
            return _Boom(self._rec, [])

    monkeypatch.setattr(S, "USE_MOCK_DB", False)
    monkeypatch.setattr(S, "get_conn", lambda: _Conn(rec))
    _fake_psycopg2(monkeypatch)

    out = asyncio.run(S.call_tool("query_db", {"sql": "SELECT boom"}))
    assert rec["closed"] is True, "查询失败时连接没归还"
    assert "error" in json.loads(out[0].text)
