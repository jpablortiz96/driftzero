"""T080 — the workflow boundary sequence. **Step 10 only** in this slice.

The full task (contracts/agents.md § Orchestration) is an eleven-step sequence:

===  ==========================================================  ==================
 1.  Truth Engine: validate incoming change, idempotency, supersession   not built
 2.  Change Intelligence Agent: extract ChangeSet                        not built
 3.  Truth Engine: validate ChangeSet, autonomy preconditions            not built
 4.  Remediation Agent: apply authorized patch                           T074/T073
 5.  Truth Engine: validate remediation result, record evidence          T076
 6.  Frontline Enablement Agent: compose and deliver delta               T077/T078
 7.  Truth Engine: validate delivery evidence                            T078
 8.  [ASYNC PAUSE — await field verification evidence]                  **here**
 9.  Field Verification Agent: process evidence image                    T079
10.  Truth Engine: deterministic PASS/FAIL, completion invariants        **here**
11.  Truth Engine: generate Change Proof if all conditions met           not built
===  ==========================================================  ==================

This module implements the pause boundary and step 10. Step 11 and the ADK
``SequentialAgent`` that would drive all eleven are deliberately absent, so **T080 stays
open**. Claiming it complete because its most visible step works would be exactly the
kind of overstatement the rest of this system exists to prevent.

Nothing here is an agent
------------------------
Despite living in ``agents/`` — the location T080 names — this module contains no agent,
no model call, no prompt, and no capability. Step 10 is a *Truth Engine* step. What it
adds over M0 is sequencing and binding, never judgement.

Authority
---------
The verdict comes from the frozen T038 comparator, reached through the frozen T037
ingestion path. This module does not compare anything itself: it establishes that the
inputs are authoritative, then hands them to code it cannot influence.

* **Expected** is read from the approved change. Never from a caller, a request, or an
  agent.
* **Observed** is read from a Crossing-4-*accepted* observation. An observation that was
  merely produced — not validated — is refused before the comparator is reached.

A hand-built ``FieldObservation`` therefore cannot reach the comparator at all: the input
to this module is the *boundary result*, and only Crossing 4 can produce an accepted one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from driftzero.field.evidence import FieldEvidenceStore
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import DataClassification
from driftzero.models.verification import (
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.models.workflow import StateCategory, Workflow, WorkflowState
from driftzero.orchestration import ObservationBoundaryResult
from driftzero.truth_engine.state_machine import can_transition, transition
from driftzero.truth_engine.verification import (
    IngestOutcome,
    ingest_observation,
    latest_authoritative_event,
)

VERDICT_STATE: dict[VerificationResult, WorkflowState] = {
    VerificationResult.PASS: WorkflowState.VERIFICATION_PASSED,
    VerificationResult.FAIL: WorkflowState.VERIFICATION_FAILED,
    VerificationResult.INCONCLUSIVE: WorkflowState.VERIFICATION_INCONCLUSIVE,
}
"""The frozen result→state mapping. One entry per comparator outcome, no defaults."""


class VerdictStatus(StrEnum):
    """How one adjudication attempt ended."""

    ADJUDICATED = "ADJUDICATED"
    """A new authoritative verification event was recorded."""
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    """Already adjudicated. The existing event was returned; no second attempt exists."""
    OBSERVATION_NOT_VALIDATED = "OBSERVATION_NOT_VALIDATED"
    """Crossing 4 did not accept the observation, so it may not be adjudicated."""
    NOT_AWAITING_VERIFICATION = "NOT_AWAITING_VERIFICATION"
    EVIDENCE_NOT_RESOLVABLE = "EVIDENCE_NOT_RESOLVABLE"
    EVIDENCE_CONTEXT_MISMATCH = "EVIDENCE_CONTEXT_MISMATCH"


@dataclass(frozen=True)
class VerdictContext:
    """The narrowest authoritative context step 10 needs.

    Deliberately holds no expected value, no observation, and no verdict. The expected
    value is derived from ``change``; the observation is read out of ``boundary``, which
    only Crossing 4 can populate. A caller can supply the *inputs* to adjudication and
    never the answer.
    """

    workflow: Workflow
    change: ApprovedChange
    boundary: ObservationBoundaryResult
    store: FieldEvidenceStore
    event_id: str
    occurred_at: datetime
    data_classification: DataClassification
    existing_events: tuple[VerificationEvent, ...] = ()


@dataclass(frozen=True)
class VerdictOutcome:
    """The result of one adjudication.

    ``change_deployed`` and ``proof_generated`` are fixed False and are *not* fields a
    caller may set. A PASS is a passed verification, not a completed deployment: the
    frozen contract reserves that for ``PROOF_COMPLETE``, which requires all seven proof
    invariants (T043) and step 11, neither of which this slice builds.
    """

    status: VerdictStatus
    result: VerificationResult | None = None
    event: VerificationEvent | None = None
    workflow: Workflow | None = None
    expected_value: str | None = None
    observed_value: str | None = None
    rejection_reason: str | None = None

    @property
    def adjudicated(self) -> bool:
        return self.status in (
            VerdictStatus.ADJUDICATED,
            VerdictStatus.DUPLICATE_SUBMISSION,
        )

    @property
    def duplicate(self) -> bool:
        return self.status is VerdictStatus.DUPLICATE_SUBMISSION

    @property
    def passed(self) -> bool:
        """Verification passed. Says nothing about deployment or proof."""
        return self.result is VerificationResult.PASS

    @property
    def change_deployed(self) -> bool:
        """Derived from the frozen state table, never asserted.

        ``PROOF_COMPLETE`` is the only ``TERMINAL_SUCCESS`` state in the canonical
        13-state model, so it is the only state that may be described as deployed.
        """
        return change_is_deployed(self.workflow)

    @property
    def proof_generated(self) -> bool:
        return bool(self.workflow and self.workflow.proof_id)


def change_is_deployed(workflow: Workflow | None) -> bool:
    """Whether the change is deployed, per the frozen state categories.

    Read from ``STATE_CATEGORY`` rather than compared against a state name, so this
    cannot drift from the model if the categorisation ever changes.
    """
    if workflow is None:
        return False
    return workflow.state_category is StateCategory.TERMINAL_SUCCESS


def remaining_condition_for(workflow: Workflow | None) -> str | None:
    """What still stands between the current state and deployment. Honest, not vague."""
    if workflow is None:
        return "no workflow has been adjudicated yet"
    if change_is_deployed(workflow):
        return None
    if workflow.state is WorkflowState.VERIFICATION_PASSED:
        return (
            "Change Proof generation (the seven PROOF_COMPLETE invariants, T043/T044) "
            "is not wired; VERIFICATION_PASSED -> PROOF_COMPLETE has not been taken"
        )
    if workflow.state is WorkflowState.VERIFICATION_FAILED:
        return "the latest authoritative verification FAILED; new field evidence is required"
    if workflow.state is WorkflowState.VERIFICATION_INCONCLUSIVE:
        return (
            "the latest authoritative verification was INCONCLUSIVE; clearer field "
            "evidence is required"
        )
    return f"the workflow is in {workflow.state}, before field verification"


def authoritative_expected_value(change: ApprovedChange) -> str:
    """The expected value, read from approved change state and nowhere else.

    Named and exported so every call site reaches the expected value through one
    function, and so a test can prove no other source is consulted.
    """
    return change.current_value


def adjudicate_field_verification(context: VerdictContext) -> VerdictOutcome:
    """Step 10 — derive the authoritative verdict for one validated observation.

    Fails closed at four gates before any comparison happens: the observation must have
    been accepted at Crossing 4, the workflow must actually be awaiting verification, the
    provider evidence must still resolve, and that evidence must belong to this change
    and source version.

    Only then does it call the frozen ingestion path, which applies the frozen T038
    comparator. This function computes no verdict of its own — there is no ``==``
    between an expected and an observed value anywhere in it.
    """
    boundary = context.boundary
    workflow = context.workflow

    if not boundary.accepted or boundary.accepted_observation is None:
        return VerdictOutcome(
            status=VerdictStatus.OBSERVATION_NOT_VALIDATED,
            rejection_reason=(
                "the observation was not accepted at Crossing 4, so it may not be "
                "adjudicated: " + (boundary.rejection_reason or "not validated")
            ),
        )

    if workflow.state is not WorkflowState.AWAITING_FIELD_VERIFICATION:
        return VerdictOutcome(
            status=VerdictStatus.NOT_AWAITING_VERIFICATION,
            workflow=workflow,
            rejection_reason=(
                f"workflow {workflow.workflow_id} is in {workflow.state}, not "
                f"{WorkflowState.AWAITING_FIELD_VERIFICATION}"
            ),
        )

    observation = boundary.accepted_observation
    record = context.store.resolve(observation.raw_evidence_ref)
    if record is None:
        return VerdictOutcome(
            status=VerdictStatus.EVIDENCE_NOT_RESOLVABLE,
            workflow=workflow,
            rejection_reason=(
                f"{observation.raw_evidence_ref} no longer resolves to field evidence"
            ),
        )

    mismatches = _context_mismatches(record, workflow=workflow, change=context.change)
    if mismatches:
        return VerdictOutcome(
            status=VerdictStatus.EVIDENCE_CONTEXT_MISMATCH,
            workflow=workflow,
            rejection_reason="field evidence does not belong to this change: "
            + ", ".join(mismatches),
        )

    expected = authoritative_expected_value(context.change)
    ingest = ingest_observation(
        observation,
        workflow_id=workflow.workflow_id,
        expected_value=expected,
        existing_events=context.existing_events or workflow.verification_events,
        event_id=context.event_id,
        timestamp=context.occurred_at,
        data_classification=context.data_classification,
    )

    if ingest.outcome is IngestOutcome.TRANSPORT_DUPLICATE:
        # Already adjudicated. Returning the existing event is the whole point: a
        # resubmission must not allocate a sequence or create a second attempt.
        return VerdictOutcome(
            status=VerdictStatus.DUPLICATE_SUBMISSION,
            result=ingest.event.verification_result,
            event=ingest.event,
            workflow=workflow,
            expected_value=ingest.event.expected_value,
            observed_value=str(ingest.event.derived_observation),
        )

    event = ingest.event
    target = VERDICT_STATE[event.verification_result]
    advanced = transition(workflow, target, occurred_at=context.occurred_at)
    advanced = advanced.model_copy(
        update={
            # Append-only: every attempt is retained, FAIL and INCONCLUSIVE included.
            "verification_events": [*workflow.verification_events, event],
            "latest_verification_status": event.verification_result,
            "event_sequence": event.event_sequence,
        }
    )
    return VerdictOutcome(
        status=VerdictStatus.ADJUDICATED,
        result=event.verification_result,
        event=event,
        workflow=advanced,
        expected_value=event.expected_value,
        observed_value=str(event.derived_observation),
    )


def _context_mismatches(
    record: dict[str, Any], *, workflow: Workflow, change: ApprovedChange
) -> list[str]:
    """Bind the stored provider evidence to this change and version."""
    mismatches: list[str] = []
    if record.get("change_id") != change.change_id:
        mismatches.append(
            f"evidence change {record.get('change_id')!r} != {change.change_id!r}"
        )
    if record.get("source_version") != change.source_version:
        mismatches.append(
            f"evidence version {record.get('source_version')!r} != "
            f"{change.source_version!r}"
        )
    if workflow.change_id != change.change_id:
        mismatches.append("workflow is not tracking this change")
    if workflow.source_version != change.source_version:
        mismatches.append("workflow is bound to a different source version")
    return mismatches


def reopen_for_new_evidence(
    workflow: Workflow, *, occurred_at: datetime
) -> Workflow:
    """Return to ``AWAITING_FIELD_VERIFICATION`` after a non-passing verdict.

    Uses the frozen transition table, which permits this only from
    ``VERIFICATION_FAILED`` and ``VERIFICATION_INCONCLUSIVE``. A historical FAIL must
    never permanently poison a workflow (spec US6), and the failed event stays in
    ``verification_events`` — reopening adds an attempt, it never erases one.
    """
    if not can_transition(workflow.state, WorkflowState.AWAITING_FIELD_VERIFICATION):
        return workflow
    return transition(
        workflow, WorkflowState.AWAITING_FIELD_VERIFICATION, occurred_at=occurred_at
    )


def verification_history(events: Iterable[VerificationEvent]) -> tuple[dict[str, Any], ...]:
    """Every attempt, oldest first, projected for display. Adds no judgement."""
    return tuple(
        {
            "event_id": event.event_id,
            "submission_id": event.submission_id,
            "event_sequence": event.event_sequence,
            "observed": str(event.derived_observation),
            "expected": event.expected_value,
            "result": str(event.verification_result),
            "raw_evidence_ref": event.raw_evidence_ref,
            "timestamp": event.timestamp.isoformat(),
        }
        for event in sorted(events, key=lambda e: e.event_sequence)
    )


def authoritative_result(
    events: Iterable[VerificationEvent], workflow_id: str
) -> VerificationResult | None:
    """The current authoritative verdict, via the frozen chronology rule (T037)."""
    latest = latest_authoritative_event(events, workflow_id)
    return None if latest is None else latest.verification_result


__all__ = [
    "VERDICT_STATE",
    "ObservedPosition",
    "VerdictContext",
    "VerdictOutcome",
    "VerdictStatus",
    "adjudicate_field_verification",
    "authoritative_expected_value",
    "authoritative_result",
    "change_is_deployed",
    "remaining_condition_for",
    "reopen_for_new_evidence",
    "verification_history",
]
