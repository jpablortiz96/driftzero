# DRIFTZERO — limitations

Everything here is a real constraint of the system as built, stated so it can be checked
against the evidence rather than discovered later. Where a limitation was the result of a
deliberate decision, the decision is given too.

Current runtime: **`CLOUD_PILOT`**, `production_ready: false`. The deployed service
reports this itself at `/ready`, and nothing in the product claims otherwise.

---

## 1. Identity and authorization

**The per-agent boundary is application-level, not Agent Identity and not per-agent
IAM.**

The four agents run in a single Cloud Run service, so that service has exactly **one**
runtime identity: `driftzero-run-sa`. There are no per-agent Cloud IAM principals.
Per-agent separation is enforced by an in-process authorization broker holding a single
frozen capability policy — one capability per agent, verified on every mutation attempt.

This is genuine enforcement and it is tested (a cross-boundary attempt is denied), but it
is **application-level**, not platform-enforced. A compromise of the process would sit
inside that boundary rather than behind it. Gemini Enterprise Agent Platform Agent
Identity would move the boundary into the platform; it was not provisioned.

Evidence: [`m2/cloud_run_deployment/cloud_run_iam.json`](m2/cloud_run_deployment/cloud_run_iam.json),
[`m2/cloud_foundation/iam.json`](m2/cloud_foundation/iam.json)

---

## 2. GEAP components are deferred, not implemented

Every Gemini Enterprise Agent Platform capability — Agent Runtime, Agent Registry, Agent
Identity, Agent Gateway, advanced Agent Observability — is `TRACK_ENHANCEMENT` in
plan.md and gated on account access. **None was provisioned.** The core hero workflow was
built never to depend on them, and runs entirely on Cloud Run, ADK, Firestore, Pub/Sub
and Cloud Storage.

There is consequently **no `geap_access_gate.json`**: the phase that would record
per-component ACCESS_CHECK results was not run, so no result is claimed. See
`absent_slots` in [`MANIFEST.json`](MANIFEST.json).

---

## 3. Model Armor was not used; images are not screened

**`SCREENING_SKIPPED`.** No Model Armor or equivalent content-screening layer sits in
front of Gemini or Gemma. Field evidence images are passed to the model without
screening, and prompts are not filtered by a platform safety service.

What does exist is structural rather than detective: the field model can only return one
of three values (`LEFT`, `TOP_RIGHT`, `INCONCLUSIVE`) and anything else is rejected
outright; the semantic agent holds no tool capability at all. Injection resistance comes
from **absent tools and closed output domains**, not from screening.

Evidence: `tests/integration/test_agent_output_validation.py`,
[`m2/api_pubsub/authoritative_refusal.json`](m2/api_pubsub/authoritative_refusal.json)

---

## 4. Immutability is operational, not a ledger

Evidence and proofs are **write-once by application semantics**: a Firestore proof
document is created with a must-not-exist precondition and never rewritten; a Cloud
Storage object is written with `if_generation_match=0`; differing content under an
existing identity is refused. This was verified against real Firestore and real Cloud
Storage.

That is immutability **within one project's trust boundary**. It is not an append-only
ledger, and it is not tamper-proof against an actor holding project write credentials.

---

## 5. The proof hash is content identity, nothing more

`ChangeProof.content_hash` is a SHA-256 over the proof's canonical JSON **excluding its
own `content_hash` field**. It establishes content identity and detects alteration.

It is **not** a digital signature, **not** an attestation, **not** a trusted timestamp,
**not** non-repudiation, and **not** a blockchain or ledger entry. No key signs it and no
third party witnesses it.

Because the stored file contains `content_hash`, the SHA-256 of the whole file differs
from the value inside it. That is expected. The verification recipe is in
[`../docs/verifying_a_change_proof.md`](../docs/verifying_a_change_proof.md).

---

## 6. G1 selected MaaS after a self-deployed GPU route failed

The feasibility gate returned **GO**, but not on the route originally planned. The
Vertex AI Model Garden self-deploy was `PLATFORM_SUPPORTED` — the platform accepted the
configuration — but **no deployment ever completed**, and no GPU quota was granted. The
active route is Vertex AI MaaS, serverless on-demand, recorded as
`requires_self_deployment: false` and `self_deploy_status: NOT_THE_ACTIVE_ROUTE`.

This is not the documented FALLBACK path: the deterministic/manual observation adapter
(T068) was never needed, because MaaS serves the real model. But the route in use is the
second one attempted, and the evidence says so.

Evidence: [`g1_gemma_feasibility.json`](g1_gemma_feasibility.json),
[`m3/architecture/serving_route.json`](m3/architecture/serving_route.json)

---

## 7. Engineering targets are not product claims

Numbers used during implementation — a 375 px viewport, a 60 s model timeout, a 120 s
resume lease, `max-instances=2` — are **non-binding engineering targets** (plan.md
§ Engineering Targets). None was measured as a performance guarantee, and none should be
read as a service level. No latency, throughput, accuracy or availability figure in this
repository is offered as a claim.

The Frontline Surface Minimums check is likewise a **minimum-interaction check, not a
WCAG conformance audit** and not a device-support claim. All six minimums pass; formal
accessibility certification is out of scope.

Evidence: [`reports/frontline_minimums.json`](reports/frontline_minimums.json)

---

## Current pilot limitations

**The source corpus is file-backed, not a live registry.** The source procedures and the
artifact catalog ship inside the container image as controlled pilot fixtures. A
production runtime would read them from Cloud Storage and Firestore. The deployed service
reports this itself in `/ready` under `pilot_limitations`, which is why its runtime mode
is `CLOUD_PILOT` rather than anything stronger.

**The worker surface is operator-reachable, not worker-reachable.** It sits behind the
same Cloud Run IAM boundary as the API, so reaching it needs an identity token. A real
frontline worker on their own phone would need a mediated device session or capability
link. That was not in scope for the surface tasks and is **not implemented**. The
decision was deliberate: making the API public to simplify the frontend would have
dissolved the authentication boundary the rest of the system depends on.

**The observation domain is position-specific.** Field Verification currently answers one
question — where is the label — over the closed set `LEFT` / `TOP_RIGHT` /
`INCONCLUSIVE`. The comparator, crossings and proof machinery are general; the observed
vocabulary is not. A second kind of physical change would need a new domain and a new
prompt.

**Pilot data is synthetic; the execution is not.** The procedures and work instructions
are fabricated for the pilot. The photographs, the cloud execution, the model inferences
and the proof are real. Every artifact in [`MANIFEST.json`](MANIFEST.json) is labelled
with which it is.

---

## Known non-blocking technical debt

**The Gemma timeout is per-phase, not a total deadline.** The 60 s value is configured
and applied, but `httpx` applies a bare timeout to each connect/read/write phase rather
than as a wall-clock budget. One ambiguous inference was observed taking **~90 s** end to
end while no single phase exceeded 60 s. The retry policy owns the overall attempt
budget; the effective bound on a single call is per-phase.

Measured in [`reports/multimodal_eval.json`](reports/multimodal_eval.json)
(`label_ambiguous_01.jpg`, 90.175 s).

**ADK 2.7.1 deprecates `SequentialAgent` in favour of `Workflow`.** The orchestration is
correct and tested; migration was deferred deliberately rather than undertaken during a
milestone that depended on it.

**A durable ADK session service was written rather than adopted.** Of the three the ADK
ships, `InMemorySessionService` dies with the process, `DatabaseSessionService` requires
a SQL instance, and `VertexAiSessionService` targets a different product. The Firestore
implementation satisfies the documented `BaseSessionService` contract and delegates state
semantics to the ADK, but it is ours to maintain.

---

## Out of scope

Deliberately not built, and not claimed:

- Comprehensive design-system work, a native mobile application, and formal accessibility
  certification (spec.md Non-Goals Class A)
- Enterprise SSO, tenant isolation, or multi-customer separation
- Any GEAP platform capability (§2)
- The optional governed-fleet and video phases
- Cost reconciliation against real billing — owned by a task not yet executed, so
  `cost_model.json` records no `actual_cost_observed`
- Production operations: alerting policies, runbooks, SLOs, on-call

---

## What this pilot does do

Stated as plainly as the limitations, because both are needed to judge it:

a real approved change arrives by authenticated Pub/Sub; a real Gemini call proposes
impact and a deterministic engine overrules it; a real artifact is remediated under a
verified capability; only the delta reaches the worker; a real photograph of a real box
is observed by real Gemma on Vertex AI; a frozen comparator — not the model — decides
PASS or FAIL; a failed attempt blocks the proof and is kept in history; and a Change
Proof exists only when all seven conditions hold. It survives the death of the process
running it.
