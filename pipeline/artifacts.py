"""
图表产物持久化(Stage 10 交付物之一:可视化图表 URL)。

沙箱网络隔离,不能直接写 GCS;plot 节点在沙箱里只产出图像内容(svg 文本或
png_base64),由可信主进程在这里落盘:
    - 优先上传 GCS,返回 gs:// URL
    - GCS 不可用(无凭证 / mock)则存本地 artifacts/,返回 file:// 路径
"""
from __future__ import annotations

import base64
import logging
import os

from pipeline import config

log = logging.getLogger("pipeline.artifacts")

LOCAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts")
_LOCAL_DIR = LOCAL_DIR  # 向后兼容别名


def save_local(artifact: dict, name: str) -> str | None:
    """只存本地 artifacts/,返回文件名(如 'abc.svg')。
    供 API 静态服务用 —— 浏览器可直接 http 访问,不依赖 GCS。"""
    if artifact.get("svg"):
        data, ext = artifact["svg"].encode("utf-8"), "svg"
    elif artifact.get("png_base64"):
        data, ext = base64.b64decode(artifact["png_base64"]), "png"
    else:
        return None
    os.makedirs(LOCAL_DIR, exist_ok=True)
    fname = f"{name}.{ext}"
    with open(os.path.join(LOCAL_DIR, fname), "wb") as f:
        f.write(data)
    return fname


def persist_plot(artifact: dict, name: str) -> str | None:
    """把图像产物落盘,返回可访问 URL(gs:// 或 file://)。"""
    if artifact.get("svg"):
        return _persist(artifact["svg"].encode("utf-8"), name, "svg", "image/svg+xml")
    if artifact.get("png_base64"):
        return _persist(base64.b64decode(artifact["png_base64"]), name, "png", "image/png")
    return None


def _persist(data: bytes, name: str, ext: str, content_type: str) -> str:
    # 1) 试 GCS
    try:
        from google.cloud import storage
        client = storage.Client(project=config.GCP_PROJECT)
        bucket = client.bucket(config.GCS_BUCKET)
        blob = bucket.blob(f"plots/{name}.{ext}")
        blob.upload_from_string(data, content_type=content_type)
        url = f"gs://{config.GCS_BUCKET}/plots/{name}.{ext}"
        log.info("plot 已上传 %s", url)
        return url
    except Exception as e:
        log.warning("GCS 上传失败,回退本地: %s", e)

    # 2) 回退本地
    os.makedirs(_LOCAL_DIR, exist_ok=True)
    path = os.path.join(_LOCAL_DIR, f"{name}.{ext}")
    with open(path, "wb") as f:
        f.write(data)
    return f"file://{path}"
