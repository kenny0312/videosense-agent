"""
跳伞专栏 —— 受控词表 + 元数据表 schema(单一真源)。

一个视频 = skydive_segments 表里【一行】。受控的阶段(出舱前/出舱/自由落体/开伞/
开伞后/降落)各占一组列。

★ Null-safety(本模块的核心设计):**不是每个视频都有全部阶段**(只拍了自由落体的
  片段就没有 landing;座舱视角可能没有 canopy)。缺席的阶段一律 NULL —— **绝不**用
  0.0 之类假值冒充(旧 Activity 模型 start_ts=0.0 那种默认就是"空 feature 崩溃"的根源:
  0.0 会被下游当成"第 0 秒发生过")。这里所有字段都是 Optional 且默认 None,缺了就是
  真 NULL,下游用 IS NOT NULL / COALESCE 取数,空 feature 不会让任何环节崩。

一次定义,多处复用:
  · SKYDIVE_PHASES / JUMP_TYPES —— 受控词表(也喂给抽取 prompt)
  · SkydiveExtraction(Pydantic)—— Gemini 抽取输出契约,全部 Optional、默认 None
  · create_table_sql()         —— Postgres/SQLite 通用 DDL(所有阶段列 NULLABLE)
  · mock_schema()              —— 给 mock get_schema 的 PG 风格列定义
  · to_row() / COLUMNS / insert_sql() —— 抽取结果 → 表行(派生指标 null-safe)
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

# ── 受控词表:跳伞阶段 (key, 中文名, 判定说明)。每个阶段都【可能缺席】。
SKYDIVE_PHASES: list[tuple[str, str, str]] = [
    ("aircraft", "出舱前/机内", "跳出之前在飞机内的画面"),
    ("exit",     "出舱",        "离开飞机的瞬间/动作"),
    ("freefall", "自由落体",    "主伞未开、自由下落(强气流、四肢张开)"),
    ("deploy",   "开伞",        "主伞从拖出到充气张开的过程/瞬间"),
    ("canopy",   "开伞后/伞降",  "伞已张开、在伞下滑翔下降"),
    ("landing",  "降落",        "接近地面到触地着陆"),
]
# 受控词表:跳伞类型(jump_type 取值范围;判不出留 None)
JUMP_TYPES = ["wingsuit", "freefly", "belly", "tracking", "tandem", "base", "other"]

PHASE_KEYS = [k for k, _, _ in SKYDIVE_PHASES]


class PhaseSpan(BaseModel):
    """一个阶段的时间段;缺测一律 None(不是 0.0)。"""
    start_ts:   Optional[float] = None
    end_ts:     Optional[float] = None
    confidence: Optional[float] = None


class SkydiveExtraction(BaseModel):
    """Gemini 对【一个视频】的跳伞抽取结果。所有字段 Optional —— 视频里没有的阶段
    就让它 None(不要编)。Pydantic 对缺键/缺字段/显式 null 都不报错,这是 null-safe
    的第一道防线。"""
    aircraft: Optional[PhaseSpan] = None
    exit:     Optional[PhaseSpan] = None
    freefall: Optional[PhaseSpan] = None
    deploy:   Optional[PhaseSpan] = None
    canopy:   Optional[PhaseSpan] = None
    landing:  Optional[PhaseSpan] = None
    jump_type:   Optional[str]  = None       # 取自 JUMP_TYPES
    is_wingsuit: Optional[bool] = None
    summary:     Optional[str]  = None        # 一句话内容概述


# ── 表列(DDL / INSERT / mock 共用一份,避免漂移)──────────────────────
def _phase_cols() -> list[str]:
    out: list[str] = []
    for k in PHASE_KEYS:
        out += [f"{k}_start_ts", f"{k}_end_ts", f"{k}_confidence"]
    return out


# INSERT 时我们显式提供的列(extracted_at 走 DB 默认值,不在此列)
COLUMNS: list[str] = (["video_id"] + _phase_cols()
                      + ["jump_type", "is_wingsuit", "summary", "freefall_sec"])

_COL_PG_TYPE: dict[str, str] = {
    "video_id": "character varying", "jump_type": "character varying",
    "is_wingsuit": "boolean", "summary": "text", "freefall_sec": "double precision",
}
for _c in _phase_cols():
    _COL_PG_TYPE[_c] = "double precision"


def create_table_sql() -> str:
    """Postgres 与 SQLite 通用 DDL(所有阶段列 NULLABLE;用 CURRENT_TIMESTAMP 而非
    NOW() 以便 mock 的 SQLite 也能执行)。"""
    cols = ["video_id VARCHAR PRIMARY KEY REFERENCES video_metadata(video_id)"]
    for k in PHASE_KEYS:
        cols += [f"{k}_start_ts FLOAT", f"{k}_end_ts FLOAT", f"{k}_confidence FLOAT"]
    cols += [
        "jump_type VARCHAR",            # 受控:wingsuit/freefly/belly/tracking/tandem/base/other
        "is_wingsuit BOOLEAN",
        "summary TEXT",
        "freefall_sec FLOAT",           # 派生:freefall_end - freefall_start(缺端 → NULL)
        "extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    ]
    return "CREATE TABLE IF NOT EXISTS skydive_segments (\n    " + ",\n    ".join(cols) + "\n);"


def mock_schema() -> list[dict]:
    """给 mock get_schema 的 PG 风格列定义(与 _SCHEMA_FOR_LLM 其它表同格式)。"""
    rows = [{"column": c, "type": _COL_PG_TYPE[c]} for c in COLUMNS]
    rows.append({"column": "extracted_at", "type": "timestamp without time zone"})
    return rows


def to_row(video_id: str, ext: SkydiveExtraction) -> dict:
    """抽取结果 → 表行(dict,键 = COLUMNS)。缺席阶段全是 None;派生 freefall_sec
    仅当两端都在且有效时才算,否则 None —— null-safe,绝不抛 KeyError/TypeError。"""
    row: dict = {"video_id": video_id}
    for k in PHASE_KEYS:
        span = getattr(ext, k) or PhaseSpan()
        row[f"{k}_start_ts"]   = span.start_ts
        row[f"{k}_end_ts"]     = span.end_ts
        row[f"{k}_confidence"] = span.confidence
    row["jump_type"]   = ext.jump_type
    row["is_wingsuit"] = ext.is_wingsuit
    row["summary"]     = ext.summary
    ff = ext.freefall
    row["freefall_sec"] = (
        round(ff.end_ts - ff.start_ts, 2)
        if (ff is not None and ff.start_ts is not None and ff.end_ts is not None
            and ff.end_ts >= ff.start_ts)
        else None
    )
    return row


def row_values(row: dict) -> tuple:
    """按 COLUMNS 顺序取值(缺键 → None,再防一层)。"""
    return tuple(row.get(c) for c in COLUMNS)


def insert_sql(placeholder: str = "%s") -> str:
    """INSERT 语句;placeholder='%s'(psycopg2)或 '?'(sqlite)。ON CONFLICT 幂等。"""
    cols = ", ".join(COLUMNS)
    ph = ", ".join([placeholder] * len(COLUMNS))
    return (f"INSERT INTO skydive_segments ({cols})\n"
            f"VALUES ({ph})\nON CONFLICT (video_id) DO NOTHING;")


# ── 桥接:跳伞抽取 → 一条可被【常规视频查询】检索的 video_facts ──────────────
# 跳伞视频只落 skydive_segments,而 loop 大脑答"有什么视频/类别/有没有 skydiving"是查 video_facts.predicate。
# 入库时顺手写一条 video_facts(predicate 据类型派生),新传的跳伞视频就【天生可检索】,不会再"查不到"。
def video_facts_upsert_sql(placeholder: str = "%s") -> str:
    """幂等:ON CONFLICT (video_id, predicate) DO NOTHING(video_facts 上有 uq_facts_vid_pred)。"""
    p = placeholder
    return ("INSERT INTO video_facts (video_id, predicate, matched, confidence, rationale, start_ts, end_ts) "
            f"VALUES ({p}, {p}, true, {p}, {p}, {p}, {p}) "
            "ON CONFLICT (video_id, predicate) DO NOTHING")


def video_facts_values(video_id: str, row: dict) -> tuple:
    """跳伞行 → video_facts 值:predicate 据 is_wingsuit/jump_type 派生;复用 summary 作 rationale、freefall 时段。"""
    is_ws = bool(row.get("is_wingsuit")) or row.get("jump_type") == "wingsuit"
    predicate = "wingsuit skydiving" if is_ws else "skydiving"
    conf = row.get("freefall_confidence")
    return (video_id, predicate,
            conf if conf is not None else 1.0,
            row.get("summary"),
            row.get("freefall_start_ts"), row.get("freefall_end_ts"))
