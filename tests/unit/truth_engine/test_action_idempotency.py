"""M0-D focused tests for T029-T036 — identity, ledger, retry, reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftzero.models.action import ActionExecution, ActionStatus, ActionType
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.models.verification import (
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.models.workflow import WorkflowState
from driftzero.truth_engine.actions import (
    ActionLedger,
    DeliveryOutcome,
    DuplicateActionError,
    ReconciliationBlocker,
    ReconciliationOutcome,
    RetryDecision,
    build_remediation_intent,
    decide_retry,
    no_op_admissible,
    reconcile_delivery,
    reconcile_mutation,
    was_ever_dispatched,
)
from driftzero.truth_engine.idempotency import (
    ChangeEventOutcome,
    SubmissionOutcome,
    classify_change_event,
    classify_submission,
    derive_action_id,
    derive_delivery_action_id,
    derive_field_evidence_action_id,
    derive_proof_action_id,
    derive_remediation_action_id,
)

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
BEFORE_HASH = "a" * 64
AFTER_HASH = "b" * 64
WF = "wf-001"


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


def make_event(submission_id: str, *, event_id: str, sequence: int) -> VerificationEvent:
    return VerificationEvent(
        event_id=event_id,
        submission_id=submission_id,
        workflow_id=WF,
        event_sequence=sequence,
        raw_evidence_ref=f"gs://evidence/{submission_id}.jpg",
        derived_observation=ObservedPosition.LEFT,
        expected_value="TOP_RIGHT",
        verification_result=VerificationResult.FAIL,
        timestamp=T0,
        data_classification=SYNTHETIC,
    )


def planned_remediation(
    ledger: ActionLedger, *, change: ApprovedChange, artifact: DownstreamArtifact
) -> str:
    action_id = derive_remediation_action_id(
        workflow_id=WF, change=change, artifact_id=artifact.artifact_id
    )
    ledger.plan(
        action_id=action_id,
        workflow_id=WF,
        action_type=ActionType.REMEDIATE_ARTIFACT,
        target_ref=artifact.artifact_id,
        intent=build_remediation_intent(
            change=change, artifact=artifact, expected_before_hash=BEFORE_HASH
        ),
        occurred_at=T0,
    )
    return action_id


# ============================ T030 — stable action identity ===========================


def test_identical_logical_inputs_produce_identical_action_id() -> None:
    a = derive_remediation_action_id(
        workflow_id=WF, change=make_change(), artifact_id="wi-packing-standard-001"
    )
    b = derive_remediation_action_id(
        workflow_id=WF, change=make_change(), artifact_id="wi-packing-standard-001"
    )
    assert a == b


@pytest.mark.parametrize(
    "kwargs",
    [
        {"workflow_id": "wf-other"},
        {"action_type": ActionType.DELIVER_DELTA},
        {"target_ref": "wi-other"},
        {"change_id": "chg-other"},
        {"source_version": "v9"},
    ],
)
def test_materially_different_actions_get_different_ids(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "workflow_id": WF,
        "action_type": ActionType.REMEDIATE_ARTIFACT,
        "target_ref": "wi-packing-standard-001",
        "change_id": "chg-1",
        "source_version": "v2",
    }
    assert derive_action_id(**base) != derive_action_id(**{**base, **kwargs})  # type: ignore[arg-type]


def test_action_id_survives_reconstructed_equivalent_objects() -> None:
    """Rebuilt-from-serialization objects yield the same identity, as after a restart."""
    change = make_change()
    rebuilt = ApprovedChange.model_validate(change.model_dump())
    assert derive_remediation_action_id(
        workflow_id=WF, change=change, artifact_id="wi-1"
    ) == derive_remediation_action_id(workflow_id=WF, change=rebuilt, artifact_id="wi-1")


def test_action_id_is_not_process_randomized() -> None:
    """SHA-256 over canonical JSON, not Python's per-process randomized hash()."""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from driftzero.models.action import ActionType;"
        "from driftzero.truth_engine.idempotency import derive_action_id;"
        "print(derive_action_id(workflow_id='wf-001', action_type=ActionType.GENERATE_PROOF,"
        " target_ref='wf-001', change_id='chg-1', source_version='v2'))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1
    assert runs.pop() == derive_action_id(
        workflow_id="wf-001",
        action_type=ActionType.GENERATE_PROOF,
        target_ref="wf-001",
        change_id="chg-1",
        source_version="v2",
    )


def test_all_four_action_types_have_distinct_identities() -> None:
    change = make_change()
    ids = {
        derive_remediation_action_id(workflow_id=WF, change=change, artifact_id="wi-1"),
        derive_delivery_action_id(workflow_id=WF, change=change, worker_id="worker-1"),
        derive_field_evidence_action_id(workflow_id=WF, submission_id="sub-1"),
        derive_proof_action_id(workflow_id=WF),
    }
    assert len(ids) == 4


def test_proof_action_id_is_stable_per_workflow() -> None:
    """One canonical proof identity; retries resolve to it. Generation is T043+."""
    assert derive_proof_action_id(workflow_id=WF) == derive_proof_action_id(workflow_id=WF)
    assert derive_proof_action_id(workflow_id=WF) != derive_proof_action_id(workflow_id="wf-2")


# ============================ T029 — duplicate change events ==========================


def test_first_delivery_is_a_new_logical_change() -> None:
    decision = classify_change_event(make_change(), {})
    assert decision.outcome is ChangeEventOutcome.NEW_LOGICAL_CHANGE
    assert decision.is_duplicate is False
    assert decision.existing_workflow_id is None


def test_redelivery_is_absorbed_as_transport_duplicate() -> None:
    decision = classify_change_event(make_change(), {"chg-1": WF})
    assert decision.outcome is ChangeEventOutcome.TRANSPORT_DUPLICATE
    assert decision.is_duplicate is True
    assert decision.existing_workflow_id == WF


def test_different_change_id_is_not_a_duplicate() -> None:
    decision = classify_change_event(make_change(change_id="chg-2"), {"chg-1": WF})
    assert decision.outcome is ChangeEventOutcome.NEW_LOGICAL_CHANGE


# ============================ T031 — field evidence submissions =======================


def test_duplicate_submission_id_is_absorbed() -> None:
    existing = [make_event("sub-1", event_id="ev-1", sequence=1)]
    decision = classify_submission("sub-1", existing)
    assert decision.outcome is SubmissionOutcome.TRANSPORT_DUPLICATE
    assert decision.existing_event_id == "ev-1"


def test_duplicate_submission_never_allocates_a_new_event_sequence() -> None:
    existing = [make_event("sub-1", event_id="ev-1", sequence=1)]
    assert classify_submission("sub-1", existing).allocates_event_sequence is False


def test_duplicate_submission_is_not_treated_as_corrected_evidence() -> None:
    existing = [make_event("sub-1", event_id="ev-1", sequence=1)]
    decision = classify_submission("sub-1", existing)
    assert decision.outcome is not SubmissionOutcome.NEW_EVIDENCE_ATTEMPT


def test_different_submission_id_is_a_new_evidence_attempt() -> None:
    existing = [make_event("sub-1", event_id="ev-1", sequence=1)]
    decision = classify_submission("sub-2", existing)
    assert decision.outcome is SubmissionOutcome.NEW_EVIDENCE_ATTEMPT
    assert decision.allocates_event_sequence is True
    assert decision.existing_event_id is None


# ============================ T032 — ledger ===========================================


def test_ledger_holds_one_record_per_action_id() -> None:
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    assert ledger.require(action_id).status is ActionStatus.PLANNED
    with pytest.raises(DuplicateActionError):
        planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    assert len(ledger.all_records()) == 1


def test_ledger_status_progression_and_attempt_count() -> None:
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    assert ledger.mark_attempted(action_id, occurred_at=T1).attempt_count == 1
    assert ledger.mark_attempted(action_id, occurred_at=T1).attempt_count == 2
    assert (
        ledger.mark_completed(action_id, occurred_at=T1, receipt_ref="rcpt-1").status
        is ActionStatus.COMPLETED
    )


def test_action_execution_is_not_workflow_state() -> None:
    assert {s.value for s in ActionStatus}.isdisjoint({s.value for s in WorkflowState})
    assert {t.value for t in ActionType}.isdisjoint({s.value for s in WorkflowState})
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    record = ledger.require(action_id)
    assert isinstance(record, ActionExecution)
    assert not isinstance(record.status, WorkflowState)


# ============================ T033 — retry deduplication ==============================


def test_unknown_action_is_safe_to_execute() -> None:
    assert decide_retry(ActionLedger(), "act-unknown") is RetryDecision.SAFE_TO_EXECUTE


def test_planned_action_is_safe_to_execute() -> None:
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    assert decide_retry(ledger, action_id) is RetryDecision.SAFE_TO_EXECUTE


def test_completed_action_is_never_re_executed() -> None:
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    ledger.mark_attempted(action_id, occurred_at=T1)
    ledger.mark_completed(action_id, occurred_at=T1)
    assert decide_retry(ledger, action_id) is RetryDecision.ALREADY_COMPLETED


@pytest.mark.parametrize("status", [ActionStatus.ATTEMPTED, ActionStatus.FAILED_OR_UNCERTAIN])
def test_uncertain_outcome_requires_reconciliation(status: ActionStatus) -> None:
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    ledger.mark_attempted(action_id, occurred_at=T1)
    if status is ActionStatus.FAILED_OR_UNCERTAIN:
        ledger.mark_failed_or_uncertain(action_id, occurred_at=T1)
    assert decide_retry(ledger, action_id) is RetryDecision.RECONCILIATION_REQUIRED


def test_retry_reuses_the_same_logical_action_id() -> None:
    """A restart recomputes the identity rather than minting a new action."""
    ledger = ActionLedger()
    change, artifact = make_change(), make_artifact()
    first = planned_remediation(ledger, change=change, artifact=artifact)
    ledger.mark_attempted(first, occurred_at=T1)
    after_restart = derive_remediation_action_id(
        workflow_id=WF, change=make_change(), artifact_id=artifact.artifact_id
    )
    assert after_restart == first
    assert decide_retry(ledger, after_restart) is RetryDecision.RECONCILIATION_REQUIRED
    assert len(ledger.all_records()) == 1


# ============================ T034/T035 — mutation reconciliation =====================


def crashed_ledger(
    change: ApprovedChange, artifact: DownstreamArtifact
) -> tuple[ActionLedger, str]:
    """Intent persisted, dispatched, then the process died before completion."""
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=change, artifact=artifact)
    ledger.mark_attempted(action_id, occurred_at=T1)
    return ledger, action_id


def mutated(**overrides: object) -> DownstreamArtifact:
    """The artifact as recovery observes it: already at the intended after-state."""
    base: dict[str, object] = {
        "current_value": "TOP_RIGHT",
        "requirements": {
            "label_position": "TOP_RIGHT",
            "box_size": "STANDARD",
            "seal_type": "TAPE",
        },
    }
    return make_artifact(**{**base, **overrides})


def reconcile(
    ledger: ActionLedger,
    action_id: str,
    *,
    observed: DownstreamArtifact,
    change: ApprovedChange | None = None,
    applicable: bool = True,
):
    return reconcile_mutation(
        ledger,
        action_id,
        observed_artifact=observed,
        observed_after_hash=AFTER_HASH,
        after_ref="gs://evidence/after.json",
        change=change or make_change(),
        source_version_applicable=applicable,
        occurred_at=T1,
        data_classification=SYNTHETIC,
    )


def test_crash_after_success_reconciles_when_all_invariants_hold() -> None:
    change, artifact = make_change(), make_artifact()
    ledger, action_id = crashed_ledger(change, artifact)
    result = reconcile(ledger, action_id, observed=mutated())
    assert result.outcome is ReconciliationOutcome.RECONCILED_MUTATION
    assert result.blockers == ()
    assert result.requires_review is False
    assert ledger.require(action_id).status is ActionStatus.COMPLETED
    assert ledger.require(action_id).reconciled is True


def test_reconciled_result_is_mutation_evidence_with_reconciled_true() -> None:
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    evidence = reconcile(ledger, action_id, observed=mutated()).evidence
    assert isinstance(evidence, MutationEvidence)
    assert evidence.remediation_type == "MUTATION"
    assert evidence.reconciled is True
    assert evidence.before_value == "LEFT"
    assert evidence.after_value == "TOP_RIGHT"
    assert evidence.action_id == action_id


def test_reconciliation_can_never_produce_no_op_evidence() -> None:
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    result = reconcile(ledger, action_id, observed=mutated())
    assert not isinstance(result.evidence, NoOpEvidence)
    for other in (
        reconcile(ActionLedger(), "act-missing", observed=mutated()),
        reconcile(*crashed_ledger(make_change(), make_artifact()), observed=make_artifact()),
    ):
        assert not isinstance(other.evidence, NoOpEvidence)
        assert other.evidence is None


def test_already_completed_action_is_not_reconciled_again() -> None:
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    ledger.mark_completed(action_id, occurred_at=T1)
    result = reconcile(ledger, action_id, observed=mutated())
    assert result.outcome is ReconciliationOutcome.ALREADY_COMPLETED
    assert result.evidence is None


# --- fail-closed cases (T035) ---------------------------------------------------------


def test_missing_pre_action_intent_prevents_reconciliation() -> None:
    result = reconcile(ActionLedger(), "act-never-planned", observed=mutated())
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.MISSING_PRE_ACTION_INTENT in result.blockers
    assert result.requires_review is True
    assert result.evidence is None


def test_never_dispatched_action_cannot_be_reconciled() -> None:
    """PLANNED means no side effect was attributed to this action."""
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    result = reconcile(ledger, action_id, observed=mutated())
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.NEVER_DISPATCHED in result.blockers


def test_action_identity_mismatch_prevents_reconciliation() -> None:
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    result = reconcile(ledger, action_id, observed=mutated(), change=make_change(change_id="chg-9"))
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.ACTION_IDENTITY_MISMATCH in result.blockers


def test_intent_target_mismatch_prevents_reconciliation() -> None:
    change = make_change(authorized_scope=["wi-packing-standard-001", "wi-other"])
    ledger, action_id = crashed_ledger(change, make_artifact())
    result = reconcile(ledger, action_id, observed=mutated(artifact_id="wi-other"), change=change)
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.INTENT_TARGET_MISMATCH in result.blockers


def test_unexpected_current_target_state_prevents_reconciliation() -> None:
    """Matching value is required; here the artifact is not in the intended after-state."""
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    result = reconcile(ledger, action_id, observed=make_artifact(current_value="BOTTOM_LEFT"))
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.TARGET_NOT_IN_INTENDED_AFTER_STATE in result.blockers


def test_superseded_source_version_prevents_reconciliation() -> None:
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    result = reconcile(ledger, action_id, observed=mutated(), applicable=False)
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.SOURCE_VERSION_NOT_APPLICABLE in result.blockers


def test_lost_authorization_prevents_reconciliation() -> None:
    ledger, action_id = crashed_ledger(make_change(), make_artifact())
    result = reconcile(ledger, action_id, observed=mutated(authorized_for_remediation=False))
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED
    assert ReconciliationBlocker.AUTHORIZATION_NO_LONGER_VALID in result.blockers


def test_matching_value_alone_never_establishes_ownership() -> None:
    """No ledger record: the value matches, yet nothing proves this workflow did it."""
    result = reconcile(ActionLedger(), "act-unowned", observed=mutated())
    assert result.outcome is ReconciliationOutcome.FAIL_CLOSED


# ============================ NO_OP vs reconciled MUTATION ============================


def test_no_op_admissible_only_before_dispatch() -> None:
    ledger = ActionLedger()
    action_id = planned_remediation(ledger, change=make_change(), artifact=make_artifact())
    assert no_op_admissible(ledger, action_id) is True
    assert was_ever_dispatched(ledger, action_id) is False

    ledger.mark_attempted(action_id, occurred_at=T1)
    assert no_op_admissible(ledger, action_id) is False
    assert was_ever_dispatched(ledger, action_id) is True


def test_same_final_value_two_histories_two_evidence_types() -> None:
    """History A (already compliant) -> NO_OP. History B (mutate, crash) -> MUTATION.

    Both artifacts end at TOP_RIGHT. Only the recorded history distinguishes them, and
    it must, because claiming a mutation that never happened — or a no-op that hides
    one — would both be false evidence.
    """
    change = make_change()

    # History A: already compliant before this workflow mutated anything.
    ledger_a = ActionLedger()
    action_a = planned_remediation(ledger_a, change=change, artifact=make_artifact())
    assert no_op_admissible(ledger_a, action_a) is True
    evidence_a = NoOpEvidence(
        artifact_id="wi-packing-standard-001",
        evaluated_artifact_ref="fixtures/already_compliant_artifact.json",
        evaluated_artifact_hash=AFTER_HASH,
        observed_value="TOP_RIGHT",
        expected_value="TOP_RIGHT",
        no_op_reason="artifact already represented the approved value",
        compliance_basis="requirements.label_position",
        data_classification=SYNTHETIC,
    )

    # History B: this workflow mutated it, then crashed before persisting completion.
    ledger_b, action_b = crashed_ledger(change, make_artifact())
    evidence_b = reconcile(ledger_b, action_b, observed=mutated()).evidence

    assert isinstance(evidence_a, NoOpEvidence)
    assert isinstance(evidence_b, MutationEvidence)
    assert evidence_a.observed_value == evidence_b.after_value == "TOP_RIGHT"
    assert evidence_a.remediation_type != evidence_b.remediation_type
    assert evidence_b.reconciled is True
    assert no_op_admissible(ledger_b, action_b) is False


# ============================ T036 — delivery reconciliation ==========================


def planned_delivery(ledger: ActionLedger, change: ApprovedChange) -> str:
    action_id = derive_delivery_action_id(
        workflow_id=WF, change=change, worker_id="worker-opaque-01"
    )
    ledger.plan(
        action_id=action_id,
        workflow_id=WF,
        action_type=ActionType.DELIVER_DELTA,
        target_ref="worker-opaque-01",
        intent={"worker_id": "worker-opaque-01", "change_id": change.change_id},
        occurred_at=T0,
    )
    ledger.mark_attempted(action_id, occurred_at=T1)
    return action_id


def test_recoverable_receipt_resolves_delivery_without_resending() -> None:
    ledger = ActionLedger()
    action_id = planned_delivery(ledger, make_change())
    result = reconcile_delivery(
        ledger, action_id, recoverable_receipt_ref="rcpt-abc", occurred_at=T1
    )
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert result.delivered is True
    assert result.receipt_ref == "rcpt-abc"
    assert ledger.require(action_id).status is ActionStatus.COMPLETED
    assert ledger.require(action_id).attempt_count == 1, "no re-send occurred"


def test_absent_receipt_cannot_establish_delivered() -> None:
    ledger = ActionLedger()
    action_id = planned_delivery(ledger, make_change())
    result = reconcile_delivery(ledger, action_id, recoverable_receipt_ref=None, occurred_at=T1)
    assert result.outcome is DeliveryOutcome.UNCERTAIN_NO_RECEIPT
    assert result.delivered is False
    assert result.receipt_ref is None
    assert ledger.require(action_id).status is ActionStatus.FAILED_OR_UNCERTAIN


def test_empty_receipt_string_is_not_a_positive_receipt() -> None:
    ledger = ActionLedger()
    action_id = planned_delivery(ledger, make_change())
    result = reconcile_delivery(ledger, action_id, recoverable_receipt_ref="", occurred_at=T1)
    assert result.delivered is False


def test_already_delivered_action_is_not_delivered_twice() -> None:
    ledger = ActionLedger()
    action_id = planned_delivery(ledger, make_change())
    reconcile_delivery(ledger, action_id, recoverable_receipt_ref="rcpt-abc", occurred_at=T1)
    again = reconcile_delivery(
        ledger, action_id, recoverable_receipt_ref="rcpt-abc", occurred_at=T1
    )
    assert again.outcome is DeliveryOutcome.ALREADY_DELIVERED
    assert again.receipt_ref == "rcpt-abc"


def test_delivery_reconciliation_accepts_no_agent_assertion() -> None:
    """The function signature admits a receipt reference only — never agent text."""
    import inspect

    params = set(inspect.signature(reconcile_delivery).parameters)
    assert params == {"ledger", "action_id", "recoverable_receipt_ref", "occurred_at"}


# ============================ no invented lifecycle states ============================


def test_no_workflow_lifecycle_state_was_invented() -> None:
    assert len(WorkflowState) == 13
    assert {s.value for s in WorkflowState} == {
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


def test_reconciliation_performs_no_workflow_transition() -> None:
    from driftzero.truth_engine import actions

    exported = set(dir(actions))
    for forbidden in ("transition", "WorkflowState", "assert_transition_allowed"):
        assert forbidden not in exported
