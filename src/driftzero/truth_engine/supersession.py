"""T024 — Source-version applicability and the SUPERSEDED transition (FR-009, SC-015).

An incomplete workflow whose source version has been overtaken by a newer approved
version must stop and never reach ``PROOF_COMPLETE``. The newer version starts a
distinct workflow, which is the caller's concern, not this module's.

Determinism: applicability is decided by comparing fields already present on the
approved-change records — no version-ordering heuristic is invented, no registry is
queried, nothing is fetched, and no LLM participates. The successor relationship is
read from the explicit ``previous_version`` chain that ``ApprovedChange`` already
carries.

No alternate state machine: the SUPERSEDED transition is applied through
``state_machine.transition``, so terminal-state protection is enforced by the one
canonical mechanism rather than duplicated here.
"""

from __future__ import annotations

from datetime import datetime

from driftzero.models.change import ApprovedChange
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.truth_engine.state_machine import is_terminal, transition


def is_applicable_successor(
    current_change: ApprovedChange, incoming_change: ApprovedChange
) -> bool:
    """True when ``incoming_change`` supersedes ``current_change``.

    All four conditions must hold:

    1. same authoritative source procedure;
    2. same operational scope (operation and requirement) — an unrelated
       requirement changing elsewhere does not supersede this workflow;
    3. the incoming change declares ``previous_version`` equal to the version this
       workflow is deployed against, i.e. it is the direct successor in the chain;
    4. it actually advances the version.

    A different logical change on the same version, or a successor to some other
    version, is not applicable.
    """
    return (
        current_change.source_procedure_id == incoming_change.source_procedure_id
        and current_change.operation_id == incoming_change.operation_id
        and current_change.requirement_id == incoming_change.requirement_id
        and incoming_change.previous_version == current_change.source_version
        and incoming_change.source_version != current_change.source_version
    )


def is_supersedable(workflow: Workflow) -> bool:
    """True when the workflow is still eligible to be superseded.

    Only incomplete (non-terminal) workflows are eligible. A workflow already in
    ``PROOF_COMPLETE``, ``SUPERSEDED``, or ``FAILED`` is finished and is never
    re-opened, whatever arrives afterwards.
    """
    return not is_terminal(workflow.state)


def should_supersede(
    workflow: Workflow, current_change: ApprovedChange, incoming_change: ApprovedChange
) -> bool:
    """Deterministic decision: does this incoming change supersede this workflow?

    Requires both an applicable successor version and an eligible workflow. This is
    a predicate only — it applies nothing.
    """
    return is_supersedable(workflow) and is_applicable_successor(current_change, incoming_change)


def supersede(workflow: Workflow, *, occurred_at: datetime) -> Workflow:
    """Return a NEW workflow transitioned to ``SUPERSEDED``.

    Delegates to the canonical transition mechanism, so a terminal workflow raises
    ``IllegalTransitionError`` rather than being silently re-terminated. Evidence
    already recorded on the workflow is carried through untouched — supersession
    stops progress, it does not erase history (spec US9).
    """
    return transition(workflow, WorkflowState.SUPERSEDED, occurred_at=occurred_at)
