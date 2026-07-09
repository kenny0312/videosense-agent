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
import hashlib
import json
import logging
import secrets
import time
import uuid
import warnings

warnings.filterwarnings("ignore", category=UserWarning, module="vertexai.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="vertexai.*")

import os
import threading
import weakref

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
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

log = logging.getLogger("api.server")

# ── 最小鉴权(B 方案):设了 APP_ACCESS_KEYS(逗号分隔的口令)才生效;不设 = 无鉴权(本地开发)。
# 对外暴露(Cloud Run 等)前务必设它。/health 始终放行,供探活。
# 审计要记"谁",所以支持 name:key 格式 —— 命中哪个 key 就记成对应 name。
#   推荐:APP_ACCESS_KEYS="alice:k_9f3k2,bob:k_7x2qd"  → 审计里记 alice / bob
#   兼容:老的裸 key "k_9f3k2,k_7x2qd"               → 记成不可逆短标签 u_xxxxxx(绝不把口令写进日志)
def _parse_access_keys(raw: str) -> tuple[list[str], dict[str, str]]:
    keys: list[str] = []
    name_of: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, _, key = item.partition(":")
            name, key = name.strip(), key.strip()
        else:
            key, name = item, ""                      # 裸 key → 下面用 hash 短标签兜底
        if key:
            keys.append(key)
            name_of[key] = name or ("u_" + hashlib.sha256(key.encode()).hexdigest()[:6])
    return keys, name_of


_ACCESS_KEYS, _KEY_TO_NAME = _parse_access_keys(os.environ.get("APP_ACCESS_KEYS", ""))
_OPEN_PATHS = {"/health"}

# ── fail-closed:生产环境(APP_ENV=prod)必须设 APP_ACCESS_KEYS,否则拒绝启动 ──
# 防"忘设/写错口令 = 全站裸奔"(videosense-pyai 就是这么死的:allUsers + 无口令 → 匿名可烧钱)。
# Cloud Run 会把启动即抛的 revision 标为 unhealthy,不切流量,旧的好版本继续服务。
# 本地开发不设 APP_ENV(或设 APP_DEV_MODE=1)即可无鉴权跑。
if not _ACCESS_KEYS and os.environ.get("APP_ENV") == "prod" and os.environ.get("APP_DEV_MODE") != "1":
    raise RuntimeError(
        "APP_ACCESS_KEYS 未设置但 APP_ENV=prod —— 拒绝以无鉴权模式对外启动。"
        "请设 APP_ACCESS_KEYS='name:key,...'(或本地调试用 APP_DEV_MODE=1)。")


@app.middleware("http")
async def _gate(request: Request, call_next):
    request.state.app_user = "anon"                   # 默认:本地无鉴权 / 非受控路径
    if _ACCESS_KEYS and request.url.path not in _OPEN_PATHS:
        matched = None
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:                       # Basic 里 password 部分当口令(用户名随便填)
                pwd = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2]
                for k in _ACCESS_KEYS:
                    if secrets.compare_digest(pwd, k):
                        matched = k
                        break
            except Exception:
                matched = None
        if matched is None:
            return Response(status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="VideoSense"'})
        request.state.app_user = _KEY_TO_NAME.get(matched, "user")   # 记下"谁"供审计
    return await call_next(request)


class VibeQueryRequest(BaseModel):
    query: str = Field(..., description="自然语言视频分析问题")
    session_id: str | None = Field(
        None, description="多轮会话 id;省略则开新会话,响应会回传一个 session_id 供下一轮带上")
    pro_video: bool = Field(
        False, description="Pro 视频分析:本请求的 analyze_video 用更强的 pro 模型(更准、更慢)")
    image: str | None = Field(
        None, description="可选:粘贴的截图,data URL(data:image/png;base64,...)。作多模态输入附在本轮。")


# 粘贴图片:data URL → (bytes, mime)。限类型 + 大小(防超大 base64 撑爆请求/成本)。
_IMG_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_IMG_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))   # 解码后 8MB


def _parse_image(data_url: str | None) -> "tuple[bytes, str] | None":
    if not data_url or not data_url.startswith("data:"):
        return None
    try:
        head, _, b64 = data_url.partition(",")
        mime = head[5:].split(";")[0].strip().lower()
        if mime not in _IMG_MIMES or "base64" not in head:
            return None
        raw = base64.b64decode(b64, validate=True)
        if not raw or len(raw) > _MAX_IMG_BYTES:
            return None
        return raw, mime
    except Exception:
        return None


@app.get("/")
def index():
    return FileResponse(_INDEX_HTML)


@app.get("/health")
def health():
    # gated 供上线后探活:确认鉴权墙生效(False = 全站无口令,危险信号)。不泄露 key,只报布尔。
    return {"status": "ok", "mode": "mock" if config.USE_MOCK_DB else "alloydb",
            "gated": bool(_ACCESS_KEYS)}


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


def _client_ip(request: Request) -> str | None:
    """Cloud Run 在 Google 代理之后 → 真实调用方是 X-Forwarded-For 最左一项;
    本地/无代理回退到 request.client.host。(最左值客户端可伪造,强信任只认 Google 追加段。)"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _audit(request: Request, req: VibeQueryRequest, result: dict,
           usage: dict, latency_ms: int) -> None:
    """每请求一行结构化 JSON → stdout → Cloud Run 自动收进 Cloud Logging。
    在 Logs Explorer 按 jsonPayload.* 筛即可:谁/从哪/何时/问了什么/用了多少 token。"""
    record = {
        "severity":     "INFO",
        "logType":      "usage_audit",                # 过滤锚点:jsonPayload.logType="usage_audit"
        "app_user":     getattr(request.state, "app_user", "anon"),
        "ip":           _client_ip(request),
        "session_id":   result.get("session_id"),
        "query":        req.query,
        "status":       result.get("status"),
        "turn_type":    result.get("turn_type"),
        "tokens_in":    usage.get("tokens_in", 0),
        "tokens_out":   usage.get("tokens_out", 0),
        "tokens_total": usage.get("tokens_total", 0),
        "llm_calls":    usage.get("llm_calls", 0),
        "cost_usd":     usage.get("cost_usd", 0.0),
        # 序列化成字符串:模型名带点/横线(gemini-2.5-pro),作 JSON 对象会在 BigQuery 里炸成一堆动态列
        "by_model":     json.dumps(usage.get("by_model", {}), ensure_ascii=False),
        "latency_ms":   latency_ms,
        "ts":           time.time(),
    }
    # M6:loop 执行可观测 —— 步数/终止原因/工具直方图 + Trace 落服务端(原本只在响应体里)
    _loop = result.get("loop") or {}
    record["step_count"]        = _loop.get("steps")
    record["terminated_reason"] = _loop.get("terminated")
    record["tool_calls"]        = json.dumps(_loop.get("tool_calls", {}), ensure_ascii=False)
    record["trace_summary"]     = result.get("trace_summary")
    if result.get("status") == "error":                  # 失败轮落完整 trace,供事后重建
        record["trace"]         = json.dumps(result.get("trace", []), ensure_ascii=False)
    record["message"] = (f'audit user={record["app_user"]} status={record["status"]} '
                         f'tokens={record["tokens_total"]} cost=${record["cost_usd"]}')
    print(json.dumps(record, ensure_ascii=False), flush=True)


@app.post("/v1/video_vibe_query")
def video_vibe_query(req: VibeQueryRequest, request: Request):
    t0 = time.perf_counter()
    sid = req.session_id or uuid.uuid4().hex        # 没带 session_id → 开一个新会话
    owner = getattr(request.state, "app_user", "anon")   # 会话按认证身份归属(关 IDOR)
    with _session_lock(f"{owner}:{sid}"):           # 同会话 read-modify-write 串行,防丢轮
        session = STORE.get_or_create(sid, owner=owner)
        result = run_query(req.query, quiet_trace=True, session=session, owner=owner,
                           pro_video=req.pro_video, image=_parse_image(req.image))
        STORE.save(session, owner=owner)            # 写时机:每请求一次(纯内存模式无操作)
    result["session_id"] = sid                      # 回传,客户端下一轮带上即可续聊
    usage = result.pop("usage", {}) or {}           # token/成本:内部审计用,不回传给前端

    # 图表产物:优先 chart_spec(前端 ECharts 交互渲染);svg/png 保留作兜底 → 存本地拿 http URL
    plot_url = None
    plot = result.pop("plot", {}) or {}
    result["chart_spec"] = plot.get("chart_spec")           # 前端有则用 ECharts 渲染
    if plot.get("svg") or plot.get("png_base64"):
        fname = artifacts.save_local(plot, name=uuid.uuid4().hex[:12])
        if fname:
            plot_url = str(request.base_url).rstrip("/") + f"/plots/{fname}"

    result["plot_url"] = plot_url

    try:                                            # 审计绝不能拖垮请求 → 整体兜底
        _audit(request, req, result, usage, int((time.perf_counter() - t0) * 1000))
    except Exception:
        log.warning("audit emit failed (fail-open)", exc_info=True)

    return result


class UploadUrlRequest(BaseModel):
    content_type: str = Field("video/mp4", description="将上传文件的 Content-Type(PUT 时必须一致)")


@app.post("/v1/upload_url")
def upload_url(req: UploadUrlRequest, request: Request):
    """M5 实时上传:发一个【PUT 直传签名 URL】+ 临时 video_id。前端把视频直传到 upload_url(不经后端)后,
    即可在对话里就这个 video_id 提问 —— analyze_video / show_video 会解析到上传的视频。临时、有 TTL、不进语料库。"""
    from pipeline import uploads
    from pipeline.video_url import sign_gcs_put_url
    if req.content_type not in config.UPLOAD_CONTENT_TYPES:        # 类型白名单(别拿来塞任意内容)
        return Response(status_code=415, content=f"只支持:{', '.join(config.UPLOAD_CONTENT_TYPES)}")
    owner = getattr(request.state, "app_user", "anon")
    reg = uploads.register(owner, content_type=req.content_type)  # 原子配额计数
    if reg is None:
        return Response(status_code=429, content=f"已达今日上传上限({config.MAX_UPLOADS_PER_DAY} 个)")
    video_id, gcs_uri = reg
    put_url = sign_gcs_put_url(gcs_uri, content_type=req.content_type,
                               max_bytes=config.MAX_UPLOAD_BYTES)  # 大小上限签进 URL
    if not put_url:                                  # 本地用户 ADC 签不了;Cloud Run(SA)可用
        return Response(status_code=503, content="无法生成上传链接(本地凭证签不了;部署到 Cloud Run 后可用)")
    return {"video_id": video_id, "upload_url": put_url, "gcs_uri": gcs_uri,
            "content_type": req.content_type, "max_bytes": config.MAX_UPLOAD_BYTES}


class ResignRequest(BaseModel):
    video_ids: list[str] = Field(default_factory=list, description="要重新签发播放 URL 的 video_id 列表")


@app.post("/v1/resign")
def resign(req: ResignRequest, request: Request):
    """重签视频播放 URL。签名直链短命(TTL 15 分钟),前端把它连同回答存进 localStorage;
    离开后重新打开历史会话时旧 URL 已过期 → 播不了。前端渲染历史(或播放报错)时调本端点
    换一批新鲜 URL。
    能力面 = 与 show_video 完全一致(任意库内 video_id 可签;up_ 上传视频经注册表能力式解析)。
    【注意:本端点不做 owner 隔离】—— 与现存 show_video 同一敞口,单用户下无影响;多用户前
    需把 owner 贯通到 _resolve_gcs(deferred upload-IDOR task)。id 先过白名单再拼 SQL(防注入)。"""
    from pipeline.node_executor import _VIDEO_ID_RE, _resolve_gcs
    from pipeline.video_url import sign_gcs_uri
    signed: dict[str, str | None] = {}
    for vid in (req.video_ids or [])[:8]:              # 与 show_video 一致:一次最多 8 个
        vid = str(vid)
        if not _VIDEO_ID_RE.match(vid):                # 白名单校验:_resolve_gcs 内是 f-string 拼 SQL,
            signed[vid] = None                         #   必须挡住注入(与 show_video 同一道防线)
            continue
        try:
            gcs = _resolve_gcs(vid)                     # up_ 走注册表(能力式);其余查 video_metadata
            signed[vid] = sign_gcs_uri(gcs) if gcs else None
        except Exception:
            signed[vid] = None                         # 单个失败不拖累其余(fail-open)
    return {"signed": signed}


class EnrichRequest(BaseModel):
    video_id: str = Field(..., description="要富化的视频 id(上传 PUT 成功后调用)")


@app.post("/v1/enrich")
def enrich(req: EnrichRequest, request: Request):
    """V1.5:入库富化(转录+caption → 语义索引)。前端直传 GCS 成功后调用;幂等
    (已富化直接返回);后台线程执行不阻塞。语义层关闭时 no-op。全程 fail-open。"""
    from pipeline.node_executor import _VIDEO_ID_RE, _resolve_gcs
    if not config.USE_SEMANTIC_SEARCH:
        return {"status": "disabled"}
    vid = str(req.video_id or "")
    if not _VIDEO_ID_RE.match(vid):
        return Response(status_code=422, content="非法 video_id")
    from pipeline import enrichment
    if enrichment.already_enriched(vid):
        return {"status": "already"}
    try:
        gcs = _resolve_gcs(vid)
    except Exception:
        gcs = None
    if not gcs:
        return Response(status_code=404, content="找不到该视频")

    def work():
        try:
            log.info("enrich 完成: %s", enrichment.enrich_video(vid, gcs))
        except Exception:
            log.warning("enrich 失败(fail-open): %s", vid, exc_info=True)
    threading.Thread(target=work, daemon=True).start()
    return {"status": "started"}


@app.post("/v1/video_vibe_query/stream")
def video_vibe_query_stream(req: VibeQueryRequest, request: Request):
    """SSE 流式(M6b):loop 多步往返时把每步进度实时推给前端,最后推一条 result。
    仅 loop 路径有逐步 step 事件;dag 路径只会收到最终 result。"""
    import queue as _queue
    t0 = time.perf_counter()
    q: "_queue.Queue" = _queue.Queue()
    sid = req.session_id or uuid.uuid4().hex
    owner = getattr(request.state, "app_user", "anon")

    def work():
        try:
            with _session_lock(f"{owner}:{sid}"):
                session = STORE.get_or_create(sid, owner=owner)
                result = run_query(req.query, quiet_trace=True, session=session, owner=owner,
                                   on_step=lambda ev: q.put(ev), pro_video=req.pro_video,
                                   image=_parse_image(req.image))
                STORE.save(session, owner=owner)
            result["session_id"] = sid
            usage = result.get("usage", {}) or {}        # get(非 pop):留在 result 里给前端 context 监控
            plot = result.pop("plot", {}) or {}
            result["chart_spec"] = plot.get("chart_spec")     # 前端 ECharts 渲染
            if plot.get("svg") or plot.get("png_base64"):
                fname = artifacts.save_local(plot, name=uuid.uuid4().hex[:12])
                result["plot_url"] = (str(request.base_url).rstrip("/") + f"/plots/{fname}") if fname else None
            q.put({"type": "result", "result": result})
            try:
                _audit(request, req, result, usage, int((time.perf_counter() - t0) * 1000))
            except Exception:
                log.warning("audit emit failed (fail-open)", exc_info=True)
        except Exception as e:
            q.put({"type": "error", "error": repr(e)})
        finally:
            q.put(None)                              # 结束哨兵

    threading.Thread(target=work, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
