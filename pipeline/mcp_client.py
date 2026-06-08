"""
真正的 MCP stdio 客户端 —— 替换掉旧代码里 `*_via_mcp` 的"假 MCP"(直连 psycopg2)。

对外提供两个同步方法,签名与旧 fetch_schema / run_sql 完全一致:
    get_schema() -> dict
    query_db(sql) -> list[dict]

内部:
    - spawn `python -m mcp_server.server` 作为子进程,通过 stdio 跑标准 MCP 协议
    - 维持一个持久 session(整条流水线复用一次连接,避免每次查询重启子进程)
    - Windows 上子进程需要 ProactorEventLoop;client 在独立线程里建专用 loop,
      用 run_coroutine_threadsafe 把同步调用桥接到 async MCP SDK

这样 Stage 3 (MCP) 被真实使用:Planner 拿 schema、节点执行查 DB,全走协议。
mock 模式(REPL_USE_MOCK_DB=1)下子进程后端自动切内存 SQLite,无需 AlloyDB。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from typing import Any, Optional

from pipeline import config

log = logging.getLogger("pipeline.mcp_client")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MCPClient:
    """持久化的 MCP stdio 客户端(同步外壳 + 后台 async loop)。"""

    _singleton: Optional["MCPClient"] = None

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._ready = threading.Event()
        self._closing: Optional[asyncio.Event] = None
        self._start_error: Optional[BaseException] = None
        self._start()

    # ── 单例(整条流水线共享一个连接) ──
    @classmethod
    def shared(cls) -> "MCPClient":
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    # ── 生命周期 ──
    def _start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="mcp-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=40):
            raise RuntimeError("MCP server 启动超时(40s)")
        if self._start_error is not None:
            raise RuntimeError(f"MCP server 启动失败: {self._start_error!r}")

    def _run_loop(self) -> None:
        # Windows: 子进程必须用 Proactor loop;Selector loop 不支持 subprocess
        if sys.platform == "win32":
            self._loop = asyncio.ProactorEventLoop()
        else:
            self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as e:  # 启动阶段任何异常都要回报给主线程
            self._start_error = e
            self._ready.set()

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # 子进程继承当前环境(含 REPL_USE_MOCK_DB / ALLOYDB_PASSWORD)
        env = dict(os.environ)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.server"],
            env=env,
            cwd=_REPO_ROOT,
        )
        self._closing = asyncio.Event()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                log.info("MCP session 就绪 (mock=%s)", config.USE_MOCK_DB)
                self._ready.set()
                await self._closing.wait()   # 保持连接,直到 close()

    def close(self) -> None:
        if self._loop and self._closing and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._closing.set)
        if self._thread:
            self._thread.join(timeout=5)
        if MCPClient._singleton is self:
            MCPClient._singleton = None

    # ── 同步桥接 ──
    def _call(self, coro) -> Any:
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=60)

    async def _call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments)
        # MCP TextContent → 取首个文本块
        for block in result.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    # ── 对外 API(与旧 fetch_schema / run_sql 同签名) ──
    def get_schema(self) -> dict:
        text = self._call(self._call_tool("get_schema", {}))
        data = json.loads(text)
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"get_schema 失败: {data['error']}")
        return data

    def query_db(self, sql: str) -> list[dict]:
        text = self._call(self._call_tool("query_db", {"sql": sql}))
        data = json.loads(text)
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(data["error"])
        return data


# ── 便捷函数(默认走共享单例) ─────────────────

def get_schema() -> dict:
    return MCPClient.shared().get_schema()


def query_db(sql: str) -> list[dict]:
    return MCPClient.shared().query_db(sql)
