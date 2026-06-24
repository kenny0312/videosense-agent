"""
前置 Router(小模型 critic)—— 在 planner 之前判断:这题能不能答 + 意图分类。

无记忆(单轮)时:catalog/history 为空 → have_memory=false,引用上文(this/above/那批…)一律 refuse。
多轮(已实现):judge() 收 history / artifact_catalog 视图并【渲染进 prompt】;指代能在已保存结果里
              对上号就解析(turn_type=followup/meta、references.resolved_to 填 artifact id),由
              orchestrator 再校验 id 真实性、把配方传给 planner;对不上号则照旧诚实拒答。
输出契约一开始就为多轮留好字段(turn_type / references),这轮只补输入与少量规则,未改契约。

结构仿 sql_fixer / code_generator(vertexai + GenerativeModel),用小模型 CRITIC_MODEL。
所有失败路径 fail-open(默认 answer)—— Router 出问题绝不卡正常查询。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import vertexai
from vertexai.generative_models import GenerativeModel
from pydantic import BaseModel, Field

from pipeline import config, usage
from pipeline.code_generator import _strip_fence   # 复用同一套去围栏逻辑

log = logging.getLogger("pipeline.router")

REFUSE_MIN_CONFIDENCE = 0.5   # 低于此置信的 refuse 一律放行(fail-open,防误拒)

# 身份/打招呼/闲聊(decision="smalltalk")时,由 orchestrator 直接返回这句固定俏皮回复
SMALLTALK_REPLY = (
    "嘿 👋 我是 Kenny Qiu 旗下的超级智能体,专治各种视频理解难题。"
    "说吧,你想问关于视频的什么?——搜视频、数数量、看内容、做分析、出图,我都接。"
)


class RouterVerdict(BaseModel):
    decision:   str = "answer"          # "answer" | "refuse"
    confidence: float = 0.0
    reason:     str = ""
    intent:     str = "other"           # retrieve|aggregate|analyze|visualize|meta|other
    turn_type:  str = "new"             # new|followup|meta(单轮恒 new)
    references: list[dict] = Field(default_factory=list)


def parse_verdict(raw: Any) -> RouterVerdict:
    """解析模型输出;任何异常 → 默认 answer(fail-open)。"""
    try:
        v = RouterVerdict.model_validate(raw)
        if v.decision not in ("answer", "refuse", "smalltalk"):
            v.decision = "answer"
        return v
    except Exception:
        return RouterVerdict(decision="answer", confidence=0.0,
                             reason="router parse failed -> fail-open")


def should_refuse(v: RouterVerdict) -> bool:
    """只有 refuse 且置信达标才真拒;否则放行(交给 planner)。"""
    return v.decision == "refuse" and v.confidence >= REFUSE_MIN_CONFIDENCE


# few-shot:用真 dict→json.dumps,避免 f-string 大括号转义。
# 基础例子(单/多轮通用)
_FEWSHOT_BASE = [
    ("How many videos are there in total?",
     {"decision": "answer", "confidence": 0.95, "reason": "", "intent": "aggregate",
      "turn_type": "new", "references": []}),
    ("Find all videos that contain skiing.",
     {"decision": "answer", "confidence": 0.9, "reason": "", "intent": "retrieve",
      "turn_type": "new", "references": []}),
    ("who are you?",
     {"decision": "smalltalk", "confidence": 0.95, "reason": "", "intent": "other",
      "turn_type": "new", "references": []}),
]
# 无记忆时:指代上文 / 元问题一律拒答
_FEWSHOT_NOMEM = [
    ("what is the first video above",
     {"decision": "refuse", "confidence": 0.9,
      "reason": "“above”指向上一轮的结果,我没有会话记忆,无法确定是哪条。",
      "intent": "retrieve", "turn_type": "new",
      "references": [{"text": "above", "resolvable": False, "resolved_to": None}]}),
    ("how did you decide that?",
     {"decision": "refuse", "confidence": 0.85,
      "reason": "这是关于先前分析的元问题,但我没有可参考的上一轮分析。",
      "intent": "meta", "turn_type": "new", "references": []}),
]
# 有记忆时:指代能在"已保存结果"里对上号 → 解析(turn_type=followup/meta、resolved_to 填 id)
_FEWSHOT_MEM = [
    ("plot start time vs confidence for those",   # 已保存结果里有 a1(上一轮的滑雪视频)
     {"decision": "answer", "confidence": 0.85, "reason": "", "intent": "visualize",
      "turn_type": "followup",
      "references": [{"text": "those", "resolvable": True, "resolved_to": "a1"}]}),
    ("how did you get that number?",              # 元问题,且有可参考的上一轮结果
     {"decision": "answer", "confidence": 0.8, "reason": "", "intent": "meta",
      "turn_type": "meta",
      "references": [{"text": "that number", "resolvable": True, "resolved_to": "a1"}]}),
]

_SKELETON = {
    "decision": "answer|refuse|smalltalk", "confidence": 0.0, "reason": "一句话(与问题同语言)",
    "intent": "retrieve|aggregate|analyze|visualize|meta|other", "turn_type": "new",
    "references": [{"text": "...", "resolvable": True, "resolved_to": None}],
}


def _router_prompt(question: str, schema: dict, tools: str,
                   history: list | None, catalog: list | None) -> str:
    have_memory = bool(history or catalog)
    mem_note = ("你有对话历史和已保存的上一轮结果(见下),可据此解析指代。" if have_memory
                else "你【没有】之前对话的记忆,也没有任何已保存的上一轮结果。")
    mem_block = ""
    if have_memory:
        mem_block = (
            f"# 对话历史(最近在后)\n{json.dumps(history or [], ensure_ascii=False)}\n\n"
            "# 已保存的上一轮结果(可被指代;每条有唯一 id,resolved_to 必须填这里出现过的 id)\n"
            f"{json.dumps(catalog or [], ensure_ascii=False)}\n\n"
        )
    fewshot_list = _FEWSHOT_BASE + (_FEWSHOT_MEM if have_memory else _FEWSHOT_NOMEM)
    fewshot = "\n".join(f"问:{q}\n{json.dumps(a, ensure_ascii=False)}" for q, a in fewshot_list)
    return (
        "你是一个视频分析查询的【路由器】。判断下面这个问题能否用现有工具和数据库回答,并分类意图。\n\n"
        f"{mem_note}\n\n"
        f"{mem_block}"
        f"# 数据库结构(只有这些表/列)\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"# 可用工具\n{tools}\n\n"
        "# 判断规则\n"
        "- 指代上文(this/that/those same/it/above/前面/刚才/上面/那批/第一个那个 等):\n"
        "    · 能在上面【已保存结果】里对上对应那条 → references 填 resolvable=true、resolved_to=该条 id,"
        "decision=\"answer\",turn_type=\"followup\";指代含糊或匹配多条时取【最近】(id 序号最大)那条。\n"
        "    · 没有记忆、或在已保存结果里找不到对应条目 → references 填 resolvable=false、resolved_to=null,"
        "decision=\"refuse\"。\n"
        "- 问题要的数据/分析在上面的表和工具里【根本做不到】→ decision=\"refuse\",reason 说明做不到。\n"
        "- 元问题(你怎么得出/用了什么方法/上一条怎么算的):有可参考的上一轮结果 → "
        "decision=\"answer\",intent=\"meta\",turn_type=\"meta\",resolved_to 指向那条;"
        "没有先前结果 → decision=\"refuse\",intent=\"meta\"。\n"
        "- 身份/打招呼/闲聊(who are you / hi / 你是谁 / 你能做什么 等)→ decision=\"smalltalk\"(系统会给固定友好回复,你只需分类正确)。\n"
        "- 其余正常新问题 → decision=\"answer\",turn_type=\"new\"。\n"
        "- 【重要】拿不准时倾向 decision=\"answer\"(交给后面的规划器),只在确信做不到时才 refuse;confidence 反映把握。\n"
        "- 所有 reason 用友好的产品口吻,【不要】暴露任何内部组件名(如 router/planner/critic)。\n\n"
        f"# 只输出 JSON(不要解释、不要 markdown),格式:\n{json.dumps(_SKELETON, ensure_ascii=False)}\n\n"
        f"# 例子\n{fewshot}\n\n"
        f"# 现在判断这个问题:\n{question}\n"
    )


class Router:
    def __init__(self) -> None:
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        self.model = GenerativeModel(config.CRITIC_MODEL)

    def judge(self, question: str, *, schema: dict, tools: str,
              history: list | None = None,
              artifact_catalog: list | None = None) -> RouterVerdict:
        resp = self.model.generate_content(
            _router_prompt(question, schema, tools, history, artifact_catalog),
            generation_config={"response_mime_type": "application/json", "temperature": 0.0},
        )
        usage.add_usage(resp, config.CRITIC_MODEL)
        raw = _strip_fence(resp.text)
        try:
            data = json.loads(raw)
        except Exception:
            return parse_verdict(None)   # → fail-open answer
        return parse_verdict(data)
