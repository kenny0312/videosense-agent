"""
videoUnderstanding — Pipeline 包

把大纲的 Stage 4 (Planner) → Code Generator → Stage 5/6 (Sandbox + 自愈)
串成一条真正的流水线,对外由 orchestrator.run_query() 暴露。

模块分工:
    config.py         中央配置(DB / GCP / Sandbox),消除三处重复
    mcp_client.py     真正的 MCP stdio 客户端(get_schema / query_db)
    dag_schema.py     DAG 节点类型定义 + Pydantic 校验
    node_specs.py     每种节点类型的元数据(描述 / 是否进沙箱 / codegen 提示)
    planner.py        自然语言 → DAG(Stage 4)
    code_generator.py 单节点 → Python 代码 + 自愈修复(Stage 6 的"生成"半边)
    node_executor.py  单节点执行:mcp_query 直走 MCP,其余 codegen→sandbox→自愈
    orchestrator.py   DAG 拓扑执行,把上面所有件装起来(Stage 10 的编排核心)
    trace.py          结构化 trace(实时打印 + 可序列化)
    main.py           完整流水线的 CLI 入口
"""
