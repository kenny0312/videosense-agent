"""
跨轮 artifact【值】仓 —— "重算"之外的补充策略:把上一轮真正算出来的值原样存下,
下一轮可直接载入,免去对昂贵/非确定结果(ols_regress、沙箱算出/外部拉取的数据)的重跑。

与 session 仓【物理隔离】(严守"潘多拉"):
    本仓只存"结果值",和 planner 的 SQL 所查的业务库(MCP get_schema/query_db)不在一处 ——
    planner 写的 SQL 永远够不着这里,故值仓里的内容绝不会污染数据获取层。
    默认纯内存(进程级 LRU);接口刻意做小(put/get/delete),日后换 GCS/Redis 只实现这几个方法,
    orchestrator / session / node_executor 一行不改(完全对照 session 仓的 BaseSessionStore 模式)。

封顶:序列化超 ~MAX_VALUE_BYTES 的值【跳过不存】(put 返回 False);进程内仓再叠一层 LRU
条数封顶(MAX_VALUE_ENTRIES),超出淘汰最旧 —— 值仓是优化,不是必需,宁可不存,也不让
大对象/无界增长撑爆内存。

【取不到 ≠ 自动重算】:本仓不提供"miss 自动回退重算"。真正的防线在【规划阶段】——
session.planner_context 只在【活仓里确有此值】时才把该 artifact 标 value_cached,planner
据此只对【值确实在场】的 artifact 发 load_artifact;值不在(重启/跨副本/LRU 淘汰)时不暴露
value_cached,planner 自然改走配方重算。万一仍发了 load_artifact 却取不到值,该节点失败 →
orchestrator 让【本轮失败】(不自动改规划重试)。
"""
from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

log = logging.getLogger("pipeline.artifact_value_store")

# 单个值序列化(UTF-8 JSON)的字节上限;超出则跳过存储(下一轮回退重算)。
MAX_VALUE_BYTES = 256 * 1024   # ~256KB

# 进程内值仓的条目数上限(LRU 淘汰最旧);防内存无界增长。
MAX_VALUE_ENTRIES = 256


def make_key(session_id: str, artifact_id: str) -> str:
    """值仓主键 = 会话 + artifact;两者一起才唯一(artifact id 仅在会话内唯一)。"""
    return f"{session_id}::{artifact_id}"


def _serialized_size(value: Any) -> int | None:
    """返回值按 UTF-8 JSON 序列化的字节数;无法序列化 → None(不可存)。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return None


# ── 值仓接口 ────────────────────────────────────────────────
# 接口刻意只三个方法(put/get/delete),换后端(GCS/Redis)只实现它们即可。
class BaseArtifactValueStore(ABC):
    @abstractmethod
    def put(self, key: str, value: Any) -> bool:
        """存值。成功(在封顶内且可序列化)返回 True;超封顶/不可序列化 → 跳过并返回 False。"""
        ...

    @abstractmethod
    def get(self, key: str) -> Any:
        """取值;不存在返回 None。

        取不到(从未存过/已被 LRU 淘汰/换了副本或重启后内存已清空)时返回 None —— 这是
        正常路径:planner_context 只在【活仓里确有此值】时才向 planner 暴露 value_cached,
        故 planner 不会对缺失的值发 load_artifact。万一仍发了(陈旧上下文等),load_artifact
        节点会失败,从而【让本轮失败】—— 本仓【不】自动重算回退,该回退由"不暴露 value_cached"
        在规划阶段就避免。"""
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """删除一条值(不存在则无操作);为接口完整性提供。"""
        ...


# ── 进程内值仓(默认实现)────────────────────────────────────
class InMemoryArtifactValueStore(BaseArtifactValueStore):
    """进程内【LRU】仓 —— 默认实现。单副本/本地够用;多副本续聊换 Redis/GCS 实现同接口即可。

    用 OrderedDict 做简易 LRU:get/put 都 move_to_end 标为"最近用",超过 max_entries 条则
    淘汰最旧条(popitem(last=False))—— 防进程内存随会话无界增长。

    注意:进程内意味着重启即丢、跨副本不共享、可能被 LRU 淘汰 —— 这与"值复用是优化、
    取不到就在规划阶段不暴露 value_cached、从而走重算"的定位一致,不影响正确性。
    换 GCS/Redis 后端可获持久化/跨副本共享。
    """

    def __init__(self, max_bytes: int = MAX_VALUE_BYTES,
                 max_entries: int = MAX_VALUE_ENTRIES) -> None:
        self._d: "OrderedDict[str, Any]" = OrderedDict()
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def put(self, key: str, value: Any) -> bool:
        size = _serialized_size(value)
        if size is None:
            log.warning("artifact 值不可序列化,跳过存储: key=%s", key)
            return False
        if size > self._max_bytes:
            log.info("artifact 值超封顶(%d > %d),跳过存储(下一轮回退重算): key=%s",
                     size, self._max_bytes, key)
            return False
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)                       # 标为最近用
            while len(self._d) > self._max_entries:         # 超条数封顶 → 淘汰最旧
                evicted, _ = self._d.popitem(last=False)
                log.info("值仓超条数封顶,LRU 淘汰最旧条: key=%s", evicted)
        return True

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._d:
                return None
            self._d.move_to_end(key)                        # 命中即标为最近用
            return self._d[key]

    def delete(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)


# 进程级默认值仓(对照 session.py 的 STORE)。换后端:替换这一处的实例即可。
VALUE_STORE: BaseArtifactValueStore = InMemoryArtifactValueStore()
