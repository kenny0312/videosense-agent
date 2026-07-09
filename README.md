<div align="center">

<img src="docs/hero.svg" alt="VideoSense — ask your video library anything; it answers with proof" width="100%" />

<br/><br/>

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/) [![Gemini 2.5](https://img.shields.io/badge/Gemini%202.5-1C69FF?logo=googlegemini&logoColor=white)](https://deepmind.google/technologies/gemini/) [![Postgres + pgvector](https://img.shields.io/badge/Postgres%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)](https://github.com/pgvector/pgvector) [![Cloud Run](https://img.shields.io/badge/Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/run) [![BigQuery](https://img.shields.io/badge/BigQuery-669DF6?logo=googlebigquery&logoColor=white)](https://cloud.google.com/bigquery)

It watches your videos, reasons about what it sees, and answers with the clip and the chart to prove it.

### [▶ Try it live — **videosense.work**](https://videosense.work)

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

## Eval it in 30 seconds

VideoSense ships with its own τ²-bench-style evaluation — **128 tasks** across 8 capability dimensions, scored by deterministic verifiers (no LLM judge). Run it and a local dashboard opens with the pass rate, per-dimension scores, and the trend across runs.

```bash
python -m evals serve      # local console — click to run, dashboard auto-refreshes
# python -m evals          # scripted lane  (free, no tokens)
# python -m evals live     # real-Gemini lane  (spends tokens)
# Windows cmd shortcut: `eval serve` (repo-root eval.bat; PowerShell: .\eval serve)
```

<div align="center">
  <img src="docs/eval-dashboard.svg" alt="VideoSense eval dashboard: 78% pass on the real-Gemini lane over 96 single-turn tasks (part of a 128-task suite, deterministic verifiers), with per-dimension pass rates — timestamp 100%, counting 95%, tool-use 80%, retrieval 58%, honesty 52%." width="760" />
</div>

<sub>On the real-Gemini lane it holds a <b>78%</b> baseline — and it caught real bugs on day one (a prompt-injection leak, an identity slip), now pinned as regression tasks. Full write-up in <a href="evals/RESULTS.md">evals/RESULTS.md</a>.</sub>

<br/>

<div align="center">

<sub>Built by <a href="https://kenny0312.github.io">Kenny Qiu</a> &nbsp;·&nbsp; see also <a href="https://github.com/kenny0312/social-video-insights">SocialLens</a>, a social-video insights demo &nbsp;·&nbsp; <a href="README.zh-CN.md">简体中文</a></sub>

</div>
