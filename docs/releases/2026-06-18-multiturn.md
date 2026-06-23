# v1.2 · 多轮对话:会话记忆 + 跨轮 artifact 复用
日期:2026-06-18 ｜ 类型:feature

主题:把"指代上文"从【诚实拒答】升级为【真正解析】—— 同一会话里可以接着问。

## 更新内容
- **会话记忆层**(`pipeline/session.py`):按 session_id 存对话历史 + 可被指代的结果清单(artifact catalog)。纯内存、接口可换(将来可换 Redis/DB)。
- **跨轮 artifact 复用**(策略=重算 / 存配方):每条 artifact 存上一轮的 SQL 或整张 DAG 当"配方" + 一份小预览;follow-up 时 Planner 拿配方**重建一个全新自洽 DAG**。不改执行器、不加节点类型、DAG 仍可审计;视频事实静态,重跑无漂移。
- **Router 升级为 turn-router**:把历史 / 结果清单【渲染进 prompt】,指代能在已保存结果里对上号就解析(`turn_type=followup/meta`、`references.resolved_to` 填 artifact id);对不上号仍诚实拒答。
- **meta 路径**:"你刚才怎么算的?"用纯 Python 模板回放上一轮的 SQL / 步骤链(只说**用了什么**,不编造**为什么**,也不再调模型)。
- **防误指代**:orchestrator 用真实 id 集合校验 `resolved_to`,丢弃模型幻觉 id;follow-up 解析为空 → 降级为诚实拒答,不瞎规划。
- **接入**:API 新增可选 `session_id`(省略则开新会话、响应回传);内置测试页持久化会话 + "新会话"按钮;CLI 整个 REPL 共用一个会话、`:new` 重置。

## 影响 / 注意
- API 契约新增可选字段:请求 `session_id`;响应 `session_id` / `turn_type`(`new` / `followup` / `meta`)。**旧客户端不带 `session_id` 仍可用**(每次独立单轮,行为与 v1.1 完全一致)。
- 视图非对称控制 prompt 体积:Router 只看 `{id,label,preview,n}`(**不含 recipe**),Planner 只拿【已解析】那条的 recipe。
- 容量上限:每会话最近 12 轮 / 20 个 artifact;**只存预览不存完整结果值**,限制内存与 prompt。
- 测试:**34/34 通过**(session 13 + 多轮编排 6 + router 9 + sql 6)。

## 下一个更新方向
- 会话持久化(Redis/DB 后端;目前纯内存,进程重启即忘)。
- 跨轮"注入 / 存数据"复用:沙箱产物(回归、传感器数据等不便重算的)直接复用上一轮的值,作为"重算"之外的补充。
