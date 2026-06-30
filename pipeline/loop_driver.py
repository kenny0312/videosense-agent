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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
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
    "show_table":  ["data_result_id"],   # 必填:要展示的查询结果
}
_OPTIONAL_HANDLE = {"show_video"}        # 句柄非必填的工具
ANALYZE_PREVIEW_CELL = 1200               # #2:analyze_video 结果给大预览(答案含完整理由,默认 80 会砍掉)
SQL_PREVIEW_ROWS = 30                      # sql_query 列举类:大脑看到更多行(默认 3 行 → 让它列 14 个就会编/重复)


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
    table: dict = field(default_factory=dict)
    ms: float = 0.0                          # M4.2:本工具墙钟耗时(ms)
    cache_hit: bool = False                  # M4.2:analyze_video 是否命中缓存


@dataclass
class LoopResult:
    answer: str | None
    steps: int
    terminated: str                          # text | max_steps | repeat
    trace: list[dict]
    ledger: dict[str, ExecResult]
    llm_calls: int
    step_walls: list = field(default_factory=list)   # M4.2:每步墙钟(ms),vs Σtool_ms 量化并行加速


# ── 纯控制流(注入 conversation + execute,离线可测)──────────────
def run_loop(user_query: str, conversation, execute: Callable, *,
             max_steps: int | None = None, repeat_limit: int | None = None,
             on_step=None) -> LoopResult:
    max_steps = config.MAX_LOOP_STEPS if max_steps is None else max_steps
    repeat_limit = config.LOOP_REPEAT_LIMIT if repeat_limit is None else repeat_limit
    ledger: dict[str, ExecResult] = {}
    trace: list[dict] = []
    seen: dict = {}
    step_walls: list[float] = []
    msg: Any = user_query
    llm_calls = 0
    for step in range(max_steps):
        calls, text = conversation.send(msg)
        llm_calls += 1
        if not calls:                                        # 收敛:纯文本即答案
            if on_step:
                on_step({"type": "answer", "text": text or ""})
            return LoopResult(text or "", step, "text", trace, ledger, llm_calls, step_walls)

        # ① 准备(主线程):算 cid/sig/upstream;重复失败 → 即时终止
        prepared = []
        for i, call in enumerate(calls):
            cid = f"c{step}_{i}"
            sig = (call.name,
                   json.dumps(call.inputs, sort_keys=True, ensure_ascii=False, default=str),
                   tuple(call.uses))
            if seen.get(sig, 0) >= repeat_limit:             # 重复失败 → 强制终止
                return LoopResult(None, step, "repeat", trace, ledger, llm_calls, step_walls)
            upstream = {u: ledger[u].value for u in call.uses if u in ledger}
            prepared.append((cid, call, sig, upstream))

        # ② 执行:同一步内 analyze_video 互不依赖(uses 只指前序步)→ 线程池并发;其余串行。
        #    每个 worker 经 copy_context().run 携带本请求的 MODEL_OVERRIDE/_USAGE(否则 Pro 降级 + token 漏算)。
        step_t0 = time.perf_counter()
        results: dict[str, ExecResult] = {}
        analyze_grp = [(cid, call, up) for (cid, call, _s, up) in prepared if call.name == "analyze_video"]
        for cid, call, _s, up in prepared:                   # 非 analyze:主线程串行(不扩并发面)
            if call.name != "analyze_video":
                results[cid] = execute(cid, call.name, call.inputs, up, call.uses)
        if len(analyze_grp) > 1 and config.MAX_ANALYZE_PARALLEL > 1:
            workers = min(len(analyze_grp), config.MAX_ANALYZE_PARALLEL)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {}
                for cid, call, up in analyze_grp:
                    ctx = copy_context()                     # 主线程快照(含 MODEL_OVERRIDE/_USAGE)
                    futs[cid] = pool.submit(ctx.run, execute, cid, call.name, call.inputs, up, call.uses)
                for cid, fut in futs.items():
                    results[cid] = fut.result()
        else:                                                # 0/1 个或 MAX_ANALYZE_PARALLEL=1 → 退回串行
            for cid, call, up in analyze_grp:
                results[cid] = execute(cid, call.name, call.inputs, up, call.uses)
        step_walls.append((time.perf_counter() - step_t0) * 1000.0)

        # ③ 回收(主线程,按 cid 顺序单线程写)→ 回喂 Gemini 的顺序与串行一致(确定性)
        responses, step_tools = [], []
        for cid, call, sig, _up in prepared:
            res = results[cid]
            ledger[cid] = res
            trace.append({"cid": cid, "tool": call.name, "inputs": call.inputs,
                          "uses": call.uses, "ok": res.ok,
                          "ms": round(res.ms, 1), "cache_hit": res.cache_hit})
            step_tools.append({"tool": call.name, "cid": cid, "ok": res.ok})
            if res.ok:
                responses.append((call.name, {"result_id": cid, "preview": res.preview, "n": res.n}))
            else:
                seen[sig] = seen.get(sig, 0) + 1
                responses.append((call.name, {"result_id": cid, "error": (res.stderr or "")[:300]}))
        if on_step:                                          # M6b:每步事件(供 SSE 流式)
            on_step({"type": "step", "step": step, "tools": step_tools})
        msg = responses
    return LoopResult(None, max_steps, "max_steps", trace, ledger, llm_calls, step_walls)


# ── 真实适配器(live;M2 spike 已验)──────────────
class GeminiConversation:
    def __init__(self, model_name: str, declarations: list[dict], system: str):
        from vertexai.generative_models import FunctionDeclaration, GenerativeModel, Tool
        tool = Tool(function_declarations=[FunctionDeclaration(**d) for d in declarations])
        self._model = GenerativeModel(model_name, tools=[tool], system_instruction=system)
        self._model_name = model_name
        self._chat = self._model.start_chat()
        self.tokens = 0

    def send(self, msg):
        from vertexai.generative_models import Part
        from pipeline import usage
        payload = msg if isinstance(msg, str) else [
            Part.from_function_response(name=n, response=r) for n, r in msg]
        resp = self._chat.send_message(payload, generation_config={"temperature": 0.0})
        try:
            self.tokens += resp.usage_metadata.total_token_count
            usage.add_usage(resp, self._model_name)        # loop 的 token 也记进 usage(审计 + 前端监控,之前漏了)
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
    quota_lock = threading.Lock()                         # M4.3:并行 analyze 组下保护 quota 读-改-写(串行也无害)

    def _do(cid, name, inputs, upstream, uses) -> ExecResult:
        if name not in ALL_TOOLS:
            return ExecResult(ok=False, stderr=f"unknown tool: {name}")
        if name == "analyze_video":                       # 配额护栏(M2 stopgap,设计 §9)
            with quota_lock:                              # check+increment 原子(并行下防漏算/失控)
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
        if name == "analyze_video":
            pv, n = _preview(nr.value, cell=ANALYZE_PREVIEW_CELL)       # 答案含完整理由
        elif name == "sql_query":
            pv, n = _preview(nr.value, rows=SQL_PREVIEW_ROWS)           # 列举类:看到更多行,别只看 3 行就编/漏
        else:
            pv, n = _preview(nr.value)
        return ExecResult(ok=nr.ok, value=nr.value, preview=pv, n=n, stderr=nr.stderr,
                          code=nr.code, artifact=nr.artifact, videos=nr.videos, table=nr.table,
                          cache_hit=nr.cache_hit)

    def execute(cid, name, inputs, upstream, uses) -> ExecResult:
        t0 = time.perf_counter()                          # M4.2:per-tool 墙钟
        res = _do(cid, name, inputs, upstream, uses)
        res.ms = (time.perf_counter() - t0) * 1000.0
        return res
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
    trace: list                              # [{cid,tool,inputs,uses,ok,ms,cache_hit}] —— 供 M5 记 transcript
    step_walls: list = field(default_factory=list)   # M4.2:每步墙钟(ms)→ loop_metrics 算并行加速


_LOOP_SYSTEM = (
    "你是视频分析查询的编排器。每步可调用工具;工具执行后会返回 result_id + 结果预览。\n"
    "要把某个先前结果喂给下游工具,就把它的 result_id 填进该工具的句柄参数"
    "(如 plot 的 data_result_id;merge_asof 的 left_result_id=左表/视频侧、"
    "right_result_id=右表/传感器侧)。\n"
    "拿到足够信息后,用【纯文本】回答用户,不要再调用工具。\n\n"
    "# 收口前自检(没做到位别急着停)\n"
    "用纯文本收口【之前】先过一遍:用户要的我【真给到位了吗】?—— 比如要「全部/全量/都列出来」"
    "却只给了一截、或还有更合适的做法没用上。**没到位、且还有办法,就继续调工具把它做完**,"
    "别做一半就停、也别用「要不要继续」把活推回给用户。\n"
    "但若【确实做不到 / 没法一次全给】(数据里就没有、太多一次列不完、超出能力),就诚实说清"
    "(如「这是其中 N 个,共 X 个」),**别假装给全了、也别空转硬试**。简单/单值问答(打招呼、问个数)不必自检。\n\n"
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
    "别把 LIMIT 的条数当成总数说出来。要给总数就单独 COUNT(*)。\n"
    "- 【跟着用户这句到底要什么走 —— 别套固定流程、别一律 show】:问什么答什么、别多给。要一个"
    "【答案 / 数字 / 有没有】就直接用文字答 —— 哪怕问的是「有没有 X 视频 / 有几个 X 视频」,那也只是问"
    "【有无 / 数量】,文字答「有,N 个」就好,**别一提到「视频」就 show_video 把它们全播出来**;"
    "用户【明确要看 / 要清单】时才动用展示工具(show_table / show_video,按各自用途挑)。\n"
    "- 一个实情要记住:工具只回你【结果预览(前几十行)】,不是全部行 —— 所以当用户确实要【看全 / 全部"
    "列出】很多行时,你文字列不全、也别编,该用 show_table 把完整结果直接渲染成表格交给用户(不经你逐行复述)。\n"
    "- 只有结果就几行、或用户只要一个具体答案时,才直接文字作答。自己用文字列举时:【只】列预览里"
    "真实出现的行,【绝不】编造或重复凑数;列不全就如实说「前 N 条,共 X 条」(或干脆用 show_table 给全)。\n\n"
    "# 视频内容分析(analyze_video)\n"
    "- analyze_video 一次只看【一个】视频,且每请求有数量上限。候选很多时:先用 sql_query 把范围"
    "缩到几个最相关的,再 analyze_video —— **可在同一步一次性发起多个(每个一个视频),它们会并行执行、更快**;"
    "别一上来就想看全部,会撞上限且浪费。\n"
    "- 收口作答:直接回答用户【实际问的】,形式跟着用户走(要挑就挑、要描述就描述、要打分才打分,"
    "标准以用户的问题为准,别自作主张套格式);用自然语言把看到的依据讲清;别反问「要不要继续」—— "
    "配额内看够了就给结论。\n"
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
                       preview_value, r.ledger, r.trace, r.step_walls)


def loop_metrics(lo: "LoopOutcome") -> dict:
    """M6/M4.2 审计指标:步数、终止原因、工具直方图 + per-tool 计时 / 并行加速 / 缓存命中。"""
    from collections import Counter
    tr = lo.trace
    tool_ms = sum(s.get("ms", 0.0) for s in tr)               # 各工具墙钟之和(串行假想)
    wall_ms = sum(getattr(lo, "step_walls", None) or [])      # 各步真实墙钟之和(并行后 < tool_ms)
    analyze = [s for s in tr if s["tool"] == "analyze_video"]
    m = {"steps": lo.steps, "terminated": lo.terminated,
         "tool_calls": dict(Counter(s["tool"] for s in tr)),
         "tool_ms": round(tool_ms, 1),
         "wall_ms": round(wall_ms, 1),
         "analyze_calls": len(analyze),
         "analyze_cache_hits": sum(1 for s in analyze if s.get("cache_hit"))}
    if wall_ms > 0:                                            # 并行加速比 = Σtool_ms / 墙钟
        m["parallel_speedup"] = round(tool_ms / wall_ms, 2)
    return m
