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
                    cols = ",".join(f"'{t}'" for t in config.BUSINESS_TABLES)
                    with conn.cursor() as cur:
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
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

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
                from repl._mock_db import mock_run_sql
                result = mock_run_sql(sql)
                log.info("[mock] query_db() 返回 %d 行", len(result))
            else:
                import psycopg2.extras
                conn = get_conn()
                try:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(sql)
                        result = [dict(r) for r in cur.fetchall()]
                finally:
                    conn.close()
                log.info("query_db() 返回 %d 行", len(result))

            return [types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, default=str)
            )]
        except Exception as e:
            log.error("query_db() 执行失败: %s", e)
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]

    else:
        return [types.TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}))]

# ── 启动 ──────────────────────────────────

async def main():
    log.info("MCP Server 启动，等待连接...")
    async with stdio_server() as streams:
        await app.run(*streams, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
