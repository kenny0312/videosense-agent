# M4:视频理解 —— 并行 + 覆盖 + 缓存 + 延迟

> 状态:Design(评审后即动手) · 范围:`pipeline/loop_driver.py`、`pipeline/node_executor.py`、`perception/analyze_video_contextual.py` · 关联:M1(analyze 原语)、M2(loop 多 call)、`realtime-video-understanding.md` §9(配额护栏)

## 1. 背景 / 问题

真机测试暴露:**"问 12 个翼装视频哪个最刺激"** 的体验差在两点:

- **只看了 2–3 个就答**:`MAX_VIDEOS_PER_REQUEST=5` 的配额护栏(`loop_driver.py` `_make_executor`)在串行下被逐个吃掉,大脑(Gemini loop)在覆盖不全时被迫收口 → "挑最好"的候选池太小。
- **慢**(原 #5 延迟):同一步若返回 N 个独立 `analyze_video`,现在是**严格串行**逐个阻塞(`run_loop` 的 `for i, call in enumerate(calls)`),N 个视频 ≈ N×(单次 Gemini 多模态推理,每次约 10–40s)。

`analyze_video` 是 **I/O 密集**(等 Gemini 多模态返回),不是 CPU 密集 → 线程池并发能把延迟从 N× 压到 ~1×。

## 2. 目标 / 非目标

**目标**
1. **并行 analyze_video**:同一步内**无相互依赖**的 analyze 调用用线程池并发,多视频问题延迟 N× → ~1×。
2. **覆盖**:并行变快后放宽配额(或先 SQL 缩到 top-N 再并行看),让"挑最好"从更全候选里挑。
3. **缓存**:按 `(video_id, question, rubric, model, time_range, context)` 缓存 `AnalyzeResult`,重复不重看。
4. **延迟 + 成本度量**:per-tool 计时进 trace / loop_metrics,量化并行 + 缓存收益;**顺手补上 analyze_video 的 usage 上报**(见 §5.1 注),让视频分析的 token/成本第一次进监控。

**非目标**
- 不改 Gemini SDK(不上 `google-genai` 异步,见 M2 spike findings);只用 `ThreadPoolExecutor`。
- 不并行 **sandbox** / **sql_query**(本轮只并行 analyze_video;其余串行,降低并发面)。
- 不做跨请求的视频内容失效推送(靠 TTL,见开放问题)。

## 3. 现状全貌(据代码核实)

### 3.1 执行流哪段是串行
`run_loop`(`loop_driver.py`)主循环:每步 `conversation.send()` 拿回 1–N 个 `Call`,然后:
```
for i, call in enumerate(calls):        # ← 串行块
    upstream = {u: ledger[u].value ...} # ← 取上游(跨步依赖)
    res = execute(cid, ...)             # ← BLOCKING,逐个等
    ledger[cid] = res
```
`execute()`(`_make_executor` 闭包)同步调 `execute_node()` → `_run_analyze_video()` → 同步 Gemini 多模态调用。**直到一个 `execute()` 返回,下一个 call 才进来。**

### 3.2 同一步内 call 之间有没有依赖
**没有。** `call.uses` 只指向**前面步骤**的 cid;Gemini 无法在单步内让一个 call 的结果当另一个 call 的输入(那要两步)。M2 spike 10/10 验证多 call 返回是常态。→ **同一步内的 calls 天生可并行**,其引用的上游(都是已完成的前序步)已在 `ledger` 里(并行只在**步内**并发、**步间**仍顺序,依旧满足)。

### 3.3 共享状态盘点(并行要碰的)
| 状态 | 位置 | 并行下风险 |
|---|---|---|
| `quota = {"analyzed": 0}` | `_make_executor` 闭包 | **读-改-写竞态**(漏加 → 配额失控) |
| `seen`(失败重复检测) | `run_loop` | 并发写竞态 |
| `ledger`(cid→ExecResult) | `run_loop` | key 唯一,但仍需并发安全写 |
| `trace`(append) | `run_loop` | `list.append` 竞态 |
| `on_step` 回调 | `run_loop` / `api/server.py` | 当前喂 `queue.Queue`(线程安全);若改全局 list 则危险 |
| `MODEL_OVERRIDE`(contextvar) | `analyze_video_contextual.py` | **不跨线程**(见 §5) |
| `_USAGE`(contextvar) | `pipeline/usage.py` | **不跨线程**(见 §5) |

### 3.4 缓存面
**当前无缓存。** 但基础设施齐备:
- `AnalyzeResult` 是 Pydantic `BaseModel`,`node_executor._run_analyze_video` 已用 `.model_dump()` → JSON 可序列化。
- `pipeline/redis_client.py` 的 `build_redis_client()` 已被 session 仓 / transcript_store 复用,支持 TCP(redis-py)与 HTTP(Upstash)。
- 视频文件离线投递 GCS 后不再改 → 内容**静态**,可长存缓存。

## 4. 设计

### 4.1 并行 analyze_video(注入点 = `run_loop` 内层循环)
把串行 for 改成"**先分组、组内并发、组间顺序**":
1. **分组**:同一步内的 `calls` 里,把 `analyze_video` 且彼此 `uses` 不交叉的归为一个可并发组;其余工具(sql_query / sandbox / merge…)仍逐个串行。(本轮只并发 analyze_video,最简、收益最大。)
2. **并发**:对 analyze 组,用 `ThreadPoolExecutor(max_workers=min(len(group), MAX_ANALYZE_PARALLEL))` 提交;每个任务**必须** `ctx.run(...)` 携带本请求 context(§5)。
3. **回收**:`future.result()` 收齐后,**按 cid 顺序**写 `ledger` / `trace` / `responses`,保证回喂 Gemini 的顺序与串行一致(确定性,便于测试与 transcript 回放)。

> 步内可并发组完成后才进入下一步 → 跨步依赖天然满足,无需额外等待逻辑。

### 4.2 覆盖(配额)
两条路,可叠加:
- **放宽 cap**:并行后总延迟 ≈ max(单次),把 `MAX_VIDEOS_PER_REQUEST` 从 5 提到并行度可承受的值(见开放问题)。
- **先 SQL 缩到 top-N**:沿用 `_LOOP_SYSTEM` 已有指引("候选多 → 先 sql_query 缩范围"),把 12 个候选先缩到 top-N 再并行 analyze。缓存命中**不消耗配额**(见 4.3),进一步放大有效覆盖。

### 4.3 缓存
- **键**:`av:{video_id}:{md5(json.dumps({question, context, rubric, time_range, model}, sort_keys, ensure_ascii=False))}`。`model` 取实际生效模型(`MODEL_OVERRIDE.get() or PERCEPTION_MODEL`),避免 Pro/Flash 串味。
- **值**:`AnalyzeResult.model_dump()` 的 JSON。
- **存储**:**Redis 优先**(复用 `build_redis_client()`,Cloud Run 跨副本共享),**fail-open** —— Redis 不可用就实时分析,绝不卡主循环;无 Redis 退**进程内 LRU**(有界 dict + 锁)。
- **接入点**:`_run_analyze_video` 进 Gemini 前查、出后写。命中则**不消耗配额**(配额 +1 只发生在实际调 Gemini 时)。
- **TTL**:视频静态 → 可长存;保守取 `ARTIFACT_VALUE_TTL_SECONDS`(默认 24h)与 artifact 仓统一(见开放问题)。

### 4.4 延迟 + 成本度量
- `execute()` 包一层 per-tool 计时(`time.perf_counter()`),写进 `trace[i]`:`{cid, tool, ms, cache_hit}`。
- 步级聚合进 `loop_metrics`:`step_wall_ms`(组并发墙钟)对比 `sum(tool_ms)`(串行假想)→ 量化并行加速比 + 缓存命中率,可喂前端监控。

## 5. 【重点】并发正确性
并行的真正难点不是"加线程池",而是下面两类。**不解决就上线 = Pro 模式静默失效 + token 漏算 + 配额失控。**

### 5.1 contextvar 不跨线程(最隐蔽)
`MODEL_OVERRIDE`(`analyze_video_contextual.py`)与 `_USAGE`(`usage.py`)都是 `ContextVar`。它们现在能工作,**只因为整条链在同一个 HTTP 请求线程内**(orchestrator 设值 → 同线程深处 `MODEL_OVERRIDE.get()` 读到值)。

`ThreadPoolExecutor` 的 worker **默认拿不到**提交线程的 context → worker 里 `MODEL_OVERRIDE.get()` 返回 `None`(默认)→ **模型降级回 flash**(Pro 失效)。

**改法(必须)**:提交时拷贝当前 context,worker 在该 context 里跑。
```python
from contextvars import copy_context
ctx = copy_context()                       # 在【主线程】快照(含 MODEL_OVERRIDE / _USAGE)
fut = pool.submit(ctx.run, execute, cid, call.name, call.inputs, upstream, call.uses)
```
> `copy_context()` 自动带上**所有** contextvar,无需逐个枚举;每个 worker 一份**独立快照**,互不串。

**⚠️ 修正(核对发现)**:`analyze_video` 的 `_gemini_generate` **当前根本没调 `usage.add_usage`**(不像 router/code_generator/loop 那样上报)—— 所以**视频分析的 token/成本现在压根没算进 usage / 监控 / 审计**(这是个独立的现存缺口,不是并行才有的)。本设计要**顺手补上**:让 `_gemini_generate` 也 `add_usage(resp, 实际model)`。补完之后,因为 worker 的 `_USAGE` 是快照副本,需要**把各 worker 的 usage 汇总回主 context**(或把 `add_usage` 改成写一个**线程安全累加器**而非纯 contextvar)——这一步在 §5.2(B) 预分配 + 回收阶段主线程合并里一并解决。**补 usage 上报应优先于并行**(M4.1/M4.2 阶段先做),否则并行后 cost 监控仍然错。

### 5.2 共享可变状态的竞态
`quota` / `seen` / `trace` / `ledger` 串行下安全,并行下竞态。推荐 **(B)预分配 + 回主线程顺序写**(锁面最小):
- **并行前预分配名额**:分发 analyze 组前,主线程一次性按剩余配额给该组**切名额**(超额的当场返回"已达上限"结果),worker 内不再碰 `quota` → 根除计数竞态。
- **worker 只做无副作用的 analyze**;`ledger / trace / seen / responses / usage 合并` 一律回收阶段在**主线程**顺序写。
- 备选 **(A)加锁**:`quota` / `seen` / `trace` 各加 `threading.Lock`(更直接但锁面大)。

**`on_step` 线程安全**:回调当前在主线程回收阶段调用(非 worker)→ 维持现状。约定**禁止**在 worker 内调 `on_step`。

## 6. 改动点 + 受影响测试
| 文件 | 改动 |
|---|---|
| `pipeline/loop_driver.py`(run_loop 内层) | 串行 for → 分组 + `ThreadPoolExecutor` + `copy_context().run`;回收阶段单线程顺序写 `ledger/trace/seen` + 合并 usage |
| `pipeline/loop_driver.py`(`_make_executor`) | `quota` 预分配/加锁;命中缓存不 +1;per-tool 计时 |
| `pipeline/node_executor.py`(`_run_analyze_video`) | 接缓存:进 Gemini 前查 / 出后写 |
| `perception/analyze_video_contextual.py`(`_gemini_generate`) | **补 `usage.add_usage`**(现在没上报,§5.1);可选改线程安全累加器 |
| `pipeline/config.py` | 新增 `MAX_ANALYZE_PARALLEL`、`ANALYZE_CACHE_TTL_SECONDS`、`ANALYZE_CACHE_BACKEND` |
| `pipeline/`(新)`analyze_cache.py` | 缓存键/读写/fail-open/LRU fallback |

**受影响测试**
- `test_analyze_video_tool.py`:配额护栏、preview 大小 —— 加**并行下配额不漏算**用例。
- **新增**:(a)contextvar 跨线程传播单测(worker 内 `MODEL_OVERRIDE.get()` == 主线程设值;usage 不漏算回归);(b)缓存命中/未命中、命中不消耗配额、Redis 挂掉 fail-open;(c)并发竞态压测(N 个 analyze 并发,`quota["analyzed"]` 精确等于实际调用数);(d)并行后回喂顺序仍确定。

## 7. 里程碑(小步可回退)
- **M4.0 加固(零并行,立即可做)**:`_make_executor` 加注释/`assert`("contextvar/quota 不跨线程,改并行需 copy_context+Lock")。纯防卫。
- **M4.1 补 usage + 缓存(无并行)**:① 给 `_gemini_generate` 补 `add_usage`(修视频分析成本不计的现存 bug,**独立收益** —— 监控立刻准);② 接 `analyze_cache`(先进程内 LRU,后 Redis),fail-open,`ANALYZE_CACHE_BACKEND` 可一键关回。可单独上线。
- **M4.2 度量**:per-tool 计时进 trace + loop_metrics。只读零风险,为并行提供基线。
- **M4.3 并行(核心)**:`run_loop` 内层改造 + `copy_context` + 预分配。`MAX_ANALYZE_PARALLEL=1` 即退回串行 → **配置秒级回退**。先灰度(`=2`)看延迟与正确性(Pro 不降级、token 不漏、配额精确)。
- **M4.4 覆盖**:并行稳定后放宽 `MAX_VIDEOS_PER_REQUEST` / 落地 SQL top-N 预筛。配置回退。

## 8. 开放问题(评审定夺)
1. **并行度上限** `MAX_ANALYZE_PARALLEL`:Gemini 多模态并发 quota 是新瓶颈?起步 2–3,压测看 429/限流。
2. **配额放到几**:cap 从 5 提到多少?是否区分"调用数"与"不同视频数"(缓存命中不算)?
3. **缓存 TTL**:与 24h 统一,还是视频静态 → 更长(7d/永久)?若 `video_facts.predicate` 后台会更新,TTL 必须 ≤ 更新周期。
4. **Redis vs 进程内**:Cloud Run 多副本 → Redis 命中率高但加网络往返;进程内 LRU 零延迟但不跨副本。是否两级(L1 进程内 + L2 Redis)?
5. **usage 合并方案**:worker 快照里的 usage 如何精确合回主 context —— 改线程安全累加器,还是回收阶段手动 merge?(§5.1/§5.2 必须定方案再动手。)

---

### 已核实事实锚点
- 执行串行:`run_loop` 内层 `for call in calls` 逐个 BLOCKING `execute`;同一步 calls 无相互依赖(uses 只指前序步)→ 可并行。
- `quota` 是 `_make_executor` 闭包 dict;`MODEL_OVERRIDE` / `_USAGE` 都是 contextvar,worker 线程默认拿不到 → 必须 `copy_context`。
- `AnalyzeResult` 是 pydantic(可缓存);`redis_client` 已复用;视频内容静态。
- **核对修正**:`analyze_video._gemini_generate` 当前**未**调 `add_usage` → 视频分析成本现在不计;M4 需顺手补(优先于并行)。
