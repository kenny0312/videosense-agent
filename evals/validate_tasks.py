"""校验数据集：每道题的金标必须能对上真实假片库，配置必须自洽。

检查项：
1. video_id 都存在（含 up_* 上传件）；时间段合法；id 不重复；kind 合法
2. 计分项（reward_basis）点名的每把尺子，题里必须真的配了对应内容
   —— 否则那道题会永远 0 分还报不出错（配置错误当场抓住）
3. 用户动作名必须是认识的（say/correct/upload_video/enrich_video/paste_image）

    python -m evals.validate_tasks
"""
from __future__ import annotations

import sys

from evals.runner import load_tasks
from evals.tools import USER_TOOLS

KNOWN_CHECKS = {"honesty", "retrieval", "timestamp", "count", "entity_match",
                "no_id_leak", "identity", "safety"}
KNOWN_BASIS = KNOWN_CHECKS | {"required_actions", "no_call", "no_forbidden",
                              "state_assertions", "jga"}


def _mock_ids() -> set:
    from repl._mock_db import VIDEOS

    return {v[0] for v in VIDEOS}


def _ok_id(vid, ids) -> bool:
    return vid in ids or str(vid).startswith("up_")   # up_* = 题里用户上传的新视频


def _basis_has_config(basis: str, t: dict) -> bool:
    """计分项在题里有没有对应的配置。没有=配置错误。"""
    ec = t.get("evaluation_criteria", {})
    if basis == "required_actions":
        return bool(ec.get("required_actions"))
    if basis == "no_call":
        return bool(ec.get("no_call_expected"))
    if basis == "no_forbidden":
        return bool(ec.get("forbidden_actions"))
    if basis == "jga":
        return bool(ec.get("jga_slots"))
    if basis == "state_assertions":
        return bool(ec.get("state_assertions"))
    return basis in ec.get("output_checks", {})


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
                errs.append((tid, f"reward_basis 有不认识的尺子: {b}"))
            elif not _basis_has_config(b, t):
                errs.append((tid, f"计分项 {b} 在题里没有对应配置（会永远 0 分）"))
        ec = t.get("evaluation_criteria", {})
        oc = ec.get("output_checks", {})
        for vid in oc.get("retrieval", {}).get("must_surface_video_ids", []):
            if not _ok_id(vid, ids):
                errs.append((tid, f"retrieval 引用了不存在的 video_id: {vid}"))
        for vid in oc.get("retrieval", {}).get("allowed_video_ids", []) or []:
            if not _ok_id(vid, ids):
                errs.append((tid, f"allowed 引用了不存在的 video_id: {vid}"))
        span = oc.get("timestamp", {}).get("gold_span")
        if span and not (0 <= span[0] < span[1]):
            errs.append((tid, f"timestamp 区间非法: {span}"))
        for slot in ec.get("jga_slots", []) or []:
            for vid in slot.get("video_ids", []):
                if not _ok_id(vid, ids):
                    errs.append((tid, f"jga video_id 不存在: {vid}"))
            for vid in (slot.get("resolved_ordinal", {}) or {}).values():
                if not _ok_id(vid, ids):
                    errs.append((tid, f"jga resolved_ordinal 不存在: {vid}"))
        for step in t.get("user", {}).get("script", []) or []:
            act = step.get("action") or {}
            name = act.get("tool") or act.get("type")
            if act and name not in USER_TOOLS:
                errs.append((tid, f"不认识的用户动作: {name}"))
    errs += _validate_split(tasks)
    return tasks, errs


def _validate_split(tasks) -> list:
    """GD-1 泄漏 lint:切分清单覆盖恰好全部题;家族不跨堂;安全/身份类必过题必须在封存;
    完全重复的问句不得跨堂(跨了 = 训练题泄漏验证/封存答案)。"""
    import json as _json
    import os as _os
    from evals.split_tool import MANIFEST_PATH, family_of
    errs: list = []
    if not _os.path.exists(MANIFEST_PATH):
        return [("split", "缺 split_manifest.json —— 跑 python -m evals.split_tool 生成")]
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        m = _json.load(f)
    splits, fams = m.get("splits", {}), m.get("families", {})
    ids = {t["id"] for t in tasks}
    for tid in sorted(ids - set(splits)):
        errs.append((tid, "不在切分清单里(题库变了 → 重跑 split_tool 并过目 diff)"))
    for tid in sorted(set(splits) - ids):
        errs.append((tid, "清单里有、题库里没有(改名/删题后没重跑 split_tool)"))
    fam_split: dict = {}
    for tid, sp in splits.items():
        f = fams.get(tid)
        if f in fam_split and fam_split[f] != sp:
            errs.append((tid, f"家族 {f} 跨堂({fam_split[f]} vs {sp})—— 金标事实泄漏"))
        fam_split.setdefault(f, sp)
    for t in tasks:
        f = family_of(t["id"])
        if t.get("pinned") and f in ("safety", "identity") and splits.get(t["id"]) != "sealed":
            errs.append((t["id"], "安全/身份类必过题必须在封存堂"))
    seen_q: dict = {}
    for t in tasks:
        q = (t.get("user_query") or "").strip()
        if not q:
            continue
        if q in seen_q and splits.get(seen_q[q]) != splits.get(t["id"]):
            errs.append((t["id"], f"与 {seen_q[q]} 问句完全相同却跨堂 —— 泄漏"))
        seen_q.setdefault(q, t["id"])
    return errs


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
    print("全部对得上，配置自洽，无问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
