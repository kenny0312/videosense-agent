"""L2:跨会话用户记忆的离线单测(fake blob,不碰 GCS)。
    python -m pytest pipeline/test_user_memory.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pytest

from pipeline import user_memory as um


class _FakeBlob:
    store: dict[str, str] = {}

    def __init__(self, key: str):
        self.key = key

    def download_as_text(self) -> str:
        if self.key not in self.store:
            raise FileNotFoundError(self.key)
        return self.store[self.key]

    def upload_from_string(self, s: str, content_type: str = "") -> None:
        self.store[self.key] = s


@pytest.fixture(autouse=True)
def _fake_gcs(monkeypatch):
    _FakeBlob.store = {}
    um._CACHE.clear()
    monkeypatch.setattr(um, "_blob", lambda owner: _FakeBlob(um._key(owner)))
    yield


def test_append_and_load_roundtrip():
    um.update("kenny", "问数量时直接报数字,不用解释")
    text = um.load("kenny")
    assert "直接报数字" in text and text.startswith("- [")     # 带日期行
    assert "遵照执行" in um.render_section("kenny")            # 注入节拼装


def test_append_caps_oldest_first(monkeypatch):
    from pipeline import config
    monkeypatch.setattr(config, "USER_MEMORY_MAX_CHARS", 40)   # 单行 ~30 字符:两行必超 → 掐最旧
    um.update("kenny", "旧偏好" * 5)
    um.update("kenny", "新偏好" * 5)
    text = um.load("kenny")
    assert "新偏好" in text and "旧偏好" not in text            # 超限掐最旧(近因优先)


def test_rewrite_replaces_all():
    um.update("kenny", "A")
    um.update("kenny", "整体重写后的唯一内容", mode="rewrite")
    text = um.load("kenny")
    assert text == "整体重写后的唯一内容" and "A" not in text


def test_validation():
    with pytest.raises(ValueError):
        um.update("kenny", "  ")
    with pytest.raises(ValueError):
        um.update("kenny", "x", mode="delete")


def test_owner_isolation():
    um.update("alice", "alice 的偏好")
    assert "alice 的偏好" not in um.load("bob")                # 不串号
    assert um.render_section("bob") == ""                      # 无记忆不占 token


def test_cache_invalidated_on_write():
    um.update("kenny", "第一条")
    assert "第一条" in um.load("kenny")                        # 命中缓存
    um.update("kenny", "第二条")
    assert "第二条" in um.load("kenny")                        # 写入即刷新


def test_update_memory_declaration_gated(monkeypatch):
    from pipeline import config
    from pipeline import loop_driver as ld
    monkeypatch.setattr(config, "USE_USER_MEMORY", False)
    assert "update_memory" not in [d["name"] for d in ld.loop_function_declarations()]
    monkeypatch.setattr(config, "USE_USER_MEMORY", True)
    assert "update_memory" in [d["name"] for d in ld.loop_function_declarations()]
