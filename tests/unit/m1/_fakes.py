"""Shared offline fakes for the M1-A semantic layer.

No network, no SDK, no credentials. Fakes implement the real
:class:`SemanticModelClient` protocol and return raw structured material, so every test
exercises the genuine validation boundary rather than stepping around it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents.change_intel import ReadOnlyTools  # noqa: E402
from driftzero.agents.model_client import SemanticRequest  # noqa: E402
from driftzero.models.artifact import DownstreamArtifact  # noqa: E402
from driftzero.models.change import ApprovedChange  # noqa: E402
from driftzero.models.classification import ClassificationLabel, DataClassification  # noqa: E402

CHANGE_ID = "CHG-001"
ARTIFACT_ID = "WI-114"
OTHER_ARTIFACT_ID = "WI-220"


def make_classification() -> DataClassification:
    return DataClassification(labels=[ClassificationLabel.SYNTHETIC])


def make_change(**overrides: Any) -> ApprovedChange:
    defaults: dict[str, Any] = {
        "change_id": CHANGE_ID,
        "source_procedure_id": "PROC-77",
        "source_version": "v3",
        "previous_version": "v2",
        "operation_id": "OP-9",
        "requirement_id": "label_position",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": [ARTIFACT_ID],
        "approved_status": "APPROVED",
        "source_evidence_ref": "gs://evidence/chg-001",
        "received_at": datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        "data_classification": make_classification(),
    }
    defaults.update(overrides)
    return ApprovedChange(**defaults)


def make_artifact(artifact_id: str = ARTIFACT_ID, **overrides: Any) -> DownstreamArtifact:
    defaults: dict[str, Any] = {
        "artifact_id": artifact_id,
        "artifact_type": "work_instruction",
        "operation_id": "OP-9",
        "requirement_id": "label_position",
        "current_value": "LEFT",
        "content_ref": f"gs://artifacts/{artifact_id}",
        "authorized_for_remediation": True,
        "requirements": {"label_position": "LEFT"},
        "data_classification": make_classification(),
    }
    defaults.update(overrides)
    return DownstreamArtifact(**defaults)


def valid_payload(**overrides: Any) -> dict[str, Any]:
    """A well-formed ChangeSet payload matching the fixture change."""
    payload: dict[str, Any] = {
        "change_id": CHANGE_ID,
        "source_procedure_id": "PROC-77",
        "source_version": "v3",
        "operation_id": "OP-9",
        "requirement_id": "label_position",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": [ARTIFACT_ID],
        "candidate_affected_artifacts": [
            {
                "artifact_id": ARTIFACT_ID,
                "impact_reason": "same operation and requirement, value still LEFT",
                "operation_match": True,
                "instruction_correspondence": True,
                "value_conflict": True,
                "in_authorized_scope": True,
                "is_affected": True,
            }
        ],
    }
    payload.update(overrides)
    return payload


class FakeModelClient:
    """Scripted client implementing the real protocol.

    Each entry in ``script`` is either a mapping to return or an exception to raise, so a
    test can drive success, transient failure, malformed output, or exhaustion exactly.
    """

    def __init__(self, script: Sequence[Any]) -> None:
        self.script = list(script)
        self.requests: list[SemanticRequest] = []

    def generate_structured(self, request: SemanticRequest) -> Mapping[str, Any]:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("fake client called more times than the script allows")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def make_tools(
    change: ApprovedChange | None = None,
    artifacts: Sequence[DownstreamArtifact] | None = None,
) -> ReadOnlyTools:
    """Read-only tools over in-memory fixtures. Neither can write anything."""
    stored = make_change() if change is None else change
    registry = [make_artifact()] if artifacts is None else list(artifacts)

    def read_approved_change(change_id: str) -> ApprovedChange | None:
        return stored if change_id == stored.change_id else None

    def read_artifact_registry() -> Sequence[DownstreamArtifact]:
        return tuple(registry)

    return ReadOnlyTools(
        read_approved_change=read_approved_change,
        read_artifact_registry=read_artifact_registry,
    )
