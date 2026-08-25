"""T072/T076 — the semantic/deterministic boundary for Crossings 1 and 2.

Every agent or tool result passes through here before anything downstream sees it. The
boundary validates and hands off; it owns no authoritative state of its own.

Explicitly **not** owned by this module:

* workflow state transitions — the state machine owns those
* impact qualification and target cardinality — ``truth_engine.impact`` owns those
* autonomy conditions, divergence, PASS/FAIL, proof generation

A rejected proposal fails closed. Nothing partially validated is forwarded, and the
rejection reference is recorded so the audit trail shows what was refused and why
(FR-011 → ``REVIEW_REQUIRED``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from driftzero.agents.change_intel import ChangeIntelligenceResult, ProposalStatus
from driftzero.models.action import ActionStatus
from driftzero.models.change import ApprovedChange, ChangeSet
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.tools.artifact_mutation import ArtifactRepository, artifact_content_hash
from driftzero.truth_engine.actions import ActionLedger, no_op_admissible
from driftzero.truth_engine.validation import (
    ValidationLayer,
    ValidationOutcome,
    validate_change_set,
    validate_remediation_evidence,
)


@dataclass(frozen=True)
class BoundaryResult:
    """Verdict for one crossing attempt.

    ``accepted_change_set`` is populated only on acceptance. It is the sole value any
    downstream deterministic step may read, and even then impact remains undecided —
    acceptance means "structurally trustworthy input", not "this artifact is affected".
    """

    accepted: bool
    accepted_change_set: ChangeSet | None
    outcome: ValidationOutcome | None
    rejection_reason: str | None = None

    @property
    def failed_layers(self) -> tuple[str, ...]:
        if self.outcome is None:
            return ()
        return tuple(str(layer) for layer in self.outcome.failed_layers)


def accept_change_set(
    result: ChangeIntelligenceResult,
    *,
    change: ApprovedChange,
    known_artifact_ids: frozenset[str],
    source_version_applicable: bool,
    rejection_ref: str,
) -> BoundaryResult:
    """Validate an agent proposal at Crossing 1.

    Two gates, in order. A failure at either one stops the proposal here:

    1. the agent must actually have produced a proposal — a failed call is not an
       empty proposal, and must never be forwarded as one;
    2. the proposal must pass deterministic Crossing 1 validation against the
       authoritative ``ApprovedChange``.

    ``candidate.is_affected`` is never consulted, here or in the validator. Impact is a
    Truth Engine decision, and a proposal that asserts otherwise carries no more weight
    than one that stays silent.
    """
    if not result.succeeded or result.proposal is None:
        return BoundaryResult(
            accepted=False,
            accepted_change_set=None,
            outcome=None,
            rejection_reason=(
                f"no proposal to validate: agent status {result.status}"
                + (f" ({result.failure_reason})" if result.failure_reason else "")
            ),
        )

    outcome = validate_change_set(
        result.proposal,
        change=change,
        known_artifact_ids=known_artifact_ids,
        source_version_applicable=source_version_applicable,
        rejection_ref=rejection_ref,
    )
    if outcome.rejected:
        return BoundaryResult(
            accepted=False,
            accepted_change_set=None,
            outcome=outcome,
            rejection_reason=(
                "Crossing 1 rejected the proposal: "
                + ", ".join(str(layer) for layer in outcome.failed_layers)
            ),
        )

    return BoundaryResult(
        accepted=True, accepted_change_set=result.proposal, outcome=outcome
    )


def boundary_requires_review(result: BoundaryResult) -> bool:
    """True when the workflow must fail closed rather than proceed.

    Kept as a named predicate so call sites express the decision explicitly instead of
    inferring it from a bare boolean, and so no future caller reads a rejection as a
    reason to continue with a partial proposal.
    """
    return not result.accepted


PROPOSAL_FAILURE_STATUSES = frozenset(
    status for status in ProposalStatus if status is not ProposalStatus.PROPOSED
)
"""Every agent status that must not reach the deterministic layer."""


# ============================ T076 — Crossing 2: RemediationEvidence ==================


class RemediationRejection(StrEnum):
    """Precise M1 reason codes, alongside the M0 ``ValidationLayer`` verdict.

    The layers say *which class* of check failed and are the auditable M0 vocabulary;
    these say exactly what went wrong. Both are recorded — neither replaces the other.
    """

    NO_LEDGER_RECORD = "NO_LEDGER_RECORD"
    ACTION_NOT_COMPLETED = "ACTION_NOT_COMPLETED"
    MISSING_PRE_ACTION_INTENT = "MISSING_PRE_ACTION_INTENT"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    REQUIREMENT_MISMATCH = "REQUIREMENT_MISMATCH"
    BEFORE_HASH_MISMATCH = "BEFORE_HASH_MISMATCH"
    AFTER_HASH_MISMATCH = "AFTER_HASH_MISMATCH"
    HASHES_IDENTICAL = "HASHES_IDENTICAL"
    RECONCILED_FLAG_MISMATCH = "RECONCILED_FLAG_MISMATCH"
    NO_OP_NOT_ADMISSIBLE = "NO_OP_NOT_ADMISSIBLE"
    LAYER_VALIDATION_FAILED = "LAYER_VALIDATION_FAILED"


@dataclass(frozen=True)
class RemediationCrossingContext:
    """The narrowest authoritative context Crossing 2 needs.

    Deliberately holds no caller-supplied path, URI, or hash. Every value the crossing
    compares against is derived here from the ledger and the repository — an agent or a
    frontend can supply evidence, never the yardstick it is measured with.
    """

    ledger: ActionLedger
    repository: ArtifactRepository
    change: ApprovedChange
    action_id: str
    expected_artifact_id: str
    expected_requirement_id: str
    source_version_applicable: bool
    rejection_ref: str


@dataclass(frozen=True)
class RemediationBoundaryResult:
    """Verdict for one Crossing 2 attempt.

    ``accepted_evidence`` is populated **only** on acceptance, so a caller holding this
    object cannot mistake raw tool output for validated evidence: the two are reachable
    through different attributes, and the raw value is never copied across on rejection.
    """

    accepted: bool
    accepted_evidence: MutationEvidence | NoOpEvidence | None
    outcome: ValidationOutcome | None
    rejections: tuple[RemediationRejection, ...] = ()
    rejection_reason: str | None = None
    authoritative_before_hash: str | None = None
    authoritative_after_hash: str | None = None

    @property
    def failed_layers(self) -> tuple[str, ...]:
        if self.outcome is None:
            return ()
        return tuple(str(layer) for layer in self.outcome.failed_layers)

    @property
    def requires_review(self) -> bool:
        """A rejected crossing fails closed; the workflow's path leads to REVIEW_REQUIRED."""
        return not self.accepted

    def evidence_ref(self) -> str | None:
        """Deterministic reference for ``EvidenceManifest.rejected_result_refs``.

        Returns a reference string only. Nothing here persists it — no store exists yet,
        and pretending otherwise would fabricate durability.
        """
        if self.accepted:
            return None
        return (
            f"crossing2-rejected:{self.rejection_ref_value}:"
            f"{','.join(str(r) for r in self.rejections)}"
        )

    rejection_ref_value: str = ""


def _reject(
    reasons: list[RemediationRejection],
    *,
    outcome: ValidationOutcome | None,
    rejection_ref: str,
    detail: str,
    before_hash: str | None = None,
    after_hash: str | None = None,
) -> RemediationBoundaryResult:
    return RemediationBoundaryResult(
        accepted=False,
        accepted_evidence=None,
        outcome=outcome,
        rejections=tuple(dict.fromkeys(reasons)),
        rejection_reason=detail,
        authoritative_before_hash=before_hash,
        authoritative_after_hash=after_hash,
        rejection_ref_value=rejection_ref,
    )


def accept_remediation_evidence(
    evidence: MutationEvidence | NoOpEvidence,
    *,
    context: RemediationCrossingContext,
) -> RemediationBoundaryResult:
    """Validate a remediation result at Crossing 2, or fail closed.

    Order matters. The authoritative yardsticks are established first — the pre-action
    intent recorded *before* dispatch, and the artifact as actually committed — then the
    frozen M0 validator runs against them, then the MUTATION/NO_OP-specific adjudication
    the M0 layer does not perform.

    This crossing validates evidence. It does not execute, authorize, or re-authorize
    remediation (T075 owns authorization), does not mutate anything, and owns no workflow
    transition. Accepted evidence is *eligible* to enter authoritative state; entering it
    is someone else's decision.
    """
    reasons: list[RemediationRejection] = []
    ledger, repository = context.ledger, context.repository

    record = ledger.get(context.action_id)
    if record is None:
        return _reject(
            [RemediationRejection.NO_LEDGER_RECORD],
            outcome=None,
            rejection_ref=context.rejection_ref,
            detail=f"no ActionExecution recorded for {context.action_id!r}",
        )

    intent = record.intent or {}
    authoritative_before_hash = str(intent.get("expected_before_hash") or "")
    if not authoritative_before_hash:
        reasons.append(RemediationRejection.MISSING_PRE_ACTION_INTENT)

    try:
        artifact = repository.read(context.expected_artifact_id)
    except Exception as exc:  # noqa: BLE001 - a read failure is a rejection, not a crash
        return _reject(
            [RemediationRejection.ARTIFACT_NOT_FOUND],
            outcome=None,
            rejection_ref=context.rejection_ref,
            detail=f"artifact read failed: {type(exc).__name__}: {exc}",
        )
    if artifact is None:
        return _reject(
            [RemediationRejection.ARTIFACT_NOT_FOUND],
            outcome=None,
            rejection_ref=context.rejection_ref,
            detail=context.expected_artifact_id,
        )
    authoritative_after_hash = artifact_content_hash(artifact)

    # --- frozen M0 layer validation, measured against the authoritative before-hash ---
    outcome = validate_remediation_evidence(
        evidence,
        change=context.change,
        expected_artifact_id=context.expected_artifact_id,
        expected_before_hash=authoritative_before_hash,
        expected_action_id=context.action_id,
        source_version_applicable=context.source_version_applicable,
        rejection_ref=context.rejection_ref,
    )
    if outcome.rejected:
        reasons.append(RemediationRejection.LAYER_VALIDATION_FAILED)
        if ValidationLayer.BEFORE_STATE_CONSISTENCY in outcome.failed_layers:
            reasons.append(RemediationRejection.BEFORE_HASH_MISMATCH)
        if ValidationLayer.EXPECTED_ARTIFACT_IDENTITY in outcome.failed_layers:
            reasons.append(RemediationRejection.ARTIFACT_MISMATCH)

    # --- discriminated adjudication the M0 layer does not perform ---------------------
    if isinstance(evidence, MutationEvidence):
        if record.status is not ActionStatus.COMPLETED:
            reasons.append(RemediationRejection.ACTION_NOT_COMPLETED)
        # The agent's after_hash never becomes authoritative: it must equal the hash of
        # the artifact as actually committed.
        if evidence.after_hash != authoritative_after_hash:
            reasons.append(RemediationRejection.AFTER_HASH_MISMATCH)
        if evidence.before_hash == evidence.after_hash:
            reasons.append(RemediationRejection.HASHES_IDENTICAL)
        # Requirement identity is bound to observable state rather than to a claim:
        # the committed artifact must carry the approved value under the expected key.
        if artifact.requirements.get(context.expected_requirement_id) != evidence.after_value:
            reasons.append(RemediationRejection.REQUIREMENT_MISMATCH)
        # reconciled=True is admissible only for an actually reconciled action, and a
        # reconciled mutation stays MUTATION — it is never re-labelled NO_OP.
        if evidence.reconciled != record.reconciled:
            reasons.append(RemediationRejection.RECONCILED_FLAG_MISMATCH)
    else:
        # NO_OP is admissible only while this workflow never dispatched a mutation for
        # this action. Matching the after-value is necessary but never sufficient.
        if not no_op_admissible(ledger, context.action_id):
            reasons.append(RemediationRejection.NO_OP_NOT_ADMISSIBLE)
        if evidence.evaluated_artifact_hash != authoritative_after_hash:
            reasons.append(RemediationRejection.AFTER_HASH_MISMATCH)
        if artifact.requirements.get(context.expected_requirement_id) != evidence.observed_value:
            reasons.append(RemediationRejection.REQUIREMENT_MISMATCH)

    if reasons:
        return _reject(
            reasons,
            outcome=outcome,
            rejection_ref=context.rejection_ref,
            detail="Crossing 2 rejected the evidence: "
            + ", ".join(str(r) for r in dict.fromkeys(reasons)),
            before_hash=authoritative_before_hash,
            after_hash=authoritative_after_hash,
        )

    return RemediationBoundaryResult(
        accepted=True,
        accepted_evidence=evidence,
        outcome=outcome,
        authoritative_before_hash=authoritative_before_hash,
        authoritative_after_hash=authoritative_after_hash,
        rejection_ref_value=context.rejection_ref,
    )
