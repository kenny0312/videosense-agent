"""
第2阶段：多模态感知原子化 (Gemini Predicates)
- 从 video_metadata 读取 GCS URI
- 调用 Gemini 对每个视频做谓词判断
- 结果写入 video_facts 表

运行方式: python gemini_predicates.py
"""

import os
import json
import time
import psycopg2
import vertexai
from vertexai.generative_models import GenerativeModel, Part
from pydantic import BaseModel, ValidationError

# ── 配置(从环境变量读取,默认占位符)──────────────────────────────────────────
PROJECT_ID  = os.environ.get("GCP_PROJECT", "your-gcp-project-id")
LOCATION    = os.environ.get("GCP_REGION", "us-central1")
MODEL_NAME  = "gemini-1.5-flash-002"

DB_CONFIG = dict(
    host=os.environ.get("ALLOYDB_HOST", "localhost"),
    port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    sslmode="require",
    connect_timeout=10,
)

# 谓词列表（对 ActivityNet 动作类别有代表性）
PREDICATES = [
    "running or jogging",
    "jumping or leaping",
    "swimming",
    "playing ball sports",
    "cooking or food preparation",
]

MAX_VIDEOS   = 10   # 先跑 10 个测试，没问题再改成 100
RETRY_LIMIT  = 2    # Gemini 调用失败重试次数
SLEEP_BETWEEN = 1.5 # 每次调用间隔（秒），避免触发限速


# ── Pydantic Schema（大纲 PredicateResultV1）──────────────────────────────────
class PredicateResult(BaseModel):
    matched:    bool
    confidence: float   # 0.0 ~ 1.0
    rationale:  str
    start_ts:   float   # 匹配片段起始秒（不确定时填 0.0）
    end_ts:     float   # 匹配片段结束秒（不确定时填 0.0）


# ── Gemini 调用 ───────────────────────────────────────────────────────────────
def analyze_video(model: GenerativeModel, gcs_uri: str, predicate: str) -> PredicateResult | None:
    """
    传入 GCS URI + 谓词，返回 PredicateResult。
    Gemini 直接读 GCS，无需下载视频。
    """
    video_part = Part.from_uri(uri=gcs_uri, mime_type="video/mp4")

    prompt = f"""You are a video analysis expert. Watch the video and determine whether the following activity is present:

Activity to detect: "{predicate}"

Respond ONLY with a valid JSON object (no markdown, no extra text) in this exact format:
{{
  "matched": true or false,
  "confidence": 0.0 to 1.0,
  "rationale": "one sentence explaining your decision",
  "start_ts": start time in seconds where activity occurs (0.0 if not found or uncertain),
  "end_ts": end time in seconds where activity ends (0.0 if not found or uncertain)
}}"""

    for attempt in range(RETRY_LIMIT + 1):
        try:
            response = model.generate_content(
                [video_part, prompt],
                generation_config={"temperature": 0.1, "max_output_tokens": 300},
            )
            raw = response.text.strip()

            # 清理可能的 markdown code block
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            return PredicateResult(**data)

        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            print(f"      [解析失败 attempt {attempt+1}] {e}")
        except Exception as e:
            print(f"      [API错误 attempt {attempt+1}] {e}")
            time.sleep(3)

    return None


# ── 数据库写入 ────────────────────────────────────────────────────────────────
INSERT_SQL = """
INSERT INTO video_facts
    (video_id, predicate, matched, confidence, rationale, start_ts, end_ts)
VALUES
    (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING;
"""

def save_result(cur, video_id: str, predicate: str, result: PredicateResult):
    cur.execute(INSERT_SQL, (
        video_id,
        predicate,
        result.matched,
        result.confidence,
        result.rationale,
        result.start_ts,
        result.end_ts,
    ))


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    password = input("AlloyDB postgres 密码: ")
    DB_CONFIG["password"] = password

    # 初始化 Vertex AI
    print(f"\n初始化 Vertex AI (project={PROJECT_ID}, location={LOCATION})...")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel(MODEL_NAME)
    print(f"模型加载完成: {MODEL_NAME}")

    # 连接数据库
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    # 读取待处理视频（跳过已有 facts 的视频）
    cur.execute(f"""
        SELECT vm.video_id, vm.gcs_uri
        FROM video_metadata vm
        WHERE vm.video_id NOT IN (
            SELECT DISTINCT video_id FROM video_facts
        )
        LIMIT {MAX_VIDEOS};
    """)
    videos = cur.fetchall()
    print(f"\n待处理视频: {len(videos)} 个\n")

    total_written = 0
    total_failed  = 0

    for i, (video_id, gcs_uri) in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {video_id}")
        print(f"  URI: {gcs_uri}")

        for predicate in PREDICATES:
            print(f"  谓词: {predicate!r} ...", end=" ", flush=True)

            result = analyze_video(model, gcs_uri, predicate)

            if result:
                save_result(cur, video_id, predicate, result)
                conn.commit()
                total_written += 1
                flag = "MATCH" if result.matched else "no"
                print(f"{flag} (confidence={result.confidence:.2f})")
            else:
                total_failed += 1
                print("FAILED")

            time.sleep(SLEEP_BETWEEN)

        print()

    cur.close()
    conn.close()

    print("=" * 50)
    print(f"完成! 写入: {total_written} 条  失败: {total_failed} 条")
    print("=" * 50)

    # 快速统计
    conn2 = psycopg2.connect(**DB_CONFIG)
    cur2 = conn2.cursor()
    cur2.execute("SELECT COUNT(*), AVG(confidence) FROM video_facts WHERE matched = TRUE;")
    row = cur2.fetchone()
    print(f"matched=TRUE: {row[0]} 条，平均置信度: {row[1]:.3f}" if row[0] else "暂无匹配记录")
    cur2.close()
    conn2.close()


if __name__ == "__main__":
    main()
