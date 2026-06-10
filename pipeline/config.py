"""
中央配置 —— 消除 planner / repl / mcp_server 三处重复的 DB 与 GCP 常量。

所有取值优先读环境变量,给出和 .env.example 一致的默认值。
任何模块需要 DB / GCP / Sandbox 配置,都从这里 import,不再各写一份。
"""
from __future__ import annotations

import os

# ── GCP / Vertex AI ───────────────────────────
GCP_PROJECT = os.environ.get("GCP_PROJECT", "your-gcp-project-id")
GCP_REGION  = os.environ.get("GCP_REGION", "us-central1")
GCS_BUCKET  = os.environ.get("GCS_BUCKET", "your-gcs-bucket")

# Planner 与 Code Generator 用的模型(可分别覆盖,默认同一个)
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "gemini-2.5-pro")
CODEGEN_MODEL = os.environ.get("CODEGEN_MODEL", "gemini-2.5-pro")

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
]

# ── Sandbox (Stage 5) ─────────────────────────
SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://localhost:8080")

# ── 运行模式开关 ──────────────────────────────
# REPL_USE_MOCK_DB=1  → 用内存 SQLite mock,零成本、不需要 AlloyDB
USE_MOCK_DB = os.environ.get("REPL_USE_MOCK_DB", "").lower() in ("1", "true", "yes")


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
