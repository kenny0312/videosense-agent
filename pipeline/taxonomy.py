"""受控大类:normalize 与 video 级主类推导(设计 docs/design/ingest-category-standard.md)。

真源 = pipeline/taxonomy_seed.py(代码即词表,git 可审可回滚;Neon 两表由
perception/setup_categories.py 同步,只为 SQL join 服务)。本模块是纯函数,无 DB 依赖:
  · normalize_category(raw)      —— 任意说法(中/英/别名/谓词)→ 受控大类;对不上 → None
  · category_for_predicate(pred) —— 细谓词 → 大类(197 映射表;不在表里再走 normalize)
  · main_categories_for(preds)   —— 一个视频的谓词集合 → 主类(恰 1,并列最多 2;泛化类靠后)
"""
from __future__ import annotations

from collections import Counter

from pipeline.taxonomy_seed import (
    ALIASES, CATEGORIES, GENERIC_CATEGORIES, PREDICATE_TO_CATEGORY,
)

_CATEGORY_SET = set(CATEGORIES)
MAX_MAIN_CATEGORIES = 2          # 恰 1 个主类;真并列(计票相同)最多 2


def normalize_category(raw: str | None) -> str | None:
    """任意原始说法 → 受控大类;对不上词表返回 None(调用方【绝不】自造新大类)。
    ① casefold 精确命中 categories;② 命中 aliases;③ 命中谓词映射表;④ None。"""
    if not raw or not str(raw).strip():
        return None
    key = str(raw).strip().casefold()
    if key in _CATEGORY_SET:
        return key
    hit = ALIASES.get(key)
    if hit:
        return hit
    return PREDICATE_TO_CATEGORY.get(key)


def category_for_predicate(predicate: str | None) -> str | None:
    """细谓词 → 大类。谓词本身是大类标签时(如 skydiving)返回它自己。"""
    return normalize_category(predicate)


def main_categories_for(predicates: list[str]) -> list[str]:
    """一个视频的谓词集合 → 主类列表(1 个;票数并列最多 2 个)。
    多数决,泛化类(everyday/talking/spectating)只在【没有任何具体类】时兜底 ——
    一个"边说话边攀岩"的视频,主类是 climbing 不是 talking & presenting。"""
    votes = Counter()
    for p in predicates or []:
        c = category_for_predicate(p)
        if c:
            votes[c] += 1
    if not votes:
        return []
    specific = {c: n for c, n in votes.items() if c not in GENERIC_CATEGORIES}
    pool = specific or dict(votes)                     # 有具体类就只在具体类里选
    top = max(pool.values())
    winners = sorted([c for c, n in pool.items() if n == top])   # 排序保证幂等输出
    return winners[:MAX_MAIN_CATEGORIES]
