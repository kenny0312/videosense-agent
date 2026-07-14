#!/usr/bin/env python3
"""
回填 video_metadata —— ingestion 把视频传到了 GCS,但没把元数据写进库(那一环缺失)。
这一步扫 GCS 桶里的 .mp4,把 (video_id, gcs_uri) 灌进 video_metadata,供 perception 读取。

用法(指向 Neon):
    $env:GCS_BUCKET       = "你的桶名"
    $env:GCP_PROJECT      = "你的 gcp 项目"
    $env:ALLOYDB_HOST     = "ep-xxxx-pooler.<region>.aws.neon.tech"
    $env:ALLOYDB_DB       = "neondb"
    $env:ALLOYDB_USER     = "<role>"
    $env:ALLOYDB_PASSWORD = "<password>"
    python -m ingestion.backfill_metadata           # 或 --bucket / --prefix 覆盖

幂等:ON CONFLICT(video_id) DO NOTHING —— 可重复跑,只补新视频。
video_id 取自 GCS 文件名(activitynet/720p/<video_id>.mp4);title 暂填 video_id,
duration_sec 留空(perception 不需要;以后想要可用 ffprobe 回填)。
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg2
from google.cloud import storage

from pipeline import config


def list_videos(bucket: str, prefix: str) -> list[tuple[str, str]]:
    client = storage.Client(project=config.GCP_PROJECT)
    out: list[tuple[str, str]] = []
    for blob in client.list_blobs(bucket, prefix=prefix):
        if blob.name.lower().endswith(".mp4"):
            vid = os.path.splitext(os.path.basename(blob.name))[0]
            out.append((vid, f"gs://{bucket}/{blob.name}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="扫 GCS 桶回填 video_metadata")
    ap.add_argument("--bucket", default=os.environ.get("GCS_BUCKET", config.GCS_BUCKET))
    ap.add_argument("--prefix", default="activitynet/720p/")
    args = ap.parse_args()

    if not args.bucket or args.bucket == "your-gcs-bucket":
        sys.exit("[!] 请设 GCS_BUCKET 或传 --bucket")

    rows = list_videos(args.bucket, args.prefix)
    if not rows:
        sys.exit(f"[!] gs://{args.bucket}/{args.prefix} 下没找到 .mp4")

    dsn = config.alloydb_dsn()
    if not dsn.get("password"):
        sys.exit("[!] 未设 ALLOYDB_PASSWORD(指向 Neon 的密码)")

    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    cur = conn.cursor()
    inserted = 0
    for vid, uri in rows:
        cur.execute(
            "INSERT INTO video_metadata(video_id, title, gcs_uri, source) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(video_id) DO NOTHING",
            (vid, vid, uri,
             "pexels" if vid.startswith("v_px") else "activitynet" if vid.startswith("v_") else "seed"))
        inserted += cur.rowcount
    cur.close()
    conn.close()
    print(f"[OK] 扫到 {len(rows)} 个视频,新插入 {inserted} 行 video_metadata "
          f"(gs://{args.bucket}/{args.prefix})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
