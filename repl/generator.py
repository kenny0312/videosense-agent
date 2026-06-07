"""
第6阶段 — Agentic REPL: 代码生成器
两步生成：
  1) generate_sql()    → 让 Gemini 写 SQL（在主进程执行，有 DB 凭证）
  2) generate_code()   → 让 Gemini 写 Python 分析代码（在 Sandbox 安全执行）
"""

import json
import logging
import os
import psycopg2
import psycopg2.extras
import vertexai
from vertexai.generative_models import GenerativeModel

PROJECT_ID       = "your-gcp-project-id"
REGION           = "us-central1"
ALLOYDB_IP       = "your-db-host"
ALLOYDB_DB       = "your_database"
ALLOYDB_USER     = "postgres"
ALLOYDB_PASSWORD = os.environ.get("ALLOYDB_PASSWORD", "")

# 切换:set REPL_USE_MOCK_DB=1 → 走内存 SQLite mock(零成本演示用)
USE_MOCK_DB = os.environ.get("REPL_USE_MOCK_DB", "").lower() in ("1", "true", "yes")

log = logging.getLogger("repl.generator")


# ── DB 访问(仅在 REPL 主进程使用) ─────────

def get_conn():
    return psycopg2.connect(
        host=ALLOYDB_IP, port=5432,
        dbname=ALLOYDB_DB, user=ALLOYDB_USER,
        password=ALLOYDB_PASSWORD, sslmode="require",
    )


def fetch_schema() -> dict:
    """读 schema:mock 模式下从内存 SQLite 取,否则从 AlloyDB information_schema 取。"""
    if USE_MOCK_DB:
        from repl._mock_db import mock_fetch_schema
        log.info("[mock] using in-memory SQLite schema")
        return mock_fetch_schema()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name IN (
                    'video_metadata', 'video_discovery',
                    'video_facts', 'video_fact_instances'
                )
                ORDER BY table_name, ordinal_position
            """)
            rows = cur.fetchall()
        schema = {}
        for table, column, dtype in rows:
            schema.setdefault(table, []).append({"column": column, "type": dtype})
        return schema
    finally:
        conn.close()


def run_sql(sql: str) -> list[dict]:
    """执行只读 SQL:mock 模式走 SQLite + 翻译器,否则走 AlloyDB。"""
    if USE_MOCK_DB:
        from repl._mock_db import mock_run_sql
        return mock_run_sql(sql)

    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("只允许 SELECT 查询")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── Prompt 构造 ──────────────────────────────

def _sql_prompt(schema: dict) -> str:
    return f"""你是一个 SQL 生成器。根据用户问题写一条只读 SELECT 语句。

# 数据库结构
{json.dumps(schema, ensure_ascii=False, indent=2)}

# 规则
- 只允许 SELECT
- video_discovery.all_activities 是 TEXT[]，用 all_activities::text ILIKE '%keyword%'
- video_facts.predicate 是 **英文** 活动描述，用 ILIKE。常见示例:
  'skiing', 'snowboarding', 'applying mascara', 'baking cookies',
  'dancing salsa', 'walking dog', 'riding bike', 'cooking on grill'
- **如果用户用中文/其它语言描述活动，必须先翻译成英文再 ILIKE**
  例:用户问"滑雪" → ILIKE '%skiing%' OR ILIKE '%snowboarding%'
  例:用户问"做饭" → ILIKE '%cooking%' OR ILIKE '%baking%'
- video_facts.matched 查已确认事实加 AND matched = true
- 求数组长度用 array_length(arr, 1),不要用 cardinality()
- 列名必须与 schema 一致
- 只输出 SQL 语句本身，不要 markdown 围栏，不要解释
"""


def _code_prompt() -> str:
    return """你是一个数据分析 Python 代码生成器。
环境已注入变量 `data`（list[dict]），是上一步 SQL 的查询结果。
请编写代码对 data 进行分析，并用 print() 输出最终答案。

# 可用库
pandas, numpy, json, math, statistics, collections, itertools, functools, datetime

# 严禁
socket, requests, urllib, subprocess, importlib, ctypes, eval, exec, open, __import__

# 输出
- 只输出 Python 代码，不要 markdown 围栏，不要解释
- 用 print() 输出结论
- 如果 data 为空，print("无匹配数据")
"""


# ── 围栏清理 ────────────────────────────────

def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        for prefix in ("python", "sql"):
            if t.lower().startswith(prefix):
                t = t[len(prefix):]
                break
        t = t.strip().rstrip("`").strip()
    return t


# ── 主类 ────────────────────────────────────

class CodeGenerator:
    """封装两步生成；code 阶段维护 history 以便基于报错自我修复。"""

    def __init__(self, schema: dict | None = None):
        vertexai.init(project=PROJECT_ID, location=REGION)
        self.model = GenerativeModel("gemini-2.5-pro")
        self.schema = schema if schema is not None else fetch_schema()
        self.code_history: list[dict] = []   # {role, text}

    # ---- 第1步：自然语言 → SQL ----
    def generate_sql(self, question: str) -> str:
        log.info("生成 SQL ...")
        resp = self.model.generate_content(
            [_sql_prompt(self.schema), f"用户问题：{question}"],
            generation_config={"temperature": 0.0},
        )
        return _strip_fence(resp.text)

    # ---- 第1.5步：SQL 自愈(P0 改进) ----
    def repair_sql(self, question: str, failed_sql: str, error: str) -> str:
        """
        SQL 执行失败时,把错误回喂 LLM 重新生成。
        典型场景:列名打错、类型不匹配、不存在的表名。
        """
        log.info("修复 SQL ...")
        repair_msg = (
            f"用户问题：{question}\n\n"
            f"上一次生成的 SQL 执行失败,请基于 schema 修复后重新生成。\n\n"
            f"--- 失败 SQL ---\n{failed_sql}\n\n"
            f"--- 错误信息 ---\n{error}\n\n"
            "只输出修复后的 SQL,不要解释。"
        )
        resp = self.model.generate_content(
            [_sql_prompt(self.schema), repair_msg],
            generation_config={"temperature": 0.0},
        )
        return _strip_fence(resp.text)

    # ---- 第2步：data + 问题 → Python 代码 ----
    def reset_code_history(self):
        self.code_history = []

    # P0 改进:限制 history 大小,防止 prompt 无界增长
    HISTORY_TAIL = 4   # 保留首条(原问题+data 预览) + 最近 4 条

    def generate_code(self, user_message: str) -> str:
        """
        user_message 首轮是 "问题 + data 预览"；
        后续轮是 "上次报错回退 + 请修复"。
        """
        self.code_history.append({"role": "user", "text": user_message})

        # P0:截断历史 —— 保留首条(原问题)+ 最近若干条
        if len(self.code_history) > self.HISTORY_TAIL + 1:
            self.code_history = (
                [self.code_history[0]]
                + self.code_history[-self.HISTORY_TAIL:]
            )

        # Gemini 不直接支持 dict 历史，组装成纯字符串提示
        history_text = ""
        for turn in self.code_history:
            tag = "用户" if turn["role"] == "user" else "助手"
            history_text += f"\n[{tag}]\n{turn['text']}\n"

        log.info("生成 Python 代码（history=%d 条）...", len(self.code_history))
        resp = self.model.generate_content(
            [_code_prompt(), history_text],
            generation_config={"temperature": 0.2},
        )
        code = _strip_fence(resp.text)
        self.code_history.append({"role": "model", "text": code})
        return code
