"""VideoWorld 的工具面（dual-control）：agent 侧 + user 侧各自能用的 tool。

τ² 的精髓是 dual-control —— agent 和 user 都能对【共享的 VideoWorld 状态】动作。
这里把两边各自的 tool 面显式列出，方便出题、展示、和给模拟用户约束动作范围。

    python -m evals.tools     # 打印工具清单
"""
from __future__ import annotations

# ── User 侧动作（模拟用户能对共享 VideoWorld 状态做的事；对应真实 API seam）──
USER_TOOLS = {
    "say":          "说一句话 / 追问（普通对话轮）。",
    "correct":      "纠正 / 改口（如「不对，我说的是跳伞不是滑雪」）。",
    "upload_video": "上传一个新视频 → 改 uploads 共享状态（真 seam：POST /v1/upload_url）。",
    "paste_image":  "Ctrl+V 贴一张图 → 本轮多模态输入（真 seam：VibeQueryRequest.image）。",
    "enrich_video": "触发 ingestion 把视频灌进语义索引 → 改 content_embeddings（真 seam：POST /v1/enrich）。",
}

# 需要 config 开关才可见的 agent 工具
_GATED = {
    "web_search": "USE_WEB_SEARCH",
    "semantic_search": "USE_SEMANTIC_SEARCH",
    "update_memory": "USE_USER_MEMORY",
    "spawn_agents": "USE_SUBAGENTS",
}


def agent_tools() -> dict:
    """agent 侧工具：直接取自 pipeline.node_specs.SPECS（单一真相），值为一句话描述。"""
    from pipeline.node_specs import SPECS

    out = {}
    for name, spec in SPECS.items():
        desc = " ".join(spec.planner_desc.split())
        out[name] = (desc[:110] + "…") if len(desc) > 110 else desc
    return out


def manifest() -> str:
    lines = [
        "# VideoWorld 工具面（dual-control）",
        "",
        "共享状态 = 视频语料 + pgvector 索引 + per-owner memory + transcript。两边都能动它。",
        "",
        "## Agent 侧（被测的真 VS —— 取自 node_specs.SPECS）",
    ]
    for name, desc in agent_tools().items():
        gate = f"  〔需开关 {_GATED[name]}〕" if name in _GATED else ""
        lines.append(f"- **{name}**{gate}：{desc}")
    lines += ["", "## User 侧（模拟用户能对共享状态做的动作）"]
    for name, desc in USER_TOOLS.items():
        lines.append(f"- **{name}**：{desc}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print(manifest())
