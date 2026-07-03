# 2026-07-03 SA —— spawn_agents 子 agent 异质分解(opt-in)

> 回应"能否像 CC ultra 那样多 agent":给主脑加**一个工具**而非改架构 —— 主脑当场为每个子任务写【不同】的 instruction,并行跑受限 mini-loop,自己综合。**异质分解**(评审纠正了最初的同质 per-video 设计 —— 拆任务、每个 agent 干不同的活,才是 CC 的真形态)。默认关,数据决定开不开。

## 0. 一览

| PR | 内容 | 回滚 |
|---|---|---|
| [#89](https://github.com/kenny0312/videosense-agent/pull/89) | 设计文档 v1(同质 fan-out,`deep_analyze`) | — |
| [#90](https://github.com/kenny0312/videosense-agent/pull/90) | 设计修订:同质 → **异质分解**(`spawn_agents(tasks:[{instruction,…}])`),评审反馈驱动 | — |
| [#92](https://github.com/kenny0312/videosense-agent/pull/92) | **实现**:`pipeline/subagents.py` + 全部接线 + 16 项单测 + 对抗 review 两处修复 | `USE_SUBAGENTS=0`(默认)或 revert |

线上:rev `videosense-00035-98m`(默认关,零行为变化)。

## 1. 工具形态(核心)

```
spawn_agents(tasks: [{instruction: "子 agent 要干什么(主脑现场写)",
                      video_ids?: [...], tools?: [...]}, ...])
  → [{instruction, output}, ...]        # 原样返回,综合归主脑
```

- **结构死、内容活**:编排形状固定在代码里(扇出 → 并行 → 收回),模型现场写的只有每个子 agent 的 instruction —— 与 CC Workflow 的区别是 CC 连编排结构都由模型写;这里刻意不做(别加层)。
- **多阶段无需引擎**:loop 本身就是外层编排器 —— 第 1 步 spawn 侦查、第 2 步读结果按需再 spawn、第 3 步综合,跨步串阶段,零新代码。
- 子 agent = 换了【受限声明 + 自定义 system】的另一个 `run_loop`(mini-loop,步数上限 4)。

## 2. 复用与护栏

| 复用 | 说明 |
|---|---|
| `run_loop`/`make_conversation` | 子 agent 控制流零新造 |
| **父 `execute` 闭包** | 关键:子 agent 的 analyze_video 计入**同一** `MAX_VIDEOS_PER_REQUEST` 配额(不绕成本闸);token 经 `add_usage` 折进本请求 usage 审计 |
| `ThreadPoolExecutor + copy_context` | 同 analyze 组范式;contextvar(`MODEL_OVERRIDE`/`_USAGE`)随线程传播 |
| `analyze_cache` | 子 agent 看过的片,主脑/其它子 agent 命中免费 |

护栏:`SUBAGENT_MAX_FANOUT=6`(超截断+告知)· `SUBAGENT_MAX_STEPS=4` · 工具白名单只读感知/检索(**剔除 spawn_agents = 一层无递归**;show_* 交付归主脑)· fail-open(单个子 agent 崩不拖垮整批)· `USE_SUBAGENTS=0` 关=声明消失零残留。

触发判据写在**工具声明**(planner_desc)—— 单工具用途归声明,不进宪法(保 byte-stable 缓存)、不进教训集(入集三问 #1 不过)。

## 3. 对抗 review(5 维 → 逐条对抗验证)

5 个独立视角(正确性/并发/成本递归/安全注入/集成回滚)审 diff,每条发现再由独立验证者以"默认 REFUTED"立场复核。**共 2 条发现、全部 CONFIRMED、0 误报**,均已修复+补回归测:

1. **medium** — task 请求的工具若被 feature flag 全关(如 `tools:['web_search']` 而 `USE_WEB_SEARCH=0`),decls 算成空 → 子 agent 无工具凭空编答案。修:与【当前启用】的声明求交,空则退回启用默认(analyze_video/sql_query 从不设门)。
2. **low** — `SUBAGENT_MAX_FANOUT` 误配 0/负 → `cleaned[0]` IndexError。修:clamp ≥1。

测试:`pipeline/test_subagents.py` 16 项(归一/白名单交集/无递归/截断/fail-open/真并行 Barrier/复用父 execute/开关门/e2e 分发+大格预览);全量回归 212 passed。

## 4. 验收探针(SA-0 + SA-3 合并跑)

同一异质任务(「跳伞/翼装类 vs 球类对抗运动类,哪类更精彩?每类深看 2 个代表」),本地三组配置各跑一次:

| 组 | 配置 | 触发 spawn? | 墙钟 | tokens | 成本 | 大脑步/工具 |
|---|---|---|---|---|---|---|
| A | 单 loop 基线(`USE_SUBAGENTS=0`) | —(工具隐藏) | **84s** | 142k | **$0.077** | 3 步:2 sql + **4 并行 analyze** + show |
| B | 子 agent @ 3.5-flash | ✅ 1 次 | 93s | 177k | $0.123 | 4 步:2 sql + **spawn_agents** + show |
| C | 子 agent @ 3.1-pro-preview | ✅ 1 次 | **157s** | 177k | $0.105 | 5 步:4 sql + spawn_agents + show |

**观察**:
1. **触发判据有效** —— B/C 在工具可用时都主动 fan out(这是可分解的异质任务),A 在工具关闭时正常单干。判据"值得才 spawn"起作用,没滥用。
2. **质量**:三者都强、结论先行、诚实(描述的是真实画面,无编造)。**B 最丰**(精确时间码 `0:08` 出舱 / `1:53` 开伞 / `0:44` 中球倒地、POV vs 第三人称对比更锐 —— 子 agent 每个视频深了一层);A 已很好(4 路并行 analyze 撑起细节);C 深度与 B 相当但有展示接缝(id 双重标注 `第2个视频（第1个）`)且慢 1.7×。
3. **成本/延迟**:fan-out = +60% token 换**中等**质量提升,并行让 B 墙钟贴近 A;3.1-pro(C)相对 flash **无**质量优势却大幅更慢。

**判定**:
- **默认保持关**(按需,像 web_search)—— 中等收益配 ~60% 成本溢价,不足以压过默认路径不变的"别加层"原则;工具已验证可用(判据只在该 spawn 时 spawn、护栏成立、成本折进审计)。
- **`SUBAGENT_MODEL` = 3.5-flash**(= `LOOP_MODEL` 默认)。**SA-0 结论**:3.1-pro-preview 做子 agent 无质量优势、慢 1.7×,不切换。

## 5. 上线决定

- **线上无需动作** —— 部署的 `00035-98m` 本就 `USE_SUBAGENTS` 未设=关,已与"默认关"决定一致;要按需启用只需在 Cloud Run 设 `USE_SUBAGENTS=1`(可随时,零重部代码)。
- 遗留(不阻塞,非本功能引入):多源合成答案里 id 清洗器偶有"第N个（第M个）"双重标注接缝 —— 各路径都有,后续 polish 候选。

## 6. 回滚

`USE_SUBAGENTS=0`(默认即是)= 工具从声明消失、零残留;或整条 revert PR #92。默认关意味着**本批次对现有行为零影响**。
