"""M5:上传注册表 + gcs 解析(注入 fake redis,离线)。"""
from pipeline import uploads, config
import pipeline.node_executor as ne


class _FakeRedis:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v, ex=None): self.store[k] = v
    def incr(self, k): self.store[k] = int(self.store.get(k, 0)) + 1; return self.store[k]
    def decr(self, k): self.store[k] = int(self.store.get(k, 0)) - 1; return self.store[k]
    def expire(self, k, ttl): pass


def test_register_and_resolve(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(uploads, "_redis", lambda: fake)
    vid, gcs = uploads.register("alice")
    assert vid.startswith("up_")
    assert gcs.endswith(f"/{vid}.mp4") and "alice" in gcs and config.UPLOAD_PREFIX in gcs
    assert uploads.resolve_gcs(vid) == gcs                  # 注册后可解析
    assert uploads.resolve_gcs("up_nonexistent") is None


def test_content_type_extension(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(uploads, "_redis", lambda: fake)
    _, gcs = uploads.register("a", content_type="video/quicktime")
    assert gcs.endswith(".mov")                             # .mov 不被当成 .mp4
    _, gcs2 = uploads.register("a", content_type="video/mp4")
    assert gcs2.endswith(".mp4")


def test_daily_quota_atomic(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(uploads, "_redis", lambda: fake)
    monkeypatch.setattr(config, "MAX_UPLOADS_PER_DAY", 2)
    assert uploads.register("bob") is not None              # 1
    assert uploads.register("bob") is not None              # 2
    assert uploads.register("bob") is None                  # 3 → 超上限(INCR→3>2→decr→拒)
    assert uploads.count_today("bob") == 2                  # 计数回退到 2(没漂)
    assert uploads.register("carol") is not None            # 另一个用户独立计数


def test_failopen_no_redis(monkeypatch):
    monkeypatch.setattr(uploads, "_redis", lambda: None)
    vid, gcs = uploads.register("x")                        # Redis 无 → 仍给位(降级,不崩)
    assert vid.startswith("up_")
    assert uploads.resolve_gcs(vid) is None                 # 但没真注册 → 解析不到
    assert uploads.count_today("x") == 0


def test_resolve_gcs_upload_vs_metadata(monkeypatch):
    from pipeline import mcp_client as mc
    monkeypatch.setattr(mc, "query_db", lambda sql: [{"gcs_uri": "gs://b/meta.mp4"}])
    monkeypatch.setattr(uploads, "resolve_gcs",
                        lambda vid: "gs://b/up.mp4" if vid == "up_abc" else None)
    assert ne._resolve_gcs("up_abc") == "gs://b/up.mp4"     # 上传 → uploads 注册表
    assert ne._resolve_gcs("v_normal") == "gs://b/meta.mp4"  # 普通 → video_metadata
    assert ne._resolve_gcs("up_missing") == "gs://b/meta.mp4"  # up_ 没注册到 → 回退 metadata
