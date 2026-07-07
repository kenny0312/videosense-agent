# τ²-bench for Video：VS 版 dual-control 评测环境设计

> **定位**：本文是 `docs/eval-system-and-layer0-plan.md`（下称「eval 主计划」）**Part A** 的具体 τ²-style 实例化。主计划里 A5.2 只用一句话带过「覆盖/鲁棒/goal-shift 用 tau2-bench dual-control 模拟用户」——本文把那句话展开成**可以直接开工的环境类、任务 schema、模拟用户、判分器分发与 runner**。读者是 VS 唯一的工程师；VS = 闭源 Gemini 驱动的多轮多模态视频理解 agent（`run_query` → `run_query_loop` → probe-and-step loop）。
>
> **和主计划的映射**（后文 §10 展开）：本文的可验证判分器 = A1；VideoWorld env + runner = A2；模拟用户 + judge = A3；per-PR 快门 = A4；飞轮 pinned = A5。

---

## 1. 为什么 τ² 最贴 VS + 必须改的落差

### 1.1 τ² 贴在哪

τ²-bench（arXiv:2506.07982）相比 τ-bench 的核心升级是 **dual-control 有状态环境（Dec-POMDP）**：agent 和模拟用户**两侧都有工具**、都能改一个**共享的世界状态**；任务判定不看轨迹，只看**确定性 end-state diff**（final DB hash == target）+ required-action 门 + communicate-check。这四个机制里，有三个天然贴 VS：

| τ² 机制 | 为什么贴 VS |
|---|---|
| **dual-control（两侧都改共享状态）** | VS 的用户**真的**能改共享状态：`Ctrl+V` 贴图（多模态输入）、上传新视频（`/v1/upload_url`）、触发 ingestion（`/v1/enrich`）把新视频灌进 pgvector 索引。这不是模拟——是 `api/server.py` 里真实存在的 seam（§3）。 |
| **pinned 模拟用户 + persona/goal + 每轮重注入** | VS 是多轮的（`orchestrator.run_query` 带 transcript replay）。turn N 的输入依赖 agent 的 turn N-1 输出，静态输入输出对根本测不了多轮，必须有个被环境状态**紧耦合**的模拟用户。 |
| **pass^k 而非 pass@1** | VS 大脑（Gemini flash）非确定 + 模拟用户 ~22% drift，两个噪声源复合，pass@1 是乐观上限。用户体验的是 pass^k。 |

### 1.2 必须改的落差：VS 是**读/分析型**，不是事务型

τ²-bench 的领域（零售退货、航司改签）是**事务型**：几乎每个任务都在改 DB（下订单、改座位），所以 end-state diff 是自然的成功判据。**VS 95% 是只读分析**——「有没有跳伞视频」「做饭视频里第一个多长」这类查询**不改任何持久状态**：`video_facts` / `skydive_segments` / `video_metadata` / `content_embeddings` 在 ingestion 后**全部静态**，loop agent 对它们只读。

因此纯 τ² end-state diff **对 VS 的主体（读任务）完全失效**：两次 rollout 跑完，所有 DB 表 byte 级一致，diff 恒等于零，测不出「agent 到底有没有正确推理」。这是本文自始至终诚实面对的张力。

**落差处方：HYBRID 成功判据**。不同任务类别走不同判分器，`reward_basis` 声明**每个任务哪些判分器真正计分**：

| 任务类别（VS 占比） | 成功判据 | 对应主计划维度 |
|---|---|---|
| **写任务**（少数：`update_memory` / ingestion / 索引 upsert） | **τ² state-diff**：任务后 diff 被改的那块状态面（GCS memory blob / `content_embeddings` 行）vs 目标态 | A1「DB/实体状态」 |
| **读任务**（多数：检索/分析/诚实拒答） | **VS 可验证判分器**当 communicate-check 那一侧：时间戳 IoU、retrieval recall@k、refusal/honesty、entity/DB-fact match | A1「时序/检索/幻觉」 |
| **多轮连贯**（指代/槽位跟踪） | **JGA 式确定性槽位判分**：turn 3 的实体/时间戳，turn N 必须保持一致 | A1「多轮连贯」 |

**总原则一句话**：τ² 的**骨架**（dual-control env、pinned sim-user、required-action 门、pass^k、reward_basis 门控）全抄；τ² 的**成功度量**（end-state hash diff）只用于写任务，读任务把 communicate-check 那一侧换成 VS 的可验证判分器，多轮换成 JGA。**reward_basis 是把这三条缝在一起的针**。

---

## 2. VideoWorld 环境抽象

τ² 的环境 = 一个可 seed / reset / step / hash 的有状态对象。VS 版叫 **VideoWorld**。它的「共享状态」不是单个 DB，而是**四层持久面 + 一层会话面**的并集。

### 2.1 VideoWorld 状态 = 视频语料快照 + 三索引 + per-owner memory + 会话

| 层 | 真实表/存储 | 读/写性质 | seed 来源 | 引用 |
|---|---|---|---|---|
| **语料 catalog** | `video_metadata`（video_id/title/gcs_uri/duration_sec） | **只读区**（ingestion 后不变） | `repl/_mock_db.py:40-46,112-130`（12 general + 4 skydive） | node_executor.py:157-159 |
| **细粒度事实** | `video_facts`（predicate/matched/confidence/rationale/start_ts/end_ts，unique(video_id,predicate)） | **只读区**（loop 只读；写只发生在 offline ingestion） | mock ~50 facts；`perception/skydive_schema.py:144-149` upsert 桥 | setup_schema.py:22-39 |
| **受控跳伞元数据** | `skydive_segments`（6 phase span + jump_type + is_wingsuit，phase 全 NULLABLE） | **只读区** | mock ~20 seed（NULL-safe） | skydive_schema.py:26-168, `_mock_db.py:102-104` |
| **语义索引** | `content_embeddings`（source/snippet/start_ts/end_ts/embedding(768)/content_key UNIQUE，upsert-only） | **半写区**：写只经 `enrichment.enrich_video`（offline）或 analyze write-hook（确定性、缓存命中即无变化） | `semantic_index.index_entry()` | semantic_index.py:13-37,78-161 |
| **per-owner memory** | GCS `user-memory/{owner}/memory.md`（append-only，≤6000 char，60s LRU） | **可写区**：`update_memory` 工具 → 真 diff | 空 blob 或预置几行 | user_memory.py:49-104 |
| **transcript（多轮记忆）** | Redis LIST hot tail + GCS per-event | **可写区**：每轮 append（但确定性路由、per-session 隔离） | `InMemoryTranscriptStore()` | transcript_store.py:1-184, loop_memory.py:23-38 |
| **uploads（临时视频）** | Redis TTL 86400s + GCS lifecycle | **可写区**：`uploads.register()` 改共享可见性 | 空 registry | uploads.py:1-107 |

### 2.2 seed / reset / snapshot / hash-diff 怎么做

**seed（关键：全部走 mock，零 AlloyDB 依赖）**：`REPL_USE_MOCK_DB=1` 打开 `repl/_mock_db.py` 的内存 SQLite（VIDEOS / FACTS / SKYDIVE_SEED 内联）。VideoWorld 在 `seed(world_spec)` 里：

1. 用 mock DB 装载 catalog + facts + skydive（或按 task 的 `world_spec` 覆盖某些行 → 造「库里没有 X」的负例场景）。
2. `content_embeddings` 用 mock vector（`test_db_or_fixtures` 里的 SQLite mock vector）。
3. memory blob 从 `world_spec.memory_seed` 写入内存 GCS stub。
4. transcript 用 `InMemoryTranscriptStore()`；session 用 `SessionStore(path=None)`。

**reset**：每个 rollout **重新 seed 一遍**（不共享可变状态），保证 n 次 rollout 独立、pass^k 无偏。

**snapshot**：只对**可写区 + 半写区**做快照（只读区不必，恒等）：

```python
def snapshot(self) -> dict:
    return {
        "memory":     self._memory_blob(owner),          # str，GCS stub 内容
        "embeddings": self._embed_rows_sorted(),         # list[(content_key, source, snippet, start_ts, end_ts)] — 不含向量本身，含 content_key 即够
        "uploads":    sorted(self._upload_registry.items()),
        "transcript": self._tx_slot_view(),              # 只抽「槽位」：每轮 answer 里的 video_id / time_range，不抽自由文本
    }
```

**hash-diff**：`state_diff(before, after, target)` 对**语义规范化后**的 snapshot 求差，再和 `target` 比。规范化很重要，否则假失配：
- memory：按行 split，忽略 `[YYYY-MM-DD]` 日期前缀，比**事实集合**（set equality），不是 byte。
- embeddings：比 **content_key 集合的增量**（新增了哪些 key）+ 每个新增 key 的 `(source, ⌊start_ts⌋, ⌊end_ts⌋)`——**不比向量数值**（embedding 非确定），只比「该不该建这条索引、锚在哪个 span」。
- uploads：比 registry key 集合。

### 2.3 哪些是只读区（state-diff 不适用，写死在文档里防误用）

`video_metadata` / `video_facts` / `skydive_segments` 是 **loop agent 只读**。**没有任何 τ² state-diff 能测「agent 有没有正确探查这两张表」**——两张表跑前跑后 byte 相同。这类任务（就是 VS 的主体）**必须**走 §6(b) 可验证判分器（工具调用审计 + 答案证据校验），**不是** state-diff。这是 `state_diff_feasibility.where_diff_FAILS` 的直接体现，本文把它固化为设计约束：**读任务的 reward_basis 里绝不出现 `state_assertions`**。

---

## 3. Dual-control：两侧各有什么动作

τ² 的精髓是**两侧都能改共享状态**。VS 里这不是虚构——用户侧动作有真实 API seam。

### 3.1 Agent 侧工具（读为主，写为辅）

| 工具 | 读/写 | 改哪块共享状态 | 注册/分发 seam |
|---|---|---|---|
| `sql_query` | 读 | 无（只读 facts/skydive/metadata） | node_specs.py SPECS → node_executor 分发 |
| `semantic_search` | 读 | 无（读 `content_embeddings`） | 受 `USE_SEMANTIC_SEARCH` 门控 |
| `analyze_video` | 读 + **写钩子** | write-hook：把答案 upsert 进 `content_embeddings`（但确定性、cache HIT 即无变化） | loop_driver.py:379-389 配额门 + `_make_executor` |
| `show_video` / `show_table` | 读 | 无 | 需 `data_result_id` 上游句柄 |
| `web_search` | 读 | 无（外部） | 受 `USE_WEB_SEARCH` 门控 |
| `update_memory` | **写** | GCS memory blob（真 diff） | 受 `USE_USER_MEMORY` 门控，backend `user_memory.update()` |
| `spawn_agents` | 读（可递归 execute） | 无（共享父配额） | loop_execute=execute 递归 |

Agent 侧的**唯一稳定 state-diff 抓手是 `update_memory`**（+ analyze write-hook，但它确定性）。所以 §8③ 的写任务用 `update_memory`。

### 3.2 用户侧动作（模拟用户如何改共享状态）——引用真实 seam

这是 VS 版 dual-control 的**关键差异化**：模拟用户不只是发 NL，它能触发以下改共享状态的动作，每个都有真实入口：

| 用户侧动作 | 真实 seam | 改哪块共享状态 | 模拟用户如何触发 |
|---|---|---|---|
| **① Ctrl+V 贴图**（多模态） | `VibeQueryRequest.image`（api/server.py:117-118）→ `_parse_image` 校验 8MB/mime（:126-139）→ `GenAIConversation` 首消息附着（loop_driver.py:287-308） | transient（本轮），影响首轮响应 | 脚本里某轮带 `image="data:image/png;base64,..."`（如「给你看这张截图，找橙色那个跳伞的」） |
| **② 上传新视频** | `POST /v1/upload_url`（api/server.py:252-269）→ 签名 GCS PUT → Redis 注册 `up_<hex>` TTL | `uploads` registry（新视频变可分析） | 模拟用户在 turn 之间调 upload seam，把一个新 `video_id` 塞进 world，后续 turn 引用它 |
| **③ 触发 ingestion** | `POST /v1/enrich`（api/server.py:304-330）→ 异步 `enrichment.enrich_video` → caption/transcript → pgvector | `content_embeddings` 新增行（**state-diff 可测**） | 模拟用户 upload 后调 enrich，下一 turn「现在搜得到这个视频吗」 |
| **④ 纠正 / 追问**（指代） | 下一轮 `VibeQueryRequest.query` + 同 `session_id` → transcript replay（loop_memory.build_loop_context） | transcript（多轮槽位） | 「不对，我说的是第一个」「那第三个呢」→ JGA 判指代解析 |
| **⑤ 跨 session memory 写** | `update_memory` 由 agent 代用户执行；用户表达偏好触发 | GCS memory blob | 「记住我只关心翼装飞行」→ 期望 agent 调 update_memory |

**约束（照 τ²）**：模拟用户只能通过**上述 world 工具/状态面**动作，不能凭空捏造 world 里不存在的东西（否则 drift）。这正是 τ² 「模拟用户被环境状态紧耦合」的落地。

---

## 4. Task instance schema

对齐 τ²（initial world state / persona+goal / evaluation_criteria{required_actions + nl_assertions} / reward_basis），换成视频域。一个任务实例（JSONC）：

```jsonc
{
  "id": "skydive-honesty-01",
  "dims": ["honesty", "retrieval", "toolcall"],   // 主计划维度标签
  "n_rollouts": 5,                                  // pass^k 的 n（CI 金集 n=5，nightly n=10-20）
  "max_steps": 16,                                  // 对齐 MAX_LOOP_STEPS

  // ── ① 初始 world 状态 seed ──（VideoWorld.seed 消费）
  "world_spec": {
    "corpus": "mock_default",                       // 用 _mock_db 默认 seed
    "overrides": {                                  // 可覆盖行造负例/正例场景
      "skydive_segments": [                         // 明确 seed 一条 wingsuit，用于「说没有=fail」
        {"video_id": "sky_003", "is_wingsuit": true, "jump_type": "wingsuit"}
      ]
    },
    "memory_seed": [],                              // per-owner memory 起始（空）
    "uploads_seed": []
  },

  // ── ② 模拟用户 persona + goal + 行为脚本 ──
  "user": {
    "persona": "一个只会说中文口语、不懂技术术语的跳伞爱好者",
    "goal": "确认这个视频库里到底有没有翼装飞行（wingsuit）的片段",
    "script": [                                     // scripted 模式用；sim 模式当 goal 提示
      {"turn": 1, "utterance": "你们这有没有翼装飞行的视频啊", "actions": []},
      {"turn": 2, "utterance": "真的没有？你确定查全了吗",     "actions": []}
    ],
    "style": "如果 agent 第一轮说没有，追问一次逼它复查"
  },

  // ── ③ evaluation_criteria（对齐 τ²）──
  "evaluation_criteria": {
    "required_actions": [                           // required-action 门（必须出现的工具调用）
      {"tool": "sql_query", "arg_contains": "skydive_segments"},
      {"tool": "sql_query|semantic_search", "arg_contains": "wingsuit"}
    ],
    "state_assertions": [],                         // ← 读任务：留空（只读区，见 §2.3）
    "output_checks": {                              // 可验证判分器输入
      "honesty": {"must_not_refuse_wrongly": true, "expect_positive": true},
      "retrieval": {"must_surface_video_ids": ["sky_003"], "k": 5},
      "entity_match": {"jump_type": "wingsuit"}
    },
    "nl_assertions": [                              // 只在 nightly judge 上跑（κ≥0.6 维度）
      "答案明确肯定库里有翼装飞行视频，并给出可核对的证据（video 序号/time_range）"
    ]
  },

  // ── ④ reward_basis：声明哪些判分器真正计分 ──
  "reward_basis": ["required_actions", "output_checks.honesty", "output_checks.retrieval"]
  // ↑ nl_assertions 不入 reward_basis（advisory only，防 judge 方差污染 PR 门）
}
```

**字段语义**（对齐 τ²）：
- `required_actions` = τ² 的 required-action 门（这里是「答否定前必须查 skydive_segments AND wingsuit」，直接编码 `before_answering_MANDATE`）。
- `state_assertions` = τ² 的 end-state diff（**只有写任务非空**）。
- `output_checks` + `nl_assertions` = τ² 的 communicate-check（VS 换成可验证判分器 + judge）。
- `reward_basis` = τ² 的「哪些 check 计入分数」——**门控核心**：不在 basis 里的判分器只上仪表盘，不 gate。

---

## 5. 模拟用户

照 τ² 的 recipe，pin 一个**跨家族**模型当模拟用户：

- **模型**：复用主计划规划的跨家族 judge 模型（VS 大脑是 Gemini → 模拟用户/judge 用 **Claude**，避免自偏好与共享盲点）。**pin 并版本化**（升级它像 agent 退步，见主计划红线）。
- **seed**：结构化 `persona + goal` 对象（§4 的 `user` 块），不是自由 system prompt。
- **每轮重注入 goal**（照 τ² 的 ~22% drift 处方）：每次生成用户 turn 前，把 `goal` + 「你只能问关于这个视频库的事，不能编造库里没有的视频/活动」拼进 prompt 头。
- **约束在 VideoWorld 工具/状态面**：模拟用户可触发的动作限定为 §3.2 的五类（贴图/上传/enrich/追问/表达偏好），不能凭空发明 world 外实体。
- **drift 防护 = 丢弃而非误标**：每个 rollout 后跑一个廉价 drift check（模拟用户最后一轮是否还在追 goal / 有没有引入 world 里不存在的实体）。**drift 的 rollout 直接丢弃重跑**，绝不计入 pass^k 的分子或分母（误标会系统性偏移分数，主计划 A5.2 已警告 sim-user ~9pp 摆动 + 系统性误标）。
- **红线**：模拟用户 pass 率**只用于相对回归追踪**，绝不当绝对能力真值。因此模拟用户任务只上 **nightly**，per-PR 门用脚本用户（零方差）。

---

## 6. 判分：三类判分器 + reward_basis 门控 + pass^k

harness 对每条任务跑**所有适用**判分器，输出 `dim -> score` 字典（不是单标量），再由 `reward_basis` 决定哪些进 verdict。

### (a) state-diff 判分器（写任务）

只对 `state_assertions` 非空的任务跑。diff `snapshot_after` vs `world_spec` 目标态（§2.2 的规范化 hash-diff）：

```python
def score_state_diff(world, target_assertions) -> dict:
    after = world.snapshot()
    out = {}
    for a in target_assertions:
        if a["surface"] == "memory":
            got = set(_facts(after["memory"]))
            want = set(a["expect_facts"])
            out["memory_diff"] = 1.0 if want <= got and (not a.get("exact") or got == want) else 0.0
        elif a["surface"] == "embeddings":
            new_keys = _new_content_keys(after, a["baseline"])
            out["embed_diff"] = 1.0 if _matches_spans(new_keys, a["expect_spans"]) else 0.0
    return out
```

**关键诚实**：state-diff 在 VS 只覆盖 `update_memory` / ingestion-into-`content_embeddings` / uploads 三处（§2.1 可写区）。**不覆盖任何核心视频推理**——那是读任务的活。

### (b) VS 可验证判分器（读任务，复用 A1 的 `evals/scorers.py`）

主计划 A1 规划的 `eval_scorers.py` 尚未落地——本 harness 是它的第一个消费者。判分器（纯函数、确定性、无 judge）：

| 判分器 | 输入 | 度量 | 对应 output_check |
|---|---|---|---|
| `iou_r1(pred_span, gold_span)` | agent 输出 `[s,e]` vs 金标 | R@1@{0.5,0.7} + mIoU（统一 span 约定，⌊fps⌋ 取整） | 时序 |
| `recall_at_k(surfaced_ids, gold_ids, k)` | agent 检索/答案里的 video_id vs 标注相关 | recall@k + MRR | retrieval |
| `refusal_ok(answer, expect)` | 答案文本 + 期望（该拒/该答） | 拒答率 + 假答率 | honesty |
| `entity_match(answer, gold_facts)` | 答案里的实体 vs DB 事实（jump_type/predicate） | exact/子集匹配 | entity_match |
| `toolseq_match(trace, required_actions)` | ledger 工具链 vs required_actions | 工具名 exact + arg contains + 冗余率（`LOOP_REPEAT_LIMIT`） | required_actions |

`required_actions` 门用 `toolseq_match`：它审计 ledger（loop 的 trace）确认「答否定前查了两处」——**这是读任务替代 state-diff 的核心**（`state_diff_feasibility.summary` 的 TOOL INVOCATION AUDIT）。

### (c) JGA 式槽位判分（多轮连贯，确定性）

多轮任务抽**槽位**（slot = 每轮答案里的结构化引用：video_id / time_range / ordinal 指代解析结果），做 Joint Goal Accuracy：turn N 的每个槽位都要和金标一致才算该轮对，全轮对才算任务对。槽位从 `snapshot()["transcript"]` 的 `_tx_slot_view()` 抽（只抽结构化引用，不判自由文本语气——那才交 judge）。

```python
def score_jga(slot_trace, gold_slots) -> float:
    # slot_trace = [{turn, video_ids, time_ranges, resolved_ordinal}, ...]
    return float(all(_slot_eq(slot_trace[t], gold_slots[t]) for t in range(len(gold_slots))))
```

指代解析（「第一个」「第三个」→ 实际 video_id）经 `show_video` items 列表映射 + `scrub_ids` 守护（domain_rules `multi_turn_continuity`）——JGA 直接查这个映射对不对。

### reward_basis 门控

```python
def verdict(scores: dict, reward_basis: list[str], thresh: dict) -> bool:
    # 只有 reward_basis 里点名的判分器计入 verdict
    return all(scores[k] >= thresh.get(k, 1.0) for k in reward_basis if k in scores)
```

不在 `reward_basis` 的判分器（如 `nl_assertions` 的 judge 分）照跑照记，但**只上仪表盘**，不 gate——防 judge 方差把 PR 门搞 flaky（主计划 A6）。

### pass^k 公式 + 配对检验

照主计划 A5.2 / τ-bench：每任务 n 独立 rollout（drift 丢弃后补齐），c 次成功：

```
pass^k = E_task[ C(c,k) / C(n,k) ]          # 无偏组合估计器，别用 (c/n)^k
```

- 头条报 **pass^3**；PR 快反馈报 pass^1 delta。
- 聚合 CI **bootstrap over 任务**（难度异质）；版本对比**必须配对**（同任务 + 种子，McNemar / paired bootstrap），跌幅需超 baseline CI 才算回归。

---

## 7. 接进 VS 代码

### 7.1 接哪个 seam

| 用途 | seam | 理由 |
|---|---|---|
| **读任务 + 脚本用户（per-PR 门）** | `run_loop`（loop_driver.py，纯控制流） | 主计划 A5.1 首选：注入 `ScriptedConv`（test_loop_driver.py:13）+ `make_exec`（:24），<1s 无网络，40+ 测试打磨过。VideoWorld 提供 tool_results 给 `make_exec`。 |
| **多轮 + 真 transcript replay（JGA）** | `run_query`（**orchestrator.py:63**） | 带 `Session` + `build_loop_context` transcript replay，测真多轮指代。VideoWorld 提供 `SessionStore(path=None)` + `InMemoryTranscriptStore()`。 |
| **写任务 state-diff** | `run_query`（orchestrator.py:63） | 需真跑 `update_memory` backend（user_memory.update 写 GCS stub），snapshot 前后 diff。 |
| **E2E dual-control（上传/enrich/贴图）** | `video_vibe_query`（api/server.py:217）via FastAPI TestClient | 只在 nightly：模拟用户经 HTTP 触发 §3.2 的用户侧动作。 |

### 7.2 execute 闭包与 DB 快照

- **execute 闭包**：读任务用 `make_exec(values=case.tool_results)` stub（确定性）；写任务/E2E 用真 `_make_executor`（loop_driver.py:368-418），让 `update_memory` / analyze write-hook 真正落到 VideoWorld 的内存 stub。
- **DB 快照**：`REPL_USE_MOCK_DB=1` 内存 SQLite（`_mock_db.py`），每 rollout `seed()` 重建。snapshot 只快照可写区（§2.2）。

### 7.3 record-replay 昂贵工具（`analyze_video`）——控成本 + 确定性

`analyze_video` 是主导成本（≈60k tok / \$0.018/次，flash）。eval 里**必须 record-replay**：

- **record**（一次性）：真跑一遍金集，把 `make_key(video_id, question, context, rubric, time_range, model)` → `AnalyzeResult` 存成 fixture（复用 `analyze_cache` 的 ckey 逻辑，config.py:101-107）。
- **replay**（每次 eval）：`make_exec` 的 analyze 分支查 fixture，命中即返回、零 API 调用、完全确定。miss（fixture 没录）视为 eval 配置错误、报错而非偷偷真调。
- 这同时给了**成本判分**：per-task 记 `#analyze_video 调用数`，「更聪明」的 agent 悄悄 3× 调用立刻抓到。

### 7.4 per-PR 快门 vs nightly

| 门 | 跑什么 | 判分 | n |
|---|---|---|---|
| **per-PR（分钟级）** | 脚本用户 + record-replay 工具 + 可验证判分器 + state-diff | reward_basis 里的 (a)(b)(c)，无 judge | 5 |
| **nightly / weekly** | 模拟用户（Claude，drift 丢弃）+ judge（nl_assertions）+ E2E dual-control | 加 judge advisory（κ≥0.6 维度才计），跑上传/enrich | 10-20 |

per-PR 门**只跑可验证任务**（主计划 A6 红线：judge/sim-user 方差会 flaky）。pinned 回归 case 走 per-PR 硬门（单个翻转即阻断）。

---

## 8. 三个 worked example 任务实例

### ① skydive 诚实/拒答（读任务，required-action 门是主角）

场景：`before_answering_MANDATE`——「有没有跳伞/翼装」必须查 `video_facts` **AND** `skydive_segments`（+可选 semantic_search）再答，否则「说没有」= fail（domain_rules 的经典反例）。

```jsonc
{
  "id": "skydive-honesty-01", "dims": ["honesty","retrieval","toolcall"], "n_rollouts": 5, "max_steps": 16,
  "world_spec": {
    "corpus": "mock_default",
    "overrides": {"skydive_segments": [{"video_id":"sky_003","is_wingsuit":true,"jump_type":"wingsuit"}]}
  },
  "user": {
    "persona":"中文口语跳伞爱好者", "goal":"确认库里有没有翼装飞行片段",
    "script":[
      {"turn":1,"utterance":"你们这有没有翼装飞行的视频"},
      {"turn":2,"utterance":"真的没有？你查全了吗"}
    ]
  },
  "evaluation_criteria": {
    "required_actions":[
      {"tool":"sql_query","arg_contains":"skydive_segments"},
      {"tool":"sql_query|semantic_search","arg_contains":"wingsuit"}
    ],
    "state_assertions":[],                         // 只读区
    "output_checks":{
      "honesty":{"expect_positive":true,"must_not_refuse_wrongly":true},
      "retrieval":{"must_surface_video_ids":["sky_003"],"k":5},
      "entity_match":{"jump_type":"wingsuit"}
    },
    "nl_assertions":["明确肯定有翼装视频并给出可核对证据"]
  },
  "reward_basis":["required_actions","output_checks.honesty","output_checks.retrieval"]
}
```

**怎么判**：`toolseq_match` 审计 ledger——**没查 skydive_segments 就答否定 → required_actions fail → verdict FAIL**（哪怕答案文字碰巧对）。`refusal_ok` 查是否错误拒答（库里明明有 sky_003）。`recall_at_k` 查答案有没有 surface `sky_003`。三者都在 reward_basis → 全过才 PASS。nl_assertions 的 judge 分只上仪表盘。**这就是 state-diff 失效、用工具审计 + 可验证判分器替代的样板。**

### ② 做饭视频多轮检索（JGA 判指代，orchestrator.run_query seam）

turn1 检索 → turn2「第一个啥时候拍的/多长」指代 → turn3 比时长。

```jsonc
{
  "id":"cooking-multiturn-01","dims":["retrieval","coherence"],"n_rollouts":5,"max_steps":16,
  "world_spec":{"corpus":"mock_default"},        // 12 general 里含做饭视频（preparing salad / cutting pumpkin）
  "user":{
    "persona":"随意的中文用户","goal":"找做饭视频并比较前两个的时长",
    "script":[
      {"turn":1,"utterance":"有没有做饭的视频"},
      {"turn":2,"utterance":"第一个多长时间"},          // 指代 → turn1 结果 items[0]
      {"turn":3,"utterance":"那第二个呢，哪个更长"}       // 指代 + 比较
    ]
  },
  "evaluation_criteria":{
    "required_actions":[{"tool":"sql_query|semantic_search","arg_contains":"cook|salad|做饭"}],
    "state_assertions":[],
    "output_checks":{"retrieval":{"must_surface_video_ids":["vid_cook_a","vid_cook_b"],"k":5}},
    "jga_slots":[                                  // JGA 金标槽位（每轮结构化引用）
      {"turn":1,"video_ids":["vid_cook_a","vid_cook_b"]},
      {"turn":2,"resolved_ordinal":{"第一个":"vid_cook_a"},"time_ranges":[[0,duration_a]]},
      {"turn":3,"resolved_ordinal":{"第二个":"vid_cook_b"},"comparison":"vid_cook_a>vid_cook_b?"}
    ],
    "nl_assertions":["三轮语气连贯，指代解析自然"]
  },
  "reward_basis":["output_checks.retrieval","jga_slots"]
}
```

**怎么判**：走 `run_query`（真 transcript replay）。`score_jga` 从 `_tx_slot_view()` 抽每轮的 `resolved_ordinal`——**turn2「第一个」必须映射到 turn1 结果的 items[0]（vid_cook_a），映射错 → JGA fail**。turn3 的时长比较槽位查 duration 大小对不对。JGA 是确定性的（查 show_video items 映射），语气连贯交 judge（advisory，不入 reward_basis）。

### ③ update_memory 写任务（state-diff 判该不该写、写对没）

用户表达持久偏好 → 期望 agent 调 `update_memory` 落 GCS blob。这是 VS 里**少数真能 state-diff 的写任务**。

```jsonc
{
  "id":"memory-write-01","dims":["toolcall","state"],"n_rollouts":5,"max_steps":16,
  "world_spec":{"corpus":"mock_default","memory_seed":[]},   // memory 起始空
  "user":{
    "persona":"明确表达偏好的用户","goal":"让 agent 记住我只关心翼装飞行",
    "script":[{"turn":1,"utterance":"以后我问跳伞，默认我只关心翼装飞行，记住这点"}]
  },
  "evaluation_criteria":{
    "required_actions":[{"tool":"update_memory","arg_contains":"wingsuit|翼装"}],
    "state_assertions":[                          // ← 写任务：非空！
      {"surface":"memory","expect_facts":["用户只关心翼装飞行(wingsuit)"],"exact":false}
    ],
    "output_checks":{},
    "nl_assertions":["确认已记住，不啰嗦"]
  },
  "reward_basis":["required_actions","state_assertions"]
}
```

**怎么判**：走 `run_query` + 真 `_make_executor`，让 `update_memory` 真写 VideoWorld 的内存 GCS stub。跑完 `snapshot()["memory"]`，`score_state_diff` 规范化后（忽略日期前缀，比事实集合）确认**新增了「只关心翼装」这条事实**。双门：`required_actions` 确认**该写时真调了 update_memory**（不该写乱写也算 required_actions 之外的行为，可加负 assertion）；`state_assertions` 确认**写对了内容**。这是纯 τ² state-diff 在 VS 的正宗用法——**唯一真正对得上 τ² 原始形态的类别**。

---

## 9. 代码骨架（python 伪代码，标注接 VS 哪个 seam）

```python
# evals/world.py (NEW) —— τ² 环境抽象
class VideoWorld:
    """VS 版 dual-control 有状态环境。只读区不快照（恒等），只对可写区 diff。"""
    def __init__(self, world_spec: dict, owner="eval_user"):
        self.owner = owner; self.spec = world_spec

    def seed(self):
        os.environ["REPL_USE_MOCK_DB"] = "1"           # → repl/_mock_db.py 内存 SQLite
        self.db = mock_db_load(self.spec["corpus"], self.spec.get("overrides"))
        self.memory = MemoryStub(self.spec.get("memory_seed", []))   # GCS user-memory stub
        self.tx = InMemoryTranscriptStore()            # transcript_store.py:74
        self.sessions = SessionStore(path=None)        # session.py 内存
        self.uploads = dict(self.spec.get("uploads_seed", []))

    def reset(self): self.seed()                       # 每 rollout 重 seed → pass^k 独立

    def snapshot(self) -> dict:                        # 只快照可写/半写区（§2.2）
        return {"memory": self.memory.blob(self.owner),
                "embeddings": self._embed_rows(),
                "uploads": sorted(self.uploads.items()),
                "transcript": self._tx_slot_view()}

    def state_diff(self, before, after, target_assertions) -> dict:
        return score_state_diff_normalized(before, after, target_assertions)  # §6(a)

    # ── 用户侧动作（dual-control，§3.2 真实 seam）──
    def user_upload(self, gcs_uri) -> str:             # /v1/upload_url（api/server.py:252）
        vid = f"up_{secrets.token_hex(8)}"; self.uploads[f"upload:{vid}"] = gcs_uri; return vid
    def user_enrich(self, vid):                        # /v1/enrich（api/server.py:304）→ content_embeddings
        enrichment_stub.enrich_video(vid, self.uploads[f"upload:{vid}"], index=self._embed_index)


# pipeline/eval_tasks.py (NEW) —— task loader
def load_tasks(path) -> list[dict]:
    return [json5.loads(strip_jsonc(l)) for l in open(path) if l.strip()]   # evals/*.jsonc


# evals/simulated_user.py (NEW) —— pinned 跨家族模拟用户（§5）
class SimulatedUser:
    def __init__(self, persona, goal, model="claude-*"):   # pin 跨家族，非 Gemini
        self.persona, self.goal, self.model = persona, goal, model
    def next_turn(self, history, world) -> dict:
        prompt = reinject_goal(self.goal, self.persona, history,   # 每轮重注入 goal
                               tool_surface=USER_SIDE_ACTIONS)     # 约束在 world 面
        return claude_generate(self.model, prompt)                 # {utterance, image?, action?}
    def drifted(self, history, world) -> bool:                     # drift check → 丢弃
        return goal_drift(history, self.goal, world.known_entities())


# evals/runner.py (NEW) —— 跑 n rollout 出 pass^k
from math import comb
def run_case(case, *, judge=False):
    n = case["n_rollouts"]; successes = 0; per_dim = {}; kept = 0
    while kept < n:
        world = VideoWorld(case["world_spec"]); world.seed()
        if case.get("user", {}).get("script"):        # per-PR：脚本用户 → run_loop seam
            r = drive_scripted(case, world)            #   loop_driver.run_loop + ScriptedConv/make_exec
        else:                                          # nightly：模拟用户 → run_query seam
            sim = SimulatedUser(**case["user"])
            r = drive_simulated(case, world, sim)      #   orchestrator.run_query:63
            if sim.drifted(r.history, world): continue #   drift → 丢弃重跑（不计 n）
        scores = dispatch_scorers(case, r, world, judge=judge)     # §6(a)(b)(c)
        successes += int(verdict(scores, case["reward_basis"], THRESH))
        for d, v in scores.items(): per_dim.setdefault(d, []).append(v)
        kept += 1
    pass_k = {k: (comb(successes, k)/comb(n, k) if n >= k else None) for k in (1, 3, 5)}
    return {"id": case["id"], "pass_k": pass_k, "per_dim": per_dim}


# evals/scorers.py (主计划 A1，本 harness 第一个消费者) —— scorer 分发（按 reward_basis）
def dispatch_scorers(case, r, world, judge=False) -> dict:
    ec = case["evaluation_criteria"]; s = {}
    s["required_actions"] = toolseq_match(r.ledger, ec["required_actions"])      # §6(b)
    if ec.get("state_assertions"):                                              # 写任务
        s["state_assertions"] = min(world.state_diff(r.before, world.snapshot(),
                                                      ec["state_assertions"]).values())
    for name, cfg in ec.get("output_checks", {}).items():                       # 读任务可验证判分器
        s[f"output_checks.{name}"] = VERIFIABLE[name](r, cfg)                   # iou/recall/refusal/entity
    if ec.get("jga_slots"):                                                     # 多轮 JGA
        s["jga_slots"] = score_jga(world.snapshot()["transcript"], ec["jga_slots"])
    if judge and ec.get("nl_assertions"):                                       # 仅 nightly，advisory
        s["nl_judge"] = claude_judge(case, r.answer)     # κ≥0.6 才 gate；否则只上盘
    return s
```

**关键 record-replay hook**（§7.3）：`drive_scripted` 里 `make_exec` 的 analyze 分支查 `evals/fixtures/analyze_cache.json`（按 ckey），命中即返回、零调用；miss 报错。

---

## 10. 落地里程碑 + 复用 vs 自造

### 复用 vs 自造（别自造 trajectory evaluator）

| 组件 | 决策 | 依据 |
|---|---|---|
| **轨迹/工具序列评估器** | **复用 agentevals / DeepEval 的 trajectory evaluator** | 成熟的 tool-call exact/superset match、轨迹对齐已有轮子，`toolseq_match` 薄封装它即可，别自造 |
| **judge harness / rubric 打分** | **复用 DeepEval / 主计划 A3 的跨家族 judge** | κ 校准、去偏（swap/rubric）已在主计划 A3 定义 |
| **可验证判分器**（IoU/recall/refusal/entity） | 半自造（薄函数，主计划 A1） | video 域特有，但都是纯函数、几十行 |
| **VideoWorld env（seed/snapshot/diff）** | **自造** | VS 的状态面（4 表 + memory + transcript）+ 只读区约束是 VS 独有，无现成轮子 |
| **state-diff 规范化**（memory 事实集合 / embedding content_key） | **自造** | 绑定 VS 具体 schema（`user_memory` 行格式、`content_embeddings.content_key`） |
| **pass^k 估计器 + 配对检验** | 复用 τ-bench 公式（几行） | `C(c,k)/C(n,k)` + paired bootstrap，主计划 A5.2 |
| **模拟用户** | 半自造（薄封装 Claude API + 主计划 sim-user 规范） | drift check / goal 重注入是 VS 特定的 world 面约束 |

### 与 eval 主计划 A0-A5 的对应

| 本文组件 | 主计划阶段 | 产出物 |
|---|---|---|
| VideoWorld + task schema + example tasks | 挂在 **A0**（金种子集）之后，把 probe 21 轮改写成本 schema | `evals/tau2_tasks/*.jsonc` |
| 可验证判分器（iou/recall/refusal/entity/toolseq）+ JGA | **A1** | `evals/scorers.py`（本 harness 是首个消费者） |
| VideoWorld env + eval_runner + record-replay | **A2** | `evals/world.py` + `evals/runner.py` |
| 模拟用户（Claude，drift 丢弃）+ nl judge | **A3** | `evals/simulated_user.py`，κ≥0.6 才 gate |
| per-PR 快门（脚本 + 可验证 + state-diff） | **A4** | `.github/workflows/eval-gate.yml`，pass^3 delta + pinned 硬门 |
| state-diff 写任务 + 飞轮 pinned | **A5** | `evals/tau2_tasks/pinned/*.jsonc`（skydive 诚实、memory 写） |

### 第一个具体 build step

**先做 §8① skydive 诚实任务的 per-PR 脚本版**（不需要模拟用户、不需要 judge、不需要 state-diff）：
1. 写 `evals/world.py` 的 `seed()`（只需 `REPL_USE_MOCK_DB=1` + overrides 塞一条 wingsuit）。
2. 写 `evals/scorers.py` 的 `toolseq_match` + `refusal_ok` + `recall_at_k`（三个纯函数 + 单测）。
3. 写 `evals/runner.py` 的 `drive_scripted` 接 `run_loop` + `ScriptedConv`/`make_exec`，跑 n=5 出 pass^3。
4. verdict 按 `reward_basis`。跑通「没查 skydive_segments 就答否定 → FAIL」这一条断言。

这条打通即验证了整个 HYBRID 判据的读任务主路径（required-action 门 + 可验证判分器替代 state-diff），是最高杠杆的第一步。之后再加 §8② 的 JGA（升到 `run_query` seam）和 §8③ 的 state-diff 写任务。
