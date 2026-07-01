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
import sys
import time
import psycopg2
import vertexai
from vertexai.generative_models import GenerativeModel, Part
from pydantic import BaseModel, ValidationError

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.taxonomy import main_categories_for, normalize_category   # noqa: E402
from pipeline.taxonomy_seed import CATEGORIES                           # noqa: E402

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


class Extraction(BaseModel):
    """入库标准(ingest-category-standard)后的抽取信封:细活动照旧开放式,
    外加一个【从受控词表选】的 main_category(选不出/词表外 → 空,回退谓词映射推导)。"""
    main_category: str = ""
    activities: list[Activity] = []


# ── Gemini 调用(开放式细活动 + 受控主类,每视频 1 次)───────────────────────────
def _build_prompt() -> str:
    vocab = ", ".join(f'"{c}"' for c in CATEGORIES)
    return f"""You are a video analysis expert. Watch the video and do TWO things.

1. "main_category": pick exactly ONE label from this CONTROLLED list that best describes the video overall (do NOT invent new labels; if truly none fits, use ""):
[{vocab}]

2. "activities": list the distinct human activities/actions ACTUALLY present (the 3-8 most salient). For each provide:
- "activity": a short lowercase label, ActivityNet-style (e.g. "skiing", "cooking on grill", "playing basketball", "applying makeup", "walking dog")
- "confidence": 0.0 to 1.0
- "start_ts" / "end_ts": segment time in seconds
- "rationale": one sentence explaining the detection

Respond ONLY with a valid JSON object (no markdown, no extra text):
{{"main_category": "...", "activities": [{{"activity": "...", "confidence": 0.0, "start_ts": 0.0, "end_ts": 0.0, "rationale": "..."}}]}}"""


PROMPT = _build_prompt()


def analyze_video(model: GenerativeModel, gcs_uri: str) -> Extraction | None:
    """让 Gemini 列出视频里【实际存在】的活动 + 从受控词表选主类。直接读 GCS,无需下载。
    兼容旧输出:返回裸数组(无 main_category)也能解析(回退谓词映射推导主类)。"""
    video_part = Part.from_uri(uri=gcs_uri, mime_type="video/mp4")

    for attempt in range(RETRY_LIMIT + 1):
        try:
            response = model.generate_content(
                [video_part, PROMPT],
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
            if isinstance(data, list):                       # 旧格式:裸活动数组
                return Extraction(activities=[Activity(**a) for a in data])
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object or array")
            return Extraction.model_validate(data)

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


def save_category(cur, video_id: str, ext: Extraction) -> list[str]:
    """入库标准:每视频写大类行(predicate=受控大类)。优先用模型从词表选的
    main_category(过 normalize 校验,词表外一律不写);选不出 → 回退细谓词映射
    多数决(main_categories_for)。返回实际写入的大类(可能为空 → 调用方打日志)。"""
    cats = []
    picked = normalize_category(ext.main_category)
    if picked:
        cats = [picked]
        rationale = "category: selected from controlled vocab by extractor"
    else:
        cats = main_categories_for([a.activity.strip().lower() for a in ext.activities])
        rationale = "category: derived from predicates: " + \
            ", ".join(sorted(a.activity.strip().lower() for a in ext.activities)[:8])
    for c in cats:
        cur.execute(INSERT_SQL, (video_id, c, True, 1.0, rationale, None, None))
    return cats


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
        ext = analyze_video(model, gcs_uri)

        if ext is None:
            total_failed += 1
            print("  FAILED\n")
            continue

        def _write_all():
            n = 0
            for act in ext.activities:
                save_result(cur, video_id, act)
                n += 1
                print(f"  + {act.activity:<24} conf={act.confidence:.2f}  "
                      f"{act.start_ts:.0f}-{act.end_ts:.0f}s")
            cats = save_category(cur, video_id, ext)          # 入库标准:大类行(恰1,回退≤2)
            print(f"  ★ 大类: {', '.join(cats) if cats else '(推不出,待补)'}")
            return n

        try:
            total_written += _write_all()
            conn.commit()
        except psycopg2.OperationalError as e:
            print(f"  [DB 掉线,重连重写] {str(e).strip()[:50]}")
            try: conn.close()
            except Exception: pass
            conn = psycopg2.connect(**DB_CONFIG); conn.autocommit = False; cur = conn.cursor()
            try:
                total_written += _write_all()
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
