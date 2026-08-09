# VideoSense 项目完整评估报告

> 评估视角：大厂项目面试官、生产系统负责人、AI 应用工程负责人  
> 评估日期：2026-07-21  
> 评估对象：当前工作区代码、文档、测试、评测结果与部署配置  
> 评估方式：静态代码审查、架构审查、仓库一致性检查、离线测试与评测命令实测

## 1. 执行摘要

VideoSense 是一个具备明显工程深度的视频理解 Agent 项目。它已经形成从视频数据、结构化事实、语义检索、Agent 工具调用、多轮记忆，到视频片段、表格和图表证据交付的完整链路。相比常见的“套一层聊天界面调用模型”项目，本项目在 Agent loop、工具治理、回答诚实性、成本感知和评测基础设施方面更有辨识度。

但从大厂生产系统标准看，当前项目仍属于“优秀个人项目/可演示原型”，不应宣称为成熟的多租户生产平台。主要差距集中在四个方面：

1. 安全边界不足：Python sandbox 可绕过、视频资源缺少 owner 级授权、SQL 只读依赖正则、默认数据库用户权限过高。
2. 生产可靠性不足：数据库无连接池、长任务使用裸线程、跨实例会话一致性依赖 affinity、核心依赖故障大量 fail-open。
3. 评测口径与宣传不一致：CI 实际硬门禁远小于 README 宣称规模，live 指标存在多份不一致真源，当前评测命令还会在生成报告时崩溃。
4. 业务闭环不足：尚未证明复杂 Agent 相比直接多模态模型、普通 RAG 或固定检索流程，在任务成功率、延迟和单位成本上有足够收益。

综合判断：

- 作为个人项目：8.0/10，具备较强面试竞争力。
- 作为 AI Agent 工程样本：8.0/10，可支撑深入技术讨论。
- 作为多租户生产系统：4.5/10，目前不建议公开扩大流量。
- 作为高级后端系统设计案例：核心思路可讲，但需要先补齐安全、可靠性和容量证据。

## 2. 评分卡

| 维度 | 得分 | 面试官判断 |
|---|---:|---|
| AI Agent 工程 | 8.0/10 | Agent loop、工具调用、记忆、证据交付和评测意识明显强于普通项目 |
| 产品与业务闭环 | 5.0/10 | 有清晰体验，但目标客户、核心工作流、价值指标和付费理由不足 |
| 后端与数据架构 | 5.5/10 | 分层基本成立，但连接、状态、一致性和异步任务仍是原型级实现 |
| 安全与多租户 | 3.5/10 | 存在 sandbox 绕过、IDOR、SQL 权限和隐私生命周期问题 |
| 测试与评测 | 6.5/10 | 离线单测基础不错，评测框架有亮点，但门禁覆盖和指标真源不可信 |
| 可观测性与运维 | 4.0/10 | 有审计和内部 trace，缺少生产指标、SLO、告警、CD 和金丝雀 |
| 可维护性 | 6.0/10 | 模块命名和文档较好，但核心文件过大、依赖不锁定、版本信息漂移 |

## 3. 项目最有价值的部分

### 3.1 Agent 不只是生成答案，而是交付证据

系统把 `sql_query`、`semantic_search`、`analyze_video`、`show_video`、`show_table`、图表和 Python 计算统一纳入 loop。回答能够附带可播放片段、时间点、表格或图表，这比纯文本 RAG 更贴近真实视频分析产品。

### 3.2 对模型幻觉和成本有明确工程意识

项目包含最大循环步数、重复调用限制、视频分析配额、模型白名单、回答 ID 清洗、请求成本记录和 Redis 限流。这些机制说明作者考虑过模型系统的非确定性和成本失控，而不只是功能正确性。

### 3.3 评测基础设施具备差异化

仓库包含确定性 scorer、多轮 JGA、任务生成、sealed split、judge calibration、GEPA prompt 演化和 live 结果记录。即使当前门禁仍有问题，这套思考方式本身是项目的重要亮点。

### 3.4 代码对已知缺陷相对诚实

`docs/ops-hardening-backlog.md` 已识别 DB 连接池、长任务、跨实例一致性、IDOR、告警和金丝雀等缺口。这表明项目不是完全缺乏生产意识，而是尚未完成落地。

## 4. P0：公开扩大流量前必须解决

### P0-1 Python sandbox 可绕过且缺少资源隔离

证据：

- `sandbox/executor.py:37` 采用模块黑名单，但没有禁止 `os`、`sys`、`pathlib` 等高能力模块。
- `sandbox/executor.py:50` 只禁止少数 builtin。
- `sandbox/executor.py:114` 使用普通 `subprocess.run`，仅设置墙钟超时。
- `sandbox/models.py` 没有代码长度限制。

问题：

- 生成代码可以通过 `os.system` 或启动子解释器绕过 AST 检查。
- 没有内存、CPU、进程数、文件大小、stdout/stderr 大小限制。
- Cloud Run gVisor 隔离宿主机，但不能阻止容器级 DoS、出网和元数据服务访问。

业务影响：公开用户可让 sandbox 服务不可用、制造高额资源成本，或尝试获取运行身份能力。

建议：

- 不再把 AST 黑名单作为安全边界。
- 使用无网络、只读根文件系统、非特权用户、独立低权限服务账号和严格 egress policy。
- 增加 cgroup/rlimit 级 CPU、内存、进程、文件、输出限制。
- 对允许执行的能力采用白名单 DSL，或将绘图/统计收敛为固定操作。
- 增加 `os.system`、子解释器、fork、超大输出、内存炸弹和元数据访问回归测试。

验收标准：安全测试无法创建子进程、无法出网、无法访问元数据、无法超过资源配额，恶意任务只能影响自身执行实例。

### P0-2 视频资源缺少 owner/tenant 授权

证据：

- `api/server.py:405` 明确标注 `/resign` 不做 owner 隔离。
- `api/server.py:428` 的 `/enrich` 只校验 `video_id` 形状。
- `pipeline/uploads.py:93` 的上传注册表只按 `video_id` 查找。
- `pipeline/node_executor.py:313` 的 `_resolve_gcs` 没有 owner 参数。

问题：知道或获得其他用户的 `video_id` 后，调用方可能重签播放地址、触发富化或通过工具访问视频。随机 ID 是 capability，不是企业授权模型。

建议：把 `tenant_id/owner` 贯穿上传注册、元数据表、语义索引、查询、分析、展示和签名；数据库层增加 RLS 或强制 tenant 条件；所有资源端点做 object-level authorization。

验收标准：跨 owner 使用同一个 `video_id` 时，查询、签名、分析和富化均返回 404/403，并有审计测试覆盖。

### P0-3 SQL 只读约束不能作为安全边界

证据：

- `pipeline/sql_guard.py:22` 通过关键字正则判断只读。
- `pipeline/config.py:147` 默认数据库用户为 `postgres`。
- `mcp_server/server.py:142` 直接执行模型生成 SQL。
- `mcp_server/server.py:143` 无结果上限地 `fetchall()`。

问题：`SELECT` 可以调用有副作用的函数；正则无法可靠处理 SQL 语法、注释和 dollar-quoted 内容；大结果和慢查询可耗尽内存或连接。

建议：使用数据库只读专用角色、最小表权限、RLS、事务 `READ ONLY`、statement timeout、row limit；使用 SQL parser 做表和函数 allowlist；禁止危险系统函数；所有查询加可取消 deadline。

验收标准：即使应用层 guard 被绕过，数据库角色也无法写数据、调用危险函数或读取非业务表。

### P0-4 隐私、保留和删除链路不完整

证据：

- `api/server.py:283` 把完整用户 query 写入 Cloud Logging。
- `pipeline/transcript_store.py` 将会话写入 Redis/GCS。
- `pipeline/user_memory.py` 持久化跨会话用户记忆。
- API 没有会话、记忆和上传视频的删除/导出端点。

问题：问题文本、视频引用、分析结果可能包含个人或企业敏感信息；当前没有明确 retention、redaction、right-to-delete 和访问审计闭环。

建议：日志默认不记录全文或先做脱敏；定义数据分类和 TTL；提供删除、导出接口；删除需覆盖 Redis、GCS、session、memory、upload 和派生索引；记录可审计的删除结果。

## 5. P1：生产可靠性和扩展性

### P1-1 数据库连接和 MCP 架构无法承受高并发

每个查询新建 psycopg2 连接，没有连接池；schema 可能重复读取；单 MCP 进程成为集中瓶颈。Cloud Run 配置允许较高并发时，会首先产生连接风暴和排队。

建议：引入 pgbouncer/psycopg pool，缓存 schema，设置连接和查询超时；对 SQL 工具建立并发上限、bulkhead 和队列长度指标。

### P1-2 长任务使用裸线程

`api/server.py:452` 和 `api/server.py:498` 直接创建 daemon thread。实例回收、CPU throttling 或进程退出时任务会丢失，也没有 job id、状态查询、重试和死信处理。

建议：富化使用 Cloud Tasks/Pub/Sub；SSE 工作使用可取消的异步任务；所有长任务具备幂等键、状态机、重试策略和死信队列。

### P1-3 SSE 缺少取消传播和完整连接治理

浏览器 Abort 只停止客户端读取，后端线程仍继续调用 Gemini、SQL 和视频分析。当前也没有 heartbeat、总 deadline、断连检测和背压治理。

建议：统一 request deadline；检测 disconnect；将 cancel token 传递到模型、工具和 sandbox；SSE 定期心跳；为活跃流和中断后浪费成本建立指标。

### P1-4 跨实例会话存在后写覆盖

`api/server.py:241` 明确承认进程锁只能保护单副本。Session affinity 只是路由优化，扩缩容、重试或 cookie 丢失时仍可能并发写同一会话。

建议：会话改为 append-only event log；或 Redis CAS/版本号/分布式锁；写入需要 idempotency key；冲突时显式重试而不是覆盖。

### P1-5 状态后端故障会静默降低正确率

限流、session、transcript、memory 和上传注册大量采用 fail-open。依赖故障时接口可能继续返回 200，但丢失历史、取消限流或无法解析上传视频。

建议：区分安全控制、数据持久化和可选增强；安全和授权默认 fail-closed；记忆退化要在响应、指标和 trace 中显式标记；建立依赖熔断和降级等级。

### P1-6 可观测性不足

当前 `/health` 只报告进程存活和鉴权布尔值；成功 trace 多为内存态；缺少标准化 metrics、distributed tracing、SLO 和告警。

最低要求：

- readiness 检查 Redis、DB、sandbox 和必要配置。
- 指标包含 QPS、5xx、429、p50/p95/p99、模型/工具耗时、token、成本、缓存命中、取消浪费和队列长度。
- 建立可用性、正确率、延迟、成本四类 SLO。
- 预算、错误率、p95 和单用户异常用量必须告警。

## 6. 测试与评测实测结果

### 6.1 本次执行结果

| 检查 | 结果 | 判断 |
|---|---:|---|
| `python -X utf8 -m pytest tests -q` | 404 passed，1 skipped | 离线单测基础良好 |
| `python -m evals.validate_tasks` | 414 条配置全部自洽 | 只证明题库结构合法 |
| `python -m evals.runner` | 6/6 后崩溃，退出码 1 | 当前默认评测门禁不可交付 |
| 保存的 live 结果 | 143/189 passed，46 failed | 与公开总结口径不一致 |
| live 基础设施错误 | 5 条 | 需要从能力失败中单独统计 |
| judge calibration | n=25，kappa=1.0 | 样本小且只覆盖部分题型 |

### 6.2 当前评测命令的确定性缺陷

`evals/briefing.py:126` 调用了未定义的 `_has_blank`。现有 `tests/evals/test_runner.py:82` 只用全通过数据重建 dashboard，因此没有覆盖失败结果分支。默认 runner 虽先输出 6/6，最终仍以异常退出。

### 6.3 CI 覆盖盲区

`.github/workflows/eval-gate.yml:33` 只在 `pipeline|evals|tests|repl|perception` 变化时运行。以下高风险路径可以绕过门禁：

- `api/`
- `sandbox/`
- `mcp_server/`
- `web/`
- `Dockerfile`
- `requirements*.txt`
- 部署和基础设施配置

同时，`pytest.ini` 只收集 `tests/`，因此 `sandbox/test_*`、`perception/test_*` 和服务级测试不进入默认 CI。

### 6.4 评测宣传存在可信度风险

README 宣称“每次变更都过 370 道自动化评测”，但实际 CI 默认只执行 6 条 scripted task，414 条命令只是验证配置结构。`evals/RESULTS.md` 写 131/143、92%，当前 live 文件则是 143/189。

面试时不应继续使用“每次变更都过完整题库”的说法。更可信的表达是：项目维护了 400+ 测试任务定义，CI 当前执行离线 scorer 回归和 6 条脚本硬门，完整 live suite 需要凭据和成本预算，由手工或定时任务运行。

## 7. 产品和业务判断

### 7.1 目标用户与核心场景不够聚焦

“随便问你的视频库”是体验描述，不是业务定义。仍需明确第一目标客户：媒体资产管理、内容审核、体育分析、创作者素材检索、企业培训或安全监控。不同客户的数据权限、延迟、准确率和交付形态完全不同。

### 7.2 没有证明复杂 Agent 的增量价值

项目包含 loop、self-check、subagent、web search、跨会话记忆和 code sandbox，但缺少与以下基线的系统对比：

- Gemini 直接看视频并回答。
- 固定 SQL + semantic retrieval，不使用开放式 Agent。
- 普通视频 RAG。
- 只做离线索引、不做实时 analyze。

需要用同一冻结测试集比较任务成功率、p95、模型调用次数和每个成功任务成本。否则面试官会判断为“技术堆叠多，但未证明必要性”。

### 7.3 缺少业务级 KPI

至少应持续记录：

- 用户任务完成率和追问率。
- 有证据回答的 precision/recall。
- 空答、拒答、错误视频、错误时间戳比例。
- p50/p95 首事件时间和完整答案时间。
- 每个成功任务成本。
- 用户上传到可检索的端到端延迟。
- 真实用户复用率和每周活跃查询数。

### 7.4 数据入库仍是脚本，不是稳定产品能力

`ingestion/` 和 `perception/` 提供了下载、转码、抽取和回填脚本，但缺少统一 job 状态、数据 lineage、失败重试、质量门、schema migration 和数据新鲜度 SLA。生产化后应形成可恢复、可观测的异步数据管道。

## 8. 代码质量与仓库可信度

### 8.1 复杂度集中

| 文件 | 规模 | 风险 |
|---|---:|---|
| `pipeline/loop_driver.py` | 837 行 | 模型适配、循环控制、重试、prompt、metrics 集中 |
| `pipeline/node_executor.py` | 632 行 | 多种工具执行和业务规则集中 |
| `api/server.py` | 507 行 | 鉴权、API、任务、审计、静态资源混合 |
| `evals/runner.py` | 711 行 | 运行、统计、报告和状态管理集中 |
| `web/index.html` | 1200+ 行 | UI、状态、网络、渲染和存储无模块边界 |

建议按模型供应商适配、loop engine、tool handlers、API routers、job service、auth policy 和 observability 拆分，同时保持接口数量克制。

### 8.2 构建不可复现

`requirements.txt` 只有最低版本，没有 lock file；没有 dependency audit、SBOM、lint、formatter、mypy/pyright 和覆盖率门槛。未来任意依赖升级都可能在未改代码时破坏构建。

### 8.3 数据库变更不可治理

schema 通过多个 `setup_*.py` 执行 `CREATE/ALTER`，没有 migration version、升级顺序和回滚策略。应迁移到 Alembic 或等价迁移系统，并纳入部署前检查。

### 8.4 文档与真实实现漂移

- README 标 Python 3.13，Docker 和 CI 使用 Python 3.11。
- README badge 标 Gemini 2.5，默认 loop model 是 3.5。
- FastAPI app version 是 1.0，CHANGELOG 已到 2.0。
- README 的评测规模与实际 CI 不一致。
- live 结果与 `RESULTS.md` 不一致。

这些问题会让面试官怀疑指标和项目叙事是否经过验证。

## 9. 面试官可能追问的问题

1. 为什么一定要 Agent loop，固定检索流程不能解决吗？
2. 与直接 Gemini 视频问答相比，成功率提升多少，成本增加多少？
3. 100 个并发请求时，数据库、MCP、Redis、sandbox 和 Gemini 谁先成为瓶颈？
4. 用户断开 SSE 后，如何停止已经开始的模型和视频分析调用？
5. 如何证明 sandbox 中的用户代码不能出网、fork 或访问元数据？
6. 一个用户知道另一个用户的 video_id 后，在哪一层阻止访问？
7. Redis 故障时，限流和会话记忆分别采取什么策略，为什么？
8. live eval 与真实用户满意度的相关性如何验证？
9. 为什么 CI 宣称 370 道题，但实际只跑 6 条 scripted task？
10. 如何删除一个用户的全部 query、transcript、memory、上传视频和派生索引？

面试前应准备可以量化的答案，而不只是描述代码结构。

## 10. 30/60/90 天改造路线

### 0-30 天：关闭安全和可信度缺口

- 修复 sandbox 绕过，或暂时下线通用 Python 工具。
- 为 `/resign`、`/enrich`、`show_video`、`analyze_video` 增加 owner/tenant 授权。
- 创建数据库只读角色，增加 statement timeout、row limit 和查询 allowlist。
- 修复 `_has_blank`，让默认 eval 命令稳定退出 0。
- 扩大 CI 路径，纳入 API、sandbox、MCP、Docker 和依赖。
- 统一 README、版本、模型、评测规模和结果真源。

### 31-60 天：建立生产运行底座

- 引入 DB pool/pgbouncer 和 schema cache。
- 富化迁移到 Cloud Tasks/Pub/Sub，增加 job 状态与幂等。
- 解决跨实例会话 CAS/append-only 一致性。
- 增加 readiness、OpenTelemetry、metrics、成本看板和告警。
- 完成用户数据删除、保留和日志脱敏。
- 增加并发、故障注入、取消和恢复测试。

### 61-90 天：证明业务价值

- 选择一个明确垂直场景和目标用户。
- 建立 direct Gemini、RAG、固定检索、Agent loop 四组基线。
- 发布成功率、p95、单位成功成本和错误类型报告。
- 用真实用户查询建立冻结 holdout，避免只优化合成题。
- 建立金丝雀发布和自动回滚。

## 11. 上线准入清单

在以下项目完成前，不建议扩大公开流量：

- [ ] sandbox 无已知进程、网络、元数据和资源绕过。
- [ ] 所有视频资源操作都有 tenant/owner 授权测试。
- [ ] 数据库使用独立只读角色、RLS、超时和结果上限。
- [ ] Redis 故障时安全控制不会无限 fail-open。
- [ ] 长任务使用可恢复队列，不依赖 daemon thread。
- [ ] SSE 支持 heartbeat、deadline、disconnect 和 cancel propagation。
- [ ] 跨实例会话无后写覆盖。
- [ ] readiness 能识别关键依赖失效。
- [ ] 有 p95、5xx、成本和异常用户告警。
- [ ] 用户可以删除和导出自己的数据。
- [ ] CI 覆盖 API、sandbox、MCP、web、Docker 和依赖变更。
- [ ] 完整评测结果只有一个可追溯真源。
- [ ] 依赖和镜像可复现、可扫描。
- [ ] 有容量测试、故障恢复记录和回滚演练。

## 12. 最终面试结论

该项目最强的信号是：作者具备构建复杂 AI Agent、将模型能力接入真实工具、设计证据化交付和建立评测框架的能力。这足以让项目从大量普通 LLM Demo 中脱颖而出。

当前最弱的信号是：安全、授权、并发、状态一致性、异步任务、可观测性和指标真源仍没有达到生产系统标准，而且 README 对评测覆盖存在过度表述。

最合适的面试定位不是“我做了一个已经成熟的大厂级平台”，而是：

> 我独立构建了一个端到端视频理解 Agent，完成了检索、感知、工具执行、记忆、证据交付和系统化评测；随后通过生产审查识别出 sandbox、多租户授权、数据库并发和评测门禁等关键缺口，并按安全、可靠性和业务价值三个阶段推进工程化。

这种叙事既保留项目的技术含金量，也不会因过度宣称而失去可信度。

## 附录 A：评估范围与限制

本报告基于当前本地工作区，不代表线上 GCP 账户的实际 IAM、预算、网络策略和 Secret Manager 状态。账户侧 provider spend cap、Cloud Run 私有网络、服务账号权限和告警是否已经配置，需要在云环境中另行核验。

本次没有执行真实 Gemini live suite、负载测试、恶意 sandbox 动态攻击和真实数据库写权限测试，因此相关结论基于代码实现与现有结果文件。所有高风险项仍应在隔离环境中进行专项验证。
