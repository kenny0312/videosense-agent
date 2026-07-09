"""
跳伞专栏 —— 多模态抽取工具(离线批处理)。

对 video_metadata 里【还没抽取过】的视频,每个调一次 Gemini 多模态(直接读 GCS,
无需下载),识别受控跳伞阶段(出舱前/出舱/自由落体/开伞/开伞后/降落)+ 跳伞类型,
写【一行】skydive_segments。

★ Null-safety 贯穿:视频里没有的阶段 → 该阶段列写 NULL(不编、不填 0)。整段抽取
  失败(解析重试都失败)→ 跳过该视频、不写行,下次重跑再补(断点续跑),绝不让一个
  坏视频崩掉整批。表不存在会自动建(CREATE IF NOT EXISTS)。

运行:
    # .env 里配好 ALLOYDB_* / GCP_PROJECT / GCP_REGION;ADC 已登录
    python -m perception.skydive_extract
    PERCEPTION_MAX_VIDEOS=20 python -m perception.skydive_extract   # 限量

结构仿 perception/gemini_predicates.py(同一套连接/重试/断点续跑骨架)。
"""
from __future__ import annotations

import json
import os
import time

import psycopg2
import vertexai
from pydantic import ValidationError
from vertexai.generative_models import GenerativeModel, Part

from perception.skydive_schema import (
    JUMP_TYPES, SKYDIVE_PHASES, SkydiveExtraction,
    create_table_sql, insert_sql, row_values, to_row,
    video_facts_upsert_sql, video_facts_values, video_facts_predicates,
)

PROJECT_ID = os.environ.get("GCP_PROJECT", "your-gcp-project-id")
LOCATION   = os.environ.get("GCP_REGION", "us-central1")
MODEL_NAME = os.environ.get("PERCEPTION_MODEL", "gemini-2.5-flash")

DB_CONFIG = dict(
    host=os.environ.get("ALLOYDB_HOST", "localhost"),
    port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    sslmode="require",
    connect_timeout=10,
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
)

MAX_VIDEOS    = int(os.environ.get("PERCEPTION_MAX_VIDEOS", "50"))
RETRY_LIMIT   = 2
SLEEP_BETWEEN = 1.0


def _build_prompt() -> str:
    phase_lines = "\n".join(f'- "{k}": {desc}' for k, _, desc in SKYDIVE_PHASES)
    types = " / ".join(JUMP_TYPES)
    return f"""You are a skydiving video analyst. Watch the video and identify which CANONICAL SKYDIVE PHASES are actually visible, with timestamps.

Phases (a single jump goes roughly aircraft -> exit -> freefall -> deploy -> canopy -> landing):
{phase_lines}

CRITICAL: include ONLY phases that are truly present in THIS clip. If a phase is not shown (e.g. a freefall-only clip has no landing), OMIT it or set it to null — NEVER guess or invent timestamps. A clip may contain just one phase.

Also classify the jump:
- "jump_type": one of [{types}], or null if unclear
- "is_wingsuit": true / false / null
- "summary": one short sentence describing the jump

Respond ONLY with a JSON object (no markdown, no extra text). For each PRESENT phase give an object {{"start_ts": <sec>, "end_ts": <sec>, "confidence": <0-1>}}. For a brief moment (deploy/landing) set start_ts ~= end_ts. Omit or null any absent phase. Shape:
{{
  "freefall": {{"start_ts": 5.0, "end_ts": 48.0, "confidence": 0.95}},
  "deploy":   {{"start_ts": 48.0, "end_ts": 50.0, "confidence": 0.9}},
  "jump_type": "wingsuit", "is_wingsuit": true,
  "summary": "Wingsuit freefall ending in canopy deployment."
}}"""


PROMPT = _build_prompt()


def analyze_video(model: GenerativeModel, gcs_uri: str) -> SkydiveExtraction | None:
    """让 Gemini 抽取受控跳伞阶段;解析/校验都失败返回 None(由主流程跳过、下次重补)。"""
    video_part = Part.from_uri(uri=gcs_uri, mime_type="video/mp4")
    for attempt in range(RETRY_LIMIT + 1):
        try:
            resp = model.generate_content(
                [video_part, PROMPT],
                generation_config={"temperature": 0.1, "max_output_tokens": 4096,
                                   "response_mime_type": "application/json"},
            )
            raw = resp.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            # model_validate 对缺键/缺字段/显式 null 都不报错 → 全 None,null-safe
            return SkydiveExtraction.model_validate(data)
        except (json.JSONDecodeError, ValidationError, ValueError) as e:
            print(f"      [解析失败 attempt {attempt+1}] {e}")
        except Exception as e:
            print(f"      [API错误 attempt {attempt+1}] {e}")
            time.sleep(3)
    return None


def _present_phases(row: dict) -> str:
    """打印用:列出本视频实际检测到的阶段(start_ts 非 None 即视为检测到)。"""
    got = [k for k, _, _ in SKYDIVE_PHASES if row.get(f"{k}_start_ts") is not None]
    return ", ".join(got) if got else "(无可识别阶段)"


def _try_video_facts(conn, cur, vf_ins, video_id, row) -> None:
    """写一条可检索的 video_facts(独立提交,非致命):失败就回滚这步并跳过,
    绝不拖累已落库的 skydive_segments(也防缺约束等问题把整个 ingest 跑崩)。"""
    try:
        for pred in video_facts_predicates(row):     # 总写 "skydiving",wingsuit 再加一条
            cur.execute(vf_ins, video_facts_values(video_id, row, pred))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"  [video_facts 跳过] {str(e).strip()[:60]}")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="跳伞阶段离线抽取(可按 GCS 前缀限定)")
    ap.add_argument("--prefix", default=os.environ.get("PERCEPTION_PREFIX", ""),
                    help="只抽 gcs_uri 含此子串的视频(如 videos/skydive/);留空=全部未抽取的")
    ap.add_argument("--max", type=int, default=MAX_VIDEOS, help=f"最多抽几个(默认 {MAX_VIDEOS})")
    args = ap.parse_args()

    password = os.environ.get("ALLOYDB_PASSWORD") or input("DB 密码 (Neon/AlloyDB): ")
    DB_CONFIG["password"] = password

    print(f"\n初始化 Vertex AI (project={PROJECT_ID}, location={LOCATION})...")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel(MODEL_NAME)
    print(f"模型加载完成: {MODEL_NAME}")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(create_table_sql())          # 表不存在则建(幂等)
    conn.commit()

    # 断点续跑:跳过已抽取过的视频;--prefix 时只抽该 GCS 前缀下的(如某个 collection)
    if args.prefix:
        cur.execute(
            "SELECT vm.video_id, vm.gcs_uri FROM video_metadata vm "
            "WHERE vm.gcs_uri LIKE %s "
            "AND vm.video_id NOT IN (SELECT video_id FROM skydive_segments) "
            "ORDER BY vm.video_id LIMIT %s",
            (f"%{args.prefix}%", args.max))
    else:
        cur.execute(
            "SELECT vm.video_id, vm.gcs_uri FROM video_metadata vm "
            "WHERE vm.video_id NOT IN (SELECT video_id FROM skydive_segments) "
            "ORDER BY vm.video_id LIMIT %s",
            (args.max,))
    videos = cur.fetchall()
    scope = f"(前缀 {args.prefix})" if args.prefix else "(全部未抽取)"
    print(f"\n待抽取视频 {scope}: {len(videos)} 个\n")

    written = failed = 0
    ins = insert_sql("%s")
    vf_ins = video_facts_upsert_sql("%s")    # 同步写一条可被常规视频查询检索的 video_facts
    for i, (video_id, gcs_uri) in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {video_id}")
        ext = analyze_video(model, gcs_uri)
        if ext is None:
            failed += 1
            print("  FAILED(跳过,下次重补)\n")
            continue
        row = to_row(video_id, ext)          # 缺席阶段 → None;派生 freefall_sec null-safe
        try:
            cur.execute(ins, row_values(row))
            conn.commit()                    # 主数据 skydive_segments 先落
            written += 1
            _try_video_facts(conn, cur, vf_ins, video_id, row)   # 可检索行:非致命,失败不拖累主数据
            ff = row.get("freefall_sec")
            print(f"  + 阶段: {_present_phases(row)}"
                  f"  | type={row.get('jump_type')}"
                  f"  | freefall={ff if ff is not None else '—'}s\n")
        except psycopg2.OperationalError as e:
            print(f"  [DB 掉线,重连重写] {str(e).strip()[:50]}")
            try: conn.close()
            except Exception: pass
            conn = psycopg2.connect(**DB_CONFIG); conn.autocommit = False; cur = conn.cursor()
            try:
                cur.execute(ins, row_values(row)); conn.commit(); written += 1
                _try_video_facts(conn, cur, vf_ins, video_id, row)
            except Exception as e2:
                print(f"  [重写仍失败] {e2}"); failed += 1
        time.sleep(SLEEP_BETWEEN)

    cur.close(); conn.close()
    print("=" * 50)
    print(f"完成! 写入: {written} 行  失败/跳过: {failed} 个")
    print("=" * 50)


if __name__ == "__main__":
    main()
