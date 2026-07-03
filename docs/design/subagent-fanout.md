# 设计:可选子 agent fan-out(CC-ultra 式,按需 spawn 不加默认层)

> 状态:Design(评审后动手) · 范围:`pipeline/subagents.py`(新)、`node_specs`/`node_executor`(新 `deep_analyze` 工具)、`loop_driver`(复用 run_loop) · 关联:[architecture-prefer-simplicity](../../.claude/...)(本设计的核心张力)、analyze-video-cost、one-loop-and-runtime-flags

## 1. 背景与核心张力

本系统是【一个大脑 + 无状态工具】,刻意不是多 agent(见 architecture-prefer-simplicity)。用户问:能否像 CC ultra 那样多 agent。**能,但必须不背叛"别加层"**。

**解法 = CC 的做法**:不是让一切都变多 agent,而是给主脑加【一个工具】,让它【只在任务值得时】自己决定 fan-out。默认单 loop(90% 的问题:计数/类别/找片段/看单个视频),只有【深度多视频任务】才 spawn 一批受限子 agent。CC ultra 也不是每轮开子 agent —— 主 agent 判断"活儿够大"才调 Agent 工具。

**已有 80% 零件,不用重造**:`run_loop`(纯控制流,子 agent = 换受限工具集的另一个 run_loop)、`ThreadPoolExecutor + copy_context`(并行,analyze 已用)、`analyze_cache`、配额闸。要加的只有:编排壳 + 工具声明 + 预算护栏。

## 2. 目标 / 非目标

**目标**:主脑能对【深度多视频任务】fan-out 一批子 agent,每个各自多步深看一个视频,再综合 —— 答得比单 loop 串行更深、更准。
**非目标**:
- 不改默认路径(单 loop 处理绝大多数问题,一点不变);
- 不做子 agent 递归(子 agent 【不能】再 spawn 子 agent —— 一层,防失控,同 CC);
- 不做长期自主 agent(每个子 agent 是一次性、有界、完成即死)。

## 3. 何时 spawn(触发,不是固定路由)

大脑自行判断,prompt 给判据(符合"跟着问题走"):
- **值得**:要对【多个(≥3)视频】做【需要各自多步深看的比较/排名/找细节】(如「这 8 个跳伞视频跨 精彩度/画面/动作难度 排名」「哪几个视频里有近距离编队」)。
- **不值得**:计数、类别、单视频问答、语义找片段 —— 这些单 loop/现有工具已够,**别 spawn**(浪费钱和时间)。

## 4. 工具形态:`deep_analyze`

```
deep_analyze(video_ids: list[str], question: str, dimensions?: list[str], model?: 'flash'|'pro')
  → { per_video: [{video_id, findings, scores?}], synthesis: "..." }
```
- 主脑先用 sql_query/semantic_search 把候选缩到 ≤N 个,再把 ids + 要考察什么交给本工具;
- 工具内部:**每个视频一个子 agent**(mini run_loop,系统 prompt 聚焦"深看这一个视频回答 question",工具集只给 `analyze_video`+`python`,步数上限低),线程池并行 → 收集 → **一个综合子 agent**汇总/排名 → 返回;
- 主脑拿到综合结果收口(可再 show_video 交付选中的)。

## 5. 成本纪律(必须内建,否则失控)

多 agent 贵(N 视频 × 每个 mini-loop 几步 = 几十次 LLM + 视频 token)。护栏:
| 护栏 | 值 | 作用 |
|---|---|---|
| `SUBAGENT_MAX_FANOUT` | 6 | 一次最多 spawn 几个(超了截断 + 告知) |
| 每子 agent 步数上限 | 4 | mini-loop 别自己转圈 |
| 复用 `MAX_VIDEOS_PER_REQUEST` 配额 | 共享 | 子 agent 的 analyze 也计总配额,不绕过成本闸 |
| `USE_SUBAGENTS` 开关 | 默认 0 | 灰度;关掉 = `deep_analyze` 从声明消失(零残留) |
| 预算感知 | 记 usage | 子 agent 的 token 全进 usage 审计(前端成本环照常算) |

## 6. 子 agent 用什么模型(接上 pro/flash 讨论)

- 主脑保持 **3.5-flash**(快速决策/编排);
- 子 agent「深看视频」用 **analyze_video 的模型**(默认 2.5-flash,Pro 开关时 2.5-pro);
- **可选**:刚发现项目能用 `gemini-3.1-pro-preview` —— 深度子 agent 是它的最佳用武之地(慢但深,反正只在深度任务用)。S 阶段 spike 对比 3.1-pro vs 2.5-pro 做子 agent 的质量/成本再定(`SUBAGENT_MODEL` env 可配)。

## 7. 复用点(不重造轮子)

| 要的 | 复用现有 |
|---|---|
| 子 agent 控制流 | `run_loop`(注入受限 conversation + execute) |
| 并行 | `ThreadPoolExecutor + copy_context`(同 analyze 组) |
| 免重看 | `analyze_cache`(子 agent 看过的进缓存,主脑/别的子 agent 命中免费) |
| 成本闸 | 共享 `quota` + `usage.add_usage` |
| 综合 | 一次普通 LLM 调用(genai),不需新机制 |

## 8. 里程碑

- **SA-0 spike**:3.1-pro-preview vs 2.5-pro/flash 做「深看一个视频回答复杂问题」的质量/成本/延迟对比 → 定子 agent 模型。
- **SA-1**:`pipeline/subagents.py`(fan-out + 综合,复用 run_loop/线程池/配额)+ 离线单测(mock 子 loop,验并行/上限/预算/fail-open)。
- **SA-2**:`deep_analyze` 工具声明 + node_executor 接线 + prompt 触发判据 + `USE_SUBAGENTS` 开关(默认关)。
- **SA-3**:验收探针(「8 个跳伞视频跨 5 维排名」等)对比单 loop 的质量/成本 → 数据说话决定默认开不开 + 对抗 review。

## 9. 风险

- **成本失控** → 护栏(§5)+ 默认关 + 预算感知;
- **收益不明** → SA-3 用真实深度任务对比单 loop,不明显更好就【只保留工具、默认关】,不强推(符合"别加层"—— 加了但不默认走);
- **延迟** → 并行 fan-out(墙钟 ≈ 最慢一个子 agent,不是求和),但仍比单 loop 慢;工具声明里让大脑对时间敏感的场景别用。

## 10. 开放问题(评审定夺)

1. **先做 SA-0 spike 还是直接 SA-1**?(倾向:先 spike 定子 agent 模型 —— 3.1-pro 值不值直接影响设计)
2. **fan-out 上限 6 合适吗**?(倾向:6 起步,SA-3 看真实成本再调)
3. **综合 agent 要不要也能调 show_video**(直接交付选中的),还是只返结论给主脑再 show?(倾向:只返结论,主脑收口统一 show —— 保持"交付归主脑")
4. **默认开还是永远手动开**?(倾向:SA-3 数据说话;大概率【保留工具、默认关】,像 web_search 那样按需)
