#!/usr/bin/env python3
"""
本地视频 → GCS → video_metadata，【一条命令】搞定。

为补充你自己的素材(如 skydiving / wingsuit)而生:上传到 gs://<bucket>/<prefix>/<id>.mp4
【并且】同步把 video_metadata 写好 —— 修掉 download_transcode_upload(只传)↔ backfill(只写库)
的脱节,以后补片只需对着新文件夹再跑一次。幂等:ON CONFLICT(video_id) DO NOTHING。

    # 预览(不执行)
    python -m ingestion.upload_local ./my_skydive_clips --dry-run
    # 上传 + 写库(默认前缀 videos/skydive/)
    python -m ingestion.upload_local ./my_skydive_clips
    # 大文件 / 4K → 先转 720p(+faststart,网页流畅播放),需本机 ffmpeg
    python -m ingestion.upload_local ./my_skydive_clips --transcode

环境变量(.env 已含):GCS_BUCKET · GCP_PROJECT · ALLOYDB_*(指向 Neon)。
video_id 取自文件名(净化为 [A-Za-z0-9_-]);所以文件名起成稳定、有意义的名字。
跳伞/翼装都归到同一个 collection(videos/skydive/)—— wingsuit 是子类型,靠
perception.skydive_extract 抽出的 is_wingsuit / jump_type 区分,不另开目录。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg2
from google.cloud import storage

from pipeline import config

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def _slug(name: str) -> str:
    """文件名(去扩展名)→ 稳定、安全的 video_id。"""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_")
    return s or "video"


def _ffprobe_duration(path: Path) -> float | None:
    """用 ffprobe 取时长(秒);没有 ffprobe 或失败 → None(duration_sec 留空,不影响播放)。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60)
        return round(float(out.stdout.strip()), 2)
    except Exception:
        return None


def _transcode_720p(src: Path, dst: Path) -> Path:
    """转码 720p H.264 + AAC + faststart(moov 前置,网页边下边播)。需本机 ffmpeg。"""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vf", "scale=-2:720",
         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
         "-loglevel", "error", str(dst)],
        check=True, timeout=1800)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description="本地视频 → GCS → video_metadata(一条命令)")
    ap.add_argument("folder", help="本地视频文件夹")
    ap.add_argument("--bucket", default=os.environ.get("GCS_BUCKET", config.GCS_BUCKET))
    ap.add_argument("--prefix", default="videos/skydive/", help="GCS 前缀(默认 videos/skydive/)")
    ap.add_argument("--transcode", action="store_true", help="先转码 720p(+faststart),推荐给大文件/4K")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划,不上传/不写库")
    args = ap.parse_args()

    prefix = args.prefix if args.prefix.endswith("/") else args.prefix + "/"
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        sys.exit(f"[!] 找不到文件夹: {folder}")
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not files:
        sys.exit(f"[!] {folder} 下没有视频文件({', '.join(sorted(VIDEO_EXTS))})")

    plan = [(p, _slug(p.name)) for p in files]
    dup = {v for _, v in plan if [v for _, v in plan].count(v) > 1}
    if dup:
        sys.exit(f"[!] 文件名净化后 video_id 撞车: {sorted(dup)} —— 改名后重试(id 必须唯一)")

    print(f"计划上传 {len(plan)} 个 → gs://{args.bucket}/{prefix}  (transcode={args.transcode})")
    for p, vid in plan:
        print(f"  {p.name:<44} → {vid}.mp4")
    if args.dry_run:
        print("[dry-run] 未执行。")
        return 0

    if not args.bucket or args.bucket == "your-gcs-bucket":
        sys.exit("[!] 请设 GCS_BUCKET 或传 --bucket")
    dsn = config.alloydb_dsn()
    if not dsn.get("password"):
        sys.exit("[!] 未设 ALLOYDB_PASSWORD(指向 Neon 的密码)")

    client = storage.Client(project=config.GCP_PROJECT)
    bucket = client.bucket(args.bucket)
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    cur = conn.cursor()

    done = inserted = 0
    for src, vid in plan:
        obj = f"{prefix}{vid}.mp4"
        uri = f"gs://{args.bucket}/{obj}"
        tmp: Path | None = None
        try:
            upload_path = src
            if args.transcode:
                tmp = Path(tempfile.gettempdir()) / f"vs_{vid}_720p.mp4"
                _transcode_720p(src, tmp)
                upload_path = tmp
            dur = _ffprobe_duration(upload_path)
            blob = bucket.blob(obj)
            blob.content_type = "video/mp4"
            blob.upload_from_filename(str(upload_path), timeout=1800)
            cur.execute(
                "INSERT INTO video_metadata(video_id, title, gcs_uri, duration_sec, source) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(video_id) DO NOTHING",
                (vid, vid, uri, dur, "seed"))
            inserted += cur.rowcount
            done += 1
            tag = "  +db" if cur.rowcount else "  (db 已存在)"
            print(f"[OK] {uri}  ({dur}s){tag}")
        except Exception as e:
            print(f"[FAIL] {src.name}: {e}")
        finally:
            if tmp and tmp.exists():
                tmp.unlink(missing_ok=True)

    cur.close()
    conn.close()
    print(f"\n[DONE] 上传 {done}/{len(plan)} 个,新写入 video_metadata {inserted} 行。")
    print("下一步(可选):python -m perception.skydive_extract   # 抽跳伞阶段 → skydive_segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
