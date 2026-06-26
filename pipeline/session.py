"""
会话记忆 + 跨轮 artifact 存储 —— 多轮对话的"上下文构建"层(方向2)。

按 session_id 持有三块:
    history  —— 最近 ≤MAX_TURNS 轮的 Turn 记录(问题/意图/状态/答案摘要/产出&指代的 id)
    rolling  —— 被挤出 history 的更老轮(确定性"滚动摘要":整条留存、不调 LLM、不半截截断)
    catalog  —— 可被后续轮"指代"的产物句柄(Artifact):
                 · recipe(上一轮 SQL 或整张 DAG):复用策略=重算,followup 拿配方重建自洽 DAG。
                 · preview(封顶采样)+ 真实行数 n。【绝不存完整结果值。】

视图非对称(控制 prompt 体积的核心):
    catalog_view()    给 Router  —— 只最近 CATALOG_VIEW_MAX 条、newest-first、【不含 recipe】
    history_view()    更老轮走 terse,最近 HISTORY_VIEW_TURNS 走 full —— 拼成连续时间线
    planner_context() 给 Planner —— 只把【已解析】的少数 artifact 连 recipe 一起给

持久化:STORE 默认落到一个【独立 SQLite 文件】(config.SESSION_DB_PATH),单 blob 表
{session_id, blob, updated_at}。该文件只有 SessionStore 打开,MCP/AlloyDB 那条路够不着
→ 从物理上免疫"潘多拉"(planner 的 SQL 无法读到会话记忆)。path=None 则纯内存(测试用)。
全是内部 dataclass(非 LLM 解析),不用 pydantic;接口(get_or_create/save/reset)可换后端。
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
from dataclasses import asdict, dataclass, field
from typing import Any

from pipeline import config
from pipeline.artifact_value_store import (BaseArtifactValueStore, make_key)
from pipeline.dag_schema import DAG, DATA_TOOLS
from pipeline.redis_client import build_redis_client

log = logging.getLogger("pipeline.session")

# ── 容量与封顶常量(防 prompt / 内存膨胀)──────────────
MAX_TURNS = 12             # history 保留最近 N 轮
ROLLING_MAX = 20           # rolling 保留最近 N 条被淘汰轮(整条淘汰)
MAX_ARTIFACTS = 20         # catalog 保留最近 M 个 artifact
HISTORY_VIEW_TURNS = 6     # 喂模型时走 full 的最近轮数(更老走 terse)
CATALOG_VIEW_MAX = 10      # 给 Router 的货架视图只放最近 N 条(降多干扰项;存储仍 MAX_ARTIFACTS)
TERSE_Q = 60               # 滚动摘要里 question 的截断
PREVIEW_ROWS = 5           # 预览最多行
PREVIEW_COLS = 8           # 预览每行最多列
PREVIEW_CELL = 80          # 预览每格字符上限
LABEL_MAX = 120            # label 截断
QUESTION_MAX = 240         # history 里 question 截断
SUMMARY_MAX = 200          # answer_summary 截断
RECIPE_DAG_MAX = 4096      # DAG 配方序列化超过此值 → 退化为工具链摘要


def _truncate(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _cap_preview(value: Any) -> tuple[list[dict], int]:
    """把任意结果值压成 ≤PREVIEW_ROWS 行 × ≤PREVIEW_COLS 列 × 每格≤PREVIEW_CELL 字的预览。
    返回 (preview_rows, true_n)。完整值【不】留存。"""
    if value is None:
        return ([], 0)
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = [value]
    else:  # 标量 / 字符串
        return ([{"value": _truncate(value, PREVIEW_CELL)}], 1)

    n = len(rows)
    out: list[dict] = []
    for row in rows[:PREVIEW_ROWS]:
        if isinstance(row, dict):
            capped: dict = {}
            for i, (k, v) in enumerate(row.items()):
                if i >= PREVIEW_COLS:
                    break
                capped[str(k)] = _truncate(v, PREVIEW_CELL)
            out.append(capped)
        else:
            out.append({"value": _truncate(row, PREVIEW_CELL)})
    return (out, n)


def _make_label(question: str, intent: str) -> str:
    q = " ".join(str(question).split())
    label = f"[{intent}] {q}" if intent and intent != "other" else q
    return _truncate(label, LABEL_MAX)


def _infer_kind(final_tool: str, final_value: Any) -> str:
    """resolver 不靠 kind(靠 label+preview),这里只给个粗分类便于展示。"""
    if final_tool == "plot":
        return "plot"
    if final_tool == "ols_regress":
        return "scalar"
    if isinstance(final_value, list):
        return "table"
    if isinstance(final_value, dict):
        return "scalar"
    return "other"


def _is_reusable(final_tool: str) -> bool:
    """值复用启发式:哪些 artifact 值得把真实值存进值仓供下一轮直接载入。
    纯数据获取类(sql_query / threshold_sweep / load_artifact)→ 重算便宜且要的是最新数据,
    仍走"重算"不存值;其余(沙箱算出的:ols_regress / merge_asof / interpolate / python /
    load_sensor_csv / plot 等,重算昂贵或非确定)→ 存值,允许下一轮 load_artifact 复用。
    默认仍是重算;存值只是把"复用"这个选项摆上桌,由 planner 显式选取。

    【关于"上游有 sql_query 也照存"的取舍】:本启发式只看【最终工具】,不因 DAG 上游含
    sql_query 就一律不存。这是有意为之 —— 本应用几乎每条 DAG 都从 sql_query 取数,若上游
    见 sql 就禁存,等于废掉整个值复用特性。前提假设:同一会话内 video-facts 业务库实际是
    【静态】的,故复用一份当轮算出的值是安全的;且值复用的目标是省掉【昂贵的沙箱计算】,
    而非省查库。对"要最新/变了数据"的诉求,防线在规划层:planner 被明确引导为仅对
    【重新呈现/重渲染同一份刚算出的结果】用 load_artifact,任何数据/筛选/范围/时间的变化
    都改走配方重算(见 planner.py 的复用指引)。"""
    return final_tool not in DATA_TOOLS


def _derive_recipe(dag: DAG) -> dict:
    """复用策略=重算:把这一轮的 DAG 压成一份"配方"供下一轮重建。
    只两种形:单 sql_query 节点 → {type:sql};其余一律 {type:dag}(过大则退化截断)。"""
    if len(dag.nodes) == 1 and dag.nodes[0].tool == "sql_query":
        return {"type": "sql", "sql": dag.nodes[0].inputs.get("sql", "")}

    blob = dag.model_dump()
    if len(json.dumps(blob, ensure_ascii=False, default=str)) <= RECIPE_DAG_MAX:
        return {"type": "dag", "dag": blob}

    # 退化(安全阀,仍归为 dag 形):工具链摘要 + 数据节点 SQL
    chain = " → ".join(f"{n.id}:{n.tool}" for n in dag.topo_order())
    sqls = {n.id: n.inputs.get("sql") for n in dag.nodes
            if n.tool == "sql_query" and n.inputs.get("sql")}
    return {"type": "dag", "truncated": True, "chain": chain, "sqls": sqls}


def _evict(lst: list, maxlen: int) -> None:
    if len(lst) > maxlen:
        del lst[: len(lst) - maxlen]


def _summarize(answer: Any) -> str:
    if answer is None:
        return ""
    if isinstance(answer, str):
        return _truncate(answer, SUMMARY_MAX)
    try:
        return _truncate(json.dumps(answer, ensure_ascii=False, default=str), SUMMARY_MAX)
    except Exception:
        return _truncate(answer, SUMMARY_MAX)


def _turn_view(t: "Turn", *, terse: bool) -> dict:
    """喂模型的一条历史投影。terse(更老/已淘汰):question 截短、answer_summary 留空;
    full(最近):完整。两形【同 key】,消费端(router/planner)无需分支。
    'produced'/'used' 是确定性 id 指针(本轮产出/指代),滚动摘要靠它保住 a1/a2 不丢。"""
    return {
        "turn": t.turn,
        "question": _truncate(t.question, TERSE_Q if terse else QUESTION_MAX),
        "turn_type": t.turn_type, "intent": t.intent, "status": t.status,
        "answer_summary": "" if terse else t.answer_summary,
        "produced": list(t.artifact_ids),
        "used": list(t.referenced_artifact_ids),
    }


# ── 数据结构 ─────────────────────────────────────────────
@dataclass
class Artifact:
    id: str                       # "a1" 递增(会话内唯一)
    turn: int                     # 产出它的轮次(1-based,全局单调)
    kind: str                     # table | scalar | plot | other
    label: str                    # 人话描述(问题 + intent),resolver 主要靠它
    preview: list = field(default_factory=list)   # 封顶预览(≤5×8,每格≤80)
    n: int = 0                    # 真实行数(预览是采样)
    recipe: dict = field(default_factory=dict)    # {type:sql|dag} —— 只给 Planner
    artifact_ref: str | None = None               # 已存图 url/文件名(仅 plot)
    # 跨轮【值复用】:完整结果值【不】进 session blob,只在【独立值仓】里存一份;
    # 这里只留指针/标志(has_value=True 时 value_key 指向值仓里的那条)。
    has_value: bool = False       # 值仓里是否存了这个 artifact 的真实值(可被 load_artifact 复用)
    value_key: str | None = None  # 值仓主键(session_id::artifact_id);has_value 时才有意义


@dataclass
class Turn:
    turn: int
    question: str
    turn_type: str = "new"
    intent: str = "other"
    status: str = "ok"            # ok | refused | error | smalltalk
    answer_summary: str = ""
    artifact_ids: list = field(default_factory=list)              # 本轮产出的 artifact id
    referenced_artifact_ids: list = field(default_factory=list)   # 本轮指代/复用的 id(冻结指代)


@dataclass
class Session:
    session_id: str
    history: list = field(default_factory=list)   # list[Turn] 最近 ≤MAX_TURNS
    catalog: list = field(default_factory=list)   # list[Artifact]
    rolling: list = field(default_factory=list)   # list[Turn] 被淘汰的更老轮(滚动摘要)
    _seq: int = field(default=0, init=False)      # artifact id 计数
    _turn_no: int = field(default=0, init=False)  # 全局单调轮号(不随淘汰回退)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    # ── 写入 ─────────────────────────────────────────
    def register_artifact(self, dag: DAG, node_values: dict[str, Any],
                          question: str, intent: str,
                          artifact_ref: str | None = None,
                          value_store: BaseArtifactValueStore | None = None) -> Artifact:
        """成功轮把结果登记为可指代的 artifact。【只在 status==ok 时调,且在 record_turn 之前。】
        node_values: {node_id: 该节点的 .value}(完整值只在当轮短暂存在,session blob 里只取预览)。

        value_store 给定且本 artifact"可复用"(沙箱算出/外部拉取类,见 _is_reusable)时,
        把【最终节点的真实值】另存进【独立值仓】(超封顶则自动跳过),并在 Artifact 上只留
        has_value/value_key 指针 —— 完整值绝不进 session blob,免膨胀、守"潘多拉"隔离。
        默认仍是重算:不传 value_store 行为与从前完全一致。"""
        with self._lock:
            order = dag.topo_order()
            final = order[-1]
            # 预览来源:plot-final 取最后一个非 plot 节点(plot 的 value 只有 {n_points})
            preview_node = final
            if final.tool == "plot":
                for nd in reversed(order):
                    if nd.tool != "plot":
                        preview_node = nd
                        break
            preview, n = _cap_preview(node_values.get(preview_node.id))

            self._seq += 1
            aid = f"a{self._seq}"
            art = Artifact(
                id=aid,
                turn=self._turn_no + 1,               # 即将记录的这轮(与紧接的 record_turn 同号)
                kind=_infer_kind(final.tool, node_values.get(final.id)),
                label=_make_label(question, intent),
                preview=preview, n=n,
                recipe=_derive_recipe(dag),
                artifact_ref=artifact_ref,
            )

            # 值复用(重算之外的补充):仅"可复用"类才尝试存值;存成功才置 has_value。
            # 存的是 preview_node(预览所依据的【同一】节点)的真实值,而非 final 节点:
            # plot-final 的 final.value 只有 {n_points},毫无复用价值;preview_node 在
            # plot-final 时指向上游的 x/y 数据节点,正是下一轮要 re-plot/变换的那份数据。
            # 非 plot DAG 里 preview_node == final,故 ols/python 等行为不变。
            if value_store is not None and _is_reusable(final.tool):
                key = make_key(self.session_id, aid)
                if value_store.put(key, node_values.get(preview_node.id)):
                    art.has_value = True
                    art.value_key = key

            self.catalog.append(art)
            _evict(self.catalog, MAX_ARTIFACTS)
            return art

    def record_turn(self, question: str, verdict: Any, status: str,
                    answer: Any, artifact_ids: list | None = None,
                    referenced_ids: list | None = None) -> Turn:
        """每一轮都记(含拒答/失败轮,供 history 与 meta);失败轮不登记 artifact。
        超出 MAX_TURNS 的最老轮【整条转入 rolling】(不硬删),rolling 再按整条封顶。"""
        with self._lock:
            turn_type = getattr(verdict, "turn_type", "new") if verdict else "new"
            intent = getattr(verdict, "intent", "other") if verdict else "other"
            self._turn_no += 1
            t = Turn(
                turn=self._turn_no,
                question=_truncate(question, QUESTION_MAX),
                turn_type=turn_type, intent=intent, status=status,
                answer_summary=_summarize(answer),
                artifact_ids=list(artifact_ids or []),
                referenced_artifact_ids=list(referenced_ids or []),
            )
            self.history.append(t)
            while len(self.history) > MAX_TURNS:      # 淘汰边界:整条转存,不硬删
                self.rolling.append(self.history.pop(0))
            _evict(self.rolling, ROLLING_MAX)
            return t

    # ── 读取 / 视图 ──────────────────────────────────
    def catalog_view(self) -> list[dict]:
        """给 Router:只放最近 CATALOG_VIEW_MAX 条、newest-first、【不含 recipe】。
        存储仍 MAX_ARTIFACTS;截断视图是为降低'多干扰项'对指代解析的干扰。空会话 → []。"""
        recent = self.catalog[-CATALOG_VIEW_MAX:]
        return [
            {"id": a.id, "turn": a.turn, "kind": a.kind,
             "label": a.label, "preview": a.preview, "n": a.n}
            for a in reversed(recent)
        ]

    def history_view(self) -> list[dict]:
        """更老轮(rolling + history 超窗部分)走 terse,最近 HISTORY_VIEW_TURNS 走 full;
        按轮号连续、单调。"""
        older = self.rolling + self.history[:-HISTORY_VIEW_TURNS]
        recent = self.history[-HISTORY_VIEW_TURNS:]
        return ([_turn_view(t, terse=True) for t in older]
                + [_turn_view(t, terse=False) for t in recent])

    def resolve_references(self, verdict: Any) -> list[str]:
        """用 catalog 真实 id 集合过滤模型给的 resolved_to —— 不信 resolvable 标志,
        只信集合成员。丢弃幻觉 id;保序去重。"""
        valid = {a.id for a in self.catalog}
        out: list[str] = []
        for ref in (getattr(verdict, "references", None) or []):
            rid = ref.get("resolved_to") if isinstance(ref, dict) else getattr(ref, "resolved_to", None)
            if rid in valid and rid not in out:
                out.append(rid)
        return out

    def planner_context(self, resolved_ids: list[str],
                        value_store: BaseArtifactValueStore | None = None) -> dict:
        """给 Planner 的上下文:history + 【仅已解析】artifact(连 recipe),newest-first。

        value_cached 必须反映【当下能否真取到】,不能只看持久化的 has_value 标志:
        has_value/value_key 记的是"我们当时存过",但值活在易失的进程内仓里 —— 重启 / 跨副本 /
        被 LRU 淘汰后值就没了,而 has_value 还是 True。若照搬 has_value,会向 planner 谎称
        value_cached=true,planner 选 load_artifact,该节点取不到值 → 本轮硬失败。
        故这里【实查活仓】:value_cached = has_value 标记过 且 活仓里此键确有值。值不在场时
        不暴露 value_cached,planner 自然改走配方重算 —— 这是把"缺失"消化在规划阶段的核心。
        不传 value_store(如无值复用的纯多轮场景)→ value_cached 一律 False(保守、安全)。"""
        by_id = {a.id: a for a in self.catalog}
        arts = [by_id[i] for i in resolved_ids if i in by_id]
        arts.sort(key=lambda a: a.turn, reverse=True)

        def _live_cached(a: Artifact) -> bool:
            return bool(value_store is not None and a.value_key
                        and value_store.get(a.value_key) is not None)

        return {
            "history": self.history_view(),
            "resolved_artifacts": [
                {"id": a.id, "label": a.label, "kind": a.kind,
                 "preview": a.preview, "recipe": a.recipe, "artifact_ref": a.artifact_ref,
                 # value_cached=true → 活仓里【此刻确有】这条的真实值,planner 可选 load_artifact 复用
                 "value_cached": _live_cached(a)}
                for a in arts
            ],
        }

    def get_artifact(self, aid: str) -> Artifact | None:
        return next((a for a in self.catalog if a.id == aid), None)

    # ── 序列化(持久化用;排除 _lock)──────────────────
    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "_seq": self._seq,
            "_turn_no": self._turn_no,
            "history": [asdict(t) for t in self.history],
            "rolling": [asdict(t) for t in self.rolling],
            "catalog": [asdict(a) for a in self.catalog],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        s = cls(session_id=d["session_id"])
        s._seq = int(d.get("_seq", 0))
        s._turn_no = int(d.get("_turn_no", 0))
        s.history = [Turn(**t) for t in d.get("history", [])]
        s.rolling = [Turn(**t) for t in d.get("rolling", [])]
        s.catalog = [Artifact(**a) for a in d.get("catalog", [])]
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
# 持久化与 pipeline 解耦:请求开头 get_or_create(读)、结尾 save(写),中间流水线只动内存里的
# Session 对象。换后端只实现这三个方法即可,router/planner/orchestrator 一行不改。
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
    """会话以 `key_prefix+session_id → JSON blob` 存进 Redis —— 形状和 SQLite 那版一模一样
    (一次 GET / 一次 SET),只是换成所有副本共享的外部 KV。client 只需暴露 get/set(ex=)/delete,
    redis-py(TCP)与 upstash-redis(REST)都满足,故本类与具体客户端库解耦。

    刻意【不留进程内 L0 缓存】:Redis 是唯一真相源,每请求开头都重新读 —— 否则副本 A 的缓存
    会在副本 B 写入后变脏。TTL 直接交给 Redis(`SET ... EX`),省掉 SQLite 那版的懒清理。
    Redis 读写异常一律 fail-open(退化为新会话/跳过写),不让记忆层拖垮主请求。

    并发:save 是无条件 SET(后写覆盖)。SQLite 版靠 L0 缓存让同进程并发轮共用同一对象、
    经 Session._lock 合并;这里无缓存,故并发同会话轮在【同进程】也会互相覆盖丢轮 ——
    因此 read-modify-write 由 API 层每会话一把锁串行化(见 api/server.py:_session_lock),
    单副本安全。【跨副本】并发同会话仍会后写覆盖:正常多轮是串行的(要等上一轮答案才能追问),
    故部署开 session affinity 即可;要严格跨副本原子,再上 WATCH/MULTI(TCP)或 append-only。
    """

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
    """建 Redis 客户端 —— 实现已抽到 pipeline.redis_client(与 artifact 值仓共享,避免循环引用)。"""
    return build_redis_client()


def _make_store() -> BaseSessionStore:
    if config.SESSION_BACKEND == "redis":
        return RedisSessionStore(client=_build_redis_client(),
                                 ttl_seconds=config.SESSION_TTL_SECONDS)
    return SessionStore(config.SESSION_DB_PATH or None, ttl_seconds=config.SESSION_TTL_SECONDS)


STORE: BaseSessionStore = _make_store()
