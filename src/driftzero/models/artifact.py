"""T007 — Downstream operational artifact (FR-002).

A derived work instruction that may contain a stale requirement value. The
authoritative source procedure is a different concept and is never represented
by this model, because the workflow must never modify the source.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from driftzero.models.classification import DataClassification


class DownstreamArtifact(BaseModel):
    """An authorized operational artifact that may be affected by a change.

    ``authorized_for_remediation`` records the registry's authorization state. It is
    an input to the deterministic qualification performed later by the Truth Engine
    (T025); this model does not decide whether the artifact is affected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1, description="e.g. work_instruction")
    operation_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1, description="Requirement this artifact implements")
    current_value: str = Field(min_length=1, description="Value currently represented")
    content_ref: str = Field(min_length=1, description="Reference to artifact content")
    authorized_for_remediation: bool
    requirements: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Structured requirement set for this operational scope. Input to the "
            "condition-8 divergence comparator (T027); this model performs no comparison."
        ),
    )
    data_classification: DataClassification
