# DRIFTZERO — recording runbook

Follow this in order. It assumes the deployed service is up and produces exactly **one**
live hero run.

**The rule that governs everything below:** never capture a success the backend has not
actually produced. If a step does not show the expected state, stop and read
[Troubleshooting](#troubleshooting) — do not re-shoot until it does.

---

## 1. Before you press record

**Services**

| | Check | Expected |
| --- | --- | --- |
| Cloud Run | `gcloud run services describe driftzero-api --project=driftzero-runtime-2026 --region=us-central1 --format='value(status.url,status.latestReadyRevisionName)'` | a URL and a `READY` revision |
| Auth | `gcloud auth print-identity-token` | a token (never shown on camera) |
| Firestore | live | reachable |

The service is **private**. A browser cannot open it without an identity token, so use
the local run for the on-camera browser (step 3) and keep the deployed service for the
architecture/evidence beat.

**Do not** run `scripts/m2_exit_gate`, `m3_exit_gate`, or any cleanup script during the
session — the first two are slow and the third is destructive.

**Terminal hygiene:** clear scrollback, set a large font, and make sure no token is in
history. Never put an identity token on screen.

---

## 2. Rehearse offline — no model calls, no cost

Rehearse the whole thing as many times as you like in deterministic mode. It is visually
identical and spends nothing.

```bash
python -m scripts.m6_product_evidence     # drives the real flow with substitutes
```

Rehearse until the click path is muscle memory. **Then** do step 3 once.

---

## 3. The one live run

```bash
export DRIFTZERO_FIELD_PROVIDER=vertex_maas
export DRIFTZERO_GCP_PROJECT=driftzero-runtime-2026
export DRIFTZERO_SEMANTIC_PROVIDER=google_adk
export DRIFTZERO_GEMINI_MODEL=gemini-3.5-flash
export DRIFTZERO_GEMINI_LOCATION=global
export DRIFTZERO_PERSISTENCE=firestore
export DRIFTZERO_EVIDENCE_BUCKET=driftzero-evidence-driftzero-runtime-2026

python -m uvicorn driftzero_api.app:app --port 8080
```

Budget: **3 live model calls.** One Gemini (change analysis), two Gemma (LEFT, TOP_RIGHT).

### Browser tabs, left to right

| Tab | URL | Used in |
| --- | --- | --- |
| 1 | `http://127.0.0.1:8080/web/delta?workflow=<ID>` | beats 4–5 |
| 2 | `http://127.0.0.1:8080/web/verify?workflow=<ID>` | beat 5 |
| 3 | `http://127.0.0.1:8080/web/proof?workflow=<ID>` | beat 6 |
| 4 | `docs/architecture.md` rendered | beat 7 |
| 5 | `evidence/JUDGES_START_HERE.md` | end card / B-roll |

Put tab 1 and 2 in a **mobile viewport** (375 × 812). That is the frontline surface.

### The steps

| # | Action | Expected on screen — do not proceed otherwise |
| --- | --- | --- |
| 1 | `POST /api/v1/changes` with `fixtures/hero_change.json` | `201`, a `workflow_id`, `state: CHANGE_RECEIVED` |
| 2 | Analyze (Gemini — **live call 1**) | 5 candidates proposed, **exactly 1** qualified: `wi-packing-standard-001` |
| 3 | Check the decoy | `wi-forklift-turn-014` still reads `turn_direction: LEFT` |
| 4 | Deploy the remediation | artifact now `label_position: TOP_RIGHT`; capability shown as granted |
| 5 | Deliver to frontline | tab 1 shows **Your work has changed**, `Was LEFT` → `Now TOP_RIGHT` |
| 6 | Tab 2 → upload `fixtures/multimodal/label_left_01.jpg` (**live call 2**) | "Checking your photo" → **Not done yet**, red ✖, retry offered |
| 7 | Upload `fixtures/multimodal/label_top_right_01.jpg` (**live call 3**) | "Checking your photo" → **Verified**, green ✔ |
| 8 | Tab 3 → the proof | `7/7`, `PROOF_COMPLETE`, chronology `FAIL → PASS`, proof id, content hash |
| 9 | Click **Verify integrity** | "Content hash matches" — recomputed in the browser |

**Use `label_left_01.jpg` first and `label_top_right_01.jpg` second. Never the ambiguous
fixture** — it measured ~90 s and adds nothing to the story.

---

## 4. Do not click

- **Verify integrity** before beat 6 — it is the payoff, don't spend it early
- Anything under **Audit detail** on camera — raw JSON is the anti-narrative
- The **/ready** endpoint on camera — it correctly says `production_ready: false`, which
  needs a sentence of context the video has no room for
- Any **cleanup or shutdown** script — it would take the demo URL down
- **Browser back** after a verdict — re-fetching is fine, but don't imply a state change
  that did not happen

---

## 5. Latency

Gemini takes a few seconds; Gemma took **2.1 s** and **2.5 s** on the recorded fixtures.

That is short enough to keep in the cut. If a call is slower on the day:

- **Do** cut the waiting time in the edit — the "Checking your photo" state is real, and
  trimming its duration changes nothing about what happened
- **Do not** pre-render a result, splice a verdict from a different run, or reorder
  photo #2 before photo #1

If a call fails outright, the workflow is unharmed: submit again. A retry produces a new
attempt and the chronology stays honest.

---

## 6. Fallbacks

| If | Then |
| --- | --- |
| Gemini is slow or erroring | Rehearsal mode is visually identical. Record beats 3–4 there, label nothing as live, and keep the two Gemma calls live — they are the ones that matter |
| Gemma is unavailable | Use `evidence/m6/worker_failed.png` and `worker_verified.png` as stills, and say "recorded run" in the voiceover |
| The whole live path is down | The entire story is already evidenced: `evidence/runs/hero_run_001/real_camera_hero_run.json` is a real camera run with real Gemma. Narrate over the recorded evidence and say so |

In every fallback: **say what the viewer is looking at.** A recorded run presented as live
is the one thing that would undermine the whole submission.

---

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| `403` on every route | expected — the deployed service is private | use the local run for on-camera browsing |
| Worker page shows "Not available yet" | delivery has not completed | finish step 5 first |
| Verify returns `INCONCLUSIVE` | field provider not configured | re-export `DRIFTZERO_FIELD_PROVIDER=vertex_maas` and restart |
| Verify returns `409` | that workflow is held elsewhere | create a fresh change and start again |
| Proof returns `404` | fewer than 7 conditions hold | check the latest verification is `PASS` |
| Impact qualifies 0 targets | the semantic provider is unset | check `DRIFTZERO_SEMANTIC_PROVIDER=google_adk` |

---

## After recording

1. Note the `workflow_id`, the proof id and the content hash — they appear on screen and
   should match what you can show in Firestore afterwards if asked.
2. Do **not** delete the workflow. It is the run the video shows.
3. Leave the deployed service running until judging closes.
