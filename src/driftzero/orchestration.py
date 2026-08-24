"""T072 — the semantic/deterministic boundary for Crossing 1.

Every agent proposal passes through here before anything downstream sees it. The
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

from driftzero.agents.change_intel import ChangeIntelligenceResult, ProposalStatus
from driftzero.models.change import ApprovedChange, ChangeSet
from driftzero.truth_engine.validation import ValidationOutcome, validate_change_set


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
