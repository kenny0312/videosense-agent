"""L1:id 清洗器 + 教训集纪律的离线单测(纯 Python)。
    python -m pytest pipeline/test_answer_guard.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline.answer_guard import ID_PAT, scrub_ids


def _show_value(*pairs):
    return {"note": "🎬", "items": [{"n": n, "video_id": vid} for n, vid in pairs]}


# ── 清洗器三路:替换 / 删除 / 无命中 ───────────────────────────
def test_scrub_replaces_mapped_id_with_ordinal():
    ans = "最精彩的是视频 803174656_822455_10761781481860,它有桶滚动作。"
    out, hits = scrub_ids(ans, [_show_value((2, "803174656_822455_10761781481860"))])
    assert hits == 1 and "803174656" not in out
    assert "第 2 个" in out                                # 映射到最近 show 的编号


def test_scrub_deletes_unmapped_and_tidies_wrappers():
    ans = "第 2 个视频(`803174656_822455_10761781481860`)最精彩;另见 GX010537_2 和 v_-02DygXbn6w。"
    out, hits = scrub_ids(ans, [])                         # 无 show 结果 → 全删
    assert hits == 3
    for tok in ("803174656", "GX010537", "v_-02DygXbn6w"):
        assert tok not in out
    assert "(`" not in out and "()" not in out            # 空括号/反引号残渣被收拾


def test_scrub_skips_redundant_ordinal():
    # 前文已写「第 2 个」,残留 id 直接删,不产生「第 2 个(第 2 个)」
    ans = "第 2 个(803174656_822455_10761781481860)最精彩。"
    out, hits = scrub_ids(ans, [_show_value((2, "803174656_822455_10761781481860"))])
    assert hits == 1 and out.count("第 2 个") == 1


def test_scrub_noop_on_clean_answer():
    ans = "有,共 14 个跳伞视频;2016 年的纪录是 32.094 公里。"    # 数字/年份不是 id
    out, hits = scrub_ids(ans, [_show_value((1, "GX010523"))])
    assert hits == 0 and out == ans                        # 零开销路径,原样返回


def test_scrub_uses_latest_show_result():
    older = _show_value((1, "GX010523"))
    newer = _show_value((3, "GX010523"))                   # 同一视频在最近一次列表里是第 3
    out, _ = scrub_ids("看 GX010523 那个。", [older, newer])
    assert "第 3 个" in out                                 # 最近一次 show 生效


def test_id_pattern_shapes():
    for tok in ("802393384_403362_14891780700587", "GX010537", "GX010537_2",
                "v_-02DygXbn6w", "up_" + "a1" * 8):
        assert ID_PAT.search(f"x {tok} y"), tok
    for tok in ("2016", "32.094", "466.20", "10米台", "n==3"):
        assert not ID_PAT.search(tok), tok


# ── 教训集纪律(预算/字段齐全/渲染)─────────────────────────
def test_lessons_budget_and_fields():
    from pipeline.lessons import LESSONS, MAX_LESSONS, render
    assert len(LESSONS) <= MAX_LESSONS                     # 预算硬上限:满了先蒸馏再进新
    ids = [l.id for l in LESSONS]
    assert len(ids) == len(set(ids))
    for l in LESSONS:
        assert l.born and l.origin and l.text and l.sunset, l.id   # 写不出退役条件不许入集
    r = render()
    assert all(l.id in r and l.text[:10] in r for l in LESSONS)
    assert all(l.sunset not in r or l.sunset in l.text for l in LESSONS)  # 元数据不烧 token
