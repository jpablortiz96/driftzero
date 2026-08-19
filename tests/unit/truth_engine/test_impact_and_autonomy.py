"""M0-C focused tests for T025-T028 — qualification, cardinality, divergence, autonomy gate."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import AffectedArtifactCandidate, ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.truth_engine.autonomy_gate import (
    AutonomyCondition,
    AutonomyDecision,
    evaluate_autonomy,
)
from driftzero.truth_engine.divergence import (
    DivergenceReason,
    TargetStatus,
    evaluate_divergence,
)
from driftzero.truth_engine.impact import (
    ImpactOutcome,
    QualificationCondition,
    qualify_candidate,
    qualify_candidates,
    resolve_cardinality,
)

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

SOURCE_V2 = {"label_position": "TOP_RIGHT", "box_size": "STANDARD", "seal_type": "TAPE"}


def make_change(**overrides: object) -> ApprovedChange:
    base: dict[str, object] = {
        "change_id": "chg-1",
        "source_procedure_id": "proc-warehouse-packing",
        "source_version": "v2",
        "previous_version": "v1",
        "operation_id": "packing_label_placement",
        "requirement_id": "label_position",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": ["wi-packing-standard-001"],
        "approved_status": "APPROVED",
        "source_evidence_ref": "fixtures/source_procedure_v2.json",
        "received_at": T0,
        "data_classification": SYNTHETIC,
    }
    return ApprovedChange(**{**base, **overrides})  # type: ignore[arg-type]


def make_artifact(**overrides: object) -> DownstreamArtifact:
    base: dict[str, object] = {
        "artifact_id": "wi-packing-standard-001",
        "artifact_type": "work_instruction",
        "operation_id": "packing_label_placement",
        "requirement_id": "label_position",
        "current_value": "LEFT",
        "content_ref": "fixtures/stale_artifact.json",
        "authorized_for_remediation": True,
        "requirements": {"label_position": "LEFT", "box_size": "STANDARD", "seal_type": "TAPE"},
        "data_classification": SYNTHETIC,
    }
    return DownstreamArtifact(**{**base, **overrides})  # type: ignore[arg-type]


def make_change_degraded(**overrides: object) -> ApprovedChange:
    """Build an ApprovedChange bypassing validation.

    Conditions 1-4 cannot be violated through the validated model (M0-A enforces
    ``min_length=1``). This simulates a corrupted or partially-populated record so the
    gate's fail-closed behavior on each condition is still provable.
    """
    base = make_change().model_dump()
    return ApprovedChange.model_construct(**{**base, **overrides})


def make_artifact_degraded(**overrides: object) -> DownstreamArtifact:
    """Build a DownstreamArtifact bypassing validation (see make_change_degraded)."""
    base = make_artifact().model_dump()
    return DownstreamArtifact.model_construct(**{**base, **overrides})


def make_candidate(artifact_id: str = "wi-packing-standard-001", *, is_affected: bool = True):
    return AffectedArtifactCandidate(
        artifact_id=artifact_id,
        impact_reason="proposed by agent",
        operation_match=True,
        instruction_correspondence=True,
        value_conflict=True,
        in_authorized_scope=True,
        is_affected=is_affected,
    )


# ============================ QUALIFICATION (T025) ====================================


def test_all_four_conditions_true_qualifies() -> None:
    q = qualify_candidate(make_candidate(), make_artifact(), make_change())
    assert q.qualified
    assert q.failed_conditions == ()
    assert all(q.conditions.values())


@pytest.mark.parametrize(
    ("overrides", "expected_failure"),
    [
        ({"operation_id": "forklift_navigation"}, QualificationCondition.OPERATION_MATCH),
        (
            {"requirement_id": "turn_direction"},
            QualificationCondition.INSTRUCTION_CORRESPONDENCE,
        ),
        ({"current_value": "TOP_RIGHT"}, QualificationCondition.VALUE_CONFLICT),
        ({"authorized_for_remediation": False}, QualificationCondition.AUTHORIZED_SCOPE),
        ({"artifact_id": "wi-not-in-scope"}, QualificationCondition.AUTHORIZED_SCOPE),
    ],
)
def test_each_condition_individually_false_blocks_qualification(
    overrides: dict[str, object], expected_failure: QualificationCondition
) -> None:
    q = qualify_candidate(make_candidate(), make_artifact(**overrides), make_change())
    assert not q.qualified
    assert expected_failure in q.failed_conditions


def test_agent_is_affected_true_cannot_override_failed_condition() -> None:
    """The unrelated-artifact case: agent says affected, deterministic rules say no."""
    unrelated = make_artifact(
        artifact_id="wi-forklift-turn-014",
        operation_id="forklift_navigation",
        requirement_id="turn_direction",
        authorized_for_remediation=False,
        requirements={"turn_direction": "LEFT"},
    )
    q = qualify_candidate(
        make_candidate("wi-forklift-turn-014", is_affected=True), unrelated, make_change()
    )
    assert not q.qualified
    assert q.agent_proposed_is_affected is True
    assert q.agent_proposal_disagreed is True


def test_agent_is_affected_false_cannot_veto_passing_conditions() -> None:
    """The flag is proposal-only in both directions."""
    q = qualify_candidate(make_candidate(is_affected=False), make_artifact(), make_change())
    assert q.qualified is True
    assert q.agent_proposed_is_affected is False
    assert q.agent_proposal_disagreed is True


def test_qualification_ignores_agent_condition_booleans() -> None:
    """Agent-supplied condition booleans are not read; only structured records are."""
    lying = AffectedArtifactCandidate(
        artifact_id="wi-packing-standard-001",
        impact_reason="agent claims everything matches",
        operation_match=True,
        instruction_correspondence=True,
        value_conflict=True,
        in_authorized_scope=True,
        is_affected=True,
    )
    q = qualify_candidate(lying, make_artifact(operation_id="forklift_navigation"), make_change())
    assert not q.qualified


# ============================ CARDINALITY (T026) ======================================


def test_zero_qualified_requires_review_and_does_not_fabricate_target() -> None:
    unrelated = make_artifact(
        artifact_id="wi-forklift-turn-014", operation_id="forklift_navigation"
    )
    resolution = resolve_cardinality(
        qualify_candidates(
            [(make_candidate("wi-forklift-turn-014"), unrelated)], make_change()
        )
    )
    assert resolution.outcome is ImpactOutcome.NO_QUALIFIED_TARGET
    assert resolution.requires_review is True
    assert resolution.affected_artifact_id is None
    assert resolution.qualified_artifact_ids == ()
    assert resolution.candidate_artifact_refs == ("wi-forklift-turn-014",)
    assert "zero" in resolution.impact_reason


def test_exactly_one_qualified_is_the_autonomous_path() -> None:
    resolution = resolve_cardinality(
        qualify_candidates([(make_candidate(), make_artifact())], make_change())
    )
    assert resolution.outcome is ImpactOutcome.SINGLE_QUALIFIED_TARGET
    assert resolution.requires_review is False
    assert resolution.affected_artifact_id == "wi-packing-standard-001"


def test_more_than_one_qualified_requires_review() -> None:
    change = make_change(authorized_scope=["wi-a", "wi-b"])
    pairs = [
        (make_candidate("wi-a"), make_artifact(artifact_id="wi-a")),
        (make_candidate("wi-b"), make_artifact(artifact_id="wi-b")),
    ]
    resolution = resolve_cardinality(qualify_candidates(pairs, change))
    assert resolution.outcome is ImpactOutcome.MULTIPLE_QUALIFIED_TARGETS
    assert resolution.requires_review is True
    assert resolution.qualified_artifact_ids == ("wi-a", "wi-b")


def test_multiple_qualified_never_selects_a_candidate() -> None:
    change = make_change(authorized_scope=["wi-a", "wi-b", "wi-c"])
    pairs = [
        (make_candidate(i), make_artifact(artifact_id=i)) for i in ("wi-c", "wi-a", "wi-b")
    ]
    resolution = resolve_cardinality(qualify_candidates(pairs, change))
    assert resolution.affected_artifact_id is None, "must not pick first, sorted, or any"
    assert resolution.qualified_artifact_ids == ("wi-c", "wi-a", "wi-b"), "input order preserved"


def test_full_candidate_evidence_preserved_in_every_outcome() -> None:
    change = make_change(authorized_scope=["wi-a"])
    pairs = [
        (make_candidate("wi-a"), make_artifact(artifact_id="wi-a")),
        (
            make_candidate("wi-unrelated"),
            make_artifact(artifact_id="wi-unrelated", operation_id="forklift_navigation"),
        ),
    ]
    resolution = resolve_cardinality(qualify_candidates(pairs, change))
    assert resolution.candidate_artifact_refs == ("wi-a", "wi-unrelated")
    assert len(resolution.qualifications) == 2
    rejected = next(q for q in resolution.qualifications if q.artifact_id == "wi-unrelated")
    assert rejected.failed_conditions  # why it was rejected is retained


# ============================ DIVERGENCE (T027) =======================================


def test_only_target_differs_is_conflict_free() -> None:
    result = evaluate_divergence(make_artifact(), SOURCE_V2, make_change())
    assert result.has_additional_divergence is False
    assert result.target_status is TargetStatus.AT_PREVIOUS_VALUE
    assert result.divergent_requirement_ids == ()
    assert result.is_conflict_free is True


def test_unrelated_requirement_difference_is_additional_divergence() -> None:
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "TAPE"}
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.has_additional_divergence is True
    assert result.divergent_requirement_ids == ("box_size",)
    assert DivergenceReason.ADDITIONAL_DIVERGENCE in result.reason_codes
    assert result.is_conflict_free is False


def test_multiple_non_target_differences_return_all_divergent_ids() -> None:
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "GLUE"}
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.divergent_requirement_ids == ("box_size", "seal_type")


def test_duplicate_contradictory_target_representation_blocks() -> None:
    """Scalar current_value and structured requirements disagree about the target."""
    artifact = make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "LEFT", "box_size": "STANDARD", "seal_type": "TAPE"},
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.DUPLICATE_REPRESENTATION
    assert DivergenceReason.TARGET_DUPLICATE_REPRESENTATION in result.reason_codes
    assert result.is_conflict_free is False


def test_target_third_value_is_rejected() -> None:
    artifact = make_artifact(
        current_value="BOTTOM_LEFT",
        requirements={"label_position": "BOTTOM_LEFT", "box_size": "STANDARD",
                      "seal_type": "TAPE"},
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.UNEXPECTED_VALUE
    assert DivergenceReason.TARGET_UNEXPECTED_VALUE in result.reason_codes
    assert result.is_conflict_free is False


def test_target_already_current_is_not_a_divergence() -> None:
    """The already-compliant case stays conflict-free so the NO_OP path remains open."""
    artifact = make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "TOP_RIGHT", "box_size": "STANDARD",
                      "seal_type": "TAPE"},
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.ALREADY_CURRENT
    assert result.has_additional_divergence is False
    assert result.is_conflict_free is True


def test_missing_requirements_are_flagged() -> None:
    missing_target = make_artifact(requirements={"box_size": "STANDARD", "seal_type": "TAPE"})
    result = evaluate_divergence(missing_target, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.MISSING_IN_ARTIFACT
    assert not result.is_conflict_free

    missing_scope = make_artifact(requirements={"label_position": "LEFT", "box_size": "STANDARD"})
    scoped = evaluate_divergence(missing_scope, SOURCE_V2, make_change())
    assert scoped.divergent_requirement_ids == ("seal_type",)
    assert DivergenceReason.SCOPE_REQUIREMENT_MISSING in scoped.reason_codes


def test_source_not_carrying_approved_value_is_rejected() -> None:
    stale_source = {"label_position": "LEFT", "box_size": "STANDARD", "seal_type": "TAPE"}
    result = evaluate_divergence(make_artifact(), stale_source, make_change())
    assert DivergenceReason.SOURCE_TARGET_NOT_APPROVED_VALUE in result.reason_codes
    assert result.is_conflict_free is False


def test_divergence_is_independent_of_agent_proposal() -> None:
    """The comparator never receives a candidate, so no agent flag can reach it."""
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "TAPE"}
    )
    assert evaluate_divergence(artifact, SOURCE_V2, make_change()).is_conflict_free is False


# ============================ AUTONOMY GATE (T028) ====================================


def test_all_nine_conditions_true_authorizes() -> None:
    decision = evaluate_autonomy(make_artifact(), SOURCE_V2, make_change())
    assert decision.authorized is True
    assert decision.failed_conditions == ()
    assert decision.requires_review is False
    assert len(decision.conditions) == 9
    assert all(decision.conditions.values())


@pytest.mark.parametrize(
    ("artifact_overrides", "change_overrides", "expected"),
    [
        ({}, {"source_version": ""}, AutonomyCondition.C1_SOURCE_VERSION_KNOWN),
        ({}, {"requirement_id": ""}, AutonomyCondition.C2_REQUIREMENT_KNOWN),
        ({}, {"previous_value": ""}, AutonomyCondition.C3_PREVIOUS_VALUE_KNOWN),
        ({}, {"current_value": ""}, AutonomyCondition.C4_NEW_VALUE_KNOWN),
        ({}, {"previous_value": "TOP_RIGHT"}, AutonomyCondition.C5_SINGLE_ATOMIC_CHANGE),
        ({"authorized_for_remediation": False}, {}, AutonomyCondition.C6_TARGET_AUTHORIZED),
        (
            {"requirements": {"box_size": "STANDARD", "seal_type": "TAPE"}},
            {},
            AutonomyCondition.C7_TARGET_UNIQUELY_IDENTIFIABLE,
        ),
        (
            {"requirements": {"label_position": "LEFT", "box_size": "LARGE",
                              "seal_type": "TAPE"}},
            {},
            AutonomyCondition.C8_NO_CONFLICTING_DIVERGENCE,
        ),
        ({"content_ref": ""}, {}, AutonomyCondition.C9_EVIDENCE_PRESERVABLE),
    ],
)
def test_each_condition_individually_false_denies_authorization(
    artifact_overrides: dict[str, object],
    change_overrides: dict[str, object],
    expected: AutonomyCondition,
) -> None:
    artifact = make_artifact_degraded(**artifact_overrides)
    change = make_change_degraded(**change_overrides)
    decision = evaluate_autonomy(artifact, SOURCE_V2, change)
    assert decision.authorized is False
    assert expected in decision.failed_conditions
    assert decision.requires_review is True


def test_all_nine_conditions_are_evaluated_independently() -> None:
    """Every condition appears in the result, so tests can name the failing one."""
    decision = evaluate_autonomy(make_artifact(), SOURCE_V2, make_change())
    assert set(decision.conditions) == set(AutonomyCondition)
    assert len(AutonomyCondition) == 9


def test_condition_8_delegates_to_the_divergence_comparator() -> None:
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "TAPE"}
    )
    change = make_change()
    decision = evaluate_autonomy(artifact, SOURCE_V2, change)
    standalone = evaluate_divergence(artifact, SOURCE_V2, change)

    assert decision.divergence == standalone, "one authoritative comparison, not two"
    assert (
        decision.conditions[AutonomyCondition.C8_NO_CONFLICTING_DIVERGENCE]
        is standalone.is_conflict_free
    )


def test_no_partial_or_threshold_approval() -> None:
    """Eight of nine is still a denial."""
    artifact = make_artifact_degraded(content_ref="")
    decision = evaluate_autonomy(artifact, SOURCE_V2, make_change())
    passing = sum(1 for ok in decision.conditions.values() if ok)
    assert passing == 8
    assert decision.authorized is False


def test_failed_gate_returns_deterministic_condition_identifiers() -> None:
    artifact = make_artifact_degraded(
        authorized_for_remediation=False,
        content_ref="",
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "TAPE"},
    )
    decision = evaluate_autonomy(artifact, SOURCE_V2, make_change())
    assert set(decision.failed_conditions) == {
        AutonomyCondition.C6_TARGET_AUTHORIZED,
        AutonomyCondition.C8_NO_CONFLICTING_DIVERGENCE,
        AutonomyCondition.C9_EVIDENCE_PRESERVABLE,
    }
    for condition in decision.failed_conditions:
        assert isinstance(condition, AutonomyCondition)


def test_gate_performs_no_mutation_and_no_transition() -> None:
    """The gate authorizes; it never writes, builds evidence, or advances state."""
    from driftzero.truth_engine import autonomy_gate

    exported = set(dir(autonomy_gate))
    for forbidden in (
        "transition",
        "apply_patch",
        "write_artifact",
        "MutationEvidence",
        "build_mutation_evidence",
    ):
        assert forbidden not in exported

    artifact = make_artifact()
    before = artifact.model_dump()
    decision = evaluate_autonomy(artifact, SOURCE_V2, make_change())
    assert isinstance(decision, AutonomyDecision)
    assert artifact.model_dump() == before, "input artifact must be untouched"


def test_already_current_target_still_authorizes_for_the_no_op_path() -> None:
    """An artifact that became compliant is authorized to proceed and resolve as NO_OP."""
    artifact = make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "TOP_RIGHT", "box_size": "STANDARD",
                      "seal_type": "TAPE"},
    )
    decision = evaluate_autonomy(artifact, SOURCE_V2, make_change())
    assert decision.authorized is True
    assert decision.divergence.target_status is TargetStatus.ALREADY_CURRENT
