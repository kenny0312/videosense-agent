# Structured Video Understanding via Multimodal LLM Predicates

> An end-to-end pipeline that transforms raw video streams into queryable, structured knowledge — using Gemini as a multimodal perception engine, MCP as a standardized database protocol, and a declarative DAG planner to answer natural language queries over video facts.

---

## Overview

Traditional video retrieval systems rely on metadata tags or transcript search. This project explores a different approach: **atomizing video content into structured predicates** — fine-grained, confidence-scored facts extracted directly from pixels by a multimodal LLM — and building an agentic query layer on top.

The system is designed around three core ideas:

1. **Perception as predicate evaluation.** Rather than generating free-form captions, Gemini 2.5 evaluates each video against specific activity predicates (e.g., *"snowboarding down a slope"*, *"applying mascara"*) and returns a structured `PredicateResult` with confidence, rationale, and timestamp bounds.

2. **Schema-grounded query planning.** A Planner LLM receives the real database schema via MCP, then compiles natural language questions into executable JSON DAGs — eliminating hallucinated column names and invalid SQL.

3. **Safe agentic execution.** Generated code runs inside an isolated sandbox (Cloud Run + gVisor), with a self-healing REPL loop that feeds tracebacks back to the LLM for automatic correction.

---

## Architecture

```
User Query (natural language)
        │
        ▼
┌───────────────────┐
│   Stage 4: Planner│  ← Gemini 2.5 Flash + MCP Schema
│   NL → JSON DAG   │
└────────┬──────────┘
         │
         ▼
┌───────────────────┐     ┌──────────────────────┐
│  Stage 3: MCP     │────▶│  AlloyDB (PostgreSQL) │
│  get_schema()     │     │  video_metadata       │
│  query_db(sql)    │     │  video_facts (1,084)  │
└───────────────────┘     └──────────────────────┘
         │
         ▼
┌───────────────────┐
│  Stage 5: Sandbox │  ← FastAPI + Cloud Run + gVisor
│  POST /execute    │
└───────────────────┘
         │
         ▼
┌───────────────────┐
│  Stage 2: Gemini  │  ← Vertex AI, direct GCS URI
│  Predicate Engine │     582 unique predicates
└───────────────────┘     87 videos · avg confidence 0.942
```

---

## Dataset

- **Source:** ActivityNet (subset, 100 videos)
- **Storage:** Google Cloud Storage (`gs://activitynet/activitynet/720p/`)
- **Format:** 720p H.264 MP4, transcoded via ffmpeg
- **Metadata:** Stored in AlloyDB (`video_metadata`, 100 rows)
- **Facts:** 1,084 predicate evaluation records across 87 videos (`video_facts`)

---

## Implementation Stages

| Stage | Component | Description | Status |
|-------|-----------|-------------|--------|
| 1 | **GCP Foundation** | Video download → 720p transcode → GCS upload → AlloyDB metadata | ✅ Complete |
| 2 | **Gemini Predicates** | Multimodal predicate evaluation via Vertex AI; structured `PredicateResult` output | ✅ Complete |
| 3 | **MCP Server** | stdio-based MCP server exposing `get_schema()` and `query_db()` | ✅ Complete |
| 4 | **DAG Planner** | LLM compiles natural language to executable JSON DAG; topological execution engine | ✅ Complete |
| 5 | **Sandbox Engine** | Isolated FastAPI execution environment on Cloud Run with gVisor | 🔜 In Progress |
| 6 | **Agentic REPL** | Self-healing code loop: generate → execute → parse traceback → retry | ⏳ Planned |
| 7 | **Data Engineering** | Cross-modal ETL: sensor CSV ↔ video facts via `pandas.merge_asof` | ⏳ Planned |
| 8 | **Temporal Alignment** | Multi-rate resampling (1 fps video ↔ 100 Hz sensor) via `scipy.interpolate` | ⏳ Planned |
| 9 | **Dynamic Simulation** | Exploratory threshold sweeps; OLS regression on visual features | ⏳ Planned |
| 10 | **Orchestration** | Production FastAPI `POST /v1/video_vibe_query` with full pipeline | ⏳ Planned |

---

## Key Design Decisions

**Why predicate-based perception over captioning?**
Free-form captions are hard to query at scale. Predicates produce boolean + confidence outputs that map directly to SQL `WHERE` clauses, enabling precise retrieval without embedding search.

**Why MCP for database access?**
Giving the Planner LLM raw database credentials leads to hallucinated schema. The MCP `get_schema()` tool injects the real column names into the LLM context at query time, grounding every generated SQL statement in the actual schema.

**Why a DAG over direct SQL generation?**
A DAG intermediate representation separates *planning* from *execution*, making it easier to inspect, debug, and extend the query logic without re-prompting the LLM.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Video Storage | Google Cloud Storage |
| Database | AlloyDB for PostgreSQL |
| Multimodal LLM | Gemini 2.5 Flash (Vertex AI) |
| DB Protocol | MCP Python SDK (stdio) |
| API Framework | FastAPI |
| Execution Sandbox | Cloud Run + gVisor |
| Orchestration | Python state machine / LangGraph |

---

## Repository Structure

```
├── ingestion/
│   └── download_transcode_upload.py  # Download ActivityNet → 720p transcode → GCS → AlloyDB
│
├── perception/
│   ├── gemini_predicates.py          # Gemini 2.5 predicate evaluation pipeline
│   └── setup_schema.py               # Create video_facts table in AlloyDB
│
├── mcp_server/
│   └── server.py                     # MCP stdio server (get_schema, query_db)
│
├── planner/
│   └── dag_planner.py                # Natural language → JSON DAG → execution engine
│
├── sandbox/                          # Stage 5 — isolated code execution (planned)
│
├── utils/
│   ├── test_connections.py           # Verify GCS + AlloyDB connectivity
│   ├── inspect_facts.py              # Inspect video_facts table stats
│   └── inspect_db.py                 # List all tables and row counts
│
├── requirements.txt
├── .env.example
└── .claude/
    └── launch.json                   # Dev server launch configurations
```

---

## Environment Setup

```bash
# Python dependencies
pip install psycopg2-binary google-cloud-storage google-cloud-aiplatform mcp

# GCP authentication
gcloud auth application-default login
gcloud config set project your-gcp-project-id
gcloud auth application-default set-quota-project your-gcp-project-id

# AlloyDB password (never hardcode)
export ALLOYDB_PASSWORD=<your_password>
```
