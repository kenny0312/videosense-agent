<div align="center">

<img src="docs/hero.svg" alt="VideoSense — 随便问你的视频库，它用证据回答" width="100%" />

<br/><br/>

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/) [![Gemini 2.5](https://img.shields.io/badge/Gemini%202.5-1C69FF?logo=googlegemini&logoColor=white)](https://deepmind.google/technologies/gemini/) [![Postgres + pgvector](https://img.shields.io/badge/Postgres%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)](https://github.com/pgvector/pgvector) [![Cloud Run](https://img.shields.io/badge/Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/run) [![BigQuery](https://img.shields.io/badge/BigQuery-669DF6?logo=googlebigquery&logoColor=white)](https://cloud.google.com/bigquery)

它看你的视频、对看到的内容推理，并用可播放的片段和图表来证明它的回答。

### [▶ 在线体验 — **videosense.work**](https://videosense.work)

<sub>[English](README.md) · **简体中文**</sub>

</div>

<br/>

<div align="center">
  <a href="https://kenny0312.github.io/demo/videosense.html"><img src="docs/demo-replay.svg" alt="一段真实会话的回放：问题「哪些片段只有自由落体」被逐字打出，agent 流式展示工具步骤（含一次自我修复），然后给出「3 of 12」的结论和三个真实可播放的跳伞片段。" width="860" /></a>
  <br/><br/>
  <sub>一段真实会话的回放——问题被逐字打出，agent 流式跑完工具步骤（含一次自我修复），然后用<b>库里三段真实视频</b>作答。&nbsp;<a href="https://kenny0312.github.io/demo/videosense.html"><b>▶ 玩可交互版 demo</b></a></sub>
</div>

<br/>

## 答案——以及它花了多少钱

大白话提问；先给结论，再给证明它的片段和数字。每条回复的页脚都安静地带着收据：步骤、工具、耗时——还有花费。

<div align="center">
  <img src="docs/answer-card.svg" alt="一个完整的 VideoSense 回答：「3 of 12 个片段是纯自由落体」、三张带时间码的片段卡、一个自由落体对比开伞的小图表，页脚是 Steps 12 · sql ×2 · watch ×3 · 8.4s · $0.0535 · 92k tok" width="860" />
</div>

<br/>

## 它是怎么回答的

<div align="center">
  <img src="docs/loop.svg" alt="VideoSense 的 agent 循环：Gemini 2.5 大脑决定下一步、调用工具、读结果、重复直到能证明答案——周围是它的工具：看视频、查事实、语义检索、展示片段和表格、画图、跑代码。护栏：只读 SQL、答案里的 id 会被清洗、每个请求都计量成本。" width="760" />
</div>

<br/>

没有写死的流水线：模型自己决定每一步，直到能**证明**答案为止，每一步都实时流式返回。有一次分析中途缺库，它自己重写代码把活干完——[看那次运行](docs/DEMO.md)。

<sub>想看内部实现？架构笔记在 [`docs/design/`](docs/design/)。</sub>

<br/>

## 30 秒跑起来

```bash
export GCP_PROJECT="your-gcp-project"
export REPL_USE_MOCK_DB=1
uvicorn api.server:app --port 8000        # 然后打开 http://localhost:8000
```

<sub>不需要数据库、零成本——内置示例视频库。只需 <code>gcloud auth application-default login</code> 用于调用 Gemini。</sub>

<br/>

## 30 秒跑评测

VideoSense 自带一套 τ²-bench 风格的评测——**128 道题**、8 个能力维度，用确定性校验器打分（不用 LLM 裁判）。跑完自动弹出本地仪表盘，看通过率、各维度分数、以及跨轮次的走势。

```bash
python -m evals serve      # 本地控制台——点按钮跑，仪表盘自动刷新
# python -m evals          # 脚本车道（免费，不花 token）
# python -m evals live     # 真 Gemini 车道（花 token）
# Windows cmd 快捷方式：`eval serve`（仓库根的 eval.bat；PowerShell 里写 .\eval serve）
```

<div align="center">
  <img src="docs/eval-dashboard.svg" alt="VideoSense 评测仪表盘：真 Gemini 车道 96 道单轮题通过率 78%（属于 128 题套件，确定性校验器），各维度通过率——时间戳 100%、计数 95%、工具选择 80%、检索 58%、诚实性 52%。" width="760" />
</div>

<sub>真 Gemini 车道稳定在 <b>78%</b> 基线——而且上线第一天就抓到了真 bug（一次提示注入泄漏、一次身份暴露），现已固化成回归题。完整报告见 <a href="evals/RESULTS.zh-CN.md">evals/RESULTS.zh-CN.md</a>。</sub>

<br/>

<div align="center">

<sub>由 <a href="https://kenny0312.github.io">Kenny Qiu</a> 构建 &nbsp;·&nbsp; 另见 <a href="https://github.com/kenny0312/social-video-insights">SocialLens</a>——社媒视频洞察 demo &nbsp;·&nbsp; <a href="README.md">English</a></sub>

</div>
