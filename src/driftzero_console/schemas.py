"""Response envelopes for the Hero Console API.

Deliberately thin. The service already returns plain, UI-shaped projections built from
real domain results; these models exist to document the contract and to keep the API
surface explicit rather than to re-model the domain.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HeroState(BaseModel):
    """Everything the console renders, derived from real application data.

    Contains no capability, no ``grant_token``, and no secret: the service projects
    only what a UI may see.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    environment: dict[str, Any]
    scenario: dict[str, Any]
    artifact: dict[str, Any]
    authorization: dict[str, Any]
    fleet: list[dict[str, Any]]
    remediation: dict[str, Any] | None = None
    validated_execution: dict[str, Any] | None = None
    crossing_2: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    frontline: dict[str, Any] | None = None
    field_verification: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    capability_columns: list[str] = Field(default_factory=list)
    security_probe: dict[str, Any] | None = None
    security: dict[str, Any] | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    modules: list[dict[str, Any]] = Field(default_factory=list)
    future_capabilities: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class FrontlineView(BaseModel):
    """Worker-facing payload for one change. No operational authority of any kind."""

    model_config = ConfigDict(extra="forbid")

    change_id: str
    source_name: str
    previous_version: str
    source_version: str
    available: bool
    composed: bool = False
    delivery: dict[str, Any] | None = None
    instruction: dict[str, Any] | None = None
    acknowledgment: dict[str, Any] | None = None
    acknowledged: bool = False
    delivery_established: bool = False
    field_verification: dict[str, Any] | None = None
    delivery_note: str = ""


class EvidenceDocument(BaseModel):
    """One inspectable evidence record, pretty-printed by the UI."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    document: dict[str, Any]
