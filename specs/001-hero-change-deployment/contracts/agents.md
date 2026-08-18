# Agent Contracts: Hero Change Deployment

**Feature**: `001-hero-change-deployment`
**Date**: 2026-08-17

## Agent Topology (4 Agents + 1 Deterministic Service)

### 1. Change Intelligence Agent (`change_intel_agent`)

**Type**: ADK `LlmAgent`
**Model**: `gemini-3.5-flash`
**Responsibility**: Interpret approved source change, extract structured ChangeSet, identify candidate affected artifacts.

**Permissions**: READ-ONLY
- Read approved source procedure (GCS)
- Read downstream artifact registry (Firestore)
- NO write access to source procedure
- NO write access to downstream artifacts

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

**Failure handling**: Malformed or ambiguous extraction → workflow enters `REVIEW_REQUIRED`

---

### 2. Remediation Agent (`remediation_agent`)

**Type**: ADK `LlmAgent`
**Model**: `gemini-3.5-flash`
**Responsibility**: Apply authorized atomic patch to affected downstream artifact.

**Permissions**: SCOPED WRITE
- Read affected artifact content (GCS)
- Write ONLY to authorized derived artifacts (GCS)
- NO write access to source procedure
- NO write access to non-authorized artifacts

**Input Tool**: `read_artifact_content` → current artifact content
**Output Tool**: `write_remediated_artifact` → patched artifact + before/after refs

```python
class RemediationResult(BaseModel):
    artifact_id: str
    remediation_type: str  # "MUTATION" | "NO_OP"
    before_ref: str  # GCS URI
    after_ref: str  # GCS URI
    before_value: str
    after_value: str
    patch_description: str
```

**Autonomy gate** (checked by deterministic Truth Engine BEFORE agent executes):
All 9 autonomous boundary conditions must be satisfied. If not → `REVIEW_REQUIRED`.

**Failure handling**: Mutation failure → action NOT marked completed, retry permitted.

---

### 3. Frontline Enablement Agent (`enablement_agent`)

**Type**: ADK `LlmAgent`
**Model**: `gemini-3.5-flash`
**Responsibility**: Compose and deliver operational delta to affected worker. Optionally generate Veo microtraining asset.

**Permissions**: READ + NOTIFY
- Read ChangeSet and remediation result
- Send notification/delta to worker identity
- Optionally invoke Veo 3.1 API for training video
- NO write to procedures or artifacts
- NO verification authority

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

**Failure handling**: Delivery failure → NOT marked as `DELIVERED`, retry permitted.

---

### 4. Field Verification Agent (`verification_agent`)

**Type**: ADK `LlmAgent` (wrapping Gemma 4 call)
**Model**: `gemma-4-12b` (via Cloud Run GPU endpoint)
**Responsibility**: Process raw frontline evidence image → derive normalized observation.

**Permissions**: READ-ONLY + INFERENCE
- Read raw evidence image (GCS or upload)
- Invoke Gemma 4 model for vision inference
- NO workflow state mutation authority
- NO PASS/FAIL decision authority

**Output**:
```python
class FieldObservation(BaseModel):
    raw_evidence_ref: str  # GCS URI to raw image
    observed_label_position: str  # "LEFT" | "TOP_RIGHT" | "INCONCLUSIVE"
    confidence_note: str  # informational only, NOT authoritative
```

**Critical boundary**: The agent produces ONLY the derived observation. The deterministic Truth Engine performs: `observed == expected → PASS`, `observed != expected → FAIL`, `observed == INCONCLUSIVE → VERIFICATION_INCONCLUSIVE`.

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

**Hallucination/malformation handling**: Every agent output is validated by the Truth Engine against Pydantic schemas before state transition. Invalid output → `REVIEW_REQUIRED` or retry.

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
