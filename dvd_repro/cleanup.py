"""隔离契约的撤销器:一条命令抹掉 DVD 复现在 VS 世界留下的一切痕迹。

删什么(只删打了标签/前缀的,绝不碰存量):
    1) Neon 库:video_metadata.source = 'lvbench-dvd' 的视频,及其在
       video_discovery / video_facts / content_embeddings 里的同 video_id 行
    2) GCS:gs://<bucket>/lvbench/ 前缀下的对象
    3) 本地:dvd_repro/{db,videos,logs,results}/ 产物

用法:
    python -m dvd_repro.cleanup              # 干跑(默认):只打印将删清单,不动任何东西
    python -m dvd_repro.cleanup --execute    # 真删(会再要求键入 DELETE 确认)
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dvd_repro import config as C
from pipeline import config as vs_config


def _db_conn():
    import psycopg2
    return psycopg2.connect(host=vs_config.ALLOYDB_HOST, dbname=vs_config.ALLOYDB_DB,
                            user=vs_config.ALLOYDB_USER, password=vs_config.ALLOYDB_PASSWORD,
                            sslmode="require")


def plan_db(conn) -> dict:
    """盘点将删的行数(按标签圈定,不执行删除)。"""
    cur = conn.cursor()
    cur.execute("SELECT video_id FROM video_metadata WHERE source = %s", (C.INGEST_SOURCE_TAG,))
    vids = [r[0] for r in cur.fetchall()]
    counts = {"video_metadata": len(vids)}
    if vids:
        for table in ("video_discovery", "video_facts", "content_embeddings"):
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE video_id = ANY(%s)", (vids,))
            counts[table] = cur.fetchone()[0]
    else:
        counts.update({"video_discovery": 0, "video_facts": 0, "content_embeddings": 0})
    return {"video_ids": vids, "counts": counts}


def delete_db(conn, vids: list) -> None:
    cur = conn.cursor()
    for table in ("content_embeddings", "video_facts", "video_discovery", "video_metadata"):
        cur.execute(f"DELETE FROM {table} WHERE video_id = ANY(%s)", (vids,))
    conn.commit()


def plan_gcs() -> list:
    from google.cloud import storage
    client = storage.Client(project=vs_config.GCP_PROJECT)
    bucket = client.bucket(vs_config.GCS_BUCKET)
    return [b.name for b in bucket.list_blobs(prefix=C.GCS_PREFIX)]


def delete_gcs(names: list) -> None:
    from google.cloud import storage
    client = storage.Client(project=vs_config.GCP_PROJECT)
    bucket = client.bucket(vs_config.GCS_BUCKET)
    for n in names:
        bucket.blob(n).delete()


def plan_local() -> list:
    out = []
    for d in (C.DB_DIR, C.VIDEOS_DIR, C.LOGS_DIR, C.RESULTS_DIR):
        if os.path.isdir(d) and os.listdir(d):
            out.append(f"{d}({len(os.listdir(d))} 项)")
    return out


def delete_local() -> None:
    for d in (C.DB_DIR, C.VIDEOS_DIR, C.LOGS_DIR, C.RESULTS_DIR):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)


def main(argv=None) -> int:
    execute = "--execute" in (argv or sys.argv[1:])
    print(f"=== DVD 复现痕迹盘点(标签 source='{C.INGEST_SOURCE_TAG}',"
          f"前缀 gs://{vs_config.GCS_BUCKET}/{C.GCS_PREFIX})===")
    conn = _db_conn()
    try:
        db = plan_db(conn)
        print(f"[DB] 打标视频: {db['video_ids'] or '(无)'}")
        for t, n in db["counts"].items():
            print(f"     {t:<20} {n} 行")
        gcs = plan_gcs()
        print(f"[GCS] {len(gcs)} 个对象" + (f": {gcs[:5]}{'...' if len(gcs) > 5 else ''}" if gcs else ""))
        loc = plan_local()
        print(f"[本地] {loc or '(空)'}")

        if not execute:
            print("\n(干跑模式,没有删除任何东西。真删加 --execute)")
            return 0
        confirm = input("键入 DELETE 确认真删: ").strip()
        if confirm != "DELETE":
            print("未确认,退出。")
            return 1
        if db["video_ids"]:
            delete_db(conn, db["video_ids"])
        if gcs:
            delete_gcs(gcs)
        delete_local()
        print("已全部撤销。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
