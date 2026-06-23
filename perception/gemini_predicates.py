"""
第2阶段：多模态感知（开放式活动抽取）
- 从 video_metadata 读取 GCS URI
- 每个视频【1 次】调用 Gemini:开放式列出视频里实际存在的活动(带置信度/时间段)
- 每个活动写一条 video_facts(matched=true)

(相比旧的"固定 5 谓词逐个测":调用数 5×↓、更快、且真有匹配数据。)

运行方式:
    $env:ALLOYDB_*  指向 Neon;$env:GCP_PROJECT 设好;ADC 已登录
    $env:PERCEPTION_MAX_VIDEOS=50    # 可选,默认 50
    python -m perception.gemini_predicates
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
# gemini-1.5-* 已退役;默认用当代多模态模型,可用 PERCEPTION_MODEL 覆盖
MODEL_NAME  = os.environ.get("PERCEPTION_MODEL", "gemini-2.5-flash")

DB_CONFIG = dict(
    host=os.environ.get("ALLOYDB_HOST", "localhost"),
    port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    sslmode="require",
    connect_timeout=10,
    # keepalive:Gemini 调用慢,防止 Neon 在长空闲间隙关掉连接
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
)

MAX_VIDEOS    = int(os.environ.get("PERCEPTION_MAX_VIDEOS", "50"))  # 默认 50;可用 env 覆盖
RETRY_LIMIT   = 2     # Gemini 调用失败重试次数
SLEEP_BETWEEN = 1.0   # 每个视频之间间隔(秒),避免触发限速


# ── Pydantic Schema:一条检测到的活动 ──────────────────────────────────────────
class Activity(BaseModel):
    activity:   str
    confidence: float = 0.0   # 0.0 ~ 1.0
    rationale:  str   = ""
    start_ts:   float = 0.0   # 片段起始秒
    end_ts:     float = 0.0   # 片段结束秒


# ── Gemini 调用(开放式,每视频 1 次)──────────────────────────────────────────
def analyze_video(model: GenerativeModel, gcs_uri: str) -> list[Activity] | None:
    """让 Gemini 列出视频里【实际存在】的活动。Gemini 直接读 GCS,无需下载。"""
    video_part = Part.from_uri(uri=gcs_uri, mime_type="video/mp4")

    prompt = """You are a video analysis expert. Watch the video and list the distinct human activities/actions that are ACTUALLY present.

For each activity provide:
- "activity": a short lowercase label, ActivityNet-style (e.g. "skiing", "cooking on grill", "playing basketball", "applying makeup", "walking dog")
- "confidence": 0.0 to 1.0
- "start_ts": start time in seconds
- "end_ts": end time in seconds
- "rationale": one sentence explaining the detection

List only activities truly present (the 3-8 most salient). Respond ONLY with a valid JSON array (no markdown, no extra text):
[
  {"activity": "...", "confidence": 0.0, "start_ts": 0.0, "end_ts": 0.0, "rationale": "..."}
]"""

    for attempt in range(RETRY_LIMIT + 1):
        try:
            response = model.generate_content(
                [video_part, prompt],
                generation_config={"temperature": 0.1, "max_output_tokens": 4096,
                                   "response_mime_type": "application/json"},
            )
            raw = response.text.strip()

            # 清理可能的 markdown code block
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("expected a JSON array")
            return [Activity(**a) for a in data]

        except (json.JSONDecodeError, ValidationError, KeyError, ValueError) as e:
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


def save_result(cur, video_id: str, act: Activity):
    cur.execute(INSERT_SQL, (
        video_id,
        act.activity.strip().lower(),
        True,                       # 开放式只返回"存在"的活动 → matched=true
        act.confidence,
        act.rationale,
        act.start_ts,
        act.end_ts,
    ))


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    # 优先读 env(headless / Neon),没有再交互输入
    password = os.environ.get("ALLOYDB_PASSWORD") or input("DB 密码 (Neon/AlloyDB): ")
    DB_CONFIG["password"] = password

    print(f"\n初始化 Vertex AI (project={PROJECT_ID}, location={LOCATION})...")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = GenerativeModel(MODEL_NAME)
    print(f"模型加载完成: {MODEL_NAME}")

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    # 读取待处理视频（跳过已有 facts 的视频 → 可断点续跑）
    cur.execute(f"""
        SELECT vm.video_id, vm.gcs_uri
        FROM video_metadata vm
        WHERE vm.video_id NOT IN (SELECT DISTINCT video_id FROM video_facts)
        ORDER BY vm.video_id
        LIMIT {MAX_VIDEOS};
    """)
    videos = cur.fetchall()
    print(f"\n待处理视频: {len(videos)} 个\n")

    total_written = 0
    total_failed  = 0

    for i, (video_id, gcs_uri) in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {video_id}")
        activities = analyze_video(model, gcs_uri)

        if activities is None:
            total_failed += 1
            print("  FAILED\n")
            continue

        try:
            for act in activities:
                save_result(cur, video_id, act)
                total_written += 1
                print(f"  + {act.activity:<24} conf={act.confidence:.2f}  "
                      f"{act.start_ts:.0f}-{act.end_ts:.0f}s")
            conn.commit()
        except psycopg2.OperationalError as e:
            print(f"  [DB 掉线,重连重写] {str(e).strip()[:50]}")
            try: conn.close()
            except Exception: pass
            conn = psycopg2.connect(**DB_CONFIG); conn.autocommit = False; cur = conn.cursor()
            try:
                for act in activities:
                    save_result(cur, video_id, act)
                conn.commit()
            except Exception as e2:
                print(f"  [重写仍失败] {e2}"); total_failed += 1
        print()
        time.sleep(SLEEP_BETWEEN)

    cur.close()
    conn.close()

    print("=" * 50)
    print(f"完成! 写入活动: {total_written} 条  失败视频: {total_failed} 个")
    print("=" * 50)

    # 快速统计
    conn2 = psycopg2.connect(**DB_CONFIG)
    cur2 = conn2.cursor()
    cur2.execute("SELECT COUNT(*), COUNT(DISTINCT video_id), AVG(confidence) FROM video_facts")
    n, nv, avg = cur2.fetchone()
    print(f"video_facts: {n} 条 / {nv} 个视频" + (f", 平均置信度 {avg:.3f}" if avg else ""))
    cur2.close()
    conn2.close()


if __name__ == "__main__":
    main()
