"""Typed request and response contracts for the T094 API.

Every request model sets ``extra="forbid"``. That is the first of two defences against a
client submitting a conclusion: an unrecognised field is refused by validation before
any handler runs. The second is :data:`FORBIDDEN_FIXTURE_KEYS`, already defined by T081,
which names the specific authoritative fields and is checked explicitly so the caller is
told *which* conclusion they tried to submit rather than getting a generic schema error.

Response models are deliberately thin projections. They report what the Truth Engine
concluded; they never carry a field a client could echo back as input.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApprovedChangeRequest(BaseModel):
    """An approved source change. Input only — never a conclusion.

    The field list is exactly ``ALLOWED_FIXTURE_KEYS`` from T081, so the HTTP contract
    and the CLI contract cannot drift apart. A source change describes *what changed at
    the source*; anything describing what the system concluded is refused.
    """

    model_config = ConfigDict(extra="forbid")

    change_id: str = Field(min_length=1)
    source_procedure_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    previous_version: str = Field(min_length=1)
    operation_id: str | None = None
    requirement_id: str | None = None
    previous_value: str | None = None
    current_value: str | None = None
    authorized_scope: list[str] = Field(default_factory=list)
    approved_status: str = "APPROVED"
    source_evidence_ref: str | None = None
    received_at: str | None = None

    def to_fixture(self) -> dict[str, Any]:
        """The payload as the existing loader expects it, with unset fields omitted."""
        return self.model_dump(exclude_none=True)


class ChangeAccepted(BaseModel):
    """Response to ``POST /api/v1/changes`` — the contract's exact shape."""

    workflow_id: str
    state: str
    duplicate_of: str | None = Field(
        default=None,
        description="Set when this change_id was already accepted; the existing "
        "workflow is returned rather than a second one being started.",
    )


class WorkflowStatus(BaseModel):
    """Current workflow state and evidence summary."""

    workflow_id: str
    change_id: str
    state: str
    affected_artifact_id: str | None = None
    delivery_status: str | None = None
    latest_verification_status: str | None = None
    verification_results: list[str] = Field(default_factory=list)
    proof_id: str | None = None
    state_history: list[str] = Field(default_factory=list)
    source: str = Field(
        description="Where this status was read from: LIVE_RUNTIME or DURABLE_STORE."
    )
    durable: bool = Field(
        description="Whether this workflow's state survives a restart of this process."
    )


class VerificationResponse(BaseModel):
    """Response to a field-evidence submission — the contract's exact shape."""

    verification_result: str = Field(description="PASS, FAIL or INCONCLUSIVE")
    workflow_state: str
    submission_id: str | None = None
    accepted: bool = Field(
        description="Whether the submission crossed the trust boundary at all."
    )
    rejection_reason: str | None = None


class EvidenceListing(BaseModel):
    """Evidence artifacts recorded for a workflow.

    The field names mirror ``EvidenceManifest`` exactly rather than inventing a parallel
    vocabulary — the contract says this route returns an ``EvidenceManifest``, and a
    second naming scheme for the same refs is how two parts of a system start
    disagreeing about what an evidence reference is called.
    """

    workflow_id: str
    source_change_ref: str | None = None
    affected_artifact_ref: str | None = None
    remediation_evidence_refs: list[str] = Field(default_factory=list)
    rejected_result_refs: list[str] = Field(default_factory=list)
    delivery_ref: str | None = None
    verification_refs: list[str] = Field(default_factory=list)
    state_transition_refs: list[str] = Field(default_factory=list)
    content_hashes: dict[str, str] = Field(default_factory=dict)
    verification_event_ids: list[str] = Field(
        default_factory=list,
        description="Every verification event on the workflow, including failures, "
        "available before a proof exists.",
    )
    complete: bool = Field(
        default=False,
        description="True only once a Change Proof has been generated, which is the "
        "point at which the manifest is required to cover every event.",
    )


class Health(BaseModel):
    """Liveness. Answers only: is this process running and able to respond."""

    status: str = "ok"
    service: str = "driftzero-api"


class Readiness(BaseModel):
    """Readiness. Reports the configuration this process actually has.

    Deliberately separate from liveness: a process can be alive and correctly *not*
    ready. It reports what is configured, never what is hoped for — before T096 there is
    no Cloud Run deployment, and this must not imply one.
    """

    ready: bool
    persistence_backend: str
    durable: bool
    evidence_bucket: str | None = None
    project: str | None = None
    missing_settings: list[str] = Field(default_factory=list)
    deployment: str = Field(
        default="NOT_DEPLOYED",
        description="CLOUD_RUN only when the Cloud Run runtime contract is actually "
        "present in the environment; never inferred from configuration alone.",
    )
    revision: str | None = Field(
        default=None, description="The Cloud Run revision serving this process."
    )
    runtime_mode: str = Field(
        default="LOCAL_PILOT",
        description="CLOUD_PILOT once durable persistence and a real deployment both "
        "exist. Still a pilot: the source-procedure corpus and artifact catalog are "
        "controlled fixtures shipped with the image, not a live source registry.",
    )
    production_ready: bool = Field(
        default=False,
        description="Deliberately false. A deployment is not production readiness.",
    )
    pilot_limitations: list[str] = Field(
        default_factory=list,
        description="What is still pilot-shaped about this runtime, stated plainly.",
    )


class ErrorResponse(BaseModel):
    """A refusal, with enough detail for the caller to fix the request."""

    error: str
    detail: str
    refused_fields: list[str] = Field(default_factory=list)
