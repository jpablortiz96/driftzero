"""Shared builders for the M0 acceptance suites (T047-T059).

Hero fixture semantics throughout: ``label_position: LEFT -> TOP_RIGHT`` on the packing
operation, with an unrelated forklift artifact that also holds the value ``LEFT``.

Everything here is offline and deterministic. No cloud, no model, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import AffectedArtifactCandidate, ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.proof import EvidenceManifest
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.models.verification import (
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.truth_engine.evidence import assemble_evidence_manifest, content_hash
from driftzero.truth_engine.impact import ImpactOutcome, ImpactResolution
from driftzero.truth_engine.proof_generator import ProofContext

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
WF = "wf-001"
ARTIFACT = "wi-packing-standard-001"
UNRELATED = "wi-forklift-turn-014"
WORKER = "worker-opaque-01"
RECEIPT = "gs://evidence/delivery/receipt-001.json"

T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
T_PASS = datetime(2026, 8, 17, 11, 0, tzinfo=UTC)

BEFORE_CONTENT = '{"label_position":"LEFT"}'
AFTER_CONTENT = '{"label_position":"TOP_RIGHT"}'
BEFORE_HASH = content_hash(BEFORE_CONTENT)
AFTER_HASH = content_hash(AFTER_CONTENT)

SOURCE_V1 = {"label_position": "LEFT", "box_size": "STANDARD", "seal_type": "TAPE"}
SOURCE_V2 = {"label_position": "TOP_RIGHT", "box_size": "STANDARD", "seal_type": "TAPE"}


def make_change(**overrides: object) -> ApprovedChange:
    base: dict[str, object] = {
        "change_id": "chg-1",
        "source_procedure_id": "proc-warehouse-packing",
        "source_version": "v2",
        "previous_version": "v1",
        "operation_id": "packing_label_placement",
        "requirement_id": "label_position",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": [ARTIFACT],
        "approved_status": "APPROVED",
        "source_evidence_ref": "fixtures/source_procedure_v2.json",
        "received_at": T0,
        "data_classification": SYNTHETIC,
    }
    return ApprovedChange(**{**base, **overrides})  # type: ignore[arg-type]


def make_artifact(**overrides: object) -> DownstreamArtifact:
    """The authorized stale packing instruction (LEFT at impact time)."""
    base: dict[str, object] = {
        "artifact_id": ARTIFACT,
        "artifact_type": "work_instruction",
        "operation_id": "packing_label_placement",
        "requirement_id": "label_position",
        "current_value": "LEFT",
        "content_ref": "fixtures/stale_artifact.json",
        "authorized_for_remediation": True,
        "requirements": dict(SOURCE_V1),
        "data_classification": SYNTHETIC,
    }
    return DownstreamArtifact(**{**base, **overrides})  # type: ignore[arg-type]


def make_unrelated_artifact(**overrides: object) -> DownstreamArtifact:
    """Different operation, same lexical value ``LEFT``. Must never qualify (SC-002)."""
    base: dict[str, object] = {
        "artifact_id": UNRELATED,
        "artifact_type": "work_instruction",
        "operation_id": "forklift_navigation",
        "requirement_id": "turn_direction",
        "current_value": "LEFT",
        "content_ref": "fixtures/unrelated_artifact.json",
        "authorized_for_remediation": False,
        "requirements": {"turn_direction": "LEFT"},
        "data_classification": SYNTHETIC,
    }
    return DownstreamArtifact(**{**base, **overrides})  # type: ignore[arg-type]


def make_candidate(artifact_id: str = ARTIFACT, *, is_affected: bool = True):
    return AffectedArtifactCandidate(
        artifact_id=artifact_id,
        impact_reason="agent proposal",
        operation_match=True,
        instruction_correspondence=True,
        value_conflict=True,
        in_authorized_scope=True,
        is_affected=is_affected,
    )


def make_mutation(**overrides: object) -> MutationEvidence:
    base: dict[str, object] = {
        "artifact_id": ARTIFACT,
        "before_ref": "gs://evidence/before.json",
        "after_ref": "gs://evidence/after.json",
        "before_hash": BEFORE_HASH,
        "after_hash": AFTER_HASH,
        "before_value": "LEFT",
        "after_value": "TOP_RIGHT",
        "patch_description": "label_position LEFT -> TOP_RIGHT",
        "action_id": "act-remediate-001",
        "data_classification": SYNTHETIC,
    }
    return MutationEvidence(**{**base, **overrides})  # type: ignore[arg-type]


def make_no_op(**overrides: object) -> NoOpEvidence:
    base: dict[str, object] = {
        "artifact_id": ARTIFACT,
        "evaluated_artifact_ref": "gs://evidence/evaluated.json",
        "evaluated_artifact_hash": AFTER_HASH,
        "observed_value": "TOP_RIGHT",
        "expected_value": "TOP_RIGHT",
        "no_op_reason": "artifact already represented the approved value",
        "compliance_basis": "requirements.label_position",
        "data_classification": SYNTHETIC,
    }
    return NoOpEvidence(**{**base, **overrides})  # type: ignore[arg-type]


def make_event(
    *,
    event_id: str,
    sequence: int,
    observation: ObservedPosition,
    result: VerificationResult,
    timestamp: datetime | None = None,
    workflow_id: str = WF,
    submission_id: str | None = None,
) -> VerificationEvent:
    return VerificationEvent(
        event_id=event_id,
        submission_id=submission_id or f"sub-{sequence}",
        workflow_id=workflow_id,
        event_sequence=sequence,
        raw_evidence_ref=f"gs://evidence/{event_id}.jpg",
        derived_observation=observation,
        expected_value="TOP_RIGHT",
        verification_result=result,
        timestamp=timestamp or T0,
        data_classification=SYNTHETIC,
    )


FAIL_EVENT = make_event(
    event_id="ev-1", sequence=1, observation=ObservedPosition.LEFT, result=VerificationResult.FAIL
)
INCONCLUSIVE_EVENT = make_event(
    event_id="ev-1i",
    sequence=1,
    observation=ObservedPosition.INCONCLUSIVE,
    result=VerificationResult.INCONCLUSIVE,
)
PASS_EVENT = make_event(
    event_id="ev-2",
    sequence=2,
    observation=ObservedPosition.TOP_RIGHT,
    result=VerificationResult.PASS,
    timestamp=T_PASS,
)


def make_workflow(state: WorkflowState = WorkflowState.VERIFICATION_PASSED, **overrides: object):
    base: dict[str, object] = {
        "workflow_id": WF,
        "change_id": "chg-1",
        "source_version": "v2",
        "state": state,
        "affected_artifact_id": ARTIFACT,
        "candidate_artifact_refs": [ARTIFACT],
        "delivery_status": "DELIVERED",
        "delivery_ref": RECEIPT,
        "worker_id": WORKER,
        "created_at": T0,
        "updated_at": T_PASS,
        "event_sequence": 2,
        "data_classification": SYNTHETIC,
    }
    return Workflow(**{**base, **overrides})  # type: ignore[arg-type]


def make_impact(**overrides: object) -> ImpactResolution:
    base: dict[str, object] = {
        "outcome": ImpactOutcome.SINGLE_QUALIFIED_TARGET,
        "affected_artifact_id": ARTIFACT,
        "qualified_artifact_ids": (ARTIFACT,),
        "candidate_artifact_refs": (ARTIFACT,),
        "qualifications": (),
        "impact_reason": "exactly one candidate qualified",
        "requires_review": False,
    }
    return ImpactResolution(**{**base, **overrides})  # type: ignore[arg-type]


def make_manifest(evidence=None, events=None, *, receipt: str = RECEIPT, rejected=()):
    return assemble_evidence_manifest(
        source_change_ref="fixtures/hero_change.json",
        affected_artifact_ref=ARTIFACT,
        remediation_evidence=evidence if evidence is not None else make_mutation(),
        delivery_ref=receipt,
        verification_events=list(events if events is not None else [FAIL_EVENT, PASS_EVENT]),
        state_transition_refs=["log/transitions.json"],
        rejected_result_refs=list(rejected),
    )


_DEFAULT = object()


def make_proof_context(
    *,
    evidence=_DEFAULT,
    events=None,
    state: WorkflowState = WorkflowState.VERIFICATION_PASSED,
    history=(),
    applicable: bool = True,
    receipt: str | None = RECEIPT,
    rejected=(),
    impact: ImpactResolution | None = None,
    manifest: EvidenceManifest | None = None,
    workflow: Workflow | None = None,
) -> ProofContext:
    """A fully valid completion context; override one field to attack one condition."""
    resolved = make_mutation() if evidence is _DEFAULT else evidence
    events = list(events if events is not None else [FAIL_EVENT, PASS_EVENT])
    return ProofContext(
        workflow=workflow or make_workflow(state),
        change=make_change(),
        impact=impact or make_impact(),
        remediation_evidence=resolved,
        manifest=manifest
        or make_manifest(
            resolved if resolved is not None else make_mutation(),
            events,
            receipt=receipt or "gs://evidence/delivery/unresolved.json",
            rejected=rejected,
        ),
        verification_events=events,
        state_history=list(history) or [WorkflowState.CHANGE_RECEIVED],
        source_version_applicable=applicable,
        delivery_receipt_ref=receipt,
    )
