"""批次 1.5 R2:删生产索引那把刀的防呆逻辑。

这是全仓唯一一处会 DELETE 生产表的代码路径。它的安全性完全靠"只删导出文件里
列明的、且带 av:gate- 前缀的 key",所以这几条断言比功能本身更重要 ——
前缀写错一个字符就会删掉真实的语义索引,而 embedding 重算是要花钱的。
"""
from __future__ import annotations

import json

import pytest

from evals.longhorizon_index_cleanup import RESIDUE_LIKE, RESIDUE_PREFIX, keys_to_delete


def _line(k):
    return json.dumps({"content_key": k, "video_id": "v_x", "source": "analyze",
                       "snippet": "s", "start_ts": None, "end_ts": None}, ensure_ascii=False)


def test_accepts_a_clean_export():
    txt = "\n".join(_line(f"av:gate-C-r1:v_{i}:abc{i}") for i in range(3))
    assert keys_to_delete(txt) == [f"av:gate-C-r1:v_{i}:abc{i}" for i in range(3)]


def test_rejects_production_keys():
    """生产 key 形如 av:{video_id}:{md5}(没有命名空间段)—— 出现一条就必须整体拒绝。"""
    txt = _line("av:gate-C-r1:v_1:aaa") + "\n" + _line("av:v_2:bbb")
    with pytest.raises(ValueError, match="不带"):
        keys_to_delete(txt)


def test_rejects_empty_export():
    """空文件不是"没什么可删",是"export 没跑成" —— 不许当成 no-op 放过。"""
    with pytest.raises(ValueError, match="空"):
        keys_to_delete("\n  \n")


def test_rejects_tampered_file():
    with pytest.raises(ValueError, match="读不出"):
        keys_to_delete(_line("av:gate-C-r1:v_1:aaa") + "\n{not json}")


def test_rejects_duplicate_keys():
    """重复 = 文件不是 export 直出的(export 走 ORDER BY content_key 且 key 有 UNIQUE 约束)。"""
    txt = _line("av:gate-C-r1:v_1:aaa") + "\n" + _line("av:gate-C-r1:v_1:aaa")
    with pytest.raises(ValueError, match="重复"):
        keys_to_delete(txt)


def test_prefix_and_like_pattern_agree():
    """LIKE 模式与前缀断言必须描述同一批行 —— 两者漂移就会出现"查得到但删不掉"
    或更糟的"删得到但查不到"。"""
    assert RESIDUE_LIKE == RESIDUE_PREFIX + "%"


def test_prefix_is_not_the_plan_typo():
    """任务书 §6-R2 写的是 'gate-%',那个 WHERE 在真库上匹配 0 行(实测)。
    钉住这条:谁把前缀改回 'gate-' 就红。"""
    assert RESIDUE_PREFIX.startswith("av:"), (
        "content_key 是 analyze 缓存键 av:{ns}:{video_id}:{md5},"
        "前缀必须含 av: —— 写成 'gate-' 会一行都匹配不到")
