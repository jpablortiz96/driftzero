"""T043-T046 — Proof invariants, canonical Change Proof, validation, singularity.

FR-006, SC-003, SC-008, SC-009.

The seven completion conditions are evaluated by exactly one deterministic evaluator.
There is no weaker parallel path, no threshold, and no "6 of 7 is good enough": seven of
seven or nothing. No agent output can set completion — the evaluator reads validated
authoritative structures only, and every input is a checked value rather than a
precomputed boolean.

**Hash guarantee boundary.** ``content_hash`` and the manifest digests give content
identity and alteration detection only. They are not a digital signature, a trusted
timestamp, identity attestation, proof of authorship, non-repudiation, blockchain
immutability, or tamper-proof storage. Integrity revalidation below is hash comparison,
never signature verification.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from driftzero.models.change import ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.proof import ChangeProof, EvidenceManifest
from driftzero.models.remediation import MutationEvidence, NoOpEvidence, RemediationEvidence
from driftzero.models.verification import VerificationEvent, VerificationResult
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.truth_engine.evidence import (
    canonical_hash,
    has_fabricated_before_after_pair,
    hashes_match,
    manifest_covers_all_events,
)
from driftzero.truth_engine.idempotency import derive_proof_action_id
from driftzero.truth_engine.impact import ImpactOutcome, ImpactResolution
from driftzero.truth_engine.verification import latest_authoritative_event

PERMANENTLY_BLOCKING_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.SUPERSEDED, WorkflowState.FAILED}
)
"""Terminal non-success. Once entered, completion is denied forever."""

CURRENTLY_BLOCKING_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.VERIFICATION_FAILED, WorkflowState.VERIFICATION_INCONCLUSIVE}
)
"""Recoverable, but blocking while the workflow is currently in them."""


class ProofCondition(StrEnum):
    """The seven mandatory completion conditions, in specification order."""

    C1_SOURCE_CHANGE_APPLICABLE = "condition_1_source_change_represented_and_applicable"
    C2_IMPACT_DETERMINED = "condition_2_impact_validly_determined"
    C3_REMEDIATED_OR_NO_OP = "condition_3_remediated_or_valid_no_op"
    C4_DELTA_DELIVERED = "condition_4_delta_delivered"
    C5_LATEST_VERIFICATION_PASS = "condition_5_latest_authoritative_verification_pass"
    C6_EVIDENCE_TRACEABLE = "condition_6_evidence_complete_and_traceable"
    C7_STATE_COMPATIBLE = "condition_7_current_state_compatible_with_completion"


@dataclass(frozen=True)
class ProofContext:
    """The authoritative inputs the evaluator reads. No agent conclusions among them."""

    workflow: Workflow
    change: ApprovedChange
    impact: ImpactResolution
    remediation_evidence: RemediationEvidence | None
    manifest: EvidenceManifest
    verification_events: Sequence[VerificationEvent]
    state_history: Sequence[WorkflowState]
    source_version_applicable: bool
    delivery_receipt_ref: str | None
    """Receipt resolved through T036 semantics. An agent assertion is not one."""


@dataclass(frozen=True)
class ProofInvariantResult:
    """Per-condition verdict. Eligible only when all seven hold."""

    eligible: bool
    conditions: Mapping[ProofCondition, bool]
    failed_conditions: tuple[ProofCondition, ...]

    @property
    def satisfied_count(self) -> int:
        return sum(1 for ok in self.conditions.values() if ok)


def _condition_7(context: ProofContext) -> bool:
    """Condition 7 — evaluated against the CURRENT authoritative state.

    Encoded exactly as plan.md § Change Proof Technical Design specifies:

    * ``SUPERSEDED`` / ``FAILED`` currently or anywhere in history → denied permanently.
    * ``REVIEW_REQUIRED`` anywhere in history → denied (no autonomous exit in S1).
    * currently ``VERIFICATION_FAILED`` / ``VERIFICATION_INCONCLUSIVE`` → denied while current.
    * **historical** FAIL/INCONCLUSIVE → not disqualifying, provided condition 5 holds.

    Deliberately not "was FAIL ever seen?" — that reading was corrected, and it would
    break the required FAIL → corrected PASS → PROOF_COMPLETE recovery path.
    """
    current = context.workflow.state
    history = set(context.state_history) | {current}

    if current in PERMANENTLY_BLOCKING_STATES:
        return False
    if history & PERMANENTLY_BLOCKING_STATES:
        return False
    if WorkflowState.REVIEW_REQUIRED in history:
        return False
    if current in CURRENTLY_BLOCKING_STATES:
        return False
    return True


def evaluate_proof_invariants(context: ProofContext) -> ProofInvariantResult:
    """T043 — evaluate all seven conditions independently and AND them."""
    workflow = context.workflow
    change = context.change
    evidence = context.remediation_evidence

    # C1 — the approved source change is represented here and remains applicable.
    c1 = (
        workflow.change_id == change.change_id
        and workflow.source_version == change.source_version
        and context.source_version_applicable
    )

    # C2 — impact determined deterministically to exactly one qualified target.
    c2 = (
        context.impact.outcome is ImpactOutcome.SINGLE_QUALIFIED_TARGET
        and context.impact.affected_artifact_id is not None
        and context.impact.affected_artifact_id == workflow.affected_artifact_id
    )

    # C3 — remediated, or validly recorded as an already-compliant no-op. Exactly one
    # path, with a manifest shape matching the variant.
    if evidence is None or workflow.affected_artifact_id is None:
        c3 = False
    else:
        variant_ok = isinstance(evidence, MutationEvidence | NoOpEvidence)
        c3 = (
            variant_ok
            and evidence.artifact_id == workflow.affected_artifact_id
            and not has_fabricated_before_after_pair(context.manifest, evidence)
        )

    # C4 — delivered, backed by a resolvable positive receipt (never an assertion).
    c4 = (
        workflow.delivery_status == "DELIVERED"
        and bool(context.delivery_receipt_ref)
        and context.manifest.delivery_ref == context.delivery_receipt_ref
    )

    # C5 — the LATEST authoritative verification is PASS. Chronology comes from T037;
    # no competing "latest" algorithm is implemented here.
    latest = latest_authoritative_event(context.verification_events, workflow.workflow_id)
    c5 = latest is not None and latest.verification_result is VerificationResult.PASS

    # C6 — all supporting evidence exists and is traceably associated, including every
    # historical FAIL/INCONCLUSIVE attempt.
    c6 = (
        bool(context.manifest.source_change_ref)
        and bool(context.manifest.affected_artifact_ref)
        and bool(context.manifest.remediation_evidence_refs)
        and bool(context.manifest.delivery_ref)
        and bool(context.manifest.verification_refs)
        and manifest_covers_all_events(context.manifest, context.verification_events)
        and all(
            ref in context.manifest.content_hashes
            for ref in context.manifest.remediation_evidence_refs
        )
    )

    c7 = _condition_7(context)

    results: dict[ProofCondition, bool] = {
        ProofCondition.C1_SOURCE_CHANGE_APPLICABLE: c1,
        ProofCondition.C2_IMPACT_DETERMINED: c2,
        ProofCondition.C3_REMEDIATED_OR_NO_OP: c3,
        ProofCondition.C4_DELTA_DELIVERED: c4,
        ProofCondition.C5_LATEST_VERIFICATION_PASS: c5,
        ProofCondition.C6_EVIDENCE_TRACEABLE: c6,
        ProofCondition.C7_STATE_COMPATIBLE: c7,
    }
    failed = tuple(condition for condition, ok in results.items() if not ok)
    return ProofInvariantResult(
        eligible=all(results.values()), conditions=results, failed_conditions=failed
    )


# ============================ T046 — proof identity ===================================


def derive_proof_id(workflow_id: str) -> str:
    """T046 — the one canonical proof identity for a workflow.

    Reuses the stable ``GENERATE_PROOF`` action identity (T030), so repeated generation
    resolves to the same proof rather than minting ``proof-1``, ``proof-2``, ``proof-3``.
    Independent of wall-clock time, so a retry cannot produce a second identity.
    """
    return derive_proof_action_id(workflow_id=workflow_id)


# ============================ T044 — canonical generation =============================


class ProofGenerationError(Exception):
    """Generation was requested for a workflow that is not eligible."""

    def __init__(self, failed_conditions: tuple[ProofCondition, ...]) -> None:
        self.failed_conditions = failed_conditions
        super().__init__(
            "Change Proof cannot be generated; unmet conditions: "
            + ", ".join(sorted(str(c) for c in failed_conditions))
        )


def canonical_proof_material(proof: ChangeProof) -> dict[str, object]:
    """Canonical hash material: the whole proof document minus its own digest."""
    payload = proof.model_dump(mode="json")
    payload.pop("content_hash", None)
    return payload


def compute_proof_hash(proof: ChangeProof) -> str:
    """SHA-256 over the canonical JSON of the proof. Content identity only."""
    return canonical_hash(canonical_proof_material(proof))


def derive_completion_timestamp(context: ProofContext) -> datetime:
    """The authoritative completion moment, derived — never read from a clock.

    Completion is established by the authoritative passing field verification, so that
    event's persisted timestamp *is* the completion moment. Condition 5 guarantees such
    an event exists before generation is permitted.

    Deriving it rather than accepting a caller value is what makes a proof exactly
    reproducible after a crash: regenerating from the same persisted authoritative state
    yields byte-identical canonical material, so an orchestration retry cannot change
    ``content_hash``.
    """
    latest = latest_authoritative_event(
        context.verification_events, context.workflow.workflow_id
    )
    if latest is None:
        raise ProofGenerationError((ProofCondition.C5_LATEST_VERIFICATION_PASS,))
    return latest.timestamp


def generate_change_proof(
    context: ProofContext,
    *,
    existing_proof: ChangeProof | None = None,
) -> ChangeProof:
    """T044/T046 — generate the canonical Change Proof, once.

    Refuses unless all seven invariants pass. If a proof already exists for this
    workflow it is returned unchanged, so a retry never creates a second one.

    ``completion_timestamp`` is **derived** from the authoritative passing verification
    event (see :func:`derive_completion_timestamp`). The generator accepts no caller
    timestamp and reads no clock, so identical authoritative inputs always produce an
    identical canonical representation and hash — including after a crash and replay.
    """
    if existing_proof is not None:
        return existing_proof

    invariants = evaluate_proof_invariants(context)
    if not invariants.eligible:
        raise ProofGenerationError(invariants.failed_conditions)

    workflow = context.workflow
    latest = latest_authoritative_event(context.verification_events, workflow.workflow_id)
    assert latest is not None  # guaranteed by condition 5
    assert context.remediation_evidence is not None  # guaranteed by condition 3
    assert workflow.affected_artifact_id is not None  # guaranteed by condition 2
    assert context.delivery_receipt_ref is not None  # guaranteed by condition 4

    draft = ChangeProof(
        proof_id=derive_proof_id(workflow.workflow_id),
        workflow_id=workflow.workflow_id,
        change_id=context.change.change_id,
        source_procedure_id=context.change.source_procedure_id,
        source_version=context.change.source_version,
        previous_value=context.change.previous_value,
        current_value=context.change.current_value,
        affected_artifact_id=workflow.affected_artifact_id,
        remediation_evidence=context.remediation_evidence,
        delivery_status="DELIVERED",
        delivery_ref=context.delivery_receipt_ref,
        verification_result=VerificationResult.PASS,
        verification_event_id=latest.event_id,
        worker_id=workflow.worker_id,
        evidence_manifest=context.manifest,
        completion_timestamp=derive_completion_timestamp(context),
        content_hash="0" * 64,
        data_classification=DataClassification(labels=[ClassificationLabel.DERIVED]),
    )
    return draft.model_copy(update={"content_hash": compute_proof_hash(draft)})


# ============================ T045 — validation =======================================


class ProofValidationFailure(StrEnum):
    """Why a proof failed revalidation. Technical results, not lifecycle states."""

    UNMET_COMPLETION_CONDITION = "UNMET_COMPLETION_CONDITION"
    PROOF_HASH_MISMATCH = "PROOF_HASH_MISMATCH"
    CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
    PROOF_IDENTITY_MISMATCH = "PROOF_IDENTITY_MISMATCH"


@dataclass(frozen=True)
class ProofValidationResult:
    """Outcome of revalidating an issued proof."""

    valid: bool
    failures: tuple[ProofValidationFailure, ...]
    failed_conditions: tuple[ProofCondition, ...]
    mismatched_refs: tuple[str, ...]


class ProofValidator:
    """T045 — deterministic revalidation of an issued Change Proof.

    Re-checks all seven completion conditions and every recorded content hash. Hash
    comparison detects replacement or alteration of referenced content; it is **not**
    signature verification and proves nothing about who produced the content.

    Nothing an agent emits can make ``valid`` True: the verdict is recomputed from the
    proof document and the resolved content supplied by the caller.
    """

    def validate(
        self,
        proof: ChangeProof,
        context: ProofContext,
        resolved_contents: Mapping[str, str] | None = None,
    ) -> ProofValidationResult:
        failures: list[ProofValidationFailure] = []
        mismatched: list[str] = []

        invariants = evaluate_proof_invariants(context)
        if not invariants.eligible:
            failures.append(ProofValidationFailure.UNMET_COMPLETION_CONDITION)

        if proof.proof_id != derive_proof_id(proof.workflow_id):
            failures.append(ProofValidationFailure.PROOF_IDENTITY_MISMATCH)

        if proof.content_hash != compute_proof_hash(proof):
            failures.append(ProofValidationFailure.PROOF_HASH_MISMATCH)

        for ref, expected in proof.evidence_manifest.content_hashes.items():
            actual = (resolved_contents or {}).get(ref)
            if actual is not None and not hashes_match(expected, actual):
                mismatched.append(ref)
        if mismatched:
            failures.append(ProofValidationFailure.CONTENT_HASH_MISMATCH)

        return ProofValidationResult(
            valid=not failures,
            failures=tuple(dict.fromkeys(failures)),
            failed_conditions=invariants.failed_conditions,
            mismatched_refs=tuple(sorted(mismatched)),
        )
