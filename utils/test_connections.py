"""
连接测试脚本：GCS + AlloyDB
运行方式: python test_connections.py
"""
import os
import sys

# ── GCS 测试 ──────────────────────────────────────────────────────────────────
print("=" * 50)
print("测试 1: GCS 连接")
print("=" * 50)
try:
    from google.cloud import storage
    client = storage.Client(project=os.environ.get("GCP_PROJECT", "your-gcp-project-id"))
    blobs = list(client.list_blobs(os.environ.get("GCS_BUCKET", "your-gcs-bucket"), max_results=3))
    print(f"[OK] GCS 连接成功，bucket 内文件示例:")
    for b in blobs:
        print(f"     {b.name}  ({round(b.size/1024/1024, 1)} MB)")
except Exception as e:
    print(f"[FAIL] GCS 连接失败: {e}")

# ── AlloyDB 测试 ───────────────────────────────────────────────────────────────
print()
print("=" * 50)
print("测试 2: AlloyDB 连接")
print("=" * 50)

password = input("请输入 AlloyDB postgres 密码: ")

try:
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("ALLOYDB_HOST", "localhost"),
        port=5432,
        dbname=os.environ.get("ALLOYDB_DB", "your_database"),
        user=os.environ.get("ALLOYDB_USER", "postgres"),
        password=password,
        sslmode="require",
        connect_timeout=10,
    )
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM video_metadata;")
    vm_count = cur.fetchone()[0]
    print(f"[OK] AlloyDB 连接成功!")
    print(f"     video_metadata : {vm_count} 条记录")

    cur.execute("SELECT COUNT(*) FROM video_facts;")
    vf_count = cur.fetchone()[0]
    print(f"     video_facts    : {vf_count} 条记录")

    # 显示 video_metadata 前 3 条
    cur.execute("SELECT video_id, gcs_uri, duration_sec FROM video_metadata LIMIT 3;")
    rows = cur.fetchall()
    if rows:
        print(f"\n     video_metadata 前 3 条:")
        for r in rows:
            print(f"     {r[0]} | {r[1]} | {r[2]}s")

    cur.close()
    conn.close()

except Exception as e:
    print(f"[FAIL] AlloyDB 连接失败: {e}")

print()
print("=" * 50)
print("测试完成")
print("=" * 50)
