# VS 长程多 agent 引擎 · 实施任务书(v1 定稿)

> 本任务书自足:所有事实已核实到 file:line(分支 feat/evals-hardening 实况),不依赖任何历史对话。带"(红队修正)"标注的条目是红队评审后强制吸收的修改,不得回退。执行方式:按 Phase 顺序推进,每个任务做完对照验收标准打钩,Phase 1 是硬 gate——实验不过闸,Phase 2 不建。

---

# 0. 为什么长程任务非多 agent 不可(一页)

**现状**:VS 是单脑单循环(Gemini flash + 11 工具),`spawn_agents`(pipeline/subagents.py)已提供一层只读异质 fan-out(白名单 4 工具、一层不递归、≤6 子 agent、复用父 execute 闭包共享配额),默认关(`USE_SUBAGENTS=0`,config.py:98)。

**为什么要长程引擎**:单循环有三个结构性天花板,靠加步数解不了——
1. **上下文有界**:514 条库级聚合题("全库找 X 并逐条定位")的证据量塞不进一个循环的历史;
2. **压缩必须有损**:所有中间结果原样灌回主脑 → prompt 膨胀 → Trap 税(DVD 实测成本方差 12×,中位 $0.089/最大 $1.10);
3. **状态活在 prompt 里**:进度、已查/未查、失败分支全靠模型自己记,长任务必丢。

**三机制**(长程引擎的本质):**分解**(每个节点上下文有界)+ **聚合**(有损压缩上传,只传结论+证据引用)+ **状态外置**(树 + ledger 引用 = 程序状态)。

**对偶代价**:错误乘法——上游一个坏结论会被下游放大。所以三必须件跑前锁死:聚合边界校验/弃权、per-tree 美元熔断、树状 trace。

**实测底座**(DVD 复现,2026-07):扁平 agent 循环 0.90 > 整片直喂 0.81;agency-Δ +0.20~0.33;但 30min 档 flash 直喂已抹平——**主场在 1h+ 视频与库级聚合**。递归(depth≥2)的增量**未被验证**(ROMA 学习结论),这正是 Phase 1 要用钱买的答案。

**首发赛道**:库级聚合调查(514 条现语料,零新增成本,天然两层:层1 按视频分片圈候选 / 层2 视频内下钻定位)。1h+ 长视频次发($9-11 语料预算另批);复合交付物远期。

---

# 1. 总路线图

```
Phase 0 地基(4-5 天, 必做, 无 gate)
  P0-1 usage 思考token修复 ──┬─→ P0-3 per-tree 熔断(含大脑闸)
  P0-2 树状 trace ───────────┘        │
  P0-4 spawn gate prompt              │
  P0-5 semantic_search video_id 过滤  │
  P0-6 strong 闸信封补时间戳          │
  P0-7 裸 depth-2(默认关)←──────────┘
        ↓
Phase 1 深度2三臂实验(5-7 天, ~$41, 【gate】)
  单变量 USE_DEPTH2, 判停条件跑前写死, 结论无论方向都归档
        ↓ 过闸才继续
Phase 2 完整引擎(过闸后单独立案)
  B DAG调度 / C Aggregator蒸馏 / G 模型分层 / 复合任务
```

**(红队修正 E1/E2/C2)** 原设计把 B(DAG)、C(蒸馏)、G(分层)排在实验前合入,红队判定为归因链断裂 + 工作量低估 2-3×:实验测的必须是**裸 depth-2 单变量**,B/C/G 对一层 fan-out 同样适用、不必绑在递归 gate 上,全部移出本期到 Phase 2。本任务书按砍完的范围写,总工时 10-14 个工作日可信。

---

# 2. Phase 0 逐任务

## P0-1 usage 思考 token 修复(最先,任何计费验收之前)

- **落点**:`pipeline/agentops/usage.py:54-61`(累加)、`:77-85`(计价)。现状:`thoughts_token_count` 全仓零命中,思考 token 在 total 里有、既不进 out 也不进 cost_usd。
- **骨架**:①`:55` setdefault 的 dict 加 `"thoughts": 0`;②`:61` 后加 `d["thoughts"] += getattr(m, "thoughts_token_count", 0) or 0`;③`:85` 计价加 `+ d.get("thoughts", 0) / 1e6 * p["out"]`(思考按 out 价,官方口径);④summarize 返回加 `tokens_thoughts`。
- **(红队修正 B2)** summarize 对 `_PRICE` 查不到的模型不得静默跳过:按表内最贵单价估价,返回加 `"unpriced_models": [...]`,非空即日志告警。否则 G 件日后换模型时熔断致盲。
- **(红队修正 B4)** summarize 开头对 usage dict 浅拷贝(或复用 _LOCK),防并发遍历 RuntimeError 炸掉用户工具调用。
- **验收**:单测——mock 一个带 thoughts_token_count 的 usage_metadata,断言 cost_usd 含思考费;mock 一个未知模型名,断言 unpriced_models 非空且按最贵价估。**为什么 P0**:不修则一切成本验收全是假账,且 C 臂思考更重、低估系统性偏向 C。
- **工时**:0.5 天(含测)。

## P0-2 树状 trace

- **落点**:`pipeline/agentops/trace.py:22-31`(TraceStep)、`:43-47`(_end)、`:62-67`(step);`pipeline/subagents.py:_run_one`(:96 处子 agent 现在裸共享父 trace 平面)。
- **骨架**:TraceStep 加字段 `span_id: str = ""`、`parent_id: str | None = None`、`depth: int = 0`、`t_start/t_end: float = 0.0`(纯加字段,序列化向后兼容)。模块级 `_CURRENT_SPAN = contextvars.ContextVar(...)`;`Trace.step` 里自动填 span_id/parent_id/depth/t_start;`_run_one` 在 ctx.run 内先开本枝根 span 再 set _CURRENT_SPAN,该 worker 线程后续步自动认父。`Trace.steps` 保持扁平 list(append 在 GIL 下原子),树由 parent_id 事后重建,前端/SSE 零迁移。
- **(红队修正 A5)** span_id 不许用 `f"s{len(self.steps)}"`(并发读 len 有竞态 → 重复 id → 树重建错父),改 `itertools.count()` 模块计数器或 append 处加锁。
- **开关**:`USE_TREE_TRACE=0`(关=新字段留默认值,输出形状不变)。
- **验收**:开 USE_SUBAGENTS 跑一次 fan-out,dump trace,断言每个子 agent 工具步的 parent_id 指向其枝根 span、depth=1、无重复 span_id;关开关时输出与 main 逐字节一致。
- **工时**:1 天。观测先行——Phase 1 的"实际触达深度/按节点调用数"全靠它。

## P0-3 per-tree 美元熔断(依赖 P0-1)

- **落点**:`pipeline/loop_driver.py:539-560`(`_make_executor._do`,照抄 :546-556 的 analyze 配额闸软失败模板);**外加 run_loop 每步开头**。
- **骨架**(工具闸):`_COST_GATED = ("analyze_video", "spawn_agents", "web_search", "semantic_search")`(show_*/sql 豁免,收口不许被锁死);`_do` 内在配额闸前:`spent = usage.summarize()["cost_usd"]`(contextvar 全树总账,子 agent 按引用共享天然含全枝),`spent >= cap` 即返回软失败信封:"本请求累计成本 $X 已达熔断线:工具没执行。就已有证据收口,没查到的明确写【未核查】(弃权),不要编。"不 kill 线程,子 agent 自然收敛。
- **(红队修正 B1,必做)** 熔断同时挂 `run_loop` 每步调 generate 前:超线注入收口指令终止循环。否则进入 Trap 循环、只"思考+说话"不调工具的大脑可以在 cap 之上无限烧(3.5-flash out 价下思考 token 尤其疼)。同一个常量,+6 行。
- **(红队修正 B3)** 检查改"预估后比":`spent + 保守单次估价 > cap` 即拦,锁内完成,收窄并行 check-then-act 超冲尾巴。
- **(红队修正 B6)** 同一处加 `MAX_TREE_WALL_S` 墙钟闸(生产默认 900s),超时同款软收口信封——只有美元闸时用户请求可以不超 $0.80 挂 20 分钟。
- **(红队修正 B5)** 熔断触发时统计并披露 `wasted_usd`(作废枝已烧成本)进 trace meta 与信封,浪费必须有账。
- **成本可见**:spawn_agents 返回值末尾追加一行 `{"instruction": "(系统)", "output": "全树累计 $X.XXX"}`(subagents.py:130 旁,+3 行)。
- **开关**:`MAX_TREE_COST_USD=0.0`(关);实验用 $0.80,生产建议 0.50(对齐 RL_SESSION_COST_USD=0.75 留头寸)。与请求间护栏(api/server.py:309 事后 record)互补:per-tree 管"单请求内烧穿"。
- **验收**:集成测——把 cap 设 $0.001 跑一次多工具请求,断言:①后续 gated 工具全收信封;②run_loop 在下一步 generate 前终止;③最终答案含弃权表述;④trace 里有 wasted_usd。
- **工时**:1 天。

## P0-4 spawn gate(Atomizer 五判据)

- **落点**:`pipeline/node_specs.py:192-199`(spawn_agents planner_desc),纯 prompt +8 行,零代码,随 USE_SUBAGENTS 生效。
- **骨架**:「spawn 前先过五道判据,不全过就自己直接做:①原子性(sql/semantic 两三步能答的不拆)②独立性(子任务互不需要对方中间结果)③多步性(只查一条 SQL 的不配当子任务)④可综合(收口只需结论+证据引用)⑤成本(每子 agent ≈ 一次完整分析的钱,K 个值不值)」。
- **验收**:5 条简单题(单 SQL 可答)跑 USE_SUBAGENTS=1,spawn 触发率 ≤1/5。
- **工时**:0.25 天。

## P0-5 semantic_search 加 video_id 过滤(Stage 5 S1,主库现在**没有**,别按已有设计)

- **落点**:`pipeline/semantic_index.py:40-42`(SEARCH_SQL 加 `WHERE video_id = ANY(%s)` 可选分支)+ `pipeline/node_specs.py:164-168`(工具参数加可选 `video_ids`)+ `pipeline/node_executor.py:511-514`(透传)。
- **开关**:`USE_IN_VIDEO_SEARCH=0`。深度 2 的视频内下钻靠它;三臂实验**同开**(控制变量)。
- **验收**:指定 video_ids 检索,结果全部落在指定集合内;不传参行为与现状逐字节一致。
- **工时**:0.5 天。

## P0-6 strong 闸信封补 start_ts/end_ts

- **落点**:`pipeline/node_executor.py:542-558`。现状信封不带时间戳 → 视频内两跳结构性断,会把三臂在 T2 上一起摁在地板,实验白跑。三臂同享此修复=控制变量,不是偏袒。
- **验收**:构造一个带时间戳的 strong 闸场景,断言信封含 start_ts/end_ts。
- **工时**:0.5 天。

## P0-7 裸 depth-2(实验对象,默认关)

- **(红队修正 C2)** 只做 depth 穿透,**不带** DAG/蒸馏/分层——实验测单变量。
- **落点与骨架**:
  - `pipeline/subagents.py` 顶部两个 contextvar:`_TREE_DEPTH`(每线程快照,天然按枝隔离)、`_TREE_STATE`(`{"nodes":1,"lock":Lock()}` 按引用共享=全树总账,同 usage dict 模式);
  - 白名单 :29-:30 改函数 `_allowed(depth)`:仅 `USE_DEPTH2 and depth == 0` 时 base + spawn_agents(即只有 depth-1 子 agent 可再拆一层);`_clean_tasks`(:60)与 `_run_one`(:85)两道过滤都改用它;`_run_one` 在 ctx.run 内、run_loop 前 `_TREE_DEPTH.set(get()+1)`;
  - `run_fanout`(:109-111 处)三道硬闸:depth≥2 直接 ValueError"已到最大深度,这层必须自己做完"(原子强制);depth≥1 时 fanout 顶用 `SUBAGENT_L2_FANOUT=3`;全树节点数锁内计数、硬顶 `MAX_TREE_NODES=13`(防 6×6=36 乘法);
  - **(红队修正 D1)** "先自己试"用**代码约束**替代文本检查:depth≥1 的 spawn 只在本子 agent 已成功执行 ≥2 次工具调用后放行(execute 闭包里数),否则 ValueError 软失败教育回喂;`why_stuck` 参数保留但降级为 trace 记录用,不做放行依据(非空字符串检查=装饰,模型一句模板就绕过,退化成变相预拆全树)。
  - `_SUBAGENT_SYSTEM`(:32-38)加一句:"若你握有 spawn_agents:那是你自己做不动时的最后手段,先用自己的工具试。"
- **开关**:`USE_DEPTH2=0`(依赖 USE_SUBAGENTS=1)。`node_executor.py:415` 双保险门不动。
- **(红队修正 A3)** 开关收敛:本期只新增 `USE_TREE_TRACE`、`MAX_TREE_COST_USD`、`MAX_TREE_WALL_S`、`USE_IN_VIDEO_SEARCH`、`USE_DEPTH2` 五个,不引入 USE_AGENT_DAG/USE_AGG_DISTILL/G 常量(Phase 2 再议),避免 2^8 组合矩阵没人测。
- **验收**:①depth-2 子 agent 的白名单不含 spawn_agents(拿不到第三层);②未执行满 2 次工具就 spawn 收到教育信封;③全树节点数在并发下不超 13;④关开关时全部路径与现状逐字节一致。
- **工时**:1-1.5 天。

**Phase 0 合入顺序**:P0-1 → P0-2 → P0-3 → (P0-4/5/6 并行) → P0-7。每件独立 PR、独立单测。

---

# 3. Phase 1 实验协议(gate,跑前定稿,以下即判停条件冻结版)

**一句话**:514 条现库出 18 道库级聚合题,A/B/C 三臂各 n=2,四指标全录;C 臂只有在 T2 档质量 ≥+0.10 且成本双门达标才建 Phase 2,否则归档"当前任务分布下深度 2 不值"——**"不建"是本 gate 的合法产出**。

## 3.1 三臂定义(红队修正 C2:单变量)

| 臂 | 配置 | 增量 |
|---|---|---|
| A 单脑 | USE_SUBAGENTS=0 | 零 |
| B 一层 | USE_SUBAGENTS=1, FANOUT=6, MAX_STEPS=4(现状) | 零 |
| C 裸下钻 | B + **USE_DEPTH2=1**(唯一差异) | P0-7 |

DAG/蒸馏/分层全关且逐项列进控制变量表。裸 depth-2 都赢不了就不用谈叠 buff;赢了,B/C/G 在 Phase 2 各自单独 A/B。

## 3.2 题库配方(18 = 9 T1 + 9 T2 + 2 空集探针单列)

- 先拉谓词分布(`SELECT predicate, COUNT(*) FILTER (WHERE matched) FROM video_facts GROUP BY predicate`),只选命中 **3–10 条**的谓词。**(红队修正 C1)** gold 上限从 60 压到 ≤10:`MAX_VIDEOS_PER_REQUEST=12`(config.py:115)全树共享,gold>12 的题任何臂都结构性做不满,分数被配额天花板压扁、C−B 差被稀释——配额进控制变量表,三臂同值,副指标加"配额触发次数/臂",dry-run 见任一臂触发即停下重定参数。
- **T1(9 题,一层够)**:全库找 X → video_id 集合 + 按大类归类 + 时间线。金标准 = sql_query 直连精确算出。C 臂的阴性对照。
- **T2(9 题,天然两层)**:找出所有含 X 的视频 + 每条内 X 的时间段 + 画面证据。防捷径:只选 `start_ts IS NULL` 的谓词实例(DB 里查不到答案),gold 库外自建。**(红队修正 C4)** 预标不用 flash 考 flash(同族相关误差):换 pro 预标或双模型交叉、分歧处人裁;gold sheet 记每条置信度,低置信条目只进集合判分不进定位判分。
- **(红队修正 C5)** 2 道空集探针移出 T1 配额单列(考弃权,不是阴性对照)。
- 半数题面改写成同义表述(题面 LIKE 不中库内谓词,考语义映射)。
- **(红队修正 C7)** 题库切 **dev(6 题)/holdout(12 题)**:dev 用于组件验收与调试;holdout 冻结、只在三臂主跑时打开,gold sheet 分开存——防 C 臂(调试轮次最多)对题库过拟合直进裁决。
- 防作弊六关照 DVD 制度:元数据盲测 / 裸 LLM 先验 / SQL 直查关(T2 不可被白名单表直查解出)/ 枚举空集关(F1 判分)/ 词表泄漏关 / 判分对称关。
- **环境冻结**:关闭 `_index_analyze_result`(node_executor.py:356 的 use-to-grow 旁路,防先跑的臂喂肥后跑的臂);DB 快照冻结。

## 3.3 控制变量(三臂完全一致)

同模型(flash 全线)/ 同工具白名单(web_search 全关)/ 同 DB 快照 / 同答案格式契约 / 同 judge(temp 0 对臂盲判序随机)/ 同 per-tree 熔断($0.80)与墙钟(15min)/ **同 MAX_VIDEOS_PER_REQUEST(红队修正 C1)** / USE_IN_VIDEO_SEARCH 三臂同开。**(红队修正 C3)** 缓存不用两序拉丁方(B 臂恒排中间永远暖缓存):analyze 缓存 key 带 arm×rep 命名空间(或评测期 TTL=0),三臂全冷,成本才叫全口径。

**n**:每题每臂 2 rep;B/C 同题翻转(一胜一负)追加第 3 rep(n=1 翻转不可信)。

**四指标**:质量分 / 全口径$(P0-1 修复后含思考)/ 端到端秒 / LLM 调用次数。副指标:配额触发数、熔断触发数、wasted_usd、实际触达深度、下钻触发数、弃权数、编造 video_id 数、parse-failure 率(按臂单列,红队修正 F2:臂间差 >10 个百分点则格式分与内容分拆开复核)。

## 3.4 判分尺子

答案格式契约三臂同一(结构化 JSON `{video_ids, count, per_video:{category, start_ts, end_ts, evidence}}`,解析失败同一 regex 兜底)。集合=F1(确定性);归类=受控词表准确率(确定性);时间线=recall×归一化 Kendall τ;T2 定位=±10s 或 IoU≥0.3 vs 库外 gold(确定性);T2 证据描述=1-5 锚定量表(judge);空集=精确判空+零编造。合成:T1 = 0.8 确定性 + 0.2 judge;T2 = 0.5 F1 + 0.3 定位 + 0.2 judge。judge 上岗前 20 条人标校准 κ≥0.7(rubric 最多改 2 轮,仍不达标该成分降级为仅上报);judge prompt 动过→全套件重判。

## 3.5 判停条件(冻结)

- **R1 成本闸(红队修正 C6,双门)**:C 的 T2 单题$ **中位数 >2×B 或 P90 >3×B** → 判"不值"。中位数单门会漏 Trap 税(方差 12× 全在尾巴)。
- **R2 质量闸**:C−B 在 T2 均值差 <+0.10 → 判"不值"(门槛来由:DVD agency-Δ +0.20~0.33 的一半以下不配一层新架构)。
- **(红队修正 B5)** **cap-受限保护条款**:C 臂熔断触发率 >30% 时,R2 判"cap-受限不可判",不得写成"深度 2 不值"——预算问题不许伪装成架构结论。
- **R3 算力混淆应急**:C 在 T2、T1 以相近幅度同时赢 → 先跑 B+(B 步数预算提到与 C 实测调用量持平,9×2)再下结论。
- **R4 阴性对照(红队修正 C5,降级)**:T1 上 |C−B| 阈值放宽到 0.10,且只作"触发 R3 的信号",不做独立裁决项(7 道真 T1、n=2 撑不起 0.05 的功效)。
- **建门槛(全满足才建 Phase 2)**:T2 上 C−B ≥+0.10 且 C≥B 的题 ≥6/9 且成本双门达标且熔断触发率 ≤30%。
- **功效诚实声明**:9 对 T2 配对 Wilcoxon 80% 功效只能检出 ≥约+0.14——接受:架构投入只为大效应买单。判决押预注册门槛+方向一致性+bootstrap CI(报但不作硬门)。+0.05~0.10 灰区 → 仍"不建",归档标注"灰区,任务分布向 1h+ 迁移可重开"。
- **(红队修正 F4)** 归档措辞必须写"flash orchestrator 条件下"(LVBench 实测水位由 orchestrator 定),给换 pro 重开留钩子。

## 3.6 验尸制度(红队修正 F3,强制步)

①dry-run 后 **9 份 transcript 全读完**才准开主跑;②主跑后 B/C 分差最大的 3 题逐份验尸,确认分差来自真实行为差异而非判分伪影;③验尸记录是裁决文档必附件。DVD 的 v2 改判就来自看 transcript——隐含=不会发生,所以写死。

## 3.7 预算与停机

| 项 | $ |
|---|---|
| T1 主跑 9×2×(0.05+0.10+0.12) | 4.9 |
| T2 主跑 9×2×(0.20+0.30+0.50) | 18.0 |
| 翻转加跑 ~6×2×0.4 | 4.8 |
| judge(含校准) | 3.0 |
| dry-run 3×3×1 | 2.0 |
| B+ 应急臂(仅 R3 触发) | 6.3 |
| T2 gold 预标(pro/交叉)+人核 | 3.0 |
| **合计** | **~$42** |

三闸停机:闸1 dry-run(单题成本中位 >2×预估 或报错率 >20% 或**任一臂配额触发(红队修正 C1)**→停修);闸2 半程($25 时完成率 <50% →停重估);闸3 硬顶 **$50** 熔断,按已有数据出结论或归档"未完成"。

---

# 4. Phase 2 设计稿(简,过闸后细化,每件单独立案)

- **B 结构化 DAG**:tasks 加 `dependencies`(自指/越界静默丢弃),Kahn 波次调度替换 run_fanout 并行段(~35 行,不引 networkx);上游失败→下游整枝作废(NEEDS_REPLAN 语义)。**(红队修正 D4)** _run_one 返回结构化 `{"ok": bool, ...}`,调度器看 ok 位——不许字符串前缀嗅探(会漏"(子 agent 未收敛)"这种失败产物,垃圾注入下游=错误乘法);上游注入加长度上限。
- **C Aggregator 蒸馏**:**(红队修正 A1)** 单独立案单独 review,必须先回答"subagents.py docstring 当初为什么明文删掉 LLM 汇总、现在什么变了"。**(红队修正 D2)** 边界校验必须是确定性双向包含:`raw 中全部 video_id/时间戳 ⊆ 蒸馏文` 且 `蒸馏文 video_id ⊆ raw`,不满足 fail-open 回原文——等长数组检查不算校验,"主脑引 result_id 追回原文"的兜底在现有 UPSTREAM_HANDLES(loop_driver.py:33-39)下大半虚构,不许当兜底写。**(红队修正 A2)** spawn value 是 list 无 `.get`,蒸馏文/原文双通道要先定稿 value 形状再估工作量(历来 20-40 行+回归,不是 6 行)。
- **G task_type→模型静态分层**:config 两常量查表,3 行——但 **(红队修正 D3/B2)** 依赖 P0-1 的 unpriced fail-loud 已合,且 task_type 必须进 trace meta 供审计。
- 复合交付物(Blade Eye 式)、1h+ 长视频语料($9-11 另批)在此之后。

---

# 5. 红线合规自检表

| 改造件 | 不复活 router/动态分支 | 子 agent 只读不互写 | 成本每轮可见 | 懒惰按需(非预拆全树) | 简化优先(零框架) |
|---|---|---|---|---|---|
| P0-3 熔断 | ✓ 纯闸无路由 | — | ✓ 全树累计+wasted_usd 披露 | — | ✓ 抄现有配额闸模板 |
| P0-4 gate | ✓ 判据是工具说明非题型规则 | — | ✓ 判据⑤即成本 | ✓ 判据①原子性 | ✓ 纯 prompt |
| P0-7 depth2 | ✓ | ✓ 白名单只读四工具 | ✓ 同一闭包同一闸 | ✓ ≥2 次工具后才许拆(代码强制,红队修正 D1) | ✓ contextvar+闭包,零依赖 |
| Phase2 B DAG | ✓ 静态依赖图非动态分支 | ✓ 单向 context_input 注入 | ✓ | ✓ 主脑单层给,非全树预拆 | ✓ Kahn 手写 |
| Phase2 C 蒸馏 | ✓ | ✓ 确定性包含校验+fail-open | ✓ 蒸馏调用走 add_usage | — | 待 A1 立案答辩 |
| Phase2 G 分层 | ✓ 静态表在明文豁免内 | — | ✓ 依赖 unpriced fail-loud | — | ✓ 3 行查表 |

判停条件跑前写死(GEPA 纪律):本任务书第 3.5 节即冻结版,dry-run 后不改题库不改 judge 不调 cap。n≥2 全程执行。

---

# 6. 风险与止损表

| 风险 | 信号 | 止损动作 |
|---|---|---|
| 配额天花板摁死 T2(红队修正 C1) | dry-run 任一臂配额触发 | 停,gold 压 ≤10 或配额提至 gold_max×1.5 三臂同值,重跑 dry-run |
| C 臂被 cap 卡死误判"不值"(红队修正 B5) | 熔断触发率 >30% | R2 改判"cap-受限不可判",提 cap 重跑或归档待议 |
| Trap 税尾巴烧穿(红队修正 B1) | 某 run 成本 >P90 预估 | 大脑闸已在 P0-3;仍穿则查 unpriced_models |
| gold 噪声盖过效应(红队修正 C4) | 定位分臂间方差异常大 | 低置信 gold 条目剔出定位判分,只留集合分 |
| judge 校不准 | κ<0.7 两轮 rubric 后仍不达标 | 该成分降级仅上报,判决只靠确定性部分 |
| 格式分惩罚 C 臂(红队修正 F2) | parse-failure 臂差 >10pp | 格式/内容分拆开复核后再裁决 |
| 工期膨胀 | Phase 0 超 6 天 | 砍 P0-4 以外一切非实验必需;B/C/G 已在期外,不许回流 |
| 总预算 | 累计 $50 | 硬熔断,按已有数据出结论或归档"未完成" |

---

# 7. 总账

**工时(单人,10-14 个工作日)**:Phase 0 = P0-1(0.5)+ P0-2(1)+ P0-3(1)+ P0-4(0.25)+ P0-5(0.5)+ P0-6(0.5)+ P0-7(1.5)≈ **4.5-5 天**;Phase 1 = 题库+gold+judge 校准(2-3,与代码并行部分重叠)+ 判分器五件+格式契约(1-2)+ dry-run+验尸(1)+ 主跑+分析+裁决(2)≈ **5.5-7 天**。

**预算**:实验 ~$42,硬顶 $50。Phase 0 开发调试 <$3(dev 题集小跑)。

**依赖顺序图**:

```
P0-1 usage修复 ──→ P0-3 熔断(大脑闸+墙钟+wasted_usd)──┐
P0-2 树trace ──────────────────────────────────────────┤
P0-4 spawn gate ─┐                                      ├→ P0-7 裸depth2 ──→ dry-run 闸
P0-5 video_id ───┼──(三臂共享控制变量)─────────────────┘        ↓
P0-6 strong闸ts ─┘                                        9份transcript全读(红队修正F3)
                                                                ↓
                                                     holdout 12题主跑(n≥2)
                                                                ↓
                                                     验尸3题 → R1-R4裁决 → 归档
                                                                ↓
                                              过闸 → Phase 2(B/C/G 各自单独立案 A/B)
                                              不过 → 归档"flash orchestrator 条件下深度2不值"
```

**执行纪律收尾**:①每个 P0 件独立 PR+单测,合入前跑 dev 6 题回归;②所有新开关默认关,生产行为在 gate 裁决前零变化;③裁决文档无论方向都写——实验不过,架构不建,这句话本身就是本任务书的验收标准之一。