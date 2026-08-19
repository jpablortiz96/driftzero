"""T015 — Frontline delivery result (FR-004).

``delivered`` is an agent-reported flag, not proof. FR-004 requires positive
evidence from the delivery mechanism itself: the Truth Engine records DELIVERED
only when ``delivery_evidence_ref`` resolves to a real receipt (Crossing 3, T036).
This model carries the claim and the receipt reference; it does not adjudicate them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeliveryResult(BaseModel):
    """Result returned by the Frontline Enablement Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str = Field(min_length=1, description="Opaque worker/demo identifier — no PII")
    delivery_mechanism: str = Field(min_length=1, description="e.g. web_notification, api_push")
    delta_content: str = Field(min_length=1, description="Human-readable operational delta")
    delivered: bool = Field(
        description="Agent CLAIM. Never sufficient on its own to establish DELIVERED."
    )
    delivery_evidence_ref: str = Field(
        min_length=1, description="Receipt reference produced by the delivery mechanism"
    )
    training_video_ref: str | None = Field(
        default=None, description="Optional Veo asset. Never establishes delivery."
    )
