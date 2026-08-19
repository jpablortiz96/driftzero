"""M0-B focused tests for T020-T024 — structural transition legality and supersession.

Scope is the state graph only. The full requirement suite (T047-T059) is not
implemented here, and nothing in this file asserts domain authorization.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftzero.models.change import ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.truth_engine.state_machine import (
    LEGAL_TRANSITIONS,
    REVIEW_REQUIRED_FORBIDDEN_EXITS,
    TERMINAL_STATES,
    IllegalTransitionError,
    can_transition,
    is_terminal,
    legal_targets,
    transition,
)
from driftzero.truth_engine.supersession import (
    is_applicable_successor,
    is_supersedable,
    should_supersede,
    supersede,
)

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
S = WorkflowState


def make_workflow(state: WorkflowState, source_version: str = "v2") -> Workflow:
    return Workflow(
        workflow_id="wf-001",
        change_id="chg-1",
        source_version=source_version,
        state=state,
        worker_id="worker-opaque-01",
        created_at=T0,
        updated_at=T0,
        data_classification=SYNTHETIC,
    )


def make_change(
    *,
    change_id: str = "chg-1",
    source_version: str = "v2",
    previous_version: str = "v1",
    procedure: str = "proc-warehouse-packing",
    operation: str = "packing_label_placement",
    requirement: str = "label_position",
) -> ApprovedChange:
    return ApprovedChange(
        change_id=change_id,
        source_procedure_id=procedure,
        source_version=source_version,
        previous_version=previous_version,
        operation_id=operation,
        requirement_id=requirement,
        previous_value="LEFT",
        current_value="TOP_RIGHT",
        authorized_scope=["wi-packing-standard-001"],
        approved_status="APPROVED",
        source_evidence_ref="fixtures/source_procedure_v2.json",
        received_at=T0,
        data_classification=SYNTHETIC,
    )


# --- 1/2. matrix keys and targets are canonical states --------------------------------


def test_matrix_covers_every_canonical_state_exactly_once() -> None:
    assert set(LEGAL_TRANSITIONS) == set(WorkflowState)
    assert len(LEGAL_TRANSITIONS) == 13


def test_every_transition_target_is_a_canonical_state() -> None:
    for source, targets in LEGAL_TRANSITIONS.items():
        for target in targets:
            assert isinstance(target, WorkflowState), f"{source} -> {target!r} is not canonical"
            assert target in set(WorkflowState)


def test_no_self_transitions() -> None:
    for source, targets in LEGAL_TRANSITIONS.items():
        assert source not in targets, f"{source} may not transition to itself"


# --- 3. representative valid progressive transitions ----------------------------------


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (S.CHANGE_RECEIVED, S.IMPACT_DETERMINED),
        (S.IMPACT_DETERMINED, S.REMEDIATION_PENDING),
        (S.IMPACT_DETERMINED, S.REVIEW_REQUIRED),
        (S.REMEDIATION_PENDING, S.REMEDIATION_COMPLETED),
        (S.REMEDIATION_COMPLETED, S.FRONTLINE_DELIVERY_COMPLETED),
        (S.FRONTLINE_DELIVERY_COMPLETED, S.AWAITING_FIELD_VERIFICATION),
        (S.AWAITING_FIELD_VERIFICATION, S.VERIFICATION_PASSED),
        (S.VERIFICATION_PASSED, S.PROOF_COMPLETE),
    ],
)
def test_valid_progressive_transitions_succeed(
    current: WorkflowState, requested: WorkflowState
) -> None:
    assert can_transition(current, requested)
    advanced = transition(make_workflow(current), requested, occurred_at=T1)
    assert advanced.state is requested
    assert advanced.updated_at == T1


# --- 4/5. illegal transitions raise, and preserve original state ----------------------


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (S.CHANGE_RECEIVED, S.PROOF_COMPLETE),
        (S.CHANGE_RECEIVED, S.REMEDIATION_COMPLETED),
        (S.IMPACT_DETERMINED, S.AWAITING_FIELD_VERIFICATION),
        (S.REMEDIATION_PENDING, S.PROOF_COMPLETE),
        (S.AWAITING_FIELD_VERIFICATION, S.PROOF_COMPLETE),
        (S.VERIFICATION_FAILED, S.VERIFICATION_PASSED),
        (S.VERIFICATION_INCONCLUSIVE, S.PROOF_COMPLETE),
    ],
)
def test_illegal_transitions_raise_domain_error(
    current: WorkflowState, requested: WorkflowState
) -> None:
    with pytest.raises(IllegalTransitionError) as exc:
        transition(make_workflow(current), requested, occurred_at=T1)
    assert exc.value.current_state is current
    assert exc.value.requested_state is requested
    assert str(exc.value)


def test_illegal_transition_leaves_original_workflow_untouched() -> None:
    wf = make_workflow(S.AWAITING_FIELD_VERIFICATION)
    with pytest.raises(IllegalTransitionError):
        transition(wf, S.PROOF_COMPLETE, occurred_at=T1)
    assert wf.state is S.AWAITING_FIELD_VERIFICATION
    assert wf.updated_at == T0


# --- 6/7/8. terminal states have zero exits -------------------------------------------


@pytest.mark.parametrize("terminal", [S.PROOF_COMPLETE, S.SUPERSEDED, S.FAILED])
def test_terminal_states_have_zero_outgoing_transitions(terminal: WorkflowState) -> None:
    assert legal_targets(terminal) == frozenset()
    assert is_terminal(terminal)
    assert terminal in TERMINAL_STATES


@pytest.mark.parametrize("terminal", [S.PROOF_COMPLETE, S.SUPERSEDED, S.FAILED])
def test_no_transition_out_of_a_terminal_state_is_possible(terminal: WorkflowState) -> None:
    for target in WorkflowState:
        assert not can_transition(terminal, target)
        with pytest.raises(IllegalTransitionError):
            transition(make_workflow(terminal), target, occurred_at=T1)


# --- 9/10/11/12. REVIEW_REQUIRED exit set ---------------------------------------------


def test_review_required_is_blocking_but_not_terminal() -> None:
    assert not is_terminal(S.REVIEW_REQUIRED)
    assert legal_targets(S.REVIEW_REQUIRED) == frozenset({S.SUPERSEDED, S.FAILED})


@pytest.mark.parametrize("requested", [S.SUPERSEDED, S.FAILED])
def test_review_required_legal_exits(requested: WorkflowState) -> None:
    advanced = transition(make_workflow(S.REVIEW_REQUIRED), requested, occurred_at=T1)
    assert advanced.state is requested


@pytest.mark.parametrize("requested", sorted(REVIEW_REQUIRED_FORBIDDEN_EXITS))
def test_review_required_progressive_exits_are_illegal_in_s1(requested: WorkflowState) -> None:
    assert not can_transition(S.REVIEW_REQUIRED, requested)
    with pytest.raises(IllegalTransitionError):
        transition(make_workflow(S.REVIEW_REQUIRED), requested, occurred_at=T1)


def test_no_human_review_resume_path_exists() -> None:
    """S1 has no reviewer-resolution capability, so no progressive exit may exist."""
    assert legal_targets(S.REVIEW_REQUIRED).isdisjoint(REVIEW_REQUIRED_FORBIDDEN_EXITS)


# --- 13/14/15. verification recovery --------------------------------------------------


def test_verification_failed_can_retry() -> None:
    assert can_transition(S.VERIFICATION_FAILED, S.AWAITING_FIELD_VERIFICATION)


def test_verification_inconclusive_can_retry() -> None:
    assert can_transition(S.VERIFICATION_INCONCLUSIVE, S.AWAITING_FIELD_VERIFICATION)


def test_fail_then_corrected_pass_path_is_structurally_possible() -> None:
    """AWAITING -> FAILED_VERIFICATION -> AWAITING -> PASSED -> PROOF_COMPLETE.

    Structural only: this proves a historical FAIL does not poison the graph. It
    does NOT assert the proof invariants, which belong to T043+.
    """
    wf = make_workflow(S.AWAITING_FIELD_VERIFICATION)
    for nxt in (
        S.VERIFICATION_FAILED,
        S.AWAITING_FIELD_VERIFICATION,
        S.VERIFICATION_PASSED,
        S.PROOF_COMPLETE,
    ):
        wf = transition(wf, nxt, occurred_at=T1)
    assert wf.state is S.PROOF_COMPLETE


def test_inconclusive_then_recovery_path_is_structurally_possible() -> None:
    wf = make_workflow(S.AWAITING_FIELD_VERIFICATION)
    for nxt in (S.VERIFICATION_INCONCLUSIVE, S.AWAITING_FIELD_VERIFICATION, S.VERIFICATION_PASSED):
        wf = transition(wf, nxt, occurred_at=T1)
    assert wf.state is S.VERIFICATION_PASSED


# --- 16/17/18/19. supersession ---------------------------------------------------------


NON_TERMINAL = [s for s in WorkflowState if s not in TERMINAL_STATES]


@pytest.mark.parametrize("state", NON_TERMINAL)
def test_incomplete_workflow_can_be_superseded(state: WorkflowState) -> None:
    wf = make_workflow(state)
    assert is_supersedable(wf)
    assert supersede(wf, occurred_at=T1).state is S.SUPERSEDED


@pytest.mark.parametrize("terminal", [S.PROOF_COMPLETE, S.SUPERSEDED, S.FAILED])
def test_terminal_workflows_cannot_be_superseded(terminal: WorkflowState) -> None:
    wf = make_workflow(terminal)
    assert not is_supersedable(wf)
    with pytest.raises(IllegalTransitionError) as exc:
        supersede(wf, occurred_at=T1)
    assert exc.value.current_state is terminal
    assert exc.value.requested_state is S.SUPERSEDED
    assert wf.state is terminal


def test_supersession_preserves_recorded_evidence() -> None:
    wf = make_workflow(S.AWAITING_FIELD_VERIFICATION).model_copy(
        update={"candidate_artifact_refs": ["wi-1"], "delivery_status": "DELIVERED"}
    )
    superseded = supersede(wf, occurred_at=T1)
    assert superseded.candidate_artifact_refs == ["wi-1"]
    assert superseded.delivery_status == "DELIVERED"


def test_applicable_successor_is_the_direct_version_chain() -> None:
    current = make_change(source_version="v2", previous_version="v1")
    successor = make_change(change_id="chg-2", source_version="v3", previous_version="v2")
    assert is_applicable_successor(current, successor)


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"previous_version": "v1"}, "successor to an older version, not to v2"),
        ({"source_version": "v2", "previous_version": "v2"}, "does not advance the version"),
        ({"procedure": "proc-other"}, "different source procedure"),
        ({"operation": "forklift_navigation"}, "different operation scope"),
        ({"requirement": "turn_direction"}, "different requirement scope"),
    ],
)
def test_non_applicable_changes_do_not_supersede(kwargs: dict[str, str], why: str) -> None:
    current = make_change(source_version="v2", previous_version="v1")
    defaults: dict[str, str] = {
        "change_id": "chg-2",
        "source_version": "v3",
        "previous_version": "v2",
    }
    incoming = make_change(**{**defaults, **kwargs})
    assert not is_applicable_successor(current, incoming), why


def test_should_supersede_requires_both_applicability_and_eligibility() -> None:
    current = make_change(source_version="v2", previous_version="v1")
    successor = make_change(change_id="chg-2", source_version="v3", previous_version="v2")
    assert should_supersede(make_workflow(S.REMEDIATION_PENDING), current, successor)
    assert not should_supersede(make_workflow(S.PROOF_COMPLETE), current, successor)
    assert not should_supersede(make_workflow(S.REMEDIATION_PENDING), current, current)


# --- authority boundary ----------------------------------------------------------------


def test_module_does_not_expose_domain_authorization_helpers() -> None:
    """Legality is not authorization: proof/autonomy/comparator logic lives elsewhere."""
    from driftzero.truth_engine import state_machine

    exported = set(dir(state_machine))
    for forbidden in (
        "evaluate_proof_invariants",
        "check_autonomy_conditions",
        "compare_observation",
        "qualify_affected_artifact",
    ):
        assert forbidden not in exported
