"""T013/T014 — Field observation and verification event (FR-005).

The agent produces only a normalized observation. PASS/FAIL is derived
deterministically by the Truth Engine comparator (T038), so ``FieldObservation``
carries no verdict and ``confidence_note`` is explicitly non-authoritative.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from driftzero.models.classification import DataClassification


class ObservedPosition(StrEnum):
    """T013 — the closed set of normalized observations.

    Any other value is rejected at parse time rather than coerced (Crossing 4).
    """

    LEFT = "LEFT"
    TOP_RIGHT = "TOP_RIGHT"
    INCONCLUSIVE = "INCONCLUSIVE"


class VerificationResult(StrEnum):
    """Deterministic comparator outcome, assigned by the Truth Engine (T038)."""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class FieldObservation(BaseModel):
    """T013 — the derived observation returned by the Field Verification Agent.

    Deliberately has **no** verification_result field: an agent may not return or
    imply an authoritative PASS/FAIL (contracts/agents.md, Crossing 4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: str = Field(
        min_length=1,
        description=(
            "Stable logical identity of the evidence submission. Transport-duplicate "
            "absorption is a Truth Engine behavior (T031); this model only carries the key."
        ),
    )
    raw_evidence_ref: str = Field(min_length=1, description="Reference to the raw image")
    observed_label_position: ObservedPosition
    confidence_note: str = Field(
        default="", description="Informational only — NEVER authoritative"
    )


class VerificationEvent(BaseModel):
    """T014 — one authoritative field verification attempt within a workflow.

    ``event_sequence`` is allocated once per distinct ``submission_id``; allocation
    and ordering are Truth Engine responsibilities (T031, T037).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    submission_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    event_sequence: int = Field(ge=0, description="Monotonic chronological position")
    raw_evidence_ref: str = Field(min_length=1)
    derived_observation: ObservedPosition
    expected_value: str = Field(min_length=1, description="Current approved expected value")
    verification_result: VerificationResult
    timestamp: datetime
    data_classification: DataClassification
