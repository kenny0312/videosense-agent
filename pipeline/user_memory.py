"""跨会话用户记忆(L2 / R2;设计 prompt-constitution-lessons.md §6)。

每个 owner 一小块 markdown(GCS `user-memory/{owner}/memory.md`,与 transcript 同伦):
存的是【关于用户的事实】—— 偏好、常问领域、明确要求(与全局教训集 lessons.py 相对,
那边是【系统怎么做事】)。每轮注入 loop 的「# 用户记忆(跨会话)」节;由 update_memory
工具在【用户明确表达偏好/纠正】时写入(判据写在工具声明里)。

设计取舍:
  · 上限 USER_MEMORY_MAX_CHARS,append 超限时【掐最旧的行】(每行一条,带日期);
  · 进程内 60s 缓存(省每请求一次 GCS 读;写入即失效);
  · 全程 fail-open:读不到 = 无记忆,写失败 = 本条丢弃并报错给大脑(它会告知用户);
  · owner 作用域(同 transcript 的 _scoped 防串号)。
"""
from __future__ import annotations

import logging
import threading
import time

from pipeline import config
from pipeline.transcript_store import _scoped

log = logging.getLogger("pipeline.user_memory")

_CACHE: dict[str, tuple[str, float]] = {}       # owner_key -> (text, fetched_at)
_CACHE_TTL = 60.0
_LOCK = threading.Lock()


def _key(owner: str) -> str:
    return _scoped(owner, "memory").replace(":", "/")    # kenny/memory


def _blob(owner: str):
    from google.cloud import storage
    bkt = storage.Client(project=config.GCP_PROJECT).bucket(config.GCS_BUCKET)
    return bkt.blob(f"user-memory/{_key(owner)}.md")


def load(owner: str) -> str:
    """读该 owner 的记忆;无/读失败 → ""(fail-open)。带 60s 进程缓存。"""
    k = _key(owner)
    with _LOCK:
        hit = _CACHE.get(k)
        if hit and time.time() - hit[1] < _CACHE_TTL:
            return hit[0]
    try:
        text = _blob(owner).download_as_text()
    except Exception:
        text = ""                                # 不存在/读失败 = 无记忆
    with _LOCK:
        _CACHE[k] = (text, time.time())
    return text


def update(owner: str, text: str, mode: str = "append") -> str:
    """写入记忆(update_memory 工具的后端)。append = 追加一行(带日期,超限掐最旧);
    rewrite = 整体重写(用于用户要求清理/纠正)。返回写入后的全文(给大脑确认)。"""
    text = str(text or "").strip()
    if not text:
        raise ValueError("update_memory 需要非空 text")
    if mode not in ("append", "rewrite"):
        raise ValueError(f"mode 只能是 append|rewrite,收到 {mode!r}")
    day = time.strftime("%Y-%m-%d")
    if mode == "rewrite":
        new = text[:config.USER_MEMORY_MAX_CHARS]
    else:
        cur = load(owner)
        lines = [l for l in cur.splitlines() if l.strip()]
        lines.append(f"- [{day}] {text}")
        while lines and sum(len(l) + 1 for l in lines) > config.USER_MEMORY_MAX_CHARS:
            lines.pop(0)                         # 超限掐最旧(近因优先)
        new = "\n".join(lines)
    _blob(owner).upload_from_string(new, content_type="text/markdown")
    with _LOCK:
        _CACHE[_key(owner)] = (new, time.time())  # 写入即刷新缓存
    return new


def render_section(owner: str) -> str:
    """拼 prompt 注入节;无记忆返回 ""(不占 token)。"""
    text = load(owner)
    if not text.strip():
        return ""
    return ("# 用户记忆(跨会话;用户之前明确表达过的偏好/事实,遵照执行)\n" + text.strip())
