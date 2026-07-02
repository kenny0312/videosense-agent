"""V1 语义索引:snippet 构造 + upsert/检索 SQL(设计 semantic-retrieval.md §3-§6)。

纯函数 + SQL 模板(可离线单测);连接由调用方注入(回填脚本用 psycopg2,检索走 mcp_client)。
三类 source(未来 'transcript'|'caption' 预留):
  · fact    —— video_facts 细谓词行(排除大类溯源行):snippet = "predicate: rationale"
  · skydive —— skydive_segments.summary(freefall 时段)
  · analyze —— analyze_video 结果缓存的 answer(随用增长;evidence_ts → 时段)
"""
from __future__ import annotations

from typing import Any

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS content_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    video_id    TEXT NOT NULL,
    source      TEXT NOT NULL,
    snippet     TEXT NOT NULL,
    start_ts    DOUBLE PRECISION,
    end_ts      DOUBLE PRECISION,
    embedding   VECTOR(768) NOT NULL,
    content_key TEXT UNIQUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- review 修:【不建 ivfflat】。库小(几百~几千行),精确 KNN(顺序扫)亚毫秒且【100% 召回】;
-- ivfflat 需数据在建索引时聚类、且默认 probes=1 只扫 1 个列表 → 小表上是纯负担(漏最近邻)。
-- 若某天 >5 万行,再上 hnsw(建后不需重建、召回稳),别回 ivfflat。这里显式 DROP 掉早期误建的。
DROP INDEX IF EXISTS ix_ce_ivf;
"""

UPSERT_SQL = ("INSERT INTO content_embeddings "
              "(video_id, source, snippet, start_ts, end_ts, embedding, content_key) "
              "VALUES (%s, %s, %s, %s, %s, %s::vector, %s) "
              "ON CONFLICT (content_key) DO UPDATE SET "
              "snippet = EXCLUDED.snippet, embedding = EXCLUDED.embedding, "
              "start_ts = EXCLUDED.start_ts, end_ts = EXCLUDED.end_ts")

SEARCH_SQL = ("SELECT video_id, source, snippet, start_ts, end_ts, "
              "1 - (embedding <=> %s::vector) AS score "
              "FROM content_embeddings ORDER BY embedding <=> %s::vector LIMIT %s")


# ── snippet 构造(纯函数,离线可测)──────────────────────────────
def fact_snippet(row: dict) -> "tuple[str, str, float | None, float | None] | None":
    """video_facts 行 → (content_key, snippet, start, end)。大类溯源行/空 rationale → None(不入索引)。"""
    pred = str(row.get("predicate") or "").strip()
    rat = str(row.get("rationale") or "").strip()
    if not pred or not rat or rat.startswith("category:"):
        return None
    return (f"fact:{row.get('video_id')}:{pred}", f"{pred}: {rat}",
            row.get("start_ts"), row.get("end_ts"))


def skydive_snippet(row: dict) -> "tuple[str, str, float | None, float | None] | None":
    s = str(row.get("summary") or "").strip()
    if not s:
        return None
    return (f"skydive:{row.get('video_id')}", s,
            row.get("freefall_start_ts"), row.get("freefall_end_ts"))


def analyze_snippet(video_id: str, dump: dict, content_key: str
                    ) -> "tuple[str, str, float | None, float | None] | None":
    """analyze 结果信封 → 索引条目。失败信封/空答案 → None。evidence_ts(若为数值列表)→ 时段。"""
    ans = str(dump.get("answer") or "").strip()
    if not ans or ans.startswith("[分析失败") or ans.startswith("[ANALYZE_FAILED"):
        return None
    start = end = None
    ev = dump.get("evidence_ts")
    if isinstance(ev, (list, tuple)):
        nums = [float(x) for x in ev if isinstance(x, (int, float))]
        if nums:
            start, end = min(nums), max(nums)
    return (content_key, ans[:2000], start, end)


def upsert_params(entry: tuple, video_id: str, source: str, vec_lit: str) -> tuple:
    """(content_key, snippet, start, end) + 向量字面量 → UPSERT_SQL 参数元组。"""
    key, snippet, start, end = entry
    return (video_id, source, snippet, start, end, vec_lit, key)


# ── 运行时直连(读检索 + 写钩子;psycopg2 lazy 单连接)──────────────────
# 语义层与 Neon 直连,不走 MCP:MCP 是大脑的【通用只读 SQL】路;语义层是带参数化向量的
# 【专用类型化】读写路(与 uploads/user_memory 直连 Redis/GCS 同理)。全程 fail-open。
import logging as _logging
import os as _os
import threading as _threading

_log = _logging.getLogger("pipeline.semantic_index")
_CONN = None
_CONN_LOCK = _threading.Lock()
# 单模块级连接;psycopg2 连接【非线程安全】(不能并发跑游标)。写钩子跑在并行 analyze 的
# 线程池里,可能与 search 或彼此并发 → 用执行锁把游标操作串行化(单用户低频,串行代价可忽略)。
_EXEC_LOCK = _threading.RLock()


def _conn():
    global _CONN
    with _CONN_LOCK:
        if _CONN is None or getattr(_CONN, "closed", 1):
            import psycopg2
            _CONN = psycopg2.connect(
                host=_os.environ.get("ALLOYDB_HOST", "localhost"), port=5432,
                dbname=_os.environ.get("ALLOYDB_DB", "postgres"),
                user=_os.environ.get("ALLOYDB_USER", "postgres"),
                password=_os.environ.get("ALLOYDB_PASSWORD", ""),
                sslmode="require", connect_timeout=10,
                keepalives=1, keepalives_idle=30)
            _CONN.autocommit = True
        return _CONN


def _execute(sql: str, params: tuple):
    """带一次断线重连的执行(Neon 池会掐空闲连接)。执行锁串行化 → 共享连接线程安全。"""
    global _CONN
    with _EXEC_LOCK:                          # 同一时刻只有一个游标操作(连接非线程安全)
        for attempt in (0, 1):
            try:
                conn = _conn()
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.description and cur.fetchall() or []
            except Exception:
                with _CONN_LOCK:
                    try:
                        if _CONN is not None:
                            _CONN.close()
                    except Exception:
                        pass
                    _CONN = None
                if attempt:
                    raise
    return []


# 相似度弱相关阈:低于此 = 只是"最接近"不是"真相关"(治过度召回 —— 语义搜永不返回空,
# 库里没有你要的它也会塞几个最近的;必须让大脑看见"这些是弱的、其实没有匹配")。
WEAK_THRESHOLD = 0.6


def search(vec_lit: str, k: int) -> list[dict]:
    """pgvector 近邻检索 → 行列表(score 降序)。每行标 relevance(strong/weak)。异常上抛,调用方 fail-open。"""
    rows = _execute(SEARCH_SQL, (vec_lit, vec_lit, int(k)))
    return [{"n": i + 1, "video_id": r[0], "source": r[1], "snippet": r[2],
             "start_ts": r[3], "end_ts": r[4], "score": round(float(r[5]), 3),
             "relevance": "strong" if float(r[5]) >= WEAK_THRESHOLD else "weak",
             "label": (r[2] or "")[:40]}
            for i, r in enumerate(rows)]


def index_entry(video_id: str, source: str, entry: tuple, vec_lit: str) -> bool:
    """写一条索引(analyze 写钩子/回填共用)。失败返回 False(fail-open,绝不影响作答)。"""
    try:
        _execute(UPSERT_SQL, upsert_params(entry, video_id, source, vec_lit))
        return True
    except Exception as e:
        _log.warning("语义索引写入失败(fail-open): %r", e)
        return False
