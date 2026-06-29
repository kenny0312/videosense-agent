"""前置 Router(小模型 critic)—— planner 之前的【便宜分流门】:判可答性 + 意图 + 轮型。

记忆简化后职责收窄:Router 只做 **分流分类**,【不再解析指代】。
  - decision:  answer | refuse | smalltalk
  - intent / route:  归大类(决定走哪个 workflow)
  - turn_type: new | followup | meta —— 纯按问题的语言线索判断(有"这个/那个/上面/刚才"→followup;
               "你怎么算的"→meta;否则 new)。**具体指代哪条结果,交给 loop 用 transcript 回放自己解析**
               (回放里含完整 inputs/result_id);解析不到时由 loop 自己 clarify,不在这里提前拒答。

所以 Router 不再吃 catalog/history,也不产 references/resolved_to —— 一份持久 transcript 即记忆。
结构仿 sql_fixer / code_generator(vertexai + GenerativeModel),小模型 CRITIC_MODEL。
所有失败路径 fail-open(默认 answer)—— Router 出问题绝不卡正常查询。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

from pipeline import config, usage
from pipeline.code_generator import _strip_fence   # 复用同一套去围栏逻辑
from pipeline.skills import loader as skills        # 大类(route)注册表:词表 + route↔intent

log = logging.getLogger("pipeline.router")

REFUSE_MIN_CONFIDENCE = 0.5   # 低于此置信的 refuse 一律放行(fail-open,防误拒)

# 身份/打招呼/闲聊(decision="smalltalk")时,由 orchestrator 直接返回这句固定俏皮回复
SMALLTALK_REPLY = (
    "嘿 👋 我是 Kenny Qiu 旗下的超级智能体,专治各种视频理解难题。"
    "说吧,你想问关于视频的什么?——搜视频、数数量、看内容、做分析、出图,我都接。"
)


class RouterVerdict(BaseModel):
    decision:   str = "answer"          # "answer" | "refuse" | "smalltalk"
    confidence: float = 0.0
    reason:     str = ""
    intent:     str = "other"           # retrieve|aggregate|analyze|visualize|meta|other(兼容旧字段)
    route:      str = ""                # 大类任务名,取自 skills/*.md(决定走哪个 workflow);闲聊/元/拒答可空
    turn_type:  str = "new"             # new|followup|meta(指代具体哪条由 loop 回放解析,这里只分类)


def parse_verdict(raw: Any) -> RouterVerdict:
    """解析模型输出;任何异常 → 默认 answer(fail-open)。"""
    try:
        v = RouterVerdict.model_validate(raw)
        if v.decision not in ("answer", "refuse", "smalltalk"):
            v.decision = "answer"
        if v.turn_type not in ("new", "followup", "meta"):
            v.turn_type = "new"
        # route↔intent 互相回填(反向兼容:只给其一时补另一个,保证下游一致)。
        if not v.route and v.intent not in ("", "other"):
            v.route = skills.route_for_intent(v.intent)
        if v.route and v.intent in ("", "other"):
            v.intent = skills.intent_for(v.route)
        # 模型给了未知 route → 丢弃(避免分派到不存在的大类),但保留 intent。
        if v.route and v.route not in skills.known_routes():
            v.route = ""
        return v
    except Exception:
        return RouterVerdict(decision="answer", confidence=0.0,
                             reason="router parse failed -> fail-open")


def should_refuse(v: RouterVerdict) -> bool:
    """只有 refuse 且置信达标才真拒;否则放行(交给 planner)。"""
    return v.decision == "refuse" and v.confidence >= REFUSE_MIN_CONFIDENCE


# few-shot:用真 dict→json.dumps,避免 f-string 大括号转义。指代/元问题只判 turn_type,不填 resolved_to。
_FEWSHOT = [
    ("How many videos are there in total?",
     {"decision": "answer", "confidence": 0.95, "reason": "", "intent": "aggregate",
      "route": "aggregate", "turn_type": "new"}),
    ("Find all videos that contain skiing.",
     {"decision": "answer", "confidence": 0.9, "reason": "", "intent": "retrieve",
      "route": "retrieval", "turn_type": "new"}),
    ("who are you?",
     {"decision": "smalltalk", "confidence": 0.95, "reason": "", "intent": "other",
      "route": "", "turn_type": "new"}),
    ("plot start time vs confidence for those",   # 含"those"指代上文 → 追问;指哪批交给 loop 解析
     {"decision": "answer", "confidence": 0.85, "reason": "", "intent": "visualize",
      "route": "visualize", "turn_type": "followup"}),
    ("这个视频里有几个人?",                          # 含"这个"指代上文 → 追问
     {"decision": "answer", "confidence": 0.85, "reason": "", "intent": "analyze",
      "route": "analyze", "turn_type": "followup"}),
    ("how did you get that number?",               # 元问题 → meta
     {"decision": "answer", "confidence": 0.8, "reason": "", "intent": "meta",
      "route": "", "turn_type": "meta"}),
]

_SKELETON = {
    "decision": "answer|refuse|smalltalk", "confidence": 0.0, "reason": "一句话(与问题同语言)",
    "intent": "retrieve|aggregate|analyze|visualize|meta|other",
    "route": "<下面任务类别之一;闲聊/元问题/拒答留空>", "turn_type": "new|followup|meta",
}


def _router_prompt(question: str, schema: dict, tools: str) -> str:
    fewshot = "\n".join(f"问:{q}\n{json.dumps(a, ensure_ascii=False)}" for q, a in _FEWSHOT)
    return (
        "你是一个视频分析查询的【路由器】。判断下面这个问题能否用现有工具和数据库回答,"
        "若能答还要归到一个【任务大类(route)】,并判断它是【新问题/追问/元问题】。\n\n"
        f"# 数据库结构(只有这些表/列)\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"# 可用工具\n{tools}\n\n"
        "# 可用任务类别(route —— 给「能答的新/追问任务」挑最贴切的一个填入 route)\n"
        f"{skills.render_catalog()}\n\n"
        "# 判断规则\n"
        "- 轮型 turn_type:\n"
        "    · 指代上文(this/that/those/same/it/above/前面/刚才/上面/那批/第一个那个/这个 等)→ "
        "turn_type=\"followup\",decision=\"answer\"。【你只需判断「这是追问」,不要去猜具体指哪条】——"
        "完整对话上下文会在后续环节提供,由它精确解析;就算指代含糊也别拒,交给后续澄清。\n"
        "    · 元问题(你怎么得出/用了什么方法/上一条怎么算的)→ turn_type=\"meta\",intent=\"meta\","
        "decision=\"answer\"。\n"
        "    · 其余正常新问题 → turn_type=\"new\",decision=\"answer\"。\n"
        "- 问题要的数据/分析在上面的表和工具里【根本做不到】→ decision=\"refuse\",reason 说明做不到。\n"
        "- 身份/打招呼/闲聊(who are you / hi / 你是谁 / 你能做什么 等)→ decision=\"smalltalk\""
        "(系统会据此生成友好回复,你只需分类正确;route 留空)。\n"
        "- route:凡 decision=\"answer\" 的【新/追问任务】,都要从上面「可用任务类别」里挑【最贴切的一个】填进 route;"
        "闲聊、元问题(meta)、拒答一律把 route 留空 \"\"。\n"
        "- 【重要】拿不准时倾向 decision=\"answer\"(交给后面的环节),只在确信做不到时才 refuse;confidence 反映把握。\n"
        "- 所有 reason 用友好的产品口吻,【不要】暴露任何内部组件名(如 router/planner/critic)。\n\n"
        f"# 只输出 JSON(不要解释、不要 markdown),格式:\n{json.dumps(_SKELETON, ensure_ascii=False)}\n\n"
        f"# 例子\n{fewshot}\n\n"
        f"# 现在判断这个问题:\n{question}\n"
    )


class Router:
    def __init__(self) -> None:
        # 惰性导入(与 CodeGenerator/SqlFixer 同):import pipeline.router / orchestrator
        # (离线测试的传递依赖)不该把 vertexai 拉进来 —— 留到真正构造 Router 时再 import。
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        self.model = GenerativeModel(config.CRITIC_MODEL)

    def judge(self, question: str, *, schema: dict, tools: str) -> RouterVerdict:
        resp = self.model.generate_content(
            _router_prompt(question, schema, tools),
            generation_config={"response_mime_type": "application/json", "temperature": 0.0},
        )
        usage.add_usage(resp, config.CRITIC_MODEL)
        raw = _strip_fence(resp.text)
        try:
            data = json.loads(raw)
        except Exception:
            return parse_verdict(None)   # → fail-open answer
        return parse_verdict(data)
