# Agent Contracts: Hero Change Deployment

**Feature**: `001-hero-change-deployment`
**Date**: 2026-08-17

## Identity Model

Each agent has its **own identity**. Two mutually exclusive provisioning models, selected by the GEAP Availability Gate in plan.md:

- **PRIMARY — Agent Identity** (GEAP available): each agent receives its own SPIFFE-based Agent Identity issued by Google Cloud at deployment to Agent Runtime, used as an IAM principal of the form
  `principal://agents.global.org-ORGANIZATION_ID.system.id.goog/resources/aiplatform/projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/AGENT_ENGINE_ID`.
- **FALLBACK — dedicated service accounts** (Agent Identity not provisionable): each agent runs under its own minimally-scoped Cloud Run **service account**, with the mutation-capability restriction enforced by a deterministic in-process authorization broker in the Truth Engine.

**Terminology rule**: a service account is never called an "Agent Identity". Service accounts appear only as Cloud Run service/runtime identities or as the named fallback, and the fallback's weaker security properties are recorded in `LIMITATIONS.md`.

| Agent | Agent Identity (primary) | Fallback runtime identity | Authorized for the Artifact Mutation Tool? |
|---|---|---|---|
| Change Intelligence | `driftzero-change-intel` | `driftzero-change-intel-sa@…` | **No** |
| Remediation | `driftzero-remediation` | `driftzero-remediation-sa@…` | **Yes — the only authorized identity** |
| Frontline Enablement | `driftzero-enablement` | `driftzero-enablement-sa@…` | **No — negative security test subject** |
| Field Verification | `driftzero-field-verify` (only where deployed on Agent Runtime; a Cloud Run GPU inference service instead runs under its own dedicated Cloud Run service account, which is a runtime identity, not an Agent Identity) | `driftzero-field-verify-sa@…` | **No** |

## Trust-Boundary Policy (applies to every agent below)

**General principle**: every non-authoritative agent or tool result crossing into the deterministic Truth Engine MUST be validated before it can affect authoritative workflow state. No result is trusted because it is well-formed; schema validity is a necessary first filter, never a sufficient one. Validation is **context-appropriate** — each crossing has a defined minimum, listed per agent below and specified in full in plan.md § Trust-Boundary Validation Policy.

| Crossing | Result | Agent may | Agent may NOT |
|---|---|---|---|
| 1 | `ChangeSet` | Propose impact candidates with auditable reasons | Determine impact, authorization, or applicability |
| 2 | `RemediationEvidence` | Report a mutation or an already-compliant no-op | Establish that remediation counts toward completion |
| 3 | `DeliveryResult` | Deliver and return a mechanism receipt | Assert `DELIVERED` without positive delivery evidence |
| 4 | `FieldObservation` | Return a bounded normalized observation | Return or imply an authoritative PASS/FAIL |
| 5 | Veo output | Attach a generated asset | Establish delivery truth by generation success |

## Agent Topology (4 Agents + 1 Deterministic Service)

### 1. Change Intelligence Agent (`change_intel_agent`)

**Type**: ADK `LlmAgent`
**Model**: `gemini-3.5-flash`
**Responsibility**: Interpret approved source change, extract structured ChangeSet, identify candidate affected artifacts.

**Identity**: `driftzero-change-intel` Agent Identity (primary) / dedicated service account (fallback).

**Permissions**: READ-ONLY
- Read approved source procedure (GCS)
- Read downstream artifact registry (Firestore)
- NO write access to source procedure
- NO write access to downstream artifacts
- NOT authorized for the Artifact Mutation Tool

**Untrusted content handling**: artifact text reaching this agent is untrusted. Where Model Armor is available, the `generateContent` call carries `modelArmorConfig.promptTemplateName = driftzero-untrusted-artifact-text` (`INSPECT_AND_BLOCK`); a `blockReason: "MODEL_ARMOR"` response fails closed to `REVIEW_REQUIRED`. Screening is content protection only — it grants no authorization and decides no state.

**Input Tool**: `read_approved_change` → raw approved change document
**Input Tool**: `read_artifact_registry` → list of authorized downstream artifacts
**Output**: Structured `ChangeSet` (Pydantic model)

```python
class ChangeSet(BaseModel):
    change_id: str
    source_procedure_id: str
    source_version: str
    operation_id: str
    requirement_id: str
    previous_value: str
    current_value: str
    authorized_scope: list[str]
    candidate_affected_artifacts: list[AffectedArtifactCandidate]

class AffectedArtifactCandidate(BaseModel):
    artifact_id: str
    impact_reason: str  # auditable explanation
    operation_match: bool
    instruction_correspondence: bool
    value_conflict: bool
    in_authorized_scope: bool
    is_affected: bool  # all 4 conditions true
```

**Trust-boundary validation (Crossing 1)**: schema · workflow/change provenance (`change_id` + `correlation_id`) · source-version association (applicable and non-superseded) · expected source and artifact identities (referenced artifact IDs exist in the registry; source procedure ID is the one ingested) · semantic domain invariants (`previous_value` matches the recorded prior approved value; exactly one atomic requirement change described).

**Authority boundary**: `candidate_affected_artifacts` and `is_affected` are **proposals**. The Truth Engine decides whether the FR-002 impact conditions are satisfied; an agent-set `is_affected` flag never establishes impact.

**Failure handling**: Malformed or ambiguous extraction → workflow enters `REVIEW_REQUIRED`

---

### 2. Remediation Agent (`remediation_agent`)

**Type**: ADK `LlmAgent`
**Model**: `gemini-3.5-flash`
**Responsibility**: Apply authorized atomic patch to affected downstream artifact.

**Identity**: `driftzero-remediation` Agent Identity (primary) / dedicated service account (fallback). This is the **only** identity authorized to invoke the Artifact Mutation Tool.

**Permissions**: SCOPED WRITE
- Read affected artifact content (GCS)
- Write ONLY to authorized derived artifacts (GCS), exclusively through the Artifact Mutation Tool
- NO write access to source procedure
- NO write access to non-authorized artifacts

**Input Tool**: `read_artifact_content` → current artifact content
**Output Tool**: `apply_authorized_artifact_patch` → discriminated `RemediationEvidence`

The output is a **discriminated union**, not one shape with optional fields. An already-compliant artifact must never be reported with a fabricated mutation or a synthetic after-state (data-model.md § RemediationEvidence):

```python
class MutationEvidence(BaseModel):
    remediation_type: Literal["MUTATION"]
    artifact_id: str
    before_ref: str        # GCS URI to content before mutation
    after_ref: str         # GCS URI to content after mutation
    before_hash: str       # SHA-256 of before content
    after_hash: str        # SHA-256 of after content
    before_value: str
    after_value: str
    patch_description: str

class NoOpEvidence(BaseModel):
    remediation_type: Literal["NO_OP"]
    artifact_id: str
    evaluated_artifact_ref: str    # single evaluated state — NO before/after pair
    evaluated_artifact_hash: str
    observed_value: str
    expected_value: str
    no_op_reason: str
    compliance_basis: str          # locator of the instruction establishing compliance

RemediationEvidence = MutationEvidence | NoOpEvidence
```

Both variants are **independently auditable**: `MutationEvidence` answers "what changed, from what, to what, and are both states hash-verifiable"; `NoOpEvidence` answers "which artifact was evaluated, in what exact state, against which approved value, and on what basis was it already compliant". Completion condition 3 is satisfied by exactly one of them.

**Autonomy gate** (checked by deterministic Truth Engine BEFORE agent executes):
All 9 autonomous boundary conditions must be satisfied. If not → `REVIEW_REQUIRED`.

**Failure handling**: Mutation failure → action NOT marked completed, retry permitted.

**Trust-boundary validation (Crossing 2)**: schema · provenance/correlation · expected tool identity · expected artifact identity · authorization scope (`artifact_id ∈ authorized_scope`; caller is the mutation-authorized identity) · source-version applicability · before-state hash consistency (`before_hash` equals the pre-state hash the Truth Engine recorded before invoking the tool) · atomic-change invariants (exactly one requirement changed; `after_value` equals the approved `current_value`; no additional divergence; authoritative source untouched). A `NO_OP` claim is validated on its own terms — `evaluated_artifact_hash` resolves and `observed_value == expected_value` — and is rejected if presented with a fabricated before/after pair.

A schema-valid response naming an artifact outside `authorized_scope` or citing an inconsistent `source_version` is rejected, records rejection evidence into `EvidenceManifest.rejected_result_refs`, and enters `REVIEW_REQUIRED` with the target artifact hash unchanged.

---

### 3. Frontline Enablement Agent (`enablement_agent`)

**Type**: ADK `LlmAgent`
**Model**: `gemini-3.5-flash`
**Responsibility**: Compose and deliver operational delta to affected worker. Optionally generate Veo microtraining asset.

**Identity**: `driftzero-enablement` Agent Identity (primary) / dedicated service account (fallback).

**Permissions**: READ + NOTIFY
- Read ChangeSet and remediation result
- Send notification/delta to worker identity
- Optionally invoke Veo 3.1 API for training video
- NO write to procedures or artifacts
- NO verification authority
- **NOT authorized for the Artifact Mutation Tool.** An attempted call is the planned negative security test: with Agent Gateway promoted, the denial must come from real platform authorization/policy enforcement (no `roles/iap.egressor` binding on the tool endpoint, and no tool-name allow entry); in the fallback the denial comes from the deterministic in-process authorization broker and is labelled application-level enforcement.

**Output**:
```python
class DeliveryResult(BaseModel):
    worker_id: str
    delivery_mechanism: str  # e.g., "web_notification", "api_push"
    delta_content: str  # human-readable operational delta
    delivered: bool
    delivery_evidence_ref: str  # GCS URI or log reference
    training_video_ref: str | None  # optional Veo output GCS URI
```

**Trust-boundary validation (Crossing 3)**: schema · workflow/change provenance · intended worker identity (matches the workflow's `worker_id`) · expected delivery operation and channel · **positive delivery evidence/receipt** (`delivery_evidence_ref` must resolve to a receipt produced by the delivery mechanism itself) · source-version applicability.

**Authority boundary**: an agent asserting `delivered: true`, or narrating delivery in text, is **insufficient** and MUST NOT satisfy FR-004. `DELIVERED` is recorded only on positive mechanism evidence. A successful Veo generation (Crossing 5) never establishes delivery — `training_video_ref` is supplementary content attached to a delivery that must independently prove itself.

**Failure handling**: Delivery failure → NOT marked as `DELIVERED`, retry permitted.

---

### 4. Field Verification Agent (`verification_agent`)

**Type**: ADK `LlmAgent` (wrapping Gemma 4 call)
**Model**: `gemma-4-12b` (via Cloud Run GPU endpoint)
**Responsibility**: Process raw frontline evidence image → derive normalized observation.

**Identity**: `driftzero-field-verify` Agent Identity where deployed on Agent Runtime; where field verification remains a Cloud Run GPU inference service, that service runs under its own dedicated Cloud Run **service account** (a runtime identity, not an Agent Identity).

**Permissions**: READ-ONLY + INFERENCE
- Read raw evidence image (GCS or upload)
- Invoke Gemma 4 model for vision inference
- NO workflow state mutation authority
- NO PASS/FAIL decision authority
- NOT authorized for the Artifact Mutation Tool

**Note on screening**: field evidence images are NOT screened by Model Armor (text-only support; image screening is Preview). Image integrity relies on SHA-256 hashing plus the deterministic comparator, and this limitation is stated in `LIMITATIONS.md`.

**Output**:
```python
class FieldObservation(BaseModel):
    raw_evidence_ref: str  # GCS URI to raw image
    observed_label_position: str  # "LEFT" | "TOP_RIGHT" | "INCONCLUSIVE"
    confidence_note: str  # informational only, NOT authoritative
```

**Trust-boundary validation (Crossing 4)**: schema · workflow/change provenance · raw evidence reference (resolvable, hash-recorded, associated with this workflow) · expected verification operation · allowed normalized observation enum — `LEFT` | `TOP_RIGHT` | `INCONCLUSIVE`, any other value rejected rather than coerced · event chronology (monotonic `event_sequence`; an older event cannot override a newer one) · source-version applicability.

**Critical boundary**: The agent produces ONLY the derived observation. `FieldObservation` MUST NOT carry an authoritative PASS/FAIL, and `confidence_note` is informational only and never authoritative. The deterministic Truth Engine performs: `observed == expected → PASS`, `observed != expected → FAIL`, `observed == INCONCLUSIVE → VERIFICATION_INCONCLUSIVE`.

---

### 5. Truth Engine (Deterministic Service)

**Type**: Pure Python application logic (NOT an LLM agent)
**Responsibility**: All authoritative state management, transitions, invariants, and proof generation.

**Owns**:
- Workflow state machine (13 states, legal transitions)
- Change identity and idempotency (duplicate detection)
- Source version applicability check
- Autonomous remediation precondition evaluation (9 conditions)
- Authorization decisions
- Verification ordering (latest valid verification)
- Expected-vs-observed deterministic comparator
- Trust-boundary validation at all five non-authoritative crossings (ChangeSet, RemediationEvidence, DeliveryResult, FieldObservation, Veo output), with context-appropriate layers per crossing
- Discriminated remediation-outcome adjudication (`MutationEvidence` vs `NoOpEvidence`)
- Tool-invocation authorization broker in the fallback architecture (which agent identity may invoke which tool)
- Supersession detection and transition
- Retry deduplication
- Seven `PROOF_COMPLETE` invariant evaluation
- Evidence manifest assembly
- Change Proof generation with SHA-256 content hash
- Proof immutability enforcement

**NO LLM calls**. Pure deterministic logic.

---

## Orchestration: Hybrid Deterministic + LLM

**Top-level orchestrator**: ADK `SequentialAgent` wrapping the workflow steps in deterministic order. The Truth Engine validates preconditions and postconditions at each step boundary.

```
SequentialAgent("driftzero_hero_workflow"):
  1. Truth Engine: validate incoming change, check idempotency, check supersession
  2. Change Intelligence Agent: extract ChangeSet
  3. Truth Engine: validate ChangeSet, check autonomy preconditions
  4. Remediation Agent: apply authorized patch (if preconditions met)
  5. Truth Engine: validate remediation result, record evidence
  6. Frontline Enablement Agent: compose and deliver delta
  7. Truth Engine: validate delivery evidence
  8. [ASYNC PAUSE — await field verification evidence]
  9. Field Verification Agent: process evidence image
  10. Truth Engine: deterministic PASS/FAIL, evaluate completion invariants
  11. Truth Engine: generate Change Proof if all conditions met
```

**Async boundary**: After step 7, the workflow pauses (ADK ResumabilityConfig) awaiting field evidence submission via API. Steps 9-11 execute upon evidence arrival.

**Hallucination/malformation handling**: Every agent output is validated by the Truth Engine before state transition — schema first, then the context-appropriate trust-boundary layers for that crossing (§ Trust-Boundary Policy). Schema conformance alone never authorizes a transition. Invalid output → `REVIEW_REQUIRED` or retry.

---

## Planned Tool Contract: Artifact Mutation Tool (DESIGN ONLY — NOT IMPLEMENTED)

The authorized mutation capability is specified here as a contract only. **No implementation is produced in this correction.** It is planned as an authenticated tool/service compatible with the Agent Gateway protocol selected at implementation time (MCP is the documented option supporting per-tool authorization by tool name and read-only/read-write character).

**Governed path (OPTIONAL / TRACK ENHANCEMENT, gated by plan.md § GEAP Availability Gate):**

| Attribute | ALLOW | DENY (negative security test) |
|---|---|---|
| Traffic direction | Egress (Agent-to-Anywhere) | Egress (Agent-to-Anywhere) |
| Caller | Remediation Agent (its Agent Identity) | Frontline Enablement Agent (its Agent Identity) |
| Target tool | `artifact-mutation-tool`, operation `apply_authorized_artifact_patch` (read-write) | Same |
| Allow policy | `roles/iap.egressor` on the registered endpoint granted to the Remediation Agent principal only, plus an MCP tool-name-scoped authorization policy | No binding, no tool-name allow entry → rejected by IAP runtime enforcement |
| Expected audit evidence | Gateway telemetry + Cloud Logging allow entry (caller SPIFFE principal, tool name), Cloud Trace span, before/after artifact refs | Cloud Logging deny entry (caller SPIFFE principal, tool name), artifact SHA-256 unchanged, no `REMEDIATION_COMPLETED` transition, `evidence/security/gateway_deny_enablement_to_mutation_tool.json` |

**Operation shape** (contract sketch, deliberately minimal):

```
apply_authorized_artifact_patch(
    artifact_id: str,
    requirement_id: str,
    expected_before_value: str,
    new_value: str,
    source_procedure_id: str,
    source_version: str,
    change_id: str,
    correlation_id: str,
) -> RemediationEvidence   # MutationEvidence | NoOpEvidence
```

**Non-negotiable properties:**
- Read-write; exactly one atomic requirement change per call.
- The tool has no access whatsoever to the authoritative source procedure.
- The tool's response is never trusted on schema validity alone — the Truth Engine applies the Crossing 2 trust-boundary layers (plan.md § Trust-Boundary Validation Policy) before any state advance, and adjudicates the discriminated `MutationEvidence` / `NoOpEvidence` outcome.
- If Agent Gateway is unavailable, the same authorization boundary is enforced by the deterministic in-process broker, and evidence is labelled application-level enforcement rather than platform-enforced.

---

## API Contract (FastAPI)

### POST /api/v1/changes
Publish an approved change event (alternative to Pub/Sub for demo).
**Request**: `ApprovedChangeRequest` (JSON)
**Response**: `{ "workflow_id": "...", "state": "CHANGE_RECEIVED" }`

### GET /api/v1/workflows/{workflow_id}
Get current workflow state and evidence summary.
**Response**: `WorkflowStatus` (JSON)

### POST /api/v1/workflows/{workflow_id}/verify
Submit field verification evidence.
**Request**: Multipart (image file + metadata)
**Response**: `{ "verification_result": "PASS|FAIL|INCONCLUSIVE", "workflow_state": "..." }`

### GET /api/v1/workflows/{workflow_id}/proof
Retrieve completed Change Proof.
**Response**: `ChangeProof` (JSON) or 404 if not complete.

### GET /api/v1/workflows/{workflow_id}/evidence
List all evidence artifacts for a workflow.
**Response**: `EvidenceManifest` (JSON)
