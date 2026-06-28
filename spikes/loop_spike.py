"""M2 spike(DAG→loop):验证 Gemini 原生 function-calling 主循环 + 句柄约定。

目标(回答 roadmap 开放问题):
  ② 多输入工具(merge_asof 的 left/right_result_id)句柄可靠性 —— 模型能否稳定把
     "视频侧"结果填进 left、"传感器侧"填进 right?
  ③ loop 大脑模型选型 —— gemini-2.5-flash(CRITIC) vs gemini-2.5-pro(PLANNER):
     收敛率、句柄正确率、步数、延迟、token。

设计:用【桩工具】(无 DB / 无沙箱)隔离纯机制 —— 只测「function-calling 循环 + 句柄传递」。
工具执行后回一个 result_id;要引用先前结果就把 result_id 填进对应句柄参数。

运行:
  GCP_PROJECT=... GCP_REGION=us-central1 python spikes/loop_spike.py
"""
import json
import os
import time

import vertexai
from vertexai.generative_models import (Content, FunctionDeclaration, GenerativeModel,
                                        Part, Tool)

PROJECT = os.environ.get("GCP_PROJECT", "primeval-camera-494521-u6")
REGION = os.environ.get("GCP_REGION", "us-central1")
vertexai.init(project=PROJECT, location=REGION)

MODELS = {"flash": "gemini-2.5-flash", "pro": "gemini-2.5-pro"}

# ── 桩工具声明(含句柄参数,即 M3 要验的约定)──────────────────
DECLS = [
    FunctionDeclaration(
        name="sql_query",
        description="执行只读 SELECT 返回行。结果会带一个 result_id 句柄,供后续工具引用。",
        parameters={"type": "object",
                    "properties": {"sql": {"type": "string", "description": "完整 SELECT"}},
                    "required": ["sql"]},
    ),
    FunctionDeclaration(
        name="merge_asof",
        description=("近似时间对齐合并两张表。left_result_id = 上游【左表/视频侧】那一步的 "
                     "result_id;right_result_id = 上游【右表/传感器侧】那一步的 result_id。"),
        parameters={"type": "object",
                    "properties": {
                        "left_result_id": {"type": "string", "description": "左表(视频侧)上游步的 result_id"},
                        "right_result_id": {"type": "string", "description": "右表(传感器侧)上游步的 result_id"},
                        "left_on": {"type": "string"}, "right_on": {"type": "string"},
                        "tolerance_ms": {"type": "number"}},
                    "required": ["left_result_id", "right_result_id", "left_on", "right_on", "tolerance_ms"]},
    ),
    FunctionDeclaration(
        name="plot",
        description="对某上游结果出图。data_result_id = 要画的那一步的 result_id。",
        parameters={"type": "object",
                    "properties": {
                        "data_result_id": {"type": "string", "description": "要画的上游步的 result_id"},
                        "kind": {"type": "string", "enum": ["scatter", "line"]},
                        "x": {"type": "string"}, "y": {"type": "string"}, "title": {"type": "string"}},
                    "required": ["data_result_id", "kind", "x", "y"]},
    ),
]
TOOL = Tool(function_declarations=DECLS)
SYSTEM = ("你是数据分析编排器。每个工具执行后会返回 result_id + 结果预览。要用某个先前结果,"
          "就把它的 result_id 填进对应句柄参数。完成后用纯文本回答用户,不要再调用工具。")


def _stub_exec(name, args, ledger, flags):
    """桩执行器:不碰真实 DB/沙箱,回 result_id + 预览;并记录句柄是否填对。"""
    if name == "sql_query":
        sql = (args.get("sql") or "").lower()
        kind = "sensor" if ("sensor" in sql or "heart" in sql) else "video"
        cols = ["t", "heart_rate"] if kind == "sensor" else ["video_id", "ts", "predicate"]
        return {"kind": kind, "columns": cols, "n": 3,
                "preview": [{c: f"<{c}>" for c in cols}]}
    if name == "merge_asof":
        lk = ledger.get(args.get("left_result_id"), {}).get("kind")
        rk = ledger.get(args.get("right_result_id"), {}).get("kind")
        flags["handle_correct"] = (lk == "video" and rk == "sensor")
        flags["handle_detail"] = f"left={args.get('left_result_id')}({lk}) right={args.get('right_result_id')}({rk})"
        return {"merged": True, "n": 42}
    if name == "plot":
        flags["plot_handle_ok"] = args.get("data_result_id") in ledger
        return {"plot_url": "stub://plot.svg", "n_points": 3}
    return {"error": f"unknown tool {name}"}


def run_loop(model_name, user_query, max_steps=8):
    model = GenerativeModel(model_name, tools=[TOOL], system_instruction=SYSTEM)
    chat = model.start_chat()
    ledger, trace, flags = {}, [], {}
    tokens = 0
    msg = user_query
    t0 = time.time()
    for step in range(max_steps):
        resp = chat.send_message(msg, generation_config={"temperature": 0.0})
        tokens += resp.usage_metadata.total_token_count
        parts = resp.candidates[0].content.parts
        fcs = [p.function_call for p in parts if getattr(p, "function_call", None) and p.function_call.name]
        if not fcs:                                    # 收敛:纯文本即答案
            text = "".join(getattr(p, "text", "") for p in parts)
            return {"converged": True, "steps": step, "trace": trace, "flags": flags,
                    "tokens": tokens, "latency_s": round(time.time() - t0, 1), "answer": text[:120]}
        responses = []
        for i, fc in enumerate(fcs):
            cid = f"c{step}_{i}"
            args = dict(fc.args)
            res = _stub_exec(fc.name, args, ledger, flags)
            ledger[cid] = res
            trace.append(f"{cid}:{fc.name}({json.dumps(args, ensure_ascii=False)})")
            responses.append(Part.from_function_response(name=fc.name, response={"result_id": cid, **res}))
        msg = responses                                 # 把所有 function_response 喂回
    return {"converged": False, "steps": max_steps, "trace": trace, "flags": flags,
            "tokens": tokens, "latency_s": round(time.time() - t0, 1), "answer": None}


SCEN_MERGE = ("数据库里有视频片段表(列 video_id, ts, predicate)和传感器心率表(列 t, heart_rate)。"
              "请把这两张表按各自的时间列(ts 和 t)近似对齐合并,容差 500ms,然后告诉我合并后有多少行。")
SCEN_PLOT = ("查出每个 predicate 的视频数量(列 predicate, cnt),然后画成散点图,x=predicate,y=cnt,"
             "标题用英文。最后告诉我画好了。")

TRIALS = {"merge": (SCEN_MERGE, 3), "plot": (SCEN_PLOT, 2)}


def main():
    rows = []
    for mkey, mname in MODELS.items():
        for skey, (prompt, n) in TRIALS.items():
            for t in range(n):
                try:
                    r = run_loop(mname, prompt)
                except Exception as e:
                    rows.append((mkey, skey, t, "ERROR", str(e)[:80]))
                    print(f"[{mkey}/{skey}#{t}] ERROR: {e!r}")
                    continue
                if skey == "merge":
                    ok = r["flags"].get("handle_correct")
                    detail = r["flags"].get("handle_detail", "(no merge call)")
                else:
                    ok = r["flags"].get("plot_handle_ok")
                    detail = "plot handle " + ("ok" if ok else "BAD")
                rows.append((mkey, skey, t, r["converged"], r["steps"], ok, r["latency_s"], r["tokens"]))
                print(f"[{mkey}/{skey}#{t}] converged={r['converged']} steps={r['steps']} "
                      f"handle_ok={ok} lat={r['latency_s']}s tok={r['tokens']} | {detail}")
                print(f"            trace: {' -> '.join(r['trace'])}")

    print("\n=== 汇总(handle_ok 率 = 句柄填对的比例)===")
    for mkey in MODELS:
        for skey in TRIALS:
            sub = [r for r in rows if r[0] == mkey and r[1] == skey and r[3] != "ERROR"]
            if not sub:
                continue
            conv = sum(1 for r in sub if r[3]) / len(sub)
            okr = sum(1 for r in sub if r[5]) / len(sub)
            lat = sum(r[6] for r in sub) / len(sub)
            tok = sum(r[7] for r in sub) / len(sub)
            print(f"{mkey:5} {skey:6}: 收敛 {conv:.0%} | handle_ok {okr:.0%} | "
                  f"avg {lat:.1f}s / {tok:.0f} tok  (n={len(sub)})")


if __name__ == "__main__":
    main()
