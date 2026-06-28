# VideoSense：DAG → probe-and-step Loop 迁移 · Roadmap

> 配套设计:[dag-to-loop-migration.md](dag-to-loop-migration.md)。本表是**执行顺序 + 里程碑 + 验收**。
> 原则:**先纯加法 / 可灰度并存,后拆旧路;高风险未知项用 spike 前置 de-risk。**

**依赖:** `M0 → M1 → M2(spike) → M3 → {M4, M5} → M6 → M7`

---

## M0 · 决策与基线 〔gate〕
- [ ] 拍板设计文档 §8 的 5 个开放问题:① `Node`/`topo_order` 去留 · ③ 耐久真相用 GCS 还是隔离 Neon 表 · ④ compaction 策略 · ⑤ API/流式;**② 多输入句柄留到 M2 spike 验**
- [ ] 设计文档 + 本 roadmap 合入 repo(走 PR)
- [ ] 开一个 tracking issue(把本表搬成 checklist)
- **验收:** 决策记录在案,文档 merged

## M1 · 工具声明（纯加法,零风险）
- [ ] `node_specs.SPECS` 每个工具补结构化 `parameters` schema
- [ ] `build_function_declarations(SPECS)`
- [ ] 单测:每工具产出一个合法 `FunctionDeclaration`
- **验收:** **不碰运行时,DAG 路径全绿**

## M2 · Spike：Gemini 原生 function-calling 循环（de-risk）
- [ ] 最小 `run_loop`:2–3 个工具(`sql_query`+`plot`),复用 `execute_node`
- [ ] 验**开放问题②**:多输入句柄(`left/right_result_id`)可靠性
- [ ] 验**开放问题③**:loop 大脑模型选型(`CRITIC_MODEL` vs `PLANNER_MODEL`)
- **验收:** 一条 query 端到端跑通;②③ 有结论

## M3 · Loop 驱动 + 灰度开关
- [ ] `pipeline/loop_driver.py:run_loop()` 全量
- [ ] env 开关 `VS_EXECUTOR=dag|loop`(前置段两路共享)
- [ ] 护栏:`MAX_STEPS` + 重复失败检测
- **验收:** flag 下 loop 路径过现有查询集;**DAG 仍默认**

## M4 · 存储：transcript append + GCS 溢出
- [ ] transcript 存储层:Redis Stream 热尾 + GCS 全量 `.jsonl` + GCS `tool-results/`;**`owner:session_id` 作用域**
- [ ] `append_event` 写入器(确定性路由,见设计 §3.1)
- [ ] GCS Lifecycle 规则(`transcripts/` 30d / `tool-results/` 7d)
- **验收:** transcript 落盘、取尾续轮可用、清理生效

## M5 · 记忆改造：删 recipe + catalog 派生 + 回放 + compaction
- [ ] 删 `_derive_recipe` / `recipe` 字段;`catalog` → 派生索引
- [ ] `_context_block` / `_explain_meta` 改回放;LLM compaction
- [ ] **迁移** `planner.plan` 的 validate/repair + `planner.schema`(风险项,不能净删)
- [ ] `value_cached` / `load_artifact` 保活(miss 软回喂)
- **验收:** followup / meta 经 transcript 正常工作

## M6 · 观测 + API/前端
- [ ] Trace 落服务端;`_audit` 加 `step_count` / 工具直方图 / `terminated_reason`
- [ ] API:`dag` 字段换 transcript;(可选)SSE 流式
- [ ] `web/index.html` 渲染改;Cloud Run `--timeout` 调高
- **验收:** 失败 run 可重建;前端渲染 loop 输出

## M7 · 灰度对比 → 切换 → 拆 DAG
- [ ] 灰度跑,对比 latency / 正确率(成本不计)
- [ ] loop ≥ DAG → 翻默认
- [ ] 删 `Planner.plan`(DAG 发射)/ `dag_schema.topo_order` / `_validate_graph` / `parse_dag`
- **验收:** DAG 下线,loop 成唯一路径

---

## 贯穿全程
- [ ] 每个里程碑保现有测试套件绿(灰度期 DAG 路径不退化)
- [ ] M5 重写 recipe/DAG 形断言(`test_multiturn` 等)
- [ ] 沙箱 `_check_policy` + 潘多拉隔离 每步不破(加测试断言)
