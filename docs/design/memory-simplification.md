# 简化 VideoSense 记忆架构 —— 收敛为「一份持久 transcript + 一个便宜 Router 门」

> Status: Draft for review · 评审后即动手
> Scope: `pipeline/` 记忆层(session / catalog / transcript / artifact_value_store / router / orchestrator / loop)
> 一句话:**把「session/catalog + transcript 两套记忆」收敛成「一份持久 transcript(Redis 热尾 + GCS 全量)+ 一个不做指代解析的便宜 Router 分流门」。指代/meta 迁回 loop,靠 transcript 回放自己做。**

---

## 1. 背景:为什么现在是两套,为什么冗余

### 1.1 两套记忆并存(历史包袱)

VideoSense 现在同时维护**两套互不相同的记忆系统**:

| 套 | 文件 | 由来 | 现在还干什么 |
|---|---|---|---|
| **Session / Catalog**(套一) | `pipeline/session.py` + `pipeline/artifact_value_store.py` | **DAG / recipe 时代遗留**:当年 catalog 里存着 recipe(步骤链),是多轮复用与指代的核心 | recipe 已在 M6→M7b 删掉,catalog 退化成**纯 handle 索引**(id/kind/label/preview/n + 值复用指针),只剩三个用途:① Router 指代解析 ② followup/meta 拒答门 ③ load_artifact 值复用指针 |
| **Transcript**(套二) | `pipeline/transcript_store.py` + `pipeline/loop_memory.py` | **loop(probe-and-step)上来后另起的 CC 式记忆**,M7b 起是唯一执行路径的上下文来源 | 记录每轮 user/tool_call/tool_result/answer 四类事件;followup/meta 时回放(`build_loop_context`)喂给 loop |

**关键事实**:loop 上来时**只另起了 transcript,没拆旧的 catalog**。recipe 删了,但 catalog 的「指代解析 + 拒答门」逻辑没跟着迁走,于是两套记忆并行存活到今天。

### 1.2 冗余带来的具体问题

- **#4 指代两套打架**:`build_loop_context` 回放(`loop_memory._render_turn`)里**本就带 event_id + preview + 完整工具链**,loop 完全能自己定位「这个/那个」。但现在指代解析却被前置在 Router(`router.py` `_router_prompt` 把 catalog 渲进 prompt → 填 `resolved_to`),orchestrator 再用 `session.resolve_references()`(`orchestrator.py:140`)做集合校验。**同一件事(把"这个"映射到具体结果)做了两遍**:Router 在 catalog 上做一遍,loop 拿到回放后实际又能做一遍 —— catalog 那遍是多余的前置门。
- **session 无持久化 → 24h 后失忆**:catalog/history 全在 session blob 里。Redis 后端 `SET ... EX 86400`,24h 后**自动删且无 GCS 备份**;SQLite 后端靠懒清理 `sweep_locked()` 删。一旦 blob 过期,Router 看不到 catalog → followup/meta 拒答门误触发"我没有可参考的上一轮结果"。但**与此同时 transcript 的 GCS 全量真相还在**(`transcripts/{owner}/{sid}/{seq}.json` 无限期保留)—— 真相还在,门却失忆了。
- **认知负担**:两套 TTL、两套读写时序、两套测试(test_session.py 19 + test_artifact_value_reuse.py 19 + test_redis_artifact_value_store.py 13 + test_redis_session.py 等)。改一处记忆行为要同时想两套,效果不好。

---

## 2. 现状全貌(据代码核实:存哪 / TTL / 谁读写)

### 2.1 Session blob(套一·主体)

| 项 | 事实 |
|---|---|
| 装什么 | `history`(≤12 轮 Turn) + `rolling`(被淘汰的老轮 ≤20) + `catalog`(≤20 个 Artifact handle) + 元数据(`_seq`/`_turn_no`) |
| 存哪 | Redis:`vs:session:{owner}:{sid}` **或** SQLite:`~/.session_store.sqlite`(单 blob 表);整个 `Session.to_dict()` → JSON,无压缩 |
| TTL | `SESSION_TTL_SECONDS = 86400`(24h)。Redis `EX` 自动删;SQLite 懒清理 |
| 备份 | **无 GCS 备份** |
| 谁写 | `session.save()`(每请求结束一次) |
| 谁读 | `session.get_or_create()`(请求开头);`catalog_view()`→Router;`history_view()`→Router;`resolve_references()`→orchestrator;`get_artifact()`→meta 模板 |

### 2.2 Artifact 值仓(套一·值复用)

| 项 | 事实 |
|---|---|
| 装什么 | 仅「可复用」artifact 的真实 preview_value(`_is_reusable()`:ols_regress/merge_asof/interpolate/python/plot/load_sensor_csv 等;**排除** sql_query/threshold_sweep/load_artifact) |
| 存哪 | Redis:`vs:artifact:{sid}::{aid}` **或** InMemory(进程 LRU,256 条/256KB) |
| TTL | `ARTIFACT_VALUE_TTL_SECONDS`(默认 = 24h);InMemory 重启即丢 |
| 备份 | **无 GCS** |
| 谁写 | `session.register_artifact()`(成功轮,条件 `put`) |
| 谁读 | loop 的 `load_artifact` node;miss → 软失败 → loop 自动改走重算 |

### 2.3 Transcript(套二·三层,GCS 已是全量真相)

| 层 | 存哪 | 保留 | TTL | 谁读写 |
|---|---|---|---|---|
| 热尾 | Redis LIST `vs:tx:{owner}:{sid}` | 最近 200(HOT_WINDOW),RPUSH+LTRIM | 24h(每次 append 刷新 EXPIRE) | 写:`record_loop_turn`;读:`build_loop_context` |
| 耐久 | GCS `transcripts/{owner}/{sid}/{seq:09d}.json` | **全部,append-only** | **无限期** | 写:每轮;读:离线/复盘 |
| 溢出 | GCS `tool-results/{owner}/{sid}/{event_id}.json` | 大本体(>8KB / 非 JSON) | **无限期** | transcript 行只留 `result_ref` + preview |

事件四类:`user` / `tool_call`(带 `event_id` + `uses`) / `tool_result`(带 `event_id` + preview + n) / `answer`。回放渲染见 `loop_memory._render_turn`:**event_id、工具名、inputs、preview、答案全在一个文本块里** —— 这是「指代可迁回 loop」的事实基础。

---

## 3. 目标架构

### 3.1 一图

```
                        ┌──────────────────────────────────────┐
   用户 nl + sid ─────▶ │  Router(便宜分流门 · 不做指代解析)    │
                        │   输入: nl + schema + tools           │
                        │        (+ transcript 尾? 见开放问题)  │
                        │   输出: smalltalk | refuse | answer   │
                        │          + turn_type(new/followup/meta)│
                        └───────────────┬──────────────────────┘
                                        │ answer
                                        ▼
                        ┌──────────────────────────────────────┐
                        │  Loop(probe-and-step · 唯一执行路径)  │
                        │   followup/meta → 回放 transcript      │
                        │   ┌── 指代解析「这个/那个」(回放里有id)│
                        │   ├── meta「你怎么算的」(回放即真相)   │
                        │   └── clarify(指代不到 → 自己反问)     │
                        └───────────────┬──────────────────────┘
                                        │ 每轮 append
                                        ▼
                        ┌──────────────────────────────────────┐
                        │  一份持久 Transcript(唯一记忆)        │
                        │   Redis 热尾(200, 24h) ─ 低延迟回放    │
                        │   GCS 全量(无限期) ───── 真相源/审计   │
                        └──────────────────────────────────────┘

       已删:catalog · register_artifact · resolve_references · followup resolved_ids 门
       瘦身/删:session blob(history/rolling/catalog)
       待拍板:artifact_value_store(值复用)去留
```

### 3.2 读写时序对比

**现在(两套):**
```
读:  get_or_create(session blob) → catalog_view + history_view → Router(在 catalog 上解析指代,填 resolved_to)
     → resolve_references(集合校验) → followup/meta 拒答门 → [若过门] build_loop_context(transcript 回放) → loop
写:  loop 出答案 → register_artifact(写 catalog + 写值仓) → record_turn(写 history) → save(写 session blob)
     → record_loop_turn(写 transcript 热尾+GCS)
        ▲ 同一轮写了两套:session blob 一套、transcript 一套
```

**目标(一套):**
```
读:  Router(nl + schema + tools)→ smalltalk/refuse 早退;answer 带 turn_type
     → followup/meta: build_loop_context(transcript 回放)→ loop 自己解析指代/meta/clarify
写:  loop 出答案 → record_loop_turn(写 transcript 热尾+GCS)
        ▲ 只写一套
```

收益:每轮少一套写(session blob)、少一次 Router 端指代解析、少 `resolve_references` 集合校验;读路径不再依赖会 24h 失忆的 session blob。

---

## 4. 砍除清单 + 迁移去向 + 测试改动

| 砍除项 | 现位置 | 迁移去向 | 牵连测试怎么改 |
|---|---|---|---|
| **catalog**(数据结构) | `session.py` Session.catalog | 删;artifact 元数据已在 transcript 的 `tool_result` 事件里(event_id/preview/n) | test_session.py 中 catalog_view / 容量淘汰相关用例删除(`test_catalog_view_keys` / `test_caps_evict_oldest` 等) |
| **register_artifact()** | `session.py`;`orchestrator.py:218` | 删调用;loop 出答案后只 `record_loop_turn` | test_multiturn.py `test_success_registers_artifact` 删或改为断言 transcript 有 answer 事件 |
| **resolve_references()** | `session.py`;`orchestrator.py:140` | 删;**指代→loop**:loop 读回放自行定位 event_id | test_session.py `test_resolve_references_*`(2)删;test_multiturn.py `test_followup_unresolvable_refuses` 改为断言 **loop 自 clarify**(见下) |
| **catalog_view() / history_view()** | `session.py`;`orchestrator.py:95-96`;`router.py:116-126` | Router 不再吃 catalog/history;指代上下文统一走 `build_loop_context` | test_router.py:Router.judge() 去掉 `history` / `artifact_catalog` 参数,改桩;test_session.py view 用例删 |
| **followup 的 resolved_ids 拒答门** | `orchestrator.py:154-160` | **门→loop 自 clarify**:不再因 catalog 空提前拒;交给 loop,loop 读回放仍定不到 → 自己反问"指哪一条" | test_multiturn.py 该用例改为:无回放/指代不到时 loop 返回 clarify 文案,而非 orchestrator 提前 refuse |
| **meta 拒答门 + `_explain_meta()`(纯模板)** | `orchestrator.py:69-84,143-152` | **meta→loop**:从 transcript 那一轮事件回放,loop 直接据真实工具链解释"怎么算的"(比模板更准,模板现在只说"产出了什么"、不说"怎么算的") | meta 相关用例改为断言"答案含上一轮工具/步骤",或保留极薄模板兜底 |
| **Router 端指代逻辑** | `router.py:140-148` 的 references/resolved_to 规则 | 删 resolved_to 那段;Router 只产 `turn_type`(new/followup/meta) | test_router.py 断言不再检查 resolved_to,只查 turn_type 分类 |
| **session blob(history/rolling/catalog)** | `session.py` 持久化 | 瘦身或整删(里程碑最后一步);TTL 失忆问题随之消失(§5) | test_redis_session.py / test_session.py 大改:留极薄 store 测试或全删 |
| **load_artifact / 值复用** | `node_executor.py` `load_artifact` node;`artifact_value_store.py` | **待拍板**(§6):改键到 transcript `event_id`,**或**暂时下线 —— 据实测使用频率定 | test_artifact_value_reuse.py(19)+ test_redis_artifact_value_store.py(13):保留则改键;下线则删 |

> 迁移可行性已据代码验证:`_render_turn` 输出含 event_id + preview + 工具链 + 答案,loop 据此自做指代/meta/clarify **足够**,无新依赖、不破坏「潘多拉」隔离(transcript 与业务库 AlloyDB 物理隔离)、fail-open 友好。

---

## 5. 持久化:删/瘦 session 后,TTL 失忆问题消失

- **transcript GCS 已是全量真相**:`transcripts/{owner}/{sid}/{seq}.json` append-only、无限期保留。这是唯一需要长存的记忆。
- **session blob 删/瘦后**:24h TTL 只作用在 Redis 热尾(`vs:tx:*`,纯性能缓存)。热尾过期 → `build_loop_context` 退而从 GCS 全量读(需补一条 GCS fallback 读路径),**记忆不丢**。
- 对比现在:现在 catalog 在 session blob 里,blob 24h 过期且无备份 → followup/meta 误拒。删 session 后,这条失忆链路根除;真相恒在 GCS。
- ⚠️ **必须配套实现的一项**:`build_loop_context` / `RedisGcsTranscriptStore.tail` 当前**只读 Redis 热尾**(`transcript_store.py:114-120` 的 `lrange`,无 GCS 回读)。删 session 前**必须给它加一条"热尾 miss → 从 GCS `transcripts/{owner}/{sid}/` 全量回读"的 fallback**,否则会把"过 24h 还能回放"也一起弄丢。

---

## 6. 开放问题 / 需拍板

1. **值复用(load_artifact)去留** — 改键到 transcript `event_id`(loop 引用某轮 `tool_result` 的 event_id 取真实值),**还是**暂时下线(miss 本就 fail-open 重算)?**决策依据**:实测 load_artifact 命中频率;若极低则下线,省掉整个 `artifact_value_store.py` + 19+13 个测试。
2. **Router 门要不要喂 transcript 尾** — 不喂:Router 纯靠 nl 分 smalltalk/refuse/new,followup/meta 的精确判定全交 loop(最便宜)。喂最近 2-3 轮尾:Router 的 turn_type 分类更稳,但门变贵一点。建议先**不喂**,用回放兜底,实测 turn_type 误判率再定。
3. **是否保留极薄 session** — 完全删 Session 对象,还是留一个 transient 极薄壳(只存 `_turn_no` 等单调计数 + 当轮 owner 作用域)?注意 `record_loop_turn` 现在用 `session._turn_no` 取轮号,删 session 需把轮号来源迁到 transcript(读尾部最大 turn +1)。
4. **meta 兜底** — loop 从回放解释"怎么算的"需调一次小模型(成本中)。是否保留一条纯模板兜底,以防 loop/模型不可用?

---

## 7. 里程碑(小步可回退)

> 原则:**先让 loop 自己解析指代并验证 ≥ 现状,再删 catalog,最后删 session**。每步独立可上线、可回退。

- **M0 验证(不改线上)**:在 test_loop_memory 加用例,给 loop 一段 `build_loop_context` 回放,验证它能据 event_id 解析"这个/那个"、能据回放解释 meta。补 `build_loop_context` 的 **GCS 全量回读** fallback(§5)。**门槛:loop 自做指代/meta 准确率 ≥ 现状 catalog 路径**,否则停。
- **M1 指代/meta 迁回 loop(并行双跑)**:orchestrator 在 followup/meta 仍走 loop,但**额外**让 loop 自解析,与 catalog 路径结果对比打点(不改对外行为)。验证 ≥ 现状。
- **M2 切换 + 删指代门**:Router 去掉 resolved_to 逻辑,只产 turn_type;删 `resolve_references` 调用、followup resolved_ids 拒答门、`_explain_meta` 模板;followup 指代不到 → loop 自 clarify。删/改对应 router + multiturn 测试。
- **M3 删 catalog**:删 `register_artifact` / `catalog_view` / `catalog` 字段;Router 不再吃 catalog/history。值复用按 §6 决策:改键 event_id 或下线 load_artifact。改 test_session / test_artifact_value_reuse。
- **M4 删/瘦 session**:轮号来源迁到 transcript;删或瘦 session blob 的 history/rolling/catalog 持久化;`Session.from_dict` 对旧 blob 字段保留宽容兜底(向后兼容重启)。至此**只剩一份持久 transcript + 一个便宜 Router 门**,TTL 失忆问题随 session 删除而消失。

---

### 已核实事实锚点(verify 通过,test 数已修正)
- 两套记忆并存:`session.py`(catalog/history)+ `transcript_store.py`/`loop_memory.py`。
- session blob 只在 Redis/SQLite、24h TTL、**无 GCS 备份**;transcript GCS 全量无限期。
- 指代两套:Router(catalog,`router.py`)+ orchestrator `resolve_references`(`orchestrator.py:140`)+ loop 回放(`_render_turn` 已含 id)。
- `build_loop_context`/`tail` 当前**只读 Redis 热尾,无 GCS 回读**(`transcript_store.py:114-120`)—— §5 必补。
- 受影响测试(实际数):test_session.py=19、test_artifact_value_reuse.py=19、test_redis_artifact_value_store.py=13。
