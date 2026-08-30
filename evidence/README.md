# DRIFTZERO — evidence pack

Start with [JUDGES_START_HERE.md](JUDGES_START_HERE.md), then
[LIMITATIONS.md](LIMITATIONS.md). This file is the map; [MANIFEST.json](MANIFEST.json) is the index with hashes.

Every artifact below already existed before this pack was assembled. Nothing here was generated to fill the pack, and nothing was re-run to refresh it.

## What kind of evidence is what

| Class | Meaning |
| --- | --- |
| `REAL_GOOGLE_CLOUD` | Observed against live Google Cloud resources in driftzero-runtime-2026 — Cloud Run, Firestore, Cloud Storage, Pub/Sub. Not a mock and not a fixture. |
| `REAL_MAAS_EXECUTION` | Produced by real inference on Vertex AI MaaS, google/gemma-4-26b-a4b-it-maas, serverless ON_DEMAND. Each record carries the input hash and token usage. |
| `HISTORICAL_LIVE_MODEL` | A live-model run recorded earlier and referenced, never re-executed. Kept byte-identical so a later batch cannot quietly restate it. |
| `OFFLINE_DETERMINISTIC` | Deterministic, reproducible and free. The Truth Engine, crossings and proof generator are production code; only the models are substituted. |
| `REAL_PHYSICAL_EVIDENCE` | Photographs of a real box with a printed label, taken with a camera. Nothing generated, rendered or composited occupies these paths. |
| `DERIVED` | Computed from other evidence in this pack rather than observed directly — an index, a gate result, or a checklist over recorded measurements. |

## Artifacts by class

### REAL_GOOGLE_CLOUD

- [`evidence/m3/architecture/serving_route.json`](m3/architecture/serving_route.json) — The Gemma serving route is Vertex AI MaaS, ON_DEMAND, with no accelerator and no endpoint provisioned.
- [`evidence/m2/cloud_run_deployment/cloud_run_service.json`](m2/cloud_run_deployment/cloud_run_service.json) — A private Cloud Run service runs the API under a least-privilege service account, scale-to-zero, max 2 instances.
- [`evidence/m2/cloud_run_deployment/authentication.json`](m2/cloud_run_deployment/authentication.json) — Unauthenticated invocation is refused on every route; an authenticated operator succeeds. allUsers is absent.
- [`evidence/m2/cloud_run_deployment/pubsub.json`](m2/cloud_run_deployment/pubsub.json) — Approved changes arrive by authenticated Pub/Sub push (OIDC) with a dead-letter policy bounded at 5 delivery attempts.
- [`evidence/m2/cloud_run_deployment/end_to_end_push.json`](m2/cloud_run_deployment/end_to_end_push.json) — One change published twice produced exactly one workflow; a permanently invalid message was refused and dead-lettered after exactly 5 attempts.
- [`evidence/runs/hero_run_001/restart_recovery.json`](runs/hero_run_001/restart_recovery.json) — A workflow survives process death and resumes the same logical execution, with no duplicated remediation or delivery.
- [`evidence/runs/hero_run_001/idempotency_log.json`](runs/hero_run_001/idempotency_log.json) — Duplicate events and duplicate evidence are refused by real Firestore and Cloud Storage preconditions.
- [`evidence/m2/exit_gate/manifest.json`](m2/exit_gate/manifest.json) — M2 closed on 37 checks spanning the real cloud architecture.
- [`evidence/m2/cloud_foundation/task_status.json`](m2/cloud_foundation/task_status.json) — Project, billing, APIs, Firestore, Pub/Sub, Cloud Storage and two least-privilege service accounts, captured from live gcloud output.
- [`evidence/geap_access_gate.json`](geap_access_gate.json) — Each of the six Gemini Enterprise Agent Platform components was access-checked against the real account and recorded DEFERRED with its reason and the fallback actually in force. None is simulated.

### REAL_MAAS_EXECUTION

- [`evidence/reports/multimodal_eval.json`](reports/multimodal_eval.json) — The production adapter returned an in-domain observation for every real fixture, 3/3 matching the expected position.
- [`evidence/runs/hero_run_001/real_camera_hero_run.json`](runs/hero_run_001/real_camera_hero_run.json) — Real photographs of a real box: a LEFT photo produced FAIL, a TOP_RIGHT photo produced PASS, 7/7 conditions, PROOF_COMPLETE.
- [`evidence/m3/exit_gate/manifest.json`](m3/exit_gate/manifest.json) — M3 closed on 34 checks; Gemma is authorised as a live-demo dependency; zero accelerators provisioned.

### HISTORICAL_LIVE_MODEL

- [`evidence/pilot_live_change_intel_2026_08_26/change_intelligence.json`](pilot_live_change_intel_2026_08_26/change_intelligence.json) — Change Intelligence ran on live Gemini and proposed candidate artifacts; the Truth Engine qualified exactly one.
- [`evidence/g1_gemma_feasibility.json`](g1_gemma_feasibility.json) — The Gemma serving route was selected empirically and recorded GO after 9 qualifying MaaS inferences.
- [`evidence/final_live_pilot_2026_08_26/change_proof_DZ-001.json`](final_live_pilot_2026_08_26/change_proof_DZ-001.json) — A complete Change Proof produced by the frozen generator once all seven completion conditions held.

### REAL_PHYSICAL_EVIDENCE

- [`fixtures/multimodal/manifest.json`](../fixtures/multimodal/manifest.json) — Three camera-captured fixtures with expected observations, hashes, and the MIME type sniffed from their actual bytes.

### OFFLINE_DETERMINISTIC

- [`evidence/m2/durable_resumability/restart_scenario.json`](m2/durable_resumability/restart_scenario.json) — Three separate processes carry one workflow from pause through FAIL to PASS and PROOF_COMPLETE, sharing only Firestore.
- [`evidence/m6/worker_mobile.png`](m6/worker_mobile.png) — The frontline worker sees only the delta: what changed, from what, to what, and one action.
- [`evidence/m6/worker_failed.png`](m6/worker_failed.png) — A failed verification is shown as recoverable, in words, with a clear retry.
- [`evidence/m6/proof_view.png`](m6/proof_view.png) — The Change Proof is explained in plain language before any JSON, with exact hash wording and no overclaim.
- [`evidence/runs/hero_run_local/manifest.json`](runs/hero_run_local/manifest.json) — The local end-to-end workflow passes with the Truth Engine authoritative at every crossing.
- [`evidence/reports/data_lineage.json`](reports/data_lineage.json) — Every evidence item carries a classification and an ordered lineage chain.
- [`evidence/security/prompt_injection_blocked.json`](security/prompt_injection_blocked.json) — Against a model that fully obeys an injected directive, the structural boundary holds: no tool to call, and no schema field able to carry a verdict, a state or an authorization.

### DERIVED

- [`evidence/reports/frontline_minimums.json`](reports/frontline_minimums.json) — All six Frontline Surface Minimums pass against the deployed surface. Not a WCAG conformance claim.
- [`evidence/LIMITATIONS.md`](LIMITATIONS.md) — What this pilot does not do, stated plainly.
- [`evidence/JUDGES_START_HERE.md`](JUDGES_START_HERE.md) — The entry point: what DRIFTZERO is, what is real, what to inspect.

## Bundle integrity

14 of 14 recorded `SHA256SUMS.txt` bundles verify.

```
cd evidence/<bundle> && sha256sum -c SHA256SUMS.txt
```

## What is deliberately absent

- `evidence/cost_model.json` — Owned by T136, which reconciles ACTUAL COST OBSERVED from billing against the ESTIMATED model. T136 has not been executed.
- `evidence/replays/` — No replay bundles were produced. Reproduction is by running the recorded gates, which is documented in JUDGES_START_HERE.md.
- `evidence/raw/` — Raw inputs live at their source paths rather than being duplicated: the source change is fixtures/hero_change.json and the field images are fixtures/multimodal/. Copying them would create a second set of bytes to keep in sync.

## Hash boundary

SHA-256 in this pack covers **complete file bytes** and establishes content identity and alteration detection only. It is not a signature, not a trusted timestamp, not an attestation and not a ledger entry.

`ChangeProof.content_hash` is a different hash over a different preimage: the proof's canonical JSON **excluding its own `content_hash` field**. The SHA-256 of a proof file is therefore expected to differ from the value inside it.
