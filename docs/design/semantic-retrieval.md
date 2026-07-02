# 设计:语义检索层(V1)—— pgvector 内容级 + 可播时间码片段

> 状态:Design(S1,评审后动手) · 范围:Neon(pgvector 一表)、`pipeline/embeddings.py`(新)、`pipeline/node_executor.py`(新工具 + analyze 写钩子)、`node_specs`/`loop_driver`(工具接入)、回填脚本 · 关联:[ingest-category-standard](ingest-category-standard.md) 的 P2(本文是其内容级扩展)、竞品调研差距 #1 + 偷师 #1/#2、教训 L04/L05 退役条件

## 1. 背景 / 为什么现在做

四角度竞品调研**一致点名**:VideoSense 唯一的结构性缺口 = **没有语义检索层**。现状两条路 —— SQL 精确查(要词表/谓词命中)、全量看片(贵、慢)。中间缺一档:「找到有人开伞的那个瞬间」「海边那种慢镜头」这类**词表覆盖不到、又不值得逐个看片**的问题,现在要么 miss 要么烧钱。

**关键洞察(放大而非背叛"懒惰经济学")**:`analyze_cache` 已经把每一次付费看片的结论缓存着(`{video_id, answer, enough, confidence, evidence_ts}`)。**每次付费观看顺手 embed 一次 → 永久变成免费可检索**。索引不是前置成本(那是 Twelve Labs 的 $2500 起步模式),而是**用出来的副产品** —— 用得越多,免费索引越厚。这是竞品做不到的经济结构。

## 2. 目标 / 非目标

**目标**
1. loop 新增 `semantic_search(query, k)` 工具,定位在 sql_query(精确)与 analyze_video(看片)之间的中间档。
2. 内容级索引:embed 已有的三类文本(见 §4),存 Neon pgvector;检索返回**可播时间码片段**(偷师 #2)。
3. 索引随用增长:analyze_video 出结果时顺手写入(§5)。
4. 存量回填:现有 analyze 缓存 + video_facts + skydive summaries。

**非目标**
- 不做 per-frame 视觉 embedding(那要 Twelve Labs 级视频基座,与本地经济学冲突;远期另议)。
- 不做转录 / GPS 富化(偷师 #3,独立项 V1.5,本文只预留 source 类型)。
- 不引新供应商(embedding 走 Vertex,与现有 genai 同栈)。
- 不动 SQL 精确查路径(语义是**补充**不是替代;数数/筛选仍走 sql_query)。

## 3. 数据模型(Neon,一张表)

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE content_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    video_id    TEXT NOT NULL REFERENCES video_metadata(video_id),
    source      TEXT NOT NULL,        -- 'analyze' | 'fact' | 'skydive'（未来 'transcript'|'caption'）
    snippet     TEXT NOT NULL,        -- 被 embed 的原文（检索时原样返给大脑，人可读）
    start_ts    DOUBLE PRECISION,     -- 片段起（可 NULL）→ 支撑"可播时间码"
    end_ts      DOUBLE PRECISION,
    embedding   VECTOR(768) NOT NULL, -- text-embedding-005 维度
    content_key TEXT UNIQUE,          -- 幂等键（如 analyze 的 ckey）→ 重复写 upsert 不膨胀
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ON content_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 20);
```

- **只 embed 文本**,不 embed 视频帧 —— 单条几百字,一个库几千行,ivfflat 足够(lists=20 对 <1万行绰绰有余;暴力扫也行)。
- `content_key` 幂等:同一 analyze 结果重复看不重复写。
- 不改 `video_facts`(与 ingest-standard 一脉:加表不动老表)。

## 4. embed 什么(只 embed 已存在的文本,零额外看片成本)

| source | 来源 | snippet | 时段 | 数量级 |
|---|---|---|---|---|
| `analyze` | analyze_cache 里每条结果的 `answer` | 看片结论原文 | `evidence_ts`(有则) | 随使用增长 |
| `fact` | video_facts 的 `rationale`(细谓词那条,非大类行) | 活动描述 | `start_ts/end_ts` | ~580 行 |
| `skydive` | skydive_segments 的 `summary` | 跳伞一句话概述 | freefall 时段 | 14 行 |

初始回填就这三类(全是**现成文本**,不需要新看片、不烧 analyze 成本)。转录/caption 留 source 类型位,V1.5 再填。

## 5. 索引随用增长(核心机制)

`analyze_cache.put(ckey, dump)` 成功那一刻(node_executor `_run_analyze_video`),**顺手**把 `dump.answer` embed + upsert 进 content_embeddings(`content_key=ckey`,幂等):
- **best-effort、fail-open**:embed 失败/pgvector 不可用 → 只是这条没进索引,绝不影响看片作答(与全库 fail-open 一致);
- **旁路、不加延迟**:放在结果返回**之后**(或后台线程),不拖慢本轮;
- 效果:你今天花 $0.018 看的每个视频,明天起「语义搜」免费就能找到它。

## 6. 检索工具 `semantic_search`

`node_specs` 加一条(走 DATA_TOOLS,主进程直连,不走 MCP —— 与 web_search 同构):
```
semantic_search(query: str, k: int=8) →
  { results: [{video_id, snippet, source, start_ts, end_ts, score}] }
```
- 实现:embed `query` 一次 → pgvector `ORDER BY embedding <=> $1 LIMIT k` → 返回。
- **prompt 定位(教训/工具声明,非固定路由)**:「精确条件(类别/计数/筛选)→ sql_query;**模糊语义/找瞬间/说不清的描述** → semantic_search 拿候选,再按需 analyze_video 细看;两者可组合」。
- 收口形态(偷师 #2):命中即「第 N 个 · 时段 · 一句描述 · 相关度」,可直接 show_video 播那一段(复用 M4.5 的 time_range 播放)。

## 7. embedding 供应

- 模型:Vertex `text-embedding-005`(768 维;与 ingest-standard P2 选型一致,不引新供应商)。
- 通道:走现有 `genai_client`(embed_content API)或 aiplatform,S2 spike 定死一种。
- 成本:$0.025 / 1M tokens —— 一条 snippet 几十 token,**整库回填 < $0.01**,查询 embed 一次可忽略;远低于一次 analyze。
- 记账:embed 调用计入 usage(与 web_search 同)。

## 8. 里程碑

- **S1 设计**(本文)——评审定 §11 开放问题。
- **S2 embed 基建 + 表**:`CREATE EXTENSION vector` + 建表;`pipeline/embeddings.py`(embed 单文本/批量,带缓存 + fail-open);spike 验通道与维度。**离线单测**:embed mock、维度校验、幂等 upsert。
- **S3 检索工具 + 写钩子 + prompt**:`semantic_search` 节点 + analyze 写钩子 + 工具声明/教训接入;`USE_SEMANTIC_SEARCH` 开关(默认 0,回填完再开)。**验收探针**:「找到开伞瞬间」「海边慢镜头」等词表外问题命中,且答案是可播片段。
- **S4 回填 + 放量**:三类 source 全量回填(幂等脚本,dry-run 先行)+ 开开关 + 21 轮回归确认不劣化 + 对抗 review。

## 9. 与既有架构的接缝(逐一确认无冲突)

| 接缝 | 处理 |
|---|---|
| ingest-standard P2 | 本文取代并扩展它(标签级→内容级);P2 的 category_embeddings 若要仍可加,但内容级已覆盖其用途 |
| analyze_cache | 只**读**它的 dump 做 embed 源;不改缓存逻辑 |
| loop 定位 | 新工具默认 off,开关放量;prompt 走"跟着问题走",不加固定路由(符合既有理念) |
| 成本审计 | embed 计入 usage;隐式缓存不受影响 |
| fail-open | embed 失败绝不影响作答;pgvector 挂 → semantic_search 报错回喂,大脑退回 sql/analyze |
| 教训退役 | L04/L05 的"P2 上线后重评"—— 语义层给了词表外兜底,可择机松绑词表护栏 |

## 10. 风险

- **ivfflat 召回率**:小库(<1万行)lists=20 够;真变大再调 lists 或换 hnsw。低风险。
- **snippet 质量**:analyze answer 是为"回答某个具体问题"写的,未必是好的通用检索文本(问"几个人"的答案 embed 出来搜"开伞"未必命中)。缓解:snippet 存原文,S4 观察召回;必要时 S3 加一句"通用描述"作 embed 源。**这是最大不确定点,S4 重点验**。
- **维度锁定**:换 embedding 模型 = 全库重算。选 005 且写死维度,换模型走独立迁移。

## 11. 评审决策(2026-07-02 已定)

1. **embed 源**:✅ **三类一起上**(analyze + fact + skydive)。S2 建表即三源回填,S3 加 analyze 随用写钩子。索引更厚更快见效。
2. **snippet 质量兜底**:✅ **S4 用数据说话** —— 先上线观察真实召回率,analyze answer 检索质量确实差再加"通用 caption"作 embed 源(不为假想问题提前加成本)。S4 探针重点验此项。
3. **可播片段 UI**:✅ **S3 就做时间轴 marker 增强** —— 命中片段在播放器时间轴上画标记点(对齐 Moments Lab/Frame.io)。注意与刚 refresh 的前端协调,前端改动进 S3 的独立 commit。
4. **入库富化(转录/GPS,偷师 #3)**:✅ **拆 V1.5** —— V1 先把 embed 管道跑通稳定;转录(Whisper 级新依赖)+ GPS(上传元数据解析)独立立项。content_embeddings 已预留 `source='transcript'|'caption'` 类型位。
