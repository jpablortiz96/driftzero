"""T020-T023 — Canonical workflow transition matrix and legality enforcement.

The Truth Engine owns transition legality. This module answers exactly one
question: **is this state transition structurally legal?**

Authority boundary (deliberate and load-bearing):
    Structural legality is NOT authorization. ``VERIFICATION_PASSED ->
    PROOF_COMPLETE`` is a legal *edge*, but this module never establishes that the
    seven completion invariants hold — that is T043+. ``REMEDIATION_PENDING ->
    REMEDIATION_COMPLETED`` is a legal *edge*, but this module never establishes
    that the nine autonomy conditions were satisfied — that is T028. Nothing here
    derives PASS/FAIL from an observation — that is T038.

    A caller that only consults this module has proven the shape of the workflow,
    not its truth.

The matrix is written out literally, edge by edge, so it is inspectable and
diffable against data-model.md § State Transitions. It is never inferred from
enum ordering or from state names.
"""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType

from driftzero.models.workflow import Workflow, WorkflowState

_S = WorkflowState

TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {_S.PROOF_COMPLETE, _S.SUPERSEDED, _S.FAILED}
)
"""T023 — states with zero outgoing transitions. Entering one ends the workflow."""


LEGAL_TRANSITIONS: MappingProxyType[WorkflowState, frozenset[WorkflowState]] = MappingProxyType(
    {
        # --- progressive path -------------------------------------------------
        _S.CHANGE_RECEIVED: frozenset({_S.IMPACT_DETERMINED, _S.SUPERSEDED, _S.FAILED}),
        _S.IMPACT_DETERMINED: frozenset(
            {_S.REMEDIATION_PENDING, _S.REVIEW_REQUIRED, _S.SUPERSEDED, _S.FAILED}
        ),
        _S.REMEDIATION_PENDING: frozenset(
            {_S.REMEDIATION_COMPLETED, _S.REVIEW_REQUIRED, _S.SUPERSEDED, _S.FAILED}
        ),
        _S.REMEDIATION_COMPLETED: frozenset(
            {_S.FRONTLINE_DELIVERY_COMPLETED, _S.SUPERSEDED, _S.FAILED}
        ),
        _S.FRONTLINE_DELIVERY_COMPLETED: frozenset(
            {_S.AWAITING_FIELD_VERIFICATION, _S.SUPERSEDED, _S.FAILED}
        ),
        _S.AWAITING_FIELD_VERIFICATION: frozenset(
            {
                _S.VERIFICATION_PASSED,
                _S.VERIFICATION_FAILED,
                _S.VERIFICATION_INCONCLUSIVE,
                _S.SUPERSEDED,
                _S.FAILED,
            }
        ),
        _S.VERIFICATION_PASSED: frozenset({_S.PROOF_COMPLETE, _S.SUPERSEDED, _S.FAILED}),
        # --- recoverable non-pass states --------------------------------------
        # Retry with new/clearer evidence. A historical FAIL or INCONCLUSIVE must
        # never permanently poison the workflow (spec US6, condition 7).
        _S.VERIFICATION_FAILED: frozenset(
            {_S.AWAITING_FIELD_VERIFICATION, _S.SUPERSEDED, _S.FAILED}
        ),
        _S.VERIFICATION_INCONCLUSIVE: frozenset(
            {_S.AWAITING_FIELD_VERIFICATION, _S.SUPERSEDED, _S.FAILED}
        ),
        # --- T022: blocking gate, not terminal --------------------------------
        # REVIEW_REQUIRED has NO autonomous exit back to the progressive workflow
        # in S1. Reviewer-resolution is explicitly out of scope, so no resume path
        # exists here. Only supersession or an unrecoverable failure may leave it.
        _S.REVIEW_REQUIRED: frozenset({_S.SUPERSEDED, _S.FAILED}),
        # --- T023: terminal states, provably empty ----------------------------
        _S.PROOF_COMPLETE: frozenset(),
        _S.SUPERSEDED: frozenset(),
        _S.FAILED: frozenset(),
    }
)
"""T020 — the canonical matrix, one entry per canonical state."""


REVIEW_REQUIRED_FORBIDDEN_EXITS: frozenset[WorkflowState] = frozenset(
    {
        _S.REMEDIATION_PENDING,
        _S.REMEDIATION_COMPLETED,
        _S.FRONTLINE_DELIVERY_COMPLETED,
        _S.AWAITING_FIELD_VERIFICATION,
        _S.VERIFICATION_PASSED,
        _S.PROOF_COMPLETE,
    }
)
"""T022 — the six progressive exits explicitly illegal from REVIEW_REQUIRED in S1.

Named for inspectability. They are already absent from ``LEGAL_TRANSITIONS``; this
constant exists so the S1 ruling is auditable rather than implied by omission.
"""


class IllegalTransitionError(Exception):
    """T021 — a structurally illegal state transition was requested.

    Raised instead of returning False so an illegal transition can never be
    silently ignored, coerced into another state, or partially applied.
    """

    def __init__(self, current_state: WorkflowState, requested_state: WorkflowState) -> None:
        self.current_state = current_state
        self.requested_state = requested_state
        if current_state in TERMINAL_STATES:
            reason = f"{current_state} is terminal and has no legal exits"
        elif current_state is _S.REVIEW_REQUIRED and requested_state in (
            REVIEW_REQUIRED_FORBIDDEN_EXITS
        ):
            reason = (
                f"{current_state} has no autonomous exit to the progressive workflow in S1; "
                "only SUPERSEDED or FAILED are legal"
            )
        else:
            allowed = sorted(s.value for s in LEGAL_TRANSITIONS[current_state])
            reason = f"legal targets from {current_state} are {allowed}"
        self.reason = reason
        super().__init__(f"illegal transition {current_state} -> {requested_state}: {reason}")


def legal_targets(current_state: WorkflowState) -> frozenset[WorkflowState]:
    """Return the legal target states for ``current_state``. Empty for terminals."""
    return LEGAL_TRANSITIONS[current_state]


def is_terminal(state: WorkflowState) -> bool:
    """True when the state has zero outgoing transitions (T023)."""
    return state in TERMINAL_STATES


def can_transition(current_state: WorkflowState, requested_state: WorkflowState) -> bool:
    """Pure structural predicate. Does not consider domain authorization."""
    return requested_state in LEGAL_TRANSITIONS[current_state]


def assert_transition_allowed(
    current_state: WorkflowState, requested_state: WorkflowState
) -> None:
    """Raise :class:`IllegalTransitionError` when the transition is illegal."""
    if not can_transition(current_state, requested_state):
        raise IllegalTransitionError(current_state, requested_state)


def transition(
    workflow: Workflow, requested_state: WorkflowState, *, occurred_at: datetime
) -> Workflow:
    """Return a NEW workflow advanced to ``requested_state``.

    The input workflow is never mutated, so a rejected transition provably leaves
    the caller's state untouched. ``occurred_at`` is supplied by the caller rather
    than read from a clock, keeping this function deterministic.

    Structural legality only — see the module docstring on the authority boundary.
    """
    assert_transition_allowed(workflow.state, requested_state)
    return workflow.model_copy(update={"state": requested_state, "updated_at": occurred_at})
