"""反思器:父本失败病历 → LLM 反思 → 一处修改提案(§4 ②)。

输出必须是结构化 JSON(target + new_text + rationale + cites),坏输出直接丢弃
fail-open —— 反思器只有提案权,没有直接改 prompt 的权力;合法性由 space.validate 把关。
一次只改一段(论文的 minimal-edit 纪律:多处齐改无法归因)。
"""
from __future__ import annotations

import json
import os
import re

REFLECT_MODEL = os.environ.get("GEPA_REFLECT_MODEL", "gemini-2.5-pro")

_SYS = """你是 prompt 外科医生。下面是一个视频理解 agent 的【可修改 prompt 段落清单】和它\
在评测里的【失败病历】(每份病历含:用户问/agent答/判分标准/金标依据/工具轨迹/各尺子得分)。

你的任务:选【一处】修改,最大化治好病历里的共性病根。规则:
1. 只许改清单里列出的段落(lesson:<id> 或 tool:<名>),宪法不在清单里就是不许碰;
2. 一次只改一段;改写要短——比原文长很多的提案会被打回(指令越密,遵循率越差);
3. 新文本必须治"病根"不是背"题面":写普适的行为规则,禁止把某道题的具体答案写进去
   (那是作弊,封存考场会当场揭穿);
4. 输出严格 JSON(不要 markdown 代码块):
   {"target": "lesson:L04" 或 "tool:sql_query" 或 "lesson:NEW1"(新增,仅当清单说有空位),
    "new_text": "改后的完整文本",
    "rationale": "为什么这一改能治这些病历(一两句)",
    "cites": ["病历里的题目 id", ...]}
5. 如果病历里看不出 prompt 能治的共性病根(比如全是环境故障/判分器问题),输出 {"skip": true, "rationale": "..."}"""


def propose(space_doc: str, med_texts: list[str], lineage_note: str = "") -> "dict | None":
    """调 LLM 出一份提案;解析失败/不合法 → None(调用方跳过这一代)。"""
    from pipeline.genai_client import get_client
    prompt = (_SYS + "\n\n# 可修改段落清单\n" + space_doc
              + ("\n\n# 谱系备注(此前已试过的方向,别重复)\n" + lineage_note if lineage_note else "")
              + "\n\n# 失败病历\n" + "\n\n---\n\n".join(med_texts))
    try:
        resp = get_client().models.generate_content(
            model=REFLECT_MODEL, contents=prompt,
            # 2.5-pro 的思考 token 也计入上限(实测思考 ~2k),2048 会把 JSON 截断
            config={"temperature": 0.7, "max_output_tokens": 8192})
        return parse_proposal(resp.text or "")
    except Exception:
        return None


def parse_proposal(text: str) -> "dict | None":
    """从 LLM 输出里挖 JSON 提案(容忍代码块包裹);结构不对 → None。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if data.get("skip"):
        return {"skip": True, "rationale": str(data.get("rationale", ""))[:300]}
    target, new_text = data.get("target", ""), data.get("new_text", "")
    if not (isinstance(target, str) and isinstance(new_text, str) and new_text.strip()):
        return None
    kind, _, name = target.partition(":")
    if kind not in ("lesson", "tool") or not name:
        return None
    return {"target": target, "new_text": new_text.strip(),
            "rationale": str(data.get("rationale", ""))[:300],
            "cites": [str(c) for c in (data.get("cites") or [])][:12]}


def to_overrides(proposal: dict, parent_overrides: dict) -> dict:
    """提案 → 子代覆盖(= 父本覆盖 + 这一处修改;深拷贝,不动父本)。
    NEW 槽位撞名时自动顺延(NEW1 已被父本用掉 → 落到 NEW2),防静默覆盖(审计 m10)。"""
    ov = {"lessons": dict(parent_overrides.get("lessons") or {}),
          "tools": dict(parent_overrides.get("tools") or {})}
    kind, _, name = proposal["target"].partition(":")
    if kind == "lesson" and name.startswith("NEW") and name in ov["lessons"]:
        i = 1
        while f"NEW{i}" in ov["lessons"]:
            i += 1
        name = f"NEW{i}"
    ov["lessons" if kind == "lesson" else "tools"][name] = proposal["new_text"]
    return ov
