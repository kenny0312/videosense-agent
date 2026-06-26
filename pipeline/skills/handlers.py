"""
Skill handlers —— route 的"执行端"。

两类东西住在这里:
  1. smalltalk_reply(question)：闲聊轮的回复【生成器】。取代过去写死的一句 SMALLTALK_REPLY ——
     用小模型按人设+能力边界生成一句【可变】的简短回复;任何失败 → 返回 None,由
     orchestrator 回退到固定俏皮回复(fail-open,绝不卡住)。
  2. HANDLERS：route.handler 键 → 自定义 workflow 函数 的分派表(打地基)。
     现阶段所有大类(retrieval/aggregate/analyze/visualize)的 handler 都是 "planner",
     走 orchestrator 里现有的 Planner→DAG 主链路,【不】经过这张表。
     将来某个大类要走【完全不同的 workflow】(比如情感分析有自己的多步流程)时:
        ① 写 skills/<name>.md,frontmatter 里 `handler: <key>`;
        ② 在下面 HANDLERS 注册 `<key>` → 你的函数。
     orchestrator 见到非 "planner" 的 handler 就自动来这张表查并调用,无需改它的判断逻辑。

不在 import 期碰 vertexai/GCP —— 真要生成时才惰性 import(与 Router/Planner 同风格)。
"""
from __future__ import annotations

import logging

from pipeline import config, usage

log = logging.getLogger("pipeline.skills.handlers")

_SMALLTALK_PROMPT = """你是 Kenny Qiu 旗下的"超级智能体",专做视频理解与分析。
现在用户在跟你闲聊/打招呼/问你是谁或你能做什么(不是真正的视频查询)。请用【一两句话】回应。

# 你的人设与能力(回应时自然带出,别像念清单)
- 自我定位:Kenny Qiu 的视频理解智能体。
- 你能做:搜视频、数数量、看内容、做数据分析、出图。
- 语气:友好、俏皮、简洁,带一点点个性。

# 硬边界
- 只做自我介绍 / 能力说明 / 寒暄。【绝不】回答与视频分析无关的实质问题(天气、代码、新闻、算题等)——
  遇到就友好地把话题引回"你想问关于视频的什么"。
- 用与用户【同一种语言】回应。
- 不要暴露任何内部组件名(router/planner/critic/DAG 等)。
- 只输出这句回复本身,不要加引号、不要解释。

# 用户这句:
{question}
"""


def smalltalk_reply(question: str) -> str | None:
    """生成一句贴合人设的【可变】闲聊回复;任何失败 → None(调用方回退固定回复)。"""
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        model = GenerativeModel(config.CRITIC_MODEL)
        # max_output_tokens 给足:gemini-2.5-flash 的"思考"token 也计入这个预算,
        # 卡太小(如 256)会让可见回复被思考吃掉、半句截断。回复长度靠 prompt 的"一两句话"约束。
        resp = model.generate_content(
            _SMALLTALK_PROMPT.format(question=question),
            generation_config={"temperature": 0.8, "max_output_tokens": 1024},
        )
        usage.add_usage(resp, config.CRITIC_MODEL)
        text = (getattr(resp, "text", "") or "").strip()
        return text or None
    except Exception as e:
        log.warning("smalltalk 生成失败,回退固定回复: %r", e)
        return None


# ── 自定义 workflow 分派表(route.handler 键 → 函数)──────────────────────
# 现在故意为空:四个内置大类都走 "planner"(orchestrator 里的主链路),不进这张表。
# 想加一个走自定义 workflow 的大类时,在这里注册即可。函数签名约定:
#
#     def my_workflow(nl: str, *, verdict, session, context, schema, resolved_ids) -> str:
#         '''返回给用户的答案文本。'''
#         ...
#
# 然后在对应的 skills/<name>.md 里写 `handler: my_workflow`。示例(模板,未启用):
#
#     def _echo_workflow(nl, *, verdict=None, **_):
#         return f"(示例 workflow)我收到的问题是:{nl}"
#
#     HANDLERS = {"echo": _echo_workflow}
HANDLERS: dict = {}
