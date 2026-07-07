<div align="center">

<img src="docs/hero.svg" alt="VideoSense — 随便问你的视频库，它用证据回答" width="100%" />

<br/><br/>

[English](README.md) · **简体中文**

### 你的视频里藏着答案。VideoSense 帮你找到它——<br/>它真的看视频、自己推理，并用可播放的片段和图表来证明。

</div>

<br/>

<div align="center">
  <a href="https://kenny0312.github.io/demo/videosense.html"><img src="docs/demo-replay.svg" alt="一段真实会话的回放：问题「哪些片段只有自由落体」被逐字打出，agent 流式展示工具步骤（含一次自我修复），然后给出「3 of 12」的结论和三个真实可播放的跳伞片段。" width="860" /></a>
  <br/><br/>
  <sub>一段真实会话的回放——问题被逐字打出，agent 流式跑完工具步骤（含一次自我修复），然后用<b>库里三段真实视频</b>作答。&nbsp;<a href="https://kenny0312.github.io/demo/videosense.html"><b>▶ 玩可交互版 demo</b></a></sub>
</div>

<br/>

## 🧾 一段真实会话，未经剪辑

有一说一，证据随附。大白话提问，拿到结构化回答——每条回复都带一个安静的 **Steps** 页脚，完整的推理过程一键展开。

<img src="docs/shot-answer.png" alt="一个真实回答——找到三条视频，回复自带 Steps 页脚" width="100%" />

<br/>

### 💸 成本也是产品的一部分

每个会话都在输入框旁实时计量自己的花费——token、美元、预算环。生产环境同一套遥测流入 BigQuery。

<img src="docs/shot-cost.png" alt="输入框旁的实时成本计量——美元、token、预算环" width="100%" />

<br/>

<div align="center">

### 🚀 30 秒跑起来——免费 mock 模式

</div>

```bash
export GCP_PROJECT="your-gcp-project"  REPL_USE_MOCK_DB=1
uvicorn api.server:app --port 8000        # 然后打开 http://localhost:8000
```

<sub>不需要数据库、零成本——内置示例视频库。只需 <code>gcloud auth application-default login</code> 用于调用 Gemini。</sub>

<br/>

## 🧠 它是怎么回答的

没有写死的流水线。一个以 **Gemini 2.5** 为大脑的 agent 循环自己决定下一步——看视频、查它抽取过的事实、语义检索、跑计算、画图——直到能**证明**一个答案为止，每一步都实时流式返回。它跨会话记得你、按请求计量自己的成本，并带着 **146 个测试**跑在 Cloud Run 生产环境上。

<sub>想看内部实现？架构笔记在 [`docs/design/`](docs/design/)。</sub>

<br/>

<div align="center">

<sub>由 <a href="https://kenny0312.github.io">Kenny Qiu</a> 构建 &nbsp;·&nbsp; 另见 <a href="https://github.com/kenny0312/social-video-insights">SocialLens</a>——社媒视频洞察 demo &nbsp;·&nbsp; <a href="README.md">English</a></sub>

</div>
