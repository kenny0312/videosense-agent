#!/usr/bin/env python3
"""
ActivityNet 前 50 个视频 → 720p MP4 → GCS 并发流水线
依赖: pip install yt-dlp google-cloud-storage tqdm
系统依赖: ffmpeg
"""

import argparse
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── 可选依赖 ──────────────────────────────────────────────────────────────────
try:
    import yt_dlp
except ImportError:
    sys.exit("❌ 请先安装: pip install yt-dlp")

try:
    from google.cloud import storage as gcs
except ImportError:
    sys.exit("❌ 请先安装: pip install google-cloud-storage")

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # 降级：不显示进度条

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("activitynet")

# ── ActivityNet 官方视频列表 URL ───────────────────────────────────────────────
ACTIVITYNET_JSON_URL = (
    "http://ec2-52-25-205-214.us-west-2.compute.amazonaws.com/files/"
    "activity_net.v1-3.min.json"
)

# ── 数据结构 ──────────────────────────────────────────────────────────────────
@dataclass
class VideoTask:
    video_id: str          # ActivityNet vid id, e.g. "v_----9CpojUE"
    youtube_url: str       # https://www.youtube.com/watch?v=...
    gcs_object: str        # 目标 GCS 对象路径
    status: str = "pending"
    error: Optional[str] = None
    duration_s: float = 0.0


@dataclass
class PipelineConfig:
    bucket_name: str
    gcs_prefix: str = "activitynet/720p/"
    max_videos: int = 50
    download_workers: int = 4   # 并发下载数
    transcode_workers: int = 2  # 并发转码数（CPU 密集）
    upload_workers: int = 4     # 并发上传数
    tmp_dir: Optional[str] = None
    service_account_json: Optional[str] = None  # None → ADC 自动认证
    video_list_json: Optional[str] = None       # None → 从官方 URL 下载
    ffmpeg_crf: int = 23        # 转码质量 (18=高质量, 28=低质量)
    ffmpeg_preset: str = "fast"


# ── Step 0: 获取视频列表 ───────────────────────────────────────────────────────
def fetch_video_list(config: PipelineConfig) -> list[VideoTask]:
    """从 ActivityNet JSON 或本地文件读取前 N 个视频 ID → YouTube URL。"""
    if config.video_list_json and Path(config.video_list_json).exists():
        log.info("从本地文件读取视频列表: %s", config.video_list_json)
        with open(config.video_list_json) as f:
            data = json.load(f)
    else:
        log.info("从官方 URL 下载视频列表…")
        with urllib.request.urlopen(ACTIVITYNET_JSON_URL, timeout=30) as resp:
            data = json.load(resp)

    # ActivityNet JSON 结构: {"database": {"v_xxxxx": {"url": "https://..."}}}
    database = data.get("database", {})
    tasks = []
    for vid_id, meta in database.items():
        url = meta.get("url", "")
        if "youtube.com" in url or "youtu.be" in url:
            gcs_obj = f"{config.gcs_prefix}{vid_id}.mp4"
            tasks.append(VideoTask(video_id=vid_id, youtube_url=url, gcs_object=gcs_obj))
        if len(tasks) >= config.max_videos:
            break

    log.info("共选取 %d 个视频任务", len(tasks))
    return tasks


# ── Step 1: 下载 ──────────────────────────────────────────────────────────────
def download_video(task: VideoTask, tmp_dir: str) -> Optional[Path]:
    """用 yt-dlp 下载原始视频到临时目录，返回文件路径。"""
    out_tmpl = str(Path(tmp_dir) / f"{task.video_id}.%(ext)s")
    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 60,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(task.youtube_url, download=True)
            # 找到实际下载的文件
            ext = info.get("ext", "mp4")
            dl_path = Path(tmp_dir) / f"{task.video_id}.{ext}"
            # yt-dlp 合并后可能是 .mkv/.webm，glob 兜底
            if not dl_path.exists():
                candidates = list(Path(tmp_dir).glob(f"{task.video_id}.*"))
                if not candidates:
                    raise FileNotFoundError(f"下载后找不到文件: {task.video_id}")
                dl_path = candidates[0]
            task.duration_s = info.get("duration", 0)
            log.info("✅ 下载完成: %s (%.0fs)", task.video_id, task.duration_s)
            return dl_path
    except Exception as e:
        task.status = "failed"
        task.error = f"下载失败: {e}"
        log.warning("❌ %s 下载失败: %s", task.video_id, e)
        return None


# ── Step 2: 转码 ──────────────────────────────────────────────────────────────
def transcode_to_720p(src: Path, tmp_dir: str, task: VideoTask, config: PipelineConfig) -> Optional[Path]:
    """用 ffmpeg 将视频转码为 720p H.264 MP4。"""
    dst = Path(tmp_dir) / f"{task.video_id}_720p.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", "scale=-2:720",          # 保持宽高比，高度 720
        "-c:v", "libx264",
        "-crf", str(config.ffmpeg_crf),
        "-preset", config.ffmpeg_preset,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",      # 便于流媒体播放
        "-loglevel", "error",
        str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        log.info("✅ 转码完成: %s → %s (%.1f MB)",
                 task.video_id, dst.name, dst.stat().st_size / 1e6)
        return dst
    except Exception as e:
        task.status = "failed"
        task.error = f"转码失败: {e}"
        log.warning("❌ %s 转码失败: %s", task.video_id, e)
        return None
    finally:
        src.unlink(missing_ok=True)  # 立即删除原始文件，节省磁盘


# ── Step 3: 上传 ──────────────────────────────────────────────────────────────
def upload_to_gcs(src: Path, task: VideoTask, client: gcs.Client, config: PipelineConfig) -> bool:
    """将 MP4 上传到 GCS，完成后删除本地文件。"""
    try:
        bucket = client.bucket(config.bucket_name)
        blob = bucket.blob(task.gcs_object)
        blob.content_type = "video/mp4"
        blob.upload_from_filename(str(src), timeout=600)
        gcs_uri = f"gs://{config.bucket_name}/{task.gcs_object}"
        log.info("✅ 上传完成: %s", gcs_uri)
        task.status = "done"
        return True
    except Exception as e:
        task.status = "failed"
        task.error = f"上传失败: {e}"
        log.warning("❌ %s 上传失败: %s", task.video_id, e)
        return False
    finally:
        src.unlink(missing_ok=True)


# ── 流水线核心 ────────────────────────────────────────────────────────────────
def run_pipeline(tasks: list[VideoTask], config: PipelineConfig, tmp_dir: str):
    """
    三阶段并发流水线:
      ThreadPoolExecutor(下载) → ThreadPoolExecutor(转码) → ThreadPoolExecutor(上传)
    通过 Future 链式传递，无需等待全阶段完成即可启动下一阶段。
    """
    # 初始化 GCS 客户端
    if config.service_account_json:
        client = gcs.Client.from_service_account_json(config.service_account_json)
    else:
        client = gcs.Client()  # 使用 ADC / GOOGLE_APPLICATION_CREDENTIALS

    lock = threading.Lock()
    stats = {"done": 0, "failed": 0}

    def process_one(task: VideoTask):
        t0 = time.time()
        # 1. 下载
        raw = download_video(task, tmp_dir)
        if raw is None:
            with lock:
                stats["failed"] += 1
            return

        # 2. 转码
        mp4 = transcode_to_720p(raw, tmp_dir, task, config)
        if mp4 is None:
            with lock:
                stats["failed"] += 1
            return

        # 3. 上传
        ok = upload_to_gcs(mp4, task, client, config)
        with lock:
            if ok:
                stats["done"] += 1
                elapsed = time.time() - t0
                log.info("🎬 [%d/%d] %s 完成，耗时 %.0fs",
                         stats["done"] + stats["failed"], len(tasks),
                         task.video_id, elapsed)
            else:
                stats["failed"] += 1

    # 并发执行（下载+转码+上传串行，但多个视频并发）
    max_workers = max(config.download_workers, config.upload_workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                               thread_name_prefix="pipeline") as pool:
        futures = {pool.submit(process_one, t): t for t in tasks}
        iterator = concurrent.futures.as_completed(futures)
        if tqdm:
            iterator = tqdm(iterator, total=len(tasks), desc="总进度", unit="video")
        for _ in iterator:
            pass  # 异常已在 process_one 内处理

    return stats


# ── CLI 入口 ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="ActivityNet 前 N 个视频 → 720p MP4 → GCS"
    )
    parser.add_argument("--bucket", required=True, help="GCS Bucket 名称")
    parser.add_argument("--prefix", default="activitynet/720p/",
                        help="GCS 对象前缀 (默认: activitynet/720p/)")
    parser.add_argument("--max-videos", type=int, default=50,
                        help="处理视频数量 (默认: 50)")
    parser.add_argument("--workers", type=int, default=4,
                        help="并发视频数 (默认: 4)")
    parser.add_argument("--tmp-dir", default=None,
                        help="临时目录 (默认: 系统 tmp)")
    parser.add_argument("--service-account", default=None,
                        help="Service Account JSON 路径 (默认: 使用 ADC)")
    parser.add_argument("--video-list", default=None,
                        help="本地 ActivityNet JSON 路径 (默认: 从官网下载)")
    parser.add_argument("--crf", type=int, default=23,
                        help="ffmpeg CRF 质量 18-28 (默认: 23)")
    parser.add_argument("--preset", default="fast",
                        choices=["ultrafast","superfast","veryfast","faster",
                                 "fast","medium","slow","slower","veryslow"],
                        help="ffmpeg preset (默认: fast)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印任务列表，不实际执行")
    args = parser.parse_args()

    # 检查 ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("❌ 未找到 ffmpeg，请先安装: sudo apt install ffmpeg")

    config = PipelineConfig(
        bucket_name=args.bucket,
        gcs_prefix=args.prefix,
        max_videos=args.max_videos,
        download_workers=args.workers,
        upload_workers=args.workers,
        service_account_json=args.service_account,
        video_list_json=args.video_list,
        ffmpeg_crf=args.crf,
        ffmpeg_preset=args.preset,
    )

    # 获取任务列表
    tasks = fetch_video_list(config)
    if not tasks:
        sys.exit("❌ 未获取到任何视频任务")

    if args.dry_run:
        print(f"\n{'ID':<20} {'YouTube URL':<50} {'GCS 对象'}")
        print("-" * 100)
        for t in tasks:
            print(f"{t.video_id:<20} {t.youtube_url:<50} gs://{config.bucket_name}/{t.gcs_object}")
        print(f"\n共 {len(tasks)} 个任务 (dry-run，未执行)")
        return

    # 创建临时目录
    use_tmp = args.tmp_dir or tempfile.mkdtemp(prefix="activitynet_")
    Path(use_tmp).mkdir(parents=True, exist_ok=True)
    log.info("临时目录: %s", use_tmp)
    log.info("目标 Bucket: gs://%s/%s", config.bucket_name, config.gcs_prefix)
    log.info("并发数: %d，视频数: %d", args.workers, len(tasks))

    t_start = time.time()
    stats = run_pipeline(tasks, config, use_tmp)
    elapsed = time.time() - t_start

    # 汇总
    print("\n" + "=" * 60)
    print(f"  ✅ 成功: {stats['done']}")
    print(f"  ❌ 失败: {stats['failed']}")
    print(f"  ⏱  总耗时: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print("=" * 60)

    # 打印失败详情
    failed = [t for t in tasks if t.status == "failed"]
    if failed:
        print("\n失败列表:")
        for t in failed:
            print(f"  {t.video_id}: {t.error}")


if __name__ == "__main__":
    main()