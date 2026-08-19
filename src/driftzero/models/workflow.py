"""T010/T017 — Canonical workflow states and the Workflow aggregate.

``WorkflowState`` holds exactly the 13 canonical product lifecycle states from
spec.md § State Requirements. Infrastructure or ledger concepts (for example
``ActionStatus``) are deliberately NOT members of this enum.

The transition matrix, the transition engine, and every authorization decision
belong to the Truth Engine (T020+). This module only classifies states and holds
the aggregate's shape.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from driftzero.models.classification import DataClassification
from driftzero.models.remediation import RemediationEvidence
from driftzero.models.verification import VerificationEvent, VerificationResult


class WorkflowState(StrEnum):
    """The 13 canonical lifecycle states. No additions."""

    CHANGE_RECEIVED = "CHANGE_RECEIVED"
    IMPACT_DETERMINED = "IMPACT_DETERMINED"
    REMEDIATION_PENDING = "REMEDIATION_PENDING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REMEDIATION_COMPLETED = "REMEDIATION_COMPLETED"
    FRONTLINE_DELIVERY_COMPLETED = "FRONTLINE_DELIVERY_COMPLETED"
    AWAITING_FIELD_VERIFICATION = "AWAITING_FIELD_VERIFICATION"
    VERIFICATION_INCONCLUSIVE = "VERIFICATION_INCONCLUSIVE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    PROOF_COMPLETE = "PROOF_COMPLETE"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class StateCategory(StrEnum):
    """Category of a lifecycle state (spec § State occupancy vs. state history)."""

    PROGRESSIVE = "PROGRESSIVE"
    BLOCKING_RECOVERABLE = "BLOCKING_RECOVERABLE"
    BLOCKING_GATE = "BLOCKING_GATE"
    TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
    TERMINAL_NON_SUCCESS = "TERMINAL_NON_SUCCESS"


STATE_CATEGORY: MappingProxyType[WorkflowState, StateCategory] = MappingProxyType(
    {
        WorkflowState.CHANGE_RECEIVED: StateCategory.PROGRESSIVE,
        WorkflowState.IMPACT_DETERMINED: StateCategory.PROGRESSIVE,
        WorkflowState.REMEDIATION_PENDING: StateCategory.PROGRESSIVE,
        WorkflowState.REMEDIATION_COMPLETED: StateCategory.PROGRESSIVE,
        WorkflowState.FRONTLINE_DELIVERY_COMPLETED: StateCategory.PROGRESSIVE,
        WorkflowState.AWAITING_FIELD_VERIFICATION: StateCategory.PROGRESSIVE,
        WorkflowState.VERIFICATION_PASSED: StateCategory.PROGRESSIVE,
        # Blocking while current, but recoverable via a later corrected verification.
        WorkflowState.VERIFICATION_FAILED: StateCategory.BLOCKING_RECOVERABLE,
        WorkflowState.VERIFICATION_INCONCLUSIVE: StateCategory.BLOCKING_RECOVERABLE,
        # Blocking gate: no autonomous exit to the progressive workflow in S1.
        WorkflowState.REVIEW_REQUIRED: StateCategory.BLOCKING_GATE,
        WorkflowState.PROOF_COMPLETE: StateCategory.TERMINAL_SUCCESS,
        WorkflowState.SUPERSEDED: StateCategory.TERMINAL_NON_SUCCESS,
        WorkflowState.FAILED: StateCategory.TERMINAL_NON_SUCCESS,
    }
)
"""Category lookup for all 13 states. Consumed by the state machine in T020+."""


class Workflow(BaseModel):
    """T017 — the aggregate tracking one change deployment lifecycle.

    ``affected_artifact_id`` is populated **only** when exactly one candidate passes
    deterministic qualification; it stays None for the zero-qualified and
    multi-qualified cases, whose candidate sets are retained in
    ``candidate_artifact_refs`` as evidence. The qualification rule itself is T025/T026.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    state: WorkflowState
    affected_artifact_id: str | None = Field(
        default=None, description="Set only when exactly one candidate qualifies"
    )
    impact_reason: str | None = Field(default=None)
    candidate_artifact_refs: list[str] = Field(
        default_factory=list, description="All evaluated candidates, retained in every case"
    )
    remediation_evidence: RemediationEvidence | None = Field(default=None)
    delivery_status: str | None = Field(default=None, description="DELIVERED or None")
    delivery_ref: str | None = Field(default=None)
    worker_id: str = Field(min_length=1, description="Opaque identifier — no PII")
    verification_events: list[VerificationEvent] = Field(default_factory=list)
    latest_verification_status: VerificationResult | None = Field(default=None)
    proof_id: str | None = Field(default=None)
    created_at: datetime
    updated_at: datetime
    event_sequence: int = Field(default=0, ge=0)
    data_classification: DataClassification

    @property
    def state_category(self) -> StateCategory:
        """Category of the current state. Pure lookup, no transition logic."""
        return STATE_CATEGORY[self.state]
