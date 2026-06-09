# 🎬 VideoSense Agent — Demo Walkthrough

A single natural-language question, compiled into a multi-step plan, executed and
**self-healed** end to end. This is a real `POST /v1/video_vibe_query` response.

---

## The question

> **"Find the 3 highest-confidence skiing clips, generate simulated heart-rate sensor
> data for them, align them by time with `merge_asof`, resample to a unified 10 Hz
> timeline, then run an OLS regression of action confidence against heart rate and
> output the coefficients."**

---

## What the agent did

1. **Planned** a 5-node graph — no code written yet, just the plan:
   `sql_query → load_sensor_csv → merge_asof → interpolate → ols_regress`
2. **Generated** Python for each analytical step on the fly.
3. **Self-healed** when steps failed — most notably, the sandbox has **no `scipy` or
   `statsmodels`**, so the agent caught the `ModuleNotFoundError` and **rewrote the code
   using `numpy`/`math`** instead, on its own.
4. **Returned** the regression result *and* the exact code that produced it.

---

## The answer

```json
{
  "params":     { "const": 0.9274, "heart_rate": 0.00030 },
  "r_squared":  0.1017,
  "pvalues":    { "const": 0.0, "heart_rate": 1.76e-13 },
  "n":          481
}
```

> Across 481 time-aligned samples, heart rate is a statistically significant
> (p ≈ 1.8e-13) but weak (R² ≈ 0.10) predictor of action confidence.

---

## The plan (auto-generated DAG)

```json
{
  "nodes": [
    { "id": "n1", "tool": "sql_query",       "depends_on": [],
      "inputs": { "sql": "WITH top_clips AS (SELECT id FROM video_facts WHERE (predicate ILIKE '%skiing%' OR predicate ILIKE '%snowboarding%') ORDER BY confidence DESC LIMIT 3) SELECT vfi.ts, vf.confidence FROM video_fact_instances vfi JOIN video_facts vf ON vfi.fact_id = vf.id WHERE vfi.fact_id IN (SELECT id FROM top_clips) ORDER BY vfi.ts" } },
    { "id": "n2", "tool": "load_sensor_csv",  "depends_on": [],
      "inputs": { "columns": ["timestamp", "heart_rate"], "rows": 1000 } },
    { "id": "n3", "tool": "merge_asof",       "depends_on": ["n1", "n2"],
      "inputs": { "left_on": "ts", "right_on": "timestamp", "tolerance_ms": 100 } },
    { "id": "n4", "tool": "interpolate",      "depends_on": ["n3"],
      "inputs": { "target_hz": 10, "columns": ["confidence", "heart_rate"] } },
    { "id": "n5", "tool": "ols_regress",      "depends_on": ["n4"],
      "inputs": { "y": "confidence", "x": ["heart_rate"] } }
  ]
}
```

---

## Generated code (excerpt) — node `n2` · synthetic sensor data

```python
import json
import numpy as np
import random
import math

# Set a random seed for reproducibility
SEED = 42
random.seed(SEED)
np.random.seed(SEED)

# Read parameters from the injected `inputs` variable
rows = inputs.get("rows", 1000)
jitter_ms = inputs.get("jitter_ms", 100)   # default jitter when not provided

# 1. Timestamps: a 0,1,2,... base series plus random millisecond jitter
base_timestamps = np.arange(rows, dtype=float)
jitter_seconds = np.random.uniform(0, jitter_ms / 1000.0, size=rows)
timestamps = base_timestamps + jitter_seconds

# 2. Heart rate: a sine wave (cyclical rest/activity pattern) + Gaussian noise
hr_min, hr_max = 60, 160
hr_amplitude = (hr_max - hr_min) / 2
hr_center = hr_min + hr_amplitude
period = 900                                   # ~15-minute cycle
phase_shift = random.uniform(0, 2 * math.pi)

cyclical = hr_amplitude * np.sin(2 * math.pi * base_timestamps / period + phase_shift)
noise = np.random.normal(loc=0, scale=5, size=rows)
heart_rates = np.clip(hr_center + cyclical + noise, hr_min, hr_max)

# 3. Assemble records
records = [
    {"timestamp": round(ts, 3), "heart_rate": int(hr)}
    for ts, hr in zip(timestamps, heart_rates)
]

# 4. Emit as JSON for the downstream node
print(json.dumps(records))
```

> Nodes `n3`–`n5` were generated directly in English. Their comments even document the
> self-repair, e.g. node `n4`:
> `# np.interp ... used as a replacement for scipy.interpolate.interp1d due to ModuleNotFoundError`

---

## Execution trace — self-healing in action

| Step | Status | Note |
|------|--------|------|
| Plan DAG | ✅ | 5 nodes |
| `n1` sql_query (MCP) | ✅ | 124 rows, **4 ms** |
| `n2` load_sensor_csv | ✅ | first try |
| `n3` merge_asof | 🔁 → ✅ | failed once (timedelta dtype mismatch), auto-fixed |
| `n4` interpolate | 🔁 → ✅ | `scipy` missing → rewrote with `numpy.interp` |
| `n5` ols_regress | 🔁🔁🔁 → ✅ | `statsmodels` missing → rewrote OLS in pure `numpy`/`math` |

**15/20 steps green on the first pass; every failure was recovered automatically.**
The "failures" aren't bugs — they're the agent discovering its environment and adapting.

---

<div align="center">
<sub>Reproduce: <code>uvicorn api.server:app</code> → open <code>http://localhost:8000/docs</code> → paste the question above.</sub>
</div>
