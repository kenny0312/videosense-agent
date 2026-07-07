<div align="center">

<img src="docs/hero.svg" alt="VideoSense — ask your video library anything; it answers with proof" width="100%" />

<br/><br/>

<img src="docs/proof-strip.svg" alt="146 tests passing · live on Cloud Run · τ²-bench eval 78% · Gemini 2.5 multimodal" width="760" />

<br/><br/>

It watches your videos, reasons about what it sees, and answers with the clip and the chart to prove it.

<sub>**English** · [简体中文](README.zh-CN.md)</sub>

</div>

<br/>

<div align="center">
  <a href="https://kenny0312.github.io/demo/videosense.html"><img src="docs/demo-replay.svg" alt="A replayed VideoSense session: the question 'which clips show only freefall' is typed, the agent streams its tool steps — including a self-repair — then answers '3 of 12' with three real, labelled skydiving clips and a quiet Steps footer." width="860" /></a>
  <br/><br/>
  <sub>A real session, replayed — the question is typed, the agent streams its tool steps (one self-repair included), then answers with <b>three real clips</b> from the library. &nbsp;<a href="https://kenny0312.github.io/demo/videosense.html"><b>▶ Play the interactive demo</b></a></sub>
</div>

<br/>

## The answer — and what it cost

Ask in plain language; get the conclusion first, the clips that prove it, and the numbers behind the claim. Every reply carries its receipts in a quiet footer: steps, tools, seconds — and dollars.

<div align="center">
  <img src="docs/answer-card.svg" alt="A finished VideoSense answer: '3 of 12 clips are pure freefall', three labelled clip cards with timecodes, a freefall-vs-canopy mini chart, and a footer reading Steps 12 · sql ×2 · watch ×3 · 8.4s · $0.0535 · 92k tok" width="860" />
</div>

<br/>

## How it answers

<div align="center">
  <img src="docs/loop.svg" alt="The VideoSense agent loop: a Gemini 2.5 brain decides, calls a tool, reads the result, and repeats until proven — surrounded by its tools: watch a video, query the facts, semantic search, show clips and tables, draw a chart, run code. Guardrails: read-only SQL, ids scrubbed, every request metered." width="760" />
</div>

<br/>

No pre-baked pipeline: the model picks every next move and keeps going until it can *prove* an answer, streaming each step back live. When a library went missing mid-analysis, it rewrote its own code and finished the job — [see that run](docs/DEMO.md).

<sub>Curious about the internals? Architecture notes live in [`docs/design/`](docs/design/).</sub>

<br/>

## Run it in 30 seconds

```bash
export GCP_PROJECT="your-gcp-project"
export REPL_USE_MOCK_DB=1
uvicorn api.server:app --port 8000        # then open http://localhost:8000
```

<sub>No database, no cost — a built-in sample library. You only need <code>gcloud auth application-default login</code> for Gemini.</sub>

<br/>

<div align="center">

<sub>Built by <a href="https://kenny0312.github.io">Kenny Qiu</a> &nbsp;·&nbsp; see also <a href="https://github.com/kenny0312/social-video-insights">SocialLens</a>, a social-video insights demo &nbsp;·&nbsp; <a href="README.zh-CN.md">简体中文</a></sub>

</div>
