"""
videoUnderstanding — Pipeline 包

单一大脑 probe-and-step loop:大脑逐步调用【无状态工具】直到用纯文本收口。
对外由 orchestrator.run_query() 暴露。(旧 Planner→DAG→CodeGen 流水线已退役。)

模块分工:
    config.py         中央配置(DB / GCP / Sandbox / 各模型档 / 特性开关)
    mcp_client.py     MCP stdio 客户端(get_schema / query_db)
    loop_driver.py    loop 核心:run_loop(控制流)+ 宪法/声明/执行器组装
    node_specs.py     每个工具的声明(用途 / 是否进沙箱 / 参数 schema)
    dag_schema.py     工具名(ToolName)+ Node 定义 + Pydantic 校验(loop 复用)
    node_executor.py  单工具执行:主进程 MCP/内建 handler,或 codegen→沙箱→自愈
    code_generator.py 沙箱工具(plot/python)→ Python 代码 + 自愈修复
    subagents.py      spawn_agents 子 agent 异质 fan-out(opt-in)
    orchestrator.py   请求级封装:回放/usage/收口,调 loop_driver.run_query_loop
    trace.py          结构化 trace(实时打印 + 可序列化)
    main.py           dev CLI 入口(跑同一 loop,看 trace)
"""
