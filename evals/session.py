"""多轮会话（真跑）：真 Gemini 当大脑 + 脚本用户，两边都能动共享的假世界。

每轮流程：用户轮（说话 + 可选动作：上传/入库/贴图）→ 动作真的落进假世界 →
agent 轮（同一个对话跨轮，记性是真的）→ 记录答案/工具链。
判分要用的账本（world_state：uploads/enriched/memory）由 EvalBackend 如实记录。

贴图说明：图在第 1 轮随首条消息送给大脑（走和线上一样的入口）；
图的内容是我们生成的说明图 —— 测"图送没送到、大脑接没接住"，不测真实视觉识别。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evals.simulated_user import SimulatedUser
from evals.world import EvalBackend, make_note_image


@dataclass
class TurnRecord:
    who: str                       # "user_sim" | "agent"
    text: str
    action: dict | None = None
    trace: list = field(default_factory=list)
    ledger: dict = field(default_factory=dict)     # cid -> ExecResult（交付面判分用）
    llm_calls: int = 0                             # 这一轮调了几次大脑（算花费用）


def _action_name(action) -> str | None:
    """动作名兼容 tool / type 两种写法。"""
    if not action:
        return None
    return action.get("tool") or action.get("type")


class DualControlSession:
    def __init__(self, task: dict, owner: str = "eval", max_turns: int = 8):
        self.task = task
        self.owner = owner
        self.max_turns = max_turns

    def _first_image(self):
        """第 1 轮如果有贴图动作，造一张说明图随首条消息送入。"""
        script = self.task.get("user", {}).get("script", []) or []
        if script and _action_name(script[0].get("action")) == "paste_image":
            ref = (script[0]["action"].get("ref") or "screenshot").replace("_", " ")
            return make_note_image(f"[user pasted image] {ref}")
        return None

    def run(self):
        from pipeline import config, loop_driver, mcp_client
        from pipeline.trace import Trace
        from sandbox.client import SandboxClient

        backend = EvalBackend(self.owner).install()

        u = self.task["user"]
        user = SimulatedUser(u.get("persona", ""), u.get("goal", ""), script=u.get("script"))

        schema = mcp_client.get_schema()
        # GD-0:runtime_facts 对齐生产(见 world.py 同处说明);多轮以首条 utterance 定语言指令。
        first_utt = (u.get("script") or [{}])[0].get("utterance", "")
        rt = loop_driver.runtime_facts_line(None, nl=first_utt or None)
        conv = loop_driver.make_conversation(          # 同一个对话跨轮 → 多轮记性是真的
            config.LOOP_MODEL, loop_driver.loop_function_declarations(),
            loop_driver._loop_system(schema, None, rt),
            image=self._first_image())
        execute = backend.wrap_execute(
            loop_driver._make_executor(SandboxClient(), Trace(), schema, None, owner=self.owner))

        history, turns = [], []
        for turn_no in range(1, self.max_turns + 1):
            ut = user.next_turn(history)
            self._apply_action(ut.get("action"), backend, turn=turn_no)
            history.append({"who": "user_sim", "text": ut["utterance"]})
            turns.append(TurnRecord("user_sim", ut["utterance"], ut.get("action")))
            r = loop_driver.run_loop(ut["utterance"], conv, execute,
                                     max_steps=self.task.get("max_steps", 16))
            history.append({"who": "agent", "text": r.answer or ""})
            turns.append(TurnRecord("agent", r.answer or "", trace=r.trace, ledger=r.ledger,
                                    llm_calls=r.llm_calls))
            if ut.get("done"):
                break
        return {"turns": turns, "world_state": backend.world_state, "history": history}

    def _apply_action(self, action, backend: EvalBackend, turn: int = 1):
        """用户动作落进假世界（说话/纠正不用落；贴图在会话开头已处理）。"""
        name = _action_name(action)
        if name == "upload_video":
            backend.upload(action.get("video_id", "up_new"),
                           title=action.get("title", ""),
                           activities=action.get("activities"),
                           duration=float(action.get("duration_sec", 30)))
        elif name == "enrich_video":
            backend.enrich(action.get("video_id", ""))
        elif name == "paste_image" and turn > 1:
            # 目前只支持首轮贴图（随首条消息送入）；放在后面轮会被静默丢掉——
            # 出这种题等于测了个寂寞，直接炸出来让出题人改题
            raise ValueError(f"paste_image 只能放在第 1 轮（现在在第 {turn} 轮）——图不会真的送给 agent")
