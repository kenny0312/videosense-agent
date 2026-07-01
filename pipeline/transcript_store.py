"""M4(DAG→loop):append-only transcript 存储层(CC 式记忆;尚未接入 loop,M5 再用)。

三层(决策③ = GCS-JSONL + Redis 热尾):
  - 热尾  : Redis LIST(RPUSH + LTRIM 到 HOT_WINDOW)—— 喂 prompt 的近窗,低延迟
  - 耐久  : GCS,一事件一对象 transcripts/{owner}/{sid}/{seq}.json —— 全保真真相
  - 溢出  : 大/二进制 tool_result 本体 → GCS tool-results/{owner}/{sid}/{event_id}.json,
           transcript 行里只留 result_ref 指针 + 预览
全部 owner:session_id 作用域;【不进业务库】(潘多拉)。路由是【确定性代码,非模型】,按 type+size。
默认 InMemory(本地/测试);SESSION_BACKEND=redis 时用 Redis+GCS(复用现有凭据,全程 fail-open)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Callable

from pipeline import config

log = logging.getLogger("pipeline.transcript_store")

OVERFLOW_BYTES = int(os.environ.get("TRANSCRIPT_OVERFLOW_BYTES", "8192"))   # 轴②:超此即溢出
HOT_WINDOW     = int(os.environ.get("TRANSCRIPT_HOT_WINDOW", "500"))        # 热尾保留条数(够 ~60-100 轮走快路)


def _scoped(owner: str, session_id: str) -> str:
    """owner:session_id 作用域键。owner 含 ':' → 哈希兜底防注入;空 → anon。"""
    owner = owner or "anon"
    if ":" in owner:
        owner = "u_" + hashlib.sha256(owner.encode()).hexdigest()[:12]
    return f"{owner}:{session_id}"


def _size(v: Any) -> int:
    try:
        return len(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:
        return OVERFLOW_BYTES + 1


def _json_safe(v: Any) -> bool:
    try:
        json.dumps(v, ensure_ascii=False)
        return True
    except Exception:
        return False


def _preview(value: Any, rows: int = 3, cols: int = 8, cell: int = 80):
    def cap(s):
        s = str(s)
        return s if len(s) <= cell else s[:cell - 1] + "…"
    if isinstance(value, list):
        out = [({k: cap(v) for k, v in list(r.items())[:cols]} if isinstance(r, dict)
                else {"value": cap(r)}) for r in value[:rows]]
        return out, len(value)
    if isinstance(value, dict):
        return [{k: cap(v) for k, v in list(value.items())[:cols]}], 1
    return [{"value": cap(value)}], 1


# ── 存储后端 ──────────────────────────────────
class BaseTranscriptStore(ABC):
    @abstractmethod
    def append(self, key: str, line: dict) -> None: ...
    @abstractmethod
    def tail(self, key: str, n: int) -> list[dict]: ...


class InMemoryTranscriptStore(BaseTranscriptStore):
    """本地/测试:进程内有序列表(无界,够测试用)。"""
    def __init__(self):
        self._d: dict[str, list[dict]] = defaultdict(list)

    def append(self, key, line):
        self._d[key].append(line)

    def tail(self, key, n):
        return list(self._d[key][-n:])

    def all(self, key):
        return list(self._d[key])


class RedisGcsTranscriptStore(BaseTranscriptStore):
    """生产:热尾=Redis LIST(RPUSH+LTRIM),耐久=GCS 一事件一对象。两者 fail-open。"""
    def __init__(self, hot_window: int = HOT_WINDOW, ttl: int | None = None):
        from pipeline.redis_client import build_redis_client
        self._r = build_redis_client()
        self._hot = hot_window
        self._ttl = ttl if ttl is not None else getattr(config, "SESSION_TTL_SECONDS", 86400)

    # ── 耐久层对象名的序号(U4-D2 修复)────────────────────────────
    # 旧实现是【进程内】计数器:实例重启/扩副本后从 1 重数 → 同名覆盖最早的 GCS 事件,
    # 耐久历史被静默腐蚀。改为 Redis INCR(跨进程/重启单调);该 key【刻意不设 TTL】——
    # 它只是个小整数,过期反而会让序号回卷再次覆盖。Redis 不可用时退化为时间戳命名
    # ('t'+纳秒,字典序排在 9 位数字之后)→ 顺序仍近似正确,且【任何情况下绝不复用旧名】。
    def _next_seq_name(self, key: str) -> str:
        try:
            seq = int(self._r.incr(f"vs:txseq:{key}"))
            if seq == 1:                              # 计数器是新的,但会话可能已有旧命名的 GCS 历史
                existing = self._gcs_count(key)       #   (老代码写下的)→ 对齐到其后,防首写覆盖
                if existing:
                    seq = existing + 1
                    self._r.set(f"vs:txseq:{key}", seq)
            return f"{seq:09d}"
        except Exception:
            return f"t{time.time_ns()}"

    def _gcs_count(self, key: str) -> int:
        try:
            from google.cloud import storage
            bkt = storage.Client(project=config.GCP_PROJECT).bucket(config.GCS_BUCKET)
            return sum(1 for _ in bkt.list_blobs(prefix=f"transcripts/{key.replace(':', '/')}/"))
        except Exception:
            return 0

    def append(self, key, line):
        blob = json.dumps(line, ensure_ascii=False, default=str)
        try:                                          # 热尾
            self._r.rpush(f"vs:tx:{key}", blob)
            self._r.ltrim(f"vs:tx:{key}", -self._hot, -1)
            self._r.expire(f"vs:tx:{key}", self._ttl)
        except Exception as e:
            log.warning("transcript 热尾 append 失败(fail-open): %r", e)
        try:                                          # 耐久(best-effort,一事件一对象)
            from google.cloud import storage
            bkt = storage.Client(project=config.GCP_PROJECT).bucket(config.GCS_BUCKET)
            path = f"transcripts/{key.replace(':', '/')}/{self._next_seq_name(key)}.json"
            bkt.blob(path).upload_from_string(blob, content_type="application/json")
        except Exception as e:
            log.warning("transcript 耐久 GCS 失败(fail-open): %r", e)

    def tail(self, key, n):
        try:
            rows = self._r.lrange(f"vs:tx:{key}", -n, -1) or []
            events = [json.loads(x) for x in rows]
        except Exception as e:
            log.warning("transcript 热尾 tail 失败(fail-open): %r", e)
            events = []
        # CC 式全量注入的前提:热尾【不足】时从 GCS 全量回读(耐久真相)。但只在【确实可能不足】时
        # 才碰 GCS,别让短会话每轮都白跑一趟:lrange 至多返回 HOT_WINDOW 条,故
        #   · 0 < len < HOT_WINDOW → Redis 里就是【全量】(从没被 LTRIM 过)→ 不读 GCS;
        #   · len == 0(过 TTL/空)或 len 顶到 HOT_WINDOW(被 LTRIM,更老的只在 GCS)→ 回读 GCS。
        if (not events or len(events) >= self._hot) and len(events) < n:
            gcs = self._tail_from_gcs(key, n)
            if len(gcs) > len(events):
                events = gcs
        return events

    def _tail_from_gcs(self, key, n):
        """从 GCS 一事件一对象回读最近 n 条(seq 零填充 → 名字字典序即时间序)。best-effort。"""
        try:
            from google.cloud import storage
            prefix = f"transcripts/{key.replace(':', '/')}/"
            bkt = storage.Client(project=config.GCP_PROJECT).bucket(config.GCS_BUCKET)
            blobs = sorted(bkt.list_blobs(prefix=prefix), key=lambda b: b.name)[-n:]
            out = []
            for b in blobs:
                try:
                    out.append(json.loads(b.download_as_text()))
                except Exception:
                    pass
            return out
        except Exception as e:
            log.warning("transcript GCS 回读失败(fail-open): %r", e)
            return []


def gcs_blob_put(owner: str, session_id: str, event_id: str, value: Any) -> str:
    """大本体溢出到 GCS tool-results,返回 gs:// 指针。"""
    from google.cloud import storage
    name = f"tool-results/{_scoped(owner, session_id).replace(':', '/')}/{event_id}.json"
    bkt = storage.Client(project=config.GCP_PROJECT).bucket(config.GCS_BUCKET)
    bkt.blob(name).upload_from_string(
        json.dumps(value, ensure_ascii=False, default=str), content_type="application/json")
    return f"gs://{config.GCS_BUCKET}/{name}"


# ── 确定性写入器(非模型;按 type+size 路由)──────────────
def append_event(store: BaseTranscriptStore, owner: str, session_id: str, event: dict, *,
                 blob_put: Callable | None = None, overflow_bytes: int = OVERFLOW_BYTES) -> dict:
    """把一个事件落盘:tool_result 的大/非 JSON 本体 → 溢出到 blob_put,行里只留 result_ref+预览。
    返回最终落盘的 line(供调用方拿 result_ref/preview)。"""
    line = dict(event)
    if line.get("type") == "tool_result":
        val = line.get("value")
        if val is not None and (_size(val) > overflow_bytes or not _json_safe(val)):
            if blob_put is not None:
                line["result_ref"] = blob_put(owner, session_id, line.get("event_id") or "evt", val)
            line["preview"], line["n"] = _preview(val)
            line.pop("value", None)               # 完整本体绝不进 transcript 行
    store.append(_scoped(owner, session_id), line)
    return line


def make_transcript_store() -> BaseTranscriptStore:
    """工厂:SESSION_BACKEND=redis → Redis+GCS(凭据缺失则退内存);否则 InMemory。"""
    if config.SESSION_BACKEND == "redis":
        try:
            return RedisGcsTranscriptStore()
        except Exception as e:
            log.warning("transcript store 退回内存(redis 不可用): %r", e)
    return InMemoryTranscriptStore()


# 模块级单例:orchestrator 在 loop 路径用它记录/回放 transcript(唯一记忆)。
STORE: BaseTranscriptStore = make_transcript_store()
