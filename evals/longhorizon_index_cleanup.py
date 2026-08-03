"""批次 1.5 R2:盘点/导出/清理写进【生产】语义索引的评测残留。

背景:`longhorizon_run.py` 只隔离了 `ANALYZE_CACHE_NS`,语义索引这一路零隔离 ——
`node_executor._index_analyze_result` 用同一组生产凭据 UPSERT 进 `content_embeddings`。
gate 实验的 1000+ 次 analyze 产物永久留在一个总共 514 条视频的生产库里,
用户做 semantic_search 会命中评测残留。

⚠️ 任务书 §6-R2 写的是 `content_key LIKE 'gate-%'` —— **那个 WHERE 匹配不到任何行**。
实际的 content_key 是 analyze 缓存键(`analyze_cache.make_key`):
    有命名空间:av:{ns}:{video_id}:{md5}      ← 评测残留,ns 形如 gate-C-r1
    无命名空间:av:{video_id}:{md5}           ← 生产正常产物
所以正确的前缀是 `av:gate-%`。照任务书写会删 0 行然后以为清干净了。

三个子命令,默认只读:
    count   统计残留(按命名空间分组),不改任何东西
    export  把要删的行整份导出成 JSONL 归档
    delete  真删(必须先 export,且要 --yes-i-exported)

用法:
    python -m evals.longhorizon_index_cleanup count
    python -m evals.longhorizon_index_cleanup export --out evals/runs/index-gate-residue.jsonl
    python -m evals.longhorizon_index_cleanup delete --yes-i-exported evals/runs/index-gate-residue.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 评测残留的 content_key 前缀。用 LIKE 而不是正则:走得到索引。
RESIDUE_LIKE = "av:gate-%"
# 防呆:任何一条要删的 key 都必须过这个断言。前缀写错会删掉生产索引 —— 不可逆。
RESIDUE_PREFIX = "av:gate-"


def _db():
    from pipeline import config  # noqa: F401  —— .env 装载副作用(只读凭证)
    from pipeline import semantic_index as si
    return si


def cmd_count(a):
    si = _db()
    total = si._execute("SELECT COUNT(*) FROM content_embeddings", ())[0][0]
    resid = si._execute("SELECT COUNT(*) FROM content_embeddings WHERE content_key LIKE %s",
                        (RESIDUE_LIKE,))[0][0]
    # 任务书原文的 WHERE —— 一并跑一次,证明它匹配不到东西
    plan_w = si._execute("SELECT COUNT(*) FROM content_embeddings WHERE content_key LIKE %s",
                         ("gate-%",))[0][0]
    print(f"content_embeddings 总行数        : {total}")
    print(f"评测残留(av:gate-%)            : {resid}")
    print(f"[对照] 任务书写的 'gate-%' 能匹配到: {plan_w}   ← 任务书那个 WHERE 是错的")
    print(f"清理后剩余                       : {total - resid}")
    if resid:
        print("\n按命名空间分组:")
        rows = si._execute(
            "SELECT split_part(content_key, ':', 2) AS ns, COUNT(*), COUNT(DISTINCT video_id) "
            "FROM content_embeddings WHERE content_key LIKE %s GROUP BY 1 ORDER BY 2 DESC",
            (RESIDUE_LIKE,))
        print(f"  {'命名空间':<22}{'行数':>8}{'涉及视频':>10}")
        for ns, n, nv in rows:
            print(f"  {ns:<22}{n:>8}{nv:>10}")
        print("\n按 source 分组:")
        for s, n in si._execute(
                "SELECT source, COUNT(*) FROM content_embeddings WHERE content_key LIKE %s "
                "GROUP BY 1 ORDER BY 2 DESC", (RESIDUE_LIKE,)):
            print(f"  {s}: {n}")
        print("\n抽样 3 条:")
        for k, vid, snip in si._execute(
                "SELECT content_key, video_id, left(snippet, 90) FROM content_embeddings "
                "WHERE content_key LIKE %s LIMIT 3", (RESIDUE_LIKE,)):
            print(f"  {k}\n    {vid}: {snip}…")
    return resid


def cmd_export(a):
    si = _db()
    rows = si._execute(
        "SELECT content_key, video_id, source, snippet, start_ts, end_ts "
        "FROM content_embeddings WHERE content_key LIKE %s ORDER BY content_key",
        (RESIDUE_LIKE,))
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for k, vid, src, snip, s0, s1 in rows:
            # 不导 embedding 向量:它是可重算的派生物,导出目的是【留证据能人读】,
            # 不是能原样灌回去(真要回灌就重跑一次 analyze,那才是可信的恢复)。
            fh.write(json.dumps({"content_key": k, "video_id": vid, "source": src,
                                 "snippet": snip, "start_ts": s0, "end_ts": s1},
                                ensure_ascii=False, default=str) + "\n")
    print(f"已导出 {len(rows)} 行 → {a.out}")
    return len(rows)


def cmd_delete(a):
    exp = ROOT / a.yes_i_exported
    if not exp.exists():
        sys.exit(f"拒绝执行:导出文件 {a.yes_i_exported} 不存在。先跑 export。")
    keys = [json.loads(l)["content_key"] for l in exp.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not keys:
        sys.exit("拒绝执行:导出文件是空的。")
    bad = [k for k in keys if not k.startswith(RESIDUE_PREFIX)]
    if bad:                                    # 防呆:导出文件被换过/前缀写错 → 立刻停
        sys.exit(f"拒绝执行:导出文件里有 {len(bad)} 条不带 {RESIDUE_PREFIX} 前缀的 key,"
                 f"例如 {bad[0]!r}。这会删到生产索引。")
    si = _db()
    before = si._execute("SELECT COUNT(*) FROM content_embeddings", ())[0][0]
    # 按【导出文件里的具体 key】删,不按 LIKE 删 —— 导出与删除必须是同一批行,
    # 否则两次查询之间新写入的行会被无声带走(评测和生产共用一张表)。
    si._execute("DELETE FROM content_embeddings WHERE content_key = ANY(%s)", (keys,))
    after = si._execute("SELECT COUNT(*) FROM content_embeddings", ())[0][0]
    left = si._execute("SELECT COUNT(*) FROM content_embeddings WHERE content_key LIKE %s",
                       (RESIDUE_LIKE,))[0][0]
    print(f"删除前 {before} 行 → 删除后 {after} 行(实删 {before - after},导出里有 {len(keys)} 条)")
    print(f"仍有残留:{left}(>0 说明导出之后又跑了评测)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("count")
    e = sub.add_parser("export")
    e.add_argument("--out", default="evals/runs/index-gate-residue.jsonl")
    d = sub.add_parser("delete")
    d.add_argument("--yes-i-exported", required=True, metavar="导出文件路径",
                   help="必须传入 export 产出的文件路径 —— 没有归档就不许删")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    {"count": cmd_count, "export": cmd_export, "delete": cmd_delete}[a.cmd](a)


if __name__ == "__main__":
    main()
