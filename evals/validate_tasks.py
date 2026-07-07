"""校验数据集：每道题的金标必须能在真 mock DB（repl/_mock_db.py）里对上。

检查：video_id 存在、时间段合法、reward_basis 判分器名已知、id 不重复、kind 合法。

    python -m evals.validate_tasks
"""
from __future__ import annotations

import sys

from evals.runner import load_tasks

KNOWN_CHECKS = {"honesty", "retrieval", "timestamp", "count", "entity_match", "no_id_leak", "identity", "safety"}
KNOWN_BASIS = KNOWN_CHECKS | {"required_actions", "no_call", "state_assertions", "jga"}


def _mock_ids() -> set:
    from repl._mock_db import VIDEOS

    return {v[0] for v in VIDEOS}


def _ok_id(vid, ids) -> bool:
    return vid in ids or str(vid).startswith("up_")   # up_* = dual-control 上传的新视频


def validate(path: str = "evals/tasks"):
    ids = _mock_ids()
    tasks = load_tasks(path)
    errs = []
    seen = set()
    for t in tasks:
        tid = t.get("id", "?")
        if tid in seen:
            errs.append((tid, "重复 id"))
        seen.add(tid)
        if not t.get("reward_basis"):
            errs.append((tid, "缺 reward_basis"))
        if t.get("kind") not in ("single", "multi", None):
            errs.append((tid, f"kind 非法: {t.get('kind')}"))
        for b in t.get("reward_basis", []):
            if b not in KNOWN_BASIS:
                errs.append((tid, f"reward_basis 未知判分器: {b}"))
        ec = t.get("evaluation_criteria", {})
        oc = ec.get("output_checks", {})
        for vid in oc.get("retrieval", {}).get("must_surface_video_ids", []):
            if not _ok_id(vid, ids):
                errs.append((tid, f"retrieval 引用了不存在的 video_id: {vid}"))
        span = oc.get("timestamp", {}).get("gold_span")
        if span and not (0 <= span[0] < span[1]):
            errs.append((tid, f"timestamp span 非法: {span}"))
        for slot in ec.get("jga_slots", []) or []:
            for vid in slot.get("video_ids", []):
                if not _ok_id(vid, ids):
                    errs.append((tid, f"jga video_id 不存在: {vid}"))
            for vid in (slot.get("resolved_ordinal", {}) or {}).values():
                if not _ok_id(vid, ids):
                    errs.append((tid, f"jga resolved_ordinal 不存在: {vid}"))
    return tasks, errs


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    tasks, errs = validate()
    print(f"校验 {len(tasks)} 道题")
    if errs:
        print(f"发现 {len(errs)} 个问题：")
        for tid, msg in errs:
            print(f"  [{tid}] {msg}")
        return 1
    print("全部 grounded，无问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
