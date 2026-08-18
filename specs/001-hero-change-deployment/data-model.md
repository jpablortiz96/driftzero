# Data Model: Hero Change Deployment

**Feature**: `001-hero-change-deployment`
**Date**: 2026-08-17

## Entities

### ApprovedChange

Represents the detected approved operational delta from the authoritative source procedure.

| Field | Type | Description |
|---|---|---|
| `change_id` | `str (UUID)` | Unique logical change identifier |
| `source_procedure_id` | `str` | Authoritative procedure identifier |
| `source_version` | `str` | Approved version that introduced the change |
| `previous_version` | `str` | Prior version being superseded |
| `operation_id` | `str` | Specific operation affected (e.g., `packing_label_placement`) |
| `requirement_id` | `str` | Specific requirement within the operation |
| `previous_value` | `str` | Previous approved value (e.g., `LEFT`) |
| `current_value` | `str` | New approved value (e.g., `TOP_RIGHT`) |
| `authorized_scope` | `list[str]` | Artifact IDs authorized for remediation |
| `approved_status` | `str` | Provenance/approval status |
| `source_evidence_ref` | `str` | Reference to source evidence (GCS URI or document ID) |
| `received_at` | `datetime` | When the change was ingested |
| `data_classification` | `DataClassification` | Classification of this record |

### DownstreamArtifact

Represents an authorized operational artifact that may be affected by a change.

| Field | Type | Description |
|---|---|---|
| `artifact_id` | `str` | Unique artifact identifier |
| `artifact_type` | `str` | Type (e.g., `work_instruction`) |
| `operation_id` | `str` | Associated operation |
| `requirement_id` | `str` | Specific requirement this artifact implements |
| `current_value` | `str` | Current represented value |
| `content_ref` | `str` | Reference to artifact content (GCS URI) |
| `authorized_for_remediation` | `bool` | Whether this artifact is in authorized scope |
| `data_classification` | `DataClassification` | Classification of this record |

### Workflow

The core state machine tracking a single change deployment lifecycle.

| Field | Type | Description |
|---|---|---|
| `workflow_id` | `str (UUID)` | Unique workflow/run identifier |
| `change_id` | `str` | Associated change |
| `source_version` | `str` | Applicable source version |
| `state` | `WorkflowState` | Current lifecycle state (enum) |
| `affected_artifact_id` | `str or None` | Identified affected artifact |
| `impact_reason` | `str or None` | Auditable reason for impact classification |
| `remediation_evidence` | `RemediationEvidence or None` | Discriminated remediation outcome (`MutationEvidence` or `NoOpEvidence`); None until remediation is validly recorded |
| `delivery_status` | `str or None` | `DELIVERED` or None |
| `delivery_ref` | `str or None` | Evidence of delivery |
| `worker_id` | `str` | Opaque worker/demo identifier |
| `verification_events` | `list[VerificationEvent]` | Ordered verification history |
| `latest_verification_status` | `str or None` | `PASS`, `FAIL`, `INCONCLUSIVE`, or None |
| `proof_id` | `str or None` | Associated Change Proof ID if complete |
| `created_at` | `datetime` | Workflow creation timestamp |
| `updated_at` | `datetime` | Last state transition timestamp |
| `event_sequence` | `int` | Monotonic event counter for ordering |
| `data_classification` | `DataClassification` | Classification of this record |

### RemediationEvidence (discriminated union)

`RemediationEvidence = MutationEvidence | NoOpEvidence`, discriminated on `remediation_type`. The two variants are **structurally different on purpose**: an already-compliant artifact must never be represented with a fabricated mutation, a synthetic after-state, or a before/after pair implying a change that did not occur. Each variant is independently auditable — the evidence required to establish it stands on its own without reference to the other variant.

#### MutationEvidence (`remediation_type = "MUTATION"`)

| Field | Type | Description |
|---|---|---|
| `remediation_type` | `Literal["MUTATION"]` | Discriminator |
| `artifact_id` | `str` | Remediated artifact |
| `before_ref` | `str` | GCS URI to the artifact content before mutation |
| `after_ref` | `str` | GCS URI to the artifact content after mutation |
| `before_hash` | `str` | SHA-256 of the before content |
| `after_hash` | `str` | SHA-256 of the after content |
| `before_value` | `str` | Prior represented value (e.g. `LEFT`) |
| `after_value` | `str` | New represented value (e.g. `TOP_RIGHT`) |
| `patch_description` | `str` | Auditable description of the atomic change applied |
| `data_classification` | `DataClassification` | Classification of this evidence |

**Audit question it answers**: what was changed, from what, to what, and are both states retrievable and hash-verifiable?

#### NoOpEvidence (`remediation_type = "NO_OP"`)

| Field | Type | Description |
|---|---|---|
| `remediation_type` | `Literal["NO_OP"]` | Discriminator |
| `artifact_id` | `str` | Artifact evaluated |
| `evaluated_artifact_ref` | `str` | GCS URI to the artifact content **as evaluated** (single state — no before/after pair) |
| `evaluated_artifact_hash` | `str` | SHA-256 of the evaluated content |
| `observed_value` | `str` | Value the artifact already represented |
| `expected_value` | `str` | Approved value it was compared against |
| `no_op_reason` | `str` | Auditable reason the artifact was already compliant |
| `compliance_basis` | `str` | Which instruction/field established compliance (locator within the artifact) |
| `data_classification` | `DataClassification` | Classification of this evidence |

**Audit question it answers**: which artifact was evaluated, in what exact state, against which approved value, and on what basis was it judged already compliant — without asserting that anything was modified.

**Prohibited representations**: `NoOpEvidence` MUST NOT carry `before_ref`/`after_ref`, MUST NOT duplicate the same reference into a before/after pair, and MUST NOT be recorded when the artifact was in fact mutated. `MutationEvidence` MUST NOT be recorded when no write occurred.

### VerificationEvent

Individual field verification attempt within a workflow.

| Field | Type | Description |
|---|---|---|
| `event_id` | `str (UUID)` | Unique event identifier |
| `workflow_id` | `str` | Parent workflow |
| `event_sequence` | `int` | Monotonic sequence for chronological ordering |
| `raw_evidence_ref` | `str` | GCS URI to raw evidence (image) |
| `derived_observation` | `str` | Normalized observation (`LEFT`, `TOP_RIGHT`, `INCONCLUSIVE`) |
| `expected_value` | `str` | Current approved expected value |
| `verification_result` | `str` | `PASS`, `FAIL`, `INCONCLUSIVE` |
| `timestamp` | `datetime` | When evidence was submitted |
| `data_classification` | `DataClassification` | Classification of this event |

### ChangeProof

The immutable auditable attestation generated upon valid workflow completion.

| Field | Type | Description |
|---|---|---|
| `proof_id` | `str (UUID)` | Unique proof identifier |
| `workflow_id` | `str` | Associated workflow |
| `change_id` | `str` | Associated change |
| `source_procedure_id` | `str` | Authoritative source |
| `source_version` | `str` | Applicable version |
| `previous_value` | `str` | Previous requirement |
| `current_value` | `str` | Current requirement |
| `affected_artifact_id` | `str` | Artifact that was remediated or validly established as already compliant |
| `remediation_evidence` | `RemediationEvidence` | Discriminated evidence: `MutationEvidence` **or** `NoOpEvidence`. Satisfies completion condition 3 by either path; the proof never contains a fabricated after-state |
| `delivery_status` | `str` | `DELIVERED` |
| `delivery_ref` | `str` | Delivery evidence |
| `verification_result` | `str` | `PASS` |
| `verification_event_id` | `str` | Authoritative passing verification event |
| `worker_id` | `str` | Opaque worker identifier |
| `evidence_manifest` | `EvidenceManifest` | Complete evidence references |
| `completion_timestamp` | `datetime` | When proof was issued |
| `content_hash` | `str` | SHA-256 of canonical proof JSON |
| `data_classification` | `DataClassification` | Always `DERIVED` |

### DataClassification

Non-exclusive lineage classification.

| Field | Type | Description |
|---|---|---|
| `labels` | `list[str]` | Non-exclusive set: `REAL`, `SYNTHETIC`, `DERIVED`, `SIMULATED` |
| `lineage` | `list[LineageEntry]` | Ordered provenance chain |

### LineageEntry

| Field | Type | Description |
|---|---|---|
| `source_ref` | `str` | Reference to source evidence/artifact |
| `source_classification` | `list[str]` | Classification labels of the source |
| `relationship` | `str` | e.g., `derived_from`, `observed_from`, `input_to` |

### EvidenceManifest

Complete evidence collection for a Change Proof.

| Field | Type | Description |
|---|---|---|
| `source_change_ref` | `str` | Reference to ApprovedChange record |
| `affected_artifact_ref` | `str` | Reference to impact determination |
| `remediation_evidence_refs` | `list[str]` | For `MUTATION`: the before and after artifact URIs. For `NO_OP`: the single evaluated-artifact URI. Never a synthesized before/after pair |
| `rejected_result_refs` | `list[str]` | References to agent/tool results rejected at a trust boundary, with the failing validation layer recorded |
| `delivery_ref` | `str` | Delivery evidence |
| `verification_refs` | `list[str]` | All verification event references |
| `state_transition_refs` | `list[str]` | State transition log references |
| `content_hashes` | `dict[str, str]` | SHA-256 hashes for all referenced artifacts |

## State Transitions (WorkflowState enum)

```
CHANGE_RECEIVED
  → IMPACT_DETERMINED
  → FAILED

IMPACT_DETERMINED
  → REMEDIATION_PENDING
  → REVIEW_REQUIRED
  → FAILED

REMEDIATION_PENDING
  → REMEDIATION_COMPLETED
  → REVIEW_REQUIRED
  → FAILED

REVIEW_REQUIRED
  (blocking in S1 — no autonomous exit)

REMEDIATION_COMPLETED
  → FRONTLINE_DELIVERY_COMPLETED
  → FAILED

FRONTLINE_DELIVERY_COMPLETED
  → AWAITING_FIELD_VERIFICATION
  → FAILED

AWAITING_FIELD_VERIFICATION
  → VERIFICATION_PASSED
  → VERIFICATION_FAILED
  → VERIFICATION_INCONCLUSIVE
  → FAILED

VERIFICATION_FAILED
  → AWAITING_FIELD_VERIFICATION (retry with new evidence)
  → FAILED

VERIFICATION_INCONCLUSIVE
  → AWAITING_FIELD_VERIFICATION (retry with clearer evidence)
  → FAILED

VERIFICATION_PASSED
  → PROOF_COMPLETE

PROOF_COMPLETE
  (terminal — immutable)

SUPERSEDED
  (terminal — entered from any non-terminal state when newer source version arrives)

FAILED
  (terminal — unrecoverable)
```

## Validation Rules

- `change_id` uniqueness enforced (idempotency key)
- `workflow_id` uniqueness enforced
- `proof_id` uniqueness enforced
- `event_sequence` monotonically increasing per workflow
- State transitions validated against legal transition matrix
- `PROOF_COMPLETE` requires all 7 completion invariants satisfied
- Completion condition 3 is satisfied by **exactly one** of two independently auditable paths: a valid `MutationEvidence` record, or a valid `NoOpEvidence` record. Neither substitutes for the other, and a proof carrying both for the same artifact is invalid
- `MutationEvidence` is valid only when `before_hash != after_hash`, both refs resolve, `before_hash` matches the artifact pre-state hash the Truth Engine recorded before invoking remediation, and `after_value` equals the approved `current_value`
- `NoOpEvidence` is valid only when `observed_value == expected_value`, the evaluated ref resolves, and `evaluated_artifact_hash` matches the content at evaluation time. No mutation may have been performed on that artifact within the workflow
- A `NO_OP` outcome MUST NOT be recorded with `before_ref`/`after_ref` fields, and MUST NOT be rendered as a diff
- `SUPERSEDED` may be entered from any non-terminal state
- `FAILED` may be entered from any non-terminal state
- No transition OUT of `PROOF_COMPLETE`, `SUPERSEDED`, or `FAILED`
- `VerificationEvent.verification_result` derived deterministically: `observed == expected → PASS`, `observed != expected and observed != INCONCLUSIVE → FAIL`, else `INCONCLUSIVE`

## Integrity Hash Semantics

All `*_hash` fields and `EvidenceManifest.content_hashes` are SHA-256 content digests. They establish **content identity and replacement/alteration detection**: a referenced artifact can be shown to be byte-identical to what was recorded at proof time, and silent substitution becomes detectable by comparison.

They do **not** establish a digital signature, a trusted timestamp, identity attestation, proof of authorship, non-repudiation, or ledger/blockchain immutability. No field in this data model may be described with those properties. `ChangeProof.content_hash` is a canonical-JSON digest of the proof document, not an attestation of it.
