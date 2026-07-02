# 2026-07-02 L 批次 —— 程序记忆重构 + 用户记忆 + 缓存 + SDK 迁移 + UI 刷新

> 来源:[prompt-constitution-lessons](../design/prompt-constitution-lessons.md)(记忆分层设计)+ [frontend-ui-refresh](../design/frontend-ui-refresh.md)(UI)+ 用户直接需求(竞品调研 + UI)。
> 节奏:每项独立 PR → 实测验收 → 对抗 review(3 镜头×双否证)→ 修复 → 终回归 9/9 → 部署。
> 测试:**208 离线测试全绿**;线上探针 L1 回归(17 问)+ 终回归(9 问)。

## 0. PR × 内容 × 回滚

| PR | 内容 | 回滚 |
|---|---|---|
| [#70](https://github.com/kenny0312/videosense-agent/pull/70) | **L1 程序记忆重构**:`_LOOP_SYSTEM` 拆成宪法 + `lessons.py` 教训集(≤15 预算、带出生/来源/退役条件)+ `answer_guard` id 清洗器下沉(命中进 metrics);工具用法归 node_specs 去重。prompt 4444→3527 字符(79%) | revert(纯 prompt/守卫) |
| [#71](https://github.com/kenny0312/videosense-agent/pull/71) | **L3 缓存记账**:隐式缓存已在 L1 稳定前缀上自动命中(实测 cached 3637/5056);usage 记 cached tokens 并按折扣价($0.15/M vs $1.50)计成本 | revert(纯记账) |
| [#72](https://github.com/kenny0312/videosense-agent/pull/72) | **L2 跨会话用户记忆**:每 owner GCS blob + `update_memory` 工具(从严判据);owner 贯通 orchestrator→loop→executor | `USE_USER_MEMORY=0` |
| [#73](https://github.com/kenny0312/videosense-agent/pull/73) | **P1 perception 迁 genai**:视频分析运行时路径迁 google-genai,M4.5 裁剪 hack 删(VideoMetadata 原生 offsets) | revert(接口不变) |
| [#74](https://github.com/kenny0312/videosense-agent/pull/74) | **review 修复**:scrub_ids 全文误伤(哨兵法根治)、CJK 贴邻 id 泄漏(ASCII lookaround)、fail-open、SSE 也清洗、用户记忆读失败不覆盖 + 单行超限截断 + 键消毒 + 资料框架防自持久化注入、time_range 亚秒精度 | revert |
| [#75](https://github.com/kenny0312/videosense-agent/pull/75) | **UI 刷新**:去 follow-up 徽章、trace 移安静页脚、全 chrome 英文化、Tabler 图标、微动效、视频卡/表格/composer/侧栏精致化;修 renderTable 重名 bug | revert(纯前端) |

## 1. 各项验收数据

**L1 程序记忆(治"prompt 只增不减")** —— 拆分本身不省 token(运行时仍拼一个 prompt),止增长的是三机制:下沉(id 禁令→清洗器,prompt 删段)、预算(≤15 硬上限)、退役闭环(清洗命中率→0 即可退役)。prompt 79%。L1 回归 14好/3弱/0破(基线 15/2);弱项揪出真因:精简时误删了 L05 的承重示例(滑雪=ILIKE '%ski%'),恢复后复测 2/2。

**L3 缓存** —— spike 证明隐式缓存在 L1 字节稳定前缀上自动生效(第 3 发 cached=3637/5056,零缓存管理)。usage.py 记 cached、按折扣计价;暖会话 loop 输入成本降 ~60-80%。

**L2 用户记忆(竞品调研点名的能力缺口之一)** —— 会话 A 说「以后问数量直接报数字」→ update_memory 写 GCS;**全新会话 B**「有几个跳伞视频」→「有 14 个跳伞视频。」(跨会话偏好生效);别的 owner 看不到(隔离)。

**P1 SDK 迁移** —— 旧 vertexai 已过官方移除期限(2026-06-24)。运行时视频分析迁 genai,裁剪 hack 删。真视频回归:clip[0,5]=1,661 tok vs 全片 5,068 tok,硬裁剪端到端确认。

**UI 刷新** —— 核心是**常态不宣告**:删 follow-up 徽章(上下文复用是常态)+ trace 移卡片底部安静页脚(`Steps 4 · sql · watch ×2 · show · 12.4s`,点击展开)。全 chrome 英文化(回答语言不变)。预览实测:空状态/视频卡(#N chip + Preview unavailable)/页脚摘要/trace 展开/View SQL 切换全部正常,0 控制台错误,徽章数组空。

## 2. 对抗 review 摘要

3 镜头(logic/state/security)× 每发现 2 否证。部分否证代理撞会话限额 → 争议项逐条人工对码。
- **确认并修复**(#74):scrub_ids 全文残渣清理会误删答案里合法的空 `()`/`''`(一次 id 命中触发)→ 哨兵法只清删除点。
- **复核属实并修复**(#74):`\b` 对 CJK 失效(「视频803…」漏泄漏)→ ASCII lookaround;用户记忆 GCS 读失败被当"无记忆"→ append 覆盖丢失 → 区分 NotFound/抛错;单行超限清空整块 → 截断;`_key` 路径消毒;自持久化注入面 → 记忆框架为"资料非指令";time_range int() 丢亚秒 → `:g`。
- **按设计接受**(记录):静态前缀字节稳定性未破;单实例 per-session 锁已覆盖同会话竞态;P1 非运行时旧 SDK 调用者(critic/sql_fixer/loop_memory summarizer)仍用固定包 —— 下批迁移候选。

## 3. 竞品调研(附带交付)

完整报告 [docs/research/2026-07-02-competitor-analysis.md](../research/2026-07-02-competitor-analysis.md)。要点:
- **真差异化**:懒惰按需看视频的经济模型(~$0.018/视频只在被问时,vs 全员预先索引)+ agent 能"再看一遍"+ SQL 精确计数 + 单用户自托管段无人服务。
- **真差距**(排序):① 无语义检索层(4 角度一致指出)② moment 级结果 UX 弱 ③ 入库富化薄(无转录/GPS)④ 无跨视频结构 ⑤ 打包分发。
- **偷师 top5**:pgvector 语义层(= ingest-standard P2 扩展)、可播 moment 表、入库转录+元数据、低分辨率分诊、NL 动态标签回填。

## 4. 部署与开关

部署:`gcloud run deploy videosense --source . --region us-central1`(保留 env)。新增开关:

| 开关 | 默认 | 作用 |
|---|---|---|
| `USE_USER_MEMORY` | 1 | 0 = update_memory 工具 + 记忆注入都消失 |
| `USER_MEMORY_MAX_CHARS` | 6000 | 用户记忆上限(≈2k token) |

## 5. 已知残留与后续

1. **P2 语义桥(pgvector)** —— 竞品调研把它从"触发式"升为"最该做的下一件事"(4 角度一致 + 偷师 #1)。建议下批优先。
2. **非运行时旧 SDK 调用者**迁 genai(critic/sql_fixer/loop_memory summarizer)。
3. 竞品偷师 #2–#5(可播 moment 表 / 入库富化 / 低分辨率分诊 / 动态标签)按价值排期。
4. UI 后续:消息 markdown 渲染、视频卡时间戳跳转 chips、亮色主题(不在本 PR)。
