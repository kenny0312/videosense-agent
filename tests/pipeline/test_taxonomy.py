"""受控大类词表 + normalize + 主类推导的离线单测(纯 Python,不碰 DB/GCP)。
    python -m pytest tests/pipeline/test_taxonomy.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline.taxonomy import (
    category_for_predicate, main_categories_for, normalize_category,
)
from pipeline.taxonomy_seed import (
    ALIASES, CATEGORIES, GENERIC_CATEGORIES, PREDICATE_TO_CATEGORY,
)


# ── 种子一致性:三份数据互相咬合,别名/映射不许指向词表外 ─────────────
def test_seed_categories_unique_lowercase():
    assert len(CATEGORIES) == len(set(CATEGORIES))
    assert all(c == c.casefold() for c in CATEGORIES)          # 全小写(比对基准)
    assert 20 <= len(CATEGORIES) <= 50                         # 设计:~30-50 的受控规模


def test_seed_aliases_point_into_vocab():
    cats = set(CATEGORIES)
    assert all(v in cats for v in ALIASES.values())
    assert all(k == k.casefold() for k in ALIASES)             # 别名键小写(casefold 查表)


def test_seed_predicate_mapping_complete_and_valid():
    cats = set(CATEGORIES)
    assert all(v in cats for v in PREDICATE_TO_CATEGORY.values())
    assert len(PREDICATE_TO_CATEGORY) >= 197                   # 覆盖回填当日全部谓词
    assert GENERIC_CATEGORIES < cats                           # 泛化类 ⊂ 词表


# ── normalize:①词表 ②别名 ③谓词 ④None ─────────────────────────
def test_normalize_exact_and_casefold():
    assert normalize_category("skydiving") == "skydiving"
    assert normalize_category("  Skydiving ") == "skydiving"


def test_normalize_aliases_zh_en():
    assert normalize_category("跳伞") == "skydiving"
    assert normalize_category("翼装") == "skydiving"
    assert normalize_category("做饭") == "cooking & food"
    assert normalize_category("滑雪") == "winter sports"
    assert normalize_category("攀岩") == "climbing"
    assert normalize_category("Wingsuit") == "skydiving"


def test_normalize_predicates():
    assert normalize_category("preparing salad") == "cooking & food"
    assert normalize_category("wingsuit skydiving") == "skydiving"
    assert category_for_predicate("vacuuming carpet") == "household & cleaning"


def test_normalize_unknown_returns_none():
    assert normalize_category("量子力学") is None
    assert normalize_category("") is None
    assert normalize_category(None) is None
    assert normalize_category("   ") is None


# ── video 级主类:多数决 + 泛化类靠后 + 恰1/并列≤2 ──────────────────
def test_main_category_majority():
    preds = ["skydiving", "wingsuit skydiving", "walking"]     # skydiving×2 vs everyday×1
    assert main_categories_for(preds) == ["skydiving"]


def test_main_category_generic_demoted():
    # "边讲解边攀岩":有具体类时泛化类永远不当主类(哪怕票多)
    preds = ["talking to camera", "explaining", "teaching", "rock climbing"]
    assert main_categories_for(preds) == ["climbing"]


def test_main_category_generic_fallback_when_nothing_specific():
    assert main_categories_for(["talking", "explaining"]) == ["talking & presenting"]


def test_main_category_tie_caps_at_two():
    # 三类各 1 票并列 → 只取 2 个(排序幂等)
    out = main_categories_for(["skiing", "swimming", "rock climbing"])
    assert len(out) == 2 and out == sorted(out)


def test_main_category_empty_and_unmapped():
    assert main_categories_for([]) == []
    assert main_categories_for(["totally unknown thing"]) == []
