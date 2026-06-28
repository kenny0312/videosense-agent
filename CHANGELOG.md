# Changelog

本项目的版本发布记录。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

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
