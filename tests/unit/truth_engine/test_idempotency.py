"""T052 — Identity and duplicate-absorption acceptance (SC-010, FR-007)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from driftzero.models.action import ActionType
from driftzero.models.change import ApprovedChange
from driftzero.truth_engine.actions import ActionLedger, RetryDecision, decide_retry
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
from driftzero.truth_engine.verification import next_event_sequence

from ._acceptance import (
    ARTIFACT,
    FAIL_EVENT,
    WF,
    WORKER,
    make_change,
)


def test_same_change_delivered_twice_is_one_logical_change() -> None:
    change = make_change()
    first = classify_change_event(change, {})
    assert first.outcome is ChangeEventOutcome.NEW_LOGICAL_CHANGE

    accepted = {change.change_id: WF}
    second = classify_change_event(change, accepted)
    assert second.outcome is ChangeEventOutcome.TRANSPORT_DUPLICATE
    assert second.existing_workflow_id == WF


def test_a_genuinely_different_change_is_not_absorbed() -> None:
    decision = classify_change_event(make_change(change_id="chg-2"), {"chg-1": WF})
    assert decision.outcome is ChangeEventOutcome.NEW_LOGICAL_CHANGE


def test_action_id_is_stable_for_the_same_logical_action() -> None:
    a = derive_remediation_action_id(workflow_id=WF, change=make_change(), artifact_id=ARTIFACT)
    b = derive_remediation_action_id(workflow_id=WF, change=make_change(), artifact_id=ARTIFACT)
    assert a == b


def test_action_id_survives_serialization_round_trip() -> None:
    """A restart rebuilding objects from storage recomputes the same identity."""
    change = make_change()
    rebuilt = ApprovedChange.model_validate(change.model_dump())
    assert derive_remediation_action_id(
        workflow_id=WF, change=change, artifact_id=ARTIFACT
    ) == derive_remediation_action_id(workflow_id=WF, change=rebuilt, artifact_id=ARTIFACT)


def test_action_id_does_not_depend_on_python_hash() -> None:
    """Two fresh interpreters (randomized ``hash()`` seeds) agree exactly."""
    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from driftzero.models.action import ActionType;"
        "from driftzero.truth_engine.idempotency import derive_action_id;"
        "print(derive_action_id(workflow_id='wf-001',"
        " action_type=ActionType.REMEDIATE_ARTIFACT, target_ref='wi-1',"
        " change_id='chg-1', source_version='v2'))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(3)
    }
    assert len(runs) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"workflow_id": "wf-other"},
        {"action_type": ActionType.DELIVER_DELTA},
        {"target_ref": "wi-other"},
        {"change_id": "chg-other"},
        {"source_version": "v9"},
    ],
)
def test_materially_different_actions_never_share_an_id(override: dict[str, object]) -> None:
    base: dict[str, object] = {
        "workflow_id": WF,
        "action_type": ActionType.REMEDIATE_ARTIFACT,
        "target_ref": ARTIFACT,
        "change_id": "chg-1",
        "source_version": "v2",
    }
    assert derive_action_id(**base) != derive_action_id(**{**base, **override})  # type: ignore[arg-type]


def test_the_four_action_types_are_distinct() -> None:
    change = make_change()
    ids = {
        derive_remediation_action_id(workflow_id=WF, change=change, artifact_id=ARTIFACT),
        derive_delivery_action_id(workflow_id=WF, change=change, worker_id=WORKER),
        derive_field_evidence_action_id(workflow_id=WF, submission_id="sub-1"),
        derive_proof_action_id(workflow_id=WF),
    }
    assert len(ids) == 4


def test_duplicate_submission_is_absorbed_and_consumes_no_sequence() -> None:
    existing = [FAIL_EVENT]
    decision = classify_submission(FAIL_EVENT.submission_id, existing)
    assert decision.outcome is SubmissionOutcome.TRANSPORT_DUPLICATE
    assert decision.existing_event_id == FAIL_EVENT.event_id
    assert decision.allocates_event_sequence is False
    assert next_event_sequence(existing, WF) == 2, "sequence unchanged by the duplicate"


def test_new_submission_id_is_a_genuine_new_attempt() -> None:
    decision = classify_submission("sub-99", [FAIL_EVENT])
    assert decision.outcome is SubmissionOutcome.NEW_EVIDENCE_ATTEMPT
    assert decision.allocates_event_sequence is True
    assert decision.existing_event_id is None


def test_retry_resolves_against_the_same_action_and_never_re_executes_completed_work() -> None:
    ledger = ActionLedger()
    action_id = derive_remediation_action_id(
        workflow_id=WF, change=make_change(), artifact_id=ARTIFACT
    )
    ledger.plan(
        action_id=action_id,
        workflow_id=WF,
        action_type=ActionType.REMEDIATE_ARTIFACT,
        target_ref=ARTIFACT,
        intent={"expected_after_value": "TOP_RIGHT"},
        occurred_at=FAIL_EVENT.timestamp,
    )
    assert decide_retry(ledger, action_id) is RetryDecision.SAFE_TO_EXECUTE

    ledger.mark_attempted(action_id, occurred_at=FAIL_EVENT.timestamp)
    assert decide_retry(ledger, action_id) is RetryDecision.RECONCILIATION_REQUIRED

    ledger.mark_completed(action_id, occurred_at=FAIL_EVENT.timestamp)
    assert decide_retry(ledger, action_id) is RetryDecision.ALREADY_COMPLETED
    assert len(ledger.all_records()) == 1, "retries never mint a second action"
