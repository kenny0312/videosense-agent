"""M5:用户实时上传的【临时视频】注册表。

设计(realtime-video-understanding §8):
  · 直传:前端拿【PUT 签名 URL】把视频直传 GCS 的专用前缀(uploads/<owner>/<vid>.mp4),不经后端。
  · 临时:video_id 形如 up_<hex>,【不进 video_metadata】(免污染正式语料),只登记在 Redis 并带 TTL
    (≈ GCS lifecycle 自动删);analyze/show 的 gcs 解析对 up_ 开头的 id 来这里查(见 node_executor._resolve_gcs)。
  · 配额:每用户每天上传数上限。
Redis 不可用 → fail-open(端点逻辑仍跑;只是 resolve 查不到 → 那次上传分析不了,不崩)。
"""
from __future__ import annotations

import datetime
import uuid

from pipeline import config

_CLIENT = None
_TRIED = False


def _redis():
    global _CLIENT, _TRIED
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


def gcs_uri_for(owner: str, video_id: str) -> str:
    return f"gs://{config.GCS_BUCKET}/{config.UPLOAD_PREFIX}/{owner}/{video_id}.mp4"


def new_video_id() -> str:
    return "up_" + uuid.uuid4().hex          # 过 _VIDEO_ID_RE 白名单 [A-Za-z0-9_-]+


def count_today(owner: str) -> int:
    r = _redis()
    if r is None:
        return 0
    try:
        cur = r.get(_count_key(owner))
        return int(cur) if cur else 0
    except Exception:
        return 0


def register(owner: str) -> tuple[str, str] | None:
    """新建一个上传位 → (video_id, gcs_uri),登记到 Redis(TTL)并把当天计数 +1。
    超过每日配额 → None。Redis 不可用 → 仍返回位(降级:resolve 会查不到,但端点不崩)。"""
    if count_today(owner) >= config.MAX_UPLOADS_PER_DAY:
        return None
    video_id = new_video_id()
    gcs_uri = gcs_uri_for(owner, video_id)
    r = _redis()
    if r is not None:
        try:
            r.set(_key(video_id), gcs_uri, ex=config.UPLOAD_TTL_SECONDS)
            ck = _count_key(owner)
            cur = r.get(ck)
            r.set(ck, str((int(cur) if cur else 0) + 1), ex=2 * 24 * 3600)
        except Exception:
            pass
    return video_id, gcs_uri


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
