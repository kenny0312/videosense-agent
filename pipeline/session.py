"""
会话极薄壳 —— 记忆简化后,会话层【只】保留跨轮单调【轮号】(供 record_loop_turn 标号)。

历史/指代/产物全部收敛到【唯一记忆 = transcript】(loop_memory + transcript_store):
  - 多轮上下文 / 指代解析 / meta:loop 用 transcript 回放(build_loop_context)自己做。
  - 旧的 history / rolling / catalog / 值复用(artifact_value_store)已删。

本对象只剩:session_id + _turn_no(+ next_turn() 推进)。持久化形状不变(一个 blob:
{session_id, _turn_no}),换后端只实现 get_or_create/save/reset 三方法。from_dict 对旧 blob
里已废弃的字段(history/rolling/catalog/_seq)一律忽略 → 升级后旧会话仍可加载(向后兼容)。

持久化默认落独立 SQLite 文件(config.SESSION_DB_PATH;path=None 则纯内存),MCP/AlloyDB 那条路
够不着 → 物理上免疫"潘多拉"。owner:session_id 作用域(见 _scoped)关掉 IDOR。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pipeline import config
from pipeline.redis_client import build_redis_client

log = logging.getLogger("pipeline.session")


@dataclass
class Session:
    session_id: str
    _turn_no: int = field(default=0, init=False)  # 全局单调轮号(不随重启回退;供 transcript 标号)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def next_turn(self) -> int:
        """推进并返回这一轮的轮号(loop 成功一轮、落 transcript 前调一次)。"""
        with self._lock:
            self._turn_no += 1
            return self._turn_no

    # ── 序列化(持久化用;排除 _lock)──────────────────
    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "_turn_no": self._turn_no}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        s = cls(session_id=d["session_id"])
        s._turn_no = int(d.get("_turn_no", 0))
        # 旧 blob 的 history/rolling/catalog/_seq 等字段一律忽略(向后兼容,不报错)。
        return s


def _scoped(owner: str, session_id: str) -> str:
    """会话的"带归属存储 key" = owner:session_id。让每条会话归属到认证身份(app_user):
    别人拿你的 session_id 来,也只会落到【他自己】的命名空间 → 读不到你的(关掉 IDOR)。
    owner 含分隔符 → 哈希兜底,防越界/碰撞。owner 为空 → "anon"(本地无鉴权时全归 anon)。"""
    owner = owner or "anon"
    if ":" in owner:
        owner = "u_" + hashlib.sha256(owner.encode()).hexdigest()[:12]
    return f"{owner}:{session_id}"


# ── 会话仓接口 ──────────────────────────────────────────────
# 持久化与 pipeline 解耦:请求开头 get_or_create(读)、结尾 save(写)。换后端只实现这三个方法。
# owner = 认证身份(API 层传 request.state.app_user);存储按 owner 命名空间隔离(见 _scoped)。
class BaseSessionStore(ABC):
    @abstractmethod
    def get_or_create(self, session_id: str, owner: str = "anon") -> Session: ...
    @abstractmethod
    def save(self, session: Session, owner: str = "anon") -> None: ...
    @abstractmethod
    def reset(self, session_id: str, owner: str = "anon") -> Session: ...


# ── 进程级会话仓(默认落独立 SQLite 文件;path=None 则纯内存)───────────
class SessionStore(BaseSessionStore):
    def __init__(self, path: str | None = None, ttl_seconds: int = 0) -> None:
        self._sessions: dict[str, Session] = {}   # L0 缓存(进程内)
        self._lock = threading.Lock()
        self._path = path or None
        self._ttl = ttl_seconds
        self._ensured = False

    def _ensure(self) -> None:
        if self._ensured or not self._path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self._path)) or ".", exist_ok=True)
        with sqlite3.connect(self._path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS sessions("
                      "session_id TEXT PRIMARY KEY, blob TEXT NOT NULL, updated_at REAL NOT NULL)")
            c.execute("PRAGMA journal_mode=WAL")
        self._ensured = True

    def get_or_create(self, session_id: str, owner: str = "anon") -> Session:
        key = _scoped(owner, session_id)          # 按归属隔离:别人的 sid 落不到你的命名空间
        with self._lock:
            self._sweep_locked()                  # 懒清理:删盘上闲置超 TTL 的会话
            s = self._sessions.get(key)
            if s is None and self._path:
                s = self._load_locked(key)         # 重启后从盘恢复
            if s is None:
                s = Session(session_id=session_id)
            self._sessions[key] = s
            return s

    def save(self, session: Session, owner: str = "anon") -> None:
        """写时机 = 每个请求结束写一次(API/CLI 调用点);纯内存模式无操作。"""
        if not self._path:
            return
        self._ensure()
        key = _scoped(owner, session.session_id)
        blob = json.dumps(session.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            with sqlite3.connect(self._path) as c:
                c.execute(
                    "INSERT INTO sessions(session_id, blob, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
                    (key, blob, time.time()))

    def reset(self, session_id: str, owner: str = "anon") -> Session:
        key = _scoped(owner, session_id)
        with self._lock:
            s = Session(session_id=session_id)
            self._sessions[key] = s
            if self._path:
                self._ensure()
                with sqlite3.connect(self._path) as c:
                    c.execute("DELETE FROM sessions WHERE session_id=?", (key,))
            return s

    # ── 内部(均在 self._lock 内调用)──────────────────
    def _load_locked(self, key: str) -> Session | None:
        self._ensure()
        try:
            with sqlite3.connect(self._path) as c:
                row = c.execute("SELECT blob FROM sessions WHERE session_id=?",
                                (key,)).fetchone()
            if row:
                return Session.from_dict(json.loads(row[0]))
        except Exception:
            pass
        return None

    def _sweep_locked(self) -> None:
        if not self._path or self._ttl <= 0:
            return
        self._ensure()
        try:
            with sqlite3.connect(self._path) as c:
                c.execute("DELETE FROM sessions WHERE updated_at < ?", (time.time() - self._ttl,))
        except Exception:
            pass


# ── Redis 会话仓(共享外部存储:多实例/Cloud Run 跨副本续聊)──────────
class RedisSessionStore(BaseSessionStore):
    """会话以 `key_prefix+session_id → JSON blob` 存进 Redis(一次 GET / 一次 SET)。
    刻意【不留进程内缓存】:Redis 是唯一真相源,每请求开头都重新读。TTL 交给 Redis(`SET ... EX`)。
    Redis 读写异常一律 fail-open(退化为新会话/跳过写)。并发同会话由 API 层每会话一把锁串行化
    (见 api/server.py:_session_lock);跨副本并发同会话靠 session affinity。"""

    def __init__(self, url: str | None = None, *, ttl_seconds: int = 0,
                 client: Any = None, key_prefix: str = "vs:session:") -> None:
        if client is not None:                     # 测试可注入 fakeredis,免依赖真 Redis
            self._r = client
        else:
            if not url:
                raise ValueError("RedisSessionStore 需要 REDIS_URL(或注入 client)")
            import redis                            # 惰性导入:只有真用 redis 后端才需要装
            self._r = redis.from_url(url, decode_responses=True)
        self._ttl = ttl_seconds
        self._prefix = key_prefix

    def _key(self, session_id: str, owner: str = "anon") -> str:
        return f"{self._prefix}{_scoped(owner, session_id)}"

    def get_or_create(self, session_id: str, owner: str = "anon") -> Session:
        try:
            blob = self._r.get(self._key(session_id, owner))
        except Exception as e:
            log.warning("redis get 失败(fail-open,退化为新会话): %r", e)
            blob = None
        if blob:
            try:
                return Session.from_dict(json.loads(blob))
            except Exception as e:
                log.warning("会话反序列化失败(退化为新会话): %r", e)
        return Session(session_id=session_id)

    def save(self, session: Session, owner: str = "anon") -> None:
        blob = json.dumps(session.to_dict(), ensure_ascii=False, default=str)
        key = self._key(session.session_id, owner)
        try:
            if self._ttl and self._ttl > 0:
                self._r.set(key, blob, ex=self._ttl)
            else:
                self._r.set(key, blob)
        except Exception as e:
            log.warning("redis save 失败(fail-open,本轮记忆未落盘): %r", e)

    def reset(self, session_id: str, owner: str = "anon") -> Session:
        try:
            self._r.delete(self._key(session_id, owner))
        except Exception as e:
            log.warning("redis delete 失败(fail-open): %r", e)
        return Session(session_id=session_id)


# ── 后端工厂:按 SESSION_BACKEND 选;默认 sqlite,本地零改动 ────────────
def _build_redis_client() -> Any:
    """建 Redis 客户端 —— 实现已抽到 pipeline.redis_client(与 transcript 存储共享,避免循环引用)。"""
    return build_redis_client()


def _make_store() -> BaseSessionStore:
    if config.SESSION_BACKEND == "redis":
        return RedisSessionStore(client=_build_redis_client(),
                                 ttl_seconds=config.SESSION_TTL_SECONDS)
    return SessionStore(config.SESSION_DB_PATH or None, ttl_seconds=config.SESSION_TTL_SECONDS)


STORE: BaseSessionStore = _make_store()
