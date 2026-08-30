# DRIFTZERO

**A process change isn't deployed when the document changes.
It's deployed when the work changes.**

Enterprises update a procedure, publish the new document, and call it deployed. Nobody
checks whether the physical work actually changed. DRIFTZERO closes that gap and proves
it closed.

---

## What DRIFTZERO does

```
SOURCE CHANGE  →  IMPACT  →  ACTION  →  FRONTLINE VERIFICATION  →  CHANGE PROOF
```

An approved procedure change arrives as a real event. The system works out which
downstream work is affected, remediates it, delivers only the delta to the person doing
the work, verifies from a photograph that the physical change happened, and emits a
Change Proof — or refuses to.

---

## The hero scenario

A packing procedure changes one requirement: **`label_position: LEFT → TOP_RIGHT`**.

1. **Change Intelligence** reads the two source versions and proposes which work
   instructions might be affected. In the recorded run it proposed **5 candidates**.
2. The **Truth Engine** qualifies exactly **one** — and overrules the other four. A
   nearby instruction that also contains the word "LEFT" (a forklift turn direction) is
   *not* touched.
3. **Remediation** updates that one artifact, under a capability it must hold.
4. **Frontline Enablement** delivers only the delta to a worker: what changed, from
   what, to what. Not the whole document.
5. The worker moves the label and photographs it. **Field Verification** observes the
   position with Gemma.
6. The **Truth Engine** compares the observation to the expected value and decides
   **PASS** or **FAIL**. The model never decides this.
7. Only when **all seven completion conditions** hold does a **Change Proof** exist.

In the recorded real-camera run, the first photo showed the label still on the left →
**FAIL**, and no proof was generated. The corrected photo → **PASS** → **7/7** →
**PROOF_COMPLETE**. Both attempts are kept: a change that took two tries is a different
fact from one that passed first time.

→ [`runs/hero_run_001/real_camera_hero_run.json`](runs/hero_run_001/real_camera_hero_run.json)

---

## Why this is agentic — and where agents stop

Four agents do the open-ended work. None of them can decide anything.

| Agents propose and act | The Truth Engine decides |
| --- | --- |
| Change Intelligence reads the source diff and **proposes** candidates | **Qualifies** which artifact is actually affected |
| Remediation **edits** an artifact within its granted scope | **Authorizes** the capability, **validates** the mutation |
| Frontline Enablement **composes** the delta | **Validates** the delivery receipt |
| Field Verification **observes** a position from a photo | **Adjudicates** PASS / FAIL, **proves** completion |

The model returns `LEFT`, `TOP_RIGHT` or `INCONCLUSIVE` — a position, never a verdict.
Anything outside that closed set is rejected, with no fuzzy matching. A request or event
that tries to state a conclusion (`verification_result`, `workflow_state`, `proof_id`, …)
is refused by name.

That separation is the product. Agents make the system flexible; the deterministic core
makes it trustworthy.

---

## The Google stack, as actually implemented

| | Used for |
| --- | --- |
| **Google ADK** | Agent orchestration, with a resumable invocation across process death |
| **Gemini** | Change Intelligence — reading the source change |
| **Gemma 4** (Vertex AI MaaS, serverless on-demand) | Field Verification — observing the label from a photograph |
| **Cloud Run** | The API and agents, private, scale-to-zero |
| **Firestore** | Authoritative durable state, action ledger, proofs, idempotency |
| **Cloud Storage** | Immutable evidence objects |
| **Pub/Sub** | Authenticated push ingestion of approved changes, with dead-letter |
| **Cloud Logging / Trace** | Structured logs and traces correlated by workflow |

No GPU is provisioned. Gemma runs on serverless MaaS, which the feasibility gate selected
empirically after a self-deployed GPU route failed.

---

## Live architecture

The backend is deployed and **private**: Cloud Run IAM is the authentication boundary,
and an unauthenticated request is refused on every route, including health.

Execution is **restart-safe**. A workflow survives the death of the process that created
it: a fresh Cloud Run instance recovers it from Firestore and resumes the *same* logical
execution — no replay from step one, no duplicated remediation, no duplicated delivery,
no second proof.

Current runtime mode is **`CLOUD_PILOT`**, and the service reports
`production_ready: false`. See [LIMITATIONS.md](LIMITATIONS.md).

---

## What to inspect first

| Look at | Why |
| --- | --- |
| [`runs/hero_run_001/real_camera_hero_run.json`](runs/hero_run_001/real_camera_hero_run.json) | The whole scenario with real photos and real Gemma |
| [`m6/worker_mobile.png`](m6/worker_mobile.png) · [`m6/worker_failed.png`](m6/worker_failed.png) · [`m6/proof_view.png`](m6/proof_view.png) | What the worker and the auditor actually see |
| [`final_live_pilot_2026_08_26/change_proof_DZ-001.json`](final_live_pilot_2026_08_26/change_proof_DZ-001.json) | A complete Change Proof |
| [`m3/architecture/serving_route.json`](m3/architecture/serving_route.json) | How Gemma is served, and why nothing was provisioned |
| [`m2/cloud_run_deployment/`](m2/cloud_run_deployment/) | The deployed service, its IAM, and the authenticated Pub/Sub path |
| [`runs/hero_run_001/restart_recovery.json`](runs/hero_run_001/restart_recovery.json) | Process death and resumption |
| [`geap_access_gate.json`](geap_access_gate.json) | Every enterprise-platform component, access-checked against the real account |
| [`security/prompt_injection_blocked.json`](security/prompt_injection_blocked.json) | Why an injected directive cannot reach authority |
| [`MANIFEST.json`](MANIFEST.json) | Every artifact, hashed and classified |
| [`LIMITATIONS.md`](LIMITATIONS.md) | What this does not do |

---

## What is real, and what is deterministic

Nothing in this pack is labelled simply "real". Each artifact carries one of six classes,
defined in [`MANIFEST.json`](MANIFEST.json) and [`README.md`](README.md):

- **`REAL_GOOGLE_CLOUD`** — observed against live Cloud Run, Firestore, Cloud Storage,
  Pub/Sub
- **`REAL_MAAS_EXECUTION`** — real Gemma inference on Vertex AI MaaS
- **`HISTORICAL_LIVE_MODEL`** — a live Gemini or Gemma run recorded earlier and
  referenced, never re-executed
- **`REAL_PHYSICAL_EVIDENCE`** — photographs of a real box with a printed label
- **`OFFLINE_DETERMINISTIC`** — reproducible with no cloud and no model; the Truth
  Engine, boundaries and proof generator are production code, only the models are
  substituted
- **`DERIVED`** — computed from other evidence, such as this index

The source procedure and work instructions are **synthetic pilot fixtures**. The
photographs, the cloud execution and the model inferences are **real**.

---

## Reproduce it

Two levels. The first needs nothing but Python.

**Deterministic core — no cloud, no model, no cost:**

```bash
pip install -e ".[dev,api,cloud]"
python -m pytest tests/ -q            # full suite
python -m scripts.m1_exit_gate        # local hero flow, 41 checks
```

**Verify the evidence you are reading:**

```bash
cd evidence/runs/hero_run_001 && sha256sum -c SHA256SUMS.txt
python -m scripts.build_evidence_pack --check
```

**Verify a Change Proof yourself** — the recipe is in
[`../docs/verifying_a_change_proof.md`](../docs/verifying_a_change_proof.md). The proof
page also re-hashes it live in your browser.

The cloud gates (`m2_exit_gate`, `m3_exit_gate`) require credentials for
`driftzero-runtime-2026` and are what produced the recorded cloud evidence. You do not
need to run them to evaluate the pack.

---

## One thing worth knowing about the proof

`ChangeProof.content_hash` is a SHA-256 over the proof's canonical JSON **excluding its
own `content_hash` field**. It establishes content identity and detects alteration.

It is **not** a digital signature, not an attestation, not a trusted timestamp, and not
a ledger entry. Because the stored file *contains* `content_hash`, the SHA-256 of the
whole file is expected to differ from it — that is arithmetic, not a discrepancy.
