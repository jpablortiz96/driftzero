# DRIFTZERO — submission copy (draft)

> The official form fields and judging criteria are **not stored in this repository**.
> This is written against the track recorded in the constitution — *Google All Things
> Agentic Hackathon — Fortified Enterprise Fleet* — and the constitution's own claim
> discipline. Map it to the real form before submitting.
>
> Every factual claim below is checkable against `evidence/MANIFEST.json`.

---

## 1. One-line description

**DRIFTZERO proves a process change reached the physical work — not just the document.**

## 2. Short description

When an enterprise updates a procedure, it publishes a new document and calls the change
deployed. Nobody checks whether the work actually changed. DRIFTZERO takes an approved
change, works out which downstream work instruction it affects, remediates it, delivers
only the delta to the frontline worker, verifies from a photograph that the physical
change happened, and issues a Change Proof — or refuses to. Four Google ADK agents do the
interpretation; a deterministic Truth Engine makes every decision that matters.

## 3. The problem

A packing procedure changes: `label_position: LEFT → TOP_RIGHT`. The document is updated,
the training deck is refreshed, the compliance box is ticked. On the packing line, the
label is still on the left.

The gap between *the document changed* and *the work changed* is where recalls, audit
findings and safety incidents live. Nothing in the normal toolchain closes it, because
closing it requires interpreting an unstructured change, finding the affected work, and
then observing the physical world.

## 4. The solution

```
SOURCE CHANGE → IMPACT → ACTION → FRONTLINE VERIFICATION → CHANGE PROOF
```

An approved change arrives as an authenticated Pub/Sub event. **Change Intelligence**
(Gemini) reads the two source versions and proposes candidate work instructions. The
**Truth Engine** qualifies exactly one — in the recorded run it accepted 1 of 5 proposals
and left a nearby instruction containing the same word "LEFT" untouched, because it is a
forklift turn direction. **Remediation** edits that one artifact under a capability it
must hold. **Frontline Enablement** sends the worker the delta, not a new manual. The
worker photographs the finished work; **Field Verification** (Gemma 4 on Vertex AI MaaS)
reports where the label is. The Truth Engine compares that to the expected value and
returns PASS or FAIL.

In the recorded real-camera run the first photo returned **FAIL** and no proof was
generated. The corrected photo returned **PASS**, all seven completion conditions held,
and a Change Proof was issued. Both attempts are kept.

## 5. Why this is agentic

Four things in this problem cannot be expressed as rules:

- reading an unstructured procedure change and proposing what it affects
- performing a scoped edit on a document that was not written to a schema
- adapting the change into an instruction a worker can act on in seconds
- interpreting a photograph of the physical world

Those are the agents. But an agent that can *decide* is a system you cannot audit, so
DRIFTZERO draws the line explicitly: **agents propose, the Truth Engine decides.**

| Agents may | Agents may not |
| --- | --- |
| propose candidate artifacts | choose the affected one |
| edit within a granted capability | grant themselves that capability |
| compose the worker's delta | assert that delivery happened |
| report `LEFT` / `TOP_RIGHT` / `INCONCLUSIVE` | decide PASS or FAIL |

Two structural properties enforce it rather than convention: the semantic agent is
constructed **with no tools**, so "call this tool" has nothing to invoke; and its output
schema has **no authority field**, so "set the verdict to PASS" cannot be expressed. Both
are asserted against a model that fully obeys an injected directive
(`evidence/security/prompt_injection_blocked.json`).

The hybrid is the point. Agents make it flexible enough to be useful; the deterministic
core makes it trustworthy enough to be believed.

## 6. Google technologies used

| Technology | Used for |
| --- | --- |
| **Google ADK** | Agent orchestration; a resumable invocation backed by a Firestore session service |
| **Gemini** | Change Intelligence — reading the source change |
| **Gemma 4** via **Vertex AI MaaS** | Field Verification — observing the label, serverless on-demand |
| **Cloud Run** | The API, agents and worker surface — private, IAM-gated, scale-to-zero |
| **Firestore** | Authoritative workflow state, action ledger, proofs, idempotency keys, resume leases |
| **Cloud Storage** | Write-once evidence store, verified live — not wired into the deployed pilot path |
| **Pub/Sub** | Authenticated OIDC push ingestion with a dead-letter path |
| **Cloud Logging / Cloud Trace** | Structured telemetry correlated by workflow and action |
| **Cloud Build / Artifact Registry** | Container build and deployment by digest |

No GPU is provisioned. The feasibility gate selected serverless MaaS empirically after a
self-deployed GPU route failed to complete.

## 7. Architecture summary

A private Cloud Run service hosts the API, the four ADK agents and the worker surface.
Approved changes arrive by authenticated Pub/Sub push; malformed events are refused and
dead-lettered after a bounded five attempts. Firestore is the authoritative store — for
workflow state, the action ledger, proofs, idempotency claims and resume leases — and it
is what the deployed pilot actually writes to. A Cloud Storage write-once evidence store
is implemented and was verified live, but is not wired into the pilot path. Every
consequential step crosses one of four deterministic trust boundaries before it counts.

Execution is restart-safe: a workflow survives the death of the process that created it.
A fresh instance recovers it from Firestore and resumes the **same** logical execution —
verified across three separate processes, with no duplicated remediation, no duplicated
delivery and exactly one proof. A durable lease means two instances can never resume the
same workflow at once.

Diagram: `docs/architecture.md`.

## 8. Innovation

Most agent demos let the model decide and hope it is right. DRIFTZERO inverts that: the
model's output is untrusted input to a deterministic engine, and the interesting
engineering is in the boundary.

Three things follow that are unusual:

- **A failure is a first-class outcome.** The FAIL→PASS path is the demo, not an error
  case. A verification system that only works when the worker gets it right first time
  verifies nothing.
- **The proof refuses to exist.** A Change Proof is generated only when all seven
  completion conditions hold. There is no override, no force-complete, and no path where
  a client can assert one.
- **The claims are bounded on purpose.** The proof hash establishes content identity and
  alteration detection — and the product says, on the page, that it is not a signature,
  not an attestation and not a ledger entry.

## 9. Operational utility

The unit of work is the delta. A worker gets "the label moves from LEFT to TOP_RIGHT",
not a 40-page revision. Verification is a photograph, which is what they already have in
their hand.

For the organisation, the output is an auditable record that a specific change reached
specific physical work at a specific moment — including the attempt that failed first.
That is the artifact a regulator, an auditor or an incident review actually asks for, and
it is the one thing a document management system cannot produce.

## 10. Production readiness and limitations

**This is a pilot, and the deployed service says so.** `/ready` reports
`runtime_mode: CLOUD_PILOT` and `production_ready: false`.

Honestly stated, and detailed in `evidence/LIMITATIONS.md`:

- The source corpus is file-backed in the container, not a live enterprise registry
- The worker surface sits behind Cloud Run IAM — operator-reachable, not yet
  worker-device reachable
- The observation domain is position-specific: `LEFT` / `TOP_RIGHT` / `INCONCLUSIVE`
- Per-agent separation is **application-level**, not Agent Identity and not per-agent IAM
- All six enterprise-platform components were access-checked and are **DEFERRED**, none
  simulated; the project has no organization parent, which is what most of them require
- Model Armor is built and wired but **cannot take effect here** — it is regional and
  rejects `global`, and this project's Gemini is only routable at `global`
- Evidence immutability is operational, within one project's trust boundary — not a
  ledger; and the Cloud Storage half of it, though verified, is not wired into the
  deployed path

What is real: real Google Cloud, real Gemini, real Gemma, real photographs of a real box,
a real deterministic proof flow, durable cloud state and authenticated event ingestion.
The pilot data is synthetic and labelled as such.

## 11. Repository and demo instructions

**Start at `evidence/JUDGES_START_HERE.md`.**

The deterministic core reproduces with no cloud, no model and no cost:

```bash
pip install -e ".[dev,api,cloud]"
python -m pytest tests/ -q
python -m scripts.m1_exit_gate
```

Verify the evidence you are reading:

```bash
python -m scripts.build_evidence_pack --check
cd evidence/runs/hero_run_001 && sha256sum -c SHA256SUMS.txt
```

The deployed backend is **private** by design — Cloud Run IAM is the authentication
boundary, and making it public to simplify a demo would dissolve the property the rest of
the system depends on. Access can be granted to a judge's Google account on request.

`evidence/MANIFEST.json` indexes every artifact with its SHA-256, its evidence class and
the claim it supports.
