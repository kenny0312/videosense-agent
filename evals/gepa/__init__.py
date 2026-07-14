"""GEPA:prompt 遗传进化(设计 docs/design/gepa-prompt-evolution.md §4/§4.5)。

独立于 evals 系统文件的子包 —— evals/ 是考场和尺子,evals/gepa/ 是考生进化器。
只消费 evals 的公开接口(runner.run_case / briefing.task_feedback / split_manifest),
不反向依赖;生产 prompt(loop_driver._LOOP_SYSTEM)byte-stable 不受影响,
所有变异只发生在本进程内存里,产出是【人审的 diff 报告】,不是自动上线。

文件流程(一轮进化):
    evolve.py   总指挥:CLI + 代际循环 + 三道闸的调度
    space.py    搜索空间:lessons 条文 + 工具声明的快照/应用/校验(宪法锁死)
    reflect.py  反思器:父本失败病历 → LLM → 一处修改提案(JSON,坏输出丢弃)
    frontier.py Pareto 前沿:分数矩阵 → 谁称王几题 → 加权抽父本
    gates.py    三道闸:minibatch 准入 / sign-test 显著性 / 预算台账
    state.py    簿记:候选谱系 + 分数矩阵 + 病历 + 偷看计数,落盘可续跑
产物:evals/gepa/runs/<运行id>/{state.json, events.jsonl, report.md}(gitignored)
"""
