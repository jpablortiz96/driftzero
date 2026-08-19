"""T048 — Supersession acceptance (SC-015, FR-009).

A newer approved source version must stop an incomplete older workflow, and nothing
that arrives afterwards may resurrect it.
"""

from __future__ import annotations

import pytest

from driftzero.models.verification import ObservedPosition, VerificationResult
from driftzero.models.workflow import WorkflowState
from driftzero.truth_engine.proof_generator import (
    ProofCondition,
    ProofGenerationError,
    evaluate_proof_invariants,
    generate_change_proof,
)
from driftzero.truth_engine.state_machine import IllegalTransitionError, transition
from driftzero.truth_engine.supersession import (
    is_applicable_successor,
    is_supersedable,
    should_supersede,
    supersede,
)
from driftzero.truth_engine.verification import latest_verification_status

from ._acceptance import (
    PASS_EVENT,
    T_PASS,
    make_change,
    make_event,
    make_proof_context,
    make_workflow,
)

INCOMPLETE_STATES = [
    WorkflowState.CHANGE_RECEIVED,
    WorkflowState.IMPACT_DETERMINED,
    WorkflowState.REMEDIATION_PENDING,
    WorkflowState.REVIEW_REQUIRED,
    WorkflowState.REMEDIATION_COMPLETED,
    WorkflowState.FRONTLINE_DELIVERY_COMPLETED,
    WorkflowState.AWAITING_FIELD_VERIFICATION,
    WorkflowState.VERIFICATION_FAILED,
    WorkflowState.VERIFICATION_INCONCLUSIVE,
    WorkflowState.VERIFICATION_PASSED,
]


@pytest.mark.parametrize("state", INCOMPLETE_STATES)
def test_incomplete_v1_workflow_is_superseded_by_v2(state: WorkflowState) -> None:
    v1_change = make_change(source_version="v1", previous_version="v0")
    v2_change = make_change(change_id="chg-2", source_version="v2", previous_version="v1")
    workflow = make_workflow(state, source_version="v1")

    assert is_applicable_successor(v1_change, v2_change)
    assert should_supersede(workflow, v1_change, v2_change)
    assert supersede(workflow, occurred_at=T_PASS).state is WorkflowState.SUPERSEDED


def test_superseded_workflow_can_never_produce_a_change_proof() -> None:
    """Everything else is valid; SUPERSEDED alone permanently denies completion."""
    context = make_proof_context(state=WorkflowState.SUPERSEDED)
    result = evaluate_proof_invariants(context)
    assert result.eligible is False
    assert ProofCondition.C7_STATE_COMPATIBLE in result.failed_conditions
    with pytest.raises(ProofGenerationError):
        generate_change_proof(context)


def test_supersession_in_history_blocks_even_after_apparent_recovery() -> None:
    context = make_proof_context(history=[WorkflowState.SUPERSEDED])
    assert evaluate_proof_invariants(context).eligible is False


def test_late_pass_event_cannot_resurrect_a_superseded_workflow() -> None:
    """A PASS arriving after supersession changes verification, never eligibility."""
    late_pass = make_event(
        event_id="ev-late",
        sequence=3,
        observation=ObservedPosition.TOP_RIGHT,
        result=VerificationResult.PASS,
    )
    context = make_proof_context(
        state=WorkflowState.SUPERSEDED, events=[PASS_EVENT, late_pass]
    )
    assert latest_verification_status(context.verification_events, "wf-001") is (
        VerificationResult.PASS
    )
    assert evaluate_proof_invariants(context).eligible is False


def test_superseded_is_terminal_with_no_outgoing_transitions() -> None:
    workflow = make_workflow(WorkflowState.SUPERSEDED)
    assert not is_supersedable(workflow)
    for target in WorkflowState:
        with pytest.raises(IllegalTransitionError):
            transition(workflow, target, occurred_at=T_PASS)


def test_superseding_change_must_be_an_applicable_successor() -> None:
    v1 = make_change(source_version="v1", previous_version="v0")
    unrelated = make_change(
        change_id="chg-x", source_version="v2", previous_version="v1",
        operation_id="forklift_navigation",
    )
    assert not is_applicable_successor(v1, unrelated)
    assert not should_supersede(make_workflow(WorkflowState.REMEDIATION_PENDING), v1, unrelated)
