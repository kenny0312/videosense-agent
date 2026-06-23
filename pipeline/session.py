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

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pipeline import config
from pipeline.dag_schema import DAG

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
                          artifact_ref: str | None = None) -> Artifact:
        """成功轮把结果登记为可指代的 artifact。【只在 status==ok 时调,且在 record_turn 之前。】
        node_values: {node_id: 该节点的 .value}(完整值只在当轮短暂存在,这里只取预览)。"""
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
            art = Artifact(
                id=f"a{self._seq}",
                turn=self._turn_no + 1,               # 即将记录的这轮(与紧接的 record_turn 同号)
                kind=_infer_kind(final.tool, node_values.get(final.id)),
                label=_make_label(question, intent),
                preview=preview, n=n,
                recipe=_derive_recipe(dag),
                artifact_ref=artifact_ref,
            )
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

    def planner_context(self, resolved_ids: list[str]) -> dict:
        """给 Planner 的上下文:history + 【仅已解析】artifact(连 recipe),newest-first。"""
        by_id = {a.id: a for a in self.catalog}
        arts = [by_id[i] for i in resolved_ids if i in by_id]
        arts.sort(key=lambda a: a.turn, reverse=True)
        return {
            "history": self.history_view(),
            "resolved_artifacts": [
                {"id": a.id, "label": a.label, "kind": a.kind,
                 "preview": a.preview, "recipe": a.recipe, "artifact_ref": a.artifact_ref}
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


# ── 进程级会话仓(默认落独立 SQLite 文件;path=None 则纯内存)───────────
class SessionStore:
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

    def get_or_create(self, session_id: str) -> Session:
        with self._lock:
            self._sweep_locked()                  # 懒清理:删盘上闲置超 TTL 的会话
            s = self._sessions.get(session_id)
            if s is None and self._path:
                s = self._load_locked(session_id)  # 重启后从盘恢复
            if s is None:
                s = Session(session_id=session_id)
            self._sessions[session_id] = s
            return s

    def save(self, session: Session) -> None:
        """写时机 = 每个请求结束写一次(API/CLI 调用点);纯内存模式无操作。"""
        if not self._path:
            return
        self._ensure()
        blob = json.dumps(session.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            with sqlite3.connect(self._path) as c:
                c.execute(
                    "INSERT INTO sessions(session_id, blob, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET blob=excluded.blob, updated_at=excluded.updated_at",
                    (session.session_id, blob, time.time()))

    def reset(self, session_id: str) -> Session:
        with self._lock:
            s = Session(session_id=session_id)
            self._sessions[session_id] = s
            if self._path:
                self._ensure()
                with sqlite3.connect(self._path) as c:
                    c.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            return s

    # ── 内部(均在 self._lock 内调用)──────────────────
    def _load_locked(self, session_id: str) -> Session | None:
        self._ensure()
        try:
            with sqlite3.connect(self._path) as c:
                row = c.execute("SELECT blob FROM sessions WHERE session_id=?",
                                (session_id,)).fetchone()
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


STORE = SessionStore(config.SESSION_DB_PATH or None, ttl_seconds=config.SESSION_TTL_SECONDS)
