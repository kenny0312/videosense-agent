# v1.5 · 技能路由框架 + 跳伞垂直 + 视频展示工具
日期:2026-06-26 ｜ 类型:feature

主题:把路由从【写死的意图枚举】升级成【数据驱动的技能注册表】,据此落地第一个领域垂直(跳伞:受控阶段抽取 + null-safe 元数据表),并新增把数据库里的视频/片段直接在问答界面播放的 `show_video` 工具。

## 更新内容

### 技能路由框架(skills/*.md)
- Router 输出从硬编码 `intent` 枚举改为 `route`(取自 `skills/*.md` 渲染的清单)。**加一个大类 = 丢一个 `.md`**,router/orchestrator 的判断逻辑零改。
- orchestrator 按 `handler_for(route)` 分派:`handler=planner` 走 Planner→DAG 主链路;自定义 handler 走 `skills/handlers.py` 的 workflow(主进程、可联网调 Gemini)——为"不同大类走不同 workflow"打好地基。
- smalltalk 不再回固定一句:小模型按人设/能力边界生成【可变】回复,任何失败 fail-open 回退到固定俏皮回复。
- `RouterVerdict` 新增 `route`(与旧 `intent` 并存、互相回填),契约向后兼容。

### 跳伞垂直(受控词表 + null-safe 抽取)
- 新表 `skydive_segments`(**每视频一行**):受控阶段 `aircraft / exit / freefall / deploy / canopy / landing` 各占一组 `*_start_ts / *_end_ts / *_confidence` 列,外加 `jump_type / is_wingsuit / summary / freefall_sec`。
- **Null-safety 是核心设计**:不是每个视频都有全部阶段(只拍了自由落体的片段就没有 landing)→ 缺席阶段一律 **NULL**,绝不用 `0.0` 假值冒充(`0.0` 是合法的第 0 秒)。三道防线:Pydantic 全 `Optional=None`、派生指标缺端 → `None`、查询用 `IS NOT NULL`。
- `perception/skydive_schema.py`:受控词表 + 表结构 + `to_row` 的**单一真源**。`perception/skydive_extract.py`:Gemini 多模态读 GCS 视频 → 每视频写一行;断点续跑、失败跳过不崩、自动建表(沿用 `gemini_predicates` 骨架)。
- `pipeline/skills/skydive.md` 让 router 认出跳伞问题;`skydive_segments` 进 `BUSINESS_TABLES`(planner 可见)+ `repl/_mock_db.py` seed(4 个跳伞 mock 视频,阶段**故意各有缺失**以证明 null-safe)。

### show_video 工具 + 前端视频展示
- 新增 DAG 工具 `show_video`(`node_specs` + `dag_schema` 登记;**主进程节点**)——首个"消费上游 + 主进程联网"的数据节点。把上游选出的 `video_id`(+片段 `start_ts/label`)对应的私有 `gcs_uri` 签成可播放 https,放进响应 `videos` 侧信道(仿 `plot` 的产物通道)。
- planner **自动编排**:"给我看我最长的翼装飞行" → `sql_query → show_video`。
- 前端 `renderVideos()`:气泡内嵌 `<video>` + 点击 mark 跳播某阶段;签不出 URL 时优雅降级"暂不可播放",不崩。
- 签名(`pipeline/video_url.py`):Cloud Run 服务账号走 **IAM signBlob**;本地用户 ADC 签不出 → 返回 `None`(fail-open)。

### 其它(自 v1.4 起的零散改进)
- **跨轮 artifact【值】复用 + Redis 值仓**:把上一轮算好的真实值另存进【独立值仓】(TTL,SQLite/Redis 两后端),follow-up 若是"把同一份结果原样重画/重排"可 `load_artifact` 直载、免重跑配方;**强默认仍是重算**(数据/筛选/范围有任何变化即重算)。
- **会话按认证身份归属**:会话绑定到鉴权 `owner`,修掉客户端任意传 `session_id` 就能读他人会话的 **IDOR**(v1.4 redis 后端只把它列为待办,本阶段已修)。
- **沙箱身份令牌**:Cloud Run 调私有 sandbox 的身份令牌改用 `google-auth` 获取(不依赖 `gcloud` CLI),headless / 容器内可靠。
- **plot_url 走 https**:uvicorn `--proxy-headers`,让 Cloud Run 反代后端产出的图表 URL 用 https,修掉前端混合内容(http 图被拦)。

## 影响 / 注意
- **API 契约向后兼容**:响应新增 `videos` 字段(可空数组);`RouterVerdict` 新增 `route`(与旧 `intent` 互相回填)。
- ⚠️ `show_video` 线上要真能播:Cloud Run 运行时 SA 需对**自身**有 `roles/iam.serviceAccountTokenCreator`(IAM signBlob 签名权限)。本地签不出属预期,卡片显示降级态。
- ⚠️ 跳伞抽取是**离线批处理**(不在问答主链路):先把视频传到 GCS + 入 `video_metadata`,再跑 `python -m perception.skydive_extract`(自动建表、断点续跑)。
- **数据底座局限**:纯视觉多模态只能拿到**定性阶段**(出舱/自由落体/开伞/降落);高度、下降率、滑翔比等**物理量拿不到**,需 FlySight/GPS 进未来的 `jump_metrics` 表。
- 测试:离线 **71/71**(router 9 + 多轮 6 + session 19 + artifact 23 + show_video 7 + skydive 7);跳伞查询(含 null-safe "只有自由落体没开伞")+ `show_video` 端到端(mock,planner 自动 `sql_query→show_video`)+ 前端 `<video>` 渲染均验证通过。

## 下一个更新方向
- **`jump_metrics` 新表 + FlySight/GPS ingestion**:逐秒高度/速度/滑翔比 → 解锁开伞高度低开告警、滑翔比排行、下降率曲线(真物理 KPI,翼装党最想要的)。
- **视频上传入口 + `recorded_at`**:本地视频转码上传 GCS、绑定 FlySight log;`recorded_at`(真实跳伞日期)支撑"按月成长曲线 / 开伞越来越果断"的进步叙事。
- **show_video 增强**:`phase_timeline` 节点(出舱→自由落体→开伞→降落的时间带 SVG)、片段缩略图墙、进度条上点章节跳播。
- **危险/安全 predicate**:抽取里加 `hard_opening / spinning / 低开`,翻全库找危险时刻并一键回看。
- **上线**:部署到 Cloud Run + 配 `serviceAccountTokenCreator`,让视频在问答界面真能播。
