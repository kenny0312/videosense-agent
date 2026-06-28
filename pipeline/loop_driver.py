"""DAG→loop 迁移(M3):probe-and-step 主循环驱动器。

- `run_loop` 是【纯控制流】(注入 conversation + execute,便于离线单测)。
- `GeminiConversation` / `_make_executor` 是真实适配器(live 由 M2 spike 验过)。
  复用现有 `node_executor.execute_node` 当工具执行器;复用 M1 的
  `node_specs.build_function_declarations`,叠加 M2 验过的【上游句柄】参数。
- ① 决策(见 docs/design):M3 阶段把成功调用链【合成一个极简 DAG】交给
  `session.register_artifact`,故 recipe/记忆层原样不动;纯 transcript 化留到 M5。

灰度:`config.VS_EXECUTOR=loop` 时由 orchestrator 走这里;默认 dag 路径不受影响。
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pipeline import config
from pipeline.dag_schema import ALL_TOOLS, DAG, Node
from pipeline.node_executor import execute_node
from pipeline.node_specs import build_function_declarations

log = logging.getLogger("pipeline.loop_driver")

# 上游句柄约定(M2 spike 验过 10/10):多输入工具用命名 result_id 参数引用上游步。
UPSTREAM_HANDLES: dict[str, list[str]] = {
    "merge_asof":  ["left_result_id", "right_result_id"],
    "interpolate": ["data_result_id"],
    "ols_regress": ["data_result_id"],
    "plot":        ["data_result_id"],
    "python":      ["data_result_id"],
    "show_video":  ["data_result_id"],   # 可选(也可直接给 video_ids)
}
_OPTIONAL_HANDLE = {"show_video"}        # 句柄非必填的工具


def loop_function_declarations() -> list[dict]:
    """M1 工具声明 + 叠加上游句柄参数(loop 专用)。深拷贝,绝不污染 SPECS。"""
    out = []
    for d in build_function_declarations():
        d = copy.deepcopy(d)
        handles = UPSTREAM_HANDLES.get(d["name"], [])
        if handles:
            props = d["parameters"].setdefault("properties", {})
            for h in handles:
                props[h] = {"type": "string", "description": f"上游某步返回的 result_id（{h}）"}
            if d["name"] not in _OPTIONAL_HANDLE:
                d["parameters"]["required"] = list(d["parameters"].get("required", [])) + handles
        out.append(d)
    return out


def _preview(value: Any, rows: int = 3, cols: int = 8, cell: int = 80):
    """把结果压成 ≤rows×cols×cell 的预览 + 真实行数。完整值【不】进 prompt。"""
    def cap(s):
        s = str(s)
        return s if len(s) <= cell else s[:cell - 1] + "…"
    if value is None:
        return [], 0
    if isinstance(value, dict):
        return [{k: cap(v) for k, v in list(value.items())[:cols]}], 1
    if isinstance(value, list):
        out = []
        for r in value[:rows]:
            out.append({k: cap(v) for k, v in list(r.items())[:cols]} if isinstance(r, dict)
                       else {"value": cap(r)})
        return out, len(value)
    return [{"value": cap(value)}], 1


def _to_py(v):
    """proto Map/Repeated → 纯 python(可 JSON 序列化)。"""
    if isinstance(v, dict):
        return {k: _to_py(x) for k, x in v.items()}
    if hasattr(v, "items"):                                  # MapComposite
        return {k: _to_py(v[k]) for k in v}
    if not isinstance(v, (str, bytes)) and hasattr(v, "__iter__"):
        return [_to_py(x) for x in v]
    return v


# ── 数据结构 ──────────────────────────────────
@dataclass
class Call:
    name: str
    inputs: dict
    uses: list[str]


@dataclass
class ExecResult:
    ok: bool
    value: Any = None
    preview: Any = field(default_factory=list)
    n: int = 0
    stderr: str = ""
    code: str = ""
    artifact: dict = field(default_factory=dict)
    videos: list = field(default_factory=list)


@dataclass
class LoopResult:
    answer: str | None
    steps: int
    terminated: str                          # text | max_steps | repeat
    trace: list[dict]
    ledger: dict[str, ExecResult]
    llm_calls: int


# ── 纯控制流(注入 conversation + execute,离线可测)──────────────
def run_loop(user_query: str, conversation, execute: Callable, *,
             max_steps: int | None = None, repeat_limit: int | None = None) -> LoopResult:
    max_steps = config.MAX_LOOP_STEPS if max_steps is None else max_steps
    repeat_limit = config.LOOP_REPEAT_LIMIT if repeat_limit is None else repeat_limit
    ledger: dict[str, ExecResult] = {}
    trace: list[dict] = []
    seen: dict = {}
    msg: Any = user_query
    llm_calls = 0
    for step in range(max_steps):
        calls, text = conversation.send(msg)
        llm_calls += 1
        if not calls:                                        # 收敛:纯文本即答案
            return LoopResult(text or "", step, "text", trace, ledger, llm_calls)
        responses = []
        for i, call in enumerate(calls):
            cid = f"c{step}_{i}"
            sig = (call.name,
                   json.dumps(call.inputs, sort_keys=True, ensure_ascii=False, default=str),
                   tuple(call.uses))
            if seen.get(sig, 0) >= repeat_limit:             # 重复失败 → 强制终止
                return LoopResult(None, step, "repeat", trace, ledger, llm_calls)
            upstream = {u: ledger[u].value for u in call.uses if u in ledger}
            res = execute(cid, call.name, call.inputs, upstream, call.uses)
            ledger[cid] = res
            trace.append({"cid": cid, "tool": call.name, "inputs": call.inputs,
                          "uses": call.uses, "ok": res.ok})
            if res.ok:
                responses.append((call.name, {"result_id": cid, "preview": res.preview, "n": res.n}))
            else:
                seen[sig] = seen.get(sig, 0) + 1
                responses.append((call.name, {"result_id": cid, "error": (res.stderr or "")[:300]}))
        msg = responses
    return LoopResult(None, max_steps, "max_steps", trace, ledger, llm_calls)


# ── 真实适配器(live;M2 spike 已验)──────────────
class GeminiConversation:
    def __init__(self, model_name: str, declarations: list[dict], system: str):
        from vertexai.generative_models import FunctionDeclaration, GenerativeModel, Tool
        tool = Tool(function_declarations=[FunctionDeclaration(**d) for d in declarations])
        self._model = GenerativeModel(model_name, tools=[tool], system_instruction=system)
        self._chat = self._model.start_chat()
        self.tokens = 0

    def send(self, msg):
        from vertexai.generative_models import Part
        payload = msg if isinstance(msg, str) else [
            Part.from_function_response(name=n, response=r) for n, r in msg]
        resp = self._chat.send_message(payload, generation_config={"temperature": 0.0})
        try:
            self.tokens += resp.usage_metadata.total_token_count
        except Exception:
            pass
        calls, texts = [], []
        for p in resp.candidates[0].content.parts:
            fc = getattr(p, "function_call", None)
            if fc and fc.name:
                args = _to_py(dict(fc.args))
                uses = [args.pop(h) for h in UPSTREAM_HANDLES.get(fc.name, []) if h in args]
                calls.append(Call(fc.name, args, uses))
            elif getattr(p, "text", ""):
                texts.append(p.text)
        return calls, ("".join(texts) if texts else None)


def _make_executor(sandbox, trace, schema, session_id, value_store) -> Callable:
    def execute(cid, name, inputs, upstream, uses):
        if name not in ALL_TOOLS:
            return ExecResult(ok=False, stderr=f"unknown tool: {name}")
        try:
            node = Node(id=cid, tool=name, inputs=inputs, depends_on=list(uses))
        except Exception as e:                               # 幻觉/坏参数 → 软失败回喂
            return ExecResult(ok=False, stderr=f"bad node {name}: {e}")
        nr = execute_node(node, upstream, sandbox, trace, schema=schema,
                          session_id=session_id, value_store=value_store)
        pv, n = _preview(nr.value)
        return ExecResult(ok=nr.ok, value=nr.value, preview=pv, n=n, stderr=nr.stderr,
                          code=nr.code, artifact=nr.artifact, videos=nr.videos)
    return execute


def synthesize_dag(trace: list[dict]) -> "DAG | None":
    """① 决策:把成功调用链合成极简 DAG,交给 register_artifact(recipe 层不动)。"""
    nodes, ok_ids = [], set()
    for s in trace:
        if not s["ok"]:
            continue
        deps = [u for u in s["uses"] if u in ok_ids]
        nodes.append(Node(id=s["cid"], tool=s["tool"], inputs=s["inputs"], depends_on=deps))
        ok_ids.add(s["cid"])
    return DAG(nodes=nodes) if nodes else None


@dataclass
class LoopOutcome:
    answer: str | None
    steps: int
    terminated: str
    dag: "DAG | None"
    node_values: dict
    results: dict                            # cid -> ExecResult(有 .code/.artifact/.videos)


_LOOP_SYSTEM = (
    "你是视频分析查询的编排器。每步可调用工具;工具执行后会返回 result_id + 结果预览。\n"
    "要把某个先前结果喂给下游工具,就把它的 result_id 填进该工具的句柄参数"
    "(如 plot 的 data_result_id;merge_asof 的 left_result_id=左表/视频侧、"
    "right_result_id=右表/传感器侧)。\n"
    "拿到足够信息后,用【纯文本】回答用户,不要再调用工具。\n\n"
    "# 关键数据说明\n"
    "- video_facts.predicate 是英文活动描述,用 ILIKE 模糊匹配(中文先译英:滑雪→%skiing%/%snowboarding%)。\n"
    "- video_facts.matched 是布尔;查已确认事实加 AND matched = true。\n"
    "- 关系类查询(筛选/聚合/join/排序)用单个 sql_query 直接写完整 SQL。\n"
    "- 出图/科学计算的文本(SQL、plot 标题)一律用英文。\n"
)


def _loop_system(schema: dict, context: "dict | None") -> str:
    s = _LOOP_SYSTEM + "\n# 数据库结构\n" + json.dumps(schema, ensure_ascii=False)
    if context:
        from pipeline.planner import _context_block       # 复用配方上下文(followup)
        s += "\n\n" + _context_block(context)
    return s


def run_query_loop(nl: str, *, schema: dict, context: "dict | None", sandbox, trace,
                   session_id: "str | None", value_store) -> LoopOutcome:
    """orchestrator 的 loop 入口:建会话 + 执行器 → run_loop → 合成 DAG + 收产物。"""
    conv = GeminiConversation(config.LOOP_MODEL, loop_function_declarations(),
                              _loop_system(schema, context))
    execute = _make_executor(sandbox, trace, schema, session_id, value_store)
    r = run_loop(nl, conv, execute)
    dag = synthesize_dag(r.trace)
    node_values = {cid: res.value for cid, res in r.ledger.items() if res.ok}
    return LoopOutcome(r.answer, r.steps, r.terminated, dag, node_values, r.ledger)
