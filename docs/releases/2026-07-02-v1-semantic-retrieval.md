# 2026-07-02 V1 —— 语义检索层 + 视频重签修复 + 废弃代码清理

> 竞品调研点名的唯一结构性缺口(差距 #1:无语义检索)落地;顺带修了你报的"重开无法播放"、清了 one-loop 稳定后的废弃层。5 个 PR,各自独立可回滚;对抗式 review 后修复(verify 撞限额,逐条人工裁决)。

## 0. 一览

| PR | 内容 | 回滚 |
|---|---|---|
| [#78](https://github.com/kenny0312/videosense-agent/pull/78) | **视频重签** `/v1/resign`:修"离开页面重开后视频无法播放"(签名 URL 15 分钟过期,前端存了旧的) | revert |
| [#79](https://github.com/kenny0312/videosense-agent/pull/79) | V1 设计文档 + 四项评审决策 | revert |
| [#80](https://github.com/kenny0312/videosense-agent/pull/80) | **V1 语义检索**:pgvector 内容级索引 + semantic_search 工具 + 随用增长写钩子 + 时间轴 marker | `USE_SEMANTIC_SEARCH=0` 或 revert |
| [#81](https://github.com/kenny0312/videosense-agent/pull/81) | **废弃代码清理**:删 router/skills/gate(净删 903 行) | revert |
| [#82](https://github.com/kenny0312/videosense-agent/pull/82) | review 修复:ivfflat 召回、resign 注入防护、连接线程安全、docstring 诚实 | revert |

## 1. 语义检索(核心)

**为什么**:此前两条路 —— SQL 精确查(要词表/谓词命中)、全量看片(贵)。中间缺一档:「有人摔倒的画面」「海边慢镜头」这类词表覆盖不到、又不值得逐个看片的问题。四角度竞品调研一致点名这是唯一结构性缺口。

**怎么做**:
- **内容级 pgvector**(Neon `content_embeddings` 表):embed 三类现成文本 —— video_facts rationale(473)、skydive summary(14)、analyze 结果缓存(随用增长)。**只 embed 文本、不 embed 视频帧**(后者要 Twelve Labs 级基座,与本地经济学冲突)。
- **`semantic_search(query,k)` 工具**:定位在 sql_query(精确)与 analyze_video(看片)之间。返回**行列表** → 可直接作 show_video 上游,片段带时间段流到播放器。
- **随用增长索引(核心洞察)**:每次成功 analyze_video 顺手 embed+upsert(旁路、fail-open,键=缓存键幂等)。**每次付费观看永久变成免费检索** —— 放大懒惰经济学护城河而非背叛它。**验收探针跑的过程中自己往索引加了 6→20 条 analyze**,机制在真实运行中验证。
- **可播时间码 UI**:播放器下方片段 marker 条,点击跳到那一刻。

**关键技术决策**:
- **embedding 必须多语言型号**(`text-multilingual-embedding-002`,768 维)。005 是英文模型,S2 实测中文查询退化(四个不同问题命中同一批结果、分数几乎相同);多语言修复后中文精准。
- **不建 ivfflat**(review 修):库小(几百~几千行),精确 KNN 顺序扫亚毫秒且 100% 召回;ivfflat 在小表 + probes=1 下只扫 1/20 列表会漏最近邻。>5 万行再上 hnsw。
- **直连 Neon 不走 MCP**:语义层是带参数化向量的专用类型化读写路(与 uploads/user_memory 直连同理);MCP 保留给大脑的通用只读 SQL。`_EXEC_LOCK` 串行化游标(共享连接在并行 analyze 写钩子下线程安全)。

**验收(实测)**:「有人摔倒」→ semantic 命中滑板摔倒集锦(索引长出中文 analyze 答案后分数 0.732→0.866)、「海边悠闲」→ 沙滩堆沙堡;跳伞/做饭计数仍走 SQL 不被抢。`USE_SEMANTIC_SEARCH=1` 默认开(0 = 工具+写钩子消失,零残留)。

## 2. 视频重签(修你报的 bug)

**现象**:离开页面重新打开历史会话,视频无法播放。**根因**:签名播放 URL 15 分钟过期(安全设计),前端却把它连同回答存进 localStorage,重开时用的是过期 URL → GCS 403。**修法**:`/v1/resign` 端点按 video_id 重签;前端持久化前剥掉短命 URL,历史渲染时重签,onerror 兜底覆盖"页面开着超时再点播"。id 过白名单再拼 SQL(防注入,review 加固)。

## 3. 废弃代码清理(one-loop 稳定后)

删 `router.py`(152 行)+ `skills/`(loader/handlers/5 个 md)+ `USE_ROUTER_GATE` 开关 + `catalog_for_planner` + gate 分支 + skills 分派。**净删 903 行**。理由:one-loop 稳定运行数周,gate 一直为 0,自定义 handler 从未注册。`test_multiturn` 重写为无 verdict 版(8 测)。**保留**:旧 vertexai SDK conversation(是 `LOOP_MODEL=gemini-2.5-flash` 回滚路径,不是死代码)、main.py dev CLI。

## 4. 对抗式 review

3 镜头(logic/state/security)× 双否证。**verify 代理全部撞会话限额** → 所有发现默认落到 rejected,逐条人工裁决:
- **确认并修复**:ivfflat 召回退化(删索引);
- **预先已修**(review 返回前):resign SQL 注入面(白名单)、共享连接线程安全(_EXEC_LOCK);
- **诚实化**:resign docstring 谎称"owner 作用域" → 改为如实说明(与 show_video 同敞口,单用户无影响);
- **按现状接受**:resign 无限流(单用户,与其他端点一致);owner 隔离 = deferred upload-IDOR task。

## 5. 验证与部署

- **测试**:205 → **206 passed**(+resign 注入防护测试;−13 router/gate 测试后净值)。
- **线上探针**:S4 验收 4 问 + 删索引后精确检索复测 4 问 + 终回归 4 问。
- **成本**:整库 embed 回填 < $0.01;查询 embed 一次可忽略;随用增长零额外看片成本。
- **数据**:content_embeddings 503 行(fact 473 + skydive 14 + analyze 随用增长);ivfflat 已删,精确 KNN。
- **部署**:rev `videosense-00031`(`gcloud run deploy --source .`,保留 env;`USE_SEMANTIC_SEARCH` 代码默认 1)。

## 6. 后续(未排期)

- 语义 snippet 质量:若 analyze answer 检索质量不够,每次 analyze 顺带生成通用 caption(S4 决策留的兜底,目前实测够用)。
- V1.5 入库富化(转录/GPS,偷师 #3);低清粗筛(#4);动态标签(#5);MCP server 暴露。
- 竞品偷师 #2(排序可播片段答案)已随 marker UI 部分落地。
- upload-IDOR owner 隔离(resign + show_video 共用,一处修)。
