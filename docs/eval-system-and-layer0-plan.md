# VS 第 0 层落地计划：先造评测，再做自改进

> **一句话为什么 eval 先行**：VS 的大脑是**闭源 Gemini**（gemini-3.5-flash / 2.5-flash / 2.5-pro，无权重访问），你**不可能**在它上面跑 PPO/GRPO/RLVR；Vertex 只提供 SFT 与 DPO 偏好微调（少数模型），custom-reward RL 仅在 gemini-3.5-flash Preview 存在。因此第 0 层真正的瓶颈**不是算法，而是一把可信的自动裁判（verifier/judge）+ 一个真实的评测集**。没有它，GEPA、memory-of-lessons、best-of-N 三件事全都在拿"感觉"当度量衡——你根本不知道改动是进步还是退步。所以：**Part A（评测）是唯一优先级；Part B（GEPA/记忆/BoN）全部依赖 Part A 做度量衡。**

> **本文档读法**：Part A 是重头，讲"评什么 / 数据从哪来 / ground truth 怎么定 / judge 怎么校准 / harness 怎么接进 VS 的真实代码 / CI 门 / 分步清单"。Part B 对 GEPA、memory、best-of-N 各给**原理 / 做法 / 优劣**，并说明每项如何用 Part A 的 eval 闭环。Part C 是落地顺序与红线，Part D 是参考。

## 里程碑总览

| 阶段 | 目标 | 关键产出 | 验收 |
|---|---|---|---|
| **A0** | 金种子集 | probe-findings 21 轮 → 结构化 JSONL（~21-40 条） | 每条含 query / 期望形态 / 判据 / 维度标签 |
| **A1** | 可验证判分器 | 时间戳 IoU、DB 查得、工具序列、检索 hit@k、拒答率 | 无 judge，纯程序，确定性 |
| **A2** | 离线 harness | 接 `run_loop` seam，脚本用户 + stub 工具 | 单条 <1s，pass^k 报表 |
| **A3** | Judge + 校准 | 跨家族 judge（Claude 判 Gemini）+ ~200 人标校准 | Cohen's κ ≥ 0.6 才可当门禁 |
| **A4** | CI 回归门 | GitHub Action，pass^3 delta + pinned-case 硬门 | 分数回退/pinned 翻转 → 阻断合并 |
| **A5** | 飞轮 | 生产 failure（usage_audit→BigQuery）→ pinned case | 每个真实 bug 变永久回归用例 |
| **B1** | GEPA | 对 answer-synthesis / 工具选择 prompt 演化 | 用 A 的 test split 门禁提升 |
| **B2** | Memory-of-lessons | ReasoningBank 化 lessons.py + pgvector | A/B on frozen suite，成功率↑步数↓ |
| **B3** | Best-of-N + verifier | 仅 final-answer 轮，adaptive-N | 扫 N 找峰值，防 reward hacking |

---

# Part A — 评估数据集 + 评估系统（重点）

## A1 你到底要评什么：多轮多模态 video agent 的维度体系

单一"成功率"会把正交的失败模式糊成一团——时间戳退步会被工具选对掩盖。要**每维一个信号**。维度分两大家族：**可验证轴**（程序判，确定性，便宜，无偏，可当门禁）与**判断轴**（需 judge/人，主观残余）。核心设计原则：**把一切能推进可验证桶的都推进去**，judge 又贵又有偏。

| 维度 | 家族 | 判据例子（1-2 个） |
|---|---|---|
| **接地 / 幻觉** | 可验证 | 对抗视频对（VidHalluc 式）：问一个**配对但不同**的视频里的动作，正确答案必须是"不存在/未见到"；判"拒答 vs 编造"，无需 judge |
| **时序 / 时间戳** | 可验证 | 输出 [start,end] vs 金标 span：R@1 at IoU∈{0.5,0.7} + mIoU（TVG 标准指标） |
| **工具选择与调用正确性** | 可验证 | 工具名 exact-match + 参数 JSON-diff；轨迹级冗余率（同一 tool+params 重复调用计罚，对应 `LOOP_REPEAT_LIMIT`） |
| **检索正确性** | 可验证 | semantic_search / pgvector：recall@k + MRR vs 标注相关 clip；且"答案确实用了检索证据"（grounded-in-retrieved） |
| **多轮连贯 / 上下文跟踪** | 半可验证 | 槽位跟踪：turn 3 提到的实体/时间戳，turn N 必须保持（可程序查）；语气连贯 → judge |
| **诚实 / 拒答** | 可验证 | 标注"视频无法回答"集合：判拒答率 + 假答率（skydive"说没有"就是这条的经典反例） |
| **成本 / 延迟** | 可验证 | 每任务 tokens / \$ / wall-ms / #analyze_video 调用数；门禁看 p50/p95 不看均值。一次 analyze_video ≈ 60k tok / \$0.018（flash）是主导成本，"更聪明"的 agent 悄悄 3× 调用要立刻抓到 |

**关键陷阱**：
- **别把幻觉折进"accuracy"**——模型能在可答 QA 上高分、在不可答上照样编。**必须有独立的不可答/对抗 split。**
- 时间戳 IoU 要**统一 span 约定**（含/不含端点、fps 取整），否则全是假失配。
- 对抗负例要挑 **CLIP 高语义 / DINO 低视觉相似**的对（VidHalluc 洞见），随机负例太easy。

## A2 数据集从哪来：用 VS 现有资产造种子集 + failure 飞轮

**别从零标注。VS 已经有金矿。**

### A2.1 金标种子（最高价值）
- **probe-findings 21 轮**（`docs/design/probe-findings-upgrade-plan.md`）：真实多轮对话，每轮已有 **Q / A / 工具链 / good-weak-broken 判定 / 严重度 / `file:line` 根因 / DB 审计的正确答案**。这是**现成的闭环 eval 任务**，直接抽成 JSONL：

```jsonc
{"id":"B1", "dim":["retrieval","honesty"], "turns":[
  {"q":"有没有做饭的视频",
   "expect":{"must_retrieve":["preparing salad","cutting pumpkin","eating salad"],
             "must_not":"回答不存在"},
   "root_cause":"宽类中文→单窄词未 ILIKE 匹配"}]}
```

  A/B/C/D 四组直接映射维度：A1-A3=显示收口/接地，B1-B3=检索/类目，C1-C3=self-knowledge/诚实，D1-D2=边界。**最小可用就从这 21 轮起步。**

### A2.2 从 traces / transcript 挖候选
- `pipeline/trace.py`（TraceStep：name/status/elapsed_ms/meta/error）与 `pipeline/transcript_store.py`（每轮 user/tool_call/tool_result/answer 事件 + GCS 全量）——**已录制的真实工具链**是现成的候选轨迹，抽出来人工加金标即可。

### A2.3 用 usage_audit 定位真实 failure
- `api/server.py` `_audit()` 打的 `logType="usage_audit"` JSON → Cloud Logging → BigQuery。query `status="error"` 或高 `step_count`/`terminated_reason=max_steps`/成本异常，就是**真实生产失败清单**。

### A2.4 failure → pinned-case 飞轮
每个确认的生产失败：**最小化**到最小复现任务 → 标注金标（时间戳/期望检索/期望拒答）→ 打维度标签 → commit 成**pinned 回归用例**（带断言）。pinned case 走**硬门**（必须过），与软聚合阈值分开。这样同一个 bug 永远不会悄悄回来。种子 pinned：skydive"说没有"、任何 analyze_video 成本爆炸。

### 最小可用 = 多少条
- **起步：50-150 条总量，每维 10-25 条。** 先把 probe 21 轮榨干（~21-40 条），再从生产失败长到 100+。judge 校准另需 ~200 条人标（见 A4）。别追求一次性大集合——**从生产失败长出来**才代表真实分布。

## A3 Ground truth 怎么定：可验证轴程序判 + 开放轴 judge，共存于一个 harness

原则：**能确定性判分的维度，就不付钱给有偏又贵的 judge。** video 富含可验证信号。

| 轴 | 判法 | 实现 |
|---|---|---|
| 时间戳 | IoU | 预测 [s,e] vs 金标，算 R@1@{0.5,0.7} + mIoU，纯函数 |
| DB/实体状态 | state-diff | 任务后 diff 环境/记忆状态 vs goal spec（tau2 模式） |
| 检索 | recall@k / MRR | vs 标注相关 clip；+ 答案引用检索证据检查 |
| 工具调用 | JSON-diff | tool 名 + args vs 期望；+ 冗余率 |
| 幻觉 | 拒答 vs 编造 | 对抗对/不可答集，正确输出=拒答，无 judge |
| 连贯/语气/自由叙述忠实度 | **judge** | 跨家族 rubric judge（见 A4） |

**共存机制**：harness 对每条任务跑**所有适用的判分器**，输出一个 `dim -> score` 字典（不是单标量）。可验证判分器先跑（快、确定）；judge 只跑在被标为"开放轴"的任务上。**迁移铁律**：想加 judge 指标前先问"能不能标个 ground truth 变成可验证？"——video 通常答案是"能"。

**陷阱**：可验证只与金标一样好——错的金标时间戳会静默 fail 掉好模型，标注要复核。别用**生成答案的同一个模型**去验证（自我 caption 再自我 check 是循环论证）。

## A4 Judge 怎么建与校准

**judge 是测量仪器；未校准的仪器产出自信的垃圾。** 已知偏差：位置（偏第一个，pairwise 必swap）、冗长（偏长，+15-30pp）、**自偏好**（偏自家族，+10-25%）、格式、校准漂移。

**设计**：
- **pointwise**（rubric + 整数刻度 + 强制 rationale）——需要绝对分做门禁时用。
- **pairwise**（A vs B）——只需排两个版本时用，更稳但不给绝对 bar。
- rubric 给**具体锚点 + 每档 2-3 few-shot**，**强制先 CoT 再打分**。

**去偏（具体做法）**：
- **位置**：pairwise 跑**两个顺序**，只算 swap 一致的；不一致标记/丢弃。
- **冗长**：rubric 只评内容不评长度；对无支撑的注水扣分。
- **自偏好（关键）**：**用与被测 agent 不同家族的 judge**。VS 大脑是 Gemini → **用 Claude（非-Gemini）当 judge**，绝不让模型判自己的输出做门禁。
- **多轮/多语言分别校准**——一个全局 κ 会掩盖它在哪不可靠。

**校准到可当门禁**：建 ~200 条人标金集（跨分数区间；来源见"judge 校准数据"——probe baseline ~75 + lessons ~30 + 语义 ~30 + transcript ~40 + user memory ~20 + 其他 ~5）。测 judge↔人 的 **Cohen's κ**（不是裸一致率——偏斜标签下裸一致率会掩盖 chance-level）。
- **κ ≥ 0.6（substantial）→ 才可当门禁（gate）**
- κ 0.4-0.6 → 仅上仪表盘/advisory
- κ < 0.4 → 不上
- **judge 模型或 rubric 一改，必须重测 κ**（校准漂移是有名的失败模式）。校准集 keyed by `model_id` + `lessons.py` commit hash + config 快照，入库可复现。

## A5 Harness 怎么搭：接进 VS 真实代码

### A5.1 单一最佳接入 seam：`run_loop()`（`pipeline/loop_driver.py:133`）
理由：**纯控制流**，无 Gemini/DB/sandbox 依赖，只需注入两样——
- `conversation`（`send(msg)->(calls, text)`）→ 用 `ScriptedConv`（`pipeline/test_loop_driver.py:13`）脚本化
- `execute`（tool 执行器）→ 用 `make_exec(values=..., fail=...)`（`test_loop_driver.py:24`）stub

返回 `LoopResult`：`answer / trace / ledger / terminated / steps / step_walls / llm_calls`——全可观测，<1s，无网络。已被 40+ 现有测试打磨过。

**其它 seam 及用途**：
- `run_query`（`pipeline/orchestrator.py:63`）——集成级，带 `Session` + transcript replay，测**真多轮**（`test_multiturn.py` 模式）。
- `run_query_loop`（`loop_driver.py:570`）——需 sandbox/trace/schema，较重。
- `video_vibe_query`（`api/server.py:216`）——E2E HTTP，需真 server/auth，只在 nightly 用。
- 模型 pin：`config.LOOP_MODEL`（默认 gemini-3.5-flash）可 env / monkeypatch / 直接赋值切换（baseline 用 `gemini-2.5-pro`，回归用 `gemini-2.5-flash`）。

### A5.2 脚本用户 vs 模拟用户，pass^k 而非 pass@1

**多轮不能用静态输入输出对**（turn N 的输入依赖 agent 的 turn N-1）。
- **CI 门禁用脚本用户**（固定轮次，零方差，确定）。
- **覆盖/鲁棒/goal-shift 用 LLM 模拟用户**（tau2-bench **dual-control**：模拟用户 + agent 都作用于共享有状态环境，用户被环境状态**紧耦合**，比自由 chatbot 用户一致得多；任务用 **DB-state diff + communicate-check** 判定）。
- **模拟用户是已知误差源**：跨 user-LLM 可摆动 ~9pp，且系统性误标（难→低估，中→高估）。所以：**pin 并版本化模拟用户模型**（升级它会像 agent 退步）；用**结构化 persona+goal 对象**+每轮 goal 提醒注入压漂移；先对小规模真人 transcript 验证。**绝不把模拟用户 pass 率当绝对能力真值，只用于相对回归追踪。**

**pass^k 不是 pass@1**：agent 非确定，70% 单轮 → 连续 3 次全对只有 0.70³≈**34%**。用户体验的是 pass^k（k 次全成功），不是 pass@1（乐观上限）。
- 每任务跑 **n 独立 rollout**（n≈5-10 仪表盘，≥10-20 抓小回归；n≥k）。
- **无偏估计**：`pass^k = E_task[ C(c,k) / C(n,k) ]`（c=成功次数）。**别用 (c/n)^k 点近似**——用组合估计器（tau-bench 报的那个）。
- 聚合 CI **要 bootstrap over 任务**（难度异质），不只 trial。版本对比**必须配对**（同任务+种子，McNemar / paired bootstrap）。
- **头条数字报 pass^3**；PR 快反馈用 pass^1 delta。因每 rollout 烧 analyze_video \$，CI 金集 n=5，weekly 深跑 n=10-20。

### A5.3 最小 harness 伪代码骨架

```python
# evals/runner.py (NEW) — 接 run_loop seam
from pipeline.loop_driver import run_loop, loop_metrics
from pipeline.test_loop_driver import ScriptedConv, make_exec
from math import comb

def run_case(case, *, n=5, model=None):
    """case = {query, script, tool_results, expect, dims, max_steps}"""
    successes = 0
    per_dim = {}
    for _ in range(n):                      # n 独立 rollout → pass^k
        conv = ScriptedConv(case["script"])              # 脚本用户/脚本 LLM 回合
        execute = make_exec(values=case["tool_results"]) # stub 工具结果
        r = run_loop(case["query"], conv, execute,
                     max_steps=case.get("max_steps", 16))
        # ---- 可验证判分器（无 judge，确定性）----
        scores = {}
        scores["timestamp_iou"] = iou_r1(r, case["expect"])       # R@1@{.5,.7}
        scores["retrieval"]     = recall_at_k(r, case["expect"])  # hit@k / MRR
        scores["toolcall"]      = toolseq_match(r.trace, case["expect"])
        scores["honesty"]       = refusal_ok(r.answer, case["expect"])
        # ---- 开放轴：跨家族 judge（Claude 判 Gemini），仅标注的维度 ----
        if "coherence" in case["dims"]:
            scores["coherence"] = claude_judge(case, r.answer)    # κ≥0.6 才门禁
        ok = all(v >= case["expect"]["thresh"].get(d, 1.0)
                 for d, v in scores.items())
        successes += int(ok)
        for d, v in scores.items(): per_dim.setdefault(d, []).append(v)
    pass_k = {k: comb(successes, k)/comb(n, k) if n >= k else None
              for k in (1, 3, 5)}           # 无偏 pass^k
    return {"id": case["id"], "pass_k": pass_k, "per_dim": per_dim,
            "cost": loop_metrics_cost(r), "verdict": "PASS" if pass_k[3] else "FAIL"}

def run_suite(cases, **kw):
    results = [run_case(c, **kw) for c in cases]
    # 聚合 CI：bootstrap over 任务；对比 baseline 用配对检验
    return aggregate_paired(results, baseline="pinned")
```

## A6 CI 回归门

- 建**小而快的金集（~100-300 任务，尽量脚本化/确定性）**，每 PR 分钟级跑完。
- **触发**：任何改动 prompt / 模型 id / 检索配置 / 工具定义 / **`pipeline/lessons.py`**（它改行为）。
- **门禁逻辑**：对 **pass^3 delta**（非裸 pass@1）门；`OR` **任一 pinned 回归 case 从 pass→fail**（per-case 硬门——单个重现 bug 即阻断，哪怕聚合持平）。
- **区分真回归 vs 方差**：**配对**（同任务+种子 baseline vs PR）+ 假设检验，要求跌幅超过 baseline CI，别用裸阈值（噪声会假警报）。
- **别在 per-PR 门上跑 judge/模拟用户任务**（方差导致 flaky）——门只跑**可验证**任务；judged/n=10-20 深跑放 **nightly/weekly** advisory。
- **红线**：VS 是**手动部署、main 落后 live**。门必须跑在**实际发布的分支**上，不是 stale main。目前无 `.github/workflows`——**立刻加一个 Action** 是最大即时收益。

## A7 Part A 分步落地清单

| 步 | 做什么 | 产出物 | 验收标准 |
|---|---|---|---|
| A0 | 把 probe-findings 21 轮抽成 JSONL（query/expect/判据/维度/根因） | `evals/seed_probe.jsonl`（~21-40 条） | 每条可被程序读取、维度标签齐 |
| A1 | 写可验证判分器：IoU、recall@k、toolseq_match、refusal_ok、cost | `evals/scorers.py` | 各有单测；无 judge 依赖 |
| A2 | 接 `run_loop` 建离线 harness + pass^k | `evals/runner.py` + `evals/test_runner.py` | 单条 <1s，输出 pass^{1,3,5} + per_dim |
| A3 | 建 ~200 条人标校准集，跑跨家族 Claude judge，算 κ | `evals/judge_calibration.jsonl` + κ 报告 | κ≥0.6 的维度才标为"可门禁" |
| A4 | GitHub Action：pass^3 delta + pinned 硬门，配对检验 | `.github/workflows/eval-gate.yml` | 回退/pinned 翻转 → 阻断合并 |
| A5 | 飞轮脚本：BigQuery 挖 failure → 最小化 → pinned case | `evals/pinned/*.jsonl` + 挖掘 query | skydive 等已知 bug 已入 pinned |

---

# Part B — 第 0 层其余（eval 之后）

> 三者**组合不竞争**，且**全部以 Part A 的 eval 当度量衡**。推理成本上：GEPA 几乎免费（只改静态 prompt），memory 每轮加一次 embedding 查，**BoN 是唯一乘推理成本的**——门它门得最狠。

## B1 GEPA / DSPy（反射式 prompt 演化）

**原理**：GEPA（arXiv:2507.19457，ICLR 2026 Oral）把 prompt 优化当**自然语言反射引导的进化搜索**，而非标量 reward。循环：跑当前 prompt 候选于 minibatch 抓完整轨迹 → 一个更强的 **reflection LM** 读轨迹 + 指标的**文字反馈**，写出诊断（哪对哪错为什么）并突变 prompt → 评估加入候选池。关键是 **Pareto 前沿**：保留每个"在至少一个实例上最优"的候选（不是全局单最优，避免局部最优），从前沿采样父代，merge 步把不同候选的互补 lesson 拼接。核心论点：**语言是比 policy gradient 远更丰富的学习信号**——单条轨迹的文字反馈就够做大的定向编辑，因此比 GRPO 少用 **35×** rollout。

**做法（wire to Vertex 闭源 Gemini）**：
1. `pip install dspy`，经 LiteLLM `vertex_ai/` 前缀指向 Vertex Gemini：
   ```python
   task_lm = dspy.LM('vertex_ai/gemini-2.5-flash', vertex_project='PROJ',
                     vertex_location='us-central1', vertex_credentials='/sa.json')
   reflection_lm = dspy.LM('vertex_ai/gemini-2.5-pro', temperature=1.0, max_tokens=32000)
   dspy.configure(lm=task_lm)
   ```
2. 把要优化的 VS prompt 表达成 `dspy.Module`/Signature——**优先 answer-synthesis、决定 analyze_video vs semantic_search 的工具选择 prompt、taxonomy/受控词表指令**（对应 `pipeline/node_specs.py` SPECS 与 `loop_driver.py` 的 `_loop_system`）。
3. 写 metric-as-feedback（**5 参签名**）`def metric(gold, pred, trace, pred_name, pred_trace) -> {'score': float, 'feedback': str}`——**feedback 字符串是承重件**，放真实失败诊断（"答案漏了 00:32 跳伞段；检索没返回 video_facts"），不是裸数字。
4. `dspy.GEPA(metric, reflection_lm=reflection_lm, auto='medium', track_stats=True).compile(student, trainset, valset)`；val ~35 条。
5. **ADK 替代**：若 agent 已是 ADK agent，`adk optimize` 用**同一 GEPA 算法**优化 root system instruction，零 DSPy 锁定。
6. **用 A 当度量衡**：metric 的 `feedback` 直接喂 Part A 的可验证判分结果 + probe 失败签名；**GEPA 从没见过的 A3 test split 门禁提升**，防过拟合。

**优劣**：
- 优：无需权重（闭源完美契合）；比 GRPO 少 35× rollout、比 MIPROv2 +12% AIME，甚至**平均比 GRPO 高 6%**；产出**人类可读可手改**的 prompt；**推理时零成本**（只改静态文本）。
- 劣：需像样 held-out val + **有意义的文字反馈**（裸标量会退化成 MIPROv2 水平）；reflection LM（pro 32k tok/步）花真钱——先 `auto='light'` 看前沿再加预算；**离线批处理，不会 mid-session 适应用户**（那是 memory 的活）；多轮的 per-turn 归因难——确保 `pred_trace` 真的流对了那一轮的工具输出。

## B2 Memory-of-lessons（ReasoningBank / ExpeL）

**原理**：从 agent **自己**过去轨迹（**成功 + 失败**）蒸馏可复用的**策略级 lesson**，存成可嵌入文本，新任务时 top-k 检索进上下文，再写回。ReasoningBank（arXiv:2509.25140，Google）每条记忆是 `{title, description, content}`，**刻意抽象掉 run-specific 细节**以便迁移；**关键是也挖失败**：temp-0 的 LLM-as-judge 自标 Success/Failure（无 ground truth），成功→"验证过的策略"，失败→"反事实陷阱/预防性 lesson"（避免这样做的规则）。失败通道正是它胜过只存成功例的旧记忆系统之处。与 test-time scaling 复合（MaTTS）：更多 rollout 产生对比信号 → 更好的 lesson。

**做法（复用 pgvector + `pipeline/lessons.py`）**：
1. **Schema**：把 `lessons.py` 的 ad-hoc append 升级为 `{title, description, content, polarity: strategy|pitfall, source_task_id, created_at, embedding}`，每轨迹**≤3 条**（防膨胀）。保留现有 L01-L12 governance（MAX_LESSONS 预算、退役条件）。
2. **蒸馏**：session 后 temp-0 自判 Success/Failure → 第二 prompt：成功抽可迁移策略，失败抽预防 lesson（"当 X，别 Y，因为 Z"）；用**多语言 embed 模型**（承接你的既有教训）嵌入 `content`。
3. **检索**：嵌入进来的**任务意图**（不是整段多模态上下文，会稀释向量），cosine top-k（k=3-5），注入系统 prompt 并**强制"先声明每条 lesson 是否相关再行动"**（relevance-gating 显著降低误用）。复用 `semantic_search` / `content_embeddings` 检索底座。
4. **写回 + per-owner 隔离**（承接 `user_memory.py` 的跨 session per-owner 记忆），一个用户的 lesson 别污染另一个。
5. **用 A 当度量衡**：**A/B on frozen suite（memory ON vs OFF）**测成功率 AND 步数（ReasoningBank 报 +34.2% / -16% 步）；跑**投毒 canary 集**监测漂移。skydive 失败会直接被一条 pitfall lesson（"跳伞在 skydive_segments 不在 video_facts，答'没有'前查两处"）预防。

**优劣**：
- 优：**真在线**（session 间无重训改进）；失败通道给 GEPA 离线预料不到的预防 lesson；可读可编辑可删；推理便宜（一次 embedding + 几百 token）；随时间复利。
- 劣：**投毒 / 漂移是已证实攻击面**（MemoryGraft：投毒"成功"经验致持续行为漂移，检索召回 36-50%）；自判标签可能错，蒸出自信的**错** lesson；检索错配注入无关 lesson 浪费上下文；无界增长烂掉检索精度。**界限**：只从 agent 自己跑的轨迹蒸馏（绝不从用户文本）；新 lesson **隔离/信任衰减**至被检索且成功几次才影响高风险轮；LRU/效用淘汰控大小；定期 canary/consolidate 剪枝矛盾旧 lesson。**没 eval 就是盲飞——memory 会静默降性能。**

## B3 Best-of-N + verifier / 生成式奖励模型

**原理**：从固定 policy 采 N 个候选，verifier 打分取 argmax，把额外推理算力换准确率，不动权重。**verifier 质量是一切**。生成式验证器 GenRM（arXiv:2408.15240）把验证当 next-token 预测：prompt"这答案对吗？(Yes/No)"读 'Yes' token 概率当分；**GenRM-CoT** 先生成批判 CoT 再 Yes/No，还能采多个 CoT 多数投票。GenRM 胜过判别式 RM、DPO verifier、裸 LLM-judge（GSM8K 73%→93.4%）。**adaptive-N** 控成本：不固定 N——早停（分布稳定即停）、难度信号（熵/难度排序器）只在难例花 N、per-candidate 最优停止。

**做法（仅 final-answer 轮）**：
1. **只对要紧的轮做 BoN**——**final-answer 合成，不是每个工具调用**（工具调用确定性验证即可）。
2. 采 N（起 4-8）答案，temp>0。
3. **verifier = 证据接地的 GenRM-CoT**：给 Gemini"结合视频问题 + 检索到的 video_facts/transcript/skydive_segments + 此候选，逐步推理是否被证据完全支持，输出 VERDICT: Yes/No + CONFIDENCE"；分=P(Yes)。采 3 次 CoT 多数投票。**接地既是质量杠杆也是反 reward-hack 杠杆**——verifier 必须引证据，就不会被流畅但无支撑的散文骗。
4. **adaptive-N**：默认 N=1，仅当 verifier confidence 低 / 前两样本分歧时升级；候选一致或过 confidence bar 即早停。
5. **veto verifier**：关键失败（幻觉出段、错时间戳）**硬 fail**，不让一个致命缺陷被流畅度盖过。
6. **用 A 当度量衡**：**在 held-out 上扫 N 找准确率峰值并 cap 在那**（accuracy vs N 常呈驼峰，reward hacking 会让高 N 反降）；verifier 本身也用 A 的可验证轴校准。

**优劣**：
- 优：纯推理时、无权重无数据；GenRM/CoT 显著胜过 LLM-judge 且随 verifier 算力 scale；adaptive-N 收回大部分成本。
- 劣：**乘推理成本 + verifier 加串行延迟**（与 GEPA/memory 相反，是最该狠门的一项，尤其 chat 延迟敏感）；**reward hacking**——N 增大越选利用 verifier 盲点的答案（Goodhart，准确率先升后降）。缓解：cap N（别"为保险"设高）；verifier 用更强/不同 prompt/证据接地/小 ensemble（**别让同 tier Gemini 验自己的输出**——共享盲点）；Soft-BoN/pessimism（arXiv:2604.04648 用下置信界）把 argmax 往 reward-KL 最优 policy 调温。

## B4 三者与 eval 的闭环关系（文字图）

```
                    ┌─────────────────────────────────────────────┐
                    │   Part A  评测系统（唯一度量衡）             │
                    │  可验证判分器 + 跨家族 judge + pass^k + CI 门 │
                    └───────▲──────────▲──────────▲────────────────┘
                            │          │          │
        度量提升(test split)│   A/B on frozen suite│  扫 N 找峰值/校验器校准
                            │          │          │
   ┌────────────────┐  ┌────┴─────┐ ┌──┴───────┐ ┌┴──────────────┐
   │ 生产 failure   │→│ B1 GEPA  │ │ B2 Memory │ │ B3 Best-of-N   │
   │ (usage_audit→BQ)│  │ 离线演化 │ │ 在线蒸馏  │ │ 推理时验证     │
   │  →pinned case  │  │静态prompt│ │(pgvector) │ │(GenRM,adaptiveN)│
   └───────▲────────┘  └────┬─────┘ └──┬───────┘ └┬───────────────┘
           │                │          │          │
           └────────────────┴──────────┴──────────┘
              三者的新失败 → 回流成新 pinned 回归用例（飞轮闭环）
```

- GEPA 改的静态 prompt、Memory 存的 lesson、BoN 的 verifier——**改动是进是退，全由 A 判**。
- 三者跑出的**新失败**回流成 A 的新 pinned case——飞轮闭环。
- MaTTS：B3 的 rollout 也产对比信号，可蒸馏进 B2，采样不是纯成本。

---

# Part C — 落地顺序、里程碑与红线

**顺序（不可乱）**：
1. **Part A 全部**（A0→A5）——先有度量衡。
2. **B1 GEPA**——最高杠杆、推理零成本，用 A 的 test split 门。
3. **B2 Memory-of-lessons**——在线自改进，A/B on frozen suite 守门。
4. **B3 Best-of-N**——最后，成本最高，只上 final-answer 轮 + adaptive-N，扫 N 找峰值。

**红线**：

| 红线 | 说明 |
|---|---|
| **无 eval 勿微调** | 没有 A 的可信度量，GEPA/memory/BoN 全是拿感觉当尺子。**A 未达 κ≥0.6 前不碰 B。** |
| **reward hacking** | BoN 的 accuracy-vs-N 是驼峰——**扫 held-out 找峰值并 cap**，绝不设高 N"保险"；用 veto + 证据接地 verifier。 |
| **judge bias** | 位置/冗长/**自偏好**——**用 Claude（非-Gemini）judge**，pairwise 必 swap，改 judge/rubric 必重测 κ。 |
| **sim-user drift** | **pin 并版本化模拟用户模型**（升级它像 agent 退步）；结构化 persona+goal + 每轮 goal 提醒；绝不拿 sim-user pass 率当绝对真值。 |
| **memory 投毒/漂移** | 只从自己轨迹蒸馏；新 lesson 隔离至成功几次；LRU 淘汰 + canary 剪枝；per-owner 隔离。 |
| **CI 门跑对分支** | 手动部署 + main 落后 live——**门跑实际发布分支**，别跑 stale main；per-PR 门只跑可验证任务。 |
| **成本不可忘** | 一次 analyze_video ≈ 60k tok/\$0.018 是主导——每任务显式记 #analyze_video 调用，"更聪明"的 agent 悄悄 3× 要立刻抓。 |

---

# Part D — 参考清单

**评测系统**
- tau-bench (arXiv:2406.12045) — pass^k = E[C(c,k)/C(n,k)]；SOTA agent pass^1≈61%→pass^8≈25%
- tau2-bench (arXiv:2506.07982) — dual-control 环境、DB-state + communicate check、one-tool-per-turn、组合任务生成器
- Lost in Simulation (arXiv:2601.17087) — LLM sim-user 是不可靠 proxy，~9pp 摆动、系统性误标
- Drift No More (arXiv:2510.07777) — 漂移=turn-wise KL vs goal-consistent 参考；reminder 干预降漂移
- Beyond pass@1: Reliability Science (arXiv:2603.29231) — 二项 CI、rollout 定量、回归检测
- VidHalluc (arXiv:2412.03735, CVPR2025) — 对抗视频对幻觉，CLIP 高语义/DINO 低视觉负例
- INFACT (arXiv:2603.11481) — 诱导忠实度/事实性探针
- AgentChangeBench (arXiv:2510.18170) — TSR/工具效率/冗余率/goal-shift 恢复
- TVG 指标 — R@1@{0.5,0.7} + mIoU（VideoChat-T / TimeChat）
- LLM-Judge Bias Mitigation (futureagi.com, 2026) — 五偏差与量化（冗长 15-30pp，自偏好 10-25%）
- MM-JudgeBias (arXiv:2604.18164) — MLLM-as-judge 的组合偏差
- Pragmatic Engineer / Braintrust / Hamel Husain — 金集当单测、PR 门、failure→回归飞轮

**第 0 层方法**
- GEPA (arXiv:2507.19457, ICLR2026 Oral) — Pareto 前沿、reflection LM、比 GRPO 少 35× rollout
- DSPy GEPA docs — 5 参 metric 签名、`auto`/`reflection_lm`、val≤35
- LiteLLM VertexAI — `vertex_ai/gemini-2.5-*` + project/location/credentials
- Google Cloud `adk optimize` — GEPA 作为 Quality Flywheel
- ReasoningBank (arXiv:2509.25140, Google) — {title,description,content}、成功+失败蒸馏、+34.2%/-16% 步、MaTTS
- ExpeL (arXiv:2308.10144) — 成功轨迹 exemplar + 抽象 insight 双轨
- MemoryGraft (arXiv:2512.16962) — 投毒经验检索，36-50% 召回 → 动机做 provenance + quarantine
- Generative Verifiers / GenRM (arXiv:2408.15240) — Yes/No next-token、CoT+投票，GSM8K 73→93.4%
- Inference-Time Reward Hacking (arXiv:2506.19248) / Best-of-N with Pessimism (arXiv:2604.04648) — BoN 过优化、Soft-BoN/pessimism 调温

**VS 现有可复用资产**
- `pipeline/loop_driver.py:133` `run_loop`（**推荐 eval seam**）/ `:570` `run_query_loop` / `:368` `_make_executor`
- `pipeline/orchestrator.py:63` `run_query`（集成级）
- `pipeline/node_executor.py:400` `execute_node`（工具分发）
- `api/server.py:216` `video_vibe_query` / `:180` `_audit`（usage_audit→BigQuery）
- `pipeline/test_loop_driver.py:13` `ScriptedConv` / `:24` `make_exec`（stub 复用）
- `pipeline/trace.py` / `pipeline/transcript_store.py`（轨迹/多轮全量）
- `pipeline/lessons.py`（L01-L12，B2 升级基底）/ `pipeline/user_memory.py`（per-owner 隔离）
- `docs/design/probe-findings-upgrade-plan.md`（21 轮金标种子）
