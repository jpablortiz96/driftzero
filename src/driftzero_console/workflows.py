"""T081 — the process-local workflow registry and the CLI fixture loader.

The CLI runs as a series of separate OS processes. They share state by talking HTTP to
one long-lived LOCAL_PILOT runtime, which keeps each injected workflow in memory for the
lifetime of *that server process*.

This is not persistence, and nothing here may describe it as such
--------------------------------------------------------------
Nothing is serialised, nothing touches disk, and a restart loses every workflow. Durable
storage is T092's (``src/driftzero_cloud/firestore.py``, M2). An unknown workflow id is
refused rather than recreated: silently minting a fresh workflow under a requested id
would let ``status`` print ``CHANGE_RECEIVED`` as though it were history, which is the
exact failure this design exists to avoid.

The registry holds application services, not domain objects. Every consequential step
still runs through the existing T080 orchestration, so the CLI adds a transport and
nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.sources.registry import ArtifactCatalog, SourceVersion
from driftzero_console.service import HeroConsoleService, PilotDataset

REGISTRY_NOTE = (
    "Workflow state is process-local to the running LOCAL_PILOT runtime. It is not "
    "durable and does not survive a restart. Durable persistence is T092 (M2)."
)

FORBIDDEN_FIXTURE_KEYS = frozenset(
    {
        "affected_artifact_id",
        "qualified_target",
        "authorization",
        "authorized",
        "workflow_state",
        "state",
        "verification_result",
        "verdict",
        "observed_value",
        "proof_id",
        "proof_hash",
        "content_hash",
        "change_deployed",
        "impact",
        "candidate_affected_artifacts",
    }
)
"""Fields a submitted fixture may never carry.

A source change describes *what changed at the source*. Anything that describes what the
system concluded is an answer, and answers are derived here — never accepted.
"""

ALLOWED_FIXTURE_KEYS = frozenset(
    {
        "change_id",
        "source_procedure_id",
        "source_version",
        "previous_version",
        "operation_id",
        "requirement_id",
        "previous_value",
        "current_value",
        "authorized_scope",
        "approved_status",
        "source_evidence_ref",
        "received_at",
    }
)
"""Legitimate source-change input. Underscore-prefixed metadata keys are ignored."""


class FixtureRejected(Exception):
    """The submitted fixture is not acceptable source-change input."""


class UnknownWorkflow(Exception):
    """No such workflow in this runtime. Never silently recreated."""


def _classification() -> DataClassification:
    return DataClassification(labels=[ClassificationLabel.SYNTHETIC])


def validate_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept only source-change input, or refuse with the offending keys named."""
    if not isinstance(payload, dict):
        raise FixtureRejected("the fixture must be a JSON object")

    meaningful = {k: v for k, v in payload.items() if not k.startswith("_")}
    forbidden = sorted(set(meaningful) & FORBIDDEN_FIXTURE_KEYS)
    if forbidden:
        raise FixtureRejected(
            "a source change may not carry a conclusion; refused fields: "
            + ", ".join(forbidden)
        )
    unknown = sorted(set(meaningful) - ALLOWED_FIXTURE_KEYS)
    if unknown:
        raise FixtureRejected("unrecognised fixture fields: " + ", ".join(unknown))

    for required in ("change_id", "source_procedure_id", "source_version", "previous_version"):
        if not str(meaningful.get(required, "")).strip():
            raise FixtureRejected(f"{required} is required")
    return meaningful


def _load_source_versions(
    directory: Path, source_procedure_id: str, versions: tuple[str, str]
) -> tuple[SourceVersion, SourceVersion]:
    """Find and load the two named versions of a source procedure from a fixture set.

    The fixture *names* the versions; the requirement values come from the version
    documents themselves, so the change is derived from real material rather than from
    whatever the fixture claimed changed.
    """
    found: dict[str, SourceVersion] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict) or raw.get("source_procedure_id") != source_procedure_id:
            continue
        operations = raw.get("operations")
        version = raw.get("source_version")
        if not isinstance(operations, dict) or version not in versions:
            continue
        for operation_id, body in operations.items():
            requirements = (body or {}).get("requirements")
            if not isinstance(requirements, dict):
                continue
            found[version] = SourceVersion(
                source_procedure_id=source_procedure_id,
                version=version,
                operation_id=operation_id,
                title=raw.get("title", source_procedure_id),
                requirements=dict(requirements),
            )
            break

    missing = [v for v in versions if v not in found]
    if missing:
        raise FixtureRejected(
            f"no source procedure document found for {source_procedure_id} "
            f"version(s): {', '.join(missing)}"
        )
    return found[versions[0]], found[versions[1]]


def _load_catalog(directory: Path, operation_id: str) -> ArtifactCatalog:
    """Load every downstream artifact the fixture set describes.

    Decoys included. A catalog trimmed to the plausible entries would make impact
    discovery a formality.
    """
    artifacts: list[DownstreamArtifact] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries = raw.get("artifacts") if isinstance(raw, dict) else None
        candidates = entries if isinstance(entries, list) else [raw]
        for entry in candidates:
            if not isinstance(entry, dict) or "artifact_id" not in entry:
                continue
            if "requirements" not in entry or "operation_id" not in entry:
                continue
            artifact_id = entry["artifact_id"]
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            artifacts.append(
                DownstreamArtifact(
                    artifact_id=artifact_id,
                    artifact_type=entry.get("artifact_type", "work_instruction"),
                    operation_id=entry["operation_id"],
                    requirement_id=entry["requirement_id"],
                    current_value=entry["current_value"],
                    content_ref=entry.get(
                        "content_ref", f"local://artifacts/{artifact_id}"
                    ),
                    authorized_for_remediation=entry.get(
                        "authorized_for_remediation", False
                    ),
                    requirements=dict(entry["requirements"]),
                    data_classification=_classification(),
                )
            )
    if not artifacts:
        raise FixtureRejected(f"no downstream artifacts found beside the fixture in {directory}")
    return ArtifactCatalog(catalog_id=f"fixture:{operation_id}", artifacts=tuple(artifacts))


def dataset_from_fixture(payload: dict[str, Any], *, directory: Path) -> PilotDataset:
    """Build a dataset from a validated fixture plus the source documents beside it."""
    fixture = validate_fixture(payload)
    previous, current = _load_source_versions(
        directory,
        fixture["source_procedure_id"],
        (fixture["previous_version"], fixture["source_version"]),
    )
    catalog = _load_catalog(directory, current.operation_id)
    scope = fixture.get("authorized_scope") or []
    if not isinstance(scope, list) or not scope:
        raise FixtureRejected("authorized_scope must be a non-empty list")
    return PilotDataset(
        change_id=fixture["change_id"],
        source_name=current.title,
        previous=previous,
        current=current,
        catalog=catalog,
        authorized_scope=tuple(str(item) for item in scope),
        approved_status=str(fixture.get("approved_status", "APPROVED")),
    )


@dataclass
class WorkflowRegistry:
    """Workflows this server process is currently holding. In memory only."""

    _services: dict[str, HeroConsoleService] = field(default_factory=dict)
    _runs: dict[str, Any] = field(default_factory=dict)

    def register(self, service: HeroConsoleService) -> str:
        """Store a service under its own workflow id and return that id."""
        workflow_id = service.workflow_id
        self._services[workflow_id] = service
        return workflow_id

    def get(self, workflow_id: str) -> HeroConsoleService:
        """Resolve a workflow, or refuse. Never recreates one under the requested id."""
        service = self._services.get(workflow_id)
        if service is None:
            raise UnknownWorkflow(
                f"no workflow {workflow_id!r} in this runtime. {REGISTRY_NOTE}"
            )
        return service

    def set_run(self, workflow_id: str, run: Any) -> None:
        """Retain the ADK orchestration run so a later verify resumes the same one."""
        self._runs[workflow_id] = run

    def get_run(self, workflow_id: str) -> Any | None:
        return self._runs.get(workflow_id)

    def workflow_ids(self) -> tuple[str, ...]:
        return tuple(self._services)

    def clear(self) -> None:
        """Drop every workflow — what a server restart does, for tests that need it."""
        self._services.clear()
        self._runs.clear()

    def __len__(self) -> int:
        return len(self._services)
