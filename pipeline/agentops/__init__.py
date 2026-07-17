"""
pipeline.agentops — AgentOps 横切关注点(运维 / 可观测 / 成本护栏)

把「看住系统、别烧穿钱、能回放」这类【不属于 agent 核心业务逻辑】的横切模块收拢到一处,
免得根目录越堆越乱。核心 loop / 检索 / 存储仍在 pipeline/ 顶层。

模块分工:
    ratelimit.py   滥用/账单护栏:按成本$ + 速率双口径,纵深四维(IP/用户/会话/全局)限流
    trace.py       结构化 trace(实时打印 + 可序列化):一次请求的完整工具调用链
    usage.py       token / 成本记账:每次 LLM 调用累加,summarize() 出单请求账单

后续 MLOps 件(预算熔断、告警、指标导出、Langfuse 适配器…)也往这里落。
这些都是【叶子模块】—— 只向下依赖 config/redis_client,不反向依赖核心,故无循环 import。
"""
