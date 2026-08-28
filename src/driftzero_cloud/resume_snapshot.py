"""T097 — the session-level state a resumed workflow needs to finish.

``Workflow`` carries the aggregate, and T092 persists it. It does not carry everything
:meth:`HeroConsoleService._proof_context` reads: the impact resolution, the remediation
evidence, the delivery receipt ref, the rejected-result refs and the verification
chronology live beside the aggregate in the application session.

Without them a recovered workflow can be *read* but never *completed* — it would reach
VERIFICATION_PASSED and then be unable to assemble the proof context, which is a
resumability that stops one step short of the thing that matters. So the snapshot is
part of durable state, not an optimisation.

Everything here is explicit and versioned. The Pydantic models round-trip through
``model_dump(mode="json")``; the frozen dataclasses get a hand-written codec, because
``asdict`` would lose the types needed to reconstruct them. No pickle.
"""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

from pydantic import TypeAdapter

from driftzero.models.proof import RemediationEvidence
from driftzero.models.verification import VerificationEvent
from driftzero.truth_engine.impact import (
    CandidateQualification,
    ImpactOutcome,
    ImpactResolution,
    QualificationCondition,
)
from driftzero_cloud.errors import CloudAdapterError

SNAPSHOT_SCHEMA_VERSION = 1

_REMEDIATION = TypeAdapter(RemediationEvidence)
"""The discriminated union. A TypeAdapter validates it back to the right member rather
than guessing between MutationEvidence and NoOpEvidence."""


# ============================ impact ==================================================


def _encode_qualification(qualification: CandidateQualification) -> dict[str, Any]:
    """``conditions`` is a MappingProxyType keyed by an enum — neither is JSON."""
    return {
        "artifact_id": qualification.artifact_id,
        "qualified": qualification.qualified,
        "conditions": {
            str(condition): bool(value)
            for condition, value in qualification.conditions.items()
        },
        "failed_conditions": [str(c) for c in qualification.failed_conditions],
        "agent_proposed_is_affected": qualification.agent_proposed_is_affected,
        "agent_proposal_disagreed": qualification.agent_proposal_disagreed,
    }


def _decode_qualification(payload: dict[str, Any]) -> CandidateQualification:
    return CandidateQualification(
        artifact_id=payload["artifact_id"],
        qualified=bool(payload["qualified"]),
        conditions=MappingProxyType(
            {
                QualificationCondition(key): bool(value)
                for key, value in (payload.get("conditions") or {}).items()
            }
        ),
        failed_conditions=tuple(
            QualificationCondition(value) for value in payload.get("failed_conditions", ())
        ),
        agent_proposed_is_affected=bool(payload["agent_proposed_is_affected"]),
        agent_proposal_disagreed=bool(payload["agent_proposal_disagreed"]),
    )


def encode_impact(resolution: ImpactResolution) -> dict[str, Any]:
    return {
        "outcome": str(resolution.outcome),
        "affected_artifact_id": resolution.affected_artifact_id,
        "qualified_artifact_ids": list(resolution.qualified_artifact_ids),
        "candidate_artifact_refs": list(resolution.candidate_artifact_refs),
        "qualifications": [_encode_qualification(q) for q in resolution.qualifications],
        "impact_reason": resolution.impact_reason,
        "requires_review": resolution.requires_review,
    }


def decode_impact(payload: dict[str, Any]) -> ImpactResolution:
    return ImpactResolution(
        outcome=ImpactOutcome(payload["outcome"]),
        affected_artifact_id=payload.get("affected_artifact_id"),
        qualified_artifact_ids=tuple(payload.get("qualified_artifact_ids", ())),
        candidate_artifact_refs=tuple(payload.get("candidate_artifact_refs", ())),
        qualifications=tuple(
            _decode_qualification(q) for q in payload.get("qualifications", ())
        ),
        impact_reason=payload.get("impact_reason", ""),
        requires_review=bool(payload.get("requires_review", False)),
    )


# ============================ the snapshot ============================================


def encode_snapshot(session: Any) -> dict[str, Any]:
    """Project the resume-critical session state into a plain document.

    The result is round-tripped through JSON before returning. That is not decoration:
    it is the assertion that nothing Python-specific — an enum key, a MappingProxyType,
    a datetime — survived into a document a different process has to read back.
    """
    return json.loads(json.dumps(_project(session)))


def _project(session: Any) -> dict[str, Any]:
    impact = session.impact_resolution
    remediation = session.remediation_evidence
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": "resume_snapshot",
        "qualified_artifact_id": session.qualified_artifact_id,
        "impact_resolution": encode_impact(impact) if impact is not None else None,
        "remediation_evidence": (
            remediation.model_dump(mode="json") if remediation is not None else None
        ),
        "delivery_receipt_ref": session.delivery_receipt_ref,
        "rejected_result_refs": list(session.rejected_result_refs),
        "verification_events": [
            event.model_dump(mode="json") for event in session.verification_events
        ],
        "known_submission_ids": sorted(session.known_submission_ids),
        "provider_calls": session.provider_calls,
        "action_id": session.action_id,
        # The remediated artifact content, so a rebuilt repository reflects the mutation
        # that already happened rather than the pre-change fixture on disk.
        # InMemoryArtifactRepository exposes no snapshot seam and lives inside the
        # purity boundary, so its store is read from here rather than by adding a
        # method to it. This reads a private without modifying a line of that package.
        "artifacts": {
            artifact_id: artifact.model_dump(mode="json")
            for artifact_id, artifact in session.repository._artifacts.items()
        },
    }


def apply_snapshot(session: Any, document: dict[str, Any]) -> None:
    """Restore the projected state onto a freshly built session.

    Fails closed on an unknown schema version rather than partially applying a document
    it does not understand — a half-restored session would produce a proof context
    assembled from a mixture of two different runs.
    """
    version = document.get("schema_version")
    if version != SNAPSHOT_SCHEMA_VERSION:
        raise CloudAdapterError(
            f"unsupported resume_snapshot schema_version {version!r}; this build reads "
            f"{SNAPSHOT_SCHEMA_VERSION}"
        )

    impact = document.get("impact_resolution")
    session.impact_resolution = decode_impact(impact) if impact else None
    session.qualified_artifact_id = document.get("qualified_artifact_id")

    remediation = document.get("remediation_evidence")
    session.remediation_evidence = (
        _REMEDIATION.validate_python(remediation) if remediation else None
    )
    session.delivery_receipt_ref = document.get("delivery_receipt_ref")
    session.rejected_result_refs = list(document.get("rejected_result_refs") or [])
    session.verification_events = [
        VerificationEvent.model_validate(event)
        for event in document.get("verification_events") or []
    ]
    session.known_submission_ids = set(document.get("known_submission_ids") or [])
    session.provider_calls = int(document.get("provider_calls") or 0)
    session.action_id = document.get("action_id")

    artifacts = document.get("artifacts") or {}
    if artifacts:
        from driftzero.models.artifact import DownstreamArtifact  # noqa: PLC0415

        restored = {
            artifact_id: DownstreamArtifact.model_validate(payload)
            for artifact_id, payload in artifacts.items()
        }
        # Replace in place so the repository's own by-content-ref index is rebuilt the
        # way its constructor builds it.
        session.repository.__init__(restored)
