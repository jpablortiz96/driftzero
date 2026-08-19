"""M0-A structural smoke tests for T001-T019.

Scope is deliberately narrow: these prove the domain models are structurally
correct. The full deterministic suite (T047-T059) is NOT implemented here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from driftzero.models.action import ActionExecution, ActionStatus, ActionType
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import AffectedArtifactCandidate, ApprovedChange, ChangeSet
from driftzero.models.classification import (
    ClassificationLabel,
    DataClassification,
    LineageEntry,
)
from driftzero.models.delivery import DeliveryResult
from driftzero.models.proof import ChangeProof, EvidenceManifest
from driftzero.models.remediation import MutationEvidence, NoOpEvidence, RemediationEvidence
from driftzero.models.verification import (
    FieldObservation,
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.models.workflow import STATE_CATEGORY, StateCategory, Workflow, WorkflowState

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


# --- 1. package imports successfully -------------------------------------------------


def test_package_imports() -> None:
    import driftzero

    assert driftzero.__version__


# --- 2. exactly 13 WorkflowState values ----------------------------------------------


def test_workflow_state_has_exactly_13_canonical_values() -> None:
    expected = {
        "CHANGE_RECEIVED",
        "IMPACT_DETERMINED",
        "REMEDIATION_PENDING",
        "REVIEW_REQUIRED",
        "REMEDIATION_COMPLETED",
        "FRONTLINE_DELIVERY_COMPLETED",
        "AWAITING_FIELD_VERIFICATION",
        "VERIFICATION_INCONCLUSIVE",
        "VERIFICATION_FAILED",
        "VERIFICATION_PASSED",
        "PROOF_COMPLETE",
        "SUPERSEDED",
        "FAILED",
    }
    actual = {s.value for s in WorkflowState}
    assert len(WorkflowState) == 13
    assert actual == expected


def test_forbidden_infrastructure_states_absent() -> None:
    forbidden = {"FAILED_OR_BLOCKED", "COMPLETE", "PROCESSING", "PENDING", "CANCELLED"}
    assert forbidden.isdisjoint({s.value for s in WorkflowState})


def test_every_state_has_a_category() -> None:
    assert set(STATE_CATEGORY) == set(WorkflowState)
    assert STATE_CATEGORY[WorkflowState.REVIEW_REQUIRED] is StateCategory.BLOCKING_GATE
    assert (
        STATE_CATEGORY[WorkflowState.VERIFICATION_FAILED] is StateCategory.BLOCKING_RECOVERABLE
    )
    assert STATE_CATEGORY[WorkflowState.SUPERSEDED] is StateCategory.TERMINAL_NON_SUCCESS
    assert STATE_CATEGORY[WorkflowState.PROOF_COMPLETE] is StateCategory.TERMINAL_SUCCESS


# --- 3/4. MutationEvidence accepts valid shape, rejects invalid -----------------------


def _mutation() -> MutationEvidence:
    return MutationEvidence(
        artifact_id="wi-packing-standard-001",
        before_ref="fixtures/stale_artifact.json",
        after_ref="fixtures/stale_artifact.after.json",
        before_hash="a" * 64,
        after_hash="b" * 64,
        before_value="LEFT",
        after_value="TOP_RIGHT",
        patch_description="label_position LEFT -> TOP_RIGHT",
        action_id="act-remediate-001",
        data_classification=SYNTHETIC,
    )


def test_mutation_evidence_accepts_valid_shape() -> None:
    m = _mutation()
    assert m.remediation_type == "MUTATION"
    assert m.reconciled is False


def test_mutation_evidence_rejects_missing_after_state() -> None:
    with pytest.raises(ValidationError):
        MutationEvidence(
            artifact_id="wi-1",
            before_ref="ref",
            before_hash="a" * 64,
            before_value="LEFT",
            after_value="TOP_RIGHT",
            patch_description="d",
            action_id="act-1",
            data_classification=SYNTHETIC,
        )  # type: ignore[call-arg]


def test_reconciled_mutation_stays_mutation() -> None:
    m = _mutation().model_copy(update={"reconciled": True})
    assert m.reconciled is True
    assert m.remediation_type == "MUTATION"


# --- 5/6. NoOpEvidence accepts valid shape, cannot carry mutation fields --------------


def _no_op() -> NoOpEvidence:
    return NoOpEvidence(
        artifact_id="wi-packing-standard-002",
        evaluated_artifact_ref="fixtures/already_compliant_artifact.json",
        evaluated_artifact_hash="c" * 64,
        observed_value="TOP_RIGHT",
        expected_value="TOP_RIGHT",
        no_op_reason="artifact already represented the approved value",
        compliance_basis="requirements.label_position",
        data_classification=SYNTHETIC,
    )


def test_no_op_evidence_accepts_valid_shape() -> None:
    n = _no_op()
    assert n.remediation_type == "NO_OP"
    assert n.observed_value == n.expected_value


@pytest.mark.parametrize("fabricated", ["before_ref", "after_ref", "before_hash", "after_hash"])
def test_no_op_evidence_rejects_fabricated_mutation_fields(fabricated: str) -> None:
    payload = _no_op().model_dump()
    payload[fabricated] = "fabricated"
    with pytest.raises(ValidationError):
        NoOpEvidence.model_validate(payload)


# --- 7. RemediationEvidence discriminates correctly ----------------------------------


def test_remediation_union_discriminates() -> None:
    adapter = TypeAdapter(RemediationEvidence)
    assert isinstance(adapter.validate_python(_mutation().model_dump()), MutationEvidence)
    assert isinstance(adapter.validate_python(_no_op().model_dump()), NoOpEvidence)


def test_remediation_union_rejects_unknown_discriminator() -> None:
    adapter = TypeAdapter(RemediationEvidence)
    payload = _no_op().model_dump()
    payload["remediation_type"] = "PARTIAL"
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


# --- 8/9. multi-dimensional classification and lineage -------------------------------


def test_classification_is_multi_dimensional_with_lineage() -> None:
    derived_real = DataClassification(
        labels=[ClassificationLabel.DERIVED, ClassificationLabel.REAL],
        lineage=[
            LineageEntry(
                source_ref="gs://evidence/photo.jpg",
                source_classification=[ClassificationLabel.REAL],
                relationship="observed_from",
            ),
            LineageEntry(
                source_ref="fixtures/hero_change.json",
                source_classification=[ClassificationLabel.SYNTHETIC],
                relationship="input_to",
            ),
        ],
    )
    assert derived_real.has(ClassificationLabel.DERIVED)
    assert derived_real.has(ClassificationLabel.REAL)
    assert not derived_real.has(ClassificationLabel.SIMULATED)
    assert [link.relationship for link in derived_real.lineage] == ["observed_from", "input_to"]


def test_classification_requires_at_least_one_label_and_rejects_duplicates() -> None:
    with pytest.raises(ValidationError):
        DataClassification(labels=[])
    with pytest.raises(ValidationError):
        DataClassification(labels=[ClassificationLabel.REAL, ClassificationLabel.REAL])


# --- 10. ActionExecution is not a WorkflowState --------------------------------------


def test_action_status_values_are_not_workflow_states() -> None:
    assert {s.value for s in ActionStatus}.isdisjoint({s.value for s in WorkflowState})
    assert {t.value for t in ActionType}.isdisjoint({s.value for s in WorkflowState})


def test_action_execution_ledger_record() -> None:
    a = ActionExecution(
        action_id="act-remediate-001",
        workflow_id="wf-001",
        action_type=ActionType.REMEDIATE_ARTIFACT,
        status=ActionStatus.PLANNED,
        target_ref="wi-packing-standard-001",
        intent={"expected_before_hash": "a" * 64, "expected_after_value": "TOP_RIGHT"},
        created_at=NOW,
        updated_at=NOW,
    )
    assert a.status is ActionStatus.PLANNED
    assert a.attempt_count == 0
    assert not isinstance(a.status, WorkflowState)


# --- remaining model shapes -----------------------------------------------------------


def test_approved_change_and_changeset_shapes() -> None:
    change = ApprovedChange(
        change_id="chg-1",
        source_procedure_id="proc-warehouse-packing",
        source_version="v2",
        previous_version="v1",
        operation_id="packing_label_placement",
        requirement_id="label_position",
        previous_value="LEFT",
        current_value="TOP_RIGHT",
        authorized_scope=["wi-packing-standard-001"],
        approved_status="APPROVED",
        source_evidence_ref="fixtures/source_procedure_v2.json",
        received_at=NOW,
        data_classification=SYNTHETIC,
    )
    assert change.authorized_scope == ["wi-packing-standard-001"]

    candidate = AffectedArtifactCandidate(
        artifact_id="wi-packing-standard-001",
        impact_reason="operation and requirement match with conflicting value",
        operation_match=True,
        instruction_correspondence=True,
        value_conflict=True,
        in_authorized_scope=True,
        is_affected=True,
    )
    cs = ChangeSet(
        change_id="chg-1",
        source_procedure_id="proc-warehouse-packing",
        source_version="v2",
        operation_id="packing_label_placement",
        requirement_id="label_position",
        previous_value="LEFT",
        current_value="TOP_RIGHT",
        authorized_scope=["wi-packing-standard-001"],
        candidate_affected_artifacts=[candidate],
    )
    assert len(cs.candidate_affected_artifacts) == 1


def test_changeset_permits_zero_and_multiple_candidates() -> None:
    """Cardinality is a Truth Engine decision (T026), not a model constraint."""
    base = {
        "change_id": "chg-1",
        "source_procedure_id": "p",
        "source_version": "v2",
        "operation_id": "op",
        "requirement_id": "r",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": [],
    }
    assert ChangeSet(**base, candidate_affected_artifacts=[]).candidate_affected_artifacts == []
    candidate = AffectedArtifactCandidate(
        artifact_id="a",
        impact_reason="r",
        operation_match=True,
        instruction_correspondence=True,
        value_conflict=True,
        in_authorized_scope=True,
        is_affected=True,
    )
    many = ChangeSet(**base, candidate_affected_artifacts=[candidate, candidate])
    assert len(many.candidate_affected_artifacts) == 2


def test_downstream_artifact_shape() -> None:
    a = DownstreamArtifact(
        artifact_id="wi-packing-standard-001",
        artifact_type="work_instruction",
        operation_id="packing_label_placement",
        requirement_id="label_position",
        current_value="LEFT",
        content_ref="fixtures/stale_artifact.json",
        authorized_for_remediation=True,
        requirements={"label_position": "LEFT", "box_size": "STANDARD"},
        data_classification=SYNTHETIC,
    )
    assert a.authorized_for_remediation is True


def test_field_observation_carries_no_verdict() -> None:
    obs = FieldObservation(
        submission_id="sub-001",
        raw_evidence_ref="gs://evidence/photo.jpg",
        observed_label_position=ObservedPosition.LEFT,
        confidence_note="informational only",
    )
    assert not hasattr(obs, "verification_result")
    with pytest.raises(ValidationError):
        FieldObservation(
            submission_id="sub-001",
            raw_evidence_ref="r",
            observed_label_position="MAYBE_LEFT",  # type: ignore[arg-type]
        )


def test_verification_event_shape() -> None:
    ev = VerificationEvent(
        event_id="ev-1",
        submission_id="sub-001",
        workflow_id="wf-001",
        event_sequence=1,
        raw_evidence_ref="gs://evidence/photo.jpg",
        derived_observation=ObservedPosition.LEFT,
        expected_value="TOP_RIGHT",
        verification_result=VerificationResult.FAIL,
        timestamp=NOW,
        data_classification=SYNTHETIC,
    )
    assert ev.verification_result is VerificationResult.FAIL


def test_delivery_result_claim_and_receipt_are_separate_fields() -> None:
    d = DeliveryResult(
        worker_id="worker-opaque-01",
        delivery_mechanism="web_notification",
        delta_content="Place the label on the TOP-RIGHT.",
        delivered=True,
        delivery_evidence_ref="gs://evidence/delivery/receipt-001.json",
    )
    assert d.delivered is True
    assert d.delivery_evidence_ref
    assert d.training_video_ref is None


def test_workflow_aggregate_defaults_and_category() -> None:
    wf = Workflow(
        workflow_id="wf-001",
        change_id="chg-1",
        source_version="v2",
        state=WorkflowState.CHANGE_RECEIVED,
        worker_id="worker-opaque-01",
        created_at=NOW,
        updated_at=NOW,
        data_classification=SYNTHETIC,
    )
    assert wf.affected_artifact_id is None
    assert wf.candidate_artifact_refs == []
    assert wf.remediation_evidence is None
    assert wf.state_category is StateCategory.PROGRESSIVE


def test_workflow_accepts_either_remediation_variant() -> None:
    base = {
        "workflow_id": "wf-001",
        "change_id": "chg-1",
        "source_version": "v2",
        "state": WorkflowState.REMEDIATION_COMPLETED,
        "worker_id": "worker-opaque-01",
        "created_at": NOW,
        "updated_at": NOW,
        "data_classification": SYNTHETIC,
    }
    assert isinstance(
        Workflow(**base, remediation_evidence=_mutation()).remediation_evidence, MutationEvidence
    )
    assert isinstance(
        Workflow(**base, remediation_evidence=_no_op()).remediation_evidence, NoOpEvidence
    )


def test_change_proof_shape_with_either_variant() -> None:
    manifest = EvidenceManifest(
        source_change_ref="fixtures/hero_change.json",
        affected_artifact_ref="wi-packing-standard-001",
        remediation_evidence_refs=["before.json", "after.json"],
        delivery_ref="gs://evidence/delivery/receipt-001.json",
        verification_refs=["ev-1", "ev-2"],
        content_hashes={"before.json": "a" * 64},
    )
    proof = ChangeProof(
        proof_id="proof-001",
        workflow_id="wf-001",
        change_id="chg-1",
        source_procedure_id="proc-warehouse-packing",
        source_version="v2",
        previous_value="LEFT",
        current_value="TOP_RIGHT",
        affected_artifact_id="wi-packing-standard-001",
        remediation_evidence=_mutation(),
        delivery_status="DELIVERED",
        delivery_ref="gs://evidence/delivery/receipt-001.json",
        verification_result=VerificationResult.PASS,
        verification_event_id="ev-2",
        worker_id="worker-opaque-01",
        evidence_manifest=manifest,
        completion_timestamp=NOW,
        content_hash="d" * 64,
        data_classification=DataClassification(labels=[ClassificationLabel.DERIVED]),
    )
    assert proof.verification_result is VerificationResult.PASS
    assert len(proof.evidence_manifest.verification_refs) == 2

    no_op_proof = proof.model_copy(update={"remediation_evidence": _no_op()})
    assert isinstance(no_op_proof.remediation_evidence, NoOpEvidence)
