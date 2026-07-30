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
from pipeline.agentops import ratelimit
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
_OPEN_PATHS = {"/health", "/internal/tasks/advance"}   # advance 豁免口令墙,被 OIDC 罩住(S-3)

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
    # max_length:挡超大 query 直接灌进 loop 当 token 烧钱(超限 Pydantic 自动 422),见 P0-2。
    query: str = Field(..., max_length=config.QUERY_MAX_CHARS, description="自然语言视频分析问题")
    session_id: str | None = Field(
        None, description="多轮会话 id;省略则开新会话,响应会回传一个 session_id 供下一轮带上")
    pro_video: bool = Field(
        False, description="Pro 视频分析:本请求的 analyze_video 用更强的 pro 模型(更准、更慢)")
    critic: bool = Field(
        False, description="复核模式:收口前多一道自检判'满足用户没',不满足自动补一轮(略慢)")
    image: str | None = Field(
        None, description="可选:粘贴的截图,data URL(data:image/png;base64,...)。作多模态输入附在本轮。")
    model: str | None = Field(
        None, description="可选:本请求的大脑模型(服务端白名单内可切,如 gemini-2.5-pro;省略用默认)")


# 粘贴图片:data URL → (bytes, mime)。限类型 + 大小(防超大 base64 撑爆请求/成本)。
_IMG_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_IMG_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))   # 解码后 8MB


def _is_guest(owner: str) -> bool:
    """约定:APP_ACCESS_KEYS 里名字以 guest 开头(大小写不限)的账号 = 便宜档(成本保险丝)。"""
    return (owner or "").lower().startswith("guest")


def _allowed_models(owner: str) -> list[str]:
    return config.LOOP_MODEL_GUEST_CHOICES if _is_guest(owner) else config.LOOP_MODEL_CHOICES


def _resolve_model(raw: str | None, owner: str):
    """阶段A:每请求大脑模型。服务端白名单校验(绝不信任客户端字符串)。
    返回 (model, err_response);省略/空 = (None, None) 用默认。"""
    m = (raw or "").strip()
    if not m:
        return None, None
    allowed = _allowed_models(owner)
    if m not in allowed:
        return None, Response(status_code=422,
                              content="不支持的 model;本账号可选:" + ", ".join(allowed))
    return m, None


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


# ── Loop Console(开发者观测:大脑每步决策/prompt 构成/扇出;同一 Basic 鉴权墙内)──
_CONSOLE_ADMINS = set(filter(None, os.environ.get("CONSOLE_ADMINS", "").split(",")))


def _console_gate(request: Request):
    """Console 是开发者观测面(能看到【所有用户】的问题/答案)—— 普通产品 key 不该进。
    fail-closed:开了鉴权(有 _ACCESS_KEYS)就必须在 CONSOLE_ADMINS 名单;本地无鉴权保持全开。"""
    if _ACCESS_KEYS and getattr(request.state, "app_user", None) not in _CONSOLE_ADMINS:
        return Response(status_code=403, content="console 仅限 CONSOLE_ADMINS 名单")
    return None


@app.get("/console")
def console_page(request: Request):
    denied = _console_gate(request)
    if denied:
        return denied
    import os as _os
    return FileResponse(_os.path.join(_os.path.dirname(_INDEX_HTML), "console.html"))


@app.get("/v1/console/allowed")
def console_allowed(request: Request):
    """主页入口按钮的探针:普通用户拿 false(按钮压根不渲染,界面无痕),不走 403。"""
    return {"allowed": _console_gate(request) is None}


@app.get("/v1/console/traces")
def console_traces(request: Request):
    denied = _console_gate(request)
    if denied:
        return denied
    from pipeline import loop_console
    return {"traces": loop_console.list_traces()}


@app.get("/v1/console/trace/{tid}")
def console_trace(tid: str, request: Request):
    denied = _console_gate(request)
    if denied:
        return denied
    from pipeline import loop_console
    t = loop_console.get_trace(tid)
    return t or Response(status_code=404, content="not found")


@app.get("/health")
def health():
    # gated 供上线后探活:确认鉴权墙生效(False = 全站无口令,危险信号)。不泄露 key,只报布尔。
    return {"status": "ok", "mode": "mock" if config.USE_MOCK_DB else "alloydb",
            "gated": bool(_ACCESS_KEYS)}


# ── S-2 任务底座四端点(设计 docs/longhorizon-task-substrate-plan.md §S-2)────────
# USE_TASKS=0 → 全 404(特性不存在);guest 一律 403(公开部署日不返工);
# owner 隔离在 task_store 的 SQL WHERE 里(不靠这里自觉)。
class TaskCreateRequest(BaseModel):
    goal: str
    budget_cap: "float | None" = None


def _tasks_gate(request: Request) -> "Response | None":
    if not config.USE_TASKS:
        return Response(status_code=404)
    owner = getattr(request.state, "app_user", "anon")
    if _is_guest(owner):
        return Response(json.dumps({"error": "游客不能使用后台任务"}),
                        status_code=403, media_type="application/json")
    return None


@app.post("/v1/tasks")
def task_create(req: TaskCreateRequest, request: Request):
    if (r := _tasks_gate(request)) is not None:
        return r
    owner = getattr(request.state, "app_user", "anon")
    goal = (req.goal or "").strip()
    if not goal or len(goal) > 2000:
        return Response(json.dumps({"error": "goal 必填且 ≤2000 字"}),
                        status_code=422, media_type="application/json")
    # 只做 ratelimit precheck 校验、不扣占位(红队:占位与日顶制度双向冲突)。
    # sid 必须传 None:常量 sid 是全用户共享的伪会话桶 —— review 实测一个登录用户把自己
    # 会话取名同值烧到 $0.75 就能让【全站】立项 429 二十四小时(投毒 DoS)。
    # S-3 记账处同禁:任务花费绝不许挂常量 sid。
    if (rl := _rate_limited(request, owner, sid=None)) is not None:
        return rl
    # cap 夹在 (0, min(单任务硬顶, 任务日顶)]。NaN 必拒:json/pydantic 默认放行裸 NaN,
    # 而 min(nan, x)=nan、nan<=0=False → 穿透夹紧入库 = 预算闸对该任务失明 + 任务页
    # 序列化 500(review 实测);0 也必拒(旧写法 or 会把显式 0 静默换成默认再开跑烧钱)。
    import math
    cap_in = config.TASK_DEFAULT_CAP_USD if req.budget_cap is None else float(req.budget_cap)
    if not math.isfinite(cap_in) or cap_in <= 0:
        return Response(json.dumps({"error": "budget_cap 必须是正的有限数"}),
                        status_code=422, media_type="application/json")
    cap = min(cap_in, config.TASK_MAX_CAP_USD, config.RL_TASK_DAILY_COST_USD)
    from pipeline import task_queue, task_store
    task_id, created = task_store.create_task(owner, goal, cap)
    if not created:                                       # 幂等命中(含前端双击)
        # pending 幽灵自愈(review 确认两个触发器:commit→enqueue 窗口进程死 / _execute
        # 盲重试撞自己刚插的行把 created 翻成 False)—— 命中的行若还停在 pending,
        # 说明第一波从没投出去,这里补投一次(命名任务/CLAIM CAS 天然幂等,重复无害)。
        st = task_store.status_of(task_id)
        if st and st[0] == "pending":
            try:
                task_queue.enqueue_advance(task_id, st[1])
            except Exception as e:
                log.warning("pending 幽灵补投失败 %s: %r", task_id, e)
                return Response(json.dumps({"error": "任务已登记但排队服务暂时不可用,"
                                                     "请稍后重试(费用未发生)"}),
                                status_code=503, media_type="application/json")
        return {"task_id": task_id, "created": False,
                "note": "同目标的任务已在进行中,直接看它的进度即可"}
    try:
        task_queue.enqueue_advance(task_id, 0)            # 投第一波(wave 0 = 规划波)
    except Exception as e:                                # fail-closed:不留 running/pending 幽灵
        log.warning("任务 %s 投递失败(fail-closed → paused_error): %r", task_id, e)
        try:
            task_store.set_status(task_id, "paused_error")
            task_store.add_event(task_id, "enqueue_failed", {"error": repr(e)[:200]})
        except Exception:
            log.error("任务 %s 投递失败后的 fail-closed 处置也失败", task_id, exc_info=True)
        return Response(json.dumps({"error": "任务已登记但排队服务暂时不可用,"
                                             "请稍后在任务页点重试(费用未发生)"}),
                        status_code=503, media_type="application/json")
    return {"task_id": task_id, "created": True}


@app.get("/v1/tasks/{task_id}")
def task_get(task_id: str, request: Request):
    if (r := _tasks_gate(request)) is not None:
        return r
    owner = getattr(request.state, "app_user", "anon")
    from pipeline import task_store
    view = task_store.get_view(owner, task_id)
    if view is None:                                      # 不存在或不属于你,同一口径(防枚举)
        return Response(status_code=404)
    return view


@app.post("/v1/tasks/{task_id}/notes")
def task_note(task_id: str, request: Request, body: dict):
    if (r := _tasks_gate(request)) is not None:
        return r
    owner = getattr(request.state, "app_user", "anon")
    note = str((body or {}).get("note") or "").strip()
    if not note or len(note) > 1000:
        return Response(json.dumps({"error": "note 必填且 ≤1000 字"}),
                        status_code=422, media_type="application/json")
    from pipeline import task_store
    if task_store.owner_of(task_id) != owner:
        return Response(status_code=404)
    task_store.add_event(task_id, "user_note", {"note": note})   # 下一波组装注入(S-3)
    return {"ok": True}


def _oidc_claims(token: str) -> dict:
    """Google OIDC token → claims(单测打桩点;live 走 google-auth 验签)。"""
    from google.auth.transport import requests as garequests
    from google.oauth2 import id_token as gid
    return gid.verify_oauth2_token(token, garequests.Request(),
                                   audience=config.TASKS_ADVANCE_URL)


def _verify_advance_auth(request: Request, authorization: "str | None",
                         shared: "str | None") -> bool:
    """S-3 鉴权:生产(K_SERVICE 在场)只认 Cloud Tasks 的 OIDC(audience 钉死 advance
    完整 URL);共享密钥仅限本地(检测到 K_SERVICE 直接禁用 —— 红队:不留降级到生产)。
    校验失败一律 False(端点回 403 fail-closed)。"""
    on_cloudrun = bool(os.environ.get("K_SERVICE"))
    if authorization and authorization.lower().startswith("bearer "):
        try:
            claims = _oidc_claims(authorization.split(None, 1)[1])
            # 【必须钉调用者身份】(review-HIGH):verify 只校验 签名/exp/aud,而 SA 的
            # ID token audience 谁都能自选 —— 任何 Google 账号都能铸出 aud=本服务的合法
            # token。audience 之外必须比对 email == 我们配置的投递 SA,未配置 = fail-closed。
            return (bool(config.TASKS_ADVANCE_URL) and bool(config.TASKS_INVOKER_SA)
                    and claims.get("email") == config.TASKS_INVOKER_SA
                    and claims.get("email_verified") is True)
        except Exception:
            log.warning("advance OIDC 校验失败", exc_info=True)
            return False
    if on_cloudrun:                                        # 云上无 OIDC = 拒,密钥路径禁用
        return False
    import hmac as _hmac
    want = os.environ.get("TASKS_SHARED_SECRET", "")
    return bool(want) and bool(shared) and _hmac.compare_digest(want, shared)


@app.post("/internal/tasks/advance")
def tasks_advance(request: Request, body: dict):
    """Cloud Tasks 回调:推进一波。RETRY → 503(让队列退避重来,跨过租约期);其余 200。
    inline 驱动不经这里(daemon 线程直调 task_runner.advance,同一代码路径)。"""
    if not config.USE_TASKS:
        return Response(status_code=404)
    if not _verify_advance_auth(request, request.headers.get("Authorization"),
                                request.headers.get("X-Tasks-Secret")):
        return Response(status_code=403)
    task_id = str((body or {}).get("task_id") or "")
    wave_n = (body or {}).get("wave_n")
    if not task_id or not isinstance(wave_n, int) or wave_n < 0:
        return Response(json.dumps({"error": "需要 task_id 与 wave_n(int≥0)"}),
                        status_code=422, media_type="application/json")
    from pipeline import task_runner
    out = task_runner.advance(task_id, wave_n)
    if out.get("result") == task_runner.RETRY:
        return Response(json.dumps(out), status_code=503, media_type="application/json")
    return out


@app.post("/v1/tasks/{task_id}/resume")
def task_resume(task_id: str, request: Request, body: dict | None = None):
    """S-4 复活(paused_budget 提额 / paused_error 重试):新 cap + 回 running + 清租约
    + 【必须投递】当前波(红队 HIGH:v1 的 resume 没投递 = 必死锁)。
    投递名带 salt 绕命名任务墓碑(执行过的名字 ~1h 内裸重投会静默丢投 = 假活)。"""
    if (r := _tasks_gate(request)) is not None:
        return r
    owner = getattr(request.state, "app_user", "anon")
    from pipeline import task_queue, task_store
    if task_store.owner_of(task_id) != owner:
        return Response(status_code=404)
    raw = (body or {}).get("budget_cap")
    import math
    if raw is None:
        new_cap = config.TASK_MAX_CAP_USD
    else:
        new_cap = float(raw)
        if not math.isfinite(new_cap) or new_cap <= 0:
            return Response(json.dumps({"error": "budget_cap 必须是正的有限数"}),
                            status_code=422, media_type="application/json")
    new_cap = min(new_cap, config.TASK_MAX_CAP_USD, config.RL_TASK_DAILY_COST_USD)
    # 贴顶诚实回话(review-HIGH:硬顶之上任何 cap 都过不了波开头闸,旧写法回 200
    # "已恢复"却下一波立刻又暂停 = 假成功骗 UI)。收口波有收尾额度,所以只有"连收尾
    # 都不够"才算真到顶。
    try:
        live = task_store.live_state(task_id)
    except Exception:                          # 读不到就照常恢复(波开头闸会兜住),不拦用户
        log.warning("resume 读实时账目失败(fail-open)", exc_info=True)
        live = None
    if live and live[1] > new_cap + config.TASK_FINALIZE_GRACE_USD:
        return {"ok": True, "resumed": False,
                "note": (f"这个任务已经花了 ${live[1]:.2f},到了单任务的花费上限 "
                         f"(${config.TASK_MAX_CAP_USD:.2f}),再恢复也推不动了。"
                         "要继续请提高上限后重开一个任务,或就现有结果收尾。")}
    got = task_store.resume(task_id, new_cap)
    if got is None:                                        # 不在暂停态 → 幂等,不报错
        return {"ok": True, "resumed": False,
                "note": "这个任务现在不处于暂停状态,不需要恢复"}
    wave_n, cap = got
    task_store.add_event(task_id, "resumed", {"wave": wave_n, "new_cap": cap})
    try:
        task_queue.enqueue_advance(task_id, wave_n, salt=uuid.uuid4().hex[:8])
    except Exception as e:                                 # 投不出去 → 回 paused_error,别假活
        log.warning("resume 投递失败 %s: %r", task_id, e)
        task_store.set_status(task_id, "paused_error")
        task_store.add_event(task_id, "enqueue_failed", {"error": repr(e)[:200],
                                                         "at": "resume"})
        return Response(json.dumps({"error": "恢复失败:排队服务暂时不可用,请稍后再试"}),
                        status_code=503, media_type="application/json")
    return {"ok": True, "resumed": True, "wave_n": wave_n, "budget_cap": cap}


@app.post("/v1/tasks/{task_id}/nudge")
def task_nudge(task_id: str, request: Request):
    """S-5 唯一必做件:人肉救援通道 —— 对"running 但久未推进"的任务重投当前波。
    命名任务带 salt 绕墓碑;CLAIM 的 CAS 保证真在跑的波不会被重复执行。"""
    if (r := _tasks_gate(request)) is not None:
        return r
    owner = getattr(request.state, "app_user", "anon")
    from pipeline import task_queue, task_store
    if task_store.owner_of(task_id) != owner:
        return Response(status_code=404)
    st = task_store.status_of(task_id)
    if not st or st[0] != "running":
        return {"ok": True, "nudged": False,
                "note": "只有卡住的进行中任务需要重推;暂停的请用恢复"}
    try:
        task_queue.enqueue_advance(task_id, st[1], salt=uuid.uuid4().hex[:8])
    except Exception as e:
        log.warning("nudge 投递失败 %s: %r", task_id, e)
        return Response(json.dumps({"error": "重推失败:排队服务暂时不可用"}),
                        status_code=503, media_type="application/json")
    return {"ok": True, "nudged": True, "wave_n": st[1]}


@app.post("/v1/tasks/{task_id}/cancel")
def task_cancel(task_id: str, request: Request):
    if (r := _tasks_gate(request)) is not None:
        return r
    owner = getattr(request.state, "app_user", "anon")
    from pipeline import task_store
    if task_store.owner_of(task_id) != owner:
        return Response(status_code=404)
    changed = task_store.set_status(task_id, "cancelled")  # 终态同语句清租约;非法前驱=0 行
    if changed:
        task_store.add_event(task_id, "cancelled", {})
    return {"ok": True, "changed": changed}                # 已终态的重复 cancel 幂等


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


def _rate_limited(request: Request, owner: str, sid: str) -> "Response | None":
    """P0-2 请求前护栏。超额 → 429(带中文理由);放行 → None。见 pipeline/agentops/ratelimit.py。"""
    reason = ratelimit.precheck(owner, _client_ip(request), sid)
    if reason:
        return Response(status_code=429, content=reason)
    return None


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
        # P0-1:思考/工具用提示 token 单列 —— 思考按 out 价计费,是深跑账单的大头;
        # 不落日志则"成本全口径可见"红线在唯一的生产消费方处失效。
        "tokens_thought": usage.get("tokens_thought", 0),
        "tokens_tool":  usage.get("tokens_tool", 0),
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
    # P0-1 fail-loud:成本里有按【兜底最贵单价】估的模型 → 抬 severity 让 Cloud Logging 能配告警,
    # 并报出模型名(否则运维只看到一个偏高的 cost_usd,永不知道该往 _PRICE 里加哪一行)。
    if usage.get("unpriced_models"):
        record["unpriced_models"] = usage["unpriced_models"]
        record["severity"] = "WARNING"
    record["message"] = (f'audit user={record["app_user"]} status={record["status"]} '
                         f'tokens={record["tokens_total"]} cost=${record["cost_usd"]}'
                         + (f' UNPRICED={record.get("unpriced_models")}'
                            if usage.get("unpriced_models") else ''))
    print(json.dumps(record, ensure_ascii=False), flush=True)
    # P0-2 记账:把本次实际成本累加进限流的当日/会话桶(供下一请求的 precheck 比对)。fail-open。
    try:
        ratelimit.record(record["app_user"], record["ip"], record["session_id"],
                         usage.get("cost_usd", 0.0))
    except Exception:
        log.warning("ratelimit record failed (fail-open)", exc_info=True)


@app.get("/v1/models")
def list_models(request: Request):
    """阶段A:本账号可用的大脑模型。前端据此渲染切换菜单(guest 直接看不到锁着的档,
    而不是点了才 422);走门禁(不在 _OPEN_PATHS),匿名拿不到。"""
    owner = getattr(request.state, "app_user", "anon")
    return {"default": config.LOOP_MODEL, "choices": _allowed_models(owner)}


@app.get("/v1/models")
def list_models(request: Request):
    """阶段A:本账号可用的大脑模型。前端据此渲染切换菜单(guest 直接看不到锁着的档,
    而不是点了才 422);走门禁(不在 _OPEN_PATHS),匿名拿不到。"""
    owner = getattr(request.state, "app_user", "anon")
    return {"default": config.LOOP_MODEL, "choices": _allowed_models(owner)}


@app.post("/v1/video_vibe_query")
def video_vibe_query(req: VibeQueryRequest, request: Request):
    t0 = time.perf_counter()
    sid = req.session_id or uuid.uuid4().hex        # 没带 session_id → 开一个新会话
    owner = getattr(request.state, "app_user", "anon")   # 会话按认证身份归属(关 IDOR)
    rl = _rate_limited(request, owner, sid)         # P0-2:超额直接 429,不进 loop(最贵的都在 loop 里)
    if rl:
        return rl
    model, err = _resolve_model(req.model, owner)   # 阶段A:白名单外直接 422,不进 loop
    if err:
        return err
    with _session_lock(f"{owner}:{sid}"):           # 同会话 read-modify-write 串行,防丢轮
        session = STORE.get_or_create(sid, owner=owner)
        result = run_query(req.query, quiet_trace=True, session=session, owner=owner,
                           pro_video=req.pro_video and not _is_guest(owner),   # guest 也锁 pro 眼
                           image=_parse_image(req.image), model=model, critic=req.critic)
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
    rl = _rate_limited(request, owner, sid)         # P0-2:超额直接 429,在开流前拦下
    if rl:
        return rl
    model, err = _resolve_model(req.model, owner)   # 阶段A:流开始前校验,非法 422
    if err:
        return err

    def work():
        try:
            with _session_lock(f"{owner}:{sid}"):
                session = STORE.get_or_create(sid, owner=owner)
                result = run_query(req.query, quiet_trace=True, session=session, owner=owner,
                                   on_step=lambda ev: q.put(ev),
                                   pro_video=req.pro_video and not _is_guest(owner),
                                   image=_parse_image(req.image), model=model, critic=req.critic)
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
