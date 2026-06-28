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
# M5 记忆:loop 路径 transcript 回放 + 压缩(决策④)
LOOP_KEEP_TURNS           = int(os.environ.get("LOOP_KEEP_TURNS", "4"))            # 回放保最近 N 轮原文
LOOP_CONTEXT_TOKEN_BUDGET = int(os.environ.get("LOOP_CONTEXT_TOKEN_BUDGET", "3000"))  # 超此触发 LLM 压缩

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
