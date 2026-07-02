"""V1 语义索引:建表 + 三源回填(幂等;设计 semantic-retrieval.md §8 S2/S4)。

    python -m perception.setup_semantic --dry-run   # 只统计,不写库
    python -m perception.setup_semantic             # 建表 + 回填 + 报告

三源:fact(video_facts 细谓词行)、skydive(summary)、analyze(Redis L2 缓存里的结果;
ANALYZE_CACHE_BACKEND=memory 时 L2 为空 → 该源 0 条,靠 S3 写钩子随用增长,属预期)。
幂等:content_key 唯一,重跑 upsert 不膨胀。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2                                                     # noqa: E402

from pipeline.embeddings import embed_texts, vec_literal            # noqa: E402
from pipeline.semantic_index import (                               # noqa: E402
    DDL, UPSERT_SQL, analyze_snippet, fact_snippet, skydive_snippet, upsert_params)

DB_CONFIG = dict(
    host=os.environ.get("ALLOYDB_HOST", "localhost"), port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    sslmode="require", connect_timeout=10,
)


def collect_fact(cur) -> list[tuple[str, tuple]]:
    cur.execute("SELECT video_id, predicate, rationale, start_ts, end_ts "
                "FROM video_facts WHERE matched = true")
    out = []
    for vid, pred, rat, s, e in cur.fetchall():
        entry = fact_snippet({"video_id": vid, "predicate": pred, "rationale": rat,
                              "start_ts": s, "end_ts": e})
        if entry:
            out.append((vid, entry))
    return out


def collect_skydive(cur) -> list[tuple[str, tuple]]:
    cur.execute("SELECT video_id, summary, freefall_start_ts, freefall_end_ts FROM skydive_segments")
    out = []
    for vid, summary, s, e in cur.fetchall():
        entry = skydive_snippet({"video_id": vid, "summary": summary,
                                 "freefall_start_ts": s, "freefall_end_ts": e})
        if entry:
            out.append((vid, entry))
    return out


def collect_analyze() -> list[tuple[str, tuple]]:
    """扫 Redis L2 的 av:* 缓存(best-effort;memory 后端下为空,靠 S3 写钩子增长)。"""
    try:
        from pipeline.redis_client import build_redis_client
        r = build_redis_client()
        keys = []
        if hasattr(r, "scan_iter"):
            keys = [k for k in r.scan_iter(match="av:*", count=200)]
        else:                                            # upstash REST:游标 scan
            cursor = 0
            while True:
                cursor, batch = r.scan(cursor, match="av:*", count=200)
                keys.extend(batch)
                if not cursor:
                    break
        out = []
        for k in keys:
            key = k.decode() if isinstance(k, bytes) else str(k)
            try:
                dump = json.loads(r.get(key))
                vid = key.split(":", 2)[1]
                entry = analyze_snippet(vid, dump, key)
                if entry:
                    out.append((vid, entry))
            except Exception:
                continue
        return out
    except Exception as e:
        print(f"[analyze 源跳过] {str(e)[:80]}")
        return []


def main() -> None:
    ap = argparse.ArgumentParser(description="语义索引:建表+三源回填(幂等)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    password = os.environ.get("ALLOYDB_PASSWORD") or input("DB 密码: ")
    conn = psycopg2.connect(password=password, **DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    entries: list[tuple[str, str, tuple]] = []           # (video_id, source, entry)
    fact = collect_fact(cur)
    sky = collect_skydive(cur)
    ana = collect_analyze()
    entries += [(v, "fact", e) for v, e in fact]
    entries += [(v, "skydive", e) for v, e in sky]
    entries += [(v, "analyze", e) for v, e in ana]
    print(f"待入索引: fact={len(fact)}  skydive={len(sky)}  analyze={len(ana)}  合计={len(entries)}")
    if args.dry_run:
        for v, src, e in entries[:5]:
            print(f"  [dry] {src}:{v}  {e[1][:70]}")
        conn.close()
        return

    cur.execute(DDL)
    conn.commit()
    print("content_embeddings 表/索引就绪")

    vecs = embed_texts([e[1] for _, _, e in entries])
    if vecs is None:
        print("embed 失败,中止(库未写入,可重跑)")
        conn.close()
        return
    written = 0
    for (vid, src, entry), vec in zip(entries, vecs):
        cur.execute(UPSERT_SQL, upsert_params(entry, vid, src, vec_literal(vec)))
        written += 1
    conn.commit()
    cur.execute("SELECT source, COUNT(*) FROM content_embeddings GROUP BY source ORDER BY 2 DESC")
    print(f"回填完成: upsert {written} 条;库内分布: " +
          ", ".join(f"{s}={n}" for s, n in cur.fetchall()))
    conn.close()


if __name__ == "__main__":
    main()
