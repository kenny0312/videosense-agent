# τ²-video Eval Results — VideoSense

*[中文版 / Chinese version](RESULTS.zh-CN.md)*

A τ²-bench-style evaluation of VideoSense (VS) — a multi-turn, multimodal video-understanding
agent whose brain is Gemini. The **real agent** (live Gemini in the loop) is the system under
test; every backend is swapped for a hermetic fake world, so no production data is touched and
runs are reproducible.

## Dataset

**145 tasks** (single- and multi-turn, 47 must-pass), gold labels strictly grounded in the fake
corpus (16 videos, phase-annotated skydive segments, ~50 verified facts including negatives).
`python -m evals.validate_tasks` → 0 issues, and it also checks every scoring key has matching
config so a mis-wired task can't silently score 0.

| Dimension | Probes |
|---|---|
| retrieval / honesty | find the right videos; say "none" only when truly absent; never claim "none" for broad categories it has; negative facts (e.g. *no* helmet) |
| count / timestamp | counting, dedup (DISTINCT trap), aggregation; temporal grounding scored by IoU |
| toolcall / display | right tool for the job (play vs table vs SQL vs web-search vs memory); decline irrelevant asks; deliver via `show_*`, keep raw ids out of prose |
| coherence | multi-turn anaphora, ellipsis, correction, constraint accumulation |
| dualcontrol | user mutates shared state mid-conversation — upload / ingest / paste-image — and the fake world really changes |
| vision | questions answerable only by "looking" (color, headcount); analyze_video is replayed from a fact sheet since the fake corpus has no real files |
| safety / identity / selfknow | prompt-injection refusals, no provider leakage, honest "I can't get that" for unbuilt features |

## How scoring works

Deterministic verifiers only on the gate (no LLM judge deciding pass/fail): tool-call audit,
delivery-surface recall+precision (answer **plus** `show_*` side channel — matching the product
rule that raw ids stay out of prose), timestamp IoU, exact counts, refusal/honesty checks,
id/provider-leak detectors, multi-turn slot tracking, and world-state assertions. Each task
declares a `reward_basis`; only those verifiers gate it. An LLM judge (cross-family Claude) can
score open-ended `nl_assertions` for **reference only** — never on the gate, until calibrated
against human labels.

## Live baseline (real Gemini, 143 scored tasks, n=1)

**131 / 143 passed (92%)**, 95% confidence range ≈ 86–95% (after the batch-⑤ scorer calibration,
2026-07-08). Two infra errors are excluded, not counted as model failures.

| Dimension (tasks where the verifier scored full marks) | Pass | Rate |
|---|---|---|
| safety · identity · timestamp | 9/9 · 5/5 · 22/22 | 100% |
| multi-turn memory (jga) · world-state | 29/29 · 9/9 | 100% |
| required tool use | 116/121 | 96% |
| no overreach (no_call) | 18/19 | 95% |
| honesty | 30/32 | 94% |
| count | 33/36 | 92% |
| no raw-id leak | 8/9 | 89% |
| retrieval | 29/34 | 85% |
| entity match | 4/5 | 80% |

Must-pass: 46/47 (the one miss, toolcall-table-all-videos-23, looks like a gold-design weakness —
a "list everything as a table" task using "did the prose say 16" as a completeness proxy; queued
for the next triage round). Retrieval remains the most real weakness — see below.

### How the number got trustworthy

Two live runs, both n=1. Run 1 scored 82%; run 2 scored 88% after we fixed **scorer** bugs, not
the agent. The paired comparison (only tasks that flipped) put the improvement at p≈0.04 — small
enough to call a real change, not luck. That loop — run, triage each failure into *real defect /
gold error / scorer bug / infra noise*, fix the tooling, re-run — is what turned a rough prototype
into a measuring instrument. The scorer fixes are frozen as golden-replay unit tests so they can't
regress.

### Batch ⑤ wrongful-conviction cleanup (2026-07-08): 88% → 92% (calibration, not agent gains)

A multi-agent review of the 88% baseline's 17 failures (44 reviewer/refuter/auditor agents; every
"wrongful conviction" claim had to survive two adversarial refuters) found **7 wrongful convictions**
(scorer bugs or gold errors — including one must-pass task that was literally impossible: the scorer
didn't support the `a|b` alternative syntax) and **upheld 10 real defects**. All fixes landed on the
eval side (6 scorer fixes + 9 gold corrections + 2 task-authoring lints + a failure-concentration
alarm). Re-run: **131/143 (92%)** — all 7 vindicated tasks turned green, real defects stayed red.
Those 4 points are the ruler getting accurate, not the agent improving — the scorer fingerprint
changed, so old and new scores are not comparable. Process lessons: **when a scoring principle
changes, sweep the whole suite** (fixing only the task that triggered it leaves its twins as the
next round's wrongful convictions); **when failures pile up on one dimension, suspect the eval first**.

## Real defects the eval caught

1. 🔴 **Blank answers** (new, product-level bug): when the Gemini API safety-blocks a response,
   `loop_driver` passes the empty reply straight through — the user gets a blank screen instead of
   a refusal. Hit 3 random tasks this run, 1 last run; currently the biggest source of flaky
   failures. Fix located: read `finish_reason` in `GenAIConversation.send` + fall back on empty
   answers in `orchestrator.py`.
2. 🟠 **Retrieval recall by vibes, no fallback sweep**: scene/category questions filter guessed
   predicate keywords only — never search titles, never scan the (16-video!) library — so
   reachable right answers get missed. park / outdoor-scenery / tutorial / fastpaced flicker
   run to run.
3. 🟠 **Prompt injection & identity leakage**: fixed via prompt hardening, verified green on a
   live run; pinned tasks stand guard as regressions.
4. 🟡 **Occasional raw id in prose**: a deep-compare answer wrote `sky01` into the text (product
   rule: ids go through the side channel).
5. 🟡 **Overreach**: asked about the weather, it actually called web_search — against its own
   tool-boundary spec.
6. 🟡 **Cost self-knowledge**: a known unbuilt feature; the agent honestly says it can't get the
   number (which the eval now scores as correct, until the feature ships).

## Reproduce

```bash
python -m evals check      # validate gold grounding + config
python -m evals            # free scripted lane + local dashboard
python -m evals live       # real-Gemini lane (needs GCP creds, costs tokens)
python -m evals serve      # local web console: run with buttons, auto-refresh
```

The dashboard (bilingual, `dashboard.html` / `dashboard.en.html`) shows trend, per-dimension
deltas, newly-passing/failing tasks vs the previous run, and a click-to-expand card for every
failure (question / answer / expected / tool calls / gold basis). Nothing is pushed to GitHub to
read results.
