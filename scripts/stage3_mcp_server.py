#!/usr/bin/env python3
"""
第3阶段：MCP Server
暴露 get_schema() 和 query_db() 两个工具
让第4阶段 Planner 能发现并安全查询 AlloyDB
"""

import asyncio
import json
import logging
import psycopg2
import psycopg2.extras
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ══════════════════════════════════════════
#  配置
# ══════════════════════════════════════════
ALLOYDB_IP       = "your-db-host"
ALLOYDB_DB       = "your_database"
ALLOYDB_USER     = "postgres"
# 密码从环境变量读取，不硬编码
import os
ALLOYDB_PASSWORD = os.environ.get("ALLOYDB_PASSWORD", "")
# ══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("mcp-server")

app = Server("alloydb-mcp")

def get_conn():
    return psycopg2.connect(
        host=ALLOYDB_IP,
        port=5432,
        dbname=ALLOYDB_DB,
        user=ALLOYDB_USER,
        password=ALLOYDB_PASSWORD,
        sslmode="require"
    )

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
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    AND table_name IN (
                        'video_metadata',
                        'video_discovery',
                        'video_facts',
                        'video_fact_instances'
                    )
                    ORDER BY table_name, ordinal_position
                """)
                rows = cur.fetchall()

            schema = {}
            for table, column, dtype in rows:
                if table not in schema:
                    schema[table] = []
                schema[table].append({"column": column, "type": dtype})

            log.info("get_schema() 调用成功，返回 %d 张表", len(schema))
            return [types.TextContent(
                type="text",
                text=json.dumps(schema, ensure_ascii=False, indent=2)
            )]
        except Exception as e:
            log.error("get_schema() 失败: %s", e)
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
        finally:
            conn.close()

    # ── query_db ──────────────────────────
    elif name == "query_db":
        sql = arguments.get("sql", "").strip()

        if not sql:
            return [types.TextContent(type="text", text=json.dumps({"error": "sql 参数不能为空"}))]

        if not sql.upper().startswith("SELECT"):
            log.warning("拒绝非 SELECT 语句: %s", sql[:50])
            return [types.TextContent(type="text", text=json.dumps({"error": "只允许 SELECT 查询，不允许写操作"}))]

        conn = get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                result = [dict(r) for r in rows]

            log.info("query_db() 返回 %d 行", len(result))
            return [types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, default=str)
            )]
        except Exception as e:
            log.error("query_db() 执行失败: %s", e)
            return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
        finally:
            conn.close()

    else:
        return [types.TextContent(type="text", text=json.dumps({"error": f"未知工具: {name}"}))]

# ── 启动 ──────────────────────────────────

async def main():
    log.info("MCP Server 启动，等待连接...")
    async with stdio_server() as streams:
        await app.run(*streams, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
