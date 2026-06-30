"""
单节点执行器 —— DAG 里一个节点的执行单元。

路由:
    数据获取类(sql_query / threshold_sweep)
        → 主进程经 MCP 执行(持有凭证、可信),不进沙箱
    数据科学类(merge_asof / interpolate / ols_regress / plot / ...)
        → Code Generator 生成 Python → 注入上游数据 → Stage 5 沙箱执行
          → 失败把 stderr 回喂重写(Stage 6 自愈),最多 CODE_MAX_RETRIES 次

自愈作用在**单个节点**上:n3 失败只重试 n3,上游 n1/n2 的结果不丢。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pipeline import mcp_client
from pipeline.code_generator import CodeGenerator
from pipeline.sql_fixer import SqlFixer
from pipeline.dag_schema import Node
from pipeline.node_specs import needs_sandbox
from pipeline.trace import Trace
from sandbox.client import SandboxClient

log = logging.getLogger("pipeline.node_executor")

CODE_MAX_RETRIES = 3   # 沙箱节点:首发 + 至多 3 次自愈
SQL_MAX_RETRIES = 2    # sql_query 节点:首发 + 至多 2 次自愈(对称沙箱节点)


@dataclass
class NodeResult:
    node_id: str
    tool: str
    ok: bool
    value: Any = None              # 解析后的结果(list[dict] / dict)
    code: str = ""                 # 生成的 Python(仅 sandbox 节点)
    attempts: int = 0
    stderr: str = ""
    artifact: dict = field(default_factory=dict)   # 如 plot 的 png_base64
    videos: list = field(default_factory=list)     # show_video 的侧信道:可播放视频描述符
    table: dict = field(default_factory=dict)      # show_table 的侧信道:{columns, rows, n} 原样出表格
    cache_hit: bool = False                        # M4.2:analyze_video 命中缓存(供度量)


# ── 上游数据注入 ──────────────────────────────

def _inject(code: str, node: Node, upstream: dict[str, Any]) -> str:
    header = "import json\n"
    header += f"inputs = json.loads({json.dumps(node.inputs, ensure_ascii=False)!r})\n"
    for nid, val in upstream.items():
        header += f"data_{nid} = json.loads({json.dumps(val, ensure_ascii=False, default=str)!r})\n"
    return header + "\n" + code


def _parse_stdout(stdout: str) -> Any:
    """节点代码约定 print(json.dumps(...));从 stdout 末尾找可解析的 JSON。"""
    s = stdout.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line and line[0] in "[{":
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"_raw_stdout": s}   # 兜底:解析不出 JSON 就原样带回


# ── 数据获取类(MCP)──────────────────────────

def _run_sql_query(node: Node, schema: dict, trace: Trace) -> NodeResult:
    """sql_query 自愈执行(结构对称 _run_sandbox_node):
    查库失败 → 把 DB 报错回喂 SqlFixer 重写 SQL → 重试,至多 SQL_MAX_RETRIES 次。"""
    sql = node.inputs.get("sql", "")
    fixer: SqlFixer | None = None
    last_err = ""

    for attempt in range(SQL_MAX_RETRIES + 1):
        step = trace.step(f"[{node.id}/sql_query] MCP query (try {attempt + 1})")
        try:
            rows = mcp_client.query_db(sql)
            step.ok(rows=len(rows) if isinstance(rows, list) else 1)
            return NodeResult(node.id, node.tool, ok=True, value=rows, attempts=attempt + 1)
        except Exception as e:
            last_err = str(e)
            will_retry = attempt < SQL_MAX_RETRIES
            step.fail(error=last_err[:160], will_retry=will_retry)
            if not will_retry:
                break
            # 自愈:把 DB 报错回喂,重写 SQL
            rstep = trace.step(f"[{node.id}/sql_query] repair (try {attempt + 1})")
            try:
                fixer = fixer or SqlFixer()
                sql = fixer.repair(sql, last_err, schema or {})
                rstep.ok(sql_len=len(sql))
            except Exception as ge:
                rstep.fail(error=repr(ge))
                return NodeResult(node.id, node.tool, ok=False,
                                  stderr=f"sql repair failed: {ge!r}", attempts=attempt + 1)

    return NodeResult(node.id, node.tool, ok=False, stderr=last_err,
                      attempts=SQL_MAX_RETRIES + 1)


_VIDEO_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_\-]+$")


def _collect_items(node: Node, upstream: dict[str, Any]) -> list[dict]:
    """收集要展示的视频:优先用上游第一个依赖的结果行(含 video_id,可选 start_ts/end_ts/label),
    没有上游则用 inputs.video_ids。id 做白名单校验(防注入),去重保序。"""
    items: list[dict] = []
    seen: set[str] = set()

    def add(vid: Any, start=None, end=None, label=None) -> None:
        vid = "" if vid is None else str(vid)
        if not vid or not _VIDEO_ID_RE.match(vid) or vid in seen:
            return
        seen.add(vid)
        items.append({"video_id": vid, "start_ts": start, "end_ts": end, "label": label})

    for val in upstream.values():                 # 只取第一个上游
        if isinstance(val, list):
            for r in val:
                if isinstance(r, dict):
                    add(r.get("video_id") or r.get("id"),
                        r.get("start_ts"), r.get("end_ts"),
                        r.get("label") or r.get("predicate") or r.get("title"))
        break
    if not items:
        for v in (node.inputs.get("video_ids") or []):
            add(v)
    return items


def _run_show_video(node: Node, upstream: dict[str, Any]) -> NodeResult:
    """主进程节点:把要展示的视频签成可播放 URL,放进 NodeResult.videos 侧信道供前端 <video> 播放。
    缺凭证/签不出 → playable=false(fail-open),仍带回标题/片段,前端优雅降级。"""
    from pipeline.video_url import sign_gcs_uri

    items = _collect_items(node, upstream)[:8]     # 最多 8 个,防一次签太多
    if not items:
        return NodeResult(node.id, node.tool, ok=True, attempts=1,
                          value={"shown": 0, "note": "没有可展示的视频(上游无 video_id)"})

    ids = [it["video_id"] for it in items]
    in_list = ", ".join("'" + i + "'" for i in ids)   # ids 已过白名单校验
    try:
        rows = mcp_client.query_db(
            "SELECT video_id, title, gcs_uri, duration_sec FROM video_metadata "
            f"WHERE video_id IN ({in_list})")
    except Exception as e:
        return NodeResult(node.id, node.tool, ok=False, attempts=1,
                          stderr=f"show_video 查 video_metadata 失败: {e!r}")
    meta = {r.get("video_id"): r for r in (rows or [])}

    videos: list[dict] = []
    for it in items:
        m = meta.get(it["video_id"]) or {}
        gcs = m.get("gcs_uri")
        url = sign_gcs_uri(gcs) if gcs else None
        marks = []
        if it.get("start_ts") is not None:
            ts = it["start_ts"]
            lbl = it.get("label") or (f"{ts:.0f}s" if isinstance(ts, (int, float)) else str(ts))
            marks.append({"ts": ts, "label": lbl})
        videos.append({
            "video_id":     it["video_id"],
            "title":        m.get("title") or it["video_id"],
            "gcs_uri":      gcs,
            "signed_url":   url,
            "playable":     bool(url),
            "start_ts":     it.get("start_ts"),
            "end_ts":       it.get("end_ts"),
            "duration_sec": m.get("duration_sec"),
            "marks":        marks,
        })

    n, n_play = len(videos), sum(1 for v in videos if v["playable"])
    note = "" if n == n_play else f"(其中 {n - n_play} 个暂不可播放)"
    # ③:value 带【有序编号 items】→ 随 transcript 持久化(value 会被记忆),下一轮「第 N 个」可映射回真实 id。
    items = [{"n": i + 1, "video_id": v["video_id"], "title": v["title"]}
             for i, v in enumerate(videos)]
    return NodeResult(node.id, node.tool, ok=True, attempts=1, videos=videos,
                      value={"note": f"🎬 为你准备了 {n} 个视频{note}", "items": items})


SHOW_TABLE_MAX_ROWS = 1000


def _run_show_table(node: Node, upstream: dict[str, Any]) -> NodeResult:
    """主进程节点:把【上游查询的完整结果】原样放进 NodeResult.table 侧信道,供前端渲染成表格。
    完整行取自 ledger(非预览)→ 多少行都不丢不编,大脑不必逐行复述。"""
    rows = next((v for v in upstream.values() if isinstance(v, list)), None)
    if rows is None:
        return NodeResult(node.id, node.tool, ok=True, attempts=1,
                          value={"shown": 0, "note": "没有可展示的表格数据(上游结果不是行集)"})
    n = len(rows)
    shown = rows[:SHOW_TABLE_MAX_ROWS]
    norm = [r if isinstance(r, dict) else {"value": r} for r in shown]
    cols: list = []
    for r in norm:                                 # 列名 = 所有行键的并集(保序)
        for k in r:
            if k not in cols:
                cols.append(str(k))
    if not cols:
        cols = ["value"]
    note = "" if n <= SHOW_TABLE_MAX_ROWS else f"(共 {n} 条,展示前 {SHOW_TABLE_MAX_ROWS} 条)"
    caption = node.inputs.get("caption") or ""
    table = {"columns": cols, "rows": norm, "n": n, "shown": len(norm), "caption": str(caption)}
    # ③:value 带前若干条【有序编号 id】(优先 video_id 列,否则首列)→ 进 transcript 供下一轮「第 N 个」映射。
    id_col = "video_id" if "video_id" in cols else (cols[0] if cols else None)
    items = ([{"n": i + 1, "id": str(r.get(id_col, ""))} for i, r in enumerate(norm[:30])]
             if id_col else [])
    return NodeResult(node.id, node.tool, ok=True, attempts=1, table=table,
                      value={"note": f"📋 已为你列出 {n} 条{note}", "items": items})


def _run_analyze_video(node: Node, upstream: dict[str, Any]) -> NodeResult:
    """主进程节点:用多模态模型【现场看一段视频】回答 inputs.question,返回最小信封。
    选定【单个】视频:优先 inputs.video_id,否则取上游结果行里的第一个 video_id;查 video_metadata
    拿 gcs_uri 后调 perception.analyze(失败已在库内 fail-open → enough=no,绝不抛)。"""
    from perception.analyze_video_contextual import (
        AnalyzeRequest, analyze, MODEL_OVERRIDE, PERCEPTION_MODEL, FAILURE_ANSWER_PREFIX)
    from pipeline import analyze_cache

    question = str(node.inputs.get("question") or "").strip()
    if not question:
        return NodeResult(node.id, node.tool, ok=False, attempts=1,
                          stderr="analyze_video 需要 inputs.question")

    vid = node.inputs.get("video_id")
    if not vid:                                       # 兜底:从上游结果行取第一个 video_id
        items = _collect_items(node, upstream)
        vid = items[0]["video_id"] if items else None
    vid = str(vid) if vid else ""
    if not vid or not _VIDEO_ID_RE.match(vid):        # 白名单校验(防注入)
        return NodeResult(node.id, node.tool, ok=False, attempts=1,
                          stderr="analyze_video 需要一个具体 video_id(inputs.video_id 或上游含 video_id)")

    try:
        rows = mcp_client.query_db(
            f"SELECT gcs_uri FROM video_metadata WHERE video_id = '{vid}' LIMIT 1")
    except Exception as e:
        return NodeResult(node.id, node.tool, ok=False, attempts=1,
                          stderr=f"analyze_video 查 video_metadata 失败: {e!r}")
    gcs = rows[0].get("gcs_uri") if rows else None
    if not gcs:
        return NodeResult(node.id, node.tool, ok=False, attempts=1,
                          stderr=f"找不到 video_id={vid} 的 gcs_uri")

    # M4.5:time_range=[起秒,止秒] → 只看该段(硬裁剪,见 _gemini_generate)。缓存键已含 time_range。
    tr = node.inputs.get("time_range")
    time_range = None
    if isinstance(tr, (list, tuple)) and len(tr) == 2:
        try:
            time_range = (float(tr[0]), float(tr[1]))
        except (TypeError, ValueError):
            time_range = None
    req = AnalyzeRequest(question=question,
                         context=node.inputs.get("context"),
                         rubric=node.inputs.get("rubric"),
                         time_range=time_range)
    # M4.1 缓存:同一(视频+问题+上下文+细则+模型)命中则不再调 Gemini(省成本/延迟)。
    # 键含【实际生效模型】(Pro/Flash)→ 不串味;失败信封【不缓存】(避免钉死瞬时报错)。
    model = MODEL_OVERRIDE.get() or PERCEPTION_MODEL
    ckey = analyze_cache.make_key(vid, question=req.question, context=req.context,
                                  rubric=req.rubric, time_range=req.time_range, model=model)
    dump = analyze_cache.get(ckey)
    cache_hit = dump is not None
    if dump is None:
        dump = analyze(req, gcs).model_dump()         # 看视频 → 最小信封
        if not str(dump.get("answer", "")).startswith(FAILURE_ANSWER_PREFIX):
            analyze_cache.put(ckey, dump)
    # value:video_id 在前、answer 紧随 → loop preview 露出"哪个视频 + 结论(前置)+ enough"
    return NodeResult(node.id, node.tool, ok=True, attempts=1, cache_hit=cache_hit,
                      value={"video_id": vid, **dump})


def _run_threshold_sweep(node: Node) -> NodeResult:
    """Stage 9 动态探针:主进程当 MCP 代理,逐阈值代入模板查询并汇总。"""
    template = node.inputs.get("sql_template", "")
    thresholds = node.inputs.get("thresholds", [0.5, 0.6, 0.7, 0.8, 0.9])
    out = []
    for t in thresholds:
        sql = template.replace("{threshold}", str(t))
        rows = mcp_client.query_db(sql)
        # 约定模板返回单行单聚合列;取首行首个数值列
        agg = {}
        if rows:
            agg = {k: v for k, v in rows[0].items()}
        out.append({"threshold": t, **agg})
    return NodeResult(node.id, node.tool, ok=True, value=out, attempts=1)


# ── 沙箱类(CodeGen + Stage 5/6)───────────────

def _run_sandbox_node(node: Node, upstream: dict[str, Any],
                      sandbox: SandboxClient, trace: Trace) -> NodeResult:
    gen = CodeGenerator()
    code = ""
    last = None

    for attempt in range(CODE_MAX_RETRIES + 1):
        step = trace.step(f"[{node.id}/{node.tool}] gen code (try {attempt + 1})")
        try:
            code = gen.generate(node, upstream) if attempt == 0 else gen.repair(
                last.stderr, last.exit_code
            )
            step.ok(code_len=len(code))
        except Exception as e:
            step.fail(error=repr(e))
            return NodeResult(node.id, node.tool, ok=False, code=code,
                              attempts=attempt, stderr=repr(e))

        step = trace.step(f"[{node.id}/{node.tool}] sandbox exec (try {attempt + 1})")
        last = sandbox.execute(_inject(code, node, upstream), timeout=30)

        if last.ok:
            value = _parse_stdout(last.stdout)
            step.ok(stdout_chars=len(last.stdout), elapsed_s=f"{last.elapsed_seconds:.2f}")
            artifact = {}
            if isinstance(value, dict):
                for key in ("svg", "png_base64"):
                    if key in value:
                        artifact[key] = value.pop(key)
            return NodeResult(node.id, node.tool, ok=True, value=value,
                              code=code, attempts=attempt + 1, artifact=artifact)

        will_retry = attempt < CODE_MAX_RETRIES
        step.fail(error=f"exit={last.exit_code}", will_retry=will_retry,
                  policy_violation=last.policy_violation)
        if not will_retry:
            return NodeResult(node.id, node.tool, ok=False, code=code,
                              attempts=attempt + 1, stderr=last.stderr)

    return NodeResult(node.id, node.tool, ok=False, code=code, stderr="unreachable")


# ── 统一入口 ──────────────────────────────────

def execute_node(node: Node, upstream: dict[str, Any],
                 sandbox: SandboxClient, trace: Trace,
                 schema: dict | None = None,
                 *, session_id: str | None = None) -> NodeResult:
    # sql_query:自管 trace + 自愈(对称 _run_sandbox_node)
    if node.tool == "sql_query":
        return _run_sql_query(node, schema or {}, trace)

    # 其它数据节点(threshold_sweep):主进程经 MCP,单次执行
    if not needs_sandbox(node.tool):
        step = trace.step(f"[{node.id}/{node.tool}] MCP query")
        try:
            if node.tool == "threshold_sweep":
                res = _run_threshold_sweep(node)
            elif node.tool == "show_video":
                res = _run_show_video(node, upstream)
            elif node.tool == "show_table":
                res = _run_show_table(node, upstream)
            elif node.tool == "analyze_video":
                res = _run_analyze_video(node, upstream)
            else:
                raise ValueError(f"未知数据工具: {node.tool}")
            step.ok(rows=len(res.videos) if res.videos else
                    (len(res.value) if isinstance(res.value, list) else 1))
            return res
        except Exception as e:
            step.fail(error=str(e)[:160])
            return NodeResult(node.id, node.tool, ok=False, stderr=str(e))

    return _run_sandbox_node(node, upstream, sandbox, trace)
