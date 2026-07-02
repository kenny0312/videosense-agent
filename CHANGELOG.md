# Changelog

本项目的版本发布记录。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

---

## v2.0.0 — one-loop 内核全面升级:受控分类、3.5-flash 大脑、联网搜索、三层记忆体系与新前端  (2026-07-02)

> 大更新(2026-06-30,PRs #50–#59:Router 降级为开关、M4.5 时段裁剪、并行 analyze、M5 实时上传)之后的一轮大版本:数据层立标准、大脑换代、补联网与跨会话记忆两块能力拼图、程序记忆走上可治理轨道、前端全面刷新。两轮多代理对抗式 review 把关。

### 亮点

- **受控大类标准(U1)**:26 个受控大类 + 153 中英别名 + 197 谓词映射(`pipeline/taxonomy_seed.py`,代码即真源);入库即写大类行,查询走「词表精确命中 → 细谓词数准」两层。途中发现并修复 **44% 的库(50/114 视频)零 facts 完全隐身** —— 全部补抽,终态 **114/114 全覆盖**。「有没有做饭的视频」从"没有"变"有,7 个"。
- **大脑升级 gemini-3.5-flash(U5)**:经实测,3.x 只在新 google-genai SDK + `global` 端点可达(旧 SDK/us-central1 全 404)→ 新增 genai 后端,按模型代际自动选;回滚 = `LOOP_MODEL=gemini-2.5-flash` 一个 env。智力提升实测:「做饭」这类词表外问题它自己连发 SQL 挖出来;挑"最精彩"先用库内 freefall 数据而非盲目全看。
- **联网搜索(U6)**:`web_search` 工具(Gemini Google-Search grounding),带注入防护(网页=资料非指令)、范围护栏、来源引用、成本记账;`USE_WEB_SEARCH=0` 即整体消失。
- **三层记忆体系(L1/L2)**:程序记忆 = 宪法(稳定原则)+ `lessons.py` 教训集(≤15 预算、每条带出生/来源/**退役条件**)+ 机械规则下沉(`answer_guard` id 清洗器,命中率即退役依据);跨会话用户记忆 = 每 owner GCS blob + `update_memory` 工具(从严判据,资料非指令框架)。prompt 缩至旧版 79% 且增长有了对冲机制。
- **自我认知与成本(U3/L3)**:会话累计 usage 注入大脑(「用了多少 token/钱/窗口多大」给真数,不再编"32K");隐式缓存在字节稳定前缀上自动命中(实测 cached 3637/5056),usage 按缓存折扣价($0.15/M vs $1.50)诚实计账。
- **展示收口契约(U2)**:原始视频 id 永不进答案文本(「第 N 个 + 内容特征」指认 + 代码级清洗兜底)、要看/要播必以 show_video 交付、计数自动对账「共 X 展示 Y」。
- **前端刷新**:删除每轮宣告的 `follow-up · 复用了上文` 徽章(常态不宣告),trace 移卡片底部安静页脚(点击展开);UI chrome 全英文(回答语言不变);Tabler 图标 + 微动效 + 组件精致化。
- **加固**:transcript 耐久序号防覆盖(修复"实例重启静默覆盖会话最早历史"的真 bug,含线上事故实锤)、不当内容策略性拒答(零工具)、wingsuit 过度标注数据修正(12/14 对齐)。

### 架构变更(before → after)

| 维度 | v1.x | v2.0 |
|---|---|---|
| 数据分类 | 195 个自由细谓词,查询靠现场翻译 + ILIKE(时灵时不灵) | 受控大类层(26 词表)+ 细谓词两层;入库标准化,存在性问题词表优先 |
| loop 大脑 | gemini-2.5-flash(旧 vertexai SDK) | gemini-3.5-flash(google-genai @global;工厂按代际自动选后端) |
| 行为规则 | 单一 prompt 只增不减 | 宪法 + 预算化教训集 + 机械规则下沉代码 + 工具用法归声明 |
| 记忆 | 仅会话内 transcript 回放 | + 跨会话每 owner 用户记忆(update_memory) |
| 能力边界 | 只有库内数据 | + web_search 联网(grounding,带防护) |
| 视频分析 SDK | 旧 vertexai + `_raw_part` proto hack 裁剪 | google-genai 原生 VideoMetadata offsets(hack 删除) |
| 成本核算 | 全价计 token | 缓存命中按折扣价;会话累计注入大脑可自述 |
| 前端 | 中文 chrome,每轮徽章 + trace 常驻头部 | 英文 chrome,常态不宣告,安静页脚按需展开 |

### 里程碑 / PR

| 项 | PR | 摘要 |
|---|---|---|
| U2 展示收口 | [#60](https://github.com/kenny0312/videosense-agent/pull/60) | id 不进答案、播放必 show、计数对账(prompt 契约) |
| U3 自我认知 | [#61](https://github.com/kenny0312/videosense-agent/pull/61) | 会话累计 usage 注入;元问题给真数、不漏厂商 |
| U5 大脑升级 | [#62](https://github.com/kenny0312/videosense-agent/pull/62) | genai 后端 + 3.5-flash 默认 + 价目;中英 prompt A/B 附带结论:中文保留 |
| U1 分类标准 | [#63](https://github.com/kenny0312/videosense-agent/pull/63) | 词表/normalize/回填/查询桥;50 隐身视频补抽 |
| U6 联网搜索 | [#64](https://github.com/kenny0312/videosense-agent/pull/64) | web_search grounding + 防护 + 开关 |
| U4 加固 | [#65](https://github.com/kenny0312/videosense-agent/pull/65) | 安全拒答 + transcript 序号防覆盖 |
| 依赖拆弹 | [#66](https://github.com/kenny0312/videosense-agent/pull/66) | google-genai 缺失于 requirements(部署级) |
| U 批 review 修复 | [#67](https://github.com/kenny0312/videosense-agent/pull/67) | web_search 预览截断、词表同步 FK 顺序、序号对齐 max+1 |
| 人物防编造 | [#69](https://github.com/kenny0312/videosense-agent/pull/69) | Kenny Qiu 身份问题不编造(后推广为通用教训 L07) |
| L1 程序记忆 | [#70](https://github.com/kenny0312/videosense-agent/pull/70) | 宪法+教训集拆分 + id 清洗器下沉 |
| L3 缓存记账 | [#71](https://github.com/kenny0312/videosense-agent/pull/71) | 隐式缓存命中按折扣价计成本 |
| L2 用户记忆 | [#72](https://github.com/kenny0312/videosense-agent/pull/72) | 跨会话每 owner 记忆 + update_memory 工具 |
| P1 SDK 迁移 | [#73](https://github.com/kenny0312/videosense-agent/pull/73) | perception 运行时迁 genai,裁剪 hack 删除 |
| L 批 review 修复 | [#74](https://github.com/kenny0312/videosense-agent/pull/74) | 清洗器全文误伤/CJK 泄漏/记忆覆盖/注入面加固 |
| UI 刷新 | [#75](https://github.com/kenny0312/videosense-agent/pull/75) | 英文 chrome、去徽章、安静页脚、组件精致化 |
| 发布文档 | [#68](https://github.com/kenny0312/videosense-agent/pull/68) · [#76](https://github.com/kenny0312/videosense-agent/pull/76) | 两批 release 报告 + 竞品调研入库 |

### 移除 / Breaking

- **`LOOP_MODEL` 默认从 gemini-2.5-flash 改为 gemini-3.5-flash** —— loop 输入单价 5×($1.50/M,缓存命中 $0.15/M 对冲;视频分析大头仍 2.5-flash 不变)。回退:env `LOOP_MODEL=gemini-2.5-flash`。
- 删除 M4.5 的 `_raw_part.video_metadata` proto hack(genai 原生支持)。
- 前端删除 follow-up/new/meta 每轮徽章与头部 trace 摘要(移安静页脚);UI chrome 中文文案全部替换为英文。
- prompt 中 10 条行为规则迁出(→ 教训集/工具声明/清洗器),`%skiing%/%snowboarding%` 诱发重复计数的旧示例删除。
- **保留**:旧 vertexai SDK 路径仍在(loop 回滚路径 + critic/sql_fixer/离线脚本),下批迁移候选。

### 验证

- **测试**:151 → **208 passed**(+57)。
- **线上探针**:U 批 21 轮验收 + 中英 A/B 34 轮 + U5 回归 17+8 轮 + L1 回归 17 轮 + 终回归 9 轮 ×2 —— 全部含 0-id-泄漏与 CJK 语言门。
- **对抗式 review ×2**(多代理,多镜头 × 双否证):确认并修复 2 项、人工复核加固 9 项、按设计接受若干(全部记录在 release 文档)。
- **成本实测**:loop ~$0.034/轮(3.5-flash 全价)→ 暖会话经隐式缓存显著下降;单视频分析 ~$0.018 不变。
- **生产冒烟**:rev 00027/00028/00029 各一轮(跳水新数据/kennyqiu 身份/做饭 7 个 + UI chrome 英文)。

### 部署

- 线上 revision **`videosense-00029-cpj`**(2026-07-02,`gcloud run deploy videosense --source . --region us-central1`,保留 env)。
- 新增 env 开关:`GENAI_LOCATION=global`、`USE_WEB_SEARCH=1`、`WEB_SEARCH_MODEL`、`USE_USER_MEMORY=1`、`USER_MEMORY_MAX_CHARS`;既有 `USE_ROUTER_GATE=0`/`USE_SELF_CHECK_CRITIC=0` 不变。
- 详情见 [U 批报告](docs/releases/2026-07-01-u-batch-upgrade.md)、[L 批报告](docs/releases/2026-07-02-l-batch-memory-ui.md);竞品调研见 [docs/research/2026-07-02-competitor-analysis.md](docs/research/2026-07-02-competitor-analysis.md)。

---

## v1.0.0 — VideoSense 从 DAG 迁移到 probe-and-step loop  (2026-06-28)

> 执行内核从「Planner 规划 typed DAG → 拓扑执行」重写为「Router 直入 probe-and-step 主循环 + Gemini 原生 function-calling」，loop 成为唯一执行路径。

### 亮点

- **新执行内核**：Router 路由后直接进入 probe-and-step 主循环（`loop_driver`），以 Gemini 原生 function-calling 驱动交错的 tool-use（探测 → 执行一步 → 再探测），取代 plan-then-execute 的 typed DAG。
- **记忆重做**：会话记忆从 recipe-based 改为 append-only transcript（Redis 热尾 + GCS 全量/溢出），支持跨轮次回放与上下文压缩。
- **工具模型简化**：工具产物改用纯 handle catalog（句柄目录）描述，配套结构化 `node_specs`（OpenAPI schema）+ Gemini function declarations。
- **可观测 + 流式**：loop 路径服务端 Trace 落库 + 指标审计；端到端 SSE 流式，前端逐步渲染进度。
- **彻底 cutover**：loop 设为默认并最终成为唯一路径，Planner / DAG 规划 / recipe / `VS_EXECUTOR` 开关全部移除，净减 410 行。

### 架构变更（before → after）

| 维度 | 旧架构（before） | 新架构（after） |
|---|---|---|
| 执行 | Router → Planner → typed DAG → topo 节点执行器 | Router → probe-and-step loop（`loop_driver`）+ Gemini 原生 function-calling |
| 调度 | plan-then-execute（先规划整张图再跑） | 交错 tool-use：探测 → 执行一步 → 再探测 |
| 记忆 | recipe-based 会话记忆 | append-only transcript（Redis 热尾 + GCS 全量/溢出，回放 + 压缩） |
| 工具产物 | DAG 节点 | 纯 handle catalog（句柄目录） |

> 注：`dag_schema` 的 `Node` 类型被保留，继续被 `execute_node` / `code_generator` 用作 loop 执行器的节点类型；Planner / DAG 规划层 / recipe 已删除。

### 里程碑（M0–M7b）

| 里程碑 | PR | 摘要 |
|---|---|---|
| M0 | [#14](https://github.com/kenny0312/videosense-agent/pull/14) | 两份设计文档（`docs/design/dag-to-loop-migration.md` + `docs/design/dag-to-loop-roadmap.md`）定下存储/上下文/沙箱/护栏方案与 M0–M7 里程碑，纯文档无代码。 |
| M1 | [#15](https://github.com/kenny0312/videosense-agent/pull/15) | 为每个工具生成结构化 `node_specs`（OpenAPI schema）+ Gemini function declarations，为原生 function-calling 打底。 |
| M2 | [#16](https://github.com/kenny0312/videosense-agent/pull/16) | spike 验证 Gemini function-calling 循环与 handle（句柄）约定可走通，证明交错 tool-use 主循环可行。 |
| M3 | [#17](https://github.com/kenny0312/videosense-agent/pull/17) · [#18](https://github.com/kenny0312/videosense-agent/pull/18) | 落地 probe-and-step 主循环驱动器（`loop_driver`），先以 `VS_EXECUTOR` 灰度接入（#17），后重新稳定落地 main（#18）。 |
| M4 | [#19](https://github.com/kenny0312/videosense-agent/pull/19) | 新建 append-only transcript 存储层：GCS 全量/溢出 + Redis 热尾，取代 recipe 会话记忆。 |
| M5 | [#20](https://github.com/kenny0312/videosense-agent/pull/20) | 把 transcript 记忆接入 loop 路径，支持跨轮次回放与上下文压缩。 |
| M6 | [#21](https://github.com/kenny0312/videosense-agent/pull/21) | loop 路径服务端可观测：Trace 落库 + 指标，为每次请求留审计轨迹。 |
| M6b | [#22](https://github.com/kenny0312/videosense-agent/pull/22) · [#23](https://github.com/kenny0312/videosense-agent/pull/23) | 服务端 SSE 流式（+ Cloud Run timeout 调整，#22）与前端消费流并渲染逐步进度（#23）。 |
| M7 | [#24](https://github.com/kenny0312/videosense-agent/pull/24) | 默认 executor 切到 loop（cutover）+ 答案精度修正；dag 作为回退保留，全套 151 passed。 |
| M7b | [#25](https://github.com/kenny0312/videosense-agent/pull/25) | 删除 `planner.py` / recipe / `VS_EXECUTOR` 开关，loop 成为唯一执行路径；净减 410 行，146 passed。 |

### 移除 / Breaking

- 删除 **Planner**（`pipeline/planner.py`）与 **DAG plan-then-execute** 规划层。
- 删除 **recipe** 会话记忆机制（由 append-only transcript 取代）。
- 删除 **`VS_EXECUTOR`** 灰度开关 —— loop 现为唯一执行路径，不再有 dag/loop 双轨。
- 删除对应的 **conftest** 及 recipe / planner / e2e-dag 测试。
- **保留**：`dag_schema.Node` 仍作为 loop 执行器（`execute_node` / `code_generator`）的节点类型，未删除。

### 验证

- **测试**：146 tests 全过（M7/#24 时为 151 passed，M7b 移除 recipe / planner / e2e-dag 测试后为 146）。
- **dag-vs-loop 对比（真 Neon）**：loop 较 dag **约快 40%**、**约便宜 4×**，正确性相当。
- **端到端 smoke**：真库 `new → meta` 端到端 smoke 通过。

### 部署

- 代码已合入 **main**，但**不会自动部署**：仓库内无 `.github/` workflow、无 main→deploy 的 Cloud Build trigger（`sandbox/cloudbuild.yaml` 仅手动构建沙箱镜像）。线上网站需手动 redeploy 才生效：

  ```bash
  gcloud run deploy videosense --source . --region us-central1 --allow-unauthenticated \
    --memory 1Gi --cpu 1 --timeout 300 --min-instances 0 --max-instances 5 --session-affinity \
    --set-env-vars "<见 docs/DEPLOY.md 第 2 节，env 取自本地 neon.env>"
  ```

  从仓库根目录运行。若只改单个 env，用 `gcloud run services update videosense --region us-central1 --update-env-vars ...`（**不要**用 `--set-env-vars`，会清空其余 env）。
- **URL 不变**：Web UI 由 FastAPI app 同容器/镜像 serve（`GET /` → `web/index.html`），与 API 一起部署，无独立静态托管 —— redeploy 只是往同一个 Cloud Run 服务推新 revision，公网地址不变。
