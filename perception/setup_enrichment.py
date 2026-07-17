"""V1.5 存量富化回填:对库内全部视频跑 转录+caption → 语义索引(设计 ingest-enrichment.md §4)。

    python -m perception.setup_enrichment --dry-run    # 只统计待办,不调模型
    python -m perception.setup_enrichment              # 断点续跑(跳过已有 cap:{vid} 的)
    PERCEPTION_MAX_VIDEOS=20 python -m perception.setup_enrichment   # 限量

幂等:content_key 唯一,重跑 upsert 不膨胀;失败视频跳过下次重补。收尾报告:
有话/无话分布、写入行数、实测 token/成本(usage 汇总)。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2                                                     # noqa: E402

from pipeline.agentops import usage                                          # noqa: E402
from pipeline.enrichment import already_enriched, enrich_video      # noqa: E402

DB_CONFIG = dict(
    host=os.environ.get("ALLOYDB_HOST", "localhost"), port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "postgres"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    sslmode="require", connect_timeout=10,
)
MAX_VIDEOS = int(os.environ.get("PERCEPTION_MAX_VIDEOS", "200"))


def main() -> None:
    ap = argparse.ArgumentParser(description="存量富化回填(幂等,断点续跑)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    password = os.environ.get("ALLOYDB_PASSWORD") or input("DB 密码: ")
    conn = psycopg2.connect(password=password, **DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT video_id, gcs_uri FROM video_metadata "
                "WHERE gcs_uri LIKE 'gs://%' ORDER BY video_id")
    videos = cur.fetchall()
    conn.close()

    todo = [(v, g) for v, g in videos if not already_enriched(v)][:MAX_VIDEOS]
    print(f"库内 {len(videos)} 个;待富化 {len(todo)} 个(其余已有 caption 键,跳过)")
    if args.dry_run:
        for v, _ in todo[:10]:
            print(f"  [dry] {v}")
        return

    usage.reset_usage()
    ok = failed = speech = 0
    rows = 0
    for i, (vid, gcs) in enumerate(todo, 1):
        try:
            stats = enrich_video(vid, gcs)
            ok += 1
            rows += stats.get("rows", 0)
            if stats.get("has_speech"):
                speech += 1
            print(f"[{i}/{len(todo)}] {vid}  speech={stats.get('has_speech')} "
                  f"segs={stats.get('segments', 0)} rows={stats.get('rows')}")
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(todo)}] {vid}  FAILED(下次重补): {str(e)[:80]}")
        time.sleep(0.5)

    s = usage.summarize()
    print("=" * 50)
    print(f"完成: 成功 {ok}(有话 {speech}) 失败 {failed};写入 {rows} 行")
    print(f"实测成本: {s['tokens_total']:,} tokens ≈ ${s['cost_usd']}")


if __name__ == "__main__":
    main()
