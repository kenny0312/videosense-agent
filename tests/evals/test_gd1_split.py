"""GD-1(家族切分)测试:清单覆盖/家族不跨堂/安全封存/重复问句同堂 + 分布合理性。"""
import json

from evals.runner import load_tasks
from evals.split_tool import MANIFEST_PATH, build_manifest, family_of, SEALED_FAMILIES


def _tasks():
    return load_tasks("evals/tasks")


def _manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_manifest_covers_exactly_all_tasks():
    ids = {t["id"] for t in _tasks()}
    m = _manifest()
    assert set(m["splits"]) == ids                      # 不缺、不多


def test_every_task_has_family_rule():
    for t in _tasks():
        # 生成题自带 family 字段,手写题走 FAMILY_RULES —— 两条路都不许漏
        assert family_of(t["id"], t) is not None, t["id"]


def test_family_never_straddles_splits():
    m = _manifest()
    fam_split: dict = {}
    for tid, sp in m["splits"].items():
        f = m["families"][tid]
        assert fam_split.setdefault(f, sp) == sp, f"家族 {f} 跨堂"


def test_safety_identity_pinned_all_sealed():
    m = _manifest()
    for t in _tasks():
        if t.get("pinned") and family_of(t["id"]) in ("safety", "identity"):
            assert m["splits"][t["id"]] == "sealed", t["id"]


def test_duplicate_queries_same_split():
    m = _manifest()
    seen: dict = {}
    for t in _tasks():
        q = (t.get("user_query") or "").strip()
        if not q:
            continue
        if q in seen:
            assert m["splits"][seen[q]] == m["splits"][t["id"]], f"重复问句跨堂: {t['id']}"
        seen.setdefault(q, t["id"])


def test_split_proportions_sane():
    m = _manifest()
    from collections import Counter
    c = Counter(m["splits"].values())
    n = sum(c.values())
    assert c["train"] / n >= 0.5                        # 训练堂要够大(反思器的养料)
    assert c["val"] >= 15 and c["sealed"] >= 15         # 验证/封存都要有统计意义的规模


def test_build_manifest_is_deterministic():
    t = _tasks()
    assert build_manifest(t) == build_manifest(t)       # 同题库 → 字节级同清单(可重跑可 diff)


def test_wingsuit_family_is_one_unit():
    """审计点名的 20 题 is_wingsuit 事实族:必须整族同堂(防金标事实泄漏)。"""
    m = _manifest()
    fam = [tid for tid, f in m["families"].items() if f == "wingsuit"]
    assert len(fam) >= 8
    assert len({m["splits"][tid] for tid in fam}) == 1
