"""
第2阶段 Step 1: 创建 video_facts 表
运行方式: python create_video_facts.py
"""
import os
import psycopg2

password = input("AlloyDB postgres 密码: ")

conn = psycopg2.connect(
    host=os.environ.get("ALLOYDB_HOST", "localhost"),
    port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    password=password,
    sslmode="require",
    connect_timeout=10,
)
conn.autocommit = True
cur = conn.cursor()

create_sql = """
CREATE TABLE IF NOT EXISTS video_facts (
    id              SERIAL PRIMARY KEY,
    video_id        VARCHAR REFERENCES video_metadata(video_id),
    predicate       VARCHAR NOT NULL,       -- 谓词名称, 如 "running", "jumping"
    matched         BOOLEAN NOT NULL,       -- Gemini 判断是否匹配
    confidence      FLOAT NOT NULL,         -- 置信度 0.0~1.0
    rationale       TEXT,                   -- Gemini 的推理说明
    start_ts        FLOAT,                  -- 匹配片段起始秒
    end_ts          FLOAT,                  -- 匹配片段结束秒
    created_at      TIMESTAMP DEFAULT NOW()
);
"""

cur.execute(create_sql)
print("[OK] video_facts 表创建成功")

# 验证
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name = 'video_facts'
    ORDER BY ordinal_position;
""")
print("\nvideo_facts 表结构:")
for row in cur.fetchall():
    print(f"  {row[0]:<15} {row[1]}")

cur.close()
conn.close()
