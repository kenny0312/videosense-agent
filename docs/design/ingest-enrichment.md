# 设计:V1.5 入库富化 —— 转录 + caption(+ 可选遥测)

> 状态:Design(S1,评审后动手) · 范围:`pipeline/enrichment.py`(新)、`perception/setup_enrichment.py`(回填)、`api/server.py`(上传完成通知)、content_embeddings(复用 V1) · 关联:[semantic-retrieval](semantic-retrieval.md)(transcript/caption 类型位早已预留)、竞品偷师 #3+#4

## 1. 背景与 spike 结论(先有实据再设计)

竞品差距 #3:别人入库时免费算好的东西(转录/描述),我们要现场花钱问。V1 语义层就位后,transcript/caption 是**最好的 embed 源** —— 尤其转录能解「他说了什么」这类现在完全无解的问题。

**Spike 实测(2026-07-02,库内真视频)**:
1. **Gemini 转录可用,Whisper 不需要** —— flash 对有话视频输出 8-10 段带时间戳的干净转录(STRICT JSON);对纯风噪的跳伞视频诚实返回 `has_speech=false`。**零新依赖**(不装 Whisper、不加 GPU、复用 genai client)。
2. **低清模式(media_resolution=LOW)转录质量不降,省 3.1×** —— 18,205 → 5,894 tokens,段落与文本等价(音频 token 不受视频清晰度影响)。竞品偷师 #4 顺手落地在转录场景。
3. 成本:有话视频 ≈ $0.002(low res)/个,无话更低;**114 个存量回填 ≈ $0.2-0.3,一次性**。

## 2. 目标 / 非目标

**目标**
1. 每个视频入库时(上传 + 存量回填)自动获得:**转录**(带时间戳分段)+ **一句话 caption**(为检索优化的通用描述);
2. 全部进 `content_embeddings`(source='transcript'/'caption',V1 预留位)→ 语义检索直接变强;
3. 「他说了什么 / 找到有人喊 XX 的段」类问题可解(现在完全无解)。

**非目标**
- 不装 Whisper/GPU(spike 证明不需要);
- 帧级视觉 embedding(远期);
- 遥测(GoPro GPMF → 海拔/速度)**本期不做**,见 §7 开放问题 ②。

## 3. 富化内容

| 项 | 做法 | 存哪 |
|---|---|---|
| **转录** | flash + media_resolution=LOW,STRICT JSON 分段(5-15s 自然停顿);`has_speech=false` → 跳过 | 每段一行 `content_embeddings(source='transcript', start_ts, end_ts)`,content_key=`tr:{vid}:{i}` |
| **caption** | 同一次调用顺带产出:`caption` 字段 = 1-2 句"这个视频在拍什么"(为检索写,与 analyze 的"答某个具体问题"互补) | 一行 `source='caption'`,content_key=`cap:{vid}` |

**一次调用两产出**(转录 prompt 里加 caption 字段)—— 不多花一次视频 token。

## 4. 触发时机

- **上传路径(M5)**:前端 PUT 直传 GCS 成功后调用新端点 `POST /v1/enrich {video_id}`(幂等,校验 up_ 注册表/白名单)→ 后台线程 enrich(不阻塞响应)。之前上传完全没有入库处理,这补上了"新上传天生可语义搜"。
- **存量回填**:`perception/setup_enrichment.py`(断点续跑:跳过已有 `cap:{vid}` 键的;幂等 upsert)。

全程 fail-open:enrich 失败只损失富化,不影响任何现有功能。

## 5. 改动点

| 文件 | 改动 |
|---|---|
| `pipeline/enrichment.py`(新) | `enrich_video(video_id, gcs_uri)`:一次 flash 调用(low res)→ 转录分段 + caption → embed → upsert(复用 embeddings/semantic_index) |
| `api/server.py` | `POST /v1/enrich`(幂等、白名单、后台线程) |
| `web/index.html` | 上传 PUT 成功后调 /v1/enrich(一行) |
| `perception/setup_enrichment.py`(新) | 存量回填(断点续跑 + dry-run + 报告) |
| 测试 | enrichment 纯函数(分段→entries)、端点校验、fail-open |

## 6. 验收

- 探针:「视频里有人说 spin 的是哪个」「他开头说了什么」→ transcript 段命中并可播那一段;「有没有健身教学视频」→ caption 命中;
- 回填报告:114 个中有话/无话分布、embed 行数、成本实测;
- 全量测试 + 不回退(语义检索原有 4 问)。

## 7. 开放问题(评审定夺)

1. **caption 是否也写进 video_metadata 列**(除了 embeddings)?(倾向:**只进 embeddings** —— SQL 面已有谓词/大类,双写会漂移;要看 caption 语义搜就够)
2. **遥测(GoPro GPMF → 海拔/速度)**:你的翼装场景里「4000 米以上的跳」这类查询有吸引力,但需要下载视频 + gpmf 解析工具(新依赖 + 重 IO)。(倾向:**本期不做**,单独 spike 立项 —— 若 GX 文件真含 GPMF 且解析轻,再作 V1.6;不阻塞转录/caption)
3. **转录语言**:prompt 让模型保留原语言(英文视频出英文段)。多语言 embedding 已就位,中文查询能命中英文转录。(倾向:保留原语言,不翻译 —— 少一步失真)
