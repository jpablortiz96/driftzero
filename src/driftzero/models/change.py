"""T008/T009 — Approved change and the agent-proposed ChangeSet (FR-001, FR-002)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from driftzero.models.classification import DataClassification


class ApprovedChange(BaseModel):
    """T008 — the authoritative approved operational delta as ingested.

    Read-only with respect to the source procedure: this record *describes* an
    approved change, and nothing in the deterministic core may write back to the
    authoritative procedure (SC-004).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_id: str = Field(min_length=1, description="Unique logical change identifier")
    source_procedure_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1, description="Version that introduced the change")
    previous_version: str = Field(min_length=1, description="Prior version being superseded")
    operation_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    previous_value: str = Field(min_length=1)
    current_value: str = Field(min_length=1)
    authorized_scope: list[str] = Field(
        default_factory=list, description="Artifact IDs authorized for remediation"
    )
    approved_status: str = Field(min_length=1, description="Provenance/approval status")
    source_evidence_ref: str = Field(min_length=1)
    received_at: datetime
    data_classification: DataClassification


class AffectedArtifactCandidate(BaseModel):
    """T009 — one candidate proposed by the Change Intelligence Agent.

    ``is_affected`` is a **proposal only** (contracts/agents.md, Crossing 1). The
    Truth Engine decides impact deterministically in T025; this model neither
    derives nor enforces agreement between ``is_affected`` and the four condition
    booleans, because doing so would move a Truth Engine decision into Pydantic.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    impact_reason: str = Field(min_length=1, description="Auditable explanation")
    operation_match: bool
    instruction_correspondence: bool
    value_conflict: bool
    in_authorized_scope: bool
    is_affected: bool = Field(
        description="Agent PROPOSAL. Never authoritative; qualification happens in T025."
    )


class ChangeSet(BaseModel):
    """T009 — structured extraction produced by the Change Intelligence Agent.

    Cardinality is deliberately unconstrained here (0..N candidates). The
    exactly-one qualification rule is a Truth Engine decision (T026).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    change_id: str = Field(min_length=1)
    source_procedure_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    previous_value: str = Field(min_length=1)
    current_value: str = Field(min_length=1)
    authorized_scope: list[str] = Field(default_factory=list)
    candidate_affected_artifacts: list[AffectedArtifactCandidate] = Field(default_factory=list)
