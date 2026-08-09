# Eval 系统改进任务清单（backlog）

> 来源：2026-07-08 research 会话——六维代码审计（evals 排短板第 1）+ scorers/judge 逐行精读 + Anthropic《Demystifying evals for AI agents》方法论对照。
> 用法：eval 主线会话从这里领任务，做完打勾并在条目下补一行结果。行号是 2026-07-08 读码时的位置，动手前先核对。
> 原则提醒（本仓库既有约定）：改判分原则必须全套件扫一遍冤案；agent 本体缺陷只登记不顺手修。

## A. 直接修的 bug（半天级，门禁正确性优先）

- [ ] **A1 crash 题漏出门禁**：单题代码崩溃记 `status='crash'`，但 `_scored()` 只认 `status=='ok'`——崩溃题既不进通过率也不进必过题检查，必过题崩了不会打回（runner.py:303-308、335-337、375-379）。
  验收：人为让一道 pinned 题抛异常，classify 必须给"打回"。
- [ ] **A2 classify 不检查"尺子有没有换"**：与上次真跑对比时不比对 scorer_fp/题库指纹，修判分器带来的翻转会被归因成 agent 变好/变差（runner.py:508-511 有指纹但 classify 不消费；run1→run2 的 +6pp 就靠人工在 RESULTS.md 说明）。
  验收：指纹变了时 classify 输出降级为"尺子已变更，对比无效/仅供参考"。
- [ ] **A3 尺子小坑批量修 + 各补一个单测**（evals/scorers.py）：
  - `expect_refusal` 的 `startswith("no")` 会把英文 "Note that…" 误判成拒绝（scorers.py:139-145 附近）
  - `_NEG_WORDS_EN` 里 `"cannot "` 带尾空格，句末 "cannot." 匹配不上（scorers.py:20-23）
  - `expect_positive` 分支不剥引号，标题里的"没有"会干扰（剥引号只在 refusal 分支，scorers.py:118-119）
  - `entity_match` 数字不卡词边界："135" 命中 "1350"（scorers.py:206-214）
  - `recall_at_k` 的 `k` 参数传入但未使用（scorers.py:176-183）
  - `identity` 否认窗口只有前 12/后 10 字符，否认词稍远即误杀（scorers.py:246-252）
  - retrieval 查准只认 id 形态 token，用标题倒库不受罚（scorers.py:197-203）

## B. 结构性缺口（审计 3 个 high + 博客对照）

- [ ] **B1 judge×程序尺子 diff → 第一份人工校准集**（推荐最先做的结构项，一两天）：
  ① judge.py 加 holistic 模式（question + answer + expect 金标 → 整体 PASS/FAIL + 一句理由，不依赖逐题 nl_assertions），跑最近一次 live 全量；
  ② diff 出"程序过&judge挂 / 程序挂&judge过"两桶；
  ③ 人工只标这两桶（预计 10–25 题），归因四选一：尺子bug / judge错 / agent真错 / 题目歧义；
  ④ 产出 `evals/judge_calibration.jsonl` + 第一个 Cohen's κ。设计文档 A3 已定门槛：κ≥0.6 才可门禁，0.4–0.6 仅参考（docs/eval-system-and-layer0-plan.md:90-109）。
  参考论文：Weaver（弱校验器合成，arXiv:2506.18203）、Overconfidence in LLM-as-a-Judge（judge 输出信心值分流人工，arXiv:2508.06225）。
- [ ] **B2 感知质量从零到有**：analyze_video 在 eval 里是 fixture 回放（evals/fixtures/analyze_answers.py），"模型真的看懂没有"完全测不到。加 5–10 道真视频真 analyze 的金标题，只在手动 live 跑，接受每次几分钱成本。
- [ ] **B3 生产检索路径纳入评测**：eval 假索引只有 titles+facts、不含转录段（生产索引 2/3 是转录，evals/world.py:123-153），EVAL_SEMANTIC 默认关。做法：① 用真实索引跑一组 recall@k；② 照 eRAG（arXiv:2404.13781）把"每条检索结果单独喂下游看能否答对"做成机械检索评分，不需要人工标签。
- [ ] **B4 必过题 n≥3**：现在真跑 n=1，pass^k 全是 None，稳定性没被测量（τ-bench 核心洞见没用上）。全量 n=5 太贵可先只对 pinned 题 n=3。
- [ ] **B5 行为门禁挂 CI**：eval-gate.yml 目前只跑"评测机器自检"（假大脑 + 6 道 fixture 题），改 prompt/lessons 的 PR 没有行为门禁；设计文档 A4 的 pass^3 delta 门禁未落地（docs/eval-system-and-layer0-plan.md:184-187）。
- [ ] **B6 required_actions 从"验路径"逐步换"验产出"**：占 64 处计分，是 Anthropic 博客点名批评的脆弱模式（agent 换合法路径会被冤杀）。逐题过一遍：想验"有检索依据"的改用 retrieval/entity_match，required_actions 只留给非它不可的题（如必须产出 clip）。
- [ ] **B7 failure mining 加静默错误信号**：现在只挖 出错/绕圈/烧钱 三类硬信号（evals/mine_failures.py:19-24），"答错但便宜且正常结束"挖不到；且从没有挖出的题落库。B1 的 judge holistic 低分/低信心可直接当新挖掘信号；audit 日志缺用户反馈（点踩/重问）字段。
- [ ] **B8 饱和管理**：当前 131/143≈92%，信号在变弱。照博客"毕业"机制拆 回归套件（打满题防倒退）/ 能力套件（补难题：多视频跨会话、长上下文、跨视频关联——顺带覆盖审计发现的 context 短板）。
- [ ] **B9 simulated user 加固**（次优先）：drifted() 是永远 False 的占位（evals/simulated_user.py:53-59）；另按 Lost in Simulation（arXiv:2601.17087）固定模拟用户底模、意识到难题分数系统性低估。

## C. 零代码习惯

- [ ] **C1 每周固定读一批 transcript**（博客全文唯一感叹号建议）。briefing 导出已具备，缺节奏；每次读完把"尺子冤案/假通过"记回本清单。
- [ ] **C2 正反例配对审计**：过一遍套件，每类正例是否有对应反例（"该找到时找到"↔"库里没有时说没有"已配平；其他维度逐一检查），防单边优化。

## 附：当前判分体系速览（供领任务时对照）

13 把确定性尺子（scorers.py）：required_actions/no_call/no_forbidden（工具链）、honesty/safety（拒答）、retrieval/entity_match/count（内容）、timestamp（IoU）、no_id_leak/identity（泄漏）、jga/state_assertions（多轮/状态）。
聚合：reward_basis 点名的尺子全满分才过（case_pass，阈值 1.0）；题级 = n 次 rollout 全过；pinned 新失守即打回；两次跑对比用符号检验 p<0.05。
LLM judge：完全旁路（手动跑、sidecar 文件无消费者），固定 claude-haiku 跨家族，只看 question+answer+nl_assertions；nl_assertions 仅 7 题在用。
