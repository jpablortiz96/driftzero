"""T032-T036 — Action ledger, retry deduplication, and crash reconciliation.

FR-004, FR-007, FR-008, FR-011.

The ledger records one entry per stable ``action_id`` so a retry operates on the
same logical action rather than minting a new one. It is **not** workflow lifecycle
state: ``ActionStatus`` and ``WorkflowState`` are disjoint enums, and nothing here
transitions a workflow.

The central invariant is the crash case. When a mutation succeeded externally but its
completion was never persisted, recovery may reconcile that *same* logical action as
completed — and the result stays ``MUTATION`` with ``reconciled=True``. It never
becomes ``NO_OP``. ``NO_OP`` is reserved for an artifact that was already compliant
*before this workflow performed any mutation*, i.e. no ``REMEDIATE_ARTIFACT`` action
for it ever reached ``ATTEMPTED``. Two histories can end at the same physical value
and must still yield different evidence.

Ownership is never inferred from the observed value alone. Matching the expected
after-state is necessary but not sufficient; without stored pre-action intent proving
*this* action planned *this* mutation, and without authorization and source-version
invariants still holding, the engine fails closed.

Storage boundary: the ledger interface here is a minimal in-memory implementation for
deterministic evaluation. No datastore, queue, lock, or transaction framework is
selected — durable persistence is M2's concern.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from driftzero.models.action import ActionExecution, ActionStatus, ActionType
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import DataClassification
from driftzero.models.remediation import MutationEvidence

# ============================ T032 — the ledger =======================================


class DuplicateActionError(Exception):
    """A second ledger record was attempted for an existing ``action_id``."""

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        super().__init__(f"an ActionExecution already exists for action_id {action_id}")


class UnknownActionError(Exception):
    """An operation referenced an ``action_id`` with no ledger record."""

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        super().__init__(f"no ActionExecution recorded for action_id {action_id}")


class ActionLedger:
    """In-memory ledger holding exactly one record per ``action_id``.

    Deliberately minimal: dict-backed, no I/O, no transactions. M2 replaces the
    backing store without changing these semantics.
    """

    def __init__(self) -> None:
        self._records: dict[str, ActionExecution] = {}

    def get(self, action_id: str) -> ActionExecution | None:
        return self._records.get(action_id)

    def require(self, action_id: str) -> ActionExecution:
        record = self._records.get(action_id)
        if record is None:
            raise UnknownActionError(action_id)
        return record

    def all_records(self) -> tuple[ActionExecution, ...]:
        return tuple(self._records.values())

    def plan(
        self,
        *,
        action_id: str,
        workflow_id: str,
        action_type: ActionType,
        target_ref: str,
        intent: Mapping[str, object],
        occurred_at: datetime,
    ) -> ActionExecution:
        """Persist pre-dispatch intent as ``PLANNED`` before any side effect runs.

        Recording intent *before* dispatch is what later makes reconciliation possible:
        without it, a matching after-state proves nothing about ownership.
        """
        if action_id in self._records:
            raise DuplicateActionError(action_id)
        record = ActionExecution(
            action_id=action_id,
            workflow_id=workflow_id,
            action_type=action_type,
            status=ActionStatus.PLANNED,
            target_ref=target_ref,
            intent=dict(intent),
            attempt_count=0,
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        self._records[action_id] = record
        return record

    def mark_attempted(self, action_id: str, *, occurred_at: datetime) -> ActionExecution:
        """Dispatch happened; the outcome is not yet confirmed."""
        record = self.require(action_id)
        updated = record.model_copy(
            update={
                "status": ActionStatus.ATTEMPTED,
                "attempt_count": record.attempt_count + 1,
                "updated_at": occurred_at,
            }
        )
        self._records[action_id] = updated
        return updated

    def mark_completed(
        self,
        action_id: str,
        *,
        occurred_at: datetime,
        receipt_ref: str | None = None,
        outcome_evidence_ref: str | None = None,
        reconciled: bool = False,
    ) -> ActionExecution:
        """Outcome confirmed — by an observed response, or by validated reconciliation."""
        record = self.require(action_id)
        updated = record.model_copy(
            update={
                "status": ActionStatus.COMPLETED,
                "receipt_ref": receipt_ref if receipt_ref is not None else record.receipt_ref,
                "outcome_evidence_ref": (
                    outcome_evidence_ref
                    if outcome_evidence_ref is not None
                    else record.outcome_evidence_ref
                ),
                "reconciled": reconciled,
                "updated_at": occurred_at,
            }
        )
        self._records[action_id] = updated
        return updated

    def mark_failed_or_uncertain(
        self, action_id: str, *, occurred_at: datetime
    ) -> ActionExecution:
        """The attempt failed, or its outcome could not be established."""
        record = self.require(action_id)
        updated = record.model_copy(
            update={"status": ActionStatus.FAILED_OR_UNCERTAIN, "updated_at": occurred_at}
        )
        self._records[action_id] = updated
        return updated


# ============================ T033 — retry deduplication ==============================


class RetryDecision(StrEnum):
    """What a caller may do with a logical action it is about to (re-)run."""

    SAFE_TO_EXECUTE = "SAFE_TO_EXECUTE"
    """No record, or intent recorded but never dispatched."""
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    """Completed logical actions are never re-executed."""
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    """Dispatched with an unconfirmed outcome — the side effect may have taken effect."""


def decide_retry(ledger: ActionLedger, action_id: str) -> RetryDecision:
    """T033 — deduplicate retries against the ledger, keyed on the stable identity.

    A restart, a lost response, or an orchestration retry never mints a new action:
    the caller recomputes the same ``action_id`` and asks again here.

    ``ATTEMPTED`` and ``FAILED_OR_UNCERTAIN`` both mean the side effect may already
    have happened, so neither permits a blind re-run.
    """
    record = ledger.get(action_id)
    if record is None:
        return RetryDecision.SAFE_TO_EXECUTE
    if record.status is ActionStatus.COMPLETED:
        return RetryDecision.ALREADY_COMPLETED
    if record.status is ActionStatus.PLANNED:
        return RetryDecision.SAFE_TO_EXECUTE
    return RetryDecision.RECONCILIATION_REQUIRED


# ============================ T034/T035 — mutation reconciliation ======================


class ReconciliationOutcome(StrEnum):
    """Result of attempting to reconcile an unconfirmed mutation."""

    RECONCILED_MUTATION = "RECONCILED_MUTATION"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    FAIL_CLOSED = "FAIL_CLOSED"


class ReconciliationBlocker(StrEnum):
    """Deterministic reason codes for a refused reconciliation."""

    MISSING_PRE_ACTION_INTENT = "MISSING_PRE_ACTION_INTENT"
    ACTION_IDENTITY_MISMATCH = "ACTION_IDENTITY_MISMATCH"
    TARGET_NOT_IN_INTENDED_AFTER_STATE = "TARGET_NOT_IN_INTENDED_AFTER_STATE"
    SOURCE_VERSION_NOT_APPLICABLE = "SOURCE_VERSION_NOT_APPLICABLE"
    AUTHORIZATION_NO_LONGER_VALID = "AUTHORIZATION_NO_LONGER_VALID"
    INTENT_TARGET_MISMATCH = "INTENT_TARGET_MISMATCH"
    NEVER_DISPATCHED = "NEVER_DISPATCHED"


@dataclass(frozen=True)
class ReconciliationResult:
    """Verdict plus the evidence it does or does not support."""

    outcome: ReconciliationOutcome
    action_id: str
    evidence: MutationEvidence | None
    """Only ever ``MutationEvidence``. Reconciliation can never emit ``NoOpEvidence``."""
    blockers: tuple[ReconciliationBlocker, ...]
    requires_review: bool
    """True when the engine failed closed — the workflow's path leads to REVIEW_REQUIRED."""


REQUIRED_INTENT_KEYS = (
    "artifact_id",
    "expected_before_ref",
    "expected_before_hash",
    "expected_before_value",
    "expected_after_value",
    "change_id",
    "source_version",
)
"""Pre-action intent fields required before any reconciliation may be considered."""


def build_remediation_intent(
    *, change: ApprovedChange, artifact: DownstreamArtifact, expected_before_hash: str
) -> dict[str, object]:
    """Assemble the pre-dispatch intent for a ``REMEDIATE_ARTIFACT`` action."""
    return {
        "artifact_id": artifact.artifact_id,
        "expected_before_ref": artifact.content_ref,
        "expected_before_hash": expected_before_hash,
        "expected_before_value": change.previous_value,
        "expected_after_value": change.current_value,
        "change_id": change.change_id,
        "source_version": change.source_version,
    }


def reconcile_mutation(
    ledger: ActionLedger,
    action_id: str,
    *,
    observed_artifact: DownstreamArtifact,
    observed_after_hash: str,
    after_ref: str,
    change: ApprovedChange,
    source_version_applicable: bool,
    occurred_at: datetime,
    data_classification: DataClassification,
) -> ReconciliationResult:
    """T034/T035 — reconcile a possibly-completed mutation, or fail closed.

    All four approved conditions must hold:

    1. the action is not already recorded ``COMPLETED``;
    2. the target artifact is already exactly in the intended after-state;
    3. stored pre-action intent proves **this** workflow/action planned that mutation;
    4. authorization and source-version invariants still hold.

    Matching the after-state alone never suffices — condition 3 is what distinguishes
    "this workflow did it" from "someone or something else did". Every refusal returns
    reason codes and fabricates no evidence.
    """
    record = ledger.get(action_id)
    blockers: list[ReconciliationBlocker] = []

    if record is None:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.FAIL_CLOSED,
            action_id=action_id,
            evidence=None,
            blockers=(ReconciliationBlocker.MISSING_PRE_ACTION_INTENT,),
            requires_review=True,
        )

    # Condition 1 — already authoritatively completed: nothing to reconcile.
    if record.status is ActionStatus.COMPLETED:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.ALREADY_COMPLETED,
            action_id=action_id,
            evidence=None,
            blockers=(),
            requires_review=False,
        )

    # A PLANNED action was never dispatched, so no external effect can be attributed
    # to it. Reconciling here would be inventing history.
    if record.status is ActionStatus.PLANNED:
        blockers.append(ReconciliationBlocker.NEVER_DISPATCHED)

    # Condition 3 — stored pre-action intent must exist and identify this mutation.
    intent = record.intent
    missing = [key for key in REQUIRED_INTENT_KEYS if not intent.get(key)]
    if missing:
        blockers.append(ReconciliationBlocker.MISSING_PRE_ACTION_INTENT)
    else:
        if record.action_type is not ActionType.REMEDIATE_ARTIFACT:
            blockers.append(ReconciliationBlocker.ACTION_IDENTITY_MISMATCH)
        if (
            intent["artifact_id"] != observed_artifact.artifact_id
            or record.target_ref != observed_artifact.artifact_id
        ):
            blockers.append(ReconciliationBlocker.INTENT_TARGET_MISMATCH)
        if (
            intent["change_id"] != change.change_id
            or intent["source_version"] != change.source_version
        ):
            blockers.append(ReconciliationBlocker.ACTION_IDENTITY_MISMATCH)
        # Condition 2 — target already exactly in the intended after-state.
        if observed_artifact.current_value != intent["expected_after_value"]:
            blockers.append(ReconciliationBlocker.TARGET_NOT_IN_INTENDED_AFTER_STATE)

    # Condition 4 — authorization and source-version invariants still hold.
    if not source_version_applicable:
        blockers.append(ReconciliationBlocker.SOURCE_VERSION_NOT_APPLICABLE)
    if not (
        observed_artifact.authorized_for_remediation
        and observed_artifact.artifact_id in change.authorized_scope
    ):
        blockers.append(ReconciliationBlocker.AUTHORIZATION_NO_LONGER_VALID)

    if blockers:
        return ReconciliationResult(
            outcome=ReconciliationOutcome.FAIL_CLOSED,
            action_id=action_id,
            evidence=None,
            blockers=tuple(dict.fromkeys(blockers)),
            requires_review=True,
        )

    evidence = MutationEvidence(
        artifact_id=observed_artifact.artifact_id,
        before_ref=str(intent["expected_before_ref"]),
        after_ref=after_ref,
        before_hash=str(intent["expected_before_hash"]),
        after_hash=observed_after_hash,
        before_value=str(intent["expected_before_value"]),
        after_value=str(intent["expected_after_value"]),
        patch_description=(
            f"reconciled post-crash: {intent['expected_before_value']} -> "
            f"{intent['expected_after_value']} on {observed_artifact.artifact_id}"
        ),
        reconciled=True,
        action_id=action_id,
        data_classification=data_classification,
    )
    ledger.mark_completed(
        action_id,
        occurred_at=occurred_at,
        outcome_evidence_ref=after_ref,
        reconciled=True,
    )
    return ReconciliationResult(
        outcome=ReconciliationOutcome.RECONCILED_MUTATION,
        action_id=action_id,
        evidence=evidence,
        blockers=(),
        requires_review=False,
    )


def was_ever_dispatched(ledger: ActionLedger, action_id: str) -> bool:
    """True when a mutation action left ``PLANNED``.

    The NO_OP boundary: ``NoOpEvidence`` is only admissible while this returns False
    for the artifact's remediation action, i.e. this workflow never mutated it.
    """
    record = ledger.get(action_id)
    if record is None:
        return False
    return record.status is not ActionStatus.PLANNED


def no_op_admissible(ledger: ActionLedger, action_id: str) -> bool:
    """True when a NO_OP outcome is still structurally permitted for this action."""
    return not was_ever_dispatched(ledger, action_id)


# ============================ T036 — delivery reconciliation ==========================


class DeliveryOutcome(StrEnum):
    """Result of reconciling a ``DELIVER_DELTA`` action."""

    DELIVERED = "DELIVERED"
    ALREADY_DELIVERED = "ALREADY_DELIVERED"
    UNCERTAIN_NO_RECEIPT = "UNCERTAIN_NO_RECEIPT"


@dataclass(frozen=True)
class DeliveryReconciliationResult:
    """Verdict for a delivery whose response may have been lost."""

    outcome: DeliveryOutcome
    action_id: str
    receipt_ref: str | None
    delivered: bool
    """``DELIVERED`` is recorded only on a resolvable positive receipt."""


def reconcile_delivery(
    ledger: ActionLedger,
    action_id: str,
    *,
    recoverable_receipt_ref: str | None,
    occurred_at: datetime,
) -> DeliveryReconciliationResult:
    """T036 — resolve a delivery via the mechanism's receipt, never via assertion.

    ``recoverable_receipt_ref`` is what the delivery mechanism can still produce for
    this stable ``action_id`` — a receipt or idempotency-key lookup result. If a prior
    attempt genuinely succeeded, that receipt resolves the action without re-sending.

    Absent a resolvable receipt the action stays ``FAILED_OR_UNCERTAIN`` and
    ``DELIVERED`` is not recorded. No agent text asserting delivery is accepted here,
    because no agent output reaches this function at all.
    """
    record = ledger.require(action_id)

    if record.status is ActionStatus.COMPLETED and record.receipt_ref:
        return DeliveryReconciliationResult(
            outcome=DeliveryOutcome.ALREADY_DELIVERED,
            action_id=action_id,
            receipt_ref=record.receipt_ref,
            delivered=True,
        )

    if recoverable_receipt_ref:
        ledger.mark_completed(
            action_id,
            occurred_at=occurred_at,
            receipt_ref=recoverable_receipt_ref,
            reconciled=True,
        )
        return DeliveryReconciliationResult(
            outcome=DeliveryOutcome.DELIVERED,
            action_id=action_id,
            receipt_ref=recoverable_receipt_ref,
            delivered=True,
        )

    ledger.mark_failed_or_uncertain(action_id, occurred_at=occurred_at)
    return DeliveryReconciliationResult(
        outcome=DeliveryOutcome.UNCERTAIN_NO_RECEIPT,
        action_id=action_id,
        receipt_ref=None,
        delivered=False,
    )
