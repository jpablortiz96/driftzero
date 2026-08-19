"""T050 — Condition-8 divergence comparator acceptance (spec § Autonomy Boundaries)."""

from __future__ import annotations

import pytest

from driftzero.truth_engine.divergence import (
    DivergenceReason,
    TargetStatus,
    evaluate_divergence,
)

from ._acceptance import SOURCE_V2, make_artifact, make_change


def test_hero_allowed_case_is_conflict_free() -> None:
    """Only the target differs: LEFT/STANDARD/TAPE vs TOP_RIGHT/STANDARD/TAPE."""
    result = evaluate_divergence(make_artifact(), SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.AT_PREVIOUS_VALUE
    assert result.has_additional_divergence is False
    assert result.divergent_requirement_ids == ()
    assert result.is_conflict_free is True


def test_box_size_divergence_blocks() -> None:
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "TAPE"}
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.has_additional_divergence is True
    assert result.divergent_requirement_ids == ("box_size",)
    assert DivergenceReason.ADDITIONAL_DIVERGENCE in result.reason_codes
    assert result.is_conflict_free is False


def test_all_divergent_ids_are_reported() -> None:
    artifact = make_artifact(
        requirements={"label_position": "LEFT", "box_size": "LARGE", "seal_type": "GLUE"}
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.divergent_requirement_ids == ("box_size", "seal_type")


def test_unexpected_target_value_blocks() -> None:
    artifact = make_artifact(
        current_value="BOTTOM_LEFT",
        requirements={
            "label_position": "BOTTOM_LEFT",
            "box_size": "STANDARD",
            "seal_type": "TAPE",
        },
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.UNEXPECTED_VALUE
    assert result.is_conflict_free is False


def test_missing_target_blocks() -> None:
    artifact = make_artifact(requirements={"box_size": "STANDARD", "seal_type": "TAPE"})
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.MISSING_IN_ARTIFACT
    assert result.is_conflict_free is False


def test_duplicate_contradictory_target_representation_blocks() -> None:
    """Scalar current_value and the structured set disagree about the target."""
    artifact = make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "LEFT", "box_size": "STANDARD", "seal_type": "TAPE"},
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.DUPLICATE_REPRESENTATION
    assert DivergenceReason.TARGET_DUPLICATE_REPRESENTATION in result.reason_codes


@pytest.mark.parametrize(
    "requirements",
    [
        {"label_position": "LEFT", "box_size": "STANDARD"},
        {
            "label_position": "LEFT",
            "box_size": "STANDARD",
            "seal_type": "TAPE",
            "pallet_type": "EURO",
        },
    ],
    ids=["missing_on_artifact_side", "extra_on_artifact_side"],
)
def test_scope_requirement_missing_on_either_side_blocks(requirements: dict[str, str]) -> None:
    result = evaluate_divergence(make_artifact(requirements=requirements), SOURCE_V2, make_change())
    assert result.has_additional_divergence is True
    assert DivergenceReason.SCOPE_REQUIREMENT_MISSING in result.reason_codes
    assert result.is_conflict_free is False


def test_source_not_carrying_the_approved_value_blocks() -> None:
    stale_source = {"label_position": "LEFT", "box_size": "STANDARD", "seal_type": "TAPE"}
    result = evaluate_divergence(make_artifact(), stale_source, make_change())
    assert DivergenceReason.SOURCE_TARGET_NOT_APPROVED_VALUE in result.reason_codes
    assert result.is_conflict_free is False


def test_already_current_target_is_not_a_divergence() -> None:
    """Keeps the NO_OP path reachable."""
    artifact = make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "TOP_RIGHT", "box_size": "STANDARD", "seal_type": "TAPE"},
    )
    result = evaluate_divergence(artifact, SOURCE_V2, make_change())
    assert result.target_status is TargetStatus.ALREADY_CURRENT
    assert result.is_conflict_free is True
