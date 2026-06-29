<div align="center">

<img src="docs/hero.svg" alt="VideoSense" width="100%" />

<br/><br/>

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Gemini 2.5](https://img.shields.io/badge/Gemini%202.5-4285F4?logo=googlegemini&logoColor=white)](https://deepmind.google/technologies/gemini/)
[![Cloud Run](https://img.shields.io/badge/Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)](https://cloud.google.com/run)
[![tests](https://img.shields.io/badge/tests-146%20passing-3DA639?logo=pytest&logoColor=white)](#)
[![license](https://img.shields.io/badge/license-All%20Rights%20Reserved-lightgrey)](#)

**English** · [简体中文](README.zh-CN.md)

### Ask your video library in plain English — it watches, reasons, and answers,<br/>with the clip and the chart to prove it.

</div>

<br/>

<div align="center">
  <img src="docs/demo.gif" alt="VideoSense in action" width="820" />
</div>

<br/>

<div align="center"><h3>💬 You ask · it answers</h3></div>

| You ask… | …you get |
|:---|:---|
| 🪂 &nbsp;*“How many wingsuit videos are there?”* | **“12”** — it watched & phase-tagged each one |
| 🎬 &nbsp;*“Show me the shortest clip”* | plays it **inline** |
| 🔎 &nbsp;*“Which clips show only freefall, no parachute?”* | a filtered list, each **playable** |
| 📊 &nbsp;*“Plot the confidence distribution”* | a **chart** |
| 💬 &nbsp;*“List a few more · how did you get that?”* | **remembers** the conversation & continues |

<br/>

## ✨ Why it feels different

<table>
<tr>
<td width="33%" valign="top" align="center"><br/>💬<h3>Just ask</h3><sub>No SQL, no dashboards.<br/>Plain language in.</sub><br/><br/></td>
<td width="33%" valign="top" align="center"><br/>🧠<h3>It actually watches</h3><sub>Gemini multimodal reads the<br/>video — not just metadata.</sub><br/><br/></td>
<td width="33%" valign="top" align="center"><br/>🎬<h3>Answers you can see</h3><sub>The answer + the clip + the<br/>chart, and how it got there.</sub><br/><br/></td>
</tr>
</table>

<br/>

<div align="center">

### 🚀 Run it in 30 seconds — free mock mode

</div>

```bash
export GCP_PROJECT="your-gcp-project"  REPL_USE_MOCK_DB=1
uvicorn api.server:app --port 8000        # then open http://localhost:8000
```

<sub>No database, no cost — an in-memory sample library (16 videos + ~50 facts). You only need <code>gcloud auth application-default login</code> for Gemini.</sub>

<br/>

<details>
<summary><b>🏗️ How it works — in 3 steps</b></summary>

<br/>

> **1. You ask**　→　**2. The AI loops — watch / query / compute / plot, choosing each next step itself**　→　**3. Answer + clip + chart**

No pre-baked pipeline: a **probe-and-step loop** with **Gemini** as the brain picks the right tool at each step until it can answer, then streams the result back live. Architecture deep-dives live in [`docs/design/`](docs/design/).

</details>

<details>
<summary><b>🧰 Under the hood — data · tools · API · deploy · stack</b></summary>

<br/>

**Data model** — 5 tables (mock library = 16 videos + ~50 facts)

| Table | Key columns |
|---|---|
| `video_metadata` | `video_id · title · gcs_uri · duration_sec` |
| `video_discovery` | `video_id · all_activities` (JSON) |
| `video_facts` | `video_id · predicate · matched · confidence · start_ts · end_ts` |
| `video_fact_instances` | `id · fact_id · ts · frame_count` |
| `skydive_segments` | `video_id · per-phase *_start/_end/_confidence · jump_type · is_wingsuit · freefall_sec` |

**Tools the agent can call inside the loop**

`sql_query` · `threshold_sweep` · `show_video` · `analyze_video` · `merge_asof` · `interpolate` · `ols_regress` · `plot` · `python`

**API**

| Endpoint | Method | What |
|---|---|---|
| `/` | GET | chat UI |
| `/health` | GET | liveness probe |
| `/v1/video_vibe_query` | POST | sync query — `{query, session_id?}` |
| `/v1/video_vibe_query/stream` | POST | SSE streaming (step-by-step) |
| `/plots/{file}` | GET | generated chart images |

**Deploy** — Cloud Run, from source:

```bash
gcloud run deploy videosense --source . --region us-central1 \
  --allow-unauthenticated --memory 1Gi --timeout 300 --session-affinity \
  --set-env-vars "GCP_PROJECT=… ALLOYDB_*=… GCS_BUCKET=… SESSION_BACKEND=redis UPSTASH_*=… APP_ACCESS_KEYS=…"
```

> Merging to `main` does **not** auto-deploy and the URL stays the same — deploy is an explicit step. See [`docs/DEPLOY.md`](docs/DEPLOY.md) · [`docs/MONITORING.md`](docs/MONITORING.md).

**Project layout**

```text
api/        FastAPI + SSE             pipeline/    the probe-and-step engine
web/        single-page chat UI       perception/  offline Gemini fact extraction
sandbox/    isolated code execution   mcp_server/  schema-grounded DB access (MCP)
ingestion/  local/YouTube → GCS → DB  repl/        zero-cost in-memory mock DB
```

**Stack:** FastAPI · Google Cloud Run · Gemini 2.5 Pro/Flash (Vertex AI) · Neon Postgres · Upstash Redis · Google Cloud Storage · gVisor sandbox · MCP · Pydantic v2.

**Tests:** `REPL_USE_MOCK_DB=1 pytest` — **146 passing**.

</details>

<br/>

<div align="center"><sub>Built with Gemini · FastAPI · Cloud Run · MCP　·　<a href="README.zh-CN.md">简体中文</a></sub></div>
