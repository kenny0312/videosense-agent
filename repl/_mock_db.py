"""
第6阶段 — Mock DB(内存 SQLite,$0 替代 AlloyDB)

启用方式:
    set REPL_USE_MOCK_DB=1            (cmd)
    $env:REPL_USE_MOCK_DB = "1"       (PowerShell)
    python -m pipeline.main           (或 api.server / mcp_server.server)

内置数据:
    - 12 个视频(贴近 ActivityNet 风格:滑雪 / 化妆 / 烘焙 / 运动 / 跳舞 等)
    - ~50 条 video_facts,confidence 分布在 0.5~1.0,既有 matched 也有 unmatched
    - 数据足够回答您 4 道测试题

注意 ── SQLite 没有 ILIKE / TEXT[] / ::text 这些 Postgres 语法:
    mock_run_sql() 内置一个小翻译器,把 LLM 写的 PG 风格 SQL 转成 SQLite 风格,
    所以 prompt 不用改、LLM 不用知道自己在 mock 上。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from typing import Any

from perception.skydive_schema import (
    COLUMNS as SKY_COLUMNS, PhaseSpan, SkydiveExtraction,
    create_table_sql as sky_create_table_sql, mock_schema as sky_mock_schema, to_row as sky_to_row,
)

# ── 单例 connection(线程安全) ──
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


# ════════════════════════════════════════════════════
#  Schema  ──  与 AlloyDB 对齐(类型注释保留 PG 风格)
# ════════════════════════════════════════════════════

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS video_metadata (
    video_id        TEXT PRIMARY KEY,
    title           TEXT,
    gcs_uri         TEXT,
    duration_sec    REAL
);

CREATE TABLE IF NOT EXISTS video_discovery (
    video_id        TEXT PRIMARY KEY,
    all_activities  TEXT          -- JSON 列表,模拟 PG 的 TEXT[]
);

CREATE TABLE IF NOT EXISTS video_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT,
    predicate       TEXT,
    matched         INTEGER,      -- 0 / 1
    confidence      REAL,
    rationale       TEXT,
    start_ts        REAL,
    end_ts          REAL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS video_fact_instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id         INTEGER,
    ts              REAL,
    frame_count     INTEGER
);
"""

# 对外暴露的"schema 字典"——和真 fetch_schema 的格式一致(Postgres 风格类型名)
_SCHEMA_FOR_LLM = {
    "video_metadata": [
        {"column": "video_id",     "type": "character varying"},
        {"column": "title",        "type": "text"},
        {"column": "gcs_uri",      "type": "text"},
        {"column": "duration_sec", "type": "double precision"},
    ],
    "video_discovery": [
        {"column": "video_id",       "type": "character varying"},
        {"column": "all_activities", "type": "ARRAY"},
    ],
    "video_facts": [
        {"column": "id",         "type": "integer"},
        {"column": "video_id",   "type": "character varying"},
        {"column": "predicate",  "type": "character varying"},
        {"column": "matched",    "type": "boolean"},
        {"column": "confidence", "type": "double precision"},
        {"column": "rationale",  "type": "text"},
        {"column": "start_ts",   "type": "double precision"},
        {"column": "end_ts",     "type": "double precision"},
        {"column": "created_at", "type": "timestamp without time zone"},
    ],
    "video_fact_instances": [
        {"column": "id",          "type": "integer"},
        {"column": "fact_id",     "type": "integer"},
        {"column": "ts",          "type": "double precision"},
        {"column": "frame_count", "type": "integer"},
    ],
    # 跳伞专栏:受控阶段元数据(列定义由 perception.skydive_schema 单源生成)
    "skydive_segments": sky_mock_schema(),
}


# ════════════════════════════════════════════════════
#  Seed data  ──  12 个视频 + ~50 条 facts
# ════════════════════════════════════════════════════

# (video_id, title, gcs_uri, duration_sec, [activities ...])
VIDEOS = [
    ("v001", "Skiing in Aspen",                "gs://activitynet/v001.mp4", 45.0, ["skiing", "snow"]),
    ("v002", "Snowboarding Slopes",            "gs://activitynet/v002.mp4", 38.0, ["snowboarding", "jumping"]),
    ("v003", "Backcountry Snowboarding Run",   "gs://activitynet/v003.mp4", 52.0, ["snowboarding", "skiing", "mountain"]),
    ("v004", "Makeup Tutorial Mascara",        "gs://activitynet/v004.mp4", 21.0, ["applying mascara"]),
    ("v005", "Hair Braiding French Style",     "gs://activitynet/v005.mp4", 33.0, ["braiding hair"]),
    ("v006", "Baking Chocolate Chip Cookies",  "gs://activitynet/v006.mp4", 60.0, ["baking cookies", "mixing"]),
    ("v007", "Grill Cooking BBQ Ribs",         "gs://activitynet/v007.mp4", 75.0, ["cooking on grill", "BBQ"]),
    ("v008", "Riding Mountain Bike Trail",     "gs://activitynet/v008.mp4", 28.0, ["riding bike", "riding mountain bike"]),
    ("v009", "Skateboard Tricks Park",         "gs://activitynet/v009.mp4", 42.0, ["skateboarding", "jumping"]),
    ("v010", "Basketball Three Pointers",      "gs://activitynet/v010.mp4", 50.0, ["playing basketball"]),
    ("v011", "Salsa Dancing Lessons",          "gs://activitynet/v011.mp4", 36.0, ["dancing salsa", "dancing"]),
    ("v012", "Walking Dog in Park",            "gs://activitynet/v012.mp4", 18.0, ["walking dog", "walking"]),
    # ── 跳伞专栏 mock 视频(用于演示 skydive_segments;注意阶段【故意各有缺失】以证明 null-safe)──
    ("sky01", "Wingsuit Jump Over Alps",       "gs://activitynet/sky01.mp4", 130.0, ["skydiving", "wingsuit flight"]),
    ("sky02", "Freefall Headcam Clip",         "gs://activitynet/sky02.mp4",  48.0, ["skydiving", "freefall"]),
    ("sky03", "Belly Jump Full Sequence",      "gs://activitynet/sky03.mp4", 135.0, ["skydiving"]),
    ("sky04", "Wingsuit Flight (cut, no landing)", "gs://activitynet/sky04.mp4", 90.0, ["skydiving", "wingsuit flight"]),
]

# 跳伞阶段 seed —— 用 SkydiveExtraction 构造(走与线上抽取同一条 to_row 路径)。
# 故意覆盖不同完整度:sky01 全阶段 / sky02 只有 freefall / sky03 无 aircraft / sky04 无 landing。
def _sp(s, e, c):   # 简写一个 PhaseSpan
    return PhaseSpan(start_ts=s, end_ts=e, confidence=c)

SKYDIVE_SEED = [
    ("sky01", SkydiveExtraction(
        aircraft=_sp(0, 9, 0.9), exit=_sp(9, 11, 0.95), freefall=_sp(11, 62, 0.97),
        deploy=_sp(62, 65, 0.93), canopy=_sp(65, 126, 0.9), landing=_sp(126, 130, 0.88),
        jump_type="wingsuit", is_wingsuit=True, summary="Full wingsuit jump from exit to landing.")),
    ("sky02", SkydiveExtraction(                                   # 只有自由落体(其余阶段 → NULL)
        freefall=_sp(2, 46, 0.96),
        jump_type="freefly", is_wingsuit=False, summary="Headcam freefall-only clip, no deployment shown.")),
    ("sky03", SkydiveExtraction(                                   # 无 aircraft 段
        exit=_sp(0, 2, 0.9), freefall=_sp(2, 58, 0.95), deploy=_sp(58, 61, 0.9),
        canopy=_sp(61, 130, 0.88), landing=_sp(130, 135, 0.85),
        jump_type="belly", is_wingsuit=False, summary="Belly-to-earth jump, exit through landing.")),
    ("sky04", SkydiveExtraction(                                   # 翼装,剪掉了落地(landing → NULL)
        exit=_sp(0, 2, 0.92), freefall=_sp(2, 20, 0.9), deploy=_sp(20, 23, 0.9),
        canopy=_sp(23, 90, 0.87),
        jump_type="wingsuit", is_wingsuit=True, summary="Wingsuit flight under canopy, clip ends before landing.")),
]

# (video_id, predicate, matched, confidence, rationale, start_ts, end_ts)
FACTS = [
    # v001 — 5 条,涵盖 0.55 ~ 0.95
    ("v001", "skiing",           1, 0.95, "Person clearly skiing downhill",          2.0, 42.0),
    ("v001", "snow",             1, 0.92, "Heavy snow cover throughout",             0.0, 45.0),
    ("v001", "cold weather",     1, 0.78, "Visible breath and winter clothing",      0.0, 45.0),
    ("v001", "wearing goggles",  1, 0.55, "Brief glimpse of ski goggles",           12.0, 18.0),
    ("v001", "ice skating",      0, 0.10, "Activity is skiing, not ice skating",     0.0,  0.0),

    # v002 — 4 条
    ("v002", "snowboarding",     1, 0.96, "Person on snowboard down slope",          3.0, 36.0),
    ("v002", "jumping",          1, 0.85, "Multiple jumps observed",                 8.0, 22.0),
    ("v002", "falling",          1, 0.68, "One fall at 28s mark",                   27.0, 30.0),
    ("v002", "wearing helmet",   1, 0.59, "Helmet visible occasionally",             0.0, 38.0),

    # v003 — 6 条,活动种类最多
    ("v003", "snowboarding",     1, 0.94, "Primary activity throughout",             2.0, 50.0),
    ("v003", "skiing",           1, 0.74, "Brief skier passes in background",       18.0, 23.0),
    ("v003", "mountain",         1, 0.91, "Clear alpine background",                 0.0, 52.0),
    ("v003", "jumping",          1, 0.83, "Cliff drop at 35s",                      34.0, 38.0),
    ("v003", "wearing helmet",   1, 0.72, "Helmet on rider",                         0.0, 52.0),
    ("v003", "filming with camera", 1, 0.66, "Person filming visible at edges",      5.0, 48.0),

    # v004 — 3 条
    ("v004", "applying mascara", 1, 0.93, "Close-up of mascara application",         1.0, 20.0),
    ("v004", "looking in mirror",1, 0.79, "Mirror visible most frames",              0.0, 21.0),
    ("v004", "doing eye makeup", 1, 0.88, "Eye makeup actions throughout",           1.0, 20.0),

    # v005 — 3 条
    ("v005", "braiding hair",    1, 0.91, "French braiding technique shown",         0.0, 33.0),
    ("v005", "sitting",          1, 0.66, "Subject seated entire video",             0.0, 33.0),
    ("v005", "combing hair",     1, 0.82, "Combing visible at start",                0.0,  5.0),

    # v006 — 5 条
    ("v006", "baking cookies",   1, 0.95, "Chocolate chip cookies being baked",      0.0, 60.0),
    ("v006", "mixing",           1, 0.88, "Mixing batter at 5-15s",                  5.0, 15.0),
    ("v006", "using oven",       1, 0.82, "Oven open at 35s",                       35.0, 38.0),
    ("v006", "rolling dough",    1, 0.62, "Brief rolling action",                   18.0, 22.0),
    ("v006", "decorating cake",  0, 0.18, "Cookies not a cake",                      0.0,  0.0),

    # v007 — 4 条
    ("v007", "cooking on grill", 1, 0.93, "BBQ ribs on grill throughout",            0.0, 75.0),
    ("v007", "BBQ",              1, 0.89, "Classic BBQ setup",                       0.0, 75.0),
    ("v007", "applying sauce",   1, 0.75, "Sauce brushed at 30-40s",                30.0, 40.0),
    ("v007", "flipping food",    1, 0.68, "Ribs flipped at 50s",                    50.0, 52.0),

    # v008 — 3 条
    ("v008", "riding bike",      1, 0.94, "Mountain biking on trail",                0.0, 28.0),
    ("v008", "riding mountain bike", 1, 0.90, "Off-road terrain visible",            0.0, 28.0),
    ("v008", "going uphill",     1, 0.71, "Climbing section at 10-18s",             10.0, 18.0),

    # v009 — 4 条
    ("v009", "skateboarding",    1, 0.92, "Skateboard tricks in park",               0.0, 42.0),
    ("v009", "jumping",          1, 0.85, "Ollie and jump tricks",                   5.0, 38.0),
    ("v009", "falling",          1, 0.62, "Fall at 25s",                            24.0, 27.0),
    ("v009", "wearing helmet",   0, 0.25, "No helmet visible",                       0.0,  0.0),

    # v010 — 4 条
    ("v010", "playing basketball", 1, 0.96, "Three-point shooting practice",         0.0, 50.0),
    ("v010", "dribbling",        1, 0.88, "Dribbling before shots",                  2.0, 48.0),
    ("v010", "shooting hoop",    1, 0.85, "Successful three-pointers",               0.0, 50.0),
    ("v010", "running",          1, 0.58, "Brief running to retrieve ball",         20.0, 25.0),

    # v011 — 3 条
    ("v011", "dancing salsa",    1, 0.93, "Salsa dancing lesson",                    0.0, 36.0),
    ("v011", "dancing",          1, 0.97, "Continuous dance throughout",             0.0, 36.0),
    ("v011", "wearing dress",    1, 0.76, "Dance attire visible",                    0.0, 36.0),

    # v012 — 4 条
    ("v012", "walking dog",      1, 0.95, "Person walking dog in park",              0.0, 18.0),
    ("v012", "walking",          1, 0.88, "Continuous walking motion",               0.0, 18.0),
    ("v012", "park scenery",     1, 0.83, "Park environment throughout",             0.0, 18.0),
    ("v012", "running",          1, 0.52, "Brief jog at end",                       15.0, 18.0),
]


# ════════════════════════════════════════════════════
#  Init  ──  建表 + 灌数据
# ════════════════════════════════════════════════════

def _active_seeds():
    """GD-2 双世界:MOCK_WORLD=B → 世界 B 种子(repl/_mock_world_b.py,跳伞表为空)。
    默认世界 A(原 16 视频,金标冻结不动)。EvalBackend 每场重灌(mock._conn=None)时按
    env 切换 —— 两个世界永不同场。"""
    import os as _os
    w = _os.environ.get("MOCK_WORLD", "A").upper()
    if w == "B":
        from repl._mock_world_b import VIDEOS_B, FACTS_B
        return VIDEOS_B, FACTS_B, []
    if w == "C":
        from repl._mock_world_c import VIDEOS_C, FACTS_C
        return VIDEOS_C, FACTS_C, []
    if w == "D":
        from repl._mock_world_d import VIDEOS_D, FACTS_D
        return VIDEOS_D, FACTS_D, []
    return VIDEOS, FACTS, SKYDIVE_SEED


def _init_conn() -> sqlite3.Connection:
    videos, facts, sky_seed = _active_seeds()
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_DDL)

    # video_metadata
    conn.executemany(
        "INSERT INTO video_metadata(video_id, title, gcs_uri, duration_sec) VALUES (?,?,?,?)",
        [(v[0], v[1], v[2], v[3]) for v in videos],
    )

    # video_discovery — all_activities 存成 JSON 字符串
    conn.executemany(
        "INSERT INTO video_discovery(video_id, all_activities) VALUES (?, ?)",
        [(v[0], json.dumps(v[4], ensure_ascii=False)) for v in videos],
    )

    # video_facts
    conn.executemany(
        "INSERT INTO video_facts(video_id, predicate, matched, confidence, "
        "rationale, start_ts, end_ts) VALUES (?,?,?,?,?,?,?)",
        facts,
    )

    # video_fact_instances — 为每条 matched fact 在 [start_ts, end_ts] 上
    # 按 1 秒间隔生成逐帧实例,给 Stage 7/8 的时序对齐/插值提供真实时间序列。
    instances = []
    for row in conn.execute(
        "SELECT id, matched, start_ts, end_ts FROM video_facts"
    ).fetchall():
        fid, matched, s, e = row["id"], row["matched"], row["start_ts"], row["end_ts"]
        if not matched or e is None or s is None or e <= s:
            continue
        t = float(s)
        while t <= float(e):
            instances.append((fid, round(t, 3), 30))   # 30 frames/sec 占位
            t += 1.0
    conn.executemany(
        "INSERT INTO video_fact_instances(fact_id, ts, frame_count) VALUES (?,?,?)",
        instances,
    )

    # ── 跳伞专栏:建表(单源 DDL,CURRENT_TIMESTAMP 兼容 SQLite)+ 灌 seed(世界 B 为空表)──
    conn.executescript(sky_create_table_sql())
    sky_rows = [sky_to_row(vid, ext) for vid, ext in sky_seed]   # 缺席阶段 → None → SQL NULL
    conn.executemany(
        f"INSERT INTO skydive_segments ({', '.join(SKY_COLUMNS)}) "
        f"VALUES ({', '.join(['?'] * len(SKY_COLUMNS))})",
        [tuple(r[c] for c in SKY_COLUMNS) for r in sky_rows],
    )

    conn.commit()
    return conn


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _init_conn()
    return _conn


# ════════════════════════════════════════════════════
#  PG → SQLite 小翻译器
# ════════════════════════════════════════════════════

_TRANSLATIONS = [
    # ILIKE → LIKE (SQLite LIKE ASCII 默认不区分大小写)
    (re.compile(r'\bILIKE\b', re.IGNORECASE),      'LIKE'),
    # 去掉 ::text / ::int / ::float 等所有显式 cast
    (re.compile(r'::\w+'),                          ''),
    # NOW() → datetime('now')
    (re.compile(r'\bNOW\(\)', re.IGNORECASE),       "datetime('now')"),
    # CURRENT_DATE → date('now')
    (re.compile(r'\bCURRENT_DATE\b', re.IGNORECASE), "date('now')"),
    # array_length(x, 1) → json_array_length(x)  ← all_activities 是 JSON 字符串
    (re.compile(r'\barray_length\s*\(\s*([^,]+)\s*,\s*\d+\s*\)', re.IGNORECASE),
     r'json_array_length(\1)'),
    # cardinality(x) → json_array_length(x)
    (re.compile(r'\bCARDINALITY\s*\(\s*([^)]+)\s*\)', re.IGNORECASE),
     r'json_array_length(\1)'),
    # array_to_string(x, sep) → 用 JSON 字段直接 LIKE 即可,这里简单返回原 JSON
    (re.compile(r'\barray_to_string\s*\(\s*([^,]+)\s*,[^)]+\)', re.IGNORECASE),
     r'\1'),
]


def _translate(sql: str) -> str:
    for pat, repl in _TRANSLATIONS:
        sql = pat.sub(repl, sql)
    return sql


# ════════════════════════════════════════════════════
#  对外 API ── 与 generator.py 现有签名一致
# ════════════════════════════════════════════════════

def mock_fetch_schema() -> dict:
    """格式与真 fetch_schema 一致 ── PG 风格类型名,迷惑 LLM 不要紧。"""
    return _SCHEMA_FOR_LLM


def mock_run_sql(sql: str) -> list[dict]:
    """
    1. 只允许 SELECT
    2. 把 PG 风格语法翻译成 SQLite 风格
    3. 返回 list[dict] (跟 RealDictCursor 一致)
    """
    from pipeline.sql_guard import is_read_only
    if not is_read_only(sql):
        raise ValueError("只允许只读查询(SELECT / WITH ... SELECT)")

    translated = _translate(sql)
    conn = _get_conn()
    cur = conn.execute(translated)
    rows = cur.fetchall()
    # sqlite3.Row → dict
    return [dict(r) for r in rows]


# ── 自检 ──
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    print(f"videos: {len(VIDEOS)}, facts: {len(FACTS)}")
    print("\n--- self-check queries ---\n")

    print("[Q1] total videos:")
    print(" ", mock_run_sql("SELECT COUNT(*) AS n FROM video_metadata"))

    print("\n[Q2] top snow-related facts (tests ILIKE translation):")
    for row in mock_run_sql(
        "SELECT video_id, predicate, confidence FROM video_facts "
        "WHERE predicate ILIKE '%snow%' AND matched = 1 "
        "ORDER BY confidence DESC LIMIT 5"
    ):
        print(" ", row)

    print("\n[Q3] activities array for v003 (tests TEXT[] mock):")
    print(" ", mock_run_sql("SELECT all_activities FROM video_discovery WHERE video_id = 'v003'"))

    print("\n[Q4] confidence histogram (tests aggregation):")
    for row in mock_run_sql(
        "SELECT CAST(confidence * 10 AS INTEGER) AS bucket, COUNT(*) AS n "
        "FROM video_facts GROUP BY bucket ORDER BY bucket"
    ):
        print(" ", row)
