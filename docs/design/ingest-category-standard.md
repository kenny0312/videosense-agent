# 设计:入库标准 —— 受控大类 + 自由小类 + 语义查询桥

> 状态:Design(评审后动手) · 范围:`perception/`(两条入库管道)、`pipeline/`(词表/normalize/查询桥)、Neon(两张小表) · 关联:跳伞"没有"事故(PR #59 是点状修复,本文是标准化根治)、`architecture-prefer-simplicity`

## 1. 背景 / 问题
「想看跳伞的视频」→ 答「数据库里没有跳伞视频」,但库里有 14 个。点状原因是标签只写了窄的 `wingsuit skydiving`;**结构性原因**有两个:
1. **没有统一的类别层**:ActivityNet 管道(`gemini_predicates`)写了 195 个自由文本细谓词(「clipping quickdraws」级别),跳伞管道(`skydive_extract`)自己一套 —— 各管道各写各的,没人保证"大类"存在且一致。
2. **查询靠大脑现场翻译 + keyword 匹配**:「跳伞」→ 现场译英 → ILIKE,时松时紧,还可能复用上一轮的「没有」。

用户的大类问题(有没有 X / 想看 X)撞上库里的细标签 → 这一类失败会反复出现,补一个标签治不了根。

## 2. 目标 / 非目标
**目标**
1. **入库标准**:每个视频【必须】有 1 个受控大类(最多 2),外挂 0–10 个自由小类。任何管道入库都走同一个 `normalize_category`。
2. **查询桥**:「有没有 X / 想看 X」这类大类检索稳定命中 —— 中文/同义词/别名都对得上受控词表。
3. **护栏**:没做过"像样的大类广搜"之前,loop 不许下「没有」结论。

**非目标**
- 不给【每个视频】做内容 embedding(那是"语义搜视频内容"的另一档能力,与本标准正交,远期单独立项)。
- 不重构 `video_facts` 表结构(不加列、不迁移;大类就是一条谓词行,靠词表 join 区分)。
- 不动 195 个既有小类(照旧,给"他在干嘛"级细节用)。

## 3. 数据模型(不改 video_facts,加两张小表)

```
categories(label TEXT PRIMARY KEY)                  -- 受控大类词表,~30–50 个(英文,如 skydiving/skiing/climbing…)
category_aliases(alias TEXT PRIMARY KEY,            -- 别名/同义词/中文 → 大类
                 label TEXT REFERENCES categories)  --   跳伞→skydiving, wingsuit→skydiving, 翼装→skydiving, 滑雪→skiing…
```

- **视频的大类 = `video_facts` 里 predicate ∈ categories 的那(1–2)条行**。查「有什么类别」= join categories;查「有没有跳伞」= alias 解析 → `predicate = 'skydiving'` 精确命中。**零 schema 迁移**(PR #59 补的 `skydiving` 行天然就是第一条大类行)。
- 小类照旧是不在词表里的自由谓词。
- 词表落库(不散在代码里)→ 可审可改;改词表 = 改数据,不用发版。

## 4. 数量标准
| 层 | 规定 | 理由 |
|---|---|---|
| 大类 | **恰 1 个主类,真混合场景最多 2**;必须 ∈ categories | 分类要互斥+全覆盖 → 恰 1 才可靠;定死 2 会逼编造 |
| 小类 | **0–10 个**,不定死;英文动词短语 | 描述层丰富度天然不同(现状 ≈2 条/视频);定死数量 = 凑数或截断 |

## 5. 入库改造:`normalize_category`
`pipeline/taxonomy.py`(新,纯函数 + 查表):
```
normalize_category(raw: str) -> str | None
    # ① exact/casefold 命中 categories → 返回
    # ② 命中 category_aliases → 返回其 label
    # ③ 都不中 → None(调用方决定:入库时 None = 让 LLM 从词表里选/人工补;绝不自由发挥造新大类)
```
- `gemini_predicates`(ActivityNet):分类 prompt 里【给出词表】让模型从中选主类 → 过 normalize 校验 → 写大类行 + 细谓词行。
- `skydive_extract`:已写 `skydiving`(+wingsuit 细类),改为过 normalize(行为不变,走同一入口)。
- **回填**:一次性脚本 —— LLM 把现有 195 个谓词映射到词表(产出映射表,人工可审),再按映射给 114 个视频补大类行(幂等 upsert)。

## 6. 查询桥(两级,便宜的先上)
**P1(先上,可能已够)—— 词表进 prompt**:把 ~30–50 个大类【直接注入 loop 的 system prompt】(schema 已经注入了,加 50 个词可忽略)。大脑看得见 `skydiving` 在词表里,「跳伞」的映射就不再靠盲翻译;prompt 同时加一条护栏:**「答『有没有 X』前必须先对到词表大类(或明确说词表里没有近似类);没对过不许说『没有』」**。

**P2(P1 不够再上)—— pgvector 语义兜底**:
- **存哪:就在 Neon**(`CREATE EXTENSION vector`),一张 `category_embeddings(label, embedding vector(768))` —— **只 embed 标签空间**(~50 大类,可选 +195 小类 ≈ 250 行),不 embed 视频。
- 模型:Vertex `text-embedding-005`(不引新供应商)。词表变更时重算(罕见);查询时 embed 用户那一个词,常见词缓存。
- 形态:给 loop 一个 `match_category(term)` 小工具(先查 alias 表,miss 再向量近邻),返回 canonical 大类 + 置信度。
- 250 行不需要向量索引,顺手也能进程内暴力算 —— 但落 Neon 与数据同库更干净。

## 7. 护栏(与自检打通)
- prompt(P1 那条):没对词表就不许「没有」。
- critic B(已上线,开关关着):若开启,critic 对「用户问有没有 X、答案是没有」的收口追问一句「是否已按大类+别名广搜」。

## 8. 改动点
| 文件/资源 | 改动 |
|---|---|
| Neon | `categories` + `category_aliases`(+P2:`category_embeddings`,`CREATE EXTENSION vector`) |
| `pipeline/taxonomy.py`(新) | 词表加载(带缓存)+ `normalize_category`(+P2:`match_category`) |
| `perception/gemini_predicates.py` | 分类 prompt 给词表选主类;写大类行 |
| `perception/skydive_extract.py` | 大类写入改走 normalize(行为不变) |
| `pipeline/loop_driver.py` | 词表注入 system prompt + 「没对词表不许说没有」护栏 |
| 回填脚本(一次性) | 195 谓词 → 词表映射(LLM 产出、人工可审)→ 给 114 视频补大类行 |

## 9. 里程碑
- **T1 词表 + 表 + normalize + 回填**:两张小表、初版 ~30 词表(含中英别名)、normalize、回填 114 个视频的大类行。验收:每个视频 ≥1 个大类行;「有什么类别」返回的是干净大类。
- **T2 查询接通(P1)**:词表进 prompt + 护栏。验收:「有没有跳伞/滑雪/攀岩」「想看 X」全部稳定命中;故意问词表外的(如「有没有做菜」若无此类)→ 诚实说没有该类。
- **T3(选做,P2)**:pgvector + `match_category`。验收:刁钻说法(「高空飞的那种」)也能对上 skydiving。

## 10. 开放问题(评审定夺)
1. **初版词表谁定**:我按 195 个谓词聚出 ~30 个初稿给你审,还是你直接给一份?(倾向前者)
2. **大类行的 rationale 写什么**:回填时用"derived from predicates: …"这类溯源说明?(倾向是)
3. **P2 触发标准**:T2 上线后观察一段,出现 prompt 对不上的真实 case 再上 pgvector?(倾向是 —— 别为假想需求建基建)
