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
| `affected_artifact_id` | `str or None` | The single qualified affected artifact. Populated **only** when exactly one candidate passes deterministic qualification (spec.md § Affected Artifact Cardinality); remains None for the zero-qualified and multi-qualified cases |
| `impact_reason` | `str or None` | Auditable reason for impact classification |
| `candidate_artifact_refs` | `list[str]` | All evaluated candidates with their per-condition qualification results. Retained as evidence in every case — notably for the zero-qualified and multi-qualified `REVIEW_REQUIRED` outcomes |
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
| `reconciled` | `bool` | True when completion was established by post-crash reconciliation (§ Idempotency & Crash Reconciliation Rules) rather than an observed tool response. A reconciled mutation remains `MUTATION`, never `NO_OP` |
| `action_id` | `str` | Stable identity of the `REMEDIATE_ARTIFACT` action that produced this evidence |
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

### ActionExecution (idempotency / reconciliation ledger)

An internal deterministic record of one consequential logical side effect. **It is NOT a workflow lifecycle state** and never appears in the 13-state machine; it exists so retries, transport duplicates, and crash recovery resolve deterministically. Scope is limited to the four action types below — this is not a generalized workflow platform.

| Field | Type | Description |
|---|---|---|
| `action_id` | `str` | **Stable idempotency identity**, derived deterministically from (`workflow_id`, `action_type`, applicable `source_version`/`change_id`, target identity). Recomputing it for the same logical action yields the same value |
| `workflow_id` | `str` | Parent workflow |
| `action_type` | `str` | `REMEDIATE_ARTIFACT` \| `DELIVER_DELTA` \| `PROCESS_FIELD_EVIDENCE` \| `GENERATE_PROOF` |
| `status` | `str` | `PLANNED` \| `ATTEMPTED` \| `COMPLETED` \| `FAILED_OR_UNCERTAIN` |
| `target_ref` | `str` | Target identity (artifact ID, worker ID, evidence submission ID, or workflow ID for proof) |
| `intent` | `dict` | Pre-action intent recorded **before dispatch**: expected before-state ref/hash, expected after-state/value, source/change identity |
| `receipt_ref` | `str or None` | Tool/mechanism receipt when the external call returned one |
| `outcome_evidence_ref` | `str or None` | Reference to the evidence record produced on completion |
| `attempt_count` | `int` | Attempts made (bounded by the retry policy in plan.md) |
| `reconciled` | `bool` | True when completion was established by post-crash reconciliation rather than an observed response |
| `created_at` / `updated_at` | `datetime` | Timestamps |

**Status semantics**: `PLANNED` = intent persisted, not dispatched. `ATTEMPTED` = dispatched, outcome not yet confirmed. `COMPLETED` = outcome confirmed (observed response **or** validated reconciliation). `FAILED_OR_UNCERTAIN` = the attempt failed, or its outcome could not be established — the latter requires reconciliation before any retry.

### VerificationEvent

Individual field verification attempt within a workflow.

| Field | Type | Description |
|---|---|---|
| `event_id` | `str (UUID)` | Unique event identifier |
| `submission_id` | `str` | **Stable logical identity of the field-evidence submission** (client-supplied or derived from raw evidence content hash + workflow). A re-delivery carrying the same `submission_id` is a transport duplicate and MUST resolve to the existing `event_id`; a genuinely new attempt carries a different `submission_id` |
| `workflow_id` | `str` | Parent workflow |
| `event_sequence` | `int` | Monotonic sequence for chronological ordering. Allocated **once per distinct `submission_id`** — a transport duplicate never consumes a newer position |
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
| `proof_id` | `str (UUID)` | Unique proof identifier. **One canonical logical proof per workflow**: repeated generation attempts resolve to the same `proof_id` via the `GENERATE_PROOF` action identity; transport or process retry never creates a second proof |
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
  → SUPERSEDED   (newer approved source version makes this workflow obsolete)
  → FAILED       (separate, genuinely unrecoverable integrity/system condition)
  (blocking in S1 — NO autonomous exit back to the progressive workflow.
   Explicitly ILLEGAL in S1: → REMEDIATION_PENDING, → REMEDIATION_COMPLETED,
   → FRONTLINE_DELIVERY_COMPLETED, → AWAITING_FIELD_VERIFICATION,
   → VERIFICATION_PASSED, → PROOF_COMPLETE.
   Not a terminal state: a future out-of-scope reviewer-resolution capability
   may add a resume path, which does not exist in S1.)

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
- `SUPERSEDED` may be entered from any non-terminal state, including `REVIEW_REQUIRED`
- `FAILED` may be entered from any non-terminal state, including `REVIEW_REQUIRED`
- `REVIEW_REQUIRED` has exactly two legal S1 exits (`SUPERSEDED`, `FAILED`); any transition from `REVIEW_REQUIRED` into a progressive state is illegal in S1
- **Affected-artifact cardinality**: zero qualified candidates → `REVIEW_REQUIRED` with `candidate_artifact_refs` retained and `affected_artifact_id` left None; exactly one → persist `affected_artifact_id` and proceed; more than one → `REVIEW_REQUIRED` with the full candidate set retained, no arbitrary selection, no multi-artifact mutation
- **Action identity**: every consequential side effect (`REMEDIATE_ARTIFACT`, `DELIVER_DELTA`, `PROCESS_FIELD_EVIDENCE`, `GENERATE_PROOF`) has exactly one `ActionExecution` per stable `action_id`; a second execution record for the same `action_id` is invalid
- **Transport duplicate vs new attempt**: a `VerificationEvent` is uniquely keyed by `submission_id` within a workflow; re-delivery of the same `submission_id` returns the existing event without allocating a new `event_sequence` or new evidence
- **Reconciled mutation classification**: an `ActionExecution` of type `REMEDIATE_ARTIFACT` that is completed by reconciliation MUST produce `MutationEvidence` with `reconciled = true`, never `NoOpEvidence`
- **Proof singularity**: at most one `ChangeProof` per `workflow_id`
- No transition OUT of `PROOF_COMPLETE`, `SUPERSEDED`, or `FAILED`
- `VerificationEvent.verification_result` derived deterministically: `observed == expected → PASS`, `observed != expected and observed != INCONCLUSIVE → FAIL`, else `INCONCLUSIVE`

## Idempotency & Crash Reconciliation Rules

These rules are deterministic Truth Engine logic. They introduce no lifecycle state and select no queueing or distributed-transaction technology.

**Pre-dispatch intent (required for every consequential mutation)**: before a mutation is dispatched, the system persists an `ActionExecution` with `status = PLANNED` carrying the stable `action_id`, the intended artifact, the expected before-state reference and hash, the expected after-state value, and the workflow/change identity. The mutation tool receives the stable action identity (or an equivalent idempotency context) where technically possible.

**Recovery reconciliation for `REMEDIATE_ARTIFACT`** — applied when an action is not recorded `COMPLETED` and the workflow resumes. The same logical mutation MAY be reconciled as completed **only when all four hold**:
1. the action is not recorded complete;
2. the target artifact is already exactly in the intended after-state;
3. the stored pre-action evidence proves this workflow had planned that specific mutation;
4. all authorization and source-version invariants still hold.

When reconciled, the missing completion evidence is reconstructed from stored pre-action intent + the current validated post-state + the action identity / tool receipt where available, and is recorded as `MutationEvidence` with `reconciled = true`.

**Classification rule**: a reconciled mutation is `MUTATION`, never `NO_OP`. `NO_OP` remains reserved for an artifact that was already compliant **before this workflow performed any mutation** — i.e. no `REMEDIATE_ARTIFACT` action for that artifact ever reached `ATTEMPTED`. If reconciliation cannot safely establish what happened, the workflow fails closed to `REVIEW_REQUIRED` rather than fabricating evidence.

**Delivery reconciliation (`DELIVER_DELTA`)**: delivery requires a stable `action_id` and a positive receipt. If a first call succeeded but its response was lost, recovery reconciles using the mechanism's receipt/idempotency key where available. No agent text asserting delivery establishes `DELIVERED`; absent a resolvable receipt the action remains `FAILED_OR_UNCERTAIN` and is retried under its stable identity.

**Field evidence (`PROCESS_FIELD_EVIDENCE`)**: keyed by `submission_id` (above). Transport duplicates are absorbed; genuinely new evidence is a new submission and may represent the corrected attempt after FAIL/INCONCLUSIVE.

**Proof generation (`GENERATE_PROOF`)**: keyed by `workflow_id`; repeated attempts resolve to the single canonical `proof_id`.

## Integrity Hash Semantics

All `*_hash` fields and `EvidenceManifest.content_hashes` are SHA-256 content digests. They establish **content identity and replacement/alteration detection**: a referenced artifact can be shown to be byte-identical to what was recorded at proof time, and silent substitution becomes detectable by comparison.

They do **not** establish a digital signature, a trusted timestamp, identity attestation, proof of authorship, non-repudiation, or ledger/blockchain immutability. No field in this data model may be described with those properties. `ChangeProof.content_hash` is a canonical-JSON digest of the proof document, not an attestation of it.
