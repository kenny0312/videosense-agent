"""裁判对表装置的离线单测：κ 算得对、人工标注不会被 collect 冲掉。"""
import json

import pytest

from evals import calibrate_judge as cj


def _items_or_skip(n_min: int = 3):
    """样本来自本地 runs 归档(gitignored)——全新环境(CI/新 clone)没有归档时跳过,
    不把'这台机器没跑过评测'误报成代码坏了。"""
    items = cj._gather_items()
    if len(items) < n_min:
        pytest.skip(f"本地 runs 归档样本不足({len(items)} < {n_min}),数据依赖测试跳过")
    return items


def test_kappa_values():
    assert cj._kappa([(True, True), (False, False)] * 5) == 1.0        # 完全一致
    assert cj._kappa([(True, False), (False, True)] * 5) == -1.0       # 完全相反
    # 一半一半（两边各 50% 说是）≈ 跟瞎蒙一样 → κ≈0
    mixed = [(True, True), (True, False), (False, True), (False, False)] * 3
    assert abs(cj._kappa(mixed)) < 0.01


def test_gather_dedupes_and_keys():
    items = _items_or_skip(n_min=1)
    keys = [i["key"] for i in items]
    assert len(keys) == len(set(keys))                # key 唯一
    assert all(i["judge"] is None and i["human"] is None for i in items)
    assert len(items) <= 200                          # 上限防失控;下限取决于本地归档,不作断言


def test_collect_merge_keeps_human_labels(tmp_path, monkeypatch):
    """collect 重跑时，已有的人工标注和裁判判决都不能丢。"""
    cal = tmp_path / "cal.jsonl"
    monkeypatch.setattr(cj, "CAL_PATH", str(cal))
    items = _items_or_skip(n_min=3)[:3]
    items[0]["human"] = True                          # 已人工标注
    items[1]["judge"] = False                         # 已有裁判判决
    cj._save(items)
    loaded = cj._load()
    old = {it["key"]: it for it in loaded}
    fresh = cj._gather_items()[:3]
    for it in fresh:                                  # collect 里的合并逻辑
        prev = old.get(it["key"]) or {}
        it["human"] = prev.get("human")
        if prev.get("judge") is not None:
            it["judge"] = prev["judge"]
    assert fresh[0]["human"] is True
    assert fresh[1]["judge"] is False
