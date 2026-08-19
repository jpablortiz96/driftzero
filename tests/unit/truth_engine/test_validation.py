"""T058 — Trust-boundary rejection acceptance (FR-011).

Rejected crossings stay available for audit and satisfy nothing. Schema validity is
never sufficient, and no agent-supplied boolean is believed.
"""

from __future__ import annotations

import pytest

from driftzero.models.change import ChangeSet
from driftzero.models.delivery import DeliveryResult
from driftzero.models.verification import FieldObservation, ObservedPosition
from driftzero.truth_engine.impact import qualify_candidate
from driftzero.truth_engine.proof_generator import ProofCondition, evaluate_proof_invariants
from driftzero.truth_engine.validation import (
    Crossing,
    ValidationLayer,
    collect_rejections,
    validate_change_set,
    validate_delivery_result,
    validate_field_observation,
    validate_media_output,
    validate_remediation_evidence,
)
from driftzero.truth_engine.verification import (
    UnnormalizedObservationError,
    compare_observation,
    ingest_observation,
)

from ._acceptance import (
    ARTIFACT,
    BEFORE_HASH,
    RECEIPT,
    SYNTHETIC,
    T0,
    UNRELATED,
    WF,
    WORKER,
    make_candidate,
    make_change,
    make_mutation,
    make_no_op,
    make_proof_context,
    make_unrelated_artifact,
)

REJECT_REF = "evidence/security/rejected.json"


def make_change_set(**overrides: object) -> ChangeSet:
    change = make_change()
    base: dict[str, object] = {
        "change_id": change.change_id,
        "source_procedure_id": change.source_procedure_id,
        "source_version": change.source_version,
        "operation_id": change.operation_id,
        "requirement_id": change.requirement_id,
        "previous_value": change.previous_value,
        "current_value": change.current_value,
        "authorized_scope": [ARTIFACT],
        "candidate_affected_artifacts": [make_candidate()],
    }
    return ChangeSet(**{**base, **overrides})  # type: ignore[arg-type]


# ============================ Crossing 1 — ChangeSet ==================================


def test_valid_change_set_is_accepted() -> None:
    outcome = validate_change_set(
        make_change_set(),
        change=make_change(),
        known_artifact_ids=frozenset({ARTIFACT}),
        source_version_applicable=True,
        rejection_ref=REJECT_REF,
    )
    assert outcome.accepted
    assert outcome.rejection_ref is None


@pytest.mark.parametrize(
    ("overrides", "kwargs", "layer"),
    [
        ({"change_id": "chg-forged"}, {}, ValidationLayer.PROVENANCE),
        (
            {"source_procedure_id": "proc-other"},
            {},
            ValidationLayer.EXPECTED_SOURCE_IDENTITY,
        ),
        ({}, {"source_version_applicable": False}, ValidationLayer.SOURCE_VERSION_APPLICABILITY),
        (
            {"candidate_affected_artifacts": [make_candidate("wi-ghost")]},
            {},
            ValidationLayer.EXPECTED_ARTIFACT_IDENTITY,
        ),
        ({"current_value": "SIDEWAYS"}, {}, ValidationLayer.SEMANTIC_INVARIANT),
    ],
)
def test_schema_valid_change_sets_are_still_rejected(
    overrides: dict[str, object], kwargs: dict[str, object], layer: ValidationLayer
) -> None:
    outcome = validate_change_set(
        make_change_set(**overrides),
        change=make_change(),
        known_artifact_ids=frozenset({ARTIFACT}),
        source_version_applicable=bool(kwargs.get("source_version_applicable", True)),
        rejection_ref=REJECT_REF,
    )
    assert outcome.rejected
    assert layer in outcome.failed_layers
    assert outcome.rejection_ref == REJECT_REF


def test_agent_change_set_proposal_cannot_establish_impact() -> None:
    """Even an accepted ChangeSet does not decide impact — qualification does."""
    outcome = validate_change_set(
        make_change_set(candidate_affected_artifacts=[make_candidate(UNRELATED)]),
        change=make_change(),
        known_artifact_ids=frozenset({ARTIFACT, UNRELATED}),
        source_version_applicable=True,
        rejection_ref=REJECT_REF,
    )
    assert outcome.accepted, "shape is fine; the claim is not"
    q = qualify_candidate(make_candidate(UNRELATED), make_unrelated_artifact(), make_change())
    assert q.qualified is False


# ============================ Crossing 2 — RemediationEvidence ========================


def test_schema_valid_evidence_naming_an_unauthorized_artifact_is_rejected() -> None:
    outcome = validate_remediation_evidence(
        make_mutation(artifact_id="wi-not-authorized"),
        change=make_change(),
        expected_artifact_id="wi-not-authorized",
        expected_before_hash=BEFORE_HASH,
        expected_action_id="act-remediate-001",
        source_version_applicable=True,
        rejection_ref=REJECT_REF,
    )
    assert outcome.rejected
    assert ValidationLayer.AUTHORIZATION_SCOPE in outcome.failed_layers


def test_inconsistent_source_version_is_rejected() -> None:
    outcome = validate_remediation_evidence(
        make_mutation(),
        change=make_change(),
        expected_artifact_id=ARTIFACT,
        expected_before_hash=BEFORE_HASH,
        expected_action_id="act-remediate-001",
        source_version_applicable=False,
        rejection_ref=REJECT_REF,
    )
    assert ValidationLayer.SOURCE_VERSION_APPLICABILITY in outcome.failed_layers


def test_before_hash_mismatch_is_rejected() -> None:
    outcome = validate_remediation_evidence(
        make_mutation(before_hash="f" * 64),
        change=make_change(),
        expected_artifact_id=ARTIFACT,
        expected_before_hash=BEFORE_HASH,
        expected_action_id="act-remediate-001",
        source_version_applicable=True,
        rejection_ref=REJECT_REF,
    )
    assert ValidationLayer.BEFORE_STATE_CONSISTENCY in outcome.failed_layers


def test_wrong_tool_identity_is_rejected() -> None:
    outcome = validate_remediation_evidence(
        make_mutation(action_id="act-someone-else"),
        change=make_change(),
        expected_artifact_id=ARTIFACT,
        expected_before_hash=BEFORE_HASH,
        expected_action_id="act-remediate-001",
        source_version_applicable=True,
        rejection_ref=REJECT_REF,
    )
    assert ValidationLayer.EXPECTED_TOOL_IDENTITY in outcome.failed_layers


def test_no_op_claiming_non_compliance_is_rejected() -> None:
    outcome = validate_remediation_evidence(
        make_no_op(observed_value="LEFT"),
        change=make_change(),
        expected_artifact_id=ARTIFACT,
        expected_before_hash=BEFORE_HASH,
        expected_action_id="act-remediate-001",
        source_version_applicable=True,
        rejection_ref=REJECT_REF,
    )
    assert ValidationLayer.SEMANTIC_INVARIANT in outcome.failed_layers


# ============================ Crossing 3 — DeliveryResult =============================


def make_delivery(**overrides: object) -> DeliveryResult:
    base: dict[str, object] = {
        "worker_id": WORKER,
        "delivery_mechanism": "web_notification",
        "delta_content": "Place the label on the TOP-RIGHT.",
        "delivered": True,
        "delivery_evidence_ref": RECEIPT,
    }
    return DeliveryResult(**{**base, **overrides})  # type: ignore[arg-type]


def test_unearned_delivered_true_is_rejected() -> None:
    outcome = validate_delivery_result(
        make_delivery(delivery_evidence_ref="the-agent-says-so"),
        expected_worker_id=WORKER,
        expected_mechanism="web_notification",
        resolvable_receipt_refs=frozenset({RECEIPT}),
        rejection_ref=REJECT_REF,
    )
    assert outcome.rejected
    assert ValidationLayer.POSITIVE_RECEIPT in outcome.failed_layers


def test_a_rejected_delivery_cannot_satisfy_fr004() -> None:
    """The rejected result is retained for audit and still fails condition 4."""
    context = make_proof_context(receipt=None, rejected=[REJECT_REF])
    result = evaluate_proof_invariants(context)
    assert REJECT_REF in context.manifest.rejected_result_refs
    assert ProofCondition.C4_DELTA_DELIVERED in result.failed_conditions
    assert result.eligible is False


def test_delivery_to_the_wrong_worker_is_rejected() -> None:
    outcome = validate_delivery_result(
        make_delivery(worker_id="worker-someone-else"),
        expected_worker_id=WORKER,
        expected_mechanism="web_notification",
        resolvable_receipt_refs=frozenset({RECEIPT}),
        rejection_ref=REJECT_REF,
    )
    assert ValidationLayer.PROVENANCE in outcome.failed_layers


# ============================ Crossing 4 — FieldObservation ===========================


def test_out_of_enum_observation_is_rejected_not_coerced() -> None:
    with pytest.raises(UnnormalizedObservationError):
        compare_observation("TOP_RIGHT", "PROBABLY_TOP_RIGHT")


def test_observation_cannot_carry_a_verdict_field() -> None:
    observation = FieldObservation(
        submission_id="sub-9",
        raw_evidence_ref="gs://evidence/photo.jpg",
        observed_label_position=ObservedPosition.LEFT,
        confidence_note="model is certain this passes",
    )
    assert not hasattr(observation, "verification_result")
    result = ingest_observation(
        observation,
        workflow_id=WF,
        expected_value="TOP_RIGHT",
        existing_events=[],
        event_id="ev-9",
        timestamp=T0,
        data_classification=SYNTHETIC,
    )
    assert result.event.verification_result.value == "FAIL", "confidence cannot create PASS"


def test_unresolvable_raw_evidence_reference_is_rejected() -> None:
    outcome = validate_field_observation(
        FieldObservation(
            submission_id="sub-9",
            raw_evidence_ref="gs://evidence/missing.jpg",
            observed_label_position=ObservedPosition.TOP_RIGHT,
        ),
        resolvable_evidence_refs=frozenset({"gs://evidence/photo.jpg"}),
        known_submission_ids=frozenset(),
        rejection_ref=REJECT_REF,
    )
    assert outcome.rejected
    assert ValidationLayer.EVIDENCE_REFERENCE in outcome.failed_layers


def test_a_rejected_observation_cannot_create_a_pass() -> None:
    """A rejected crossing never becomes an authoritative verification event."""
    outcome = validate_field_observation(
        FieldObservation(
            submission_id="sub-9",
            raw_evidence_ref="gs://evidence/missing.jpg",
            observed_label_position=ObservedPosition.TOP_RIGHT,
        ),
        resolvable_evidence_refs=frozenset(),
        known_submission_ids=frozenset(),
        rejection_ref=REJECT_REF,
    )
    assert outcome.rejected
    context = make_proof_context(events=[], rejected=[REJECT_REF])
    result = evaluate_proof_invariants(context)
    assert ProofCondition.C5_LATEST_VERIFICATION_PASS in result.failed_conditions


# ============================ Crossing 5 — media ======================================


def test_generated_asset_without_classification_is_rejected() -> None:
    outcome = validate_media_output(
        asset_ref="gs://evidence/veo.mp4",
        resolvable_asset_refs=frozenset({"gs://evidence/veo.mp4"}),
        classification_recorded=False,
        rejection_ref=REJECT_REF,
    )
    assert outcome.rejected
    assert ValidationLayer.CLASSIFICATION_REQUIRED in outcome.failed_layers
    assert outcome.crossing is Crossing.MEDIA_OUTPUT


# ============================ rejection collection ====================================


def test_rejections_are_collected_for_the_manifest_but_prove_nothing() -> None:
    rejected = validate_delivery_result(
        make_delivery(delivery_evidence_ref="agent-assertion"),
        expected_worker_id=WORKER,
        expected_mechanism="web_notification",
        resolvable_receipt_refs=frozenset(),
        rejection_ref=REJECT_REF,
    )
    accepted = validate_media_output(
        asset_ref="gs://evidence/veo.mp4",
        resolvable_asset_refs=frozenset({"gs://evidence/veo.mp4"}),
        classification_recorded=True,
        rejection_ref="unused",
    )
    refs = collect_rejections({"delivery": rejected, "media": accepted})
    assert refs == (REJECT_REF,), "only rejected crossings contribute references"


def test_no_trusted_escape_hatch_exists() -> None:
    from driftzero.truth_engine import validation

    exported = set(dir(validation))
    for forbidden in ("trusted", "bypass", "force_accept", "override"):
        assert forbidden not in exported
