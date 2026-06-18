# v1.1 · 诚实路由 + SQL 自愈
日期:2026-06-18 ｜ 类型:feature

主题:让流水线"答不了就坦白拒答",并让 SQL 节点出错能自愈。

## 更新内容
- 前置 Router(小模型):规划前判可答性与意图;指代上文/越界 → 诚实拒答,不再瞎答;含身份俏皮回复。
- SQL 自愈:sql_query 出错时回喂数据库报错、自动改写重试(对齐沙箱节点);规划期加表名校验。
- 工具:run.ps1 新增流水线/API 启动项、自动取 GCP 项目;新增浏览器测试页 (GET /)。
- 清理:移除旧版 planner 与旧 REPL,统一到 pipeline/。

## 影响 / 注意
- 新增配置:CRITIC_MODEL(默认 gemini-2.5-flash)。
- 行为变化:指代/越界问题现在会被拒答,而非返回错误答案。
- 测试:15/15 通过(router 9 + sql 6)。

## 下一个更新方向
多轮对话升级:**会话记忆 + 跨轮 artifact 存储**。
- "this / above / 那批" 这类指代从「拒答」升级为「真正解析」。
- 对话历史 + artifact 清单接入 Router 与 Planner。
- Router 升级为 turn-router(new / followup / meta 分流)。
