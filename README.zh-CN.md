<div align="center">

# 🎬 VideoSense Agent

### 面向任意视频库的自然语言理解与分析 —— 用大白话提问,拿到答案、图表,以及背后的代码。

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Production-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Powered by Gemini](https://img.shields.io/badge/Powered%20by-Gemini%202.5-4285F4?logo=google&logoColor=white)](https://deepmind.google/technologies/gemini/)
[![Google Cloud](https://img.shields.io/badge/Google%20Cloud-AlloyDB%20%7C%20Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/)

[English](README.md) · **简体中文**

</div>

---

## 演示

<div align="center">

<img src="docs/demo.gif" alt="VideoSense Agent 演示" width="800"/>

<sub>一个问题 → 一份多步计划 → 自愈执行 → 答案 + 代码。<a href="docs/DEMO.md"><b>查看完整走读 →</b></a></sub>

</div>

```
你:   Find every video that contains skiing or snowboarding.
→     3 个视频 · Skiing in Aspen · Snowboarding Slopes · Backcountry Snowboarding Run

你:   Plot start time vs. detection confidence for all confirmed activities.
→     散点图  →  http://localhost:8000/plots/bb9ab8e1.svg

你:   Take the 3 highest-confidence skiing clips, align them with heart-rate
      sensor data, resample to 10 Hz, and run an OLS regression.
→     时间对齐样本上的 R² · 外加算出它的那段确切 Python 代码
```

---

## 它能做什么

**VideoSense Agent 把原始视频变成一个可以用自然语言追问的知识库。**

一个多模态大模型逐条"观看"视频,抽取出带置信度的结构化事实
("*snowboarding*, 0.96, 3s–36s")。在这层事实之上,你像问数据分析师一样提问 ——
*"找"*、*"对比"*、*"关联"*、*"画图"* —— 系统会:

1. **路由(Route)**:先判断这个问题用现有数据和工具到底能不能答,以及它是不是对上一轮的**追问**。同一会话里,*"把那批画出来"*、*"你刚才怎么算的?"* 这类指代会对照**之前真正算过的结果**去解析;只有实在对不上号时,它才会**老实说答不了,而不是瞎猜**。
2. **规划(Plan)**:把问题编译成一张可执行的步骤图。
3. **写码(Write)**:为每个分析步骤即时生成 Python。
4. **运行(Run)**:在隔离沙箱里跑这些代码,**出错能自我修复**(数据库步骤同样会自愈)。
5. **返回(Return)**:给出答案、图表,以及产出它们的那段确切代码。

不用配仪表盘、不用写 SQL、不用伺候 notebook。问就完事。

### 凭什么不一样

| | 传统视频搜索 | VideoSense Agent |
|---|---|---|
| **查询方式** | 关键词 / 标签匹配 | 完整自然语言 |
| **结果** | 一串片段 | 计算分析、回归、图表 |
| **新问题** | 搭一条新流水线 | 直接问 —— 代码按问题即时生成 |
| **追问上文** | 每次从头来 | 记得这次会话 —— *"把那批画出来"*、*"你刚才怎么算的?"* 直接接得上 |
| **可信度** | 黑盒 | 同时返回**计划**和**可运行的代码** |
| **答不了时** | 给错的或空的结果 | 诚实拒答,并附一句大白话理由 |

---

## 工作原理

```
   Natural-language question
            │
            ▼
   ┌──────────────────┐
   │   ROUTER          │   answerable? · intent? · follow-up?
   │   can I answer?   │   ──► resolve refs · else refuse honestly
   └────────┬─────────┘
            │ yes
            ▼
   ┌──────────────────┐   reads live DB schema     ┌──────────────────┐
   │   PLANNER        │ ◀────────────────────────▶ │  Knowledge base   │
   │   question → plan│        (via MCP)            │  (video facts)    │
   └────────┬─────────┘                            └──────────────────┘
            │  a graph of steps
            ▼
   ┌──────────────────┐   writes Python per step
   │  CODE GENERATOR  │ ─────────────────────────┐
   └──────────────────┘                          ▼
                                       ┌──────────────────────┐
                                       │  SECURE SANDBOX       │
                                       │  run · fail · self-fix│  ↺ up to 3×
                                       └──────────┬───────────┘
                                                  ▼
                              answer · chart · generated code
```

整条流水线把**"决定做什么"**(一份透明、可审计的计划)和**"具体去做"**(在隔离环境里运行、能自我修复的生成代码)分开。简单查询走快速通道;只有分析类工作才需要付出代码生成与沙箱执行的成本。

---

## 技术栈

构建在现代云原生 AI 栈之上:

- **🧠 多模态 AI** —— Gemini 2.5(Vertex AI),同时负责感知与代码生成
- **🔌 Model Context Protocol** —— 标准化、基于真实 schema 的数据库访问
- **🛡️ 隔离执行** —— Cloud Run + gVisor 沙箱,带 AST 策略闸门
- **🗄️ 云端数据** —— AlloyDB for PostgreSQL · Google Cloud Storage
- **⚡ 生产级 API** —— FastAPI,全容器化

---

## 环境要求

- **Python** 3.11+
- **Google Cloud** 账号(已开启 Vertex AI),且 `gcloud` CLI 已认证
- **(可选)** AlloyDB / PostgreSQL —— *或者*用零成本 **mock 模式**(无需数据库)

安装依赖:

```bash
pip install -r requirements.txt
```

认证 Google Cloud(Gemini 需要):

```bash
gcloud auth application-default login
```

---

## 快速开始

最快的体验方式 —— **mock 模式**用一个内存数据库,内置示例视频事实,
**无需 AlloyDB、存储零成本**。

### 1. 配置环境

```powershell
$env:GCP_PROJECT      = "your-gcp-project-id"                  # Vertex AI 项目(Gemini 必填)
$env:REPL_USE_MOCK_DB = "1"                                    # 零成本内存数据
$env:SANDBOX_URL      = "https://your-sandbox-xxxxx.run.app"   # 托管的安全沙箱(仅科学步骤需要)
$env:SANDBOX_TOKEN    = (gcloud auth print-identity-token)
```

### 2. 启动 API

```bash
uvicorn api.server:app --port 8000
```

### 3. 提问

在浏览器打开内置测试页 —— 或 Swagger 文档:

```
http://localhost:8000/         # 内置查询测试页(输入问题,看答案 + trace)
http://localhost:8000/docs     # 交互式 API 文档
```

或直接调用:

```bash
curl -X POST http://localhost:8000/v1/video_vibe_query \
  -H "Content-Type: application/json" \
  -d '{"query": "Find every video that contains skiing or snowboarding."}'
```

> 💡 想用真实 AlloyDB?去掉 `REPL_USE_MOCK_DB`,改设 `ALLOYDB_PASSWORD`。
> 想在终端用 CLI 而非 HTTP?运行 `python -m pipeline.main`。

---

## 调用 API

**`POST /v1/video_vibe_query`**

```jsonc
// 请求(省略 session_id 即开新会话)
{ "query": "Plot start time vs. confidence for those.", "session_id": "ab12cd34…" }

// 响应
{
  "ok": true,
  "status": "ok",                                  // ok · refused · error · smalltalk
  "session_id": "ab12cd34…",                       // 下一轮把它带回来即可续聊
  "turn_type": "followup",                         // new · followup · meta
  "answer": { "n_points": 45 },
  "dag": { "nodes": [ /* 实际执行的计划 */ ] },
  "generated_code": { "n2": "import json ..." },   // 真正跑过的代码
  "plot_url": "http://localhost:8000/plots/bb9ab8e1.svg",
  "trace": [ /* 每一步,带耗时 */ ],
  "trace_summary": "trace: 4/4 steps ok, total 53578ms"
}
```

图表直接由 API 提供 —— 在浏览器打开 `plot_url` 即可。

把响应里的 `session_id` 在下一次请求带回来,就能**接着聊** —— *"把那批画出来"*、*"你刚才怎么算的?"*
这类追问会对照之前真正算过的结果去解析。只有指代实在对不上任何真实结果时,响应才返回
`"status": "refused"` 和一句大白话 `reason` —— 绝不编造答案。

### 可以试试这些问题

| 这样问 | 你会得到 |
|----------|---------|
| `How many videos are in the database?` | 一个计数 |
| `Find every video that contains skiing or snowboarding.` | 一个筛选列表 |
| `Plot start time vs. confidence for all confirmed activities.` | 一个散点图 URL |
| `Show the distribution of confidence scores in 0.1 buckets.` | 一个直方图 |
| `Take the top-3 skiing clips, align them with heart-rate data, resample to 10 Hz, and run an OLS regression.` | 一个回归结果 + 代码 |

---

## 项目结构

```
pipeline/     查询引擎:router · planner · code generator · executor · orchestrator
api/          FastAPI 服务(POST /v1/video_vibe_query)
sandbox/      隔离代码执行服务(Cloud Run + gVisor)
mcp_server/   基于真实 schema 的数据库访问(经 MCP)
perception/   从视频中做多模态事实抽取
ingestion/    视频 → 转码 → 云存储 → 元数据
```

---

<div align="center">
<sub>基于 Gemini、MCP 与自愈代码沙箱,构建于 Google Cloud。</sub>
</div>
