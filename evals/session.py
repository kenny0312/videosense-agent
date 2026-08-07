"""多轮会话（真跑）：真 Gemini 当大脑 + 脚本用户，两边都能动共享的假世界。

每轮流程：用户轮（说话 + 可选动作：上传/入库/贴图）→ 动作真的落进假世界 →
agent 轮 → 记录答案/工具链 → 本轮落 transcript。
判分要用的账本（world_state：uploads/enriched/memory）由 EvalBackend 如实记录。

## 多轮记忆走【生产同款机制】,不是同一个对话对象跨轮(T1 保真修复)

生产的多轮是:每个用户轮 = 一次全新的 run_query_loop 请求(新 conversation),
跨轮连续性【只】靠 replay_context —— transcript 落盘 → 渲染 → 超预算再 LLM 压缩
(pipeline/loop_memory.build_loop_context)。以前评测图省事让 8 轮共用一个
conversation,拿的是【逐字原文历史】—— 量的是"拿着完整原始对话时的多轮能力",
生产跑的是"拿着渲染回放时的多轮能力",coherence/jga 一族分数系统性虚高;
且 conv 只建一次 → 第 2 轮起 library_state/user_memory/task_notice 全是首轮
快照、语言指令永远跟着第 1 句(用户第 2 轮切英文,指令还停在中文)。

现在每轮照抄生产 run_query_loop 的装配:重建 conversation(带回放节)、
重建 executor/guard(一轮一棵树一本账)、每轮 record_loop_turn 落进
InMemoryTranscriptStore(生产同一个写入器,连溢出预览的形状都一样)。

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


def _accumulate_usage(cum: "dict | None", this_turn: dict) -> dict:
    """生产 session.usage_cum 的替身:逐轮累计,形状对齐 runtime_facts_line 读的那几个键
    (turns / tokens_total / cost_usd / llm_calls / last)。纯函数,离线可测。"""
    base = cum or {"turns": 0, "tokens_total": 0, "cost_usd": 0.0, "llm_calls": 0}
    return {
        "turns": base["turns"] + 1,
        "tokens_total": base["tokens_total"] + int(this_turn.get("tokens_total", 0) or 0),
        "cost_usd": base["cost_usd"] + float(this_turn.get("cost_usd", 0.0) or 0.0),
        "llm_calls": base["llm_calls"] + int(this_turn.get("llm_calls", 0) or 0),
        "last": {"tokens_total": int(this_turn.get("tokens_total", 0) or 0),
                 "cost_usd": float(this_turn.get("cost_usd", 0.0) or 0.0)},
    }


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

        from pipeline import library_state as _lib
        from pipeline import loop_memory
        from pipeline import user_memory as _um
        from pipeline.agentops import usage
        from pipeline.transcript_store import InMemoryTranscriptStore

        schema = mcp_client.get_schema()
        first_image = self._first_image()
        # 多轮记忆的承载体:生产同款 transcript(内存后端本来就是给测试的)。
        store = InMemoryTranscriptStore()
        sid = "eval-mt"
        usage_cum: "dict | None" = None    # 生产 session.usage_cum 的替身(逐轮累计,喂自我认知节)

        history, turns = [], []
        for turn_no in range(1, self.max_turns + 1):
            ut = user.next_turn(history)
            self._apply_action(ut.get("action"), backend, turn=turn_no)
            history.append({"who": "user_sim", "text": ut["utterance"]})
            turns.append(TurnRecord("user_sim", ut["utterance"], ut.get("action")))

            # ── 每轮照抄生产 run_query_loop 的装配,一样不少 ──────────────
            # 回放节:transcript 尾渲染,超预算才 LLM 压缩。评测传一个【确定性】摘要器
            # (生产传 make_llm_summarizer):8 轮远够不到 LOOP_CONTEXT_TOKEN_BUDGET,
            # 这个摘要器实际永远不会被调 —— 传它是为了调用形状与生产同构
            # (test_eval_prompt_parity 的 ⊇ 规则罩着),不是为了省一次 LLM。
            replay_ctx = loop_memory.build_loop_context(
                store, self.owner, sid,
                summarize=lambda text: text[:1500] + "\n(…更早对话截断)")
            # 自我认知节:usage_cum 逐轮累计(生产是 session.add_usage);语言指令跟
            # 【当前】这句走 —— 以前钉死在第 1 句,用户第 2 轮切英文指令还停在中文。
            # has_image 只在真送图的那一轮(首轮)为 True,与生产 per-request 口径一致。
            rt = loop_driver.runtime_facts_line(usage_cum, nl=ut["utterance"] or None,
                                                has_image=(first_image is not None
                                                           and turn_no == 1),
                                                model=config.LOOP_MODEL)
            lib = _lib.library_state_line()             # 每轮现读(TTL 缓存兜住重查开销)
            mem = _um.render_section(self.owner)        # update_memory 后下一轮就要看得见
            notice, _ = loop_driver.task_done_notice(self.owner)
            conv = loop_driver.make_conversation(
                config.LOOP_MODEL, loop_driver.loop_function_declarations(),
                loop_driver._loop_system(schema, replay_ctx, rt, notice,
                                         library_state=lib, user_memory=mem),
                image=first_image if turn_no == 1 else None)
            # 一轮 = 一棵树 = 一本账(生产每请求重建;共用会让配额/熔断跨轮累积)。
            tr = Trace()
            guard = TreeGuard(trace=tr)
            execute = loop_driver._make_executor(SandboxClient(), tr, schema, None,
                                                 owner=self.owner, guard=guard)
            critic = (loop_driver.make_self_check_critic()
                      if config.USE_SELF_CHECK_CRITIC else None)
            usage.reset_usage()                          # 生产每请求 reset,会话层累计
            # req_short:生产每请求发一个新前缀(A2)。不传的话每轮都从 c0_0 起号,
            # runner 平铺合并 ledger 时同名键后覆盖前,判分取错行(实测)。
            r = loop_driver.run_loop(ut["utterance"], conv, execute,
                                     max_steps=self.task.get("max_steps", 16),
                                     guard=guard, critic=critic,
                                     req_short=f"t{turn_no}")
            # A1 连带:terminated != "text" 交的是系统占位文案,不是 agent 的回答。
            # 直接进判分会让文案里的"没能"命中 scorers._NEG_WORDS → expect_refusal 类白拿 1.0。
            atext = r.answer if r.terminated == "text" else ""
            # 终清洗照生产(scrub_ids):用户看到的永远是洗过的文本,transcript 记的也是它。
            if atext:
                atext, _ = loop_driver.scrub_ids(atext, (er.value for er in r.ledger.values()))
            # 本轮落 transcript(生产同一个写入器;blob_put=None → 大本体只留预览,
            # 与生产 GCS 不可用时的降级形状一致)。下一轮的回放节就从这里长出来。
            loop_memory.record_loop_turn(store, self.owner, sid, turn_no,
                                         ut["utterance"], r.trace, r.ledger, atext,
                                         blob_put=None)
            usage_cum = _accumulate_usage(usage_cum, usage.summarize())
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
