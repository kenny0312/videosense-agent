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
        from pipeline.agentops.trace import Trace
        from pipeline.agentops.treeguard import TreeGuard
        from sandbox.client import SandboxClient

        backend = EvalBackend(self.owner, world=self.task.get("world", "A")).install()

        u = self.task["user"]
        user = SimulatedUser(u.get("persona", ""), u.get("goal", ""), script=u.get("script"))

        schema = mcp_client.get_schema()
        # GD-0:runtime_facts 对齐生产(见 world.py 同处说明);多轮以首条 utterance 定语言指令。
        first_utt = (u.get("script") or [{}])[0].get("utterance", "")
        # has_image 必须跟着传:生产 orchestrator 传的是 has_image=image is not None,于是
        # runtime_facts 里会注入一整段「本轮附了图片…你【能看到它】…【绝不要】把它当成
        # 『只做视频』的超范围请求拒掉」。评测把图真送进去了却不注这一段 —— 贴图题量到的是
        # "没被授权的模型会不会自己接住图",生产量的是"被授权之后接不接得住",模型一句
        # "我只处理视频"就是确定性的 0 分。_first_image() 会现造 PNG,只调一次。
        first_image = self._first_image()
        rt = loop_driver.runtime_facts_line(None, nl=first_utt or None,
                                            has_image=first_image is not None,
                                            model=config.LOOP_MODEL)
        # 每一节都要传(见 world.py 同处说明 + tests/evals/test_eval_prompt_parity.py)
        from pipeline import library_state as _lib
        from pipeline import user_memory as _um
        lib = _lib.library_state_line()
        mem = _um.render_section(self.owner)
        notice, _ = loop_driver.task_done_notice(self.owner)
        conv = loop_driver.make_conversation(          # 同一个对话跨轮 → 多轮记性是真的
            config.LOOP_MODEL, loop_driver.loop_function_declarations(),
            loop_driver._loop_system(schema, None, rt, notice,
                                     library_state=lib, user_memory=mem),
            image=first_image)

        history, turns = [], []
        for turn_no in range(1, self.max_turns + 1):
            ut = user.next_turn(history)
            self._apply_action(ut.get("action"), backend, turn=turn_no)
            history.append({"who": "user_sim", "text": ut["utterance"]})
            turns.append(TurnRecord("user_sim", ut["utterance"], ut.get("action")))
            # 【每轮新建 executor + guard】—— 生产一次请求就是一棵树一本账
            # (run_query_loop 每次进来都重建)。以前建在循环外、8 轮共用一个:
            # analyze 配额(闭包里的 quota dict)与 per-tree 熔断会跨轮累积,
            # 多轮题从第 3 轮起会被评测自己的闸拦掉,而生产每轮都是满配额。
            tr = Trace()
            guard = TreeGuard(trace=tr)
            execute = loop_driver._make_executor(SandboxClient(), tr, schema, None,
                                                 owner=self.owner, guard=guard)
            critic = (loop_driver.make_self_check_critic()
                      if config.USE_SELF_CHECK_CRITIC else None)
            # req_short:生产每请求发一个新前缀(A2),让上一轮的 result_id 一眼可辨、不撞号。
            # 不传的话每轮都从 c0_0 重新起号,而 runner 把各轮 ledger 平铺合并成一本 ——
            # 同名键后覆盖前,第 1 轮摆出的视频会被末轮的顶掉,判分从台账取数就取错了行
            # (实测:两轮都用 cid c1_0,合并后只剩最后一轮那条)。
            r = loop_driver.run_loop(ut["utterance"], conv, execute,
                                     max_steps=self.task.get("max_steps", 16),
                                     guard=guard, critic=critic,
                                     req_short=f"t{turn_no}")
            # A1 连带:terminated != "text" 交的是系统占位文案,不是 agent 的回答。
            # 直接进判分会让文案里的"没能"命中 scorers._NEG_WORDS → expect_refusal 类白拿 1.0。
            atext = r.answer if r.terminated == "text" else ""
            # 终清洗照生产做(loop_driver.run_query_loop 里 scrub_ids 那一步):用户看到的
            # 永远是洗过的文本。单轮车道 world.py 已经补了,多轮这条一直没补 —— 同一个判分器
            # 在两条车道上用了两把尺子:漏 id 的题在多轮里记 0(生产会兜住),
            # 而 over-surface 惩罚又因为多数出几个 id 而偏严,两个方向都是假阴。
            if atext:
                atext, _ = loop_driver.scrub_ids(atext, (er.value for er in r.ledger.values()))
            history.append({"who": "agent", "text": atext or ""})
            turns.append(TurnRecord("agent", atext or "", trace=r.trace, ledger=r.ledger,
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
