"""T028 — The nine-condition autonomous remediation gate (FR-003).

Answers exactly one question: **may this remediation be autonomously authorized?**

It does not write the artifact, does not construct ``MutationEvidence``, does not call
a mutation tool, and does not transition the workflow. Authorization and execution are
separate concerns; execution belongs to later tasks.

Decision rule: an explicit AND over all nine conditions. There is no threshold, no
"8 of 9", no majority, no confidence score, no operator override, and no LLM override.
If any condition fails, autonomous remediation is denied and the workflow's path leads
to ``REVIEW_REQUIRED``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.truth_engine.divergence import (
    DivergenceResult,
    TargetStatus,
    evaluate_divergence,
)


class AutonomyCondition(StrEnum):
    """The nine boundary conditions, in specification order."""

    C1_SOURCE_VERSION_KNOWN = "condition_1_source_version_known"
    C2_REQUIREMENT_KNOWN = "condition_2_requirement_known"
    C3_PREVIOUS_VALUE_KNOWN = "condition_3_previous_value_known"
    C4_NEW_VALUE_KNOWN = "condition_4_new_approved_value_known"
    C5_SINGLE_ATOMIC_CHANGE = "condition_5_single_atomic_change"
    C6_TARGET_AUTHORIZED = "condition_6_target_artifact_authorized"
    C7_TARGET_UNIQUELY_IDENTIFIABLE = "condition_7_target_instruction_uniquely_identifiable"
    C8_NO_CONFLICTING_DIVERGENCE = "condition_8_no_conflicting_or_additional_divergence"
    C9_EVIDENCE_PRESERVABLE = "condition_9_before_after_evidence_preservable"


@dataclass(frozen=True)
class AutonomyDecision:
    """Deterministic authorization verdict with per-condition detail."""

    authorized: bool
    conditions: Mapping[AutonomyCondition, bool]
    failed_conditions: tuple[AutonomyCondition, ...]
    divergence: DivergenceResult
    """The T027 result backing conditions 7 and 8. One authoritative comparison."""
    requires_review: bool
    """Always the inverse of ``authorized``: a denied gate leads to REVIEW_REQUIRED."""


def evaluate_autonomy(
    artifact: DownstreamArtifact,
    source_requirements: Mapping[str, str],
    change: ApprovedChange,
) -> AutonomyDecision:
    """Evaluate all nine conditions and AND them together.

    Every condition is evaluated independently and recorded, so a caller (or a test)
    can tell exactly which one failed rather than only that the gate denied.

    Conditions 7 and 8 both read the single :func:`evaluate_divergence` result:
    condition 7 asks whether the target instruction resolves to exactly one
    non-contradictory representation, condition 8 asks whether the artifact is free of
    conflicting or additional divergence. There is no second comparison algorithm.
    """
    divergence = evaluate_divergence(artifact, source_requirements, change)

    target_resolvable = divergence.target_status not in (
        TargetStatus.MISSING_IN_ARTIFACT,
        TargetStatus.MISSING_IN_SOURCE,
        TargetStatus.DUPLICATE_REPRESENTATION,
    )

    results: dict[AutonomyCondition, bool] = {
        AutonomyCondition.C1_SOURCE_VERSION_KNOWN: bool(change.source_version),
        AutonomyCondition.C2_REQUIREMENT_KNOWN: bool(change.requirement_id),
        AutonomyCondition.C3_PREVIOUS_VALUE_KNOWN: bool(change.previous_value),
        AutonomyCondition.C4_NEW_VALUE_KNOWN: bool(change.current_value),
        # Exactly one atomic requirement change: the change names a single requirement
        # and actually moves it to a different value.
        AutonomyCondition.C5_SINGLE_ATOMIC_CHANGE: (
            bool(change.requirement_id) and change.previous_value != change.current_value
        ),
        # Both authorization records must agree (fail-closed).
        AutonomyCondition.C6_TARGET_AUTHORIZED: (
            artifact.authorized_for_remediation
            and artifact.artifact_id in change.authorized_scope
        ),
        AutonomyCondition.C7_TARGET_UNIQUELY_IDENTIFIABLE: target_resolvable,
        AutonomyCondition.C8_NO_CONFLICTING_DIVERGENCE: divergence.is_conflict_free,
        # A resolvable content reference must exist so the before-state can be captured.
        # The capture itself happens later; this only asserts it is possible.
        AutonomyCondition.C9_EVIDENCE_PRESERVABLE: bool(artifact.content_ref),
    }

    failed = tuple(condition for condition, ok in results.items() if not ok)
    authorized = all(results.values())

    return AutonomyDecision(
        authorized=authorized,
        conditions=MappingProxyType(dict(results)),
        failed_conditions=failed,
        divergence=divergence,
        requires_review=not authorized,
    )
