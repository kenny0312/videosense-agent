# 设计文档：VideoSense 的按需 / 上下文驱动 / loop 感知视频理解工具

> 状态：Draft v1（待评审后动手）　范围：方向一
> 一句话：给 VS 主循环加一个 **`analyze_video` 原语**，它接受 **context + 任意 question**，输出 **为 loop 决策而设计的结构化 result**（`answers_question / confidence / evidence / suggested_next`），让 agent 能回答"挑一个最帅的 wingsuit 给我看并说理由"这类当前**会被拒答**的问题。

---

## 1. 背景与目标 · 贯穿例子

### 1.1 现状
VS 的主循环是 **probe-and-step**（`pipeline/loop_driver.py: run_loop`）：每步 Loop LLM 看工具声明 → 发起一批 function call → 主进程执行 → 把 **压缩后的 result preview 回喂** → LLM 据此决定下一步，直到收敛成纯文本答案。

今天 loop 能 `sql_query`（查结构化元数据 / 已落库的离线感知结果）、`show_video`（签 URL 播放）、以及一串分析工具（`plot / merge_asof / ...`）。但它**没有一个"现在去看一段视频、带着我此刻的问题去看"的能力**。视频内容理解只存在于**离线批处理**里（`perception/skydive_extract.py`、`perception/gemini_predicates.py`），prompt 写死、输出固定 schema，不可被 loop 按需驱动。

### 1.2 贯穿例子（North Star）
> **"给出你觉得最帅的一个 wingsuit 视频并展示，给理由。"**

这个问题今天 agent **会拒答**：因为"帅不帅"不在任何已落库的列里，没有 `WHERE cool_score > x` 可查。它本质上需要 **agent 现场看视频、带着"哪个最帅"这个主观问题逐个打分、再比较选优**。这正是本工具要解锁的能力。

### 1.3 目标
- **G1**　新增 loop 工具 `analyze_video(context, question, video ref[, time_range])`，**现场**调 Gemini 多模态看视频回答**任意**问题。
- **G2**　工具输出是 **loop 友好的结构化 result**，不是终结性黑盒答案；loop 能据此判断"能答了吗 / 还要做什么"。
- **G3**　复用既有接入链路（`node_specs / node_executor / loop_driver / show_video` 视频通道），不重写 orchestrator。
- **非目标**：不改离线感知管线；不追求一次看 N 个长视频的极致成本（见 §8 给出可落地的缩减策略）。

---

## 2. 为什么不能直接复用 perception 的 `analyze_video`（设计点 1）

现有 `perception/skydive_extract.py: analyze_video(model, gcs_uri)`（L81）与 `gemini_predicates.py` 同构，三处**硬伤**让它**无法**直接当 loop 工具：

| 维度 | 现有 perception `analyze_video` | loop 工具需要的 |
|---|---|---|
| **Prompt** | **写死**在模块常量 `PROMPT` / `_build_prompt()`，只问 skydive 阶段与 jump_type | 运行期**动态**：把 `question` + `context` 拼进 prompt |
| **入参** | 仅 `(model, gcs_uri)`，**无问题、无上下文注入点** | `question`（任意子任务）+ `context`（总目标 / 已知 / 为何分析 / 上一步发现）+ 可选 `time_range` |
| **输出** | **固定单一 schema**（`SkydiveExtraction`：6 个阶段 + is_wingsuit + summary），是**终结性**的一条记录、写库即止 | **决策导向**：`answers_question / confidence / evidence / suggested_next`，让 loop 决定下一步 |

> 一句话：perception 的版本是"**离线批处理**心智"——单次、固定、写库；loop 需要的是"**带着此刻的问题、此刻的上下文，去看这一段**"。

**复用什么 / 新增什么**（精确边界）：

- ✅ **复用 Gemini 调用范式**（来自 perception，逐字同构）：`Part.from_uri(uri=gcs_uri, mime_type="video/mp4")` → `model.generate_content([video_part, prompt], generation_config={response_mime_type:"application/json", temperature, max_output_tokens})` → 清理 ``` ``` ``` wrapper → `json.loads` → Pydantic 校验 → `RETRY_LIMIT` 重试。
- 🆕 **新增**：动态 prompt 工厂（`question + context → prompt`）、`AnalyzeRequest` 入参、**`AnalyzeResult` 决策 schema**。
- ⚠️ **Gemini 不原生支持** `start_time/end_time/fps` 参数（`Part.from_uri` 无此入口）。`time_range` 只能两条路落地：(a) **prompt 文本指令**"只关注 X–Y 秒"（软约束，不保证遵守）；(b) **客户端 ffmpeg 预裁剪**成短片再喂（硬约束、省 token，见 §8）。M0 先做 (a)，(b) 留作优化。

---

## 3. 核心设计：loop 感知的上下文化分析原语

### 3.1 输入：`AnalyzeRequest`
```python
class AnalyzeRequest(BaseModel):
    video_id: str                       # 主键;主进程据此查 video_metadata 拿 gcs_uri(复用 show_video 同源)
    question: str                       # 本次子任务,如 "这段 wingsuit 有多精彩(0-10)?给理由"
    context: str | None = None          # loop 注入:总目标/已知/为何分析/上一步发现(见 §5)
    time_range: tuple[float, float] | None = None   # 可选关注区间(prompt 软约束;M0)
    rubric: str | None = None           # 可选评分标准,如 "近地飞行/穿越地形/编队=更帅"
```
> 注意 `gcs_uri` **不**进 LLM 工具声明——loop 只给 `video_id`，主进程内部查表换 URI（与 `show_video` 同源，避免 LLM 处理 URI）。

### 3.2 输出：为 loop 决策而设计的结构化 result（设计点 2）

这是本设计的**灵魂**。输出**不是**"答案"，而是**让 loop 自己判断的证据包**：

```python
class Evidence(BaseModel):
    ts: float | tuple[float, float]     # 时间戳/区间
    what: str                           # 在这个时刻看到了什么(支撑 finding)

class AnalyzeResult(BaseModel):
    observations: list[str]             # 客观所见(不掺判断)
    finding: str                        # 针对 question 的结论(如 "精彩度 8/10")
    confidence: float                   # 0.0-1.0
    answers_question: Literal["yes", "partial", "no"]   # ★ loop 的总开关
    evidence: list[Evidence]            # 支撑 finding 的具体时刻
    suggested_next: list[str] = []      # 若 partial/no:建议下一步("看 0:30-0:50 确认开伞"/"换更高码率版本")
```

**loop 读到它后的决策树**（loop LLM 自行执行，工具不替它决定）：

```
answers_question == "yes" 且 confidence 够   → 收口出答案 / 进入比较
answers_question == "partial"                → 按 suggested_next 再分析(换时间段/换角度)
answers_question == "no"                     → 换视频 / 换工具 / 向用户澄清
```

> 关键对比：perception 版返回"这是 wingsuit、有 6 个阶段"——**一个事实**；loop 版返回"精彩度 8/10，因为有近地穿越（evidence ts=42s），我有把握（conf 0.8），这个问题能答了（yes）"——**一个可被 loop 比较和决策的判断单元**。

### 3.3 Prompt 工厂（动态拼装）
```
[系统] 你是视频内容分析助手。看这段视频,回答下面的问题。
[上下文] {context}                       # 没有则省略
[问题]   {question}
[关注区间] 只关注 {t0}-{t1} 秒(若给定)    # M0 软约束
[评分标准] {rubric}                       # 若给定
[输出]   严格输出 JSON,字段: observations, finding, confidence,
         answers_question(yes|partial|no), evidence[{ts,what}], suggested_next[]
         若信息不足以回答,answers_question 给 partial/no 并在 suggested_next 写清还需什么。
```

---

## 4. loop 如何编排：「挑最帅 wingsuit」端到端 trace

下面是 `run_loop` 实际会跑出的 trace（每步 = 一次 Loop LLM 决策；result 回喂见 §6）。

```
用户: "给我看你觉得最帅的一个 wingsuit 视频,并说理由。"

step 0  ── 缩候选 ────────────────────────────────────────────
  call: sql_query(sql="SELECT video_id,title FROM video_metadata
                       WHERE predicate ILIKE '%wingsuit%' LIMIT 12")
  回喂: {result_id:"c0_0", preview:[{video_id:"v07",...},{video_id:"v11",...},...], n:9}
  → LLM 看到 9 个候选(只 preview 3 行,但 n=9 告诉它真有 9 个)

step 1  ── 逐候选现场打分(同一步并行多 call) ──────────────────
  call: analyze_video(video_id="v07",
          question="这段 wingsuit 有多精彩? 0-10 打分并给理由",
          context="用户要从若干 wingsuit 中挑唯一最帅的来展示;只需相对可比的精彩度",
          rubric="近地飞行/穿越地形/编队/流畅运镜=更帅")
  call: analyze_video(video_id="v11", question=..., context=..., rubric=...)
  call: analyze_video(video_id="v19", question=..., context=..., rubric=...)
  ...(top-K 见 §8,不必 9 个全跑)
  回喂(每个一条):
    c1_0 → preview:[{finding:"8/10 近地穿越峡谷",confidence:"0.8",
                     answers_question:"yes", evidence:"[{ts:42,what:'贴崖穿越'}]"}], n=...
    c1_1 → preview:[{finding:"5/10 高空平飞为主",confidence:"0.7",answers_question:"yes",...}]
    c1_2 → preview:[{finding:"画面模糊,无法判断",confidence:"0.3",
                     answers_question:"partial", suggested_next:"[换更高码率版本]"}]

step 2  ── loop 自己比较 + 处理 partial ────────────────────────
  LLM 读各 finding/confidence:v07=8 最高且 yes;v19 是 partial 但分低,不值得再分析
  → 直接选 v07,不再追加分析(若最高分恰是 partial,会按 suggested_next 再补一步)

step 3  ── 展示 + 给理由 ───────────────────────────────────────
  call: show_video(video_ids=["v07"])          # 复用现有视频通道(§6.3)
  回喂: {result_id:"c3_0", videos 侧信道带 signed_url}

step 4  ── 收口 ───────────────────────────────────────────────
  LLM 输出纯文本(无 function call → run_loop 判定收敛):
  "最帅的是《v07 — Chamonix Proximity》。它在 0:42 贴着峡谷崖壁做了一次近地穿越,
   线路紧贴地形、运镜流畅,精彩度 8/10,明显高于其余候选(多为高空平飞)。已为你播放。"
```

要点：**工具从不替 loop 下"哪个最帅"的结论**——它只给每个视频一个可比较的判断单元，**比较与选优发生在 loop LLM**。这正是"为 loop 决策设计"的含义。

---

## 5. 上下文注入设计（传什么 · 怎么控 token）

`context` 由 **loop LLM 自己填**（它在工具声明里看到这个参数，会按需写）。建议在 `planner_desc` 里引导它放：

- **总目标**：用户最终想要什么（"挑唯一最帅的来展示"）——决定打分要**相对可比**而非绝对。
- **已知信息**：上游已查到的事实（"这是 9 个 wingsuit 候选之一"）——避免重复发现。
- **为何分析**：本次调用在大计划中的角色（"用于横向比较，不需详尽阶段拆解"）。
- **上一步发现**：若是 partial 后的二次分析，带上"上次画面模糊，这次看 0:30-0:50"。

**控 token 三原则**：
1. **context 是短文本**，不是把整个 transcript 倒进去；loop 自然只会摘要关键句（它也在为自己的 token 预算省）。
2. **prompt 模板固定、变量插槽小**——视频本身才是 token 大头（见 §8），文字 context 占比极低。
3. **rubric 复用**：同一轮多次 analyze 共用同一 rubric 字符串，靠 §8 的缓存键去重。

---

## 6. 接入 VS：复用既有链路（不改 orchestrator）

### 6.0 【头号决策】工具形态：in-process tool（本文默认）vs 独立 FastMCP server

你最初提到"用 FastMCP 封装"。这里给出权衡 —— 本文 §6.1 默认按 **in-process tool** 写（和现有 `show_video / threshold_sweep` 一样，是 `node_executor` 里的一个函数）：

| | in-process tool（推荐 M1–M3） | 独立 FastMCP server |
|---|---|---|
| 形态 | `node_executor` 里一个 `_run_analyze_video()` | 单独进程 `video_understanding` server（FastMCP **3.4.2**，py≥3.10，`@mcp.tool` 返回 Pydantic）+ VS 加一个 MCP client |
| 复杂度 | 最低，零新进程/传输 | 多一个进程 + stdio/http 传输 + client 生命周期 |
| 复用性 | 仅 VS 内用 | 可被别的 client（别的 app / Claude Desktop）复用 |
| 与现状一致 | ✅ 现有工具都是 in-process | 现有 `mcp_server` 是低层 stdio，再加个 FastMCP 风格不统一 |

**建议**：M1–M3 先做 **in-process**（最快验证产品价值）；若将来想把"视频理解"做成可被多方调用的独立服务，再抽成 FastMCP server（`pip install fastmcp`，`@mcp.tool` 直接返回 `AnalyzeResult` Pydantic，自动生成 schema，无需手写）。**这是你要拍的头号决策**，下面 §6.1 按 in-process 写，抽 FastMCP 时仅 §6.1 的 #3 换成"调 MCP client"。

### 6.1 四处接入清单（in-process）
| # | 文件 | 改动 |
|---|---|---|
| 1 | `pipeline/node_specs.py`（`SPECS` 字典，L34+） | 加一条 `NodeSpec(tool="analyze_video", needs_sandbox=False, planner_desc=..., parameters=_obj({question, context, time_range, rubric}, ["question"]))`。`video_id` 既可作 `parameters` 标量，也可走上游句柄（见 #4）。 |
| 2 | `pipeline/dag_schema.py`（L36/L43） | `DATA_TOOLS` 加 `"analyze_video"`；`ToolName` Literal 加 `"analyze_video"`。（`needs_sandbox=False` ⇒ 数据类，不是沙箱类。） |
| 3 | `pipeline/node_executor.py`（`execute_node` 的 `if not needs_sandbox(...)` 分支） | 加 `elif node.tool == "analyze_video": res = _run_analyze_video(node, upstream)`；新增 `_run_analyze_video()`：查 `video_metadata` 拿 `gcs_uri`（复用 `_run_show_video` 同套 SQL）→ 调 §3 的新 perception 函数 → `return NodeResult(..., ok=True, value=AnalyzeResult.model_dump())`。 |
| 4 | `pipeline/loop_driver.py`（`UPSTREAM_HANDLES`，L26） | 若希望 loop 用"上游某步选出的 video_id"驱动，加 `"analyze_video": ["video_result_id"]`（参照 `show_video` 已是可选句柄）。纯标量 `video_id` 入参则可不加。 |

> 视频内容理解的 perception 逻辑本身放在 `perception/analyze_video_contextual.py`（新文件），`node_executor` 只做"查 URI + 调函数 + 包 NodeResult"的薄适配，与 perception 解耦。

### 6.2 结构化 result 如何作为 preview 回喂 loop（已核对机制）
- `_run_analyze_video` 返回 `NodeResult(value=AnalyzeResult.model_dump())`（一个 dict）。
- `loop_driver._make_executor`（L195）对它跑 `_preview(value)`：dict ⇒ 取前 8 个字段、每格截 80 字（L53-68）。`AnalyzeResult` 顶层字段（`finding/confidence/answers_question/...`）刚好落进这 8 列——**loop LLM 正好看到决策所需的关键字段**。
- 回喂格式（L145）：`{result_id, preview, n}`——loop 据此决策。
- 若担心 `observations/evidence` 太长被 80 字截断影响判断：把**最关键的决策字段**（`finding / confidence / answers_question`）做成**短字符串**放在最前，长证据放后面（会被截断但 loop 通常不需要逐字看；需要时它会追加一步换角度分析）。

### 6.3 视频返回通道：原样复用 `show_video`
展示环节**不**由本工具负责——loop 选定后另发一个 `show_video(video_ids=[最帅的])`。视频经 `NodeResult.videos`（侧信道，**绕过** preview）→ `ExecResult.videos`（L197）→ `orchestrator._result` L48/57 `videos` 字段 → 前端 `<video src=signed_url>`。**这条链已存在，零改动。**

---

## 7. 两种输入 × 两种输出

|  | **输出：NL 答案** | **输出：视频返回** |
|---|---|---|
| **输入：GCS 已入库视频**（主路径，M0） | loop：`sql_query → analyze_video* → 文本收口` | loop：`...analyze_video* → show_video`（贯穿例子） |
| **输入：用户实时多模态上传**（M5+） | 上传落 GCS（或临时桶）→ 拿到 `video_id/gcs_uri` → 同上 | 同左，外加 `show_video` 回放刚上传的片段 |

实时上传只需在入口处把用户文件落成一个**临时 `video_id`**（写 `video_metadata` 或一张 ephemeral 表），后续链路**完全复用** GCS 路径——`analyze_video` 不感知来源差异。

---

## 8. 成本 / 延迟 / 缓存

"看 N 个视频 = N 次多模态调用"是主要成本。落地缓解（按优先级）：

1. **缩候选 top-K**：先 `sql_query` 用结构化元数据/已落库 predicate 把候选压到 ≤K（如 6），再逐个 analyze。**绝不**对全库视频盲扫。
2. **同步并行**：同一 step 内 loop 发多条 `analyze_video` call，主进程并行起 Gemini 请求（`run_loop` 每步本就是一批 call）。
3. **便宜模型 triage**：先用 `gemini-2.5-flash`（`PERCEPTION_MODEL` 默认）快速粗筛打分，只对 top-2~3 用更强模型复核（可选第二轮）。
4. **time-range 采样**：长视频先问"哪段最可能精彩"，再只对该段（ffmpeg 预裁，§2）做细看——省 token。
5. **`(video_id, question, time_range, rubric)` hash 缓存**：同一视频被同一问题重复问（同轮 retry / 跨轮）直接命中缓存，不重调 Gemini。缓存 `AnalyzeResult` JSON 即可。

延迟预期：单次多模态约数秒~十几秒；top-K=6 并行 ≈ 一次的墙钟时间。给前端发 `on_step` 事件（`run_loop` 已支持 SSE 流式 L149）做进度反馈。

---

## 9. 开放问题（需拍板）

0. **【头号】工具形态：in-process tool vs FastMCP server**（见 §6.0）。本文默认 in-process；你最初想要 FastMCP。建议先 in-process 验证产品价值，后续按需抽成 FastMCP server。
1. **`video_id` 走标量入参还是上游句柄？** 句柄更"agentic"（loop 用上一步选出的 id），标量更简单。建议**两者都支持**（标量为主，句柄可选），与 `show_video` 一致。
2. **分数可比性**：不同 video 各自独立打分，跨样本可比吗？是否需要"一次喂多段做相对排序"的变体（更省 call 但受 Gemini 多视频上下文长度限制）？建议 M1 评测后决定。
3. **`evidence.ts` 与 `show_video` 跳播联动**：是否让 loop 把 `evidence` 里的 ts 透传给 `show_video` 的 `start_ts`，直接跳到"最帅那一刻"？（低成本增益，建议 M1 做。）
4. **实时上传的存储与清理**：临时 `video_id` 的 TTL / 桶 / 配额策略。
5. **token 上限保护**：单视频时长/大小硬上限（Gemini ~1GB 限制），超限时 fail-open 给 `answers_question:"no" + suggested_next:"视频过大，需切片"`。
6. **`needs_clarification` 回路**：partial 时除了 loop 自动再分析，要不要允许 loop 反问用户？（影响交互设计。）

---

## 10. 里程碑 Roadmap

| 里程碑 | 内容 | 退出标准 |
|---|---|---|
| **M0 设计定稿** | 本文 + §9 拍板 | 评审通过 |
| **M1 原语落地** | `perception/analyze_video_contextual.py`：`AnalyzeRequest / AnalyzeResult / 动态 prompt / 复用 Gemini 范式`；**离线单测**（mock Gemini，验 schema + partial/no 路径） | 单测绿;给定 (question,context) 产出合法 `AnalyzeResult` |
| **M2 接入 loop** | §6.1 四处登记 + `_run_analyze_video` 适配 + preview 字段顺序调优 | 在 `run_loop` 里能被 LLM 调起、result 正确回喂 |
| **M3 端到端「挑最帅」** | 贯穿例子全链路（sql→analyze*→show_video）跑通 | demo：拒答问题现在能答 + 播放 + 给理由 |
| **M4 成本/缓存** | top-K、并行、`(video,question)` 缓存、flash triage | N=6 候选墙钟 ≈ 单次;重复问命中缓存 |
| **M5 实时上传 + time-range** | 用户上传路径;ffmpeg 预裁切片 | 上传视频可被同链路分析 |
| **M6 灰度 → 上线** | feature flag 灰度、usage 日志（接现有 token 计量）、回归 | 灰度无回归 → 全量;Looker 看用量 |

---

### 已核实事实锚点（防编造，verify 通过）
- loop 回喂契约 `{result_id, preview, n}`：`loop_driver.py` L145；`_preview` 3×8×80：L53-68。
- 视频侧信道绕过 preview：`NodeResult.videos`(executor) → `ExecResult.videos` L197 → `orchestrator._result` L48/57。
- 上游句柄注入 `result_id`：`loop_driver.py` `UPSTREAM_HANDLES` L26-34（`show_video` 为可选句柄样板）。
- 四处接入约定：`node_specs.SPECS` L34、`dag_schema.DATA_TOOLS/ToolName` L36/L43、`node_executor.execute_node` 数据类分支、`loop_driver.UPSTREAM_HANDLES`。
- perception 现状（须替换）：`skydive_extract.py: analyze_video(model, gcs_uri)` L81，prompt 写死、固定 `SkydiveExtraction` schema；Gemini 范式 `Part.from_uri + generate_content(response_mime_type=json)` 可复用。
- `time_range`：Gemini `Part.from_uri` 无 start/end/fps 入口，仅 prompt 软约束或 ffmpeg 预裁。
- FastMCP 当前版本：`fastmcp` 3.4.2（py≥3.10），`@mcp.tool` 可直接返回 Pydantic 模型自动生成 schema。
