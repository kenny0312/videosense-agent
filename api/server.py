"""
Stage 10 —— 端到端编排 API。

    POST /v1/video_vibe_query
    Body:  {"query": "自然语言问题"}
    Resp:  {
        ok, answer,
        dag,               # Planner 生成的执行蓝图(可审计)
        generated_code,    # 每个沙箱节点最终版 Python(自愈后)
        plot_url,          # 图表 URL(gs:// 或 file://),无图则 null
        trace, trace_summary
    }

本地启动:
    uvicorn api.server:app --port 8000 --reload
环境变量同 pipeline.main(REPL_USE_MOCK_DB / ALLOYDB_PASSWORD / SANDBOX_URL ...)。

注意:endpoint 用同步 def,FastAPI 自动放线程池执行 —— 避免阻塞事件循环,
也避开与 MCP 客户端后台 loop / Vertex AI 阻塞调用的冲突。
"""
from __future__ import annotations

import uuid
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="vertexai.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="vertexai.*")

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import artifacts, config
from pipeline.orchestrator import run_query

app = FastAPI(title="video_vibe_query", version="1.0")

# 把本地 artifacts/ 目录挂成静态服务 —— 生成的图表用浏览器直接打开
os.makedirs(artifacts.LOCAL_DIR, exist_ok=True)
app.mount("/plots", StaticFiles(directory=artifacts.LOCAL_DIR), name="plots")


class VibeQueryRequest(BaseModel):
    query: str = Field(..., description="自然语言视频分析问题")


@app.get("/health")
def health():
    return {"status": "ok", "mode": "mock" if config.USE_MOCK_DB else "alloydb"}


@app.post("/v1/video_vibe_query")
def video_vibe_query(req: VibeQueryRequest, request: Request):
    result = run_query(req.query, quiet_trace=True)

    # 图表产物:沙箱产出的图像(svg/png)→ 存本地 → 返回浏览器可打开的 http URL
    plot_url = None
    plot = result.pop("plot", {}) or {}
    if plot:
        fname = artifacts.save_local(plot, name=uuid.uuid4().hex[:12])
        if fname:
            plot_url = str(request.base_url).rstrip("/") + f"/plots/{fname}"

    result["plot_url"] = plot_url
    return result
