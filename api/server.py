"""
Stage 10 —— 端到端编排 API。

    POST /v1/video_vibe_query
    Body:  {"query": "自然语言问题", "session_id": "可选"}
    Resp:  {
        ok, status, answer,
        dag,               # Planner 生成的执行蓝图(可审计)
        generated_code,    # 每个沙箱节点最终版 Python(自愈后)
        plot_url,          # 图表 URL(http/gs://),无图则 null
        trace, trace_summary, session_id, turn_type
    }

    GET  /                前端单页(web/index.html):气泡式多轮对话 + 富渲染

本地启动:
    uvicorn api.server:app --port 8000 --reload
环境变量同 pipeline.main(REPL_USE_MOCK_DB / ALLOYDB_PASSWORD / SANDBOX_URL ...)。

注意:endpoint 用同步 def,FastAPI 自动放线程池执行 —— 避免阻塞事件循环,
也避开与 MCP 客户端后台 loop / Vertex AI 阻塞调用的冲突。
"""
from __future__ import annotations

import base64
import secrets
import uuid
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="vertexai.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="vertexai.*")

import os
import threading
import weakref

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import artifacts, config
from pipeline.orchestrator import run_query
from pipeline.session import STORE

app = FastAPI(title="VideoSense Agent", version="1.0")

# 把本地 artifacts/ 目录挂成静态服务 —— 生成的图表用浏览器直接打开
os.makedirs(artifacts.LOCAL_DIR, exist_ok=True)
app.mount("/plots", StaticFiles(directory=artifacts.LOCAL_DIR), name="plots")

# 前端单页:气泡式多轮对话 + 富渲染(表格/图表/DAG/SQL/trace)。GET / 直接发它。
_INDEX_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")

# ── 最小鉴权(B 方案):设了 APP_ACCESS_KEYS(逗号分隔的口令)才生效;不设 = 无鉴权(本地开发)。
# 对外暴露(Cloud Run 等)前务必设它。/health 始终放行,供探活。
_ACCESS_KEYS = [k.strip() for k in os.environ.get("APP_ACCESS_KEYS", "").split(",") if k.strip()]
_OPEN_PATHS = {"/health"}


@app.middleware("http")
async def _gate(request: Request, call_next):
    if _ACCESS_KEYS and request.url.path not in _OPEN_PATHS:
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:                       # Basic 里 password 部分当口令(用户名随便填)
                pwd = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2]
                ok = any(secrets.compare_digest(pwd, k) for k in _ACCESS_KEYS)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="VideoSense"'})
    return await call_next(request)


class VibeQueryRequest(BaseModel):
    query: str = Field(..., description="自然语言视频分析问题")
    session_id: str | None = Field(
        None, description="多轮会话 id;省略则开新会话,响应会回传一个 session_id 供下一轮带上")


@app.get("/")
def index():
    return FileResponse(_INDEX_HTML)


@app.get("/health")
def health():
    return {"status": "ok", "mode": "mock" if config.USE_MOCK_DB else "alloydb"}


# 同会话请求在本进程内串行化 —— 端点是 sync def,FastAPI 放线程池并发执行;一次请求是
# read(get_or_create)→ mutate(run_query)→ write(save) 的非原子序列,两个同 session_id
# 请求重叠会"后写覆盖"整轮(丢一轮记忆)。每会话一把锁把这段串起来 → 单副本即安全。
# WeakValueDictionary:不再被持有的锁自动 GC,锁表不会无限增长。
# 跨副本(Cloud Run 多实例、无 session 亲和)仍可能后写覆盖 —— 部署建议开 session affinity
# 让同会话落同一副本;要严格跨副本原子再上 CAS/append-only(见 RedisSessionStore 注释)。
_session_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_session_locks_guard = threading.Lock()


def _session_lock(sid: str) -> threading.Lock:
    with _session_locks_guard:
        lk = _session_locks.get(sid)
        if lk is None:
            lk = threading.Lock()
            _session_locks[sid] = lk
        return lk


@app.post("/v1/video_vibe_query")
def video_vibe_query(req: VibeQueryRequest, request: Request):
    sid = req.session_id or uuid.uuid4().hex        # 没带 session_id → 开一个新会话
    with _session_lock(sid):                        # 同会话 read-modify-write 串行,防丢轮
        session = STORE.get_or_create(sid)
        result = run_query(req.query, quiet_trace=True, session=session)
        STORE.save(session)                         # 写时机:每请求一次(纯内存模式无操作)
    result["session_id"] = sid                      # 回传,客户端下一轮带上即可续聊

    # 图表产物:沙箱产出的图像(svg/png)→ 存本地 → 返回浏览器可打开的 http URL
    plot_url = None
    plot = result.pop("plot", {}) or {}
    if plot:
        fname = artifacts.save_local(plot, name=uuid.uuid4().hex[:12])
        if fname:
            plot_url = str(request.base_url).rstrip("/") + f"/plots/{fname}"

    result["plot_url"] = plot_url
    return result
