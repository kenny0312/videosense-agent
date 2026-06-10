import os
import psycopg2

password = input("AlloyDB postgres 密码: ")

conn = psycopg2.connect(
    host=os.environ.get("ALLOYDB_HOST", "localhost"), port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"), password=password, sslmode="require",
)
cur = conn.cursor()

# 列出所有表
cur.execute("""
    SELECT table_name, pg_size_pretty(pg_total_relation_size(quote_ident(table_name)))
    FROM information_schema.tables
    WHERE table_schema = 'public'
    ORDER BY table_name;
""")
print("=== your_database 数据库所有表 ===")
for row in cur.fetchall():
    print(f"  {row[0]:<30} {row[1]}")

# 每张表的行数
cur.execute("""
    SELECT schemaname, tablename
    FROM pg_tables WHERE schemaname='public';
""")
tables = [r[1] for r in cur.fetchall()]
print("\n=== 各表记录数 ===")
for t in tables:
    cur.execute(f"SELECT COUNT(*) FROM {t};")
    print(f"  {t:<30} {cur.fetchone()[0]} 行")

cur.close()
conn.close()
