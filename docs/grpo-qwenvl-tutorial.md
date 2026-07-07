# 动手学 GRPO 微调 Qwen-VL：从原理讲透到最小可跑配方

> 面向：一个能独立搭闭源-Gemini 多轮多模态 video agent（下称 **VS**）的工程师，想**真正搞懂** GRPO（Group Relative Policy Optimization，组相对策略优化），并最终为 VS 造一个开源的**窄轴专家 / 工具模型**（Layer-2 路线）。
>
> **一句话诚实预期**：你**不是**要用一个开源 7B 去替换 Gemini 大脑。GRPO / RLVR 需要**开放权重**——这正是它能用在 Qwen-VL、却**用不到**你的闭源 Gemini 上的根本原因。你要造的是一个**在某条可验证窄轴上（计数 / bbox 接地 / 时序 grounding）打得过同尺寸开源基线**的专用小模型，偶尔在**某一个**可验证 benchmark 上擦过闭源模型——**绝不是**在通用能力上赢 Gemini / GPT-4o。把这条预期钉死，后面每个决策才不会跑偏。

---

## 0. 这份指南教什么

### 0.1 你将学到

| 章节 | 你会得到 |
|---|---|
| §1 | GRPO 原理讲透：为什么去掉 critic、group-relative advantage 怎么算（公式 + 数值小例子）、目标函数 + KL、为什么天然配 RLVR |
| §2 | GRPO 的坑与后继：长度/难度偏置、Dr.GRPO / DAPO / GSPO 各修什么、Spurious Rewards 警示 |
| §3 | 为什么/怎么迁到 Qwen-VL：R1→VLM→video 的谱系、底座选型、能提升/不能提升什么 |
| §4 | 奖励函数设计：accuracy / format / 时序 IoU，确定性 + 防 reward hacking，代码骨架 |
| §5 | 框架与环境：TRL / EasyR1 / verl / ms-swift 选型（按核实结果写）、版本坑 |
| §6 | 算力现实：3B vs 7B、LoRA vs 全参、单卡可行性的真实数字 |
| §7 | **一个最小可跑配方**（重点）：装环境 → 造几百条可验证小数据集 → 写 reward → 训练 config → 启动命令 → reward 曲线 sanity check |
| §8 | 评估微调效果 + **如何接回 VS 的 eval 系统**当统一度量衡 |
| §9 | 循序渐进的学习路径 |
| §10 | 参考清单 |

### 0.2 前置知识

- 会用 PyTorch + HuggingFace `transformers` 跑过一次推理/SFT。
- 对**策略梯度 / PPO（Proximal Policy Optimization，近端策略优化）**有模糊印象即可，§1 会补关键部分。
- 有至少一张 24GB 显卡（3090/4090/A5000）能做入门；7B 全参需要多卡（§6 给真实数字）。
- 会读 LaTeX 公式、会看 YAML config。

### 0.3 为什么这条路对 VS 有意义

VS 的大脑是闭源 Gemini，**没有权重就不能做 RL**。但 VS 里有一堆**可验证的窄任务**——时间戳定位、bbox 接地、工具参数正确性——这些恰好是 GRPO+RLVR 最擅长的。你可以把其中一条蒸馏成一个开源 Qwen-VL 小专家，作为 VS 的**工具节点**或**离线标注器 / verifier**（Layer-2）。学 GRPO 的副产品，还能让你在 §8 把它的评估直接接回 VS 的统一 eval 系统（`docs/eval-system-and-layer0-plan.md`）。

---

## 1. GRPO 原理（讲透）

### 1.1 先建直觉：策略梯度要一个「基线」

RL 微调 LLM 的核心目标：**让能拿到高 reward 的输出，出现概率更高**。最朴素的策略梯度是

$$\nabla_\theta J = \mathbb{E}\big[\, R(o)\cdot \nabla_\theta \log \pi_\theta(o\mid q)\,\big]$$

问题：$R(o)$ 的**绝对值**方差极大。如果所有答案 reward 都在 8~10 之间，你其实只想知道「这条比平均**好还是差**」，而不是「它有多大的绝对分」。于是引入**基线（baseline）** $b$，用**优势（advantage）** $A = R - b$ 替换 $R$。只要 $b$ 不依赖动作，它不改变梯度期望，却大幅降方差。**基线怎么来，是 PPO 和 GRPO 的根本分歧。**

### 1.2 PPO：四个模型，贵在 critic

PPO 用一个**学出来的价值网络（value network / critic）** $V(s)$ 当基线：$A = R - V(s)$。代价是 PPO 训练时显存里同时住着**四个模型**：

| 模型 | 作用 | 代价 |
|---|---|---|
| Policy（actor） | 被训练的策略 | 必需 |
| Reference（ref） | 冻结的初始策略，算 KL 用 | 一份冻结权重 |
| Reward model | 打分 | 一份权重 + 前向 |
| **Critic / value** | 预测期望回报当基线 | **通常和 policy 同尺寸的一整份权重 + 前向 + 反向** |

那个 critic 是最贵的：一整份和 policy 同尺寸的权重，外加完整的前向+反向，而且在 LLM 这种**只有终端稀疏 reward**的场景里很难训好。

### 1.3 GRPO 的关键一招：用「一组样本的均值」当基线

GRPO 把学出来的 $V(s)$ 换成**经验基线**——直接从一组采样里算：

> 对每个 prompt $q$，从旧策略采 $G$ 个输出 $\{o_1,\dots,o_G\}$，各自打分得 $R_1,\dots,R_G$，然后**在组内归一化**：
>
> $$A_i = \frac{R_i - \operatorname{mean}(R)}{\operatorname{std}(R)}$$

这就是 **group-relative advantage（组相对优势）**：一个白化（whitened）、无量纲的优势——比组内平均好就是正，差就是负。**同一条回答 $o_i$ 的每个 token 共享这同一个标量 $A_i$**（outcome supervision，结果监督，不是过程监督）。

**从来不学价值网络，组均值就是基线。** 于是显存里只剩 policy + reference（+ 生成引擎），大约是 PPO 的一半。而且生成本来就要做（你反正要 $G$ 个样本），基线是「顺手」算出来的。

### 1.4 一个数值小例子

设 $G=4$，某 prompt 采到 4 条回答，reward 分别 $R=[1, 1, 0, 0]$（两对两错）：

- $\operatorname{mean}(R)=0.5$，$\operatorname{std}(R)=0.5$
- $A = \frac{[1,1,0,0]-0.5}{0.5} = [+1, +1, -1, -1]$

于是两条对的回答，其**每个 token** 的梯度都乘 $+1$（提升概率）；两条错的乘 $-1$（压低概率）。注意：

- 若 4 条**全对** $R=[1,1,1,1]$：$\operatorname{std}=0$，优势全为 0（或除零）→ **零梯度，白采**。全错同理。→ 这解释了 §2 和 §7 说的「过滤掉全对/全错样本」和 §3 的难度过滤。
- 若 $R=[1,1,1,0]$：$\operatorname{mean}=0.75,\ \operatorname{std}\approx0.43$，$A\approx[+0.58,+0.58,+0.58,-1.73]$——那条唯一的错答被狠压。

**记住这个例子**：GRPO 全部的信号都来自「组内有没有 reward 差异」。没有差异 = 没有学习信号。

### 1.5 完整目标函数 + KL

GRPO 沿用 PPO 的 clipped surrogate（截断代理目标），逐 token：

$$
J = \mathbb{E}\Bigg[\frac{1}{G}\sum_{i=1}^{G}\frac{1}{|o_i|}\sum_{t=1}^{|o_i|}
\min\!\Big(\rho_{i,t}\,A_i,\ \operatorname{clip}(\rho_{i,t},1-\varepsilon,1+\varepsilon)\,A_i\Big)
\;-\;\beta\, D_{\mathrm{KL}}(\pi_\theta\,\|\,\pi_{\mathrm{ref}})\Bigg]
$$

其中 $\rho_{i,t}=\dfrac{\pi_\theta(o_{i,t}\mid q,o_{i,<t})}{\pi_{\theta_{\text{old}}}(o_{i,t}\mid q,o_{i,<t})}$ 是**逐 token 重要性比（importance ratio）**。三个承重件：

1. **clip 项**：标准 PPO 信赖域截断，阻止单步把比值推得太远（防训练炸）。
2. **KL 惩罚项**：这里用 Schulman 的 **k3 无偏低方差估计器** $D_{\mathrm{KL}}\approx \frac{\pi_{\mathrm{ref}}}{\pi_\theta}-\log\frac{\pi_{\mathrm{ref}}}{\pi_\theta}-1$（恒 $\ge 0$、低方差、逐 token 从采样序列算，不用全词表求和）。注意它是**损失里的逐 token 惩罚**，不像经典 RLHF-PPO 把 KL 烘进 reward。
3. **$\frac{1}{|o_i|}$ 和外层结构**：§2 会讲这两个归一化项其实**藏着偏置**。

> **重要（TRL 默认变了）**：TRL 现在 `beta` **默认 0.0**——即**不加 KL、也不加载 reference 模型**，省显存、对齐 DAPO 风格 recipe。如果你**想要** KL 正则，必须显式设 `beta`，那时才会加载 ref 模型。从已对齐模型出发做 RL 时，建议保留一个小 KL（见 §2 坑）。

**为什么便宜又稳**：便宜——删了 critic（≈2 个模型而非 4 个），生成摊到 $G$ 个你本来就要采的样本上；稳——组基线**构造上无偏**，reward 白化让不同难度 prompt 的优势尺度大致恒定。

### 1.6 一步 GRPO 的心智模型

1. 取一批 prompt；
2. 每个 prompt 采 $G$ 个 completion（$G=8$ 是能算稳 mean/std 的**实用下限**，难数学常用 16–64）；
3. 用 reward 函数给全部 $G$ 个打分；
4. 组内 $A_i=(R_i-\operatorname{mean})/\operatorname{std}$；
5. 算 clipped 逐 token surrogate + $\beta\cdot$k3-KL；
6. 几步梯度，然后刷新 $\pi_{\text{old}}$。

在 HuggingFace TRL 里就是 `GRPOTrainer + GRPOConfig`。**生成（不是反向传播）才是瓶颈**——把大部分 GPU 小时预算花在那里，用 `use_vllm=True` 把生成 offload 给 vLLM 几乎是必须的。

### 1.7 为什么天然配 RLVR（可验证奖励）

**RLVR = Reinforcement Learning with Verifiable Rewards**：reward 是一段**确定性程序**，不是学出来的神经奖励模型（RM）。数学题就抽 `\boxed{}` 里的答案做符号/字符串比对（对=1，错=0，accuracy reward）；再加一个 format reward 奖励输出规定结构（如 `<think>…</think><answer>…</answer>`，DeepSeek-R1 的方案）。

reward 是输出的纯函数，于是它：

- **无法被 reward-hack**（没有可利用的学习代理，检查是精确的）；
- **免费评估、永不过时**（没有 RM 要随策略漂移重训）；
- **和你真正想要的东西完美对齐**（一个正确答案）。

这正是 GRPO 想要的信号：GRPO 的组基线只需要每个样本一个标量 reward，且 reward 越便宜越好——因为你**每步给每个 prompt 打 $G$ 个分**。学出来的 RM 会让这 $G\times$ 打分变贵、还会漂移/被 hack；规则检查是 O(微秒)。

DeepSeekMath → DeepSeek-R1(-Zero) 是经典演示：GRPO + 纯规则 accuracy+format reward，从**完全没做 SFT** 的 base（R1-Zero）出发，就足以让长链思维（long CoT）、自我验证、著名的「aha moment」从 RL 中**自发涌现**。DeepSeek 团队明确报告：规则奖励比神经 RM 更有效，正因为 RM 招致 reward hacking 且增加训练成本。

---

## 2. GRPO 的坑与后继：Dr.GRPO / DAPO / GSPO

### 2.1 vanilla GRPO 有两个「刻进公式」的偏置

论文 *Understanding R1-Zero-Like Training*（Liu et al., arXiv 2503.20783）证明了两个偏置：

**(1) 长度 / 冗长偏置**——来自 $\frac{1}{|o_i|}$ 逐回答长度归一化。对一条**错答**（$A_i<0$），把「逐 token 损失之和」除以 $|o_i|$，意味着**长错答**里每个 token 的惩罚幅度**小于**短错答里每个 token。梯度下降会利用这点：**把错答写得更长来稀释惩罚** → 失败样本上的回答长度失控膨胀。

**(2) 难度偏置**——来自 $/\operatorname{std}(R)$。组 reward 几乎全 1（太易）或全 0（太难）的 prompt，std 极小，除以它会**放大**这些 prompt 的优势、在更新里过度加权；而中等难度 prompt（真正有信息量的那些，std 大）被降权。

### 2.2 三个标准修法：各修什么、何时用

| 变体 | 修什么 | 关键改动 | 何时用 |
|---|---|---|---|
| **Dr.GRPO** | 长度+难度偏置 | **删掉两个归一化项**：优势变 $\tilde A_i=R_i-\operatorname{mean}(R)$（无 $/\operatorname{std}$）；损失用常数归一化而非 $1/|o_i|$ | **诚实的基线修法，几乎总该用**——恢复无偏 MC 策略梯度，杀掉错答长度失控，等精度下 token 效率更高 |
| **DAPO** | 长 CoT 规模化、熵坍缩 | 4 个独立 trick（下详）+ **丢掉 KL** | 扩到长 CoT 稠密大模型、看到熵坍缩/长度爆炸时 |
| **GSPO** | 长序列 token 比噪声、**MoE 稳定性** | 重要性比从 token 级改到**序列级** | 训 **MoE 模型**，或长序列 token 比噪声导致不稳时 |

**DAPO 的 4 个 trick**（arXiv 2503.14476, ByteDance）：

1. **Clip-Higher**：把截断上下界解耦，$\varepsilon_{\text{low}}=0.2,\ \varepsilon_{\text{high}}=0.28$。非对称给低概率「探索」token 生长空间（对称 $\varepsilon=0.2$ 时一个 prob 0.01 的 token 最多只能涨到 0.012），防熵坍缩。
2. **Dynamic Sampling（动态采样）**：丢掉 $G$ 个样本**全对或全错**的 prompt（acc 0 或 1 → 零优势 → 零梯度，白算），持续过采直到 batch 填满 $0<\text{acc}<G$ 的 prompt。（正是 §1.4 数值例子说的情况）
3. **Token-Level Loss（token 级损失）**：按整 batch **总 token 数**归一化，而不是「先按序列平均再跨序列平均」，让长回答按比例贡献。
4. **Overlong Reward Shaping**：mask/软惩罚被**截断的超长样本**，别把一个「有效但被切掉」的 CoT 判成错。

DAPO 还**整个丢掉 KL**（从零做 reasoning RL 时你**希望**策略大幅偏离 reference）。结果：Qwen2.5-32B 上 AIME24 拿 50 分，半数步数超过 R1-Zero-Qwen-32B（47）。

**GSPO 的一招**（Zheng et al., Qwen 团队, arXiv 2507.18071）：把重要性比从逐 token 换成**序列级、长度归一化**的 $s_i=\big(\pi_\theta(o_i\mid q)/\pi_{\text{old}}(o_i\mid q)\big)^{1/|o_i|}$（逐 token 比的几何平均），在序列级 clip/优化。这让**优化单位与 reward 单位对齐**（都是序列级）。最大收益是 **MoE 稳定性**：token 级比下，MoE 专家路由波动剧烈（Qwen3-30B-A3B 上每次梯度更新约 10% 激活专家会变），token 比乱飘导致 RL 不收敛（GRPO 得靠「Routing Replay」hack）；GSPO 只依赖整序列似然，对单个路由翻转不敏感，MoE RL **无需 Routing Replay** 就收敛——这被认为是 Qwen3 RL 的功臣。

**速记**：Dr.GRPO = 诚实基线修法（总用）；DAPO = 长 CoT 稠密模型的扩容工具箱；GSPO = MoE / 长序列稳定性的答案。三者大体**正交、可组合**。

### 2.3 Spurious Rewards：Qwen 上的收益有一大块是「先验放大」不是「学习」

这是对整件事的诚实检查（Shao/Wang et al., arXiv 2506.10947）。惊人结果：在 **Qwen2.5-Math-7B** 上，GRPO 用**随机奖励**（和正确性零相关）仍把 MATH-500 提了约 **+21.4** 分，逼近真实 ground-truth 奖励的 **+29.1**。奖励错误答案、甚至只奖励格式，都能给 Qwen 大涨。

**机制**：GRPO 的 clip 偏置。clip 项配合采样，会**系统性放大模型预训练里已有的高概率行为**，几乎与奖励是否有信息无关。对 Qwen2.5-Math，被放大的主导先验是「code reasoning」（用类 Python 风格推理但不真执行代码），其频率在 RLVR（哪怕是伪奖励 RLVR）下从 ~65% 跳到 >90%，而这种风格恰好和这些数学 benchmark 的正确率相关。

**关键外部效度问题**：这是**基座相关**的。同样的伪奖励在 **Llama3.1-8B / OLMo2-7B 上几乎不涨**——它们没有 Qwen 的 code-reasoning 先验。因为 Qwen 成了事实上的 RLVR 测试床，很多「我的新奖励/新 trick +X 分」可能只是「我放大了 Qwen 的预训练先验」，而非可迁移的方法。

**不自欺的实操协议**：

1. **永远跑一个伪奖励对照**——同 base 上用随机/纯格式奖励训一个一模一样的 GRPO。若你的「真」奖励只勉强超过随机奖励基线，你的收益主要是先验放大，不是学习。
2. **在 ≥2 个基座家族上验证**（如一个 Qwen + 一个 Llama/OLMo）再声称方法可迁移。
3. **报多个 $k$ 的 pass@k / pass@1**——RLVR 锐化通常提升 pass@1 而 pass@k（能力上限）持平，这是「放大已有解」而非「学到新解」的指纹。
4. **看质变**（如 code-reasoning 频率有没有飙升），知道自己是不是骑在先验上。

> **对你的 Qwen-VL 项目意味着什么**：你会在 Qwen2.5-VL 上做实验（§3 说明为什么），所以这条警示直接适用。别把「GRPO 让 Qwen-VL 在某 benchmark 涨了」当成「我的奖励教会了推理」。跑伪奖励对照、报 pass@k、有条件时在第二个基座家族（如 InternVL）上复核——这也和 §8 接回 VS eval 系统的「诚实度量衡」精神一致。

---

## 3. 为什么/怎么用在 Qwen-VL 上

### 3.1 R1 → VLM → video 的迁移谱系

R1 recipe = **规则化可验证奖励 + GRPO**（组相对优势，无价值网络）→ 模型自发出现 `<think>` 推理来提升 reward。迁到 VLM 之所以 work，是因为**很多视觉任务有能便宜判分的确定性 ground truth**：物体计数（exact match）、bbox（IoU）、时序 span（tIoU）、MCQ 答案（exact match）。

| 谱系 | 代表工作 | 任务 / 关键点 |
|---|---|---|
| **图像** | R1-V | 计数（SuperCLEVR），2B>72B OOD，$2.62 复现——**最便宜的第一跑** |
| | VLM-R1 | REC / OVD 接地，IoU 奖励，RL>SFT OOD，修 `odLength` reward-hacking |
| | Vision-R1 / Visionary-R1 | 图像数学 CoT；Visionary-R1 记录并修复 GRPO 的「视觉捷径」（不看图就答） |
| **视频** | Video-R1 | **T-GRPO**：同一问题跑两次（正序帧 vs **打乱帧**），只有正序明显优于乱序才给时序 bonus，逼模型真用时序 |
| | Time-R1 | 时序 grounding 专用，**tIoU 奖励**；核心教训「别在时间戳 token 上 SFT，用连续 IoU 奖励」 |
| | VideoChat-R1 | 多任务感知 RFT（grounding+tracking+QA 联合） |

**视频多一个时序问题**：vanilla GRPO 不逼模型真用时序顺序。T-GRPO 用对比奖励解决（正序 vs 乱序的对比 bonus）——但这**大致把 rollout 成本翻倍**（每题跑两遍），要预算进去。

### 3.2 底座选型：Qwen2.5-VL 起步，Qwen3-VL 进阶

**结论：在 Qwen2.5-VL-3B/7B 上学，为严肃视频/时序工作再毕业到 Qwen3-VL-4B。** 理由是「能抄的 recipe 数量」比「模型原始质量」更重要：

| 底座 | 优点 | 缺点 |
|---|---|---|
| **Qwen2.5-VL** | 最大 recipe 复用（§3.1 每篇论文都用它，你的数字可比）；最便宜起步（3B 单卡 24GB）；每个框架都有它的 GRPO 示例 | 维护线，原生长视频弱，T-RoPE 时序更粗 |
| **Qwen3-VL** | 原生秒级时序 grounding（视频巨大优势）；长上下文（256K→1M）；感知更好；2B–235B MoE | 公开 RL recipe 少；**Instruct vs Thinking** 二选一——Thinking 变体已经会发 `<think>`，会和你的 reward/format 设计打架，模糊「RL 到底加了什么」 |
| **InternVL** | 质量有竞争力，适合**交叉验证**收益不是 Qwen 特有（呼应 §2.3） | 社区 GRPO 胶水最薄 |

**关键选择**：如果你想用 GRPO **教**推理，Qwen3-VL 选 **Instruct** 而非 Thinking（Thinking 已内置 `<think>`，会让 reward 设计与预训练行为冲突）。

### 3.3 GRPO 在多模态上真正能提升什么、不能提升什么

| ✅ GRPO 帮得上（大、可复现） | ❌ GRPO 帮不上（约 +1 分） |
|---|---|
| 物体计数、空间推理（VSI-Bench） | 开放式/不可验证 video QA |
| 指代表达理解 / bbox 接地、开放词表检测 | 通用 chat、世界知识 |
| 时序视频 grounding、物体 tracking | （RFT 这里主要**保留**通用能力，不创造新知识） |
| 视觉数学 CoT（MathVista/Geo3K） | |
| **且 OOD 泛化好过 SFT**（VLM-R1: RL 63.16 vs SFT 54.82 on LISA-Grounding） | |

**诚实的真实数字**（都 vs 同家族 Qwen2.5-VL-7B base，不是 vs Gemini/GPT-4o）：

- **VideoChat-R1**：时序 grounding **+31.8 mIoU**、tracking +31.2，而 VideoMME/MVBench/Perception-Test 各只动 ~+0.9–1.0（「感知飙升 +30、通用保留 +1」的招牌信号）。
- **R1-V**（Qwen2VL-2B）：SuperCLEVR 计数 48.0% → **82.5%**，2B 在 OOD 上 100 步内胜 72B，30 分钟 8×A100 训完 ~$2.62。
- **Video-R1-7B**：VSI-Bench **37.1%**，在**那一个空间 benchmark** 上确实擦过 GPT-4o——这是合法的**窄轴**闭源超越，不是通用胜利。
- **Time-R1**：ActivityNet mIoU 16.3→29.2（RL），而 **SFT 反而把 base 从 16.3 拖到 15.4**——SFT 在时间戳 token 上做，落到 base 之下（§4 的核心教训）。

> **对你首跑的现实预期**：一个 3B–7B GRPO 跑在可验证任务（计数/接地）上，应能看到**该任务上清晰的两位数跳跃**，便宜（单卡/8 卡、数小时）。**别指望**通用超越 GPT-4o/Gemini，或开放式 QA 大涨。

---

## 4. 奖励函数设计（多模态/视频）

### 4.1 原则

**reward = format_reward + task_reward，两者都用纯代码（regex + 算术）算，绝不用 LLM judge**——于是确定性、快、无法被成本 hack。三类 task_reward 覆盖几乎一切：

| 类 | 用于 | 判法 |
|---|---|---|
| **(A) Accuracy** | 离散答案 | exact 字符串/数字匹配、MCQ 字母匹配、计数 exact-int（R1-V） |
| **(B) IoU / tIoU** | 任何带 span | 空间 bbox IoU、时序 tIoU（预测 `[t_start,t_end]` vs 金标） |
| **(C) Format** | 结构 | 正确包裹 `<think>…</think><answer>…</answer>` 的小 binary/graded bonus |

### 4.2 核心视频教训：连续 IoU 当 reward，而不是在时间戳 token 上 SFT

时序 grounding 是**回归问题**。如果你 SFT 让模型发时间戳 token，会得到脆弱的 token 匹配——GT `[2s,4s]` vs 预测 `[1.9s,3.9s]`，自回归损失照样很高，狠罚一个几乎对的预测。**改成让模型输出数字、用连续 tIoU = 交/并当奖励**，GRPO 就直接优化你真正在意的指标——可复现地打过 token 级 SFT（Time-R1 证实：加时间戳感知项还额外 +4–6% R1@m）。

> **这条已被对抗验证过**（覆盖研究结论）：Time-R1（base Qwen2.5-VL-7B）在 ActivityNet mIoU 上 RL(GRPO+tIoU) 16.3→29.2，而 SFT 反把 base 拖到 15.4。注意这是「RL 范式 vs SFT 范式」的整体对比（RL 臂还加了 CoT + 全参微调），SFT 的坍缩部分是通用模型的灾难性遗忘——但**「连续奖励优于时间戳 token SFT」的方向性教训稳健成立**。

### 4.3 tIoU 奖励代码骨架（承重件）

```python
import re

def tiou(pred, gt):  # pred/gt = (start, end)，单位：秒
    inter = max(0.0, min(pred[1], gt[1]) - max(pred[0], gt[0]))
    union = (pred[1] - pred[0]) + (gt[1] - gt[0]) - inter
    return inter / union if union > 0 else 0.0

def parse_interval(text):
    m = re.search(r'<answer>\s*([0-9.]+)\s*(?:to|,|-)\s*([0-9.]+)\s*</answer>', text)
    return (float(m.group(1)), float(m.group(2))) if m else None

def reward(text, gt_span, duration=None):
    # 格式分：只给小权重（防止 tag 完美但答案全错的 reward hacking）
    r_fmt = 1.0 if re.search(r'<think>.*?</think>\s*<answer>.*?</answer>', text, re.S) else 0.0
    pred = parse_interval(text)
    if pred is None:
        return 0.1 * r_fmt                       # 解析失败 → 只给一点点格式分，不崩
    ps, pe = sorted(pred)                        # 模型有时把 start/end 写反
    r_tiou = tiou((ps, pe), gt_span)
    # Time-R1 时间戳感知变体（可选，+4~6%）：
    if duration:
        dev = max(0.0, 1 - abs(ps - gt_span[0]) / duration) \
            * max(0.0, 1 - abs(pe - gt_span[1]) / duration)
        r_tiou *= dev
    return 0.1 * r_fmt + 1.0 * r_tiou            # 连续、稠密、便宜
```

**换任务只需换中间那行**：accuracy 任务换 exact-match；bbox 换 2D box IoU；MCQ 抽字母比对。所有 reward 归一到可比尺度（task 在 `[0,1]`，format 权重 ~0.1），组相对优势才好使。

### 4.4 防 reward hacking（几条铁律）

- **Reward hacking 是真的**：VLM-R1 的原始 mAP 奖励让模型狂刷 box → 他们加了 `odLength` 惩罚。**任何「预测越多 reward 越高」的梯度都要加长度/数量惩罚。**
- **别让 format 权重压过 accuracy**：若 format ≈ accuracy 权重，模型学会永远发完美 `<think>` 标签配错答案。保持 format ~0.1× task。
- **解析失败给低 reward，不要 crash**：返回 ~0（或一点格式分），让组里还有可用的优势基线。
- **时间戳统一单位**（秒 vs 归一化 `[0,1]`），在 prompt / GT / reward 三处一致，否则 tIoU 静默算垃圾。
- **T-GRPO 的时序 bonus 是第三项**，gated 在「正序 > 乱序」，很容易配错权重淹没 accuracy 信号——起步设小。

---

## 5. 框架与环境

### 5.1 四个框架，按规模递增

| 框架 | VLM/Qwen2.5-VL GRPO 真实支持情况 | 定位 |
|---|---|---|
| **TRL GRPOTrainer** | **图像：mainline 一级支持**（官方文档有 VLM 章节，明确列出 `Qwen2.5-VL-3B-Instruct` 已测；reward 是普通 Python callable；真 vLLM colocate/server 后端）。**视频：不支持**——issue #4144 请求加 Qwen2.5-VL 视频，被 maintainer 关为「not planned」，视频要自己 fork 打补丁 | **学 GRPO 首选**，概念面最小、reward 好调 |
| **EasyR1**（hiyouga，verl fork）| **确认**：专为 VLM RL，原生 Qwen2/2.5/3-VL，GRPO/DAPO/GSPO/CISPO/RLOO 等，Ray 多机，**发布明确 VRAM 表**，有 geometry3k 示例；Qwen2.5-VL 是 battle-tested 路径，Qwen3-VL 支持但有未解 issue（#527/#580） | **扩到 7B–72B VLM 的甜点** |
| **verl**（HybridFlow）| EasyR1 包的引擎，actor/rollout/ref 解耦，FSDP/Megatron + vLLM/SGLang，最可扩展但学习曲线最陡 | 只在 32B/72B 或需 Megatron 并行时用 |
| **ms-swift**（ModelScope）| 原生 Qwen 生态，GRPO+DAPO/GSPO/CISPO/RLOO，CLI 优先（`swift rlhf --rlhf_type grpo`），`swift rollout` vLLM 自动权重同步，Nov 2025 起有 Megatron-GRPO | 想要 Qwen 原生 CLï、少样板时的等价首选 |

**给学习者的推荐决策**：

> **学算法（3B，单卡）→ TRL 或 ms-swift**。TRL `examples/scripts/grpo_vlm.py` 概念面最小、Python reward 可调、vLLM colocate 单卡能塞。想要 Qwen 原生 CLI 少样板 → ms-swift。
>
> **扩到 7B–72B VLM → EasyR1**（VLM 管线替你做好，有 VRAM 表能 size 你的跑）。
>
> **只在 32B/72B 或要 Megatron 并行 → 裸 verl**。

### 5.2 环境版本坑（真实雷区）

整个栈是版本兼容雷区，因为三个快速迭代件必须彼此同意：**trainer（trl/verl/swift）↔ vLLM（rollout 引擎）↔ transformers（定义 Qwen2.5-VL 模型类 + processor）**。最大破坏源：**vLLM 升级悄悄要求另一个 torch，然后和你的 FlashAttention wheel 不匹配。**

**mid-2026 能复现的基线**：

```text
Python        3.10 或 3.11
CUDA          >= 12.4（verl 最低 12.1；12.4+ 才有 FlashAttn/vLLM wheel），cuDNN >= 9.8
vLLM          >= 0.8.3（EasyR1 要求；设 env VLLM_USE_V1=1）；新 EasyR1 Docker 带 0.11.0
FlashAttention>= 2.4.3（EasyR1 最低）——装匹配你 torch+CUDA+cxx11abi 的预编译 wheel，别从源码 pip install（30 分钟编译）
transformers  >= 4.54.0（Qwen2.5-VL；Qwen3-VL 需更新）
              ⚠ TRL 路径另外要 transformers >= 5.4.0，避开 5.3.0 的 Qwen2.5-VL batched-unpadded 崩溃
qwen-vl-utils 装上（图像/视频预处理）
```

**强烈建议直接用框架的 Docker 镜像**，别手搓。EasyR1：

```bash
docker pull hiyouga/verl:ngc-th2.8.0-cu12.9-vllm0.11.0
# 这把 torch 2.8 / CUDA 12.9 / vLLM 0.11 一起 pin 死，绕开 90% 的痛
git clone https://github.com/hiyouga/EasyR1.git && cd EasyR1 && pip install -e .
```

**几条保命 gotcha**：

- **别在装完 torch/flash-attn 后再 `pip install vllm`** 而不检查——vLLM 会拽来一个 torch 版本弄坏你的 flash-attn wheel。**先装 vLLM（它 pin torch），再让 flash-attn 匹配那个 torch。**
- Qwen2.5-VL 的 processor 在 transformers 小版本间会变——**精确 pin transformers**，太新的 transformers 会破坏框架的模型注册。
- **vLLM colocate 共享训练 GPU**：从 text-LLM 教程抄来的 `gpu_memory_utilization`（0.8–0.9）会 OOM 一个 24GB 的 VLM 跑——**调低**它留给训练（§6）。

**并行后端**：TRL/ms-swift 靠 DeepSpeed ZeRO（LoRA 用 ZeRO-2，全参/大模型用 ZeRO-3 分片参数）；verl/EasyR1 训练侧用 FSDP（默认）或 Megatron，rollout 用 vLLM，**不用 DeepSpeed 做 actor**。

---

## 6. 算力现实

### 6.1 GRPO 省显存，但新增了 rollout 成本

GRPO 对 PPO 的显存优势：**没有学出来的 critic**。但 GRPO 下你付一个 SFT 世界没有的**新成本**——rollout/生成显存。colocate 下同一 GPU 同时装（a）训练权重+梯度+优化器 **和**（b）为一批 $G$ 个样本生成的 vLLM KV-cache。**对 VLM 尤其致命：图像 token 猛增序列长度**（一张高分辨率 Qwen2.5-VL 图能是几百~几千视觉 token），所以是**prompt 长度（图像分辨率），不只是 batch**，在撑爆 KV-cache。

### 6.2 EasyR1 的公开 VRAM 表（最具体的 Qwen-VL GRPO sizing）

读法 = (GPU 数 × 每卡 VRAM)：

| 方案 | 1.5B | 3B | 7B | 32B | 72B |
|---|---|---|---|---|---|
| **GRPO 全参, AMP** | 2×24GB | 4×40GB | 8×40GB | 16×80GB | 32×80GB |
| **GRPO 全参, BF16** | 1×24GB | 1×40GB | 4×40GB | 8×80GB | 16×80GB |
| **GRPO LoRA, AMP** | 1×12GB | **1×24GB** | 2×32GB | 2×80GB | 4×80GB |

（表内数字为 EasyR1 自标「estimated」，实际随 max prompt/response 长度、rollout 组大小 n、图像分辨率剧烈变化——先在自己数据上验证。）

### 6.3 诚实的最小可行

- **单张 24GB 卡（3090/4090/A5000）+ LoRA + colocate vLLM**：**3B 的现实入门点**（表里 3B LoRA AMP = 1×24GB）。要短 prompt、低图像分辨率、开梯度检查点。
- **单张 40GB（A100-40/A6000-48）**：3B 全参 BF16 舒适。
- **7B**：LoRA 要 2×32GB，全参 BF16 要 4×40GB——**离开单消费卡领地**。注意 Unsloth 报过 7B GRPO+LoRA 在单张 T4（~16GB）上跑，但要把 context 压到很短。
- **7B 全参 / 研究级复现**：论文常用 8×80GB（A100/H100），由**全参 + 长 completion + 大 rollout batch** 驱动，不是 LoRA。

> **纠偏**（覆盖研究）：不要说「7B 一定要多卡 80GB」。分开讲：**7B 全参 GRPO → 多卡 80GB；7B LoRA GRPO → 1–2 卡（24–40GB）可行**。「4–8×A100/H100 80GB」只适用于全参和研究复现。

### 6.4 撑下去的常开杠杆

- `enable_gradient_checkpointing=True` / `gradient_checkpointing=True`——用 ~20-30% 计算换大幅激活显存下降，**VLM GRPO 事实上必开**。
- **降图像分辨率**（Qwen2.5-VL `min/max pixels`）缩视觉 token 数 → 缩 KV-cache。**VLM 里 batch 是红鲱鱼，视觉 token 数才主导 OOM——先降 max_pixels 再降 batch。**
- **colocate 的关键旋钮**：`gpu_memory_utilization` 给 vLLM KV-cache 保留每卡一个**比例**。verl 手册案例：默认 0.6 在 A100 上剩 ~20GB 空、饿死吞吐，升到 0.8 打满 >95% 不 OOM；**但 24GB 紧卡要反着来，降到 0.3–0.45** 给 LoRA 训练态留位。server 模式则把 vLLM 放**单独 GPU**干净拆分。
- `num_generations`/`rollout.n` 保持 4–8（每多一个样本都乘 rollout 显存和时间）。

### 6.5 LoRA vs 全参：学习者默认 LoRA

RL 后训练做的是**相对小、定向的行为改变**（格式、推理纪律、任务奖励），不是教新能力——正是低秩适配器够用的区间。且 GRPO 已删了 PPO 的 critic，LoRA+GRPO 叠两重省显存。

- **LoRA**：+ 3B 单卡 24GB 能塞、迭代快、checkpoint 便宜、不易灾难性遗忘 base 的视觉技能；− 限制 RL 能推多远、rank/target 选择要注意、合并回 vLLM rollout 多一步。
- **全参**：+ reward/行为改变天花板最高；− ~4× 显存、3B(AMP) 都要多卡、更慢、VLM rollout OOM 风险高。

**VLM 特有：adapter 放哪**——LoRA 语言层 + **视觉→文本 projector/merger**，冻结重的视觉 encoder。TRL：`--use_peft --lora_target_modules q_proj v_proj`（VLM 加 projector 模块）。verl/EasyR1：`lora_rank=64 lora_alpha=32`（rank 32–64 是合理起步带）。**Qwen3-VL LoRA 必须 `exclude_modules=.*visual.*`**——vLLM 不支持 ViT LoRA。

> **一个真实坑**：colocate rollout 里 vLLM 必须从**当前**策略生成——框架每步把 LoRA 权重合并/同步进 rollout 引擎；若权重同步配错，你会**静默地从 base 模型 rollout，reward 永远不动**。ms-swift 的 `swift rollout` 主打自动权重同步正是防这个。

---

## 7. 一个最小可跑配方（重点）

下面给**两条**：EasyR1 路径（推荐，可扩，示例现成）和 TRL 路径（单卡学习最省心）。**先跑绿一条 reward 曲线，再定制。**

### 7.A EasyR1 路径（推荐，7B，多卡）

**1) 装环境**（用 Docker 见 §5.2，或）：

```bash
git clone https://github.com/hiyouga/EasyR1.git && cd EasyR1 && pip install -e .
```

**2) 造几百条可验证小数据集**。EasyR1 的 parquet schema（geometry3k 布局）：三列 `images`（图像列表）/`problem`（文本，以 `<image>` 占位符开头）/`answer`（可验证字符串）。用 HF datasets 写：

```python
from datasets import Dataset, Features, Sequence, Image, Value

feats = Features({'images': Sequence(Image()),
                  'problem': Value('string'),
                  'answer':  Value('string')})
# rows = [{'images':[img_bytes_or_path], 'problem':'<image>数一数图里有几个红色方块？', 'answer':'3'}, ...]
ds = Dataset.from_list(rows, features=feats)
ds.to_parquet('train.parquet')   # 同法造 test.parquet
```

> **用 Gemini 当 teacher 造数据（关键纪律）**：teacher 只做**生成和过滤，不做标签**。
> 1. 从**已有 ground truth 的源**起（数学/几何 geometry3k、chartQA 数字答案、docVQA、MCQ；视频用 Charades-STA / ActivityNet-Captions，每 query 有标注 `[start,end]`）。这些白给你可验证的 `answer`。
> 2. Gemini 用来（a）把一条标注数据变成干净自然语言问题；（b）评难度，**丢掉 base 模型 8/8 全对或 0/8 全错的题**——GRPO 只从「组内有 reward 方差」的题学（呼应 §1.4 和 §2.2 动态采样）；（c）验证你的抽取器能解析金标。
> 3. **永远保留原始数据集标签当 `answer`，绝不把 Gemini 的自由文本当 ground truth**（那会把可验证任务变回不可验证）。
> 4. **几百条精选中等难度题就能推动 3B/7B**，不需要几万条。Time-R1 就用 Gaussian 过滤 IoU≈0.3 附近造了 2.5K 条。

**3) reward**：直接用 `examples/reward_function/math.py:compute_score`（数字/几何答案）。它的接口（EasyR1 batch reward，返回每条一个 dict）：

```python
import re
from mathruler.grader import extract_boxed_content, grade_answer

def format_reward(response: str) -> float:
    pattern = re.compile(r'<think>.*</think>.*\\boxed\{.*\}', re.DOTALL)
    return 1.0 if re.fullmatch(pattern, response.strip()) else 0.0

def accuracy_reward(response: str, ground_truth: str) -> float:
    return 1.0 if grade_answer(extract_boxed_content(response), ground_truth) else 0.0

def compute_score(reward_inputs: list[dict], format_weight: float = 0.1) -> list[dict]:
    scores = []
    for x in reward_inputs:                    # x 有 'response' 和 'ground_truth'
        resp = re.sub(r'\s*(<|>|/)\s*', r'\1', x['response'])
        fmt = format_reward(resp); acc = accuracy_reward(resp, x['ground_truth'])
        scores.append({'overall': (1 - format_weight) * acc + format_weight * fmt,
                       'accuracy': acc, 'format': fmt})
    return scores
```

（额外的 `accuracy`/`format` 键会被单独记成曲线——正是 §7.C sanity check 要看的。换视频时把 accuracy 那行换成 §4.3 的 tIoU。）

**4) 训练 config**（`examples/config.yaml`，已核实的关键值）：

```yaml
algorithm:
  adv_estimator: grpo
  kl_coef: 1.0e-2                 # KL 系数 beta（EasyR1 默认保留小 KL）
data:
  prompt_key: problem            # 对应你的列名
  answer_key: answer
  image_key: images
  max_prompt_length: 2048
  max_response_length: 2048      # == max_completion_length
  rollout_batch_size: 512        # 每 RL 步采的 prompt 数
worker:
  rollout:
    n: 5                         # 组大小 G（每 prompt 采几个 completion）
    temperature: 1.0             # rollout 采样温度（验证时 0.6）
    gpu_memory_utilization: 0.6  # 紧卡调低（见 §6.4）
  actor:
    optim:
      lr: 1.0e-6                 # 低学习率——这是在大模型上做 RL
    global_batch_size: 128       # 策略更新 minibatch
  reward:
    reward_function: ./examples/reward_function/math.py:compute_score
    reward_function_kwargs: {format_weight: 0.1}
trainer:
  total_epochs: 15
  n_gpus_per_node: 8             # 卡少就调小，并同步降 batch/rollout
```

**5) 启动**（改 `MODEL_PATH`；自定义数据就改 train/val 文件）：

```bash
MODEL_PATH=Qwen/Qwen2.5-VL-7B-Instruct \
bash examples/qwen2_5_vl_7b_geo3k_grpo.sh
# 底层等价于：
# python3 -m verl.trainer.main config=examples/config.yaml \
#   data.train_files=<你的>/train.parquet \        # ← 填自己的路径，或用 hiyouga/geometry3k@train 先冒烟
#   data.val_files=<你的>/test.parquet \            # ← 填自己的路径
#   worker.actor.model.model_path=$MODEL_PATH \
#   trainer.n_gpus_per_node=8
# 卡少：降 trainer.n_gpus_per_node、rollout_batch_size、global_batch_size，
#      升 worker.rollout.gpu_memory_utilization 或开 worker.actor.offload.offload_params
```

**训练后合并 checkpoint 成 HF 格式**：

```bash
python3 scripts/model_merger.py --local_dir checkpoints/easy_r1/<exp_name>/global_step_<N>/actor
```

### 7.B TRL 路径（fallback，3B，单/双卡）

**1) 装**：`pip install "trl[vllm]"`（**pin `transformers>=5.4.0`** 避开 Qwen2.5-VL batched-unpadded 崩溃）。

**2) 数据**：`images` 列 + 会话式 `prompt` + `solution` 列（reward 从 `**kwargs` 读）。参考数据集 `lmms-lab/multimodal-open-r1-8k-verified`。

**3) reward**（TRL 接口：每个 fn `(completions, **kwargs) -> list[float]`，ground truth 经 kwargs 从数据集列传入）：

```python
import re
from math_verify import LatexExtractionConfig, parse, verify

def format_reward(completions, **kwargs):
    pattern = r'^<think>.*?</think>\s*<answer>.*?</answer>$'
    return [1.0 if re.match(pattern, c, re.DOTALL) else 0.0 for c in completions]

def accuracy_reward(completions, **kwargs):
    solutions = kwargs['solution']                      # 你的数据集 'solution' 列
    contents  = [c[0]['content'] for c in completions]  # 会话式 completion
    rewards = []
    for content, sol in zip(contents, solutions):
        gold = parse(sol, extraction_mode='first_match', extraction_config=[LatexExtractionConfig()])
        pred = parse(content, extraction_mode='first_match', extraction_config=[LatexExtractionConfig()])
        try:    rewards.append(float(verify(pred, gold)) if gold else 1.0)
        except Exception: rewards.append(0.0)
    return rewards
# TRL 会把多个 reward_funcs 相加；要设权重就自己缩放返回值（如 format 乘 0.2）
```

**4) `GRPOConfig` 关键参数**（注意 TRL 默认和 EasyR1 不同）：

```python
from trl import GRPOConfig, GRPOTrainer

args = GRPOConfig(
    num_generations=8,            # 组大小 G（必须整除 effective global batch）
    learning_rate=1e-6,           # blog 示例用 1e-5；VL 上 1e-6 更稳
    beta=0.02,                    # ⚠ KL 系数——TRL 默认 0.0（无 KL），想要就显式设
    max_completion_length=1024,   # ⚠ TRL 默认只有 256——推理任务必须调大
    per_device_train_batch_size=2,
    gradient_accumulation_steps=8,
    use_vllm=True, vllm_mode='colocate',
    gradient_checkpointing=True,
    logging_steps=1)

trainer = GRPOTrainer(model='Qwen/Qwen2.5-VL-3B-Instruct',
                      reward_funcs=[format_reward, accuracy_reward],
                      args=args, train_dataset=train_ds)  # ← train_ds 填自己的
trainer.train()
```

**5) 或直接跑现成脚本**：

```bash
accelerate launch --config_file=examples/accelerate_configs/deepspeed_zero3.yaml \
  examples/scripts/grpo_vlm.py \
  --model_name_or_path Qwen/Qwen2.5-VL-3B-Instruct \
  --output_dir grpo-Qwen2.5-VL-3B --learning_rate 1e-5 --dtype bfloat16 \
  --max_completion_length 1024 --use_vllm --vllm_mode colocate \
  --use_peft --lora_target_modules q_proj v_proj --log_completions
```

### 7.C reward 曲线该长什么样（sanity check）

一个健康的 GRPO 跑：

| 指标 | 健康表现 |
|---|---|
| `reward/mean`、`reward/accuracy` | 前 ~50-100 步内**趋势向上**（不必单调，GRPO 有噪声） |
| `reward/format` | 前几十步内升到接近 **1.0** 并保持（格式很容易学） |
| completion 长度 | 落在一个 band 里（常先随「学会思考」略涨再稳）；每次 rollout 都跑到 `max_completion_length` = 长度 hacking 或 reward 太松 |
| KL to reference | 小且有界；KL 爆炸 = lr 太高或 beta 太低 → 策略漂移/坍缩 |

**红旗**：

- `format→1.0` 但 accuracy 平 = **reward hacking / 抽取器坏了**——手动在 5 个金标上验证 grader 能解析。
- reward 全程死在 0 = 抽取器从不匹配金标，或所有题太难——查数据集方差。
- reward 瞬间打满 = 题太易——没有学习信号。

### 7.D 常见首跑崩溃

- **`num_generations` 必须整除 effective global batch**（`per_device × grad_accum × world_size`），否则 trainer 报错——最常见首跑 crash。
- vLLM colocate OOM → 先降 `gpu_memory_utilization` 或 `per_device_train_batch_size`，再动模型尺寸。
- EasyR1 stock 7B config 要真多卡显存——小机器上开 param offload、缩 batch/rollout。
- **pin transformers/trl/vllm/math_verify 在一起**——Qwen2.5-VL processor + vLLM 版本不匹配是「blog 能跑我崩了」的头号原因。**先让某一组精确版本训绿，再 pin 死。**

---

## 8. 评估微调效果

### 8.1 可验证任务：eval == reward 减去采样

RLVR 的妙处：**eval 就是 reward，只是不采 $G$ 个**。留一个模型没见过的 test split，**确定性解码（greedy / 温度≈0，单样本）**，用**训练时同一个抽取器 + grader** 跑，报 accuracy。base 和 tuned 两个 checkpoint 走**同一 harness**，唯一变量是权重。

```python
from vllm import LLM, SamplingParams
from mathruler.grader import extract_boxed_content, grade_answer

llm = LLM(model=CKPT, dtype='bfloat16', limit_mm_per_prompt={'image': 2})
sp  = SamplingParams(temperature=0.0, max_tokens=2048)   # greedy → 可复现
# 逐条把 <image>+problem 经 Qwen2.5-VL chat 模板喂进去，收集 outputs
def em(pred, gold): return grade_answer(extract_boxed_content(pred), gold)
acc = sum(em(o, g) for o, g in zip(outputs, golds)) / len(golds)
# 视频 grounding 指标：R@0.5 = IoU≥0.5 的比例 + mean IoU（复用 §4.3 的 tiou）
```

**几条铁律**：eval 抽取器**必须**和训练抽取器完全一致（先手动在 5 个金标上验）；base 和 tuned 用**同一 prompt 模板**；grounding 固定单位（秒 vs 归一化）；**别把训练 reward 当 accuracy 报**（它含 format 项，要单独重算纯 accuracy/IoU）。若 format-pass 跳但 EM 几乎不动 = 主要学了格式，回 §4 调 reward 权重/数据难度。

### 8.2 接回 VS 的 eval 系统当统一度量衡（关键）

你造这个 Qwen-VL 专家，最终是要服务 VS。VS 已经有一套**统一评测系统**（`docs/eval-system-and-layer0-plan.md`），它的**可验证轴判分器**和你这里用的**是同一套指标**——这不是巧合，是你该利用的对齐点：

| 你在 §4/§8 用的 | VS eval 系统里的对应（`docs/eval-system-and-layer0-plan.md`） |
|---|---|
| tIoU 奖励 / R@1@{0.5,0.7} + mIoU | A1「时序/时间戳」维度 + A3 表「时间戳 → IoU 纯函数」；`evals/scorers.py` 的 `iou_r1` |
| 工具名 + 参数 exact/JSON-diff | A1「工具选择与调用正确性」；`toolseq_match` |
| 检索 recall@k / MRR | A1「检索正确性」；`recall_at_k` |
| 拒答 vs 编造（不可答集） | A1「诚实/拒答」；`refusal_ok`（skydive「说没有」经典反例） |
| 确定性、纯程序、无 LLM judge | A3「能确定性判分的维度就不付钱给有偏又贵的 judge」的铁律 |

**具体怎么接**：

1. **把你的 Qwen-VL 专家当一个「工具节点」或候选策略**，让它的输出流过 VS 的离线 harness（`evals/runner.py`，接 `run_loop` seam，`pipeline/loop_driver.py:133`）。你的 tIoU/toolseq/refusal 判分器换成 VS 的 `evals/scorers.py` 版本——**指标同源，数字可比**。
2. **用 pass^k 而非 pass@1**：VS eval 强调 agent 非确定，70% 单轮 → pass^3≈34%（`pass^k = E[C(c,k)/C(n,k)]` 无偏估计器，别用 `(c/n)^k`）。你评微调模型时也该报 pass^3 当头条、pass^1 delta 当快反馈。
3. **配对检验防方差假警报**：base vs tuned 用**同任务+同种子**配对（McNemar / paired bootstrap），要求跌/涨幅超过 baseline CI，别用裸阈值。
4. **新失败回流成 pinned case**：你的专家在某类输入上崩了 → 最小化 → 标金标 → commit 成 VS 的 pinned 回归用例（`evals/pinned/*.jsonl`，走硬门）。这样它一旦作为工具接入 VS，同一个 bug 永不悄悄回来。
5. **诚实度量衡**：呼应 §2.3——别让「Qwen-VL 专家在某 benchmark 涨了」冒充「它让 VS 整体变好」。只有走完 VS 的可验证轴 + pass^k + 配对检验，才算真的进步。

> 一句话：**你在 §4 写的奖励函数和你在 VS eval 系统里写的判分器，本该是同一份代码。** 学 GRPO 的这套 verifier 直接就是 VS 第 0 层 A1 的产物——两边共享度量衡，是这条 Layer-2 路线最省力的复利点。

---

## 9. 学习路径（循序渐进）

| 阶段 | 做什么 | 产出 / 里程碑 |
|---|---|---|
| **0. 读原理** | 读 DeepSeekMath（GRPO 出处）+ DeepSeek-R1 报告；读 *Understanding R1-Zero-Like Training*（Dr.GRPO，2503.20783）建立偏置直觉；扫 Spurious Rewards（2506.10947）建立诚实预期 | 能用 §1.4 的数值例子给别人讲清 group-relative advantage |
| **1. 跑文本 GRPO** | TRL `GRPOTrainer` 跑一个 GSM8K/MATH 文本 demo（`beta`、`num_generations`、accuracy+format reward 都亲手配一遍） | 看到 §7.C 那条健康 reward 曲线，理解「生成是瓶颈、用 vLLM」 |
| **2. 图像 GRPO** | 上 **Qwen2.5-VL-3B** + LoRA，复现 **R1-V 计数**（最便宜，$~3，48%→82.5%）或 VLM-R1 REC 接地 | 单卡 24GB 跑出该任务两位数跳跃；亲历 §6 的 VRAM/分辨率杠杆 |
| **3. reward 设计练手** | 换任务写自己的 accuracy/IoU/format reward；故意制造一次 reward hacking（format 权重调高）再修好 | 内化 §4 的防 hacking 铁律 |
| **4. 视频/时序** | 复现 **Time-R1**（tIoU 奖励）或 **VideoChat-R1**（多任务），亲测「连续 IoU >> 时间戳 SFT」 | 做出 §4.2 的核心教训，得到一个可当 VS 工具的时序专家 |
| **5. 扩规模** | 毕业到 EasyR1 + 7B（或 Qwen3-VL-4B 做原生长视频）；跑伪奖励对照（§2.3）验证收益真实 | 有可比 7B 数字，且证明不是纯先验放大 |
| **6. 接回 VS** | 用 §8.2 把专家评估接进 VS 的 `eval_harness` + pass^k + pinned case | Layer-2 专家在 VS 统一度量衡下证明是净进步 |

**动手顺序原则**：**先文本再多模态、先图像再视频、先 LoRA 再全参、先复现别人的数字再改自己的任务。** 每一步都要看到一条绿 reward 曲线再往下走。

---

## 10. 参考清单

**GRPO 原理 / 后继变体**
- DeepSeekMath（GRPO 出处）/ DeepSeek-R1(-Zero) 报告 — 规则奖励 + GRPO，long CoT + aha moment 涌现
- *Understanding R1-Zero-Like Training*（arXiv 2503.20783）— **Dr.GRPO**，长度/难度偏置的证明与最小修法
- **DAPO**（arXiv 2503.14476, ByteDance）— clip-higher / dynamic sampling / token-level loss / overlong shaping，丢 KL
- **GSPO**（arXiv 2507.18071, Qwen）— 序列级重要性比，MoE 稳定性，无需 Routing Replay
- *Spurious Rewards*（arXiv 2506.10947）— Qwen 上伪奖励 +21.4，先验放大警示；github.com/ruixin31/Spurious_Rewards
- Schulman k3 KL estimator（近似梯度 caveat 见 arXiv 2510.01555）

**多模态 / 视频 GRPO**
- **R1-V**（github.com/Deep-Agent/R1-V）— 计数，2B>72B OOD，$2.62，最便宜首跑
- **VLM-R1**（arXiv 2504.07615, github.com/om-ai-lab/VLM-R1）— REC/OVD，IoU 奖励，RL>SFT OOD，odLength 反 hacking
- Vision-R1（2503.06749）/ Visionary-R1（2505.14677）— 图像数学 CoT，修视觉捷径
- **Video-R1**（arXiv 2503.21776, NeurIPS 2025）— T-GRPO 正序 vs 乱序对比时序奖励；VSI-Bench 37.1%>GPT-4o
- **Time-R1**（arXiv 2503.13377, xuboshen.github.io/Time-R1）— tIoU 奖励，「别 SFT 时间戳用连续 IoU」核心教训
- **VideoChat-R1**（arXiv 2504.06958, OpenGVLab）— 多任务 RFT，grounding +31.8 mIoU，通用 +~1
- MUSEG（2505.20715）/ TimeZero — Time-R1 教训的独立佐证

**框架 / 环境 / 硬件**
- **TRL GRPOTrainer**（huggingface.co/docs/trl/grpo_trainer）— VLM 图像支持（Qwen2.5-VL 已测），reward_funcs，vLLM colocate/server；视频不支持（issue #4144 closed not-planned）
- HF blog: *Vision Language Model Alignment in TRL* — grpo_vlm.py, Qwen2.5-VL-3B, GSPO/RLOO for VLMs
- **EasyR1**（github.com/hiyouga/EasyR1）— verl-based VLM RL，Qwen2/2.5/3-VL，VRAM 表，Docker `hiyouga/verl:ngc-th2.8.0-cu12.9-vllm0.11.0`，geometry3k 示例
- **verl**（github.com/verl-project/verl）— HybridFlow，FSDP/Megatron + vLLM/SGLang
- **ms-swift**（github.com/modelscope/ms-swift）— swift rlhf GRPO，swift rollout 自动权重同步，Megatron-GRPO
- Unsloth vision-RL blog — 7B GRPO+LoRA 单卡（含 T4）的省显存路径
- verl GRPO+LoRA handbook（Weyaxi, HF blog）— gpu_memory_utilization 调优，lora_rank/alpha

**数据 / reward / 评估**
- hiyouga/geometry3k（EasyR1 parquet schema：images/problem/answer；train 2.1k/val 300/test 601）
- EasyR1 `examples/reward_function/math.py`（compute_score → {overall,accuracy,format}）
- math_verify（HF）/ mathruler grader — 符号答案匹配，pin 版本
- TVG 指标 — R@1@{0.5,0.7} + mIoU

**VS 内部（接回度量衡）**
- `docs/eval-system-and-layer0-plan.md` — VS 统一 eval 系统（可验证轴判分器 + 跨家族 judge + pass^k + CI 门）
- `pipeline/loop_driver.py:133` `run_loop`（推荐 eval seam）/ `evals/runner.py`（A5 harness）/ `evals/scorers.py`（A1 判分器：iou_r1 / recall_at_k / toolseq_match / refusal_ok）
- `evals/pinned/*.jsonl`（pinned 回归用例，硬门）

---

> **收尾提醒**：这条路的价值不在「造一个能替 Gemini 的模型」——它不能。价值在（1）你**真正理解**了 GRPO/RLVR 的机制与陷阱；（2）你得到一个在**某条可验证窄轴**上够强、能当 VS 工具节点/离线标注器的开源专家；（3）你写的 reward 函数**就是** VS eval 系统的判分器，两边共享度量衡。把预期钉在窄轴、把伪奖励对照和 pass^k 当纪律、把评估接回 VS——这三件事做到，这次学习就既涨了认知又产出了对 VS 有用的东西。
