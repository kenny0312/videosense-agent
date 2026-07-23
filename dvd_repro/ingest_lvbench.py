"""选片关:把 3 条 LVBench 视频灌进 VS 世界(隔离契约:打标可撤销)。

每条视频:本地 mp4 → (必要时转码 720p/h264) → 传 gs://<bucket>/lvbench/ →
video_metadata 插行(source='lvbench-dvd', ON CONFLICT 跳过) → enrichment(基线B要用)。
全程 UsageMeter+BudgetGuard 记账;幂等,可重复运行。

用法: python -m dvd_repro.ingest_lvbench
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dvd_repro import config as C
from dvd_repro.costguard import BudgetGuard, UsageMeter
from ingestion.upload_local import _ffprobe_duration, _transcode_720p  # 复用,不改写
from pipeline import config as vs_config
from pathlib import Path

VIDEOS = {  # ytid: 类型(标题下载时现取)
    "2sriHX3PbXw": "cartoon",
    "k2FIFQIYBvA": "tv",
    "rp4NKWb7dXk": "sport",
}


def _yt_title(ytid: str) -> str:
    try:
        out = subprocess.run([sys.executable, "-m", "yt_dlp", "--skip-download",
                              "--print", "title", f"https://www.youtube.com/watch?v={ytid}"],
                             capture_output=True, text=True, timeout=60, encoding="utf-8")
        t = (out.stdout or "").strip().splitlines()
        if t:
            return t[-1][:200]
    except Exception:
        pass
    return f"LVBench {ytid}"


def _codec(path: str) -> str:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                          "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                         capture_output=True, text=True)
    return (out.stdout or "").strip()


def _upload(local: str, blob_name: str) -> str:
    from google.cloud import storage
    client = storage.Client(project=vs_config.GCP_PROJECT)
    bucket = client.bucket(vs_config.GCS_BUCKET)
    blob = bucket.blob(blob_name)
    if not blob.exists():
        blob.upload_from_filename(local, timeout=600)
        print(f"    ↑ 已上传 gs://{vs_config.GCS_BUCKET}/{blob_name}")
    else:
        print(f"    = GCS 已存在,跳过上传")
    return f"gs://{vs_config.GCS_BUCKET}/{blob_name}"


def _db():
    import psycopg2
    return psycopg2.connect(host=vs_config.ALLOYDB_HOST, dbname=vs_config.ALLOYDB_DB,
                            user=vs_config.ALLOYDB_USER, password=vs_config.ALLOYDB_PASSWORD,
                            sslmode="require")


def _insert_row(vid: str, title: str, gcs_uri: str, dur: float) -> None:
    """即用即连(Neon 会掐长闲连接 —— 三跑事故:转码10分钟后 SSL closed)。"""
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO video_metadata(video_id, title, gcs_uri, duration_sec, source, ingested_at) "
            "VALUES (%s,%s,%s,%s,%s,NOW()) ON CONFLICT (video_id) DO NOTHING",
            (vid, title, gcs_uri, dur, C.INGEST_SOURCE_TAG))
        conn.commit()
    finally:
        conn.close()


CHUNK_THRESHOLD_S = 12 * 60      # 超过 12 分钟走分块富化(单次调用必撞 8192 截断)


def main() -> int:
    guard = BudgetGuard(run_id="ingest_lvbench")
    meter = UsageMeter()
    ok = 0
    for ytid, vtype in VIDEOS.items():
        vid = f"lvb_{ytid}"
        local = os.path.join(C.VIDEOS_DIR, f"{vid}.mp4")
        print(f"── {vid}({vtype})──")
        if not os.path.exists(local):
            print("    ✗ 本地文件不存在,先跑下载。跳过。")
            continue
        dur = _ffprobe_duration(Path(local)) or 0.0
        codec = _codec(local)
        print(f"    时长 {dur/60:.1f} min · 编码 {codec}")
        if codec != "h264":                                   # yt-dlp 可能给 vp9/av1
            h264 = Path(local).with_suffix(".h264.mp4")
            if h264.exists():
                print("    = 已有转码产物,复用")
            else:
                print("    转码 720p/h264 …")
                _transcode_720p(Path(local), h264)
            local = str(h264)
        gcs_uri = _upload(local, f"{C.GCS_PREFIX}{vid}.mp4")
        title = _yt_title(ytid)
        _insert_row(vid, title, gcs_uri, dur)
        print(f"    DB 行就绪(source={C.INGEST_SOURCE_TAG})· {title[:50]}")

        from dvd_repro.enrich_chunked import enrich_video_chunked
        from pipeline.enrichment import already_enriched, enrich_video
        if already_enriched(vid):
            print("    = 已富化,跳过")
            ok += 1
            continue
        print("    enrichment(基线B索引)…")
        try:
            if dur > CHUNK_THRESHOLD_S:                   # 长视频:分块(单次必截断)
                stats = enrich_video_chunked(vid, gcs_uri, dur, guard, meter)
            else:
                stats = enrich_video(vid, gcs_uri)
                guard.charge(meter.delta(), note=f"enrich {vid}")
            print(f"    ✓ 富化 {stats} · 本场累计 ${guard.spent_run():.2f}")
            ok += 1
        except Exception as e:
            cost = meter.delta()
            if cost:
                guard.charge(cost, note=f"enrich {vid} failed")
            print(f"    ⚠ 富化失败({type(e).__name__}: {str(e)[:120]}),跳过该条(验收时汇报)")
    print(f"\n完成 {ok}/{len(VIDEOS)} · 本场总花费 ${guard.spent_run():.2f} · 项目累计 ${guard.spent_total():.2f}")
    return 0 if ok == len(VIDEOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
