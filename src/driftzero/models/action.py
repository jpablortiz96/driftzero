"""T016 — ActionExecution idempotency / reconciliation ledger (FR-007, FR-008).

An internal deterministic record of one consequential side effect. This is **NOT**
a workflow lifecycle state and never appears in ``WorkflowState``; the two enums
are disjoint by construction. Scope is exactly the four action types below.

Only the data model belongs to T016. Action-identity derivation (T030), retry
deduplication (T033) and crash reconciliation (T034) are later Truth Engine tasks.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionType(StrEnum):
    """The four consequential logical side effects."""

    REMEDIATE_ARTIFACT = "REMEDIATE_ARTIFACT"
    DELIVER_DELTA = "DELIVER_DELTA"
    PROCESS_FIELD_EVIDENCE = "PROCESS_FIELD_EVIDENCE"
    GENERATE_PROOF = "GENERATE_PROOF"


class ActionStatus(StrEnum):
    """Ledger status.

    PLANNED             intent persisted, not dispatched
    ATTEMPTED           dispatched, outcome not yet confirmed
    COMPLETED           outcome confirmed (observed response or validated reconciliation)
    FAILED_OR_UNCERTAIN attempt failed, or outcome could not be established
    """

    PLANNED = "PLANNED"
    ATTEMPTED = "ATTEMPTED"
    COMPLETED = "COMPLETED"
    FAILED_OR_UNCERTAIN = "FAILED_OR_UNCERTAIN"


class ActionExecution(BaseModel):
    """One ledger record, keyed by a stable ``action_id``."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(
        min_length=1,
        description="Stable idempotency identity. Derivation is T030, not this model.",
    )
    workflow_id: str = Field(min_length=1)
    action_type: ActionType
    status: ActionStatus
    target_ref: str = Field(min_length=1, description="Artifact, worker, submission, or workflow")
    intent: dict[str, Any] = Field(
        default_factory=dict,
        description="Pre-action intent recorded BEFORE dispatch (expected before/after state)",
    )
    receipt_ref: str | None = Field(default=None, description="Tool/mechanism receipt if returned")
    outcome_evidence_ref: str | None = Field(default=None)
    attempt_count: int = Field(default=0, ge=0)
    reconciled: bool = Field(
        default=False, description="Completion established by reconciliation rather than a response"
    )
    created_at: datetime
    updated_at: datetime
