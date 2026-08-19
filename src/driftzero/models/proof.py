"""T018/T019 — Evidence manifest and Change Proof (FR-006).

The Change Proof is the primary deliverable artifact. It is NOT an LLM summary and
carries no confidence score.

Integrity hash semantics (spec § Change Proof): ``content_hash`` and the manifest
hashes provide **content identity and replacement/alteration detection only**. They
do NOT provide a digital signature, a trusted timestamp, identity attestation,
proof of authorship, non-repudiation, or ledger/blockchain immutability.

Constructing this model does not mean the proof is valid: the seven
``PROOF_COMPLETE`` invariants are evaluated by the Truth Engine (T043), and the
canonical hash is computed in T044. This module only defines the shape.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from driftzero.models.classification import DataClassification
from driftzero.models.remediation import RemediationEvidence
from driftzero.models.verification import VerificationResult


class EvidenceManifest(BaseModel):
    """T018 — the complete evidence collection referenced by a Change Proof.

    ``verification_refs`` holds **all** verification attempts, including FAIL and
    INCONCLUSIVE ones: completion condition 6 requires the full history to be
    preserved and traceably associated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_change_ref: str = Field(min_length=1)
    affected_artifact_ref: str = Field(min_length=1)
    remediation_evidence_refs: list[str] = Field(
        default_factory=list,
        description="MUTATION: before and after URIs. NO_OP: the single evaluated URI.",
    )
    rejected_result_refs: list[str] = Field(
        default_factory=list,
        description="Agent/tool results rejected at a trust boundary, with failing layer",
    )
    delivery_ref: str = Field(min_length=1)
    verification_refs: list[str] = Field(
        default_factory=list, description="ALL verification events, FAIL/INCONCLUSIVE included"
    )
    state_transition_refs: list[str] = Field(default_factory=list)
    content_hashes: dict[str, str] = Field(
        default_factory=dict, description="SHA-256 content digests for referenced artifacts"
    )


class ChangeProof(BaseModel):
    """T019 — the immutable auditable Change Proof record.

    ``remediation_evidence`` is the discriminated union, so completion condition 3
    is satisfied by exactly one path and the proof never contains a fabricated
    after-state for an already-compliant artifact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    proof_id: str = Field(min_length=1, description="One canonical logical proof per workflow")
    workflow_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    source_procedure_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    previous_value: str = Field(min_length=1)
    current_value: str = Field(min_length=1)
    affected_artifact_id: str = Field(min_length=1)
    remediation_evidence: RemediationEvidence
    delivery_status: str = Field(min_length=1, description="DELIVERED")
    delivery_ref: str = Field(min_length=1)
    verification_result: VerificationResult = Field(description="PASS for a completed proof")
    verification_event_id: str = Field(min_length=1, description="Authoritative passing event")
    worker_id: str = Field(min_length=1, description="Opaque identifier — no PII")
    evidence_manifest: EvidenceManifest
    completion_timestamp: datetime
    content_hash: str = Field(
        min_length=1,
        description="SHA-256 of canonical proof JSON — content identity only, not an attestation",
    )
    data_classification: DataClassification
