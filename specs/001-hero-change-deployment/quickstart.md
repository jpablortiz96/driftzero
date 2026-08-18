# Quickstart & Validation Guide: Hero Change Deployment

**Feature**: `001-hero-change-deployment`
**Date**: 2026-08-17

## Prerequisites

- Python 3.11+
- Google Cloud SDK (`gcloud`) authenticated
- GCP project with billing enabled and hackathon credits applied
- `uv` or `pip` for Python dependency management
- Docker (for Gemma 4 Cloud Run GPU deployment)

## Manual Setup Required

The following actions MUST be performed manually. They cannot be safely automated by repository code. Every step lists **WHY / WHEN / EXPECTED RESULT / HOW TO VERIFY / COST RISK**.

Steps are split into two tracks:

- **CORE** — required for the fallback core architecture (Cloud Run + ADK + Firestore + Pub/Sub + Cloud Storage). The hero workflow depends on these.
- **OPTIONAL / TRACK ENHANCEMENT** — Gemini Enterprise Agent Platform (GEAP) capabilities. Each is gated by the GEAP Availability Gate in plan.md. **Failure of any optional step MUST NOT block the core architecture.**

Cost risk is rated LOW / MEDIUM / HIGH relative to the $50 internal budget alert. Monetary values are `ESTIMATED` only; `ACTUAL COST OBSERVED` is recorded later from Cloud Billing into `evidence/cost_model.json`.

**Milestone references** below follow plan.md § Implementation Milestones: **M0** Truth Engine · **G1** Gemma feasibility risk spike (early) · **M1** Gemini + ADK · **M2** real cloud event + durable state · **M3** physical Gemma verification · **M4** OPTIONAL governed fleet · **M5** OPTIONAL Veo · **M6** demo surface + evidence packaging.

### Track: CORE (required)

**MS-1 — Create/select GCP project**
- WHY: every cloud resource needs a project boundary and billing target.
- WHEN: before any cloud work.
- EXPECTED RESULT: a project ID reserved for DRIFTZERO only.
- HOW TO VERIFY: `gcloud projects describe $PROJECT_ID`
- COST RISK: LOW (free).

**MS-2 — Associate billing account**
- WHY: required for non-free-tier resources, and mandatory for GPU.
- WHEN: before any cloud work.
- EXPECTED RESULT: project linked to an active billing account.
- HOW TO VERIFY: `gcloud billing projects describe $PROJECT_ID` shows `billingEnabled: true`
- COST RISK: LOW (the action itself is free; it enables spend).

**MS-3 — Redeem hackathon credits**
- WHY: offsets cloud costs; the GPU and Veo drivers are the ones that matter.
- WHEN: immediately after project creation, before GPU work.
- EXPECTED RESULT: credit balance visible on the billing account.
- HOW TO VERIFY: Cloud Console → Billing → Credits shows a non-zero balance and expiry date.
- COST RISK: LOW (reduces effective cost).

**MS-4 — Set budget alerts**
- WHY: the only hard guard against runaway GPU/Veo spend; required by the cost discipline in plan.md.
- WHEN: immediately after MS-2, before any GPU or Veo work.
- EXPECTED RESULT: a budget with a **$50 internal alert** plus $25 and $75 notification thresholds, email notifications enabled.
- HOW TO VERIFY: Cloud Console → Billing → Budgets & alerts lists the budget; send a test notification.
- COST RISK: LOW (free) — omitting it is the HIGH risk.

**MS-5 — Enable required APIs (core architecture)**
- WHY: every core service call fails without its API enabled.
- WHEN: before M2.
- EXPECTED RESULT: enabled — `firestore.googleapis.com`, `pubsub.googleapis.com`, `run.googleapis.com`, `storage.googleapis.com`, `aiplatform.googleapis.com`, `iam.googleapis.com`, `cloudbuild.googleapis.com`, `artifactregistry.googleapis.com`, `logging.googleapis.com`, `monitoring.googleapis.com`, `cloudtrace.googleapis.com`.
- HOW TO VERIFY: `gcloud services list --enabled --project $PROJECT_ID` contains each name.
- COST RISK: LOW (enabling is free).

**MS-6 — Authenticate local CLI (ADC)**
- WHY: local development and tests need Application Default Credentials.
- WHEN: before M1.
- EXPECTED RESULT: ADC credentials present for the developer account.
- HOW TO VERIFY: `gcloud auth application-default print-access-token` returns a token.
- COST RISK: LOW.

**MS-7 — Select a single region**
- WHY: cross-region calls add latency and cost; GPU availability is region-specific.
- WHEN: before M2.
- EXPECTED RESULT: one region chosen for Firestore, Cloud Run, GCS, and Vertex AI (prefer a region with L4 GPU availability, e.g. `us-central1`).
- HOW TO VERIFY: the region is recorded in `.env` and used by every deploy command.
- COST RISK: LOW (wrong choice causes rework, not spend).

**MS-8 — Create local `.env` from `.env.example`**
- WHY: the repo must never contain secrets (Constitution: Secret Hygiene); `.env.example` holds placeholder keys only.
- WHEN: before M1, first thing after cloning.
- EXPECTED RESULT: a local, git-ignored `.env` containing `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION`, `FIRESTORE_DATABASE`, `GCS_EVIDENCE_BUCKET`, `PUBSUB_TOPIC`, `GEMINI_MODEL`, plus any model endpoint variables. No real secret is ever committed.
- HOW TO VERIFY: `git check-ignore -v .env` reports it ignored; `git status --short` never lists `.env`; the app starts and reads its configuration.
- COST RISK: LOW.

**MS-9 — Create Firestore database**
- WHY: authoritative workflow state, idempotency keys, and proof records.
- WHEN: before M2.
- EXPECTED RESULT: a Standard-edition database in the selected region.
- HOW TO VERIFY: `gcloud firestore databases describe --database='(default)'`
- COST RISK: LOW (free tier covers demo volume).

**MS-10 — Create Pub/Sub topic**
- WHY: the hero workflow must start from a real external event, not a chat prompt.
- WHEN: before M2.
- EXPECTED RESULT: topic `driftzero-approved-changes` exists (plus a push subscription to the Cloud Run endpoint).
- HOW TO VERIFY: `gcloud pubsub topics describe driftzero-approved-changes`
- COST RISK: LOW (free tier).

**MS-11 — Create Cloud Storage evidence bucket**
- WHY: immutable raw evidence, before/after artifacts, rendered proofs.
- WHEN: before M2.
- EXPECTED RESULT: bucket `driftzero-evidence-$PROJECT_ID` in the selected region, uniform bucket-level access, lifecycle policy set.
- HOW TO VERIFY: `gcloud storage buckets describe gs://driftzero-evidence-$PROJECT_ID`
- COST RISK: LOW.

**MS-12 — Decide secret handling: local `.env` vs Google Secret Manager**
- WHY: deployed Cloud Run services should not carry secrets as plain environment variables if avoidable; this is an explicit architecture choice, not a default.
- WHEN: before the first Cloud Run deploy (M2).
- EXPECTED RESULT: **if Secret Manager is selected** — `secretmanager.googleapis.com` enabled, one secret per credential, the Cloud Run runtime service account granted `roles/secretmanager.secretAccessor` on those secrets only, and the service deployed with `--set-secrets`. **If not selected** — documented decision that only non-secret configuration is passed as environment variables, and no credential leaves the local `.env`.
- HOW TO VERIFY: `gcloud secrets list`; `gcloud run services describe <svc> --format='value(spec.template.spec.containers[0].env)'` shows secret references rather than literal values; `git log -p` contains no credential.
- COST RISK: LOW (Secret Manager is billed per secret version and access operation; negligible at this volume).

**MS-12b — Create the CORE Cloud Run runtime service accounts**
- WHY: the core architecture deploys **two** Cloud Run services (`driftzero-api` holding the API + Truth Engine + all four ADK agents in one process, and `gemma-verification` for GPU inference). Each service needs one least-privilege runtime identity. Running on the default Compute Engine service account would be over-privileged. **This is not Agent Identity and not per-agent IAM identity** — per-agent separation in the fallback is application-level, enforced by the in-process authorization broker (plan.md § Agent Identity and Runtime Identity Model).
- WHEN: before the first Cloud Run deploy (M2); the `gemma-verification` account before M3.
- EXPECTED RESULT: two service accounts exist — `driftzero-run-sa` and `driftzero-gemma-sa` — each granted only the roles it needs:
  - `driftzero-run-sa`: `roles/datastore.user` (Firestore), `roles/storage.objectAdmin` **scoped to the evidence bucket only**, `roles/pubsub.subscriber`, `roles/aiplatform.user` (Gemini), `roles/logging.logWriter`, `roles/monitoring.metricWriter`, `roles/cloudtrace.agent`, plus `roles/run.invoker` on `gemma-verification`, and `roles/secretmanager.secretAccessor` on named secrets only if MS-12 selected Secret Manager.
  - `driftzero-gemma-sa`: `roles/storage.objectViewer` on the evidence bucket, `roles/logging.logWriter`, `roles/monitoring.metricWriter`, `roles/cloudtrace.agent`. No Firestore, no Pub/Sub, no write access.
  - Neither account receives project-level Owner/Editor.
- HOW TO VERIFY: `gcloud iam service-accounts list` shows both; `gcloud projects get-iam-policy $PROJECT_ID --flatten=bindings --filter='bindings.members:driftzero-run-sa'` lists only the intended roles; after deploy, `gcloud run services describe driftzero-api --format='value(spec.template.spec.serviceAccountName)'` returns `driftzero-run-sa@...` and the same check on `gemma-verification` returns `driftzero-gemma-sa@...`; a deliberate cross-boundary call (Gemma SA attempting a Firestore write) is denied.
- COST RISK: LOW (IAM is free; correct scoping reduces blast radius, not spend).

**MS-13 — Configure Cloud Run CPU service `--max-instances`**
- WHY: bounds concurrency-driven spend and matches the single-workflow demo scale.
- WHEN: at first Cloud Run deploy (M2).
- EXPECTED RESULT: API/agent service deployed with `--max-instances=2 --min-instances=0` (scale-to-zero) **and `--service-account=driftzero-run-sa@$PROJECT_ID.iam.gserviceaccount.com`** (MS-12b), never the default Compute Engine account.
- HOW TO VERIFY: `gcloud run services describe driftzero-api --format='value(spec.template.metadata.annotations)'` shows the max-instances annotation as 2.
- COST RISK: LOW when set; MEDIUM if left at the platform default.

**MS-14 — Check/request Cloud Run GPU quota**
- WHY: L4 GPU quota is not granted by default and approval is not instant; this is the single most common schedule risk for the Gemma path.
- WHEN: as early as possible — during the **G1** feasibility spike, well before M3. Only required if G1 selects a GPU serving route.
- EXPECTED RESULT: a non-zero limit for the Cloud Run NVIDIA L4 GPU quota in the selected region, or an approved increase request.
- HOW TO VERIFY: Cloud Console → IAM & Admin → Quotas filtered to the Cloud Run GPU quota in the selected region (or `gcloud services quota list --service=run.googleapis.com --consumer=projects/$PROJECT_ID`) shows a non-zero limit; a successful GPU deploy is the definitive proof.
- COST RISK: LOW to request; unlocks the HIGH-risk driver.

**MS-15 — Configure Cloud Run GPU service `--max-instances`**
- WHY: GPU-seconds are the dominant cost driver in this project.
- WHEN: at the Gemma deploy (M3), on the serving route G1 selected.
- EXPECTED RESULT: Gemma service deployed with `--gpu=1 --gpu-type=nvidia-l4 --max-instances=1 --min-instances=0` (scale-to-zero) **and `--service-account=driftzero-gemma-sa@$PROJECT_ID.iam.gserviceaccount.com`** (MS-12b).
- HOW TO VERIFY: `gcloud run services describe gemma-verification` shows one GPU, max-instances 1, min-instances 0.
- COST RISK: **HIGH** if misconfigured (idle or parallel GPU instances); LOW when scale-to-zero with max-instances 1.

**MS-16 — Verify Gemma 4 model access and licence acceptance**
- WHY: Gemma is licence-gated; weights cannot be pulled or served until the licence is accepted, and the demo depends on this model.
- WHEN: during **G1** — model access and serving route are exactly what the G1 spike exists to settle, and its GO/FALLBACK decision depends on this step.
- EXPECTED RESULT: Gemma 4 12B licence accepted for the account, and the chosen serving route confirmed (Vertex AI Model Garden deployment **or** a vLLM container image built and pushed to Artifact Registry).
- HOW TO VERIFY: Model Garden shows the model as accessible for the project, or the container starts locally and answers a test inference; then `curl` the deployed Cloud Run endpoint with a fixture image and receive structured JSON.
- COST RISK: MEDIUM (GPU-seconds during validation; zero when idle).

**MS-17 — Enable observability: Logging, Monitoring, Cloud Trace**
- WHY: Constitution VII requires traceable telemetry with correlation IDs; judged evidence depends on real traces.
- WHEN: before M2 (baseline); optionally refined at M4 if advanced Agent Observability is accessible.
- EXPECTED RESULT: `logging.googleapis.com`, `monitoring.googleapis.com`, `cloudtrace.googleapis.com` enabled; the Cloud Run runtime service account holds `roles/logging.logWriter`, `roles/monitoring.metricWriter`, `roles/cloudtrace.agent`; OpenTelemetry export configured from the service.
- HOW TO VERIFY: run one workflow, then confirm a trace with the workflow correlation ID in Cloud Trace and matching structured entries in Cloud Logging.
- COST RISK: LOW (free tier at demo volume).

**MS-18 — Prepare the physical demo fixture**
- WHY: the field verification is a real-world capture, not a rendered mock; the fixture set is also the empirical input to the **G1** GO/FALLBACK decision.
- WHEN: before **G1** (needed to test distinguishability), reused at M3.
- EXPECTED RESULT: a real box, a printed high-contrast label, and repeatable LEFT and TOP-RIGHT placements plus one deliberately ambiguous framing.
- HOW TO VERIFY: capture all three photos and run them through the multimodal fixture evaluation.
- COST RISK: LOW (~$5 physical materials `ESTIMATED`, not cloud spend).

**MS-19 — Verify Veo 3.1 access (BONUS path)**
- WHY: Veo is optional and non-blocking, but access must be confirmed before planning any demo asset around it.
- WHEN: before M5.
- EXPECTED RESULT: the account can submit a Veo 3.1 generation request and poll it to completion.
- HOW TO VERIFY: one short test generation returns a video asset; the request/response is recorded in the evidence pack.
- COST RISK: MEDIUM — billed at roughly **$0.10 per generated second** (Standard, 720p) `ESTIMATED`; enforce the ≤5 development / ≤2 demo generation cap at ≤6 seconds each.

**MS-20 — Cleanup / shutdown procedure**
- WHY: prevents post-demo spend; GPU services and stored artifacts keep costing money after judging.
- WHEN: after every working session for the GPU service, and once after final submission for everything else.
- EXPECTED RESULT: per session — the Gemma GPU service is scaled to zero or deleted. Final — GPU service deleted, Cloud Run services deleted or scaled to zero, evidence bucket retained (it is the deliverable) with lifecycle rules applied, Pub/Sub subscriptions deleted, no unexpected charge accruing.
- HOW TO VERIFY: `gcloud run services list` shows no GPU service; `gcloud billing accounts describe` / Cloud Billing reports show daily spend dropping to ~$0; final figures exported to `evidence/cost_model.json` as `ACTUAL COST OBSERVED`.
- COST RISK: **HIGH if skipped** (idle GPU is the worst case); LOW when executed.

### Track: OPTIONAL / TRACK ENHANCEMENT (GEAP — must not block the core)

Each step below belongs to **M4** and runs only after its ACCESS_CHECK in plan.md (§ GEAP Availability Gate) passes. Record every outcome — PASS or FAIL — in `evidence/geap_access_gate.json`. **A FAIL here is an acceptable, documented outcome; it triggers the fallback and does not block the core architecture.** None of these steps is required for FR-001–FR-011 or SC-001–SC-015 acceptance (spec.md § Non-Goals, Class B).

**MS-21 — Agent Platform / Agent Runtime setup — OPTIONAL / TRACK ENHANCEMENT**
- WHY: managed serverless agent execution with long-running/async support; alternative to hosting ADK on Cloud Run.
- WHEN: at the start of **M4**, after the core workflow already runs on Cloud Run and M2 is stable.
- EXPECTED RESULT: `aiplatform.googleapis.com`, `storage.googleapis.com`, `logging.googleapis.com`, `monitoring.googleapis.com`, `cloudtrace.googleapis.com`, `telemetry.googleapis.com`, `cloudresourcemanager.googleapis.com` enabled; caller holds `roles/aiplatform.user`; one trivial agent deployed and invocable.
- HOW TO VERIFY: the agent resource exists and answers a test invocation; result recorded in the access gate file.
- COST RISK: MEDIUM (managed runtime execution is billable; keep test agents minimal and delete them).
- FALLBACK IF IT FAILS: keep ADK on Cloud Run (`adk deploy cloud_run`) — no change to the hero workflow.

**MS-22 — Agent Registry setup — OPTIONAL / TRACK ENHANCEMENT (required only if Agent Gateway is used)**
- WHY: Agent Gateway blocks egress to hosts that are not registered, so the mutation-tool endpoint must be registered for the governed path to work.
- WHEN: M4, before Gateway configuration.
- EXPECTED RESULT: `agentregistry.googleapis.com` enabled; the `artifact-mutation-tool` endpoint (and the agents) registered and listable.
- HOW TO VERIFY: list the registry and see the entry; record in the access gate file.
- COST RISK: LOW.
- FALLBACK IF IT FAILS: a local `fixtures/agent_registry.json` manifest labelled `SIMULATED`; no claim of platform registry is made.

**MS-23 — Agent Identity provisioning — OPTIONAL / TRACK ENHANCEMENT**
- WHY: gives each agent a SPIFFE-based, per-agent cryptographic identity usable as an IAM principal — the basis for the least-privilege demonstration. **This is not a service account.**
- WHEN: M4, after Agent Runtime access is confirmed.
- EXPECTED RESULT: the project's organization parent confirmed; agents deployed with agent identity enabled (`identity_type: AGENT_IDENTITY`); IAM bindings accepted for members of the form `principal://agents.global.org-ORGANIZATION_ID.system.id.goog/resources/aiplatform/projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/AGENT_ENGINE_ID`; the mutation capability bound to the Remediation Agent identity only.
- HOW TO VERIFY: `gcloud projects describe $PROJECT_ID --format='value(parent)'` returns an organization; the `add-iam-policy-binding --member="principal://..."` command succeeds; the agent obtains a working token.
- COST RISK: LOW.
- FALLBACK IF IT FAILS (e.g. the project has no organization): the MS-12b per-service runtime service accounts plus the logical agent context and deterministic in-process authorization broker. This is application-level per-agent authorization; documented in `LIMITATIONS.md` as such — never described as Agent Identity, and never as per-agent IAM runtime identity.

**MS-24 — Agent Gateway setup — OPTIONAL / TRACK ENHANCEMENT**
- WHY: platform-enforced policy on the one governed path (Remediation Agent → Artifact Mutation Tool ALLOW; Frontline Enablement Agent → DENIED).
- WHEN: M4, after MS-22 and MS-23, and after the tool-protocol decision (MCP vs plain HTTP) has been recorded — Gateway policy work MUST NOT begin before that decision exists.
- EXPECTED RESULT: `compute.googleapis.com`, `networksecurity.googleapis.com`, `networkservices.googleapis.com`, `dns.googleapis.com`, `iam.googleapis.com`, `agentregistry.googleapis.com`, `aiplatform.googleapis.com`, `discoveryengine.googleapis.com`, `storage.googleapis.com`, `modelarmor.googleapis.com`, `monitoring.googleapis.com`, `logging.googleapis.com` enabled; gateway imported (`gcloud network-services agent-gateways import`); authorization extension imported (`gcloud beta service-extensions authz-extensions import`) first in `DRY_RUN`, then enforcement; authorization policy imported (`gcloud network-security authz-policies import`); `roles/iap.egressor` granted on the mutation-tool endpoint to the Remediation Agent principal **only**.
- HOW TO VERIFY: the ALLOW call succeeds and appears in the gateway logs with the caller SPIFFE principal; the DENY call from the Enablement Agent is rejected and produces a deny log entry; the target artifact hash is unchanged after the denied attempt.
- COST RISK: MEDIUM (networking components and IAP-fronted endpoints are billable; delete after the demo per MS-20).
- FALLBACK IF IT FAILS: the deterministic in-process authorization broker still demonstrates the ALLOW/DENY pair, with evidence explicitly labelled application-level enforcement rather than platform-enforced.

**MS-25 — Model Armor template/policy setup — OPTIONAL / TRACK ENHANCEMENT**
- WHY: screens untrusted artifact **text** for prompt injection before Change Intelligence processing. It is content screening only — never authorization.
- WHEN: M4, independent of Gateway (this path needs neither Gateway nor Registry nor an organization).
- EXPECTED RESULT: `modelarmor.googleapis.com` enabled; template `driftzero-untrusted-artifact-text` created in the same region the model calls are routed to, with prompt injection/jailbreak filters at `INSPECT_AND_BLOCK`; `roles/modelarmor.user` granted to `service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com`; the Change Intelligence `generateContent` call passing `modelArmorConfig.promptTemplateName` (and `responseTemplateName`).
- HOW TO VERIFY: the adversarial fixture returns `blockReason: "MODEL_ARMOR"` and the workflow fails closed to `REVIEW_REQUIRED`; the clean fixture passes untouched. Confirm the template exists in the routed region (a mismatch fails with `Template not found`).
- COST RISK: LOW (billed on total prompt+response tokens screened).
- FALLBACK IF IT FAILS: deterministic untrusted-content handling only — content quarantining, a no-instruction-following prompt contract, and Truth Engine trust-boundary validation at Crossing 1. `LIMITATIONS.md` records that no external guardrail ran, and evidence never claims screening that did not occur.

**MS-26 — Advanced Agent Observability — OPTIONAL / TRACK ENHANCEMENT**
- WHY: platform-level agent and gateway telemetry beyond the Cloud Trace/Logging baseline.
- WHEN: M4, after Runtime/Gateway provisioning.
- EXPECTED RESULT: agent and gateway interaction telemetry visible and exportable.
- HOW TO VERIFY: a demo run produces agent/gateway spans in Agent Observability; exports captured into the evidence pack.
- COST RISK: LOW (telemetry ingestion volume is trivial here).
- FALLBACK IF IT FAILS: the MS-17 OpenTelemetry + Cloud Trace/Logging baseline with correlation IDs, which already satisfies Constitution VII.

## Validation Scenarios

### VS-1: Truth Engine Unit Test Suite
**Proves**: FR-001, FR-006, FR-007, FR-008, FR-009, FR-011; SC-001 through SC-015
```bash
pytest tests/unit/truth_engine/ -v
```
**Expected**: All state transition, idempotency, supersession, completion invariant, and fail-closed tests pass.

### VS-2: End-to-End Hero Flow (Local)
**Proves**: SC-001 → SC-009, SC-014
```bash
# 1. Inject synthetic approved change
python -m driftzero.cli inject-change --fixture fixtures/hero_change.json

# 2. Verify workflow progresses through states
python -m driftzero.cli status --workflow-id $WF_ID

# 3. Submit INCORRECT field evidence (LEFT)
python -m driftzero.cli verify --workflow-id $WF_ID --image fixtures/label_left.jpg

# 4. Confirm FAIL
python -m driftzero.cli status --workflow-id $WF_ID  # VERIFICATION_FAILED

# 5. Submit CORRECT field evidence (TOP_RIGHT)
python -m driftzero.cli verify --workflow-id $WF_ID --image fixtures/label_top_right.jpg

# 6. Confirm PASS and PROOF_COMPLETE
python -m driftzero.cli status --workflow-id $WF_ID  # PROOF_COMPLETE

# 7. Retrieve and validate Change Proof
python -m driftzero.cli proof --workflow-id $WF_ID --validate
```

### VS-3: Duplicate Event Idempotency
**Proves**: SC-010
```bash
python -m driftzero.cli inject-change --fixture fixtures/hero_change.json
python -m driftzero.cli inject-change --fixture fixtures/hero_change.json
# Second injection produces no duplicate workflow or evidence
```

### VS-4: Supersession
**Proves**: SC-015
```bash
# Inject v1 change, progress to AWAITING_FIELD_VERIFICATION
# Inject v2 change for same requirement
# v1 workflow → SUPERSEDED
# v2 workflow → CHANGE_RECEIVED (new workflow)
```

### VS-5: Gemma 4 Multimodal Evaluation
**Proves**: SC-006, SC-007 (deterministic verification layer)
```bash
pytest tests/multimodal/ -v --fixtures fixtures/multimodal/
```
Fixture set:
- `label_left_01.jpg` → expected observation: `LEFT`
- `label_top_right_01.jpg` → expected observation: `TOP_RIGHT`
- `label_ambiguous_01.jpg` → expected observation: `INCONCLUSIVE`

### VS-6: Security — Prompt Injection Resistance (text screening path)
**Proves**: FR-011; Model Armor enforcement path (plan.md)
```bash
pytest tests/security/test_prompt_injection.py -v
```
Adversarial fixture: `fixtures/security/injected_artifact_text.json` — a downstream artifact whose instruction **text** embeds an injection attempting to override agent instructions.
**Expected**: with Model Armor enabled, the `generateContent` call returns `blockReason: "MODEL_ARMOR"` and the workflow fails closed to `REVIEW_REQUIRED`. In the fallback (no Model Armor), the injected instruction produces no unauthorized artifact mutation and no state advance, and the evidence records `SCREENING_SKIPPED` rather than claiming screening occurred.

### VS-8: Security — Tool Poisoning Rejection (planned; not implemented yet)
**Proves**: FR-002, FR-003, FR-011; Trust-Boundary Validation Policy, Crossing 2 (plan.md)
```bash
pytest tests/security/test_tool_poisoning.py -v
```
A stubbed Artifact Mutation Tool returns **schema-valid** `RemediationEvidence` that names an artifact outside `authorized_scope` and cites an inconsistent `source_version`; a second case returns `NoOpEvidence` for an artifact that was in fact mutated.
**Expected**: rejected at the expected-artifact-identity / authorization-scope layers of Crossing 2, workflow enters `REVIEW_REQUIRED`, target artifact SHA-256 unchanged, no `REMEDIATION_COMPLETED` transition, rejection evidence written to `evidence/security/tool_poisoning_rejected.json`.

### VS-10: Trust-Boundary Rejection at Non-Remediation Crossings (planned; not implemented yet)
**Proves**: FR-004, FR-005, FR-011; Trust-Boundary Validation Policy (plan.md)
- **Delivery**: `DeliveryResult` with `delivered: true` and no resolvable receipt → NOT recorded as `DELIVERED`; `evidence/security/delivery_assertion_rejected.json`.
- **Observation**: `FieldObservation` with an out-of-enum value, or carrying a PASS/FAIL claim, or with an out-of-order `event_sequence` → rejected, never coerced; `evidence/security/observation_rejected.json`.
- **ChangeSet**: agent-asserted `is_affected` without the four FR-002 conditions satisfied → impact not established; `evidence/security/changeset_rejected.json`.
- **Veo**: successful generation with failed delivery → delivery status remains unset, `PROOF_COMPLETE` unaffected.

### VS-11: NO_OP Remediation Evidence (planned; not implemented yet)
**Proves**: SC-003 (no-op path), completion condition 3(b)
An already-compliant artifact is processed. **Expected**: `NoOpEvidence` recorded with `evaluated_artifact_ref`, `evaluated_artifact_hash`, `observed_value == expected_value`, and `no_op_reason` — with **no** `before_ref`/`after_ref`, no fabricated after-state, no diff rendering, and no write to the artifact. The Change Proof satisfies condition 3 by the no-op path and remains independently auditable.

### VS-12: Data Classification & Lineage Validation (planned; not implemented yet)
**Proves**: FR-010, SC-013
```bash
pytest tests/unit/truth_engine/test_data_lineage.py -v
```
Every judged evidence item carries a `DataClassification` with a non-exclusive `labels` set and an ordered `lineage` chain. Coverage:
- synthetic SOP fixture → `labels: [SYNTHETIC]`;
- real Gemini call over a synthetic fixture → `labels: [REAL]`, lineage referencing the synthetic source;
- derived observation from a real photo of the demo box → `labels: [DERIVED, REAL]` with both photo and demo-scenario lineage entries;
- any emulated dependency → `labels: [SIMULATED]` (e.g. the `SIMULATED` local agent-registry manifest when the Agent Registry gate fails);
- Change Proof → `labels: [DERIVED]` with lineage covering every contributing evidence ref.
**Expected**: no judged evidence item lacks a classification; no lineage chain is broken; no `REAL` label is applied to an emulated dependency.
**Evidence**: `evidence/reports/data_lineage.json`

### VS-13: Idempotency, Duplicate Evidence & Crash Reconciliation (planned; not implemented yet)
**Proves**: FR-002 (cardinality), FR-007, FR-008, FR-011; SC-010, SC-011
```bash
pytest tests/unit/truth_engine/test_action_idempotency.py -v
```
- **Transport duplicate field evidence**: the same `submission_id` re-delivered → resolves to the existing `VerificationEvent`; no second authoritative attempt, no newer `event_sequence`, no duplicated proof evidence.
- **New attempt**: a different `submission_id` after FAIL → a distinct verification attempt that may produce the corrected PASS (US6 path preserved).
- **Crash after successful mutation**: mutation applied externally, process dies before `REMEDIATION_COMPLETED` persists, workflow resumes with the artifact already at `TOP_RIGHT` → reconciliation over stored pre-action intent + validated post-state records `MutationEvidence` with `reconciled = true`, **never** `NoOpEvidence`.
- **Unsafe reconciliation**: intent missing, invariants violated, or post-state not exactly the intended after-state → fail closed to `REVIEW_REQUIRED`, no fabricated evidence.
- **Delivery retry**: lost response with a recoverable receipt → reconciled without a second delivery; no receipt → stays `FAILED_OR_UNCERTAIN`.
- **Proof singularity**: repeated `GENERATE_PROOF` attempts → one canonical `proof_id`.
- **Cardinality**: zero qualified artifacts → `REVIEW_REQUIRED` with candidate evidence and no `affected_artifact_id`; exactly one → proceeds; more than one → `REVIEW_REQUIRED` with the full candidate set, no arbitrary selection.
**Evidence**: `evidence/runs/hero_run_001/idempotency_log.json`, `evidence/runs/hero_run_001/restart_recovery.json`

### VS-14: Frontline Surface Minimums (planned; not implemented yet)
**Proves**: spec.md § Frontline Surface Minimums (supports FR-004, FR-005; no FR/SC depends on it)
Manual or lightweight automated check of the demo surface against the six minimums:
1. hero flow usable on a narrow phone viewport; 2. `FAIL`/`INCONCLUSIVE`/`PASS` conveyed as **text**, never color alone; 3. hero-flow controls carry accessible text labels; 4. file-upload fallback works when camera capture is unavailable; 5. validation/error feedback readable as text; 6. desktop core controls keyboard-operable.
**Expected**: all six satisfied, or any exception recorded honestly in `LIMITATIONS.md`. This is a minimum-interaction check, not a WCAG conformance audit and not a design review.
**Evidence**: `evidence/reports/frontline_minimums.json`

### VS-9: Security — Agent Gateway ALLOW / DENY (planned; OPTIONAL / TRACK ENHANCEMENT)
**Proves**: least-privilege boundary between the Remediation Agent and the Frontline Enablement Agent
- **ALLOW**: Remediation Agent identity → Agent Gateway → `artifact-mutation-tool` → succeeds; allow decision logged with the caller SPIFFE principal and tool name.
- **DENY**: Frontline Enablement Agent identity → Agent Gateway → `artifact-mutation-tool` → denied by IAP enforcement; deny entry logged; target artifact hash unchanged.
**If the GEAP gate fails**: the same ALLOW/DENY pair runs against the deterministic in-process authorization broker, and the evidence is labelled application-level enforcement, not platform-enforced.

### VS-7: Cloud Deployment Smoke Test
```bash
# Deploy to Cloud Run
adk deploy cloud_run --project $PROJECT_ID --region $REGION

# Trigger via Pub/Sub
gcloud pubsub topics publish driftzero-approved-changes --message-file fixtures/hero_change.json

# Verify workflow completed in Firestore
python -m driftzero.cli status --workflow-id $WF_ID --remote
```

## Evidence Pack Structure

```
evidence/
├── README.md                    # Judge entry point
├── JUDGES_START_HERE.md         # Quick demo walkthrough
├── LIMITATIONS.md               # Honest known limitations
├── MANIFEST.json                # Evidence index with hashes
├── raw/                         # Unprocessed evidence
│   ├── approved_change.json     # Source change fixture
│   ├── stale_artifact_before.json
│   └── field_images/
├── fixtures/                    # Reproducible test fixtures
│   ├── hero_change.json
│   ├── multimodal/
│   └── security/
├── runs/                        # Recorded execution runs
│   └── hero_run_001/
│       ├── state_transitions.json
│       ├── agent_traces.json
│       ├── impact_determination.json   # candidate set + qualification results
│       ├── delivery_receipt.json       # positive delivery evidence
│       ├── idempotency_log.json        # ActionExecution ledger + duplicate absorption
│       ├── restart_recovery.json       # crash reconciliation evidence
│       └── change_proof.json
├── reports/                     # Test reports
│   ├── unit_test_report.xml
│   ├── multimodal_eval.json
│   ├── data_lineage.json        # FR-010 / SC-013 classification + lineage coverage
│   └── frontline_minimums.json  # Frontline Surface Minimums check
├── geap_access_gate.json        # Per-component ACCESS_CHECK results (PASS/FAIL + fallback taken)
├── cost_model.json              # ESTIMATED unit drivers/caps vs ACTUAL COST OBSERVED from billing
├── g1_gemma_feasibility.json     # G1 GO/FALLBACK decision + fixture results
├── security/                    # Security evidence
│   ├── prompt_injection_blocked.json
│   ├── tool_poisoning_rejected.json
│   ├── changeset_rejected.json
│   ├── delivery_assertion_rejected.json
│   ├── observation_rejected.json
│   └── gateway_deny_enablement_to_mutation_tool.json
└── replays/                     # Reproducible replay data
```
