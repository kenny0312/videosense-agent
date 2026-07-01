"""受控大类:建表 + 词表同步 + 大类行回填(一次性/幂等;设计 ingest-category-standard.md T1)。

    python -m perception.setup_categories --dry-run   # 只打印将发生什么,不写库
    python -m perception.setup_categories              # 建表 + 同步词表 + 回填

做三件事(全部幂等,可反复跑):
  1. 建 categories / category_aliases 两张小表,并从 pipeline/taxonomy_seed.py 同步
     (代码即真源;表只为 SQL join「有什么类别」服务。改词表 = 改 seed + 重跑本脚本)。
  2. 回填:对每个视频,把它已有的细谓词经 taxonomy.main_categories_for 推出主类(恰 1,
     并列≤2),以 predicate=大类 写一行 video_facts(ON CONFLICT DO NOTHING;
     rationale 带溯源:'category: derived from predicates: …')。
  3. 报告:每类视频数、没有任何大类行的视频清单(谓词对不上词表 → 人工/下次补)。

连接同 perception 其它脚本(neon.env 的 ALLOYDB_*)。
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.taxonomy import main_categories_for                  # noqa: E402
from pipeline.taxonomy_seed import ALIASES, CATEGORIES             # noqa: E402

DB_CONFIG = dict(
    host=os.environ.get("ALLOYDB_HOST", "localhost"),
    port=5432,
    dbname=os.environ.get("ALLOYDB_DB", "your_database"),
    user=os.environ.get("ALLOYDB_USER", "postgres"),
    sslmode="require",
    connect_timeout=10,
)

DDL = """
CREATE TABLE IF NOT EXISTS categories (
    label TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS category_aliases (
    alias TEXT PRIMARY KEY,
    label TEXT NOT NULL REFERENCES categories(label)
);
"""

VF_UPSERT = ("INSERT INTO video_facts (video_id, predicate, matched, confidence, rationale) "
             "VALUES (%s, %s, true, 1.0, %s) "
             "ON CONFLICT (video_id, predicate) DO NOTHING")


def sync_vocab(cur) -> None:
    """seed → 两表(先补 categories,再全量对齐 aliases;删除词表里已不存在的旧行)。"""
    cur.execute(DDL)
    for c in CATEGORIES:
        cur.execute("INSERT INTO categories(label) VALUES (%s) ON CONFLICT DO NOTHING", (c,))
    cur.execute("DELETE FROM categories WHERE label != ALL(%s)", (list(CATEGORIES),))
    for alias, label in ALIASES.items():
        cur.execute("INSERT INTO category_aliases(alias, label) VALUES (%s, %s) "
                    "ON CONFLICT (alias) DO UPDATE SET label = EXCLUDED.label", (alias, label))
    cur.execute("DELETE FROM category_aliases WHERE alias != ALL(%s)", (list(ALIASES.keys()),))


def backfill(cur, dry: bool) -> tuple[int, list[str]]:
    """按视频回填大类行;返回 (新写行数, 推不出大类的视频列表)。"""
    cur.execute("SELECT vm.video_id, ARRAY_AGG(DISTINCT vf.predicate) "
                "FROM video_metadata vm LEFT JOIN video_facts vf ON vf.video_id = vm.video_id "
                "GROUP BY vm.video_id ORDER BY vm.video_id")
    rows = cur.fetchall()
    written, uncategorized = 0, []
    for video_id, preds in rows:
        preds = [p for p in (preds or []) if p]
        cats = main_categories_for(preds)
        if not cats:
            uncategorized.append(video_id)
            continue
        rationale = "category: derived from predicates: " + ", ".join(sorted(preds)[:8])
        for c in cats:
            if dry:
                print(f"  [dry] {video_id} -> {c}")
                written += 1
                continue
            cur.execute(VF_UPSERT, (video_id, c, rationale))
            written += cur.rowcount                        # DO NOTHING 命中已有行 → 0(幂等重跑不虚报)
    return written, uncategorized


def report(cur) -> None:
    cur.execute("SELECT vf.predicate, COUNT(DISTINCT vf.video_id) FROM video_facts vf "
                "JOIN categories c ON c.label = vf.predicate "
                "GROUP BY vf.predicate ORDER BY 2 DESC, 1")
    print("\n各大类视频数:")
    for label, n in cur.fetchall():
        print(f"  {n:3d}  {label}")


def main() -> None:
    ap = argparse.ArgumentParser(description="受控大类:建表+词表同步+回填(幂等)")
    ap.add_argument("--dry-run", action="store_true", help="只打印,不写库")
    args = ap.parse_args()

    password = os.environ.get("ALLOYDB_PASSWORD") or input("DB 密码 (Neon/AlloyDB): ")
    conn = psycopg2.connect(password=password, **DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        if not args.dry_run:
            sync_vocab(cur)
            conn.commit()
            print(f"词表已同步: {len(CATEGORIES)} 大类, {len(ALIASES)} 别名")
        written, uncategorized = backfill(cur, args.dry_run)
        if args.dry_run:
            conn.rollback()
            print(f"\n[dry-run] 将写入 {written} 行大类;推不出大类的视频 {len(uncategorized)} 个")
        else:
            conn.commit()
            print(f"\n回填完成: 新写 {written} 行大类;推不出大类的视频 {len(uncategorized)} 个")
            report(cur)
        if uncategorized:
            print("无大类视频(谓词对不上词表,待人工/下次 ingest 补):")
            for v in uncategorized:
                print(f"  - {v}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
