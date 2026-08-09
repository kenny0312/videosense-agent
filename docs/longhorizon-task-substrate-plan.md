# 任务底座(Task Substrate)· 任务书 v1.2

> v1.2(2026-07-29):用户定稿目标形态=CC 式(主 loop 派活后在线、任务完成回流对话、可基于产出续作)→ 新增 S-9 完成状态回流(注入式通知 + get_task_report 工具)与 S-10 任务续作(parent_task_id + 父报告注入规划波)。工期 +1 天(6-8 天)。

> **定位**:长程引擎的第二条主线,与 Phase 0/1(gate 实验)**并行且互不依赖**。回答:**任务如何脱离 HTTP 请求活下来**(立项→后台波次推进→检查点续跑→进度可见→追加指示→完成沉淀)。
> 粒度对齐 longhorizon-multiagent-plan.md 的 P0 七件。file:line 为 feat/evals-hardening 勘察实况。
> **v1.1 变更**:v1 草案经 52-agent 红队(云现实/DB并发/代码接缝/红线四镜头),24 条发现存活 23、全部吸收。致命修正:第 0 波规划(v1 漏了整个初始分解!)、认领三态分流、lease_token CAS、终态清租约、命名任务自愈、usage 显式 reset、ratelimit 实账回灌。红队原文见工作流 wf_301040d2 存档。
> 赎买:落地即关 ops 账本 P2-6(Cloud Tasks 队列),enrich 裸线程(api/server.py:452)S-8 迁走。

---

## 0. 冻结的架构决策

**D1 执行形态 = 波次链式推进,投递走薄驱动 `TASKS_DRIVER=cloudtasks|inline`**
每波 = 一个 HTTP 请求打回 `/internal/tasks/advance`:请求内全速跑一波 → 落检查点 → 投递下一波。
- **cloudtasks**(生产):命名任务 `name={task_id}-w{N}`(同名 ~1h 内 ALREADY_EXISTS 去重 → 补投天然幂等);队列显式 `maxAttempts=5, maxBackoff≈300s, dispatchDeadline=服务超时同值`——**不吃默认 ~100 次重试,这是成本护栏**(红队 HIGH:默认重试 × 波超时 = 无限烧钱回路)。本服务 `--timeout 900`(gen2 上限 3600,一行部署参数;dispatchDeadline 硬顶 1800)。
- **inline**(本地/降级):daemon 线程直接调 `task_runner.advance(task_id, wave_n)`,同一代码路径——本地开发、单测、Cloud Tasks 开通受阻三合一。**取代 v1 的 Scheduler 方案 B。**

**D2 检查点粒度 = 波次边界;第 0 波 = 规划波**(红队 HIGH:v1 全文没人把 goal 变成任务清单)
- 子 agent 是波内跑完的无状态单元(genai 会话不可序列化);检查点只存【done 结论 + remaining 清单 + 账目】;库内先例 = run_matrix 断点续跑(done_units+qhash)。
- `advance` 见 `wave_n=0 且 remaining 为空` → 先跑一次**无状态分解调用**(goal → remaining,输入输出落 event),同一事务 `pending→running`。
- **组波纪律**(红队 HIGH:该限的是单子任务串行深度,不是扇出):每个子任务 **≤2 个候选视频**(组波按 video_ids 长度切分);波时长预算常量 = `0.8×min(--timeout, dispatchDeadline)`;每波 ≤`SUBAGENT_MAX_FANOUT`(6)个子任务。

**D3 明确不做**:通用 workflow 引擎 / 跨任务依赖 / 推送通知 / 会话续跑 / 优先级抢占(enrich 迁入后若真挡道,证据会出现在 events 延迟里,届时再议)。

---

## 任务清单(S-1 … S-8)

### S-1 表结构与状态机(~90 行)

**落点**:`perception/setup_tasks.py`(家规:`CREATE TABLE IF NOT EXISTS` 幂等,setup_schema.py:23 同款)。

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
  task_id      TEXT PRIMARY KEY,
  owner        TEXT NOT NULL,
  goal         TEXT NOT NULL,
  goal_hash    TEXT NOT NULL,             -- 立项幂等键(owner+goal 哈希)
  plan         JSONB NOT NULL DEFAULT '{"remaining":[],"done":[]}',
                 -- remaining:[{id:int, instruction, video_ids?}...] ← 子任务带稳定序号(红队:v1 引用了不存在的 id)
                 -- done: {id: {answer, cost, wave}}  按 id 键控
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|paused_budget|paused_error|done|cancelled
  wave_n       INT  NOT NULL DEFAULT 0,
  lease_until  TIMESTAMPTZ,
  lease_token  TEXT,                      -- CAS 围栏(红队:僵尸波双提交)
  budget_cap   NUMERIC NOT NULL,
  spent_usd    NUMERIC NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS agent_task_events (
  id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL,
  kind TEXT NOT NULL,   -- planned|wave_attempt|wave_done|user_note|paused|resumed|cancelled|done|error
  payload JSONB, created_at TIMESTAMPTZ DEFAULT now()
);
```

状态机代码写死;**所有终态/暂停写入必须在同一事务里 `lease_until=NULL, lease_token=NULL`**(红队 HIGH:resume 死锁)。DB 即用即连(Neon 掐闲置,build_db.py:37-39 同款)。开关 `USE_TASKS=0`。
**验收**:setup 幂等;非法迁移单测;**裸 SQL 绕不过状态守卫**(检查点 UPDATE 自带 status='running' 条件)。

### S-2 四端点(~140 行)

**落点**:api/server.py(:374-455 端点群之后)。

- `POST /v1/tasks` 立项:**幂等**(active 任务里同 goal_hash → 返回已有 task_id,顺手挡前端双击);**只做 ratelimit precheck 校验,不扣占位**(红队 HIGH:占位与日顶制度双向冲突);`budget_cap ≤ RL_TASK_DAILY_COST_USD`(新常量,决策:任务预算独立于 $2 对话日顶,但有自己的日顶);投第一波(wave 0 规划波)。**enqueue 失败 fail-closed**:status=paused_error + event(enqueue_failed) + 503 人话——不留 running 幽灵(红队:本库三处 except-log-continue 惯性是这里的反面教材)。
- `GET /v1/tasks/{id}`:状态+进度(done/remaining 按 id 计数)+成本行(spent/cap/wasted)+events 尾部。
- `POST /v1/tasks/{id}/notes`:user_note 落 event,下一波组装注入。
- `POST /v1/tasks/{id}/cancel`。
- **guest 一律 403**(create/notes/cancel/resume 四端点;公开部署日不返工);resume 的新 cap 硬顶 `TASK_MAX_CAP_USD`。
**验收**:owner 隔离;USE_TASKS=0 全 404;双击只建一个任务;enqueue 故障不留幽灵。

### S-3 波次推进(心脏,~220 行;v1 低估,红队校正)

**落点**:`POST /internal/tasks/advance` + `pipeline/task_runner.py`。

**鉴权**(红队实答:半天工作量,v1 就上 OIDC 不留降级到生产):Cloud Tasks 带 oidcToken(compute 默认 SA;两条 IAM),服务端 `verify_oauth2_token` + **audience 钉死 advance 完整 URL**;**检测到 K_SERVICE 环境变量时共享密钥路径直接禁用**(共享密钥仅限本地,hmac.compare_digest);校验失败一律 403 fail-closed(server.py:89 先例);advance 加入 _OPEN_PATHS 豁免口令墙但被 OIDC 罩住。

**每波流程**:
1. **认领(三态分流,红队两条 HIGH 的修法)**:
   `UPDATE agent_tasks SET status='running', lease_until=now()+'10 min', lease_token=$uuid, updated_at=now() WHERE task_id=$1 AND status IN ('pending','running') AND wave_n=$2 AND (lease_until IS NULL OR lease_until<now()) RETURNING *`
   命中 0 行 → **补读该行分三态**:①行内 wave_n > 请求 wave_n 且 remaining 非空、status=running、租约空闲 → **补投当前波**(命名任务幂等)再 200——"重复投递"本身变成断链修复器(红队:commit→enqueue 裂缝);②wave_n 相等且租约未过期 → **503**(让 Cloud Tasks 退避重来,跨过租约期;绝不 200 吞掉);③status 非 running → 200。
2. **规划波**(wave 0):无状态分解调用 goal→remaining(带稳定 id),落 planned event。
3. **记账先行**:写 `wave_attempt` event(attempt_n + 本波**保守预估悲观计入 spent**)——超时波也推高 spent,预算闸对重试风暴恢复视力(红队 HIGH:烧钱回路的闭环件);提交时用实测覆盖,重试波把上次预估转 wasted_usd。
4. **跑波**:`usage.reset_usage()` **显式调用**(红队 HIGH:v1"天然累计"是误读,不 reset 记账为零——四跑事故同款);task_runner 自建共享 executor 并**包一层预算/取消检查**(`_make_executor` 产物外包 wrapper,run_fanout 传 `execute=wrapped`,schema 照传——红队:run_fanout"原样复用"缺的三件料);跑 `run_fanout`;结束 `usage.summarize()` 取实账。
5. **落检查点(CAS)**:`UPDATE ... SET plan=..., spent_usd=..., wave_n=wave_n+1, lease_until=NULL, lease_token=NULL WHERE task_id=$1 AND wave_n=$2 AND lease_token=$mine AND status='running'`——0 行 = 本 attempt 是僵尸,**整波战果丢弃**只记 wasted event(红队:双提交/双记账/cancelled→done 全在这一个 WHERE 里堵死)。done 按子任务 id 键控合并。
6. **续/收**:先提交后投递(命名任务);remaining 空 → 主脑收口调用 → done → 沉淀钩子;**ratelimit.record(owner, 本波实账)**——任务花费喂进日常/全站熔断(红队 HIGH:波次花费对全站熔断隐身)。
7. **审计**:finally 无论成败吐一行 `task_wave_audit` 结构化日志(_audit 模式,server.py:273-312 同款)——死掉的波也有账。

**验收**:同 (task_id,wave_n) 重复投递恰好执行一次;**租约被占期间重投必须非 2xx**;波中杀进程可续且 done 不重复;僵尸双提交被 CAS 丢弃;**构造最坏波(6×2 视频)压测一次不超时长预算**;波 event 的 $>0 且与 usage 对得上。

### S-4 预算闸 + 取消(~70 行)

波开头:`spent+预估>cap` → paused_budget(**同事务清租约**)+ 不投下波;**resume = 新 cap + status=running + 清租约 + 投递 {task_id, 当前 wave_n}**(红队 HIGH:v1 的 resume 没投递=必死锁;paused_error 重试入口同款)。步内:wrapper 每次工具调用前查 spent/cancelled(内存缓存 5s);超/取消 → 软失败信封收口。DVD costguard 制度移植,审查入口=任务页按钮。
**验收**:cap $0.5 一波内停;**暂停后 10 分钟内 resume 必须能推进**(v1 的死锁场景);cancel ≤1 步边界生效;wasted 入 event。

### S-5 前端任务页(降级为**可选**,先 curl 撑验收——红队:保工期)

必做的只有一个:任务列表对 `status=running 且 updated_at 落后 >15min` 的行渲染**"重推"按钮**(投当前波,命名任务幂等)——拉模式下的人肉救援通道,零新组件。完整页面(详情/追加框/resume)可后补。

### S-6 主脑工具位 `start_background_task`(~40 行)

四步注册,`USE_TASK_TOOL=0` 独立开关。判据**不含任何美元知识**(红队:单价塞 prompt 违反 keep-prompts-adaptive):「要深看的视频数超过单请求上限(约 12 个,一口气装不下)或用户明说'慢慢做/做完叫我'才立项;能当场答完的绝不立项。**立项成功后直接告知用户并收口——不要重复立项、不要再当场自己做**」。
**验收**:5 道当场题误立项 ≤1/5;critic 开启下无二次立项(立项端点幂等兜底)。

### S-7 完成沉淀钩子(~20 行,诚实版)

analyze 入索引已自动(_index_analyze_result,**注意前提 USE_SEMANTIC_SEARCH 开着**——钩子里检查并记录);lessons/eval 沉淀只留空钩子,自学习环另行立项。

### S-8 enrich 迁移(可选尾件,赎买 P2-6)

底座稳一周后,/v1/enrich 裸线程改立单波任务。

### S-9 完成状态回流主 loop(~40 行)——CC 式"任务通知"(v1.2 新增)

**动机**:用户目标形态 = CC 式体验:任务完成后**对话自己知道**,而不是用户去任务页拉。
**机制(零推送,纯注入)**:主 loop 组装 system/context 时(锚:loop_driver 的 `_loop_system` 拼接处,与 schema 注入同款),查一次该 owner 的【已完成且未通报】任务(`status='done' AND notified_at IS NULL`,S-1 加一列)→ 注入一行:「(系统) 后台任务『{goal 前 40 字}』已完成,报告已就绪——用户问起或本轮相关时主动告知,并可用 get_task_report 取全文」→ 同请求内标记 notified_at。配一个只读工具 `get_task_report(task_id)`(第 12 工具,四步注册,USE_TASKS 门内)让主脑按需取报告全文——**不把整份报告塞进每轮 context**(重读税教训)。
**验收**:任务完成后用户发任意消息,主脑能主动提及并按需取全文;注入行 ≤120 字;已通报任务不再重复注入。

### S-10 任务续作(iterate,~30 行)——"基于这份报告再改"(v1.2 新增)

**动机**:用户拿到报告后说"第 3 部分再细化/换个角度重排"——要能**基于上一个任务的产出**开新一轮,而不是从零开始。
**机制**:`agent_tasks` 加 `parent_task_id`;立项(API 与 S-6 工具)接受可选 parent 参数;**规划波的分解 prompt 注入父任务的最终报告 + done 结论摘要**(单向 context_input,同树引擎的依赖传递纪律——不共享可变状态,只读快照)。父任务的 analyze 缓存与索引沉淀天然复用(同 video 的重看几乎免费)。主脑侧:S-9 的通报行里带 task_id,用户说"改进"时主脑用 parent 立续作。
**验收**:续作任务的规划波 prompt 含父报告(trace 可证);父子链在任务页可见;续作成本显著低于首作(缓存命中,event 里可读)。

---

## 红线合规

| 红线 | 落法 |
|---|---|
| 懒惰按需 | 判据禁"能当场答的";规划波也是按需(立项才有) |
| 成本每轮可见 | 每波 event 带预估→实账;wasted 单列;**任务实账回灌 ratelimit 日顶/全站熔断**;死波有 task_wave_audit |
| 不复活 router | 工具说明书判据,量纲=视频数(执行层已强制的边界),零价格知识 |
| 简化优先 | 两张表一个新端点;inline 驱动让本地/降级零新组件;唯一新组件 Cloud Tasks(生产) |
| 子 agent 只读/不互写 | run_fanout 零改动,只包 executor |

## 工期与预算(红队校正)

**5-7 天**(v1 的 3-4 天低估;砍 S-5 完整页面可压回 4-5):S-1/2 一天半,S-3 两天(心脏,含三态分流/CAS/压测),S-4 一天,S-6/7 一天,机动半天。验收 **~$3**(2 个真实任务 + 规划波 + 最坏波压测)。
**总验收闸**:"分析 8 条视频出报告"任务全程:关页面×1、追加指示×1、预算暂停×1、**10 分钟内 resume**×1、模拟波超时(kill)×1——进度连续、账目与 usage/ratelimit 双审计对齐、报告 video_id 全真。

## 开放问题(红队后仅剩)

1. plan JSONB 膨胀:50 波后 done 滚动归档到 events(阈值待定,先观测);
2. 分解调用(规划波)的质量护栏——remaining 为空/超 50 条时的处置(建议:直接 paused_error 等人,不自旋)。

### S-3b 波间重规划(v1.3 增补,用户评审发现的缺口;~40 行,USE_TASK_REPLAN=0 默认关)

**缺口**:v1.2 的 plan 由第 0 波一次性定死,波间只分批消费——底座能跑"宽任务分批做",跑不了"第 N 波内容取决于前面发现了什么"的**依赖型长链**(同步路径无此问题:主脑每轮重新决策;过夜路径有)。
**机制**:任务立项可带 `replan=true`(或规划波自判并写入 plan.meta);开启时每波检查点提交后、投递下一波前,插一次**无状态重规划调用**(输入=goal+done 结论+剩余清单+user_notes → 输出=修订后的 remaining,允许增删改),输入输出落 planned event(修订历史可审计)。护栏:重规划次数 ≤ 波数(不自旋);remaining 突增 >2× 需过预算预估;修订必须保留已完成子任务的 id 不变(done 不作废)。
**与 Phase 2 DAG 的关系**:重规划是"串行自适应"(每波一次全局重想),DAG 是"声明式依赖边"(兄弟间拓扑)——前者更贴 ReAct 哲学且便宜,先做;若 Phase 1/使用数据显示大量任务需要兄弟间细粒度依赖,再上 DAG。
**验收**:构造"第 2 波必须依赖第 1 波发现"的剧本任务(如"先找出出现次数最多的人物,再盘点他的全部出场"),开 replan 能对、关 replan 答不全;修订历史在 events 完整可见。
