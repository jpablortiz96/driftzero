"""T049 — Impact qualification and cardinality acceptance (SC-002, FR-002)."""

from __future__ import annotations

import pytest

from driftzero.truth_engine.impact import (
    ImpactOutcome,
    QualificationCondition,
    qualify_candidate,
    qualify_candidates,
    resolve_cardinality,
)

from ._acceptance import (
    ARTIFACT,
    UNRELATED,
    make_artifact,
    make_candidate,
    make_change,
    make_unrelated_artifact,
)


def test_authorized_stale_artifact_qualifies() -> None:
    q = qualify_candidate(make_candidate(), make_artifact(), make_change())
    assert q.qualified is True
    assert q.failed_conditions == ()


def test_unrelated_lexical_left_artifact_does_not_qualify() -> None:
    """SC-002: the forklift instruction also says LEFT and must stay untouched."""
    q = qualify_candidate(make_candidate(UNRELATED), make_unrelated_artifact(), make_change())
    assert q.qualified is False
    assert QualificationCondition.OPERATION_MATCH in q.failed_conditions
    assert QualificationCondition.INSTRUCTION_CORRESPONDENCE in q.failed_conditions
    assert QualificationCondition.AUTHORIZED_SCOPE in q.failed_conditions


def test_agent_true_proposal_cannot_override_failed_qualification() -> None:
    q = qualify_candidate(
        make_candidate(UNRELATED, is_affected=True), make_unrelated_artifact(), make_change()
    )
    assert q.qualified is False
    assert q.agent_proposed_is_affected is True
    assert q.agent_proposal_disagreed is True


def test_agent_false_proposal_cannot_veto_a_qualified_artifact() -> None:
    q = qualify_candidate(make_candidate(is_affected=False), make_artifact(), make_change())
    assert q.qualified is True
    assert q.agent_proposed_is_affected is False
    assert q.agent_proposal_disagreed is True


@pytest.mark.parametrize(
    ("override", "condition"),
    [
        ({"operation_id": "forklift_navigation"}, QualificationCondition.OPERATION_MATCH),
        (
            {"requirement_id": "turn_direction"},
            QualificationCondition.INSTRUCTION_CORRESPONDENCE,
        ),
        ({"current_value": "TOP_RIGHT"}, QualificationCondition.VALUE_CONFLICT),
        ({"authorized_for_remediation": False}, QualificationCondition.AUTHORIZED_SCOPE),
        ({"artifact_id": "wi-out-of-scope"}, QualificationCondition.AUTHORIZED_SCOPE),
    ],
)
def test_each_fr002_condition_independently_blocks(
    override: dict[str, object], condition: QualificationCondition
) -> None:
    q = qualify_candidate(make_candidate(), make_artifact(**override), make_change())
    assert q.qualified is False
    assert condition in q.failed_conditions


# ---------------------------------------------------------------- cardinality


def test_zero_qualified_requires_review_and_fabricates_nothing() -> None:
    resolution = resolve_cardinality(
        qualify_candidates([(make_candidate(UNRELATED), make_unrelated_artifact())], make_change())
    )
    assert resolution.outcome is ImpactOutcome.NO_QUALIFIED_TARGET
    assert resolution.requires_review is True
    assert resolution.affected_artifact_id is None
    assert resolution.candidate_artifact_refs == (UNRELATED,)


def test_exactly_one_qualified_proceeds() -> None:
    resolution = resolve_cardinality(
        qualify_candidates([(make_candidate(), make_artifact())], make_change())
    )
    assert resolution.outcome is ImpactOutcome.SINGLE_QUALIFIED_TARGET
    assert resolution.requires_review is False
    assert resolution.affected_artifact_id == ARTIFACT


def test_many_qualified_requires_review_without_arbitrary_selection() -> None:
    change = make_change(authorized_scope=["wi-c", "wi-a", "wi-b"])
    pairs = [(make_candidate(i), make_artifact(artifact_id=i)) for i in ("wi-c", "wi-a", "wi-b")]
    resolution = resolve_cardinality(qualify_candidates(pairs, change))
    assert resolution.outcome is ImpactOutcome.MULTIPLE_QUALIFIED_TARGETS
    assert resolution.requires_review is True
    assert resolution.affected_artifact_id is None, "no first, no sorted, no heuristic pick"
    assert resolution.qualified_artifact_ids == ("wi-c", "wi-a", "wi-b")


def test_full_candidate_evidence_is_preserved_in_every_outcome() -> None:
    pairs = [
        (make_candidate(), make_artifact()),
        (make_candidate(UNRELATED), make_unrelated_artifact()),
    ]
    resolution = resolve_cardinality(qualify_candidates(pairs, make_change()))
    assert resolution.candidate_artifact_refs == (ARTIFACT, UNRELATED)
    rejected = next(q for q in resolution.qualifications if q.artifact_id == UNRELATED)
    assert rejected.failed_conditions, "why it was rejected is retained as evidence"
