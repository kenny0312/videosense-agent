"""M5(DAG→loop):loop 路径的 CC 式 transcript 记忆 —— 记录 + 回放 + 压缩。

只作用于 loop 路径(VS_EXECUTOR=loop);dag 路径的 recipe/catalog 记忆原样不动。
- 记录:每个 loop 轮把 user/tool_call/tool_result/answer 事件 append 进 transcript_store。
- 回放:follow-up 时读 transcript 尾,组装成 loop 的上下文(【取代】recipe 块)。
- 压缩(决策④):尾巴超 token 高水位 → 把【超出最近 KEEP 轮】的老事件 LLM 摘要进一条
  running summary,压到低水位;最近若干轮 + 摘要保持。摘要器可注入(离线可测)。

安全:resolve_references / followup 门 / register_artifact 仍走原 catalog(本步不动)→ 无破坏。
recipe 仍被 derive(register_artifact),只是 loop 的【上下文】不再用它;彻底删 recipe 留 M7。
"""
from __future__ import annotations

import json
import logging

from pipeline import config
from pipeline.transcript_store import _scoped, append_event

log = logging.getLogger("pipeline.loop_memory")


# ── 记录:一个 loop 轮 → transcript 事件 ──────────────
def record_loop_turn(store, owner, session_id, turn_no, nl, trace, ledger, answer, *,
                     blob_put=None) -> None:
    append_event(store, owner, session_id, {"type": "user", "turn": turn_no, "text": nl})
    for s in trace:
        append_event(store, owner, session_id, {
            "type": "tool_call", "turn": turn_no, "event_id": s["cid"],
            "tool": s["tool"], "inputs": s["inputs"], "uses": s["uses"]})
        res = ledger.get(s["cid"])
        ev = {"type": "tool_result", "turn": turn_no, "event_id": s["cid"],
              "tool": s["tool"], "ok": bool(s["ok"])}
        if res is not None and res.ok:
            ev["value"] = res.value                       # 大本体由 append_event 溢出
        elif res is not None:
            ev["error"] = (res.stderr or "")[:300]
        append_event(store, owner, session_id, ev, blob_put=blob_put)
    append_event(store, owner, session_id, {"type": "answer", "turn": turn_no, "text": answer or ""})


# ── 回放 + 压缩 ──────────────────────────────────
def _group_turns(events):
    turns, order = {}, []
    for e in events:
        t = e.get("turn", 0)
        if t not in turns:
            turns[t] = []
            order.append(t)
        turns[t].append(e)
    return [(t, turns[t]) for t in order]


def _render_turn(turn_no, evs) -> str:
    lines = [f"## 第{turn_no}轮"]
    for e in evs:
        ty = e["type"]
        if ty == "user":
            lines.append(f"用户:{e.get('text', '')}")
        elif ty == "tool_call":
            u = f" 用[{','.join(e['uses'])}]" if e.get("uses") else ""
            lines.append(f"  调用 {e['tool']}({json.dumps(e.get('inputs', {}), ensure_ascii=False)}){u} → {e['event_id']}")
        elif ty == "tool_result":
            if e.get("ok"):
                prev = e.get("preview", e.get("value"))
                n = f"(共{e['n']}行)" if e.get("n") else ""
                lines.append(f"    {e['event_id']} 结果{n}:{json.dumps(prev, ensure_ascii=False)[:200]}")
            else:
                lines.append(f"    {e['event_id']} 失败:{e.get('error', '')}")
        elif ty == "answer":
            lines.append(f"答:{e.get('text', '')}")
    return "\n".join(lines)


def _est_tokens(s: str) -> int:
    return len(s) // 3                                    # 粗估(中英混合)


def compact(turns, *, keep, summarize):
    """最近 keep 轮保留原文;更老 → summarize 成一条摘要。返回 (summary|None, recent_turns)。"""
    if len(turns) <= keep or summarize is None:
        return None, turns
    old, recent = turns[:-keep], turns[-keep:]
    old_text = "\n".join(_render_turn(t, evs) for t, evs in old)
    try:
        summary = summarize(old_text)
    except Exception as e:                                # 摘要失败 → fail-open,不压
        log.warning("compaction 摘要失败(fail-open): %r", e)
        return None, turns
    return summary, recent


def build_loop_context(store, owner, session_id, *, keep=None, token_budget=None,
                       summarize=None, max_tail=400) -> "str | None":
    """读 transcript 尾 → (超预算则压缩) → loop 的多轮上下文字符串。空会话返回 None。"""
    keep = config.LOOP_KEEP_TURNS if keep is None else keep
    token_budget = config.LOOP_CONTEXT_TOKEN_BUDGET if token_budget is None else token_budget
    events = store.tail(_scoped(owner, session_id), max_tail)
    if not events:
        return None
    turns = _group_turns(events)
    full = "\n".join(_render_turn(t, evs) for t, evs in turns)
    if _est_tokens(full) > token_budget:
        summary, recent = compact(turns, keep=keep, summarize=summarize)
        parts = []
        if summary:
            parts.append("# 更早对话摘要\n" + summary)
        parts.append("# 最近对话\n" + "\n".join(_render_turn(t, evs) for t, evs in recent))
        body = "\n\n".join(parts)
    else:
        body = full
    return "# 多轮上下文(这是 follow-up;以下是此前对话与中间结果,据此继续作答/复用或重算)\n" + body


def make_llm_summarizer():
    """真实摘要器:用 CRITIC_MODEL 把老对话压成几句。lazy vertexai。"""
    def summarize(text: str) -> str:
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        m = GenerativeModel(config.CRITIC_MODEL)
        resp = m.generate_content(
            "把下面的多轮分析对话压成 3-6 条要点(保留:问过什么、得到什么结果/结论、"
            "产生了哪些可复用的中间结果及其 result_id)。只输出要点:\n\n" + text[:8000],
            generation_config={"temperature": 0.2, "max_output_tokens": 512})
        return resp.text
    return summarize
