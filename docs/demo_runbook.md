# DRIFTZERO — recording runbook

**The canonical recording flow is the public URL. Nothing local is used on camera.**

```
https://driftzero-web-eepb64ze2q-uc.a.run.app
```

No localhost. No `uvicorn`. No `gcloud run services proxy`. No identity token. A browser
and that address is the entire setup, which is also the point the video is making.

**The rule that governs everything:** never capture a success the backend has not
produced. If a step does not show the expected state, stop and read
[Troubleshooting](#troubleshooting) — do not re-shoot until it does.

---

## 1. Before you press record

| Check | Command or action | Expected |
| --- | --- | --- |
| Public surface is up | open the URL | the hero, and a **Run live pilot** button |
| Backend is answering | look at the status chip on `/` | `Private backend: SERVING` |
| Providers are live | `gcloud run services describe driftzero-api --project=driftzero-runtime-2026 --region=us-central1 --format="value(spec.template.spec.containers[0].env)"` | `google_adk` and `vertex_maas` present |

**Browser hygiene:** a clean window, no extensions bar, no bookmarks bar, no other tabs.
Nothing on screen should identify the operator.

**Do not** run any cleanup or shutdown script during the session.

---

## 2. Rehearse

Rehearse against the real thing. Each run costs one Gemini call and two Gemma calls, which
is small, and the flow is identical every time — so there is no reason to rehearse against
a substitute and then hope the real one behaves.

Run it two or three times until the click path is muscle memory, then record.

Every rehearsal creates a genuinely new workflow and a genuinely new proof. Nothing is
reused, and nothing is polluted by rehearsing.

---

## 3. The take

Five actions. That is the whole demo.

| # | Action | Expected on screen — do not proceed otherwise | Time |
| --- | --- | --- | --- |
| 1 | Open the URL | Hero: *"The autonomous last-mile for operational change"*, backend chip **SERVING** | 0:00 |
| 2 | Click **Run live pilot** → **Run live pilot** | ~20–25 s, then **Your work has changed**, five steps ticked, `Was LEFT → Now TOP RIGHT`, artifact `wi-packing-standard-001` | 0:20 |
| 3 | Click **Verify current state** | ~2–3 s, then **Not done yet**, `Observed: LEFT`, `Truth Engine verdict: FAIL`, retry offered, **no proof button** | 1:00 |
| 4 | Click **Verify corrected state** | ~2–3 s, then **Verified**, `Observed: TOP_RIGHT`, `PASS`, chronology `FAIL → PASS` | 1:30 |
| 5 | Click **View Change Proof** | **Change deployed**, **7 / 7 conditions satisfied**, proof id, content hash, **Content hash matches** | 2:00 |

Step 3 is the demo. A verification system that only works when the worker gets it right
the first time verifies nothing — say that while it is on screen.

### The two lines that must be said exactly

1. *"The Truth Engine compares that to the expected value and returns FAIL. **Not the
   model.**"*
2. *"The hash establishes content identity and detects alteration. **It is not a signature
   and not a ledger entry.**"*

---

## 4. Latency

The `Run live pilot` step takes **20–25 seconds**. That is a real Gemini call, real
remediation and real delivery happening inside one request.

- **Do** cut the wait in the edit. The waiting is real and trimming its duration changes
  nothing about what happened.
- **Do not** pre-render a result, splice a verdict from a different run, or reorder the
  second photograph before the first.

Gemma is fast — measured **1.8–2.7 s** per verification on the deployed service — so the
two verification steps need no trimming at all.

---

## 5. Do not click

- **View Change Proof** before step 4 — it does not exist yet, and the page will say so
- **Upload your own photograph** on camera unless you have rehearsed it; an unfamiliar
  image can legitimately return `INCONCLUSIVE`, which is honest but not the story
- The **browser back button** after a verdict — re-fetching is fine, but do not imply a
  state change that did not happen
- Any **cleanup or shutdown** script

---

## 6. After the live flow — infrastructure proof

Cut to the Google Cloud Console to show this ran where you said it did:

1. **Cloud Run** — two services: `driftzero-web` (public) and `driftzero-api` (private).
   Show that `driftzero-api` has no `allUsers`.
2. **Firestore** — the workflow document the run just created.
3. **Logs** — the correlated entries for that `workflow_id`.

Never put an identity token, an `Authorization` header or a billing identifier on screen.

---

## 7. Fallbacks

| If | Then |
| --- | --- |
| Gemini is slow or erroring | Retry once. The pilot creates a fresh workflow each time, so a failed attempt costs nothing and taints nothing |
| Gemma returns `INCONCLUSIVE` | That is an honest outcome. Retry with the pilot photograph; if it recurs, narrate it — the product shows it rather than hiding it |
| The whole live path is down | The recorded run is preserved at `/demo` and in `evidence/runs/hero_run_001/`. Narrate over it **and say it is recorded** |

In every fallback: **say what the viewer is looking at.** A recorded run presented as live
is the one thing that would undermine the submission.

---

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| Status chip says `UNREACHABLE` | the backend is cold or the invoker binding changed | re-check `driftzero-web-sa` holds `run.invoker` on `driftzero-api` |
| "The pilot did not reach the frontline" | Change Intelligence qualified no target | retry; if it persists, check `DRIFTZERO_SEMANTIC_PROVIDER=google_adk` on the backend |
| "This pilot session is not valid" | the capability expired (30 minutes) | start a new run |
| Verify returns `INCONCLUSIVE` repeatedly | the field provider is misconfigured | check `DRIFTZERO_FIELD_PROVIDER=vertex_maas` |
| Proof page says no proof exists | fewer than seven conditions hold | confirm the latest verification actually returned PASS |
| A page 503s | the backend did not answer | the public page degrades honestly; retry |

---

## After recording

1. Note the `change_id`, `proof_id` and content hash shown on the proof page. They are
   from your run and can be shown in Firestore afterwards if a judge asks.
2. Do **not** delete the workflow.
3. Leave both services running until judging closes.
