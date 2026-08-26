"""T080 step 1 — the source-change input boundary.

An approved change is not a hand-written assertion that ``label_position`` moved. It is
what you get when you compare two **real, independently retrievable** versions of a source
procedure. This module holds those versions, hashes them, resolves them, and derives the
change from the difference.

Why derivation rather than declaration
--------------------------------------
A system that is told "the requirement is ``label_position``, before ``LEFT``, after
``TOP_RIGHT``" has been handed most of the answer. Deriving it from v13 vs v14 means the
change is a **consequence of the source material**, and a source edit anywhere else
produces a different change with no code edit at all.

Fails closed on ambiguity: zero changed requirements is not a change, and more than one is
not a single-target S1 change. Neither is guessed at.

Resolvable refs
---------------
``SourceProcedureStore`` is append-only and every ref resolves to the exact version it
described — the lesson T073, T078, and T079 each taught in turn. A ``source_evidence_ref``
that cannot retrieve the source is a string, not evidence, and this store refuses to
produce one.

Nothing here calls a model. The catalog and the versions are data; what they *mean* is the
semantic layer's question, and whether an artifact is affected is the Truth Engine's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import DataClassification
from driftzero.truth_engine.evidence import canonical_hash

SOURCE_REF_SCHEME = "source"
"""``source:PACKING-SOP:v14`` — stable, and it resolves."""


class SourceIngestionError(Exception):
    """The source material cannot yield a single well-formed change."""


@dataclass(frozen=True)
class SourceVersion:
    """One complete, immutable version of a source procedure.

    A *version*, not a diff: the whole requirement set is retained, so the change between
    any two versions can be re-derived later from the stored material alone.
    """

    source_procedure_id: str
    version: str
    operation_id: str
    title: str
    requirements: Mapping[str, str]
    notes: tuple[str, ...] = ()
    effective_from: str | None = None

    @property
    def content_ref(self) -> str:
        return f"{SOURCE_REF_SCHEME}:{self.source_procedure_id}:{self.version}"

    @property
    def content_hash(self) -> str:
        """Canonical hash of the version's content, via the frozen M0 helper."""
        return canonical_hash(
            {
                "source_procedure_id": self.source_procedure_id,
                "version": self.version,
                "operation_id": self.operation_id,
                "title": self.title,
                "requirements": dict(self.requirements),
                "notes": list(self.notes),
            }
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "source_procedure_id": self.source_procedure_id,
            "version": self.version,
            "operation_id": self.operation_id,
            "content_ref": self.content_ref,
            "content_hash": self.content_hash,
            "requirement_count": len(self.requirements),
        }

    def as_untrusted_text(self) -> str:
        """Render the version for a model. Every line is data, never instruction."""
        lines = [
            f"source_procedure_id: {self.source_procedure_id}",
            f"version: {self.version}",
            f"operation_id: {self.operation_id}",
            f"title: {self.title}",
            "requirements:",
            *(f"  {key}: {value}" for key, value in sorted(self.requirements.items())),
        ]
        if self.notes:
            lines.append("notes:")
            lines.extend(f"  - {note}" for note in self.notes)
        return "\n".join(lines)


@dataclass
class SourceProcedureStore:
    """Append-only store of source versions. Refs resolve, permanently."""

    _versions: dict[str, SourceVersion] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def register(self, version: SourceVersion) -> str:
        """Store one version and return its resolvable ref."""
        ref = version.content_ref
        existing = self._versions.get(ref)
        if existing is not None:
            if existing.content_hash != version.content_hash:
                raise SourceIngestionError(
                    f"refusing to overwrite {ref}: a different content hash is already "
                    "registered under that version"
                )
            return ref
        self._versions[ref] = version
        self._order.append(ref)
        return ref

    def resolve(self, content_ref: str) -> SourceVersion | None:
        """Retrieve the version at ``content_ref``. Resolving never mutates the store."""
        return self._versions.get(content_ref)

    def resolvable_refs(self) -> frozenset[str]:
        return frozenset(self._versions)

    def versions_of(self, source_procedure_id: str) -> tuple[SourceVersion, ...]:
        return tuple(
            self._versions[ref]
            for ref in self._order
            if self._versions[ref].source_procedure_id == source_procedure_id
        )

    def __len__(self) -> int:
        return len(self._versions)


@dataclass(frozen=True)
class RequirementDelta:
    """One requirement that differs between two versions."""

    requirement_id: str
    previous_value: str
    current_value: str


def diff_requirements(
    previous: SourceVersion, current: SourceVersion
) -> tuple[RequirementDelta, ...]:
    """Every requirement whose value differs. Additions and removals included.

    Deterministic and ordered by requirement id, so the same two versions always produce
    the same diff regardless of dict ordering.
    """
    keys = sorted(set(previous.requirements) | set(current.requirements))
    return tuple(
        RequirementDelta(
            requirement_id=key,
            previous_value=previous.requirements.get(key, ""),
            current_value=current.requirements.get(key, ""),
        )
        for key in keys
        if previous.requirements.get(key) != current.requirements.get(key)
    )


@dataclass(frozen=True)
class SourceChangeIngestion:
    """The outcome of step 1: a derived, provenance-bearing approved change."""

    change: ApprovedChange
    previous: SourceVersion
    current: SourceVersion
    delta: RequirementDelta
    all_deltas: tuple[RequirementDelta, ...]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "change_id": self.change.change_id,
            "source_procedure_id": self.change.source_procedure_id,
            "previous_version": self.previous.version,
            "current_version": self.current.version,
            "previous_content_ref": self.previous.content_ref,
            "previous_content_hash": self.previous.content_hash,
            "current_content_ref": self.current.content_ref,
            "current_content_hash": self.current.content_hash,
            "derived_requirement_id": self.delta.requirement_id,
            "derived_previous_value": self.delta.previous_value,
            "derived_current_value": self.delta.current_value,
            "changed_requirement_count": len(self.all_deltas),
            "derivation": "DIFF_OF_TWO_RETRIEVED_SOURCE_VERSIONS",
        }


def ingest_source_change(
    *,
    change_id: str,
    previous: SourceVersion,
    current: SourceVersion,
    authorized_scope: Sequence[str],
    approved_status: str,
    received_at: datetime,
    data_classification: DataClassification,
    store: SourceProcedureStore,
) -> SourceChangeIngestion:
    """Derive an :class:`ApprovedChange` from two retrieved source versions.

    Both versions are registered so ``source_evidence_ref`` resolves afterwards. The
    requirement, before-value, and after-value are read out of the diff — nothing is
    supplied by a caller, a frontend, or a model.
    """
    if previous.source_procedure_id != current.source_procedure_id:
        raise SourceIngestionError(
            "the two versions belong to different source procedures"
        )
    if previous.operation_id != current.operation_id:
        raise SourceIngestionError(
            "the operation changed between versions; that is not a requirement change"
        )
    if previous.version == current.version:
        raise SourceIngestionError("previous and current are the same version")

    deltas = diff_requirements(previous, current)
    if not deltas:
        raise SourceIngestionError(
            f"{previous.version} and {current.version} carry identical requirements; "
            "there is no change to deploy"
        )
    if len(deltas) > 1:
        raise SourceIngestionError(
            f"{len(deltas)} requirements changed "
            f"({', '.join(d.requirement_id for d in deltas)}); the single-target S1 path "
            "handles exactly one and will not pick between them"
        )

    delta = deltas[0]
    if not delta.previous_value or not delta.current_value:
        raise SourceIngestionError(
            f"{delta.requirement_id} was added or removed rather than changed; the S1 "
            "path covers value changes only"
        )

    store.register(previous)
    current_ref = store.register(current)

    change = ApprovedChange(
        change_id=change_id,
        source_procedure_id=current.source_procedure_id,
        source_version=current.version,
        previous_version=previous.version,
        operation_id=current.operation_id,
        requirement_id=delta.requirement_id,
        previous_value=delta.previous_value,
        current_value=delta.current_value,
        authorized_scope=list(authorized_scope),
        approved_status=approved_status,
        source_evidence_ref=current_ref,
        received_at=received_at,
        data_classification=data_classification,
    )
    return SourceChangeIngestion(
        change=change,
        previous=previous,
        current=current,
        delta=delta,
        all_deltas=deltas,
    )


# ============================ downstream artifact catalog =============================


@dataclass(frozen=True)
class ArtifactCatalog:
    """The downstream artifacts this deployment knows about.

    Deliberately holds decoys. A catalog whose only plausible entry is the right one
    would make impact discovery a formality; here an artifact can match the operation, or
    the requirement, or the value, or the authorization, and still not qualify.
    """

    catalog_id: str
    artifacts: tuple[DownstreamArtifact, ...]

    @property
    def catalog_hash(self) -> str:
        """Binds evidence to the exact candidate set that was considered."""
        return canonical_hash(
            {
                "catalog_id": self.catalog_id,
                "artifacts": [
                    {
                        "artifact_id": a.artifact_id,
                        "operation_id": a.operation_id,
                        "requirement_id": a.requirement_id,
                        "current_value": a.current_value,
                        "requirements": dict(a.requirements),
                    }
                    for a in self.artifacts
                ],
            }
        )

    @property
    def artifact_ids(self) -> frozenset[str]:
        return frozenset(a.artifact_id for a in self.artifacts)

    def get(self, artifact_id: str) -> DownstreamArtifact | None:
        return next(
            (a for a in self.artifacts if a.artifact_id == artifact_id), None
        )

    def as_untrusted_text(self) -> str:
        """Render candidates for a model. Structured content, no answer key.

        Every artifact is presented identically. Nothing marks one as expected, in
        scope, likely, or relevant — the model receives the catalog, not a shortlist.
        """
        blocks = []
        for artifact in self.artifacts:
            lines = [
                f"- artifact_id: {artifact.artifact_id}",
                f"  artifact_type: {artifact.artifact_type}",
                f"  operation_id: {artifact.operation_id}",
                "  requirements:",
                *(
                    f"    {key}: {value}"
                    for key, value in sorted(artifact.requirements.items())
                ),
            ]
            blocks.append("\n".join(lines))
        return "\n".join(blocks)


def load_source_version(path: Path) -> SourceVersion:
    """Load one source-procedure version from its record."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SourceVersion(
        source_procedure_id=raw["source_procedure_id"],
        version=raw["version"],
        operation_id=raw["operation_id"],
        title=raw.get("title", ""),
        requirements=dict(raw["requirements"]),
        notes=tuple(raw.get("notes", ())),
        effective_from=raw.get("effective_from"),
    )


def load_artifact_catalog(
    path: Path, *, data_classification: DataClassification
) -> ArtifactCatalog:
    """Load the downstream artifact catalog from its record."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifacts = tuple(
        DownstreamArtifact(
            artifact_id=entry["artifact_id"],
            artifact_type=entry["artifact_type"],
            operation_id=entry["operation_id"],
            requirement_id=entry["requirement_id"],
            current_value=entry["current_value"],
            content_ref=f"local://artifacts/{entry['artifact_id']}",
            authorized_for_remediation=entry["authorized_for_remediation"],
            requirements=dict(entry["requirements"]),
            data_classification=data_classification,
        )
        for entry in raw["artifacts"]
    )
    return ArtifactCatalog(catalog_id=raw["catalog_id"], artifacts=artifacts)


def load_approved_change_record(path: Path, change_id: str) -> dict[str, Any]:
    """The approval record for ``change_id``: scope and status, never an impact target."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    for entry in raw["changes"]:
        if entry["change_id"] == change_id:
            return dict(entry)
    raise SourceIngestionError(f"no approved change recorded for {change_id!r}")
