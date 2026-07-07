"""模拟用户（Mode B 多轮）：pinned 跨家族模型（Claude），persona+goal 驱动，能用 USER_TOOLS 动作。

τ² 处方：pin 模型、每轮重注入 goal、约束在 world 工具/状态面、drift 直接丢弃不误标。
两种模式：
- 脚本模式（有 script）：固定轮次、零方差，per-PR / CI 友好。
- 自由模式（无 script）：Claude 驱动，测鲁棒/goal-shift，需要 ANTHROPIC_API_KEY（见 preflight）。

默认不跑 —— 先把系统建起来。
"""
from __future__ import annotations

import json
import os

from evals.tools import USER_TOOLS

DEFAULT_USER_MODEL = "claude-haiku-4-5-20251001"   # 中档、可靠、便宜；跨家族（非 Gemini），pin 住


def preflight():
    """自由模式能不能跑。能返回 None；否则返回说明。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "没配 ANTHROPIC_API_KEY —— 自由模式模拟用户要跨家族 Claude。set ANTHROPIC_API_KEY=... 再跑。"
    return None


_SYS = (
    "你在扮演一个【真实用户】，正在和一个视频理解助手多轮对话。严格遵守：\n"
    "1) 只追你的目标，不要跑题、不要编造视频库里不存在的东西。\n"
    "2) 说人话、口语、简短，像真用户。\n"
    "3) 只能用这些动作：{tools}。要执行动作时，在回复末尾单独加一行 JSON：{{\"action\": {{...}}}}。\n"
    "4) 目标达到了就说一句收尾，并附一行 {{\"done\": true}}。"
)


class SimulatedUser:
    def __init__(self, persona: str, goal: str, script=None, model: str = DEFAULT_USER_MODEL):
        self.persona, self.goal = persona, goal
        self.script = list(script or [])
        self.model = model
        self._turn = 0

    def next_turn(self, history) -> dict:
        """返回 {utterance, action?, done}。有脚本走脚本（确定、可复现）；否则调 Claude。"""
        if self.script:                       # 脚本模式
            step = self.script[min(self._turn, len(self.script) - 1)]
            self._turn += 1
            return {"utterance": step.get("utterance", ""),
                    "action": step.get("action"),
                    "done": self._turn >= len(self.script)}
        return self._ask_claude(history)      # 自由模式

    def drifted(self, history, world_entities) -> bool:
        """粗略 drift 判断：模拟用户最后一句引入了 world 里不存在的实体 → 视为漂移，该丢弃重跑。"""
        if not history:
            return False
        last = history[-1]["text"] if history else ""
        # 占位：接自由模式后可加更细的 goal-KL / 实体核对
        return False

    def _ask_claude(self, history) -> dict:
        import anthropic

        sys_prompt = _SYS.format(tools="、".join(USER_TOOLS))
        goal_reinject = f"[你的身份] {self.persona}\n[你的目标] {self.goal}"    # 每轮重注入，抗 drift
        msgs = [{"role": "user", "content": goal_reinject}]
        for h in history:                     # history = [{who, text}...]，who ∈ {user_sim, agent}
            role = "assistant" if h["who"] == "user_sim" else "user"
            msgs.append({"role": role, "content": h["text"] or ""})
        resp = anthropic.Anthropic().messages.create(
            model=self.model, max_tokens=300, system=sys_prompt, messages=msgs)
        return _parse(resp.content[0].text if resp.content else "")


def _parse(text: str) -> dict:
    out = {"utterance": text, "action": None, "done": False}
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                j = json.loads(line)
                out["action"] = j.get("action")
                out["done"] = bool(j.get("done"))
                out["utterance"] = text.replace(line, "").strip()
            except Exception:
                pass
            break
    return out
