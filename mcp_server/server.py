#!/usr/bin/env python3
"""
第3阶段：MCP Server
暴露 get_schema() 和 query_db() 两个工具
让第4阶段 Planner / 第6阶段 Code Generator 能发现并安全查询 AlloyDB。

backend 由 REPL_USE_MOCK_DB 切换:
    未设      → 连真 AlloyDB(psycopg2)
    =1/true   → 走内存 SQLite mock(repl._mock_db),零成本、不需要 AlloyDB

这样 pipeline.mcp_client 永远走真正的 MCP stdio 协议,只是 server 的后端可换,
Stage 3 在 mock 模式下也能被真实使用、可测试。
"""

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# 让 `python -m mcp_server.server` 与直接 spawn 都能 import 到同级包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import config
from pipeline.sql_bounds import build_envelope, fetch_bounded, needs_envelope

# ══════════════════════════════════════════
#  配置(集中到 pipeline.config)
# ══════════════════════════════════════════
USE_MOCK_DB = config.USE_MOCK_DB
# ══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,   # stdout 留给 MCP 协议,日志只走 stderr
)
log = logging.getLogger("mcp-server")

app = Server("alloydb-mcp")


def get_conn():
    import psycopg2
    return psycopg2.connect(**config.alloydb_dsn())


# ── B1 服务端护栏:只读事务 + 语句/锁超时 ──────────────────────────

def _begin_readonly(conn) -> None:
    """把连接压成只读。

    与 `sql_guard.is_read_only()` 是两层不同的防线,不是重复:
    前者是【解析文本】猜这条 SQL 想干嘛(可被 CTE/函数绕过),这里是让
    Postgres 自己拒绝任何写 —— 解析器看走眼时,DB 仍然会拦下来。
    """
    conn.set_session(readonly=True)


def _apply_timeouts(cur) -> None:
    """`SET LOCAL statement_timeout / lock_timeout`。

    为什么是 SET LOCAL 而不是 SET:LOCAL 只活到本事务结束,连接归还/复用时
    自动失效,不会把超时设置泄漏给后续查询。psycopg2 在第一条 execute 前会
    隐式 BEGIN,所以把这两条放在业务 SQL 之前,它们和业务 SQL 在同一个事务里。

    值用 int() 硬转后拼进语句:PG 的 SET 不吃占位符,而 int() 之后不存在注入面。

    【这是 B2 的前置条件】:客户端超时(config.MCP_CALL_TIMEOUT_S)敢往下收,
    唯一的依据就是服务端会先放弃。顺序反了就会出现"客户端不等了、SQL 还在跑"
    的悬挂查询 —— 连接不还、锁不放,上游一重试就变成 N 条并发慢查询。
    """
    cur.execute(f"SET LOCAL statement_timeout = {int(config.SQL_STATEMENT_TIMEOUT_MS)}")
    cur.execute(f"SET LOCAL lock_timeout = {int(config.SQL_LOCK_TIMEOUT_MS)}")


def _error_payload(e: Exception) -> dict:
    """错误 wire。`error` 的取值与今天【逐字节一致】,只在 DB 给了 SQLSTATE 时
    附加一个 `pgcode` 键。

    为什么要加:上游按 SQLSTATE 决定"这个错该不该叫 LLM 重写 SQL"
    (42601/42703/42P01 该,57014 超时不该 —— 否则是烧钱死循环)。
    异常对象过不了 stdio,SQLSTATE 不搭这趟车的话,分类逻辑在【生产】上
    永远读到 None,B1 设的 statement_timeout 也就永远分不出类。
    纯增字段:老消费者只读 `error`,拿到的字符串一个字节都没变。
    """
    payload = {"error": str(e)}
    code = getattr(e, "pgcode", None)
    if code:
        payload["pgcode"] = str(code)
    return payload


# ── 声明工具 ──────────────────────────────

@app.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="get_schema",
            description="返回 AlloyDB 中所有业务表的列名和数据类型，用于了解数据库结构，防止列名幻觉",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        types.Tool(
            name="query_db",
            description="执行只读 SQL 查询，返回 JSON 格式结果。只允许 SELECT 语句",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "要执行的 SELECT SQL 语句"
                    }
                },
                "required": ["sql"]
            }
        )
    ]

# ── 执行工具 ──────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict):

    # ── get_schema ────────────────────────
    if name == "get_schema":
        try:
            if USE_MOCK_DB:
                from repl._mock_db import mock_fetch_schema
                schema = mock_fetch_schema()
                log.info("[mock] get_schema() 返回 %d 张表", len(schema))
            else:
                conn = get_conn()
                try:
                    # get_schema 也要设超时:B2 收紧的是【整个 MCP 调用】的客户端
                    # 超时,不区分工具。这里不设的话,一条卡住的 information_schema
                    # 查询照样能造出"客户端已放弃、服务端还在跑"的悬挂查询。
                    _begin_readonly(conn)
                    cols = ",".join(f"'{t}'" for t in config.BUSINESS_TABLES)
                    with conn.cursor() as cur:
                        _apply_timeouts(cur)
                        cur.execute(f"""
                            SELECT table_name, column_name, data_type
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                            AND table_name IN ({cols})
                            ORDER BY table_name, ordinal_position
                        """)
                        rows = cur.fetchall()
                finally:
                    conn.close()
                schema = {}
                for table, column, dtype in rows:
                    schema.setdefault(table, []).append({"column": column, "type": dtype})
                log.info("get_schema() 调用成功，返回 %d 张表", len(schema))

            return [types.TextContent(
                type="text",
                text=json.dumps(schema, ensure_ascii=False, indent=2)
            )]
        except Exception as e:
            log.error("get_schema() 失败: %s", e)
            return [types.TextContent(type="text", text=json.dumps(_error_payload(e)))]

    # ── query_db ──────────────────────────
    elif name == "query_db":
        sql = arguments.get("sql", "").strip()

        if not sql:
            return [types.TextContent(type="text", text=json.dumps({"error": "sql 参数不能为空"}))]

        from pipeline.sql_guard import is_read_only
        if not is_read_only(sql):
            log.warning("拒绝非只读语句: %s", sql[:50])
            return [types.TextContent(type="text", text=json.dumps({"error": "只允许只读查询(SELECT / WITH ... SELECT)，不允许写操作"}))]

        try:
            if USE_MOCK_DB:
                from repl._mock_db import mock_cursor
                cur = mock_cursor(sql)
                res = fetch_bounded(cur)          # 与真库【同一份】上界实现
                log.info("[mock] query_db() 返回 %d 行", res.returned)
            else:
                import psycopg2.extras
                conn = get_conn()
                try:
                    _begin_readonly(conn)
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        _apply_timeouts(cur)
                        cur.execute(sql)
                        res = fetch_bounded(cur)  # 必须在 cursor 还开着时读
                finally:
                    conn.close()
                log.info("query_db() 返回 %d 行", res.returned)

            if res.truncated:
                log.warning("query_db() 触发上界: reason=%s 扫到>=%d 行,只返回 %d 行",
                            res.reason, res.total_seen, res.returned)

            # wire 三形状之一。USE_BOUNDED_SQL 关(默认)且未截断 → 裸 JSON 数组,
            # 与今天【逐字节等价】;开关只放行"报告截断/零行列名"这个展示行为,
            # 上面的行数/字节/超时上界不受它控制(§12:回滚只回滚展示,不回滚安全)。
            payload = (build_envelope(res)
                       if needs_envelope(res, report=config.USE_BOUNDED_SQL)
                       else res.rows)
            return [types.TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, default=str)
            )]
        except Exception as e:
            log.error("query_db() 执行失败: %s", e)
            return [types.TextContent(type="text", text=json.dumps(_error_payload(e)))]

    else:
        return [types.TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}))]

# ── 启动 ──────────────────────────────────

async def main():
    log.info("MCP Server 启动，等待连接...")
    async with stdio_server() as streams:
        await app.run(*streams, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
