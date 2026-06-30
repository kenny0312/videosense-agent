"""
中央配置 —— 消除 planner / repl / mcp_server 三处重复的 DB 与 GCP 常量。

所有取值优先读环境变量,给出和 .env.example 一致的默认值。
任何模块需要 DB / GCP / Sandbox 配置,都从这里 import,不再各写一份。
"""
from __future__ import annotations

import os


def _load_local_env() -> None:
    """本地便利:若仓库根有 neon.env(gitignored),把其中 KEY=VALUE 载入环境
    (不覆盖已显式设置的)。这样直接 uvicorn / 跑脚本都自动连 Neon,无需先手动 source。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "neon.env")
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_local_env()

# ── GCP / Vertex AI ───────────────────────────
GCP_PROJECT = os.environ.get("GCP_PROJECT", "your-gcp-project-id")
GCP_REGION  = os.environ.get("GCP_REGION", "us-central1")
GCS_BUCKET  = os.environ.get("GCS_BUCKET", "your-gcs-bucket")

# M5 实时上传:用户直传的临时视频。前缀单独(配 GCS lifecycle 自动删);临时 video_id 形如 up_<hex>,
# 注册在 Redis(TTL 到期自删),【不进 video_metadata】(免污染正式语料)。每用户每天有上传配额。
UPLOAD_PREFIX        = os.environ.get("UPLOAD_PREFIX", "uploads")          # gs://<bucket>/uploads/<owner>/<vid>.mp4
UPLOAD_TTL_SECONDS   = int(os.environ.get("UPLOAD_TTL_SECONDS", str(24 * 3600)))   # 临时注册 TTL(≈ lifecycle)
MAX_UPLOADS_PER_DAY  = int(os.environ.get("MAX_UPLOADS_PER_DAY", "20"))    # 每用户每天上传数上限
MAX_UPLOAD_BYTES     = int(os.environ.get("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))   # 单个上传大小上限(签进 PUT URL)
UPLOAD_CONTENT_TYPES = ("video/mp4", "video/quicktime", "video/webm")     # 允许的上传类型(端点白名单)

# Planner 与 Code Generator 用的模型(可分别覆盖,默认同一个)
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "gemini-2.5-pro")
CODEGEN_MODEL = os.environ.get("CODEGEN_MODEL", "gemini-2.5-pro")
# 前置 Router(可答性/意图判定)用的小模型 —— 评判任务,小模型够用且便宜
CRITIC_MODEL  = os.environ.get("CRITIC_MODEL", "gemini-2.5-flash")

# ── 执行器:probe-and-step 主循环(M7b 起【唯一】路径;旧 Planner→DAG 仅 dev CLI main.py 保留)──
# loop 大脑模型:M2 spike 结论 = flash 与 pro 正确率相同但更快,故默认 CRITIC_MODEL
LOOP_MODEL         = os.environ.get("LOOP_MODEL", CRITIC_MODEL)
MAX_LOOP_STEPS     = int(os.environ.get("MAX_LOOP_STEPS", "16"))    # 终止护栏:防死循环
LOOP_REPEAT_LIMIT  = int(os.environ.get("LOOP_REPEAT_LIMIT", "2"))  # 同一(工具,参数)连续失败上限

# 路由层(设计 one-loop-router-demote.md):
#   0 = 单 loop 主路(默认)—— 不调前置 Router,loop 带完整 transcript 回放自己判 闲聊/超范围拒/clarify。
#       省每轮一次 flash,且根治"context-blind 的前置门误杀只有结合上文才看得懂的短回复(ok/我想看)"。
#   1 = 保留旧 Router 终结门(回退开关;一键恢复旧行为,无需改代码)。
USE_ROUTER_GATE = os.environ.get("USE_ROUTER_GATE", "0").lower() in ("1", "true", "yes")

# 自检 B(设计 self-check-critic.md):收口前插一个显式 critic 判"满足用户没",没满足喂回再来一轮。
#   opt-in(默认 0;每个收口轮多一次 flash + 可能多一轮);MAX_ROUNDS = critic 驱动的"再来"上限(防空转)。
USE_SELF_CHECK_CRITIC = os.environ.get("USE_SELF_CHECK_CRITIC", "0").lower() in ("1", "true", "yes")
SELF_CHECK_MAX_ROUNDS = int(os.environ.get("SELF_CHECK_MAX_ROUNDS", "1"))
# M5 记忆:loop 路径 transcript 回放 + 压缩(决策④)
# CC 式「全量注入 + 临窗压缩」:默认把整段回放原文喂 loop,只在【逼近 context window】时才压缩。
# 预算跟 LOOP_MODEL 的窗口挂钩(flash=1M),留头寸(FRACTION)给 system+schema+tools+本轮步骤+输出,
# 故压在 ~0.6 而非填满 → 回放高水位 ≈ 600k(旧值 3000 等于只用窗口 0.3%,过早摘要丢精度)。
LOOP_KEEP_TURNS              = int(os.environ.get("LOOP_KEEP_TURNS", "4"))            # 压缩时保最近 N 轮原文
LOOP_CONTEXT_WINDOW          = int(os.environ.get("LOOP_CONTEXT_WINDOW", "1000000"))  # LOOP_MODEL 的 context window
LOOP_CONTEXT_BUDGET_FRACTION = float(os.environ.get("LOOP_CONTEXT_BUDGET_FRACTION", "0.6"))
LOOP_CONTEXT_TOKEN_BUDGET    = int(os.environ.get(                                   # 回放压缩高水位(默认 = 窗口×FRACTION)
    "LOOP_CONTEXT_TOKEN_BUDGET", str(int(LOOP_CONTEXT_WINDOW * LOOP_CONTEXT_BUDGET_FRACTION))))

# 方向一:单请求最多【现场分析】的视频数(配额护栏;成本闸)。
# M4.4:并行(MAX_ANALYZE_PARALLEL)落地后从 5 提到 12 —— 直接覆盖设计的动机场景(「比 12 个翼装视频」),
# 大脑仍被引导「先 sql_query 缩到最相关的几个」,12 只是上限不是常态。要回退设环境变量即可。
MAX_VIDEOS_PER_REQUEST    = int(os.environ.get("MAX_VIDEOS_PER_REQUEST", "12"))

# M4.1:analyze_video 内容缓存(视频离线投递后静态 → 重复不重看,省一次 Gemini 多模态调用)。
#   memory = 进程内 LRU(默认,零基建、跨副本不共享、重启清空);off = 关闭(一键退回)。
#   后续 M4 可叠加 redis 跨副本共享(见设计 §4.3 / 开放问题)。键含【实际生效模型】→ Pro/Flash 不串味。
ANALYZE_CACHE_BACKEND = os.environ.get("ANALYZE_CACHE_BACKEND", "memory").lower()  # memory | redis | off
ANALYZE_CACHE_MAX     = int(os.environ.get("ANALYZE_CACHE_MAX", "512"))            # 进程内 L1 LRU 上限条数
# redis 后端:L1 进程内 LRU 之上加一层 L2 共享 Redis(跨 Cloud Run 副本命中)。TTL 秒;视频静态 → 默认 7 天。
ANALYZE_CACHE_TTL_SECONDS = int(os.environ.get("ANALYZE_CACHE_TTL_SECONDS", str(7 * 24 * 3600)))

# M4.3:同一步内多个 analyze_video 调用的并发上限(I/O 密集 = 等 Gemini 多模态)。
#   =1 → 退回纯串行(秒级回退开关);起步 3,压测看 Gemini 429/限流再调。
MAX_ANALYZE_PARALLEL = int(os.environ.get("MAX_ANALYZE_PARALLEL", "3"))

# ── AlloyDB ───────────────────────────────────
ALLOYDB_HOST     = os.environ.get("ALLOYDB_HOST", "localhost")
ALLOYDB_PORT     = int(os.environ.get("ALLOYDB_PORT", "5432"))
ALLOYDB_DB       = os.environ.get("ALLOYDB_DB", "your_database")
ALLOYDB_USER     = os.environ.get("ALLOYDB_USER", "postgres")
ALLOYDB_PASSWORD = os.environ.get("ALLOYDB_PASSWORD", "")

# 业务表白名单 —— get_schema 只暴露这些表
BUSINESS_TABLES = [
    "video_metadata",
    "video_discovery",
    "video_facts",
    "video_fact_instances",
    "skydive_segments",          # 跳伞专栏:受控阶段元数据(每视频一行,阶段列可为 NULL)
]

# ── Sandbox (Stage 5) ─────────────────────────
SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://localhost:8080")

# ── 运行模式开关 ──────────────────────────────
# REPL_USE_MOCK_DB=1  → 用内存 SQLite mock,零成本、不需要 AlloyDB
USE_MOCK_DB = os.environ.get("REPL_USE_MOCK_DB", "").lower() in ("1", "true", "yes")

# ── 会话持久化(多轮记忆)──────────────────────
# 独立 SQLite 文件:与 MCP 查的库【物理隔离】,planner 生成的 SQL 够不着 → 免疫"潘多拉"。
# 设 SESSION_DB_PATH="" 关闭持久化(纯内存,测试/CI 用)。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_DB_PATH = os.environ.get(
    "SESSION_DB_PATH", os.path.join(_REPO_ROOT, ".session_store.sqlite"))
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(24 * 3600)))  # 闲置超此秒数的会话懒清理

# 跨轮 artifact【值】仓的 TTL(秒);默认随会话 TTL。Redis 值仓用 SET ... EX,到期自动删(无需定时任务)。
# 想"只保留三天" → 设 SESSION_TTL_SECONDS=259200(连带值仓),或单独设 ARTIFACT_VALUE_TTL_SECONDS。
ARTIFACT_VALUE_TTL_SECONDS = int(os.environ.get("ARTIFACT_VALUE_TTL_SECONDS", str(SESSION_TTL_SECONDS)))

# 会话后端:sqlite(默认,本机单节点)| redis(共享外部存储,多实例/Cloud Run 跨副本续聊)。
# 选 redis 仍守"潘多拉"隔离 —— 会话存在独立服务,planner 的 SQL(MCP 查 Neon)够不着。
# redis 后端的连接二选一(工厂里 TCP 优先):
#   · REDIS_URL —— TCP RESP 协议(redis-py),如 rediss://default:<pwd>@<host>:6379
#   · UPSTASH_REDIS_REST_URL + _TOKEN —— Upstash 的 HTTP REST(upstash-redis),Cloud Run 同样可用
SESSION_BACKEND = os.environ.get("SESSION_BACKEND", "sqlite").lower()
REDIS_URL = os.environ.get("REDIS_URL", "")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


def alloydb_dsn() -> dict:
    """psycopg2.connect(**alloydb_dsn()) 用的连接参数。"""
    return {
        "host": ALLOYDB_HOST,
        "port": ALLOYDB_PORT,
        "dbname": ALLOYDB_DB,
        "user": ALLOYDB_USER,
        "password": ALLOYDB_PASSWORD,
        "sslmode": "require",
    }
