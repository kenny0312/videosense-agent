"""DualControlSession —— τ² 式多轮 dual-control：真 Gemini agent + 模拟用户，两边都能动共享状态。

多轮靠复用同一个 GenAIConversation 实现（chat 持续 = 多轮记忆）。每轮：
  用户轮（模拟用户 → utterance + 可选 USER_TOOL action）→ 施加用户动作到 world → agent 轮（真 Gemini）→ 收集
直到用户 done 或到 max_turns。产出每轮 trace/answer，供 JGA / state-diff 判分。

需要 GCP 凭证（agent）+（自由模式）ANTHROPIC_API_KEY（用户）。默认不跑 —— 先建系统。
upload/enrich 对 mock DB 的真正写入标了 TODO，接真执行器时补。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evals.simulated_user import SimulatedUser


@dataclass
class TurnRecord:
    who: str                       # "user_sim" | "agent"
    text: str
    action: dict | None = None
    trace: list = field(default_factory=list)


class DualControlSession:
    def __init__(self, task: dict, owner: str = "eval", max_turns: int = 8, use_mock_db: bool = True):
        self.task = task
        self.owner = owner
        self.max_turns = max_turns
        self.use_mock_db = use_mock_db

    def _apply_user_action(self, action, world_state: dict):
        """把用户侧动作施加到共享状态。"""
        if not action:
            return
        kind = action.get("type")
        if kind == "upload_video":
            world_state.setdefault("uploads", []).append(action.get("video_id"))
        elif kind == "enrich_video":
            world_state.setdefault("enriched", []).append(action.get("video_id"))
            # TODO: 真正往 content_embeddings / mock DB 插一行（接真执行器后补）
        # paste_image / correct 在 utterance / 首消息里体现

    def run(self):
        import os

        from pipeline import config, loop_driver, mcp_client
        from pipeline.trace import Trace
        from sandbox.client import SandboxClient

        if self.use_mock_db:
            os.environ.setdefault("REPL_USE_MOCK_DB", "1")

        u = self.task["user"]
        user = SimulatedUser(u.get("persona", ""), u.get("goal", ""), script=u.get("script"))

        schema = mcp_client.get_schema()
        conv = loop_driver.make_conversation(          # 单个 conv 跨轮复用 → 多轮记忆
            config.LOOP_MODEL, loop_driver.loop_function_declarations(),
            loop_driver._loop_system(schema, None, None))
        execute = loop_driver._make_executor(SandboxClient(), Trace(), schema, None, owner=self.owner)

        world_state, history, turns = {}, [], []
        for _ in range(self.max_turns):
            ut = user.next_turn(history)
            self._apply_user_action(ut.get("action"), world_state)
            history.append({"who": "user_sim", "text": ut["utterance"]})
            turns.append(TurnRecord("user_sim", ut["utterance"], ut.get("action")))
            r = loop_driver.run_loop(ut["utterance"], conv, execute, max_steps=self.task.get("max_steps", 16))
            history.append({"who": "agent", "text": r.answer or ""})
            turns.append(TurnRecord("agent", r.answer or "", trace=r.trace))
            if ut.get("done"):
                break
        return {"turns": turns, "world_state": world_state, "history": history}
