"""T051 — Nine-condition autonomy gate acceptance (FR-003).

For each condition an otherwise-valid case is constructed where only that condition
fails. Every one must independently deny authorization: 8/9 is never enough.
"""

from __future__ import annotations

import pytest

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.truth_engine.autonomy_gate import (
    AutonomyCondition,
    evaluate_autonomy,
)
from driftzero.truth_engine.divergence import TargetStatus, evaluate_divergence

from ._acceptance import SOURCE_V2, make_artifact, make_change


def degraded_change(**overrides: object) -> ApprovedChange:
    """Bypass validation to reach conditions 1-4, which the models otherwise guarantee."""
    return ApprovedChange.model_construct(**{**make_change().model_dump(), **overrides})


def degraded_artifact(**overrides: object) -> DownstreamArtifact:
    """Bypass validation to reach condition 9 (``content_ref`` is min_length=1)."""
    return DownstreamArtifact.model_construct(**{**make_artifact().model_dump(), **overrides})


def test_fully_valid_hero_case_authorizes() -> None:
    decision = evaluate_autonomy(make_artifact(), SOURCE_V2, make_change())
    assert decision.authorized is True
    assert decision.failed_conditions == ()
    assert decision.requires_review is False


def test_all_nine_conditions_are_reported_independently() -> None:
    decision = evaluate_autonomy(make_artifact(), SOURCE_V2, make_change())
    assert set(decision.conditions) == set(AutonomyCondition)
    assert len(AutonomyCondition) == 9


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
            {
                "requirements": {
                    "label_position": "LEFT",
                    "box_size": "LARGE",
                    "seal_type": "TAPE",
                }
            },
            {},
            AutonomyCondition.C8_NO_CONFLICTING_DIVERGENCE,
        ),
        ({"content_ref": ""}, {}, AutonomyCondition.C9_EVIDENCE_PRESERVABLE),
    ],
    ids=[f"condition_{i}" for i in range(1, 10)],
)
def test_each_condition_independently_denies_authorization(
    artifact_overrides: dict[str, object],
    change_overrides: dict[str, object],
    expected: AutonomyCondition,
) -> None:
    decision = evaluate_autonomy(
        degraded_artifact(**artifact_overrides), SOURCE_V2, degraded_change(**change_overrides)
    )
    assert decision.authorized is False
    assert expected in decision.failed_conditions
    assert decision.requires_review is True


def test_eight_of_nine_is_not_authorized() -> None:
    decision = evaluate_autonomy(degraded_artifact(content_ref=""), SOURCE_V2, make_change())
    assert sum(1 for ok in decision.conditions.values() if ok) == 8
    assert decision.authorized is False


def test_condition_8_delegates_to_the_single_comparator() -> None:
    """No second divergence algorithm may exist inside the gate."""
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "TAPE"}
    )
    change = make_change()
    decision = evaluate_autonomy(artifact, SOURCE_V2, change)
    standalone = evaluate_divergence(artifact, SOURCE_V2, change)
    assert decision.divergence == standalone
    assert (
        decision.conditions[AutonomyCondition.C8_NO_CONFLICTING_DIVERGENCE]
        is standalone.is_conflict_free
    )


def test_no_confidence_or_agent_override_exists() -> None:
    from driftzero.truth_engine import autonomy_gate

    exported = set(dir(autonomy_gate))
    for forbidden in ("confidence", "override", "force_authorize", "trusted"):
        assert forbidden not in exported


def test_gate_never_mutates_or_transitions() -> None:
    from driftzero.truth_engine import autonomy_gate

    exported = set(dir(autonomy_gate))
    for forbidden in ("transition", "apply_patch", "write_artifact", "MutationEvidence"):
        assert forbidden not in exported

    artifact = make_artifact()
    before = artifact.model_dump()
    evaluate_autonomy(artifact, SOURCE_V2, make_change())
    assert artifact.model_dump() == before


def test_already_current_target_still_authorizes_for_the_no_op_path() -> None:
    artifact = make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "TOP_RIGHT", "box_size": "STANDARD", "seal_type": "TAPE"},
    )
    decision = evaluate_autonomy(artifact, SOURCE_V2, make_change())
    assert decision.authorized is True
    assert decision.divergence.target_status is TargetStatus.ALREADY_CURRENT
