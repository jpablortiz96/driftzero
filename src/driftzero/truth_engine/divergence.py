"""T027 — Conflicting / additional divergence comparator (spec § Autonomy Boundaries, cond. 8).

The single authoritative implementation of condition 8. The autonomy gate delegates
here rather than recomputing the comparison with a second algorithm.

Two authoritative structured data sources, exactly as the spec names them:

1. the approved source-version structured requirements for the operation;
2. the current downstream artifact structured requirements.

No LLM assertion may establish the absence of divergence, and nothing here reads
free-form text — this is a structured field-set comparison within one operational
scope, not document reconciliation.

Target handling: the artifact's target value may legitimately be either the known
``previous_value`` (the ordinary mutation path) or already the approved
``current_value`` (the already-compliant case that later resolves as a no-op). Any
third value is conflicting. Being already-current is therefore **not** a divergence,
which is what keeps the no-op race of US3 scenario 2 reachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange


class TargetStatus(StrEnum):
    """State of the one intended atomic target requirement."""

    AT_PREVIOUS_VALUE = "AT_PREVIOUS_VALUE"
    """Artifact holds the known previous value — the ordinary mutation path."""
    ALREADY_CURRENT = "ALREADY_CURRENT"
    """Artifact already holds the approved value — compatible with the no-op path."""
    UNEXPECTED_VALUE = "UNEXPECTED_VALUE"
    """Neither previous nor current — conflicting."""
    MISSING_IN_ARTIFACT = "MISSING_IN_ARTIFACT"
    MISSING_IN_SOURCE = "MISSING_IN_SOURCE"
    DUPLICATE_REPRESENTATION = "DUPLICATE_REPRESENTATION"
    """The artifact represents the target twice with contradictory values."""


class DivergenceReason(StrEnum):
    """Deterministic reason codes. No confidence scores, no generated prose."""

    ADDITIONAL_DIVERGENCE = "ADDITIONAL_DIVERGENCE"
    SCOPE_REQUIREMENT_MISSING = "SCOPE_REQUIREMENT_MISSING"
    TARGET_UNEXPECTED_VALUE = "TARGET_UNEXPECTED_VALUE"
    TARGET_MISSING_IN_ARTIFACT = "TARGET_MISSING_IN_ARTIFACT"
    TARGET_MISSING_IN_SOURCE = "TARGET_MISSING_IN_SOURCE"
    TARGET_DUPLICATE_REPRESENTATION = "TARGET_DUPLICATE_REPRESENTATION"
    SOURCE_TARGET_NOT_APPROVED_VALUE = "SOURCE_TARGET_NOT_APPROVED_VALUE"


@dataclass(frozen=True)
class DivergenceResult:
    """Structured comparator output."""

    has_additional_divergence: bool
    """True when at least one NON-target requirement differs from the approved source."""
    target_status: TargetStatus
    divergent_requirement_ids: tuple[str, ...]
    """Sorted non-target requirement ids that differ or are missing on either side."""
    reason_codes: tuple[DivergenceReason, ...]
    is_conflict_free: bool
    """Condition 8's answer: no additional divergence and no target anomaly."""


def evaluate_divergence(
    artifact: DownstreamArtifact,
    source_requirements: Mapping[str, str],
    change: ApprovedChange,
) -> DivergenceResult:
    """Compare the artifact's structured requirements against the approved source.

    ``source_requirements`` is the approved source-version requirement set for the
    operation under change (authoritative source 1). ``artifact.requirements`` is
    authoritative source 2.

    Conflicting divergence exists when, **outside the one intended atomic target**, at
    least one other requirement in the same scope differs from the approved source. The
    target itself is conflicting only when it is missing, duplicated contradictorily, or
    holds a value that is neither the known previous nor the approved current value.
    """
    target = change.requirement_id
    reasons: list[DivergenceReason] = []

    # --- target requirement ------------------------------------------------------
    artifact_target = artifact.requirements.get(target)
    source_target = source_requirements.get(target)

    if source_target is None:
        target_status = TargetStatus.MISSING_IN_SOURCE
        reasons.append(DivergenceReason.TARGET_MISSING_IN_SOURCE)
    elif source_target != change.current_value:
        # The approved source does not carry the approved value: the comparison basis
        # itself is inconsistent, so no autonomous rewrite may be derived from it.
        target_status = TargetStatus.UNEXPECTED_VALUE
        reasons.append(DivergenceReason.SOURCE_TARGET_NOT_APPROVED_VALUE)
    elif artifact_target is None:
        target_status = TargetStatus.MISSING_IN_ARTIFACT
        reasons.append(DivergenceReason.TARGET_MISSING_IN_ARTIFACT)
    elif artifact.requirement_id == target and artifact.current_value != artifact_target:
        # The artifact states the target requirement twice — as its scalar
        # current_value and inside its structured set — and the two disagree.
        target_status = TargetStatus.DUPLICATE_REPRESENTATION
        reasons.append(DivergenceReason.TARGET_DUPLICATE_REPRESENTATION)
    elif artifact_target == change.previous_value:
        target_status = TargetStatus.AT_PREVIOUS_VALUE
    elif artifact_target == change.current_value:
        target_status = TargetStatus.ALREADY_CURRENT
    else:
        target_status = TargetStatus.UNEXPECTED_VALUE
        reasons.append(DivergenceReason.TARGET_UNEXPECTED_VALUE)

    # --- every other requirement in the same operational scope --------------------
    divergent: set[str] = set()
    missing_in_scope = False
    for requirement_id in set(source_requirements) | set(artifact.requirements):
        if requirement_id == target:
            continue
        source_value = source_requirements.get(requirement_id)
        artifact_value = artifact.requirements.get(requirement_id)
        if source_value is None or artifact_value is None:
            divergent.add(requirement_id)
            missing_in_scope = True
        elif source_value != artifact_value:
            divergent.add(requirement_id)

    has_additional_divergence = bool(divergent)
    if has_additional_divergence:
        reasons.append(DivergenceReason.ADDITIONAL_DIVERGENCE)
    if missing_in_scope:
        reasons.append(DivergenceReason.SCOPE_REQUIREMENT_MISSING)

    target_ok = target_status in (TargetStatus.AT_PREVIOUS_VALUE, TargetStatus.ALREADY_CURRENT)

    return DivergenceResult(
        has_additional_divergence=has_additional_divergence,
        target_status=target_status,
        divergent_requirement_ids=tuple(sorted(divergent)),
        reason_codes=tuple(reasons),
        is_conflict_free=target_ok and not has_additional_divergence,
    )
