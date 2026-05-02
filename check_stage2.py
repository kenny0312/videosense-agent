import psycopg2

password = input("AlloyDB postgres 密码: ")

conn = psycopg2.connect(
    host="your-db-host", port=5432, dbname="your_database",
    user="postgres", password=password, sslmode="require",
)
cur = conn.cursor()

# 总记录数
cur.execute("SELECT COUNT(*) FROM video_facts;")
print(f"video_facts 总记录数: {cur.fetchone()[0]}")

# matched 分布
cur.execute("SELECT matched, COUNT(*), ROUND(AVG(confidence)::numeric, 3) FROM video_facts GROUP BY matched;")
print("\nmatched 分布:")
for row in cur.fetchall():
    print(f"  matched={row[0]}  count={row[1]}  avg_confidence={row[2]}")

# 覆盖了多少视频
cur.execute("SELECT COUNT(DISTINCT video_id) FROM video_facts;")
print(f"\n已分析视频数: {cur.fetchone()[0]}")

# 用了哪些 predicate
cur.execute("SELECT DISTINCT predicate FROM video_facts ORDER BY predicate;")
predicates = cur.fetchall()
print(f"\n谓词列表 ({len(predicates)} 个):")
for p in predicates:
    print(f"  - {p[0]}")

# 表结构
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_name='video_facts' ORDER BY ordinal_position;
""")
print("\nvideo_facts 表结构:")
for row in cur.fetchall():
    print(f"  {row[0]:<20} {row[1]}")

# 样例数据
cur.execute("SELECT video_id, predicate, matched, confidence, rationale FROM video_facts LIMIT 3;")
print("\n样例数据 (前3条):")
for row in cur.fetchall():
    print(f"  [{row[2]}] {row[0]} | {row[1]} | conf={row[3]:.2f}")
    print(f"    {row[4][:80]}")

cur.close()
conn.close()
