"""M5:用户实时上传的【临时视频】注册表。

设计(realtime-video-understanding §8):
  · 直传:前端拿【PUT 签名 URL】把视频直传 GCS 的专用前缀(uploads/<owner>/<vid>.<ext>),不经后端。
  · 临时:video_id 形如 up_<hex>,【不进 video_metadata】(免污染正式语料),只登记在 Redis 并带 TTL
    (≈ GCS lifecycle 自动删);analyze/show 的 gcs 解析对 up_ 开头的 id 来这里查(见 node_executor._resolve_gcs)。
  · 配额:每用户每天上传数上限(原子 INCR 计数,防并发刷爆)。
Redis 不可用 → fail-open(端点逻辑仍跑;只是 resolve 查不到 → 那次上传分析不了,不崩)。
"""
from __future__ import annotations

import datetime
import threading
import uuid

from pipeline import config

_CLIENT = None
_TRIED = False
_CLIENT_LOCK = threading.Lock()

# content_type → 对象扩展名(让 .mov/.webm 不被当成 .mp4 存;mime 由扩展名反推,见 _gemini_generate)
_EXT = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}


def _redis():
    global _CLIENT, _TRIED
    if _TRIED:                                       # 已建好:无锁快路
        return _CLIENT
    with _CLIENT_LOCK:                               # 首建:互斥 + 双检,防并发重复 init / 泄漏连接
        if not _TRIED:
            try:
                from pipeline.redis_client import build_redis_client
                _CLIENT = build_redis_client()
            except Exception:
                _CLIENT = None
            _TRIED = True
    return _CLIENT


def _key(video_id: str) -> str:
    return f"upload:{video_id}"


def _count_key(owner: str) -> str:
    return f"upload_count:{owner}:{datetime.date.today().isoformat()}"


def new_video_id() -> str:
    return "up_" + uuid.uuid4().hex                  # 过 _VIDEO_ID_RE 白名单 [A-Za-z0-9_-]+


def gcs_uri_for(owner: str, video_id: str, content_type: str = "video/mp4") -> str:
    ext = _EXT.get(content_type, "mp4")
    return f"gs://{config.GCS_BUCKET}/{config.UPLOAD_PREFIX}/{owner}/{video_id}.{ext}"


def count_today(owner: str) -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        cur = r.get(_count_key(owner))
        return int(cur) if cur else 0
    except Exception:
        return 0


def register(owner: str, content_type: str = "video/mp4") -> tuple[str, str] | None:
    """新建一个上传位 → (video_id, gcs_uri),登记到 Redis(TTL)并【原子】把当天计数 +1。
    超过每日配额 → None。Redis 不可用 → 降级返回位(不限额、resolve 查不到,但端点不崩)。"""
    video_id = new_video_id()
    gcs_uri = gcs_uri_for(owner, video_id, content_type)
    r = _redis()
    if r is None:
        return video_id, gcs_uri                     # 无 Redis → 降级
    try:
        ck = _count_key(owner)
        n = r.incr(ck)                               # 原子自增(首次创建)→ 关掉 check-then-set 的 TOCTOU
        if n == 1:
            try: r.expire(ck, 2 * 24 * 3600)
            except Exception: pass
        if n > config.MAX_UPLOADS_PER_DAY:
            try: r.decr(ck)                          # 超限 → 回退计数,拒绝
            except Exception: pass
            return None
        r.set(_key(video_id), gcs_uri, ex=config.UPLOAD_TTL_SECONDS)
        return video_id, gcs_uri
    except Exception:
        return video_id, gcs_uri                     # Redis 异常 → 降级(fail-open)


def resolve_gcs(video_id: str) -> str | None:
    """up_ 开头的临时视频 → 它的 gcs_uri(供 analyze/show 解析)。查不到/Redis 不可用 → None。"""
    r = _redis()
    if r is None:
        return None
    try:
        return r.get(_key(video_id))
    except Exception:
        return None


def _reset_for_test():
    global _CLIENT, _TRIED
    _CLIENT, _TRIED = None, False
