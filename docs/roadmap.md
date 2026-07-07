# VideoSense 能力提升 Roadmap：Eval 地基 → 自改进 → 托管调优 → 开源学生

> **这是什么**：VS「不换架构、靠微调/自改进把性能练强」这条线的**执行顺序 + 里程碑 + 验收**。
> 配套三份文档（下面每个 Phase 都链到对应章节）：
> - [eval-system-and-layer0-plan.md](eval-system-and-layer0-plan.md) — Phase 0 + Phase 1 的完整设计（原理/做法/优劣 + 真实代码 seam）
> - [grpo-qwenvl-tutorial.md](grpo-qwenvl-tutorial.md) — Phase 3 的动手学习指南（GRPO 微调 Qwen-VL）
> - [design/dag-to-loop-roadmap.md](design/dag-to-loop-roadmap.md) — 另一条线（DAG→loop 迁移，已基本完成），本表不覆盖
>
> **贯穿原则**：**没有 Phase 0 的可信度量衡，后面一切微调都是拿感觉当尺子。** VS 大脑是闭源 Gemini（gemini-3.5-flash / 2.5-flash / 2.5-pro，无权重）——瓶颈从来不是算法，是**一把可信的自动裁判 + 一个真实评测集**。所以 Phase 0 是**唯一硬门**。

## 依赖图

```
Phase 0 (Eval 地基, GATE) ──┬──▶ Phase 1 (零权重自改进: GEPA / memory / BoN)
                            │
                            ├──▶ Phase 2 (托管调优: Vertex SFT → DPO → 蒸馏出开源学生)
                            │                              │
                            │                              ▼
                            └──▶ Phase 3 (开源学生 + GRPO 前沿) ◀── 蒸馏出的学生
                                     ▲
                    D0 学习 track ────┘  (可现在就并行开始，是技能储备，不阻塞 P0)
```

**硬依赖**：`P0 → P1`；`P0(含 A2 数据管线 + A5 飞轮) → P2`；`P2.C3 蒸馏出学生 → P3 生产落地`。
**可并行**：`P3.D0/D1`（学 GRPO、跑图像复现）是**纯技能储备**，现在就能和 P0/P1 并行——而且它的 reward 函数**就是** P0 的判分器（复利点，见文末）。

---

## Phase 0 · Eval 地基〔唯一 GATE，先做完这个〕
> 详见 [eval 文档 Part A](eval-system-and-layer0-plan.md)。**κ≥0.6 未达成前不碰 Phase 1/2/3 生产。**

- [ ] **A0** probe-findings 21 轮 → `evals/seed_probe.jsonl`（每条 query/期望形态/判据/维度/根因）
  - **验收**：~21–40 条，每条可程序读取、维度标签齐（源：[probe-findings-upgrade-plan.md](design/probe-findings-upgrade-plan.md)）
- [ ] **A1** 可验证判分器 `evals/scorers.py`：时间戳 IoU、recall@k、toolseq_match、refusal_ok、cost
  - **验收**：各配单测，**零 judge 依赖**，确定性
- [ ] **A2** 离线 harness `evals/runner.py`：接 `run_loop`（`pipeline/loop_driver.py:133`）seam + `ScriptedConv`/`make_exec`
  - **验收**：单条 <1s，输出无偏 `pass^{1,3,5}`（`C(c,k)/C(n,k)`，不是 `(c/n)^k`）+ per_dim
- [ ] **A3** 跨家族 judge（**Claude 判 Gemini**，避自偏好）+ ~200 条人标校准集，算 Cohen's κ
  - **验收**：**κ≥0.6 的维度才标为「可门禁」**；κ 0.4–0.6 仅上仪表盘
- [ ] **A4** CI 回归门 `.github/workflows/eval-gate.yml`：pass^3 delta + pinned 硬门 + 配对检验
  - **验收**：分数回退 / 任一 pinned case pass→fail → **阻断合并**；门跑**实际发布分支**（非 stale main）
- [ ] **A5** 失败飞轮：BigQuery `usage_audit` 挖 failure → 最小化 → `evals/pinned/*.jsonl`
  - **验收**：skydive「说没有」等已知 bug 已入 pinned，走硬门

**Phase 0 出口标准**：至少 3 个可门禁维度 κ≥0.6；CI 门在真发布分支上生效；≥50 条评测任务 + ≥5 条 pinned。**达成后才解锁下面所有 Phase。**

---

## Phase 1 · 零权重自改进〔本周即可上，直接改现在的 Gemini〕
> 详见 [eval 文档 Part B](eval-system-and-layer0-plan.md)。三者组合不竞争，**全部用 Phase 0 当度量衡**。推理成本：GEPA≈免费 < memory（每轮一次 embedding）< BoN（乘推理成本，门得最狠）。

- [ ] **B1** GEPA / DSPy（反射式 prompt 演化）— 对 answer-synthesis / 工具选择 prompt
  - 做法：DSPy `dspy.GEPA` + LiteLLM `vertex_ai/gemini-2.5-*` 后端，reflection LM 用 2.5-pro；metric 的 `feedback` 喂 A1 判分结果
  - **验收**：在 A3 没见过的 test split 上门禁提升（防过拟合）；产出人类可读可手改的 prompt
- [ ] **B2** Memory-of-lessons（ReasoningBank 化 `pipeline/lessons.py` + 复用 pgvector）
  - 做法：session 后自判 success/failure → 蒸馏 ≤3 条 lesson（含**失败通道**的预防性 lesson）→ 嵌入 → top-k 检索 + relevance-gating
  - **验收**：A/B on frozen suite（memory ON vs OFF）成功率↑步数↓；投毒 canary 集无漂移
- [ ] **B3** Best-of-N + 证据接地 GenRM verifier（**仅 final-answer 轮**）
  - 做法：adaptive-N（默认 N=1，低置信才升级）；verifier 必须引证据（video_facts/transcript）；veto 硬 fail 幻觉/错时间戳
  - **验收**：held-out 扫 N 找准确率峰值并 cap（accuracy-vs-N 是驼峰，防 reward hacking）

**顺序**：B1 → B2 → B3（杠杆递减、成本递增）。

---

## Phase 2 · 托管调优 + 蒸馏〔中期，需数据管线，仍零 GPU / 零权重〕
> 依据首份研究报告的「中期」层。**前提：Phase 0 的 A2 数据管线 + A5 飞轮已能把生产 traces 变成打标数据集。**

- [ ] **C1** Vertex 对 Gemini 的 **SFT（拒绝采样微调 / RFT）**
  - 做法：A2/A5 挖出的**成功** trajectory（judge + 完成信号过滤）→ 序列化多轮/tool 调用为 Vertex contents JSONL → gemini-2.5-flash 监督调优
  - **验收**：新 checkpoint 过 Phase 0 CI 门（pass^3 不退 + pinned 全绿）；比裸 base 有可门禁提升。**单点提升最大的一步。**
- [ ] **C2** Vertex 对 Gemini 的 **DPO 偏好调优**（仅 2.5 Flash/Flash-Lite）
  - 做法：regenerate/edit/thumbs/judge A-vs-B → (prompt, preferred, rejected) 三元组 → **在 C1 的 SFT checkpoint 之上**跑 DPO（官方建议 SFT-then-DPO）
  - **验收**：质量/工具/诚实度维度提升；注意 DPO 是文本路径，调不了原始视频接地
- [ ] **C3** **Gemini → 开源 video 学生蒸馏**（降本线，也是 Phase 3 的入口）
  - 做法：把已付费的 `analyze_video` 输出（~60k tok/次）日志化当**免费 teacher 标注** → SFT 一个小开源 video 模型（Qwen2.5-VL）扛高频低延迟回合，硬 case 上抛 Gemini
  - **验收**：学生在高频窄任务上达标、成本显著下降；**产出一个你完全掌控权重、可做 RL 的学生 → 解锁 Phase 3**

---

## Phase 3 · 开源学生 + GRPO 前沿〔重投入；D0/D1 可现在并行学〕
> 详见 [GRPO 指南](grpo-qwenvl-tutorial.md)。**诚实预期**：造的是**某条可验证窄轴**（计数/bbox 接地/时序 grounding）上打得过同尺寸开源基线的**专用工具/离线标注器**，作为 VS 的工具节点——**不是**替换 Gemini 大脑。

- [ ] **D0** 学 GRPO 原理 + 跑文本 demo〔**现在就能并行开始，纯技能储备**〕
  - 读 DeepSeekMath / R1 / Dr.GRPO(2503.20783) / Spurious Rewards(2506.10947) → TRL `GRPOTrainer` 跑 GSM8K，看健康 reward 曲线
  - **验收**：能用 group-relative advantage 数值例子给别人讲清；跑绿一条文本 reward 曲线
- [ ] **D1** 图像 GRPO 复现〔可并行〕
  - Qwen2.5-VL-3B + LoRA 复现 R1-V 计数（单卡 24GB，~$3，48%→82.5%）
  - **验收**：该任务两位数跳跃；亲历 VRAM/图像分辨率杠杆
- [ ] **D2** 视频/时序专家（生产候选）〔依赖 P2.C3 提供的学生基座 + 数据〕
  - 复现 Time-R1（tIoU 奖励）/ VideoChat-R1；**核心纪律**：连续 IoU 当 reward，不在时间戳 token 上 SFT；跑**伪奖励对照**（§2.3）验证收益真实
  - **验收**：时序 grounding 指标（R@1@{0.5,0.7}+mIoU）显著超 base，且 pass@k 不塌（非纯先验放大）
- [ ] **D3** 接回 VS 当工具节点〔依赖 P0 + D2〕
  - 用 GRPO 指南 §8.2：专家输出流过 `evals/runner.py`，**reward 函数换成 P0 的 `evals/scorers.py`（同源指标）** + pass^k + 配对检验 + 新失败回流 pinned
  - **验收**：在 VS 统一度量衡下证明是**净进步**才接入生产

---

## 现在做什么（Phase 0 前三步）

1. **A0** — probe 21 轮抽成 `evals/seed_probe.jsonl`
2. **A1** — 写 `evals/scorers.py`（IoU / recall@k / toolseq_match / refusal_ok / cost）+ 单测，零 judge
3. **A2** — 接 `run_loop` 建 `evals/runner.py`，输出无偏 `pass^{1,3,5}`

> 这三步产出物**同时是** Phase 1（GEPA 的 metric、memory 的 A/B、BoN 的验证）**和** Phase 3（D3 的判分器）的地基——先把它们做对，后面全都省力。

## 复利点：一份 verifier，两处用

GRPO 指南 §8.2 与 eval 文档 A1 指向**同一份代码**：
- 你在 Phase 3 为 Qwen-VL 学生写的 `tIoU / accuracy / toolseq` **reward 函数**
- 就是 Phase 0 A1 的 **`eval_scorers.py` 可验证判分器**

两边共享度量衡 → 学 GRPO（D0/D1）**顺手把 Phase 0 的判分器也想清楚了**。这是这条 roadmap 最省力的杠杆：**先建可验证的 verifier，它同时喂饱评测系统、自改进、和未来的 RL。**

---

## 红线（全程适用）
| 红线 | 说明 |
|---|---|
| **无 eval 勿微调** | A 未达 κ≥0.6 前不碰 B/C/D 生产 |
| **judge 用非-Gemini** | Claude 判 Gemini，pairwise 必 swap，改 judge/rubric 必重测 κ |
| **reward hacking** | BoN 扫 held-out 找峰值并 cap；GRPO 加长度/数量惩罚、跑伪奖励对照 |
| **CI 门跑对分支** | 手动部署 + main 落后 live，门必须跑实际发布分支 |
| **诚实预期** | Qwen-VL 学生只赢窄可验证轴，不冒充「让 VS 整体变好」——必须过 pass^k + 配对检验 |
| **成本可见** | 一次 analyze_video ≈ 60k tok/$0.018 主导成本，每任务记 #analyze_video 调用 |
