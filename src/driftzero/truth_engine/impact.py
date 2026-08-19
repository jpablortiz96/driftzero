"""T025/T026 — Deterministic affected-artifact qualification and cardinality (FR-002, SC-002).

The Change Intelligence Agent may *propose* candidates. This module decides, and its
decision is the only one that counts: ``AffectedArtifactCandidate.is_affected`` is
recorded for audit and then ignored. A candidate qualifies only when all four FR-002
conditions hold against the structured records, never because an agent said so.

Impact-time semantics: qualification is evaluated against the artifact state supplied
by the caller **at impact-determination time**. An artifact that validly qualified here
and later becomes compliant before remediation runs is the no-op race of US3 scenario 2
— it is not retroactively unqualified, and this module never re-reads artifact state.

Scope: qualification and cardinality only. No state transition is performed and no
workflow is mutated; the resolution is returned for later orchestration to apply.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import AffectedArtifactCandidate, ApprovedChange


class QualificationCondition(StrEnum):
    """The four FR-002 conditions. All must hold for a candidate to qualify."""

    OPERATION_MATCH = "operation_match"
    INSTRUCTION_CORRESPONDENCE = "instruction_correspondence"
    VALUE_CONFLICT = "value_conflict"
    AUTHORIZED_SCOPE = "in_authorized_scope"


@dataclass(frozen=True)
class CandidateQualification:
    """Deterministic verdict for one candidate, with per-condition detail."""

    artifact_id: str
    qualified: bool
    conditions: Mapping[QualificationCondition, bool]
    failed_conditions: tuple[QualificationCondition, ...]
    agent_proposed_is_affected: bool
    """Recorded verbatim for audit. Never consulted when deciding ``qualified``."""
    agent_proposal_disagreed: bool
    """True when the agent's proposal differs from the deterministic verdict."""


class ImpactOutcome(StrEnum):
    """Cardinality outcome after deterministic qualification."""

    NO_QUALIFIED_TARGET = "NO_QUALIFIED_TARGET"
    SINGLE_QUALIFIED_TARGET = "SINGLE_QUALIFIED_TARGET"
    MULTIPLE_QUALIFIED_TARGETS = "MULTIPLE_QUALIFIED_TARGETS"


@dataclass(frozen=True)
class ImpactResolution:
    """Deterministic cardinality decision. Applied by orchestration, not here."""

    outcome: ImpactOutcome
    affected_artifact_id: str | None
    """Set only for SINGLE_QUALIFIED_TARGET. Never fabricated for the other outcomes."""
    qualified_artifact_ids: tuple[str, ...]
    candidate_artifact_refs: tuple[str, ...]
    """Every evaluated candidate, retained as evidence in all three outcomes."""
    qualifications: tuple[CandidateQualification, ...]
    impact_reason: str
    """Deterministic templated explanation. Never model-generated."""
    requires_review: bool = field(default=False)
    """True for the zero-qualified and multi-qualified outcomes (→ REVIEW_REQUIRED)."""


def qualify_candidate(
    candidate: AffectedArtifactCandidate,
    artifact: DownstreamArtifact,
    change: ApprovedChange,
) -> CandidateQualification:
    """T025 — evaluate one candidate against the four FR-002 conditions.

    Conditions, all evaluated from structured records:

    1. **operation match** — the artifact implements the operation the change targets.
    2. **instruction correspondence** — the artifact implements the specific requirement
       the change targets. Together with (1) this is what stops an unrelated artifact
       that merely contains the token ``LEFT`` from qualifying (SC-002).
    3. **value conflict** — the artifact does not already represent the approved
       ``current_value``, i.e. it is stale at impact time and there is something to fix.
    4. **authorized scope** — the artifact is listed in the change's ``authorized_scope``
       **and** flagged ``authorized_for_remediation``. Both are required: an artifact
       authorized by only one of the two records is treated as unauthorized (fail-closed).
    """
    results: dict[QualificationCondition, bool] = {
        QualificationCondition.OPERATION_MATCH: artifact.operation_id == change.operation_id,
        QualificationCondition.INSTRUCTION_CORRESPONDENCE: (
            artifact.requirement_id == change.requirement_id
        ),
        QualificationCondition.VALUE_CONFLICT: artifact.current_value != change.current_value,
        QualificationCondition.AUTHORIZED_SCOPE: (
            artifact.authorized_for_remediation and artifact.artifact_id in change.authorized_scope
        ),
    }
    failed = tuple(condition for condition, ok in results.items() if not ok)
    qualified = not failed
    return CandidateQualification(
        artifact_id=artifact.artifact_id,
        qualified=qualified,
        conditions=MappingProxyType(dict(results)),
        failed_conditions=failed,
        agent_proposed_is_affected=candidate.is_affected,
        agent_proposal_disagreed=candidate.is_affected != qualified,
    )


def qualify_candidates(
    pairs: Iterable[tuple[AffectedArtifactCandidate, DownstreamArtifact]],
    change: ApprovedChange,
) -> tuple[CandidateQualification, ...]:
    """Qualify every candidate, preserving input order."""
    return tuple(qualify_candidate(candidate, artifact, change) for candidate, artifact in pairs)


def resolve_cardinality(
    qualifications: Iterable[CandidateQualification],
) -> ImpactResolution:
    """T026 — resolve 0 / 1 / >1 qualified artifacts into a deterministic decision.

    - **zero qualified** → ``requires_review`` (no unique S1 target exists);
      ``affected_artifact_id`` stays ``None`` and is never fabricated.
    - **exactly one** → the single autonomous S1 path; the identity is returned.
    - **more than one** → ``requires_review`` with the complete qualified set retained.
      No candidate is selected: not the first, not by sort order, not by any heuristic.

    Every evaluated candidate is retained in ``candidate_artifact_refs`` in all three
    outcomes so the decision is explainable from evidence alone.
    """
    evaluated = tuple(qualifications)
    all_refs = tuple(q.artifact_id for q in evaluated)
    qualified_ids = tuple(q.artifact_id for q in evaluated if q.qualified)
    count = len(qualified_ids)

    if count == 1:
        return ImpactResolution(
            outcome=ImpactOutcome.SINGLE_QUALIFIED_TARGET,
            affected_artifact_id=qualified_ids[0],
            qualified_artifact_ids=qualified_ids,
            candidate_artifact_refs=all_refs,
            qualifications=evaluated,
            impact_reason=(
                f"exactly one candidate qualified against all four FR-002 conditions: "
                f"{qualified_ids[0]}"
            ),
            requires_review=False,
        )

    if count == 0:
        reason = (
            f"zero of {len(evaluated)} evaluated candidate(s) qualified against all four "
            "FR-002 conditions; no unique S1 target exists"
        )
        outcome = ImpactOutcome.NO_QUALIFIED_TARGET
    else:
        reason = (
            f"{count} of {len(evaluated)} evaluated candidate(s) independently qualified "
            f"({', '.join(qualified_ids)}); S1 remains a single-artifact workflow and no "
            "candidate may be selected automatically"
        )
        outcome = ImpactOutcome.MULTIPLE_QUALIFIED_TARGETS

    return ImpactResolution(
        outcome=outcome,
        affected_artifact_id=None,
        qualified_artifact_ids=qualified_ids,
        candidate_artifact_refs=all_refs,
        qualifications=evaluated,
        impact_reason=reason,
        requires_review=True,
    )
