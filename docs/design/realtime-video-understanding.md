# 设计文档 v2：VideoSense 的视频理解能力（通用原语 → 视频专家 agent-team）

> 状态：**v2**（评审反馈已并入，P1 可动手）　范围：方向一
> **v2 变更摘要**（相对 v1）：① 输出从死 schema → **最小信封**（`answer` 自由 + `enough` + `confidence`）；② 定位是**通用原语**（`question` 是参数，覆盖任意问题），非窄工具；③ 形态**分阶段**：in-process 原语 → FastMCP 视频专家 → multi-agent 团队；④ 打分 = **对话敲 rubric 再打**；⑤ 新增 **`clarify` 反问回路**；⑥ 配额保护 `MAX_VIDEOS_PER_REQUEST=5`。

---

## 0. 设计原则（评审共识）

1. **别过度结构化、别把能力写死**——客户的问题千奇百怪（今天"帅不帅"、明天"几个人"、后天"我自己滑雪的视频"）。
2. **但 loop 看的是工具结果的 preview**（`loop_driver._preview` 截到 ≈3×8×80），**决策关键信息必须短、靠前** → 这要求"一点点结构" = **最小信封**。
3. **通用 > 专用**：一个"看视频回答**任意** question"的原语，胜过一堆窄工具。
4. **先验证、后升格**：in-process 原语先证明价值；**接口为 agent-team 留好**，升格时 VS 主 loop 零改动。

---

## 1. 背景与目标 · 贯穿例子

VS 主循环是 **probe-and-step**（`pipeline/loop_driver.py: run_loop`）：每步 Loop LLM 看工具声明 → 发 function call → 主进程执行 → 把**压缩后的 result preview 回喂** → LLM 决定下一步，直到收敛成文本。

今天 loop 能 `sql_query / show_video / plot / ...`，但**没有"现在带着我此刻的问题去看一段视频"的能力**——视频理解只在**离线批处理**里（`perception/`），prompt 写死、输出固定 schema。

**贯穿例子（North Star）**："给我看你觉得**最帅**的一个 wingsuit 视频，并说理由。" —— 今天 agent **会拒答**（"帅"不在任何列里）；它需要 agent **现场看视频、按用户认可的标准逐个判断、再比较选优**。

**目标**：G1 新增"看视频答**任意** question"的能力；G2 输出**最小信封**，loop 能判断"够了吗/还要做什么"，又不绑死模型；G3 复用既有接入链路，主 loop 不重写；G4 接口为 agent-team 升格预留。

---

## 2. 为什么不能直接复用 perception 的 `analyze_video`

`perception/skydive_extract.py: analyze_video(model, gcs_uri)`（L81）三处硬伤：**prompt 写死**（只问 skydive 阶段）、**入参无 question/context 注入点**、**输出固定 `SkydiveExtraction` schema**（终结性、写库即止）。它是"离线批处理心智"；loop 要的是"带着此刻的问题与上下文去看这一段"。

**复用 / 新增边界**：
- ✅ **复用 Gemini 调用范式**（逐字同构）：`Part.from_uri(uri, mime_type="video/mp4")` → `model.generate_content([video_part, prompt], generation_config={response_mime_type:"application/json", ...})` → 清理 wrapper → `json.loads` → Pydantic 校验 → `RETRY_LIMIT` 重试（模型默认 `gemini-2.5-flash` / `PERCEPTION_MODEL`）。
- 🆕 **新增**：动态 prompt 工厂、`AnalyzeRequest` 入参、**最小信封 `AnalyzeResult`**。
- ⚠️ Gemini `Part.from_uri` **无 start/end/fps 入口**：`time_range` 只能 (a) prompt 文本软约束，或 (b) ffmpeg 预裁（硬约束、省 token，见 §8）。M1 先 (a)。

---

## 3. 核心设计：通用原语 + 最小信封

### 3.1 它是"通用原语"，不是窄工具（回应"tool 别写死"）

**`question` 是参数**——同一个 `analyze_video` 就能答"帅不帅 / 几个人 / 在干嘛 / 哪个更…"。这就是你要的灵活，只是打包成**一个通用原语**，而非每次现写代码。

> 与现有能力的分工：**`analyze_video`（看视频答任意问题）+ `python` 逃生舱（任意数据处理，模型现写代码进沙箱）+（未来）视频 agent-team（深度多步）** 共同覆盖"千奇百怪"。
> 为什么视频不能纯靠 `python` 逃生舱：**沙箱隔离（无网络/无 Gemini 凭证）**，跑不了多模态——所以必须有这个"能调 Gemini 的入口"。

### 3.2 输入：`AnalyzeRequest`
```python
class AnalyzeRequest(BaseModel):
    video_id: str                       # 走【上游句柄】为主(见 §7.1):loop 用上一步选出的 result_id 驱动,可回溯
    question: str                       # 本次子任务,任意:"这段 wingsuit 多精彩(0-10)+理由" / "几个人" / "在干嘛"
    context: str | None = None          # loop 注入:总目标/已知/为何分析/上一步发现(见 §6)
    rubric: str | None = None           # 评分/判断细则(来自 §5 与用户对话敲定)
    time_range: tuple[float, float] | None = None   # 可选关注区间(M1 prompt 软约束)
```

### 3.3 输出：**最小信封**（回应"强结构化会限制模型"）

不是死 schema，是**主体自由 + 两个薄控制位**：
```python
class AnalyzeResult(BaseModel):
    answer: str                         # ★自由文本:模型完整回答,想怎么说怎么说。
                                        #   要点【写在最前】——preview 只露前 ~80 字。
                                        #   ("8/10,0:42 贴崖近地穿越" / "3 个人" / "后空翻接转体")
    enough: Literal["yes", "partial", "no"]   # loop 唯一需要的钩子:够不够回答
    confidence: float = 0.5             # 0-1
    evidence_ts: float | None = None    # 可选:最关键证据时刻 → 透传给 show_video 的 start_ts(§7.3)
```

**为什么这样既灵活又能用**：
- `answer` **完全不约束** → 任意问题、任意形状的答案都装得下，模型自由推理（解决"被 schema 逼成填表"）。
- `enough` 是 loop **唯一**需要的短钩子：`yes`→收口/进入比较；`partial`→再看一段（§5.3）；`no`→换视频/换工具/反问。
- **过 preview 机制**：dict 的前几个字段都会进 preview，`answer` 截 80 字——所以 **prompt 强制要求模型把结论写在 `answer` 开头**，preview 就能露出要点；`enough/confidence/evidence_ts` 本就短，完整可见。

### 3.4 Prompt 工厂（动态拼装）
```
[系统] 你是视频内容分析助手。看这段视频,回答问题。把【结论写在 answer 开头一句】,再展开。
[上下文] {context}            # 没有则省略
[问题]   {question}
[判断细则] {rubric}           # 若给定(来自与用户对话)
[关注区间] 只看 {t0}-{t1} 秒  # 若给定(M1 软约束)
[输出 JSON] answer(结论在前的自由文本) · enough(yes|partial|no) · confidence · evidence_ts(可选)
           信息不足以回答 → enough=partial/no,并在 answer 写清还差什么。
```

---

## 4. loop 编排：「挑最帅 wingsuit」端到端 trace

```
用户: "给我看你觉得最帅的一个 wingsuit 视频,并说理由。"

step A  ── clarify:细则未知,先反问(§5.1) ───────────────────────
  loop 判断 "最帅" 主观、rubric 未知 → 不分析,先反问(status=clarify):
  "你说的'帅'更看重哪点?① 近地飞行 ② 编队 ③ 运镜流畅 ④ 综合"
  〔用户回:"近地飞行 + 运镜"〕                              # 下一轮带着这个继续(多轮记忆)

step 0  ── 缩候选 ───────────────────────────────────────────────
  sql_query("SELECT video_id FROM ... WHERE is_wingsuit LIMIT ≤5")   # 配额 §9
  回喂 preview: 5 个候选 id

step 1  ── 逐候选打分(同步并行;带 rubric) ──────────────────────
  analyze_video(video_id=⟨候选句柄⟩,
     question="这段 wingsuit 按用户标准多精彩? 0-10 + 理由",
     context="从 5 个 wingsuit 里挑唯一最帅来展示;只需相对可比",
     rubric="近地飞行 + 运镜流畅 优先")
  ...(≤5 个并行)
  回喂(每个最小信封):
    c1_0 → answer:"8/10 0:42 贴崖近地穿越,运镜稳", enough:"yes", confidence:0.8, evidence_ts:42
    c1_1 → answer:"5/10 高空平飞为主",           enough:"yes", confidence:0.7
    c1_2 → answer:"画面模糊,无法判断",           enough:"partial", confidence:0.3

step 2  ── loop 比较 + 处理 partial(§5.3) ──────────────────────
  c1_0=8 最高且 yes → 选它;c1_2 partial 但分低,不值得再看 → 直接定 c1_0

step 3  ── 展示(跳到最帅那一刻,§7.3) ──────────────────────────
  show_video(video_ids=[⟨c1_0 句柄⟩], start_ts=42)   # evidence_ts 透传

step 4  ── 收口 ─────────────────────────────────────────────────
  "最帅的是这条:0:42 贴着峡谷崖壁做了一次近地穿越,运镜很稳,8/10,
   明显高于其余(多为高空平飞)。已为你跳到那一刻播放。"
```

**要点：工具从不替 loop 下"哪个最帅"的结论**——它只给每个视频一个可比较的最小信封，**比较与选优发生在 loop LLM**。

---

## 5. 三个交互回路（把 clarify / rubric / partial 串起来）

### 5.1 `clarify`：问题模糊 → 反问用户（回应决策 #7）
当 question 主观/模糊（"最帅""好不好看"）且 `rubric` 未知时，loop **先反问**而非硬猜。机制：
- 新增结果状态 **`clarify`**（与 `answer/refused/smalltalk` 并列）；其 `answer` 字段其实是**给用户的问题**。
- 前端在 `refuse/answer` 之外渲染一个 **clarify 状态**（一句问 + 可选选项 chip）；用户回答 = 一个 followup 轮。
- 下一轮，用户的澄清经**多轮记忆/transcript 回放**进入上下文，loop 据此**固化成 `rubric`** 继续。

### 5.2 rubric-via-dialogue（回应决策 #3）
**不一次性硬打分**——先把用户认可的细则敲成一个 `rubric` 字符串，**同一把尺子**喂给本轮所有 `analyze_video`。顺带解决"跨视频可比性"。rubric 来源：用户在 §5.1 的回答 / 用户原话里已含的标准 / 默认通用细则（用户没要求时）。

### 5.3 `partial` 自纠
`enough=="partial"` 且该候选还**值得**（如分高但某段没看清）→ loop 按需**再发一次** `analyze_video`（带 `time_range` 看具体段）。不值得（分低）就跳过。**自纠由 loop 决定，工具不替它决定。**

---

## 6. context 注入（传什么 · 控 token）

`context` 由 **loop LLM 自己填**（它在工具声明看到该参数）。`planner_desc` 引导它放：**总目标**（"挑唯一最帅来展示"→ 打分要相对可比）、**已知**（"5 个候选之一"→ 免重复发现）、**为何分析**（"横向比较，不需详尽阶段拆解"）、**上一步发现**（partial 二次分析时带"上次模糊，这次看 0:30-0:50"）。
控 token：context 是**短文本**（非整段 transcript）；视频本身才是 token 大头（§9）；rubric 同轮复用、靠 §9 缓存键去重。

---

## 7. 接入 VS + 形态分阶段

### 7.0 形态分阶段（回应质疑 3 + 决策 #1：先验证、给后续）

| 阶段 | 形态 | 目的 |
|---|---|---|
| **P1 验证**（M1–M3） | **in-process 通用原语**（最小信封） | 最快证明"现场看视频答任意问题"有价值，不绑死模型 |
| **P2 升格** | 抽成独立 **FastMCP server = 视频专家**（`fastmcp` 3.4.2，`@mcp.tool` 直接返回 `AnalyzeResult` Pydantic），内部**自己一个 mini probe-and-step loop**（可多步：抽帧/对比/复看） | 视频理解能多步、能自纠错；VS 委派即可 |
| **P3 团队化** | server 内部 **multi-agent 串/并联**（triage → 分头分析 → 汇总评审） | 复杂视频任务的深度与并行 |

> **接口不变是关键**：VS 主 loop 永远只发"`{question, context, video, rubric?}`"、拿回"最小信封"。所以从 P1 原语 → P2/P3 agent-team，**VS 主 loop 零改动**——这就是 agent-as-tool / sub-agent delegation：对 VS 它像"问视频专家一个问题"，内部是不是一支队伍 VS 不关心。

### 7.1 P1 四处接入（in-process，**句柄为主**，回应决策 #2）
| # | 文件 | 改动 |
|---|---|---|
| 1 | `pipeline/node_specs.py`（`SPECS` L34+） | 加 `NodeSpec(tool="analyze_video", needs_sandbox=False, planner_desc=..., parameters=_obj({question, context, rubric, time_range}, ["question"]))` |
| 2 | `pipeline/dag_schema.py`（L36/L43） | `DATA_TOOLS` + `ToolName` 加 `"analyze_video"` |
| 3 | `pipeline/node_executor.py` | `elif node.tool=="analyze_video": _run_analyze_video(node, upstream)`；新函数查 `video_metadata` 拿 `gcs_uri`（复用 `_run_show_video` SQL）→ 调 `perception/analyze_video_contextual.py` → `NodeResult(value=AnalyzeResult.model_dump())` |
| 4 | `pipeline/loop_driver.py`（`UPSTREAM_HANDLES` L26） | 加 `"analyze_video": ["video_result_id"]`（**句柄为主**：loop 用上一步 `sql_query` 选出的 id 驱动，**每步有 result_id 可追溯谁喂给谁**——debug/审计全靠它；标量 video_id 作兜底） |

> 视频理解逻辑放 `perception/analyze_video_contextual.py`（新文件），`node_executor` 只做薄适配——**P2 升格时只换第 3 步为"调 MCP client"，其余不动。**

### 7.2 最小信封如何过 preview 回喂（已核对机制）
`_run_analyze_video` 返回 `NodeResult(value={answer, enough, confidence, evidence_ts})`。`loop_driver._make_executor`（L195）跑 `_preview`：dict 取前 8 字段、每格 80 字（L53-68）→ loop 看到 `answer[:80]`（含前置结论）+ `enough` + `confidence` + `evidence_ts`。回喂格式 `{result_id, preview, n}`（L145）。**所以 prompt 必须强制"结论写 answer 开头"**（§3.4）。

### 7.3 视频返回：复用 `show_video` + evidence 跳播（回应决策 #4）
loop 选定后另发 `show_video(video_ids=[…], start_ts=evidence_ts)`。视频经 `NodeResult.videos`（侧信道，绕过 preview）→ `ExecResult.videos`（L197）→ `orchestrator._result` L48/57 → 前端 `<video>`（前端已支持 `start_ts` 跳播）。**链已存在，零改动。**

---

## 8. 两种输入 × 两种输出 + 实时上传存储（回应决策 #5）

|  | 输出：NL 答案 | 输出：视频返回 |
|---|---|---|
| **GCS 已入库视频**（主路径 M1） | `sql → analyze_video* → 收口` | `...analyze_video* → show_video`（贯穿例子） |
| **用户实时多模态上传**（M5） | 上传落 GCS → 拿临时 `video_id` → 同上 | 同左 + `show_video` 回放刚传的 |

**实时上传存储策略（你问有哪些）**，推荐 **(c)→(a)→(b)** 组合：
- **(a) 临时前缀 + GCS lifecycle 自动删**：`gs://…/uploads/{user}/{uuid}.mp4`，bucket 规则 N 天过期（你已有 lifecycle 经验：transcripts）。
- **(b) ephemeral 表 + TTL**：临时 `video_id` **不进** `video_metadata`（免污染正式语料）。
- **(c) 前端直传签名 URL**：resumable signed URL 直传 GCS，**不经后端**（省后端带宽）。
- **(d) 配额**：每用户每天 X 个 / 总大小上限。

---

## 9. 成本 / 延迟 / 缓存 / **配额保护**（回应决策 #6）

"看 N 个视频 = N 次多模态调用"是主要成本。缓解（按优先级）：
1. **缩候选 top-K**：先 `sql_query` 把候选压到 ≤K，绝不盲扫全库。
2. **同步并行**：同一 step 内多条 `analyze_video` call 并行起 Gemini。
3. **flash triage**：先 `gemini-2.5-flash` 粗筛，只对 top-2~3 用更强模型复核（可选第二轮）。
4. **time-range 采样**：长视频先定位精彩段，再只细看该段（ffmpeg 预裁）。
5. **`(video_id, question, time_range, rubric)` hash 缓存**：重复问直接命中，不重调 Gemini。

**配额保护（先一个蠢但能用的）**：
- 常量 **`MAX_VIDEOS_PER_REQUEST = 5`**：单请求 `show_video` + `analyze_video` 累计涉及视频数 ≤5；超了在**工具层截断** + 给 loop 提示（"超过上限，只处理前 5 个 / 请缩小范围"）。
- **后续方案（让用户选）**：做成**按 plan 分级**（free=5 / pro=N）；触顶时返回一个结构化提示（`{type:"quota", limit:5, upgrade:true}`）供前端展示"想分析更多？→ 升级"。

延迟：单次多模态数秒~十几秒；top-K=5 并行 ≈ 一次墙钟。靠 `on_step` SSE（`run_loop` L149）给前端进度。

---

## 10. 决策记录（评审已定）+ 仍开放

**已定（本 v2 已并入）**：
- #0 形态 = **先 in-process 原语，后升格 FastMCP 视频专家 / agent-team**（§7.0）。
- #1 输出 = **最小信封**（§3.3）。
- #2 video_id = **句柄为主，可回溯**（§7.1）。
- #3 打分 = **对话敲 rubric 再打**（§5.2）。
- #4 evidence_ts = **透传 show_video 跳播**（§7.3）。
- #6 保护 = **`MAX_VIDEOS_PER_REQUEST=5` + 后续 plan 分级**（§9）。
- #7 = **`clarify` 反问回路**（§5.1）。

**仍开放**：
- 跨视频"**一次喂多段做相对排序**"的变体（更省 call，但受 Gemini 多视频上下文长度限制）——M3 评测后定。
- **P2 触发时机**：什么信号判定"该升格成多步视频专家"（如单次 analyze 的 partial 率、用户复杂度）。
- clarify 的"**何时该反问 vs 直接用默认 rubric 干**"的阈值（太爱反问会烦人）。

---

## 11. Roadmap

| 里程碑 | 内容 | 退出标准 |
|---|---|---|
| **M0 v2 定稿** | 本文 | 评审通过 |
| **M1 原语** | `perception/analyze_video_contextual.py`：`AnalyzeRequest / AnalyzeResult(最小信封) / 动态 prompt / 复用 Gemini 范式`；离线单测（mock Gemini，验 yes/partial/no + answer 前置） | 单测绿 |
| **M2 接入 loop** | §7.1 四处（句柄为主）+ `_run_analyze_video` + preview front-load + 配额截断 | loop 能调起、最小信封正确回喂 |
| **M3 端到端「挑最帅」** | `clarify→rubric→analyze*→show_video(跳播)` 全链路 | demo：拒答问题现在能答 + 跳到关键时刻播放 + 给理由 |
| **M4 成本/缓存/配额** | top-K、并行、flash triage、`(video,question)` 缓存、`MAX_VIDEOS_PER_REQUEST` + plan 提示 | N=5 并行 ≈ 单次；重复问命中缓存 |
| **M5 实时上传** | 直传签名 URL + lifecycle 临时前缀 + ephemeral 表 + ffmpeg time-range | 上传视频可被同链路分析 |
| **M6 升格 P2** | 抽成 FastMCP 视频专家 server（内部 mini-loop）；node_executor 第 3 步改调 MCP client | VS 委派、行为不回归 |
| **M7 P3 团队化** | server 内 multi-agent 串/并联 | 复杂任务质量提升（远期） |
| **M8 灰度 → 上线** | feature flag、usage 日志、回归 | 灰度无回归 → 全量 |

---

### 已核实事实锚点（verify 通过）
- loop 回喂 `{result_id, preview, n}`：`loop_driver.py` L145；`_preview` 3×8×80：L53-68。
- 视频侧信道绕 preview：`NodeResult.videos` → `ExecResult.videos` L197 → `orchestrator._result` L48/57。
- 句柄注入 `result_id`：`loop_driver.UPSTREAM_HANDLES` L26-34（`show_video` 为可选句柄样板）。
- 四处接入：`node_specs.SPECS` L34、`dag_schema.DATA_TOOLS/ToolName` L36/L43、`node_executor.execute_node` 数据类分支、`loop_driver.UPSTREAM_HANDLES`。
- perception 现状（须替换）：`skydive_extract.py: analyze_video(model, gcs_uri)` L81；Gemini 范式 `Part.from_uri + generate_content(response_mime_type=json)` 可复用。
- `time_range`：Gemini `Part.from_uri` 无 start/end/fps，仅 prompt 软约束或 ffmpeg 预裁。
- FastMCP：`fastmcp` 3.4.2（py≥3.10），`@mcp.tool` 可直接返回 Pydantic 自动生成 schema。
