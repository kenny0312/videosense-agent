# VS 长程引擎 · 总任务书 v2(2026-07-29,可独立开工版)

> **v2 变更**:按用户四点需求重构——①全链路 trace(测试失败能定位到设计层)②rollback 设计(新系统崩溃不伤原主 loop)③新版本性能测试集 ④自足可独立开工。新增 T/R/E 三章,执行顺序重排(观测与基线先行)。
> **一句话目标**:把 VS 升级为 CC 式长程视频分析 agent——主 loop 派活后在线,后台任务用受控递归树推进,完成后回流对话、可续作;深度凭实验赎买,钱/时间/故障半径写死在代码里。
> **文档族**(细则,本稿为总纲):①engine-design.md(树引擎)②multiagent-plan.md(Phase 0/1)③task-substrate-plan.md v1.2(底座)④design-review.md(独立评审)。
> **质量履历**:树引擎 52+36 agent 两轮审查;底座红队 23/24 修入;本稿 T/R/E 三章由既有已审查件组装。

---

# Part 0 · 总不变量(全工程红线,验收时逐条打勾)

1. **主 loop 零行为变化**:所有新能力挂在默认关的开关后;开关全关时,主 loop 对同一输入的行为与 main 基线**逐字节等价**(由 E-2 回放闸证明)。唯二例外见 R-1 例外清单。
2. **成本全口径可见**:每次 LLM/工具调用都入账(思考 token+表外模型 fail-loud);每棵树/每个任务有硬顶;死掉的执行也有审计行。
3. **每个失败路径必打因由码**(T-2):没有静默失败。
4. 不复活 router / 子 agent 只读 / 懒惰按需 / 简化优先(零新依赖;唯一新组件 Cloud Tasks 且有 inline 降级)。

---

# Part T · 全链路 Trace(测试失败 → 直接指认设计层)

### T-1 一条链贯穿三个世界(~60 行,并入 P0-2 实施)

关联键链:`request_id → session_id → [task_id →] wave_n → span_id(parent_id, depth)`。
每个 span 必带:`component`(见 T-2 枚举)、t_start/t_end、tok{in,out,thought,cached}、cost_usd、status(ok|softfail|error|refused)、cause(失败时必填)。
落点:TraceStep 扩展(trace.py:22-31 现状扁平)+ 底座 task_wave_audit(S-3 已含)+ 子 agent span 不再裸共享父对象(评审 A5)。

### T-2 因由码枚举(核心交付,~30 行常量+打点)

**设计层 → 因由码**的固定映射,失败路径必打其一:

| 设计层 | component | 因由码(枚举,禁自由文本) |
|---|---|---|
| 触发/gate | gate | GATE_MISFIRE(误拆)/ GATE_MISS(漏拆,由探针题标注) |
| 分解/规划 | plan | PLAN_EMPTY / PLAN_OVERSPLIT(>上限截断)/ PLAN_BAD_TASK(空 instruction 剔除) |
| 子 agent 执行 | exec | EXEC_TOOL_ERROR / EXEC_NO_EVIDENCE(弃权)/ EXEC_TRAP(防 Trap 拍肩触发)/ EXEC_NOT_CONVERGED |
| 聚合/上传 | agg | AGG_TRUNCATED / AGG_EVIDENCE_FAIL(Phase 2 校验回退原文) |
| 护栏 | guard | GUARD_BUDGET / GUARD_WALL / GUARD_NODE_CAP / GUARD_CANCELLED |
| 底座 | sub | SUB_CLAIM_RACE(503 路径)/ SUB_ZOMBIE_DISCARD(CAS 丢弃)/ SUB_ENQUEUE_FAIL / SUB_LEASE_TIMEOUT |
| 回流/续作 | relay | RELAY_DUP_SUPPRESSED / RELAY_REPORT_MISS |

### T-3 诊断工具 `tools_ops/trace_report.py`(~120 行,$0)

两个命令:`--trace <request_id|task_id>` 拼出全树时间线(每 span 一行:层/耗时/花费/状态/因由);`--triage <结果目录>` 按因由码聚合失败分布——**测试挂了先跑这个,输出直接回答"问题出在哪一层设计"**。DVD 侧五次验尸的脚本经验直接沿用。
**验收(故障注入)**:人为制造 4 种故障(子 agent 抛错 / 强制熔断 / 波超时 kill / enqueue 失败),triage 各自指认正确的 component+cause,一条命令内。

---

# Part R · Rollback(新系统崩了,主 loop 无恙)

### R-1 爆炸半径矩阵(文档+断言,~20 行)

| 开关(全部默认关) | 关掉后 | 影响残留 |
|---|---|---|
| USE_DEPTH2=0 | 树退回一层 fan-out(现状) | 零 |
| USE_SUBAGENTS=0 | 连一层树也没有,纯单脑 | 零 |
| USE_TASKS=0 | 任务端点 404,advance 拒收,已有任务冻结在库(不删) | 库里静态行,无执行 |
| USE_TASK_TOOL=0 | 主脑看不见立项工具 | 零 |
| MAX_TREE_COST_USD=0 | 熔断关(仅实验用) | 零 |
| TASKS_DRIVER=inline | 脱离 Cloud Tasks | 零 |

**例外清单(唯二改既有行为的,单独回滚方式)**:①P0-1 usage 计价修复——影响=记账数字变准(不改行为),回滚=revert 单 commit;②loop_driver 过滤链/node_specs 各加数行——回滚=revert,且 E-2 证明开关关时行为等价。
**代码断言**:启动时若 USE_TASKS=1 但 Cloud Tasks 探活失败 → 自动降级 USE_TASKS=0 + 告警日志(fail-safe 不 fail-open)。

### R-2 三层回滚路径

①**运行时**(秒级,免部署):env 开关全关 → 现状;②**数据**:新表(agent_tasks/agent_task_events)完全独立,不触碰既有七表,`DROP TABLE` 即净(cleanup 脚本带 dry-run,家规);③**部署**:Cloud Run revision 一条命令回上一版(DEPLOY.md 已有实践,runbook 里写死命令)。

### R-3 金丝雀与撤退剧本(~半天)

放量顺序:shadow(USE_TASKS=1 仅 owner=自己)一周 → 全量。**撤退 runbook(三步,写进 DEPLOY.md)**:关开关 → 跑 E-3/B1 冒烟确认主 loop 与基线一致 → trace_report --triage 出验尸报告。
**验收(回滚演练,进总验收闸)**:全开→制造一次任务故障→执行 runbook→B1 二十题结果与基线零差异→新表 DROP→主 loop 无感。

---

# Part E · 测试集(新版本性能的三层尺子)

### E-1 离线单测层($0,CI 每次必跑)

现有 pytest 模式延伸(DVD 侧 62 条的纪律):底座=红队 23 场景全部固化为单测(三态分流/CAS 丢弃/终态清租约/resume 推进/立项幂等/预估入账);树引擎=depth 封顶/节点 13/熔断双挂点/五判据探针;判分器/trace 因由码打点。**红队发现→单测,一条不许口头闭环。**

### E-2 回放闸层($0,行为等价的证明工具)

①检索五口径回放(replay_search.py 已有,继续当合并闸);②**主 loop 回归回放(新,~100 行)**:冻结 20 条历史 query 的完整工具调用序列快照(从现有 trace 采),新版本开关全关跑同批 query,对比工具序列与最终答案——**这就是 Part 0 不变量 1 与 R 的证明**。允许白名单差异(时间戳/成本数字),其余不等价即 CI 红。

### E-3 能力基准层(花钱,题库冻结后不改)

| 套件 | 内容 | 用途 | 成本/次 |
|---|---|---|---|
| **B1 主 loop 冒烟 20 题** | 现库日常题(检索/单视频/计数/展示各 5),gold 人工冻结 | 每次发版+回滚演练;阈值:与基线差 ≤1 题 | ~$1 |
| **B2 gate 实验 18 题** | 即 P0-7(9 T1+9 T2+2 弃权探针,dev/holdout) | Phase 1 判停专用 | $42(一次性) |
| **B3 长任务端到端 3 剧本** | ①8 视频报告全程(六事故穿越)②完成回流+续作③预算暂停+resume | 底座发版验收 | ~$3 |

判分纪律全套沿用(judge κ≥0.7 才上岗/冻结不改/n≥2 翻转可见/Δ 必带 Fisher p+Wilson 区间/改判分规则→全套件重判)。
**基线快照(先行件)**:动工前在当前 main 上跑 B1+B3 存 `results/baseline_v0/`——**没有基线,之后一切"变好/变坏"都是空话。**

### E-4 长程多任务基准 VS-LH Bench(细则见 longhorizon-bench-design.md;+1.5 天,FULL ~$25 / LITE ~$4)

八任务族:F1 库级清点 / F2 深挖聚合(复用 B2 十八题及其三臂数据)/ F3 报告合成 / F4 事故穿越(结局奇偶性)/ F5 中途转向(trace 断言)/ F6 并发多任务(账本隔离)/ F7 续作迭代(成本<50% 首作)/ F8 诚实探针(零编造)。F1-F3 四臂对照,F4-F8 只测底座。对 ROMA 基准的修正:带消融臂、全口径成本、judge 校准、弃权一等公民。LITE 档每次发版跑;FULL 档大版本前后各一次。

---

# Part S · 施工线(五条线,细则见文档族)

**线 0 文档修正(0.5 天)**:D-1 五处"预拆全树"改"急切逐节点分解(配置默认 5/代码默认 2)";D-2 补 unpriced fail-loud 与 C1 引用;D-3 40% 补条件/停更加时间戳/协议补 bootstrap。

**线 1 Phase 0 七件(5 天,$0)→ Phase 1 gate($42-50)**
P0-1 账本(思考 token+表外 fail-loud,**最先**)→ P0-2 树状 trace(**与 T-1/T-2 合并实施**)→ P0-3 $熔断+墙钟(双挂点/软收口/wasted)→ P0-4 五判据(+**该拆探针**)→ P0-5 video_id 硬过滤 → P0-6 depth-2(≥2 工具才下钻/L2=3/全树 13)→ P0-7 题库=E-3 B2。
Phase 1 三臂(判停冻结:T2 上 C−B≥+0.10 且中位≤2×B 且 P90≤3×B 且熔断率≤30%;B+ 算力对照/T1 阴性对照/cap-受限判不可判/验尸后出结论/措辞带"flash 条件下";$2 dry-run/$25 半程/$50 硬顶)。**"不建"是合法结局。**

**线 2 任务底座 S-1~S-10(6-8 天,~$3)**(细则 substrate-plan v1.2)
S-1 两表+状态机(稳定 id/lease_token/终态清租约)→ S-2 四端点(立项幂等/fail-closed/guest 403)→ **S-3 波次推进心脏**(OIDC/认领三态分流:租约被占→503/第 0 波规划/usage 显式 reset/executor 包预算取消/CAS 提交/命名任务自愈/死波必审计)→ S-4 预算闸(悲观预估入账/resume=新 cap+清租约+投递/实账回灌 ratelimit)→ S-5 前端(必做仅"重推"按钮)→ S-6 工具位(判据零美元知识)→ S-7 沉淀钩子(诚实版)→ S-8 enrich 迁移(尾件,关 ops P2-6)→ **S-9 完成回流**(context 注入一行+get_task_report)→ **S-10 续作**(parent_task_id+父报告注入规划波)。

**线 3 serving 前置件(gate 后、放量前,1-2 天)**:SV-1 --timeout≥1.5×墙钟;SV-2 断连协作取消(修"Stop 是假的");SV-3 SSE 心跳+粗进度;SV-4 任务化触发指标化。

**线 4 Phase 2 赎买(各自 A/B)**:
- **PH2-1 树内 DAG 依赖调度**(tasks 加 dependencies 整数索引;Kahn 波次;上游失败整枝作废;产出经 context_input 单向只读注入)——覆盖**机械传递型**依赖(交接处无判断);判断型交接仍归主脑杠铃(特性不是妥协);全局重想归 S-3b。**可观测触发器(T 层直出)**:主脑多轮 spawn 中"纯机械交接轮"(某轮 tasks 可由上轮结论直接推导、主脑轮间零取舍,trace 断言可判)占比 >30% 即立案。注:gate 单变量纪律只要求**实验期间 USE_AGENT_DAG=0**,不禁止代码先写——观测提前达标可在底座线并行实现,开关照旧默认关。
- PH2-2 专职 Aggregator(先答"当初为何删 LLM 汇总"+证据双向校验);PH2-3 模型静态分层;PH2-4 judge groundedness。

**背包 A(需新预算)**:1h+ 视频档 $9-11 / 注册表稳定编号重建 $3.2 / BudgetGuard 手搓 $0。**背包 B(用户冻结中)**:strong 闸/LOW 档/转写时间戳(唯一今天在伤用户)/in-video search。

---

# Part X · 执行顺序、预算、总验收

## 执行顺序(单人,15-18 人日)

```
D1     线0 + E-3基线快照(main 上跑 B1+B3 存档)★没有基线一切对比作废
D2     P0-1 账本(一切成本判断的地基)
D3-6   P0-2+T-1/T-2/T-3(观测先行) → P0-3~P0-7;E-1 单测随件交付
D7-12  底座 S-1~S-7、S-9/S-10(E-1 红队场景单测随件;穿插 Phase1 dry-run)
D13    E-2 主 loop 回归回放搭好并首跑(证明不变量1)
D14-16 Phase 1 主跑+验尸+裁决
裁决后  线3 serving → R-3 金丝雀(shadow一周)→ 回滚演练 → 视裁决翻 USE_DEPTH2
之后    Phase 2 按数据立案;S-8 尾件
```

## 预算

| 项 | $ |
|---|---|
| E-3 基线快照(B1+B3 于 main) | ~$4 |
| Phase 1 gate 实验 | $42-50 |
| 底座验收(B3 全程) | ~$3 |
| P0-4 探针 + 杂项 | ~$2 |
| **合计** | **~$51-59** |

## 总验收闸(全部通过才算交付)

1. **T**:四种注入故障,triage 一条命令指认正确设计层;
2. **R**:回滚演练全流程,B1 与基线零差异,新表 DROP 主 loop 无感;
3. **E**:E-1 全绿;E-2 开关全关行为等价;B1 ≤1 题差;B3 三剧本(六事故穿越/回流续作/预算暂停恢复)全过且双审计对齐;
4. **Phase 1**:按冻结判据出裁决并归档(建或不建都算完成);
5. Part 0 四条不变量逐条打勾。
