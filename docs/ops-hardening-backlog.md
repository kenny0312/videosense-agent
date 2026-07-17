# VS Ops 加固作战手册 — 基线审计 + 路线图

> 生成日期：2026-07-16 ｜ 分支：feat/evals-hardening ｜ 作者：Kenny + Claude(ops 会话线)
> 这份文档是「持续完善 VS ops 系统」这条会话线的主账本。每完成一项就在 checklist 打勾并记下 PR。

栈：FastAPI（单进程）+ Gemini（google-genai，global 端点）+ Redis(Upstash REST) + Postgres(Neon, pgvector)，部署在 GCP Cloud Run。
线上：`videosense` = `https://videosense-qud75u5tfa-uc.a.run.app`（**Cloud Run IAM = allUsers，公网可调**）。

---

## 0. 一句话体检结论

**架构底子好（模型分层、配置集中、离线 eval、会话外置都做对了），但"对公网开放"这件事的三块承重墙全缺：没有跨请求的账单熔断、鉴权靠人肉记得设口令、数据库层在并发下会第一个塌。** 这三件事是上线前的硬门槛，其余是优化。

---

## 1. 现状 × 业界 × 差距（五维度）

### 维度一：安全性 / 账单防护 —— 差距最大

| 项 | VS 现状 | 业界标准做法 | 差距 |
|---|---|---|---|
| Provider 侧硬顶 | 无（billingbudgets API 都没启用） | AI Studio 月度 spend cap（到顶停服） | ❌ 缺最后一道底线 |
| 预算告警 | 无 | GCP Budgets 50/80/100% 邮件 | ❌ |
| 应用层限流 | **完全没有**（任何维度） | Redis 计数器按 token/成本 限 IP/用户/会话/全局 | ❌ 最大敞口 |
| 单请求护栏 | 有：loop≤16 步、单请求≤12 视频分析、上传≤20/天 | 同类 | ✅ 单请求内够用 |
| 跨请求预算熔断 | **无**（usage_cum 只记账不比对） | 单会话/单用户/每日 token 或 $ 上限 | ❌ |
| web_search/sql/python 次数 | **不计数、无上限** | 每工具每请求上限 | ❌ |
| 鉴权 | 可选 HTTP Basic Auth（`APP_ACCESS_KEYS`），默认关 | API key/OAuth + 匿名极小额度 | ⚠️ 靠人肉记得开 |
| fail-closed 兜底 | 只在 `APP_ENV=prod` 生效，**但部署命令没设这个变量** | 部署即强制 | ❌ 网没武装 |
| CORS | 无 | 显式白名单 | ⚠️ 同源部署暂时不炸 |
| query 长度上限 | 无（只有 Cloud Run 32MB 兜底） | 应用层 max_length | ❌ |
| 上传防滥用 | 有大小/类型/每日配额，**但 Redis 挂→fail-open 变无限**；匿名共享一个桶 | 同上 + fail-closed | ⚠️ |
| IDOR | `/resign`、`/enrich` 无 owner 隔离（代码自标 deferred） | 资源级 owner 校验 | ❌ 越权读全库 |

**最坏情况推算**：一条消息最坏 = 16 次大脑调用 + 12 次视频分析(≈$0.22) + **无上限的 web_search/python 调用**，5 分钟内跑完；再叠加无限流 → 一个人可无限重复 → 聚合花费无界。这正是代码注释里那个 "videosense-pyai 匿名烧钱" 事故的同类风险。

### 维度二：高并发上线

| 项 | VS 现状 | 业界标准 | 差距 |
|---|---|---|---|
| async 模型 | 端点是 sync `def`，靠 FastAPI 默认线程池(~40) | 全链路 async + async LLM client | ⚠️ 能跑但吃 GIL |
| 流式 | 有 SSE `/stream` 端点 | SSE + 流内错误事件 + 心跳 + generator 超时 | ✅ 基本有，细节待补 |
| 长任务 | enrich 用裸 `threading.Thread` | Cloud Tasks 队列 + 私有 worker（减震器） | ❌ |
| **DB 连接层** | **每查询新建 psycopg2 + 零池化 + 单 MCP 子进程串行 + 全局锁** | 连接池(pgbouncer/psycopg_pool) | ❌❌ **100 并发第一个崩** |
| 429 退避 | loop 有退避重试 | capped 指数退避 + full jitter，单层重试 | ✅ 大致有 |
| Cloud Run 参数 | cpu1/mem1Gi/concurrency80/max5/session-affinity | 代理型可高 concurrency；min-instances 灭冷启动 | ⚠️ max5 封死横扩 |
| 会话一致性 | 每会话锁只在单副本内，跨副本靠 affinity 尽力 | 分布式锁或 affinity | ⚠️ 后写覆盖风险 |

**100 并发最先崩的三处**：① DB 访问层（连接风暴 + 串行队列）；② 1CPU 单 worker + Gemini 每分钟配额 429；③ 共享 Upstash Redis 的 REST 限流。

### 维度三：token 最省化

| 手段 | VS 现状 | 业界现价(2026-07) | 差距 |
|---|---|---|---|
| 模型分层 | ✅ 已做（3.5-flash 打杂 / 2.5-pro 攻坚，服务端白名单） | RouteLLM：26% 强模型调用保 95% 质量 | ✅ 方向对 |
| 隐式缓存(前缀) | ⚠️ 靠 Gemini 自动，但每步 schema/replay 拼在后面，命中率未度量 | 稳定内容放最前，命中=输入 1/10 价 | ⚠️ 排序 + 度量待做 |
| 显式缓存 | 无 | 高频重用大上下文才开（Flash 存储 $1/M/h） | 视场景（同一视频连问）可加 |
| analyze 内容缓存 | ✅ 有，**但默认 memory 后端→不跨副本、重启失效** | L2 共享 | ⚠️ 改 redis 后端 |
| Batch API 半价 | ❌ 未用 | 离线索引/eval 全走 batch，5 折 | ❌ 纯省钱零风险 |
| 上下文裁剪 | ✅ 工具结果预览裁剪 | 老轮摘要 + 相关工具注入 | ✅ |
| system prompt 预算 | ⚠️ 无统一总量护栏，每步全量重发 | 前缀缓存 + 预算 | ⚠️ |
| 语义缓存 | 未用 | **多用户个性化 agent 慎用**（假阳性泄漏） | ✅ 不做是对的 |

### 维度四：高效率（降延迟）

感知延迟 ≈ 首 token 时间。OpenAI 官方七原则对照：

- ✅ 流式首 token（有 SSE）
- ❌ 并行工具调用：同一步内 **非 analyze 工具串行**（sql/semantic/show 都在主线程），只有 analyze_video 走线程池
- ⚠️ 连接复用：Redis client 是单例（好），但 DB 每查询新建（坏）
- ⚠️ schema 每请求重取（近乎静态却每次打 DB）
- 少生成/少输入：可加 max_output_tokens 约束

### 维度五：可维护性

| 项 | VS 现状 | 业界标准 | 差距 |
|---|---|---|---|
| 配置管理 | ✅ 中央 config.py + env 驱动 | 同 | ✅ 优秀 |
| trace | ⚠️ 内存态，**只在 error 时落盘**，成功链路不持久 | Langfuse 全量 trace（prompt/响应/token/延迟/成本） | ❌ 缺 trace 平台 |
| 日志结构化 | ⚠️ 审计日志是 JSON，业务日志是裸 logging | 全结构化 | ⚠️ |
| 测试 | ✅ 24 文件 ~3900 行离线单元 | 单元 + 集成 + 负载 | ⚠️ 缺并发/压测 |
| CI/CD | ⚠️ 有 eval-gate CI，**无 CD（手动部署）** | eval 门禁 + 金丝雀 | ⚠️ |
| 金丝雀 | ❌ 未用（Cloud Run 自带流量分割却没用） | 1→5→20→50→100% | ❌ 白送的没用 |
| prompt 版本 | ✅ git 管 constitution+lessons | git+PR 或 Langfuse 标签 | ✅ 够用 |
| 花费可查 | ⚠️ usage→stdout→需手工建 BigQuery sink 才能聚合，且 sink 不回填 | 现成看板 | ⚠️ |
| 告警 | ❌ 无（预算/5xx/p95/单用户异常都没接） | 三件套告警 | ❌ |

---

## 2. 作战顺序（按性价比排序）

### P0 —— 上线前硬门槛（代码部分已于 2026-07-16 完成，见分支 feat/evals-hardening）

- [~] **P0-1 账单三道底线**（代码侧已备；**账户侧待你亲自执行** → docs/billing-guardrails.md）
  - [ ] AI Studio 设 Gemini 月度 spend cap（provider 侧硬顶，到顶停服）← 你执行
  - [ ] 启用 billingbudgets API + GCP Budget 50/80/100% 告警 ← 你执行（命令已备好）
  - [x] 确认 Gemini tier 自带的 10 分钟滚动熔断（免费第二层，已写进手册）
- [x] **P0-2 应用层按成本分层限流** —— `pipeline/agentops/ratelimit.py` + config + server 接线
  - [x] 按【成本$】+【速率】双口径，纵深四维：IP / 用户 / 会话 / 全局每日 + 匿名小额度档
  - [x] 两拍记账（precheck 前置比对 + record 后置累加）；query 加 max_length（8000）→ 422
  - [x] 10 个单测覆盖全维度 + fail-open + 开关；335 全绿
  - 起步额度：具名 $2/日、匿名 $0.2/日、单会话 $0.75、全站 $15/日熔断（env 可调）
- [x] **P0-3 鉴权 fail-closed 落地** —— Dockerfile 钉死 `APP_ENV=prod`
  - [x] 镜像 fail-closed：忘设口令 = 启动即 raise、不切流量（本地 uvicorn 不受影响）
  - [x] DEPLOY.md 更新说明；已验证「prod 无口令拒启 / prod 有口令正常 / 本地正常」三态
  - [ ] （遗留）复核 videosense 的 allUsers IAM 是否该收紧 —— 现由 app 层口令门兜住，暂 P2

### P1 —— 扛并发 + 省钱，公测期做

- [ ] **P1-1 DB 连接池**（100 并发第一个崩的地方）
  - psycopg_pool 或在 Neon 前挂 pgbouncer；干掉每查询新建连接
  - 评估把 MCP 单子进程串行改成池化
- [ ] **P1-2 analyze 缓存改 redis 后端**（跨副本共享，`ANALYZE_CACHE_BACKEND=redis`）
- [ ] **P1-3 Batch API 半价**：ingest enrichment + eval 全量回归改走 batch
- [ ] **P1-4 隐式缓存排序 + 度量**：稳定内容(宪法/schema/教训)前置，动态内容后置；用 `usage.total_cached_tokens` 度量命中率
- [ ] **P1-5 schema 进程级缓存**（近乎静态，别每请求打 DB）

### P2 —— 可维护性 + 效率打磨

- [ ] **P2-1 Langfuse 免费档接 trace**（全量调用链可回放）
- [ ] **P2-2 eval suite 接 CI 当门禁**（掉线 block 合并；已有 eval-gate，强化断言 + 成本/延迟断言）
- [ ] **P2-3 Cloud Run 金丝雀**（revision 流量分割，白送）
- [ ] **P2-4 告警三件套**（预算 + 5xx/p95 + 单用户 token 异常）
- [ ] **P2-5 同步非 analyze 工具并行化**（sql/semantic/show 独立调用 asyncio.gather）
- [ ] **P2-6 长任务上 Cloud Tasks 队列**（enrich 从裸线程改成队列减震器）
- [ ] **P2-7 修 IDOR**（`/resign`、`/enrich` 加 owner 隔离）
- [ ] **P2-8 usage→BigQuery sink 建好**（"昨天花了多少钱"可查）

---

## 3. 已经做对、别乱动的地方

模型分层 + 服务端白名单、中央 config、离线 eval suite、会话外置 Redis（Postgres 正史 + Redis 热数据）、owner 作用域会话（防会话 IDOR）、SSE 流式端点、loop 步数/重复护栏、上下文预览裁剪、**不做语义缓存**（多用户个性化场景假阳性会泄漏，业界也劝退）。

---

## 4. 关键数字备查（2026-07 官方现价）

- Gemini 2.5 Flash：$0.30/$2.50 每 1M（输入/输出）；隐式缓存命中输入 $0.03/M（1/10）；命中门槛 2,048 tok
- Gemini 2.5 Pro：$1.25/$10.00 每 1M
- Flash-Lite：$0.10/$0.40
- 显式缓存存储：Flash $1.00/M/小时（约 >4 次查询/小时/M 才回本）
- Batch API：输入输出均 5 折，24h 出结果，独立更高限额
- Gemini 限额 per-project（多 key 无用），升 Tier 2 = 累计消费 $100 + 3 天
- Cloud Run：request timeout 默认 5min / 最长 60min；代理型服务瓶颈在上游配额不在 CPU

来源：ai.google.dev/gemini-api/docs 的 pricing/caching/rate-limits/billing 页、cloud.google.com Cloud Run & Budgets 文档、OpenAI latency guide、Langfuse/promptfoo 文档。
