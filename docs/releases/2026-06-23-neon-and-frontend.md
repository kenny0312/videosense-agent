# v1.3 · Neon 迁移 + 会话持久化 + 聊天前端
日期:2026-06-23 ｜ 类型:feature

主题:把数据从 AlloyDB 迁到 Neon(省钱)、会话记忆落盘可续聊、并给 agent 配一个富渲染的聊天前端。

## 更新内容

### 多轮会话(持久化 + 滚动摘要)
- 会话记忆落【独立本地 SQLite】(单 json blob / 会话),**重启可续聊**;该文件只有 `SessionStore` 打开,与 MCP 查的库**物理隔离** → 免疫"潘多拉"(planner 的 SQL 读不到会话记忆)。
- 淘汰边界把"硬删"改为**确定性 rolling summary**(整条留存、0 LLM、保住 artifact id);`catalog_view` 截最近 N 条、newest-first(抗 lost-in-the-middle)。`Turn` 增加 `referenced_artifact_ids` 冻结指代。

### 数据层:AlloyDB → Neon
- 视频事实/元数据迁到 **Neon serverless Postgres**(免费档、idle $0;对比 AlloyDB 最小集群 ~$250-550/月)。psycopg2 / MCP 路径**零改**,只换连接串(`ALLOYDB_*` 指向 Neon)。
- 新增 `ingestion/backfill_metadata.py`:扫 GCS 桶回填 `video_metadata`(补上 ingestion→perception 之间缺的那一环)。
- `perception/gemini_predicates.py` 重写为**"每视频 1 次开放式抽取"**(列出实际活动,~5× 更省、数据更丰富);换当代模型 `gemini-2.5-flash`;headless(env 读密码 / `PERCEPTION_MAX_VIDEOS`);**真跑加固**:强制 JSON 输出(`response_mime_type`)+ TCP keepalives + 单视频重连。
- 实测:100 视频元数据 + 50 视频 / **212 条 fact** 落 Neon(均值置信度 0.94),`video_discovery` 由 facts 聚合生成。

### 前端
- 新增 `web/index.html`:气泡式多轮对话 + **富渲染** —— 表格(排序 + 置信度条 + CSV)、图表内联、DAG 步骤条 + 可展开「SQL / 生成代码 / trace 时间线」、统计卡;follow-up 标"复用了上文"。`GET /` 经 `FileResponse` 发它。
- 渲染拆成纯函数(`renderTable/renderChart/renderPlan/...`),**后端零改即可升 React**。

### 便利
- `config.py` 自动加载本地 `neon.env`(不覆盖已设的)→ 直接 `uvicorn` / 跑脚本就连 Neon,无需先 source。
- `run.ps1` 有 neon.env 时默认连 Neon;`.claude/launch.json` 加 `videosense-api` 预览配置。

## 影响 / 注意
- API 契约不变(仍 `POST /v1/video_vibe_query`,回 `answer/dag/generated_code/plot_url/trace/turn_type/session_id`);新增 `GET /` 发前端。
- **不入库的本地文件**(均 gitignored):`neon.env`(密钥)、`.session_store.sqlite`(会话)、`artifacts/`(图表)。
- **真实数据在 Neon + GCS(云),非本地** —— 换机器开发只需重建 `neon.env` + `gcloud auth`。
- 测试:离线 **40/40**(session 19 + 多轮 6 + router 9 + sql 6);真链路多轮 + 浏览器端到端验证通过。
- ⚠️ API **当前无鉴权 / 无 CORS / 测试页公开**,仅适合本机;对外暴露前必须加鉴权(见下)。

## 下一个更新方向
- **部署**:给 API 加 Dockerfile + 鉴权,上 Cloud Run(目前仅本机 localhost)。
- **会话长期记忆 / 跨实例共享**(目前本地 SQLite 单节点)。
- AlloyDB 实例删除(数据已全在 Neon,止住月账单)。
