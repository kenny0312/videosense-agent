"""
把私有 gs:// 视频签成【浏览器可播放的短期 https URL】(给 show_video 节点用)。

GCS 对象是私有的,浏览器不能直接放 gs://。这里用 V4 签名出一个有过期时间的 https
直链(浏览器 <video> 原生支持 Range 拖动)。两种凭证路径都覆盖:
  · Cloud Run / 服务账号:走 IAM signBlob(creds 带 service_account_email + token)——
    需要运行时 SA 对自身有 roles/iam.serviceAccountTokenCreator(可签自己)。
  · 本地用户 ADC:没有签名私钥 → 签不了 → 返回 None(fail-open),前端优雅降级显示
    "暂不可播放",绝不抛错卡住请求。

惰性 import google-cloud-storage / google-auth —— 不在模块加载期碰网络/GCP。
"""
from __future__ import annotations

import logging
from datetime import timedelta

from pipeline import config

log = logging.getLogger("pipeline.video_url")

DEFAULT_TTL_MIN = 15


def parse_gcs_uri(uri: str) -> tuple[str, str] | None:
    """gs://bucket/path/to.mp4 → (bucket, 'path/to.mp4');非 gs:// → None。"""
    if not uri or not uri.startswith("gs://"):
        return None
    bucket, _, name = uri[5:].partition("/")
    if not bucket or not name:
        return None
    return bucket, name


def sign_gcs_uri(gcs_uri: str | None, ttl_minutes: int = DEFAULT_TTL_MIN) -> str | None:
    """把 gs:// 签成短期 https;任何失败 → None(fail-open)。"""
    parsed = parse_gcs_uri(gcs_uri or "")
    if not parsed:
        return None
    bucket_name, blob_name = parsed
    try:
        import google.auth
        from google.auth.transport import requests as ga_requests
        from google.cloud import storage

        creds, _ = google.auth.default()
        client = storage.Client(project=config.GCP_PROJECT, credentials=creds)
        blob = client.bucket(bucket_name).blob(blob_name)

        kwargs: dict = {"version": "v4",
                        "expiration": timedelta(minutes=ttl_minutes),
                        "method": "GET"}
        # 服务账号路径(Cloud Run):【必须先 refresh 再读 email】—— compute 凭证的
        # service_account_email 在刷新前是占位 "default"(signBlob 会报
        # "Invalid form of account ID default"),刷新后才解析成真实邮箱;
        # 然后走 IAM signBlob 签名,无需私钥文件。
        try:
            creds.refresh(ga_requests.Request())
        except Exception:
            pass
        email = getattr(creds, "service_account_email", None)
        token = getattr(creds, "token", None)
        if email and email != "default" and token:
            kwargs["service_account_email"] = email
            kwargs["access_token"] = token
        return blob.generate_signed_url(**kwargs)
    except Exception as e:
        log.warning("签名失败(fail-open,返回不可播放): %r", e)
        return None


def sign_gcs_put_url(gcs_uri: str | None, content_type: str = "video/mp4",
                     ttl_minutes: int = DEFAULT_TTL_MIN) -> str | None:
    """M5:把 gs:// 签成短期【PUT 直传】https URL —— 前端拿它把视频直传 GCS,【不经后端】。
    method=PUT + content_type 必须与前端 PUT 的 Content-Type 一致。失败 → None(fail-open;本地用户 ADC 签不了)。"""
    parsed = parse_gcs_uri(gcs_uri or "")
    if not parsed:
        return None
    bucket_name, blob_name = parsed
    try:
        import google.auth
        from google.auth.transport import requests as ga_requests
        from google.cloud import storage

        creds, _ = google.auth.default()
        client = storage.Client(project=config.GCP_PROJECT, credentials=creds)
        blob = client.bucket(bucket_name).blob(blob_name)
        kwargs: dict = {"version": "v4", "expiration": timedelta(minutes=ttl_minutes),
                        "method": "PUT", "content_type": content_type}
        try:
            creds.refresh(ga_requests.Request())                  # SA 路径:刷新后才有真实 email(同 GET)
        except Exception:
            pass
        email = getattr(creds, "service_account_email", None)
        token = getattr(creds, "token", None)
        if email and email != "default" and token:
            kwargs["service_account_email"] = email
            kwargs["access_token"] = token
        return blob.generate_signed_url(**kwargs)
    except Exception as e:
        log.warning("PUT 签名失败(fail-open): %r", e)
        return None
