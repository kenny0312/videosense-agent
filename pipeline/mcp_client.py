"""
真正的 MCP stdio 客户端 —— 替换掉旧代码里 `*_via_mcp` 的"假 MCP"(直连 psycopg2)。

对外提供两个同步方法,签名与旧 fetch_schema / run_sql 完全一致:
    get_schema() -> dict
    query_db(sql, meta=None) -> list[dict]      # meta 是可选 out 参数,见函数 docstring

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
        # B2:客户端超时。这条【必须】晚于服务端 statement_timeout 落地 ——
        # 顺序反了(先收紧客户端、服务端还没设超时)就会造出悬挂查询:客户端
        # 不等了,PG 那条 SQL 还在跑,连接不归还、锁不释放,上游看到超时又去
        # 重试 → 一条慢查询滚成 N 条并发慢查询,比原来更糟。
        # config.MCP_CALL_TIMEOUT_S 自带 `>= statement_timeout + 5s` 的下界,
        # 保证"服务端先放弃"这个前提不会被 env 配置改掉。
        return fut.result(timeout=config.MCP_CALL_TIMEOUT_S)

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
            raise _db_error(f"get_schema 失败: {data['error']}", data)
        return data

    def query_db(self, sql: str, meta: dict | None = None) -> list[dict]:
        text = self._call(self._call_tool("query_db", {"sql": sql}))
        data = json.loads(text)
        if isinstance(data, dict) and "error" in data:
            raise _db_error(data["error"], data)
        return _unwrap(data, meta)


# ── wire 归一化(服务端三形状 → (list, meta))────────────────────────
# 服务端 `query_db` 只会吐三种形状:
#   ① 裸 JSON 数组                    —— 没截断(且 USE_BOUNDED_SQL 关时恒为此形)
#   ② {"rows": [...], "columns": [...], "truncated"?...}   —— 截断了,或零行要带列名
#   ③ {"error": "...", "pgcode"?: "..."}                   —— 出错
# 这里把 ①② 归一成"返回 list、截断信息填进 meta",③ 抛异常。

def _unwrap(data, meta: dict | None) -> list[dict]:
    if isinstance(data, dict) and "rows" in data:
        if meta is not None:
            if data.get("truncated"):
                # 只在真截断时写这四个键 —— 键的【存在】本身就是信号,
                # 不能有 truncated=False 这种半吊子填法。
                meta["truncated"] = True
                meta["total_seen"] = data.get("total_seen")
                meta["returned"] = data.get("returned")
                meta["reason"] = data.get("reason")
            if "columns" in data:
                meta["columns"] = data["columns"]
        return data["rows"]
    return data


def _db_error(message: str, data: dict) -> RuntimeError:
    """把 SQLSTATE 挂到异常对象上,让上游一行 `getattr(e, "pgcode", None)`
    同时吃真库(经 wire 带回 pgcode)、假库(mock 直接抛带 pgcode 的异常)。
    异常的【消息文本】与今天完全一致,只是多了个属性。"""
    err = RuntimeError(message)
    code = data.get("pgcode")
    if code:
        err.pgcode = code           # type: ignore[attr-defined]
    return err


# ── 便捷函数(默认走共享单例) ─────────────────

def get_schema() -> dict:
    return MCPClient.shared().get_schema()


def query_db(sql: str, meta: dict | None = None) -> list[dict]:
    """执行只读 SQL,返回 list[dict]。

    【返回类型没变】—— 所有既有调用点零迁移,没截断的路逐字节不变。

    `meta` 是可选的 out 参数:传一个 dict 进来,截断时函数把下面的键【填进去】。
        meta["truncated"]  = True
        meta["total_seen"] = 服务端实际扫到的行数(至少 cap+1)
        meta["returned"]   = 真正返回的行数
        meta["reason"]     = "row_cap" | "byte_cap" | "single_row_too_large"
    没截断就【不动】这个 dict 的截断四键;不传 meta = 行为与今天完全一致。
    (另有 meta["columns"]:服务端报了列名时填 —— 零行结果靠它区分
     "这张表没有匹配" 和 "我不知道有哪些列",见 sql_bounds.columns_of。)

    为什么用 out 参数而不是改返回类型:任务书要求"wire 格式不变 → 零消费者迁移",
    但服务端必须有办法把"我截断了"传出来。把返回值改成信封会波及每一个调用点,
    而 out 参数让【没截断的路一个字节都不变】,只有今天"要么静默返回全量、要么
    直接崩"的那条路多出信息。
    """
    rows = MCPClient.shared().query_db(sql, meta)
    return _mask_ts(rows)


# ── 评测用:时间戳掩码(GATE_TS_MASK_PREDICATE,默认空=不掩码,生产零影响)────────
# 为什么需要:gate 实验的 T2 题问"这个动作在第几秒",本意是考【看视频】的能力。
# 但库里 video_facts 已有 start_ts —— 试跑实测,大脑 12 次 sql_query、一个视频不看,
# 直接把时间戳抄出来答得有模有样。号称考感知的题变成了考 SQL,而且是"看起来有结果"
# 的那种无效(最危险)。掩码把被测谓词的时间戳在【返回给 agent 的行上】置空,
# 逼它真去看视频;gold 用的是库外预标,不受影响。
def _mask_ts(rows):
    import os
    preds = [p.strip() for p in os.environ.get("GATE_TS_MASK_PREDICATE", "").split(",")
             if p.strip()]
    if not preds or not isinstance(rows, list):
        return rows
    lowered = {p.lower() for p in preds}
    out = []
    for r in rows:
        if isinstance(r, dict) and str(r.get("predicate", "")).lower() in lowered:
            r = {**r, "start_ts": None, "end_ts": None}
        out.append(r)
    return out
