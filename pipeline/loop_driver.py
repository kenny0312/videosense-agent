"""DAG→loop 迁移(M3):probe-and-step 主循环驱动器。

- `run_loop` 是【纯控制流】(注入 conversation + execute,便于离线单测)。
- `GeminiConversation` / `_make_executor` 是真实适配器(live 由 M2 spike 验过)。
  复用现有 `node_executor.execute_node` 当工具执行器;复用 M1 的
  `node_specs.build_function_declarations`,叠加 M2 验过的【上游句柄】参数。
- 记忆简化:不再 register_artifact / catalog / 值复用;唯一记忆 = transcript,上一轮上下文
  走 transcript 回放(loop_memory)。loop 是 orchestrator 唯一执行路径。
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from pipeline import config
from pipeline.dag_schema import ALL_TOOLS, Node
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
ANALYZE_PREVIEW_CELL = 1200               # #2:analyze_video 结果给大预览(答案含完整理由,默认 80 会砍掉)


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
             max_steps: int | None = None, repeat_limit: int | None = None,
             on_step=None) -> LoopResult:
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
            if on_step:
                on_step({"type": "answer", "text": text or ""})
            return LoopResult(text or "", step, "text", trace, ledger, llm_calls)
        responses, step_tools = [], []
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
            step_tools.append({"tool": call.name, "cid": cid, "ok": res.ok})
            if res.ok:
                responses.append((call.name, {"result_id": cid, "preview": res.preview, "n": res.n}))
            else:
                seen[sig] = seen.get(sig, 0) + 1
                responses.append((call.name, {"result_id": cid, "error": (res.stderr or "")[:300]}))
        if on_step:                                          # M6b:每步事件(供 SSE 流式)
            on_step({"type": "step", "step": step, "tools": step_tools})
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


def _make_executor(sandbox, trace, schema, session_id) -> Callable:
    quota = {"analyzed": 0}                               # 配额:本请求 analyze_video 调用计数
    def execute(cid, name, inputs, upstream, uses):
        if name not in ALL_TOOLS:
            return ExecResult(ok=False, stderr=f"unknown tool: {name}")
        if name == "analyze_video":                       # 配额护栏(M2 stopgap,设计 §9)
            if quota["analyzed"] >= config.MAX_VIDEOS_PER_REQUEST:
                note = (f"已达本请求视频分析上限({config.MAX_VIDEOS_PER_REQUEST} 个),这个【没分析】。"
                        "请【就已分析过的那些视频】给出结论:不要再调 analyze_video,"
                        "也不要把没分析的视频当成分析过了来说;要覆盖更多就让用户缩小候选或分批问。")
                pv, n = _preview({"answer": note, "enough": "no"})
                return ExecResult(ok=True, value={"answer": note, "enough": "no"}, preview=pv, n=n)
            quota["analyzed"] += 1
        try:
            node = Node(id=cid, tool=name, inputs=inputs, depends_on=list(uses))
        except Exception as e:                               # 幻觉/坏参数 → 软失败回喂
            return ExecResult(ok=False, stderr=f"bad node {name}: {e}")
        nr = execute_node(node, upstream, sandbox, trace, schema=schema,
                          session_id=session_id)
        # #2 修:analyze_video 的结论+理由都在 answer 里;默认 80 字/格会把理由砍掉,大脑收口时
        # 只看到前 80 字 → 答案干瘪。给它大额度预览,完整证据进得了最终答案(其余工具仍用小预览省 token)。
        cell = ANALYZE_PREVIEW_CELL if name == "analyze_video" else 80
        pv, n = _preview(nr.value, cell=cell)
        return ExecResult(ok=nr.ok, value=nr.value, preview=pv, n=n, stderr=nr.stderr,
                          code=nr.code, artifact=nr.artifact, videos=nr.videos)
    return execute


@dataclass
class LoopOutcome:
    answer: str | None
    steps: int
    terminated: str
    final_tool: "str | None"                 # 最终成功步的工具(决定 artifact kind)
    final_value: Any                         # 最终成功步的结果值
    preview_value: Any                       # 预览/值复用依据(plot 时=上游 x/y,否则=final_value)
    results: dict                            # cid -> ExecResult(有 .code/.artifact/.videos)
    trace: list                              # [{cid,tool,inputs,uses,ok}] —— 供 M5 记 transcript


_LOOP_SYSTEM = (
    "你是视频分析查询的编排器。每步可调用工具;工具执行后会返回 result_id + 结果预览。\n"
    "要把某个先前结果喂给下游工具,就把它的 result_id 填进该工具的句柄参数"
    "(如 plot 的 data_result_id;merge_asof 的 left_result_id=左表/视频侧、"
    "right_result_id=右表/传感器侧)。\n"
    "拿到足够信息后,用【纯文本】回答用户,不要再调用工具。\n\n"
    "# 指代与追问(指代解析现在归你做)\n"
    "- 用户指代之前的结果(这个/那个/上面/刚才/那批/those/it/above 等)时:从上方【多轮上下文】回放里"
    "找到对应那一条 —— 回放含每一步的完整 inputs(如某次 show_video 的 video_ids、某次 sql_query 的条件),"
    "据此定位到具体的 result_id / 视频 id 再继续。\n"
    "- 元问题(你怎么得出的/用了什么方法)同样据回放里那一轮的真实工具链来解释,不要编造步骤。\n"
    "- 若回放里【找不到】能对上的那一条(或根本没有上文),就用纯文本反问让用户说具体些"
    "(指哪一条 / 哪个视频),【不要瞎猜、不要随便挑一条】。\n"
    "- 收口作答时,凡涉及具体视频/结果的,点名它(如视频 id),别只说「那个」。\n\n"
    "# 关键数据说明\n"
    "- video_facts.predicate 是英文活动描述,用 ILIKE 模糊匹配(中文先译英:滑雪→%skiing%/%snowboarding%)。\n"
    "- video_facts.matched 是布尔;查已确认事实加 AND matched = true。\n"
    "- 关系类查询(筛选/聚合/join/排序)用单个 sql_query 直接写完整 SQL。\n"
    "- 出图/科学计算的文本(SQL、plot 标题)一律用英文。\n"
    "- 报【总数/数量】时必须真的 COUNT 过;列举或抽样(LIMIT)拿到的条数【不是】总数 —— "
    "别把 LIMIT 的条数当成总数说出来。要给总数就单独 COUNT(*)。\n\n"
    "# 视频内容分析(analyze_video)\n"
    "- analyze_video 一次只看【一个】视频,且每请求有数量上限。候选很多时:先用 sql_query 把范围"
    "缩到几个最相关的,再对这几个【逐个】analyze_video —— 别一上来就想看全部,会撞上限且浪费。\n"
    "- 收口作答:用【自然语言】复述具体证据(画面/动作 + 关键时刻),并给出【明确的取舍/排名】——"
    "即使几个很接近也要选出一个、说清凭什么打破平局;【别】自作主张打数字分(用户没要求就别 X/10)、"
    "别回避说「都差不多」、也别反问「要不要继续分析」(配额内看够了就直接给结论)。\n"
)


def _loop_system(schema: dict, replay_context: "str | None") -> str:
    s = _LOOP_SYSTEM + "\n# 数据库结构\n" + json.dumps(schema, ensure_ascii=False)
    if replay_context:                                    # M5:transcript 回放(取代 recipe 块)
        s += "\n\n" + replay_context
    return s


def run_query_loop(nl: str, *, schema: dict, replay_context: "str | None", sandbox, trace,
                   session_id: "str | None", on_step=None) -> LoopOutcome:
    """orchestrator 的 loop 入口:建会话 + 执行器 → run_loop → 收产物(纯 handle,无合成 DAG)。
    replay_context(M5)= 从 transcript 回放出的多轮上下文(取代旧 recipe 块)。
    on_step(M6b)= 每步回调,供 SSE 流式。"""
    conv = GeminiConversation(config.LOOP_MODEL, loop_function_declarations(),
                              _loop_system(schema, replay_context))
    execute = _make_executor(sandbox, trace, schema, session_id)
    r = run_loop(nl, conv, execute, on_step=on_step)
    # 最终成功步 → artifact 的 kind/value;preview_value:plot-final 取上游数据
    # (plot 自身 value 只有 {n_points},无复用价值),其余 = final_value。
    final_tool = final_value = preview_value = None
    ok_steps = [s for s in r.trace if s["ok"]]
    if ok_steps:
        last = ok_steps[-1]
        final_tool, final_value = last["tool"], r.ledger[last["cid"]].value
        preview_value = final_value
        if final_tool == "plot":
            for s in reversed(ok_steps[:-1]):
                if s["tool"] != "plot":
                    preview_value = r.ledger[s["cid"]].value
                    break
    return LoopOutcome(r.answer, r.steps, r.terminated, final_tool, final_value,
                       preview_value, r.ledger, r.trace)


def loop_metrics(lo: "LoopOutcome") -> dict:
    """M6 审计指标:步数、终止原因、工具调用直方图(供 _audit 落服务端)。"""
    from collections import Counter
    return {"steps": lo.steps, "terminated": lo.terminated,
            "tool_calls": dict(Counter(s["tool"] for s in lo.trace))}
