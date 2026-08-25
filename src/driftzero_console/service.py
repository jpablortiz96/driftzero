"""Hero Console application service.

The browser never touches the domain. It calls this service, which owns one in-memory
demo session and delegates every consequential step to the **real** components already
built and tested:

* authorization policy and capability broker (T075)
* Remediation Agent (T074)
* Artifact Mutation Tool (T073)
* Crossing 2 validation (T076)
* frontline delta composition and delivery (T077, T078)
* Crossing 3 delivery validation (T078)
* field observation, Crossing 4, and the live model provider (T079)

No business logic is reimplemented here. The service composes, records presentation
events, and projects results into shapes a UI can render — it decides nothing.

Deliberately outside ``src/driftzero``: that package's purity guard asserts its entire
third-party surface is pydantic, and the console needs a web framework. Keeping the
console a sibling package means the deterministic core stays provably clean and no M0
test had to be relaxed to accommodate a UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from driftzero.agents.enablement import (
    DeltaInstruction,
    DeltaStatus,
    FrontlineAcknowledgment,
    FrontlineEnablementAgent,
)
from driftzero.agents.field_verify import (
    FieldVerificationAgent,
    ObservationContext,
    ObservationStatus,
    evidence_is_replayable,
    get_field_observation_provider,
    has_field_observation_provider,
)
from driftzero.agents.orchestrator import (
    VerdictContext,
    adjudicate_field_verification,
    authoritative_expected_value,
    change_is_deployed,
    remaining_condition_for,
    reopen_for_new_evidence,
    verification_history,
)
from driftzero.agents.remediation import (
    RemediationAgent,
    RemediationIntent,
    RemediationStatus,
)
from driftzero.capabilities import (
    AUTHORIZATION_POLICY,
    ENFORCEMENT_MODEL,
    PLATFORM_ENFORCED_PER_AGENT_IDENTITY,
    SHARED_RUNTIME_SERVICE_ACCOUNT,
    AgentIdentity,
    CapabilityBroker,
    ToolCapability,
    is_authorized,
)
from driftzero.config import DriftZeroConfig, FieldProviderConfig
from driftzero.delivery.local_channel import (
    LocalPilotDeliveryChannel,
    UncertainDeliveryError,
)
from driftzero.field.evidence import (
    ACCEPTED_CONTAINERS,
    MAX_IMAGE_BYTES,
    FieldEvidenceStore,
    ImageRejected,
    accept_field_image,
    derive_observation_operation_id,
    derive_submission_id,
)
from driftzero.media.container import MIME_BY_CONTAINER
from driftzero.models.action import ActionType
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.orchestration import (
    DeliveryCrossingContext,
    ObservationCrossingContext,
    RemediationCrossingContext,
    accept_delivery_result,
    accept_field_observation,
    accept_remediation_evidence,
)
from driftzero.tools.artifact_mutation import (
    InMemoryArtifactRepository,
    MutationToolContext,
    artifact_content_hash,
)
from driftzero.truth_engine.actions import (
    ActionLedger,
    RetryDecision,
    decide_retry,
    reconcile_delivery,
)
from driftzero.truth_engine.idempotency import (
    derive_delivery_action_id,
    derive_remediation_action_id,
)
from driftzero.truth_engine.state_machine import can_transition, transition


class Environment(StrEnum):
    """How this instance presents itself. Never inferred optimistically."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


def current_environment() -> Environment:
    """Read ``DRIFTZERO_ENV``; anything unrecognised means development.

    Failing to development is the honest default: a misconfigured instance must not
    silently claim to be production.
    """
    raw = os.environ.get("DRIFTZERO_ENV", "development").strip().lower()
    return Environment.PRODUCTION if raw == "production" else Environment.DEVELOPMENT


@dataclass(frozen=True)
class ChangeCase:
    """A pilot case expressed as **data**.

    Nothing about the packing label is special to the code. Swapping this record
    changes the whole product surface, which is what the regression proves.
    """

    change_id: str
    source_name: str
    source_procedure_id: str
    operation_id: str
    previous_version: str
    source_version: str
    requirement_id: str
    previous_value: str
    current_value: str
    artifact_id: str
    artifact_type: str
    requirements: dict[str, str]
    source_evidence_ref: str
    rationale: str | None = None


PACKING_LABEL_PILOT = ChangeCase(
    change_id="DZ-001",
    source_name="Packing SOP",
    source_procedure_id="PACKING-SOP",
    operation_id="OP-PACK-01",
    previous_version="v13",
    source_version="v14",
    requirement_id="label_position",
    previous_value="LEFT",
    current_value="TOP_RIGHT",
    artifact_id="WI-114",
    artifact_type="work_instruction",
    requirements={
        "label_position": "LEFT",
        "instructions": "Keep the LEFT support arm attached",
        "packing_mode": "STANDARD",
    },
    source_evidence_ref="local://changes/DZ-001",
)
"""First pilot operational case. Real physical box, real printed label."""


RUNTIME_READINESS = "LOCAL_PILOT"
"""What the runtime actually is.

Deliberately independent of ``DRIFTZERO_ENV``. That variable selects a *presentation*
mode; it is not evidence that persistence, cloud deployment, or operational identity
exist. Conflating the two is how a local process starts describing itself as
production-ready.
"""

VERDICT_AUTHORITY = "DRIFTZERO TRUTH ENGINE"
"""Who decides PASS/FAIL. Never the model, never an agent, never the browser."""

MODEL_OBSERVATION_SOURCE = "Gemma 4 MaaS"
"""Who reports what was seen. Named separately so a UI cannot conflate the two."""

DESTINATION_REF = "frontline:pilot-surface"
"""Where the pilot delivers. A surface, not a named employee."""

WORKFLOW_ID = "wf-dz-001"
ARTIFACT_ID = PACKING_LABEL_PILOT.artifact_id
REQUIREMENT_ID = PACKING_LABEL_PILOT.requirement_id
UNRELATED_INSTRUCTIONS = PACKING_LABEL_PILOT.requirements["instructions"]

AGENT_ROLES: tuple[tuple[AgentIdentity, str, str], ...] = (
    (AgentIdentity.CHANGE_INTELLIGENCE, "Change Intelligence", "READ / ANALYZE"),
    (AgentIdentity.REMEDIATION, "Remediation", "SCOPED WRITE"),
    (AgentIdentity.ENABLEMENT, "Frontline Enablement", "DELIVER"),
    (AgentIdentity.FIELD_VERIFICATION, "Field Verification", "OBSERVE"),
)

PRODUCT_MODULES: tuple[dict[str, str], ...] = (
    {"id": "mission", "label": "Mission Control", "status": "ACTIVE"},
    {"id": "fleet", "label": "Agent Fleet", "status": "ACTIVE"},
    {"id": "security", "label": "Security", "status": "ACTIVE"},
    {"id": "evidence", "label": "Evidence", "status": "PARTIAL"},
    {"id": "frontline", "label": "Frontline", "status": "ACTIVE"},
    {"id": "field", "label": "Field Verification", "status": "ACTIVE"},
    {"id": "proof", "label": "Change Proof", "status": "NOT WIRED"},
    {"id": "coverage", "label": "Coverage", "status": "NOT WIRED"},
)

FUTURE_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "group": "Frontline",
        "status": "PARTIAL",
        "milestone": "Composition, proven delivery, and acknowledgment are live",
        "items": [
            "Teach the Delta — LIVE",
            "Proven delivery receipt — LIVE",
            "Worker acknowledgment — LIVE",
            "Micro-training — NOT WIRED",
        ],
    },
    {
        "group": "Field Verification",
        "status": "PARTIAL",
        "milestone": "Observation is live; the deterministic verdict is not",
        "items": [
            "Photo upload — LIVE",
            "Actual-bytes MIME detection — LIVE",
            "Gemma 4 observation — LIVE",
            "LEFT / TOP_RIGHT / INCONCLUSIVE — LIVE",
            "Deterministic PASS / FAIL — NOT WIRED",
        ],
    },
    {
        "group": "Change Proof",
        "status": "NOT WIRED",
        "milestone": "Proof generation exists in M0; not wired to this slice",
        "items": [
            "Seven proof invariants",
            "Expected-vs-observed comparison",
            "Proof JSON + SHA-256",
            "Replay audit",
        ],
    },
    {
        "group": "Deployment Coverage",
        "status": "NOT WIRED",
        "milestone": "Requires frontline delivery + verification",
        "items": [
            "Affected artifacts",
            "Remediated artifacts",
            "Workers reached",
            "Workers verified",
            "Failures",
            "Deployment %",
        ],
    },
    {
        "group": "AI Enhancements",
        "status": "NOT WIRED",
        "milestone": "Optional track (M4)",
        "items": ["Veo micro-training video", "Audio micro-learning"],
    },
    {
        "group": "Security",
        "status": "PARTIAL",
        "milestone": "Tool-permission denial is live; the rest is not",
        "items": [
            "Tool permission denial — LIVE",
            "Scope protection — LIVE",
            "Prompt injection signal — NOT WIRED",
        ],
    },
)


def _classification() -> DataClassification:
    return DataClassification(labels=[ClassificationLabel.SYNTHETIC])


def _change_from_case(case: ChangeCase) -> ApprovedChange:
    return ApprovedChange(
        change_id=case.change_id,
        source_procedure_id=case.source_procedure_id,
        source_version=case.source_version,
        previous_version=case.previous_version,
        operation_id=case.operation_id,
        requirement_id=case.requirement_id,
        previous_value=case.previous_value,
        current_value=case.current_value,
        authorized_scope=[case.artifact_id],
        approved_status="APPROVED",
        source_evidence_ref=case.source_evidence_ref,
        received_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        data_classification=_classification(),
    )


def _artifact_from_case(case: ChangeCase) -> DownstreamArtifact:
    return DownstreamArtifact(
        artifact_id=case.artifact_id,
        artifact_type=case.artifact_type,
        operation_id=case.operation_id,
        requirement_id=case.requirement_id,
        current_value=case.previous_value,
        content_ref=f"local://artifacts/{case.artifact_id}",
        authorized_for_remediation=True,
        requirements=dict(case.requirements),
        data_classification=_classification(),
    )


@dataclass
class _Session:
    """One demo session. Reset creates a new one rather than undoing side effects."""

    session_id: str
    case: ChangeCase
    change: ApprovedChange
    ledger: ActionLedger
    repository: InMemoryArtifactRepository
    broker: CapabilityBroker
    channel: LocalPilotDeliveryChannel
    field_store: FieldEvidenceStore
    action_id: str
    delivery_action_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    remediation: dict[str, Any] | None = None
    crossing: dict[str, Any] | None = None
    security: dict[str, Any] | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    delta: DeltaInstruction | None = None
    acknowledgment: FrontlineAcknowledgment | None = None
    validated_execution: dict[str, Any] | None = None
    """The original validated execution. Never overwritten by an idempotent replay."""
    delivery: dict[str, Any] | None = None
    delta_composed_at: datetime | None = None
    workflow: Workflow | None = None
    verdict: dict[str, Any] | None = None
    verification_events: list[Any] = field(default_factory=list)
    """Append-only. Every attempt is retained, FAIL and INCONCLUSIVE included."""
    field_verification: dict[str, Any] | None = None
    field_attempts: list[dict[str, Any]] = field(default_factory=list)
    known_submission_ids: set[str] = field(default_factory=set)
    provider_calls: int = 0
    """Billable model calls made in this session. Counted, never estimated."""


class HeroConsoleService:
    """Safe, narrow use cases over the real DRIFTZERO path.

    Every method returns plain projections. The service exposes no capability minting,
    no tool invocation, and no ledger primitive to its caller.
    """

    def __init__(self, case: ChangeCase = PACKING_LABEL_PILOT) -> None:
        self._case = case
        self._sequence = 0
        self._session = self._new_session()

    # ------------------------------------------------------------------ session

    def _new_session(self) -> _Session:
        self._sequence += 1
        case = self._case
        change = _change_from_case(case)
        artifact = _artifact_from_case(case)
        session = _Session(
            session_id=f"session-{self._sequence:03d}",
            case=case,
            change=change,
            ledger=ActionLedger(),
            repository=InMemoryArtifactRepository({case.artifact_id: artifact}),
            broker=CapabilityBroker(clock=lambda: datetime.now(UTC)),
            channel=LocalPilotDeliveryChannel(),
            field_store=FieldEvidenceStore(),
            action_id=derive_remediation_action_id(
                workflow_id=f"{WORKFLOW_ID}-{self._sequence:03d}",
                change=change,
                artifact_id=case.artifact_id,
            ),
            delivery_action_id=derive_delivery_action_id(
                workflow_id=f"{WORKFLOW_ID}-{self._sequence:03d}",
                change=change,
                worker_id=DESTINATION_REF,
            ),
        )
        session.workflow = Workflow(
            workflow_id=f"{WORKFLOW_ID}-{self._sequence:03d}",
            change_id=change.change_id,
            source_version=change.source_version,
            state=WorkflowState.CHANGE_RECEIVED,
            affected_artifact_id=case.artifact_id,
            candidate_artifact_refs=[case.artifact_id],
            worker_id=DESTINATION_REF,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data_classification=_classification(),
        )
        self._record(
            session, "CHANGE_CASE_LOADED", f"{case.change_id} — {case.source_name}"
        )
        return session

    def _advance(self, session: _Session, target: WorkflowState) -> None:
        """Advance the workflow through the frozen state machine, or leave it alone.

        Structural legality is the state machine's call, never this service's. An
        illegal request is skipped rather than forced, so a presentation layer can
        never push the workflow somewhere the domain forbids.
        """
        workflow = session.workflow
        if workflow is None or not can_transition(workflow.state, target):
            return
        session.workflow = transition(workflow, target, occurred_at=datetime.now(UTC))

    def _record(self, session: _Session, event: str, detail: str) -> None:
        session.events.append(
            {
                "sequence": len(session.events) + 1,
                "event": event,
                "detail": detail,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )

    def start_new_session(self) -> dict[str, Any]:
        """Begin a new change session.

        Never claims a dispatched action was undone: a new session gets a new ledger, a
        new repository, and a new action identity.
        """
        self._session = self._new_session()
        return self.get_state()

    # Retained name so existing callers keep working.
    reset_demo = start_new_session

    # ------------------------------------------------------------------ projections

    def _artifact_view(self, session: _Session) -> dict[str, Any]:
        artifact = session.repository.read(session.case.artifact_id)
        return {
            "artifact_id": artifact.artifact_id,
            "content_ref": artifact.content_ref,
            "content_hash": artifact_content_hash(artifact),
            "requirements": dict(artifact.requirements),
            "authorized_for_remediation": artifact.authorized_for_remediation,
        }

    def _environment_view(self) -> dict[str, Any]:
        environment = current_environment()
        production = environment is Environment.PRODUCTION
        return {
            "presentation_environment": str(environment),
            "environment": str(environment),
            "is_production": production,
            # Presentation mode is not readiness. This stays LOCAL_PILOT until
            # persistence, cloud deployment, and operational identity actually exist.
            "runtime_readiness": RUNTIME_READINESS,
            "production_ready": False,
            "identity_basis": "UNAUTHENTICATED_LOCAL_SESSION",
            # Customer-facing production hides unfinished modules rather than
            # advertising them; development keeps the diagnostics visible.
            "show_roadmap": not production,
            "show_diagnostics": not production,
            "session_action_label": "New Change" if production else "New Session",
            "control_verification_label": "Run Control Verification",
        }

    def _modules_view(self) -> list[dict[str, Any]]:
        """Production shows only what actually works."""
        if current_environment() is Environment.PRODUCTION:
            return [dict(m) for m in PRODUCT_MODULES if m["status"] == "ACTIVE"]
        return [dict(m) for m in PRODUCT_MODULES]

    def _policy_view(self) -> dict[str, Any]:
        return {
            "enforcement_model": ENFORCEMENT_MODEL,
            "platform_enforced_per_agent_identity": PLATFORM_ENFORCED_PER_AGENT_IDENTITY,
            "shared_runtime_service_account": SHARED_RUNTIME_SERVICE_ACCOUNT,
            "note": (
                "Application-level enforcement inside one process under a shared runtime "
                "identity. Not Google Cloud IAM and not GEAP Agent Identity."
            ),
            "entries": sorted(f"{i} → {t}" for i, t in AUTHORIZATION_POLICY),
        }

    def _fleet_view(self) -> list[dict[str, Any]]:
        """The capability matrix, derived cell by cell from AUTHORIZATION_POLICY.

        Every column is enumerated from ``ToolCapability`` and every cell answered by
        ``is_authorized``. Nothing is written down twice: adding a capability to the
        policy adds a column here, and no frontend asset carries a copy of the matrix.
        """
        return [
            {
                "identity": str(identity),
                "name": name,
                "role": role,
                "status": "OPERATIONAL",
                # Capability-specific, so "DENIED" can never read as "agent unavailable".
                "capabilities": [
                    {
                        "capability": str(tool),
                        "permission": (
                            "ALLOWED" if is_authorized(identity, tool) else "DENIED"
                        ),
                    }
                    for tool in ToolCapability
                ],
                "artifact_mutation": (
                    "ALLOWED"
                    if is_authorized(identity, ToolCapability.ARTIFACT_MUTATION)
                    else "DENIED"
                ),
            }
            for identity, name, role in AGENT_ROLES
        ]

    def _capability_columns(self) -> list[str]:
        """Column order for the fleet matrix. Derived from the enum, never a UI literal."""
        return [str(tool) for tool in ToolCapability]

    def get_state(self) -> dict[str, Any]:
        session = self._session
        change = session.change
        return {
            "session_id": session.session_id,
            "environment": self._environment_view(),
            "scenario": {
                "change_id": change.change_id,
                "source": session.case.source_name,
                "source_procedure_id": change.source_procedure_id,
                "previous_version": change.previous_version,
                "source_version": change.source_version,
                "requirement_id": change.requirement_id,
                "previous_value": change.previous_value,
                "current_value": change.current_value,
                "authorized_scope": list(change.authorized_scope),
                "action_id": session.action_id,
            },
            "artifact": self._artifact_view(session),
            "authorization": self._policy_view(),
            "fleet": self._fleet_view(),
            "remediation": session.remediation,
            "validated_execution": session.validated_execution,
            "crossing_2": session.crossing,
            "delivery": session.delivery,
            "frontline": self._frontline_view(session),
            "field_verification": self._field_verification_view(),
            "verdict": self._verdict_state_view(),
            "security_probe": {
                "identity": str(AgentIdentity.ENABLEMENT),
                "capability": str(ToolCapability.ARTIFACT_MUTATION),
            },
            "capability_columns": self._capability_columns(),
            "security": session.security,
            "timeline": list(session.events),
            "modules": self._modules_view(),
            "future_capabilities": (
                [dict(c) for c in FUTURE_CAPABILITIES]
                if self._environment_view()["show_roadmap"]
                else []
            ),
            "evidence_ids": sorted(session.evidence),
        }

    # ------------------------------------------------------------------ use cases

    def deploy_change(self) -> dict[str, Any]:
        """Run the real remediation path, then the real Crossing 2 validation."""
        session = self._session
        self._record(session, "REMEDIATION_REQUESTED", "Approved DZ-001 intent submitted")

        artifact = session.repository.read(session.case.artifact_id)
        intent = RemediationIntent(
            action_id=session.action_id,
            artifact_id=session.case.artifact_id,
            requirement_id=session.case.requirement_id,
            expected_before_value=session.change.previous_value,
            expected_before_hash=artifact_content_hash(artifact),
            expected_after_value=session.change.current_value,
            source_procedure_id=session.change.source_procedure_id,
            source_version=session.change.source_version,
            change_id=session.change.change_id,
            correlation_id=f"corr-{session.session_id}",
        )
        tool_context = MutationToolContext(
            ledger=session.ledger,
            repository=session.repository,
            capability=None,
            capability_verifier=session.broker.verify,
            workflow_id=WORKFLOW_ID,
            change=session.change,
            source_version_applicable=True,
            data_classification=_classification(),
            clock=lambda: datetime.now(UTC),
        )

        result = RemediationAgent(broker=session.broker).remediate(intent, tool_context)

        if result.status is not RemediationStatus.CAPABILITY_DENIED:
            self._record(
                session,
                "AUTHORIZATION_GRANTED",
                f"{result.identity} → ARTIFACT_MUTATION",
            )

        session.remediation = {
            "status": str(result.status),
            "identity": result.identity,
            # Named by the server so no UI asset has to know the capability vocabulary.
            "capability": str(ToolCapability.ARTIFACT_MUTATION),
            "dispatched": result.dispatched,
            "dispatch_count": session.repository.dispatch_count,
            "enforcement_model": result.enforcement_model,
            "platform_enforced_per_agent_identity": (
                result.platform_enforced_per_agent_identity
            ),
            "denial_reason": result.denial_reason,
            "evidence_id": None,
            "remediation_type": None,
            "reconciled": None,
        }

        if result.status is RemediationStatus.MUTATED:
            self._record(session, "ARTIFACT_MUTATED", "label_position LEFT → TOP_RIGHT")
        elif result.status is RemediationStatus.ALREADY_COMPLETED:
            self._record(
                session,
                "IDEMPOTENT_REPLAY",
                "Action already completed — no duplicate dispatch",
            )

        if result.evidence is not None:
            evidence_id = f"remediation-evidence-{session.session_id}"
            session.evidence[evidence_id] = result.evidence.model_dump(mode="json")
            session.remediation["evidence_id"] = evidence_id
            session.remediation["remediation_type"] = result.evidence.remediation_type
            session.remediation["reconciled"] = getattr(result.evidence, "reconciled", None)
            self._validate(session, result.evidence)
            # Capture the first validated execution and keep it. An idempotent replay
            # reports ALREADY_COMPLETED; it must not blank the evidence panel, because
            # the original execution is still the authoritative thing that happened.
            if session.validated_execution is None and session.crossing:
                session.validated_execution = {
                    "evidence_id": evidence_id,
                    "remediation_type": result.evidence.remediation_type,
                    "reconciled": getattr(result.evidence, "reconciled", None),
                    "dispatch_count": session.repository.dispatch_count,
                    "crossing_2": session.crossing["verdict"],
                    "accepted": session.crossing["accepted"],
                    "authoritative_before_hash": session.crossing[
                        "authoritative_before_hash"
                    ],
                    "authoritative_after_hash": session.crossing[
                        "authoritative_after_hash"
                    ],
                    "action_id": session.action_id,
                }
                # Real progress, driven by validated results rather than by a UI click.
                self._advance(session, WorkflowState.IMPACT_DETERMINED)
                self._advance(session, WorkflowState.REMEDIATION_PENDING)
                self._advance(session, WorkflowState.REMEDIATION_COMPLETED)
                self._compose_delta(session)

        return self.get_state()

    def _validate(self, session: _Session, evidence: Any) -> None:
        """Run Crossing 2 against the real authoritative context."""
        verdict = accept_remediation_evidence(
            evidence,
            context=RemediationCrossingContext(
                ledger=session.ledger,
                repository=session.repository,
                change=session.change,
                action_id=session.action_id,
                expected_artifact_id=session.case.artifact_id,
                expected_requirement_id=session.case.requirement_id,
                source_version_applicable=True,
                rejection_ref=f"rej-{session.session_id}",
            ),
        )
        crossing_id = f"crossing2-{session.session_id}"
        session.evidence[crossing_id] = {
            "accepted": verdict.accepted,
            "failed_layers": list(verdict.failed_layers),
            "rejections": [str(r) for r in verdict.rejections],
            "rejection_reason": verdict.rejection_reason,
            "authoritative_before_hash": verdict.authoritative_before_hash,
            "authoritative_after_hash": verdict.authoritative_after_hash,
            "evidence_ref": verdict.evidence_ref(),
        }
        session.crossing = {
            "verdict": "ACCEPTED" if verdict.accepted else "REJECTED",
            "accepted": verdict.accepted,
            "authoritative_before_hash": verdict.authoritative_before_hash,
            "authoritative_after_hash": verdict.authoritative_after_hash,
            "failed_layers": list(verdict.failed_layers),
            "rejections": [str(r) for r in verdict.rejections],
            "requires_review": verdict.requires_review,
            "evidence_id": crossing_id,
        }
        self._record(
            session,
            "CROSSING_2_ACCEPTED" if verdict.accepted else "CROSSING_2_REJECTED",
            "RemediationEvidence validated against authoritative state",
        )

    def run_security_test(self) -> dict[str, Any]:
        """Frontline Enablement attempts the mutation tool. It must be denied."""
        session = self._session
        self._record(
            session,
            "SECURITY_REQUESTED",
            "driftzero-enablement → ARTIFACT_MUTATION",
        )

        before = self._artifact_view(session)
        dispatches_before = session.repository.dispatch_count

        artifact = session.repository.read(session.case.artifact_id)
        intent = RemediationIntent(
            action_id=f"{session.action_id}-security-probe",
            artifact_id=session.case.artifact_id,
            requirement_id=session.case.requirement_id,
            expected_before_value=artifact.requirements[session.case.requirement_id],
            expected_before_hash=artifact_content_hash(artifact),
            expected_after_value=session.change.current_value,
            source_procedure_id=session.change.source_procedure_id,
            source_version=session.change.source_version,
            change_id=session.change.change_id,
            correlation_id=f"corr-security-{session.session_id}",
        )
        tool_context = MutationToolContext(
            ledger=session.ledger,
            repository=session.repository,
            capability=None,
            capability_verifier=session.broker.verify,
            workflow_id=WORKFLOW_ID,
            change=session.change,
            source_version_applicable=True,
            data_classification=_classification(),
            clock=lambda: datetime.now(UTC),
        )
        result = RemediationAgent(
            broker=session.broker, identity=AgentIdentity.ENABLEMENT
        ).remediate(intent, tool_context)

        after = self._artifact_view(session)
        record = result.denial_evidence
        evidence_id = f"denial-evidence-{session.session_id}"
        denial: dict[str, Any] = {}
        if record is not None:
            denial = {
                "denial_id": record.denial_id,
                "requested_by": record.requested_by,
                "requested_tool": record.requested_tool,
                "decision": record.decision,
                "reason_code": str(record.reason_code),
                "policy_basis": record.policy_basis,
                "enforcement_model": record.enforcement_model,
                "platform_enforced_per_agent_identity": (
                    record.platform_enforced_per_agent_identity
                ),
                "shared_runtime_service_account": record.shared_runtime_service_account,
                "dispatch_count_delta": record.dispatch_count_delta,
                "no_state_transition": record.no_state_transition,
            }
            session.evidence[evidence_id] = dict(denial)

        session.security = {
            "status": str(result.status),
            "probe_identity": str(AgentIdentity.ENABLEMENT),
            "probe_capability": str(ToolCapability.ARTIFACT_MUTATION),
            "denied": result.status is RemediationStatus.CAPABILITY_DENIED,
            "denial": denial,
            "evidence_id": evidence_id if denial else None,
            # Independently measured, not copied from the denial record.
            "artifact_hash_before": before["content_hash"],
            "artifact_hash_after": after["content_hash"],
            "artifact_hash_unchanged": before["content_hash"] == after["content_hash"],
            "dispatch_count_before": dispatches_before,
            "dispatch_count_after": session.repository.dispatch_count,
            "dispatch_count_unchanged": dispatches_before == session.repository.dispatch_count,
        }
        self._record(
            session,
            "SECURITY_DENIED" if session.security["denied"] else "SECURITY_UNEXPECTED_ALLOW",
            f"{result.identity} refused the mutation capability",
        )
        return self.get_state()

    # ------------------------------------------------------------------ frontline

    def _compose_delta(self, session: _Session) -> None:
        """Compose the operational delta once the change is validated.

        Composition happens only after Crossing 2 accepted the remediation: teaching a
        delta that was never validated would push unverified content to the floor.
        """
        artifact = session.repository.read(session.case.artifact_id)
        result = FrontlineEnablementAgent().compose_delta(
            change=session.change,
            artifact=artifact,
            instruction_id=f"delta-{session.change.change_id}-{session.session_id}",
            rationale=session.case.rationale,
        )
        if result.status is not DeltaStatus.COMPOSED or result.instruction is None:
            self._record(session, "DELTA_NOT_COMPOSED", result.reason or str(result.status))
            return
        session.delta = result.instruction
        session.delta_composed_at = datetime.now(UTC)
        session.evidence[result.instruction.instruction_id] = result.instruction.model_dump(
            mode="json"
        )
        self._record(
            session,
            "DELTA_COMPOSED",
            f"{result.instruction.requirement_id}: "
            f"{result.instruction.before_value} → {result.instruction.after_value}",
        )

    def _frontline_view(self, session: _Session) -> dict[str, Any]:
        delta = session.delta
        ack = session.acknowledgment
        established = bool(session.delivery and session.delivery["delivery_established"])
        # The worker surface opens on validated delivery, not on composition. That is
        # what makes delivery a visible, real step rather than a label.
        return {
            "available": delta is not None and established,
            "composed": delta is not None,
            "delivery": session.delivery,
            "instruction": delta.model_dump(mode="json") if delta else None,
            "acknowledgment": ack.model_dump(mode="json") if ack else None,
            "acknowledged": bool(ack and ack.acknowledged),
            "delivery_established": established,
            "field_verification": self._field_verification_view()
            if session is self._session
            else None,
            "delivery_note": (
                "Delivery is established by a resolvable mechanism receipt validated at "
                "Crossing 3. Acknowledgment is a separate event recording that the "
                "instruction was read — it is not delivery and not a verification. "
                "Physical verification remains a further step against field evidence."
            ),
        }

    def get_frontline(self, change_id: str) -> dict[str, Any] | None:
        """Worker-facing payload for one change, or ``None`` if it is not this session."""
        session = self._session
        view = self._frontline_view(session)
        if change_id != session.change.change_id or not view["available"]:
            return None
        return {
            "change_id": session.change.change_id,
            "source_name": session.case.source_name,
            "previous_version": session.change.previous_version,
            "source_version": session.change.source_version,
            **view,
        }

    def acknowledge(self, change_id: str) -> dict[str, Any] | None:
        """Record a worker acknowledgment. Never a PASS, never a delivery receipt."""
        session = self._session
        if change_id != session.change.change_id or not self._frontline_view(
            session
        )["available"]:
            return None
        session.acknowledgment = FrontlineEnablementAgent().acknowledge(
            session.delta,
            operator_ref=f"local-session:{session.session_id}",
            identity_basis=(
                "UNAUTHENTICATED_LOCAL_SESSION — no enterprise employee identity "
                "system is integrated, so this acknowledgment identifies a session, "
                "not a named worker."
            ),
            occurred_at=datetime.now(UTC),
        )
        session.evidence[f"acknowledgment-{session.session_id}"] = (
            session.acknowledgment.model_dump(mode="json")
        )
        self._record(
            session,
            "FRONTLINE_ACKNOWLEDGED",
            "Operator confirmed reading the delta — not delivery, not verification",
        )
        return self.get_frontline(change_id)

    def deliver_to_frontline(self) -> dict[str, Any]:
        """Deliver the composed delta through the pilot channel, then validate it.

        Idempotent on the stable ``DELIVER_DELTA`` action identity: a repeat request
        resolves the existing delivery rather than dispatching a second one, and an
        uncertain dispatch is reconciled against the mechanism's receipt instead of
        being blindly re-sent.
        """
        session = self._session
        if session.delta is None:
            self._record(session, "DELIVERY_REJECTED", "no composed delta to deliver")
            return self.get_state()

        decision = decide_retry(session.ledger, session.delivery_action_id)
        if decision is RetryDecision.ALREADY_COMPLETED:
            self._record(
                session, "DELIVERY_ALREADY_ESTABLISHED", "no duplicate dispatch"
            )
            if session.delivery:
                session.delivery = {**session.delivery, "last_request": "ALREADY_DELIVERED"}
            return self.get_state()

        if decision is RetryDecision.RECONCILIATION_REQUIRED:
            outcome = reconcile_delivery(
                session.ledger,
                session.delivery_action_id,
                recoverable_receipt_ref=session.channel.recoverable_receipt_ref(
                    session.delta.instruction_id
                ),
                occurred_at=datetime.now(UTC),
            )
            self._record(
                session,
                "DELIVERY_RECONCILED"
                if outcome.delivered
                else "DELIVERY_UNCERTAIN_NO_RECEIPT",
                f"reconciliation returned {outcome.outcome}",
            )
            if not outcome.delivered:
                return self.get_state()

        self._record(session, "DELIVERY_REQUESTED", f"→ {DESTINATION_REF}")
        if session.ledger.get(session.delivery_action_id) is None:
            session.ledger.plan(
                action_id=session.delivery_action_id,
                workflow_id=WORKFLOW_ID,
                action_type=ActionType.DELIVER_DELTA,
                target_ref=DESTINATION_REF,
                intent={
                    "instruction_id": session.delta.instruction_id,
                    "change_id": session.change.change_id,
                    "channel": session.channel.channel,
                },
                occurred_at=datetime.now(UTC),
            )
        session.ledger.mark_attempted(
            session.delivery_action_id, occurred_at=datetime.now(UTC)
        )

        try:
            # The service asks the broker for the grant; the agent presents it and the
            # mechanism verifies it. Nothing here decides whether Enablement may deliver.
            grant = session.broker.issue_grant(
                holder=AgentIdentity.ENABLEMENT,
                tool=ToolCapability.FRONTLINE_DELIVERY,
                scope_ref=DESTINATION_REF,
                change_id=session.change.change_id,
                source_version=session.change.source_version,
            )
            dispatch = FrontlineEnablementAgent().deliver_delta(
                session.delta,
                channel=session.channel,
                destination_ref=DESTINATION_REF,
                occurred_at=datetime.now(UTC),
                grant=grant,
                grant_verifier=session.broker.grant_verifier(
                    ToolCapability.FRONTLINE_DELIVERY
                ),
            )
        except UncertainDeliveryError as exc:  # pragma: no cover - reconciliation path
            session.ledger.mark_failed_or_uncertain(
                session.delivery_action_id, occurred_at=datetime.now(UTC)
            )
            self._record(session, "DELIVERY_UNCERTAIN", str(exc))
            return self.get_state()

        verdict = accept_delivery_result(
            dispatch.result,
            context=DeliveryCrossingContext(
                channel=session.channel,
                instruction=session.delta,
                expected_destination_ref=DESTINATION_REF,
                rejection_ref=f"rej-delivery-{session.session_id}",
                composed_at=session.delta_composed_at,
            ),
        )

        receipt_id = f"delivery-receipt-{session.session_id}"
        if verdict.receipt is not None:
            session.evidence[receipt_id] = {
                "receipt_id": verdict.receipt.receipt_id,
                "instruction_id": verdict.receipt.instruction_id,
                "change_id": verdict.receipt.change_id,
                "channel": verdict.receipt.channel,
                "destination_ref": verdict.receipt.destination_ref,
                "payload_hash": verdict.receipt.payload_hash,
                "status": str(verdict.receipt.status),
                "issued_at": verdict.receipt.issued_at.isoformat(),
                "identity_basis": verdict.receipt.identity_basis,
                "evidence_ref": verdict.receipt.evidence_ref,
            }

        session.delivery = {
            "status": "ESTABLISHED" if verdict.accepted else "REJECTED",
            "delivery_established": verdict.delivery_established,
            "last_request": "DELIVERED" if verdict.accepted else "REJECTED",
            "crossing_3": "ACCEPTED" if verdict.accepted else "REJECTED",
            "receipt_id": verdict.receipt.receipt_id if verdict.receipt else None,
            "receipt_ref": verdict.receipt.evidence_ref if verdict.receipt else None,
            "receipt_integrity": "VALIDATED" if verdict.accepted else "NOT VALIDATED",
            "authoritative_payload_hash": verdict.authoritative_payload_hash,
            "channel": session.channel.channel,
            "destination_ref": DESTINATION_REF,
            "identity_basis": verdict.receipt.identity_basis if verdict.receipt else None,
            "dispatch_count": session.channel.dispatch_count,
            "failed_layers": list(verdict.failed_layers),
            "rejections": [str(r) for r in verdict.rejections],
            "evidence_id": receipt_id if verdict.receipt else None,
            "change_deployed": False,
            "field_verified": False,
        }

        if verdict.accepted:
            session.ledger.mark_completed(
                session.delivery_action_id,
                occurred_at=datetime.now(UTC),
                receipt_ref=verdict.receipt.evidence_ref,
                reconciled=False,
            )
            self._advance(session, WorkflowState.FRONTLINE_DELIVERY_COMPLETED)
            self._advance(session, WorkflowState.AWAITING_FIELD_VERIFICATION)
            self._record(
                session,
                "DELIVERY_ESTABLISHED",
                f"Crossing 3 accepted receipt {verdict.receipt.receipt_id}",
            )
        else:
            session.ledger.mark_failed_or_uncertain(
                session.delivery_action_id, occurred_at=datetime.now(UTC)
            )
            self._record(
                session, "DELIVERY_REJECTED", verdict.rejection_reason or "rejected"
            )
        return self.get_state()

    # ------------------------------------------------------------------ field evidence

    def submit_field_evidence(
        self,
        raw: bytes,
        *,
        declared_filename: str | None = None,
        declared_content_type: str | None = None,
    ) -> dict[str, Any]:
        """The single field-evidence use case. Mission Control and the worker share it.

        Both surfaces upload bytes to this one method; neither has a code path of its
        own. Duplicating verification logic per surface is how two surfaces end up
        disagreeing about what was observed.

        The caller supplies bytes and nothing else that matters. The filename and the
        browser Content-Type are recorded as *claims* and never consulted: the MIME type,
        the hash, the submission identity, the prompt, the model, and the agent identity
        are all derived server-side.
        """
        session = self._session
        self._record(session, "FIELD_EVIDENCE_SUBMITTED", f"{len(raw)} bytes received")

        try:
            image = accept_field_image(
                raw,
                declared_filename=declared_filename,
                declared_content_type=declared_content_type,
            )
        except ImageRejected as exc:
            session.field_verification = {
                "status": "REJECTED",
                "rejected": True,
                "rejection_reason": str(exc.reason),
                "detail": exc.detail,
                "history": list(session.field_attempts),
                **self._field_static_view(),
            }
            self._record(
                session, "FIELD_EVIDENCE_REJECTED", f"{exc.reason}: {exc.detail}"
            )
            return self.get_state()

        change = session.change
        submission_id = derive_submission_id(
            change_id=change.change_id,
            source_version=change.source_version,
            image_sha256=image.sha256,
        )
        operation_id = derive_observation_operation_id(
            change_id=change.change_id,
            source_version=change.source_version,
            image_sha256=image.sha256,
        )

        # Replay guard, before anything billable. The same image under the same change
        # and version is the same operation; repeating it must cost nothing.
        existing = session.field_store.find_operation(operation_id)
        if existing is not None and evidence_is_replayable(existing):
            self._record(
                session,
                "FIELD_OBSERVATION_REPLAYED",
                "identical submission — no additional provider call",
            )
            previous = session.field_verification or {}
            session.field_verification = self._field_view(
                session,
                image=image,
                record=existing,
                status=ObservationStatus.REPLAYED,
                crossing=previous.get("crossing_4"),
            )
            return self.get_state()

        config = self._field_config()
        if not config.enabled or not has_field_observation_provider():
            session.field_verification = {
                "status": "PROVIDER_DISABLED",
                "rejected": False,
                "detail": (
                    "Live field observation is not configured for this instance, so no "
                    "observation was made. Nothing was assumed in its place."
                ),
                "history": list(session.field_attempts),
                **image.as_evidence(),
                **self._field_static_view(),
            }
            self._record(session, "FIELD_PROVIDER_DISABLED", "no observation was made")
            return self.get_state()

        try:
            provider = get_field_observation_provider(config)
        except Exception as exc:  # noqa: BLE001 - surfaced, never silently faked
            session.field_verification = {
                "status": "PROVIDER_UNAVAILABLE",
                "rejected": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "history": list(session.field_attempts),
                **image.as_evidence(),
                **self._field_static_view(),
            }
            self._record(session, "FIELD_PROVIDER_UNAVAILABLE", str(exc))
            return self.get_state()

        grant = session.broker.issue_grant(
            holder=AgentIdentity.FIELD_VERIFICATION,
            tool=ToolCapability.FIELD_OBSERVATION,
            scope_ref=change.change_id,
            change_id=change.change_id,
            source_version=change.source_version,
        )
        context = ObservationContext(
            change_id=change.change_id,
            source_version=change.source_version,
            submission_id=submission_id,
        )
        evidence_ref = session.field_store.evidence_ref(operation_id)

        session.provider_calls += 1
        result = FieldVerificationAgent().observe(
            image,
            raw,
            provider=provider,
            context=context,
            config=config,
            grant=grant,
            grant_verifier=session.broker.grant_verifier(
                ToolCapability.FIELD_OBSERVATION
            ),
            raw_evidence_ref=evidence_ref,
        )

        document = dict(result.evidence)
        document["operation_id"] = operation_id
        document["status"] = str(result.status)
        document["failure_reason"] = result.failure_reason
        stored_ref = session.field_store.record(
            operation_id=operation_id,
            document=document,
            recorded_at=datetime.now(UTC),
        )
        record = session.field_store.resolve(stored_ref) or document
        session.evidence[f"field-observation-{operation_id}"] = dict(record)
        session.field_attempts.append(
            {
                "operation_id": operation_id,
                "evidence_ref": stored_ref,
                "image_sha256": image.sha256,
                "mime_type": image.mime_type,
                "status": str(result.status),
                "observation": record.get("normalized_observation"),
                "attempt_count": result.attempt_count,
            }
        )

        crossing = None
        if result.succeeded and result.observation is not None:
            crossing, boundary = self._validate_observation(session, result.observation)
            if boundary is not None and boundary.accepted:
                self._adjudicate(session, boundary)
        else:
            self._record(
                session,
                "FIELD_OBSERVATION_FAILED",
                result.failure_reason or str(result.status),
            )

        session.known_submission_ids.add(submission_id)
        session.field_verification = self._field_view(
            session, image=image, record=record, status=result.status, crossing=crossing
        )
        return self.get_state()

    def _validate_observation(
        self, session: _Session, observation: Any
    ) -> tuple[dict[str, Any], Any]:
        """Run Crossing 4 against the independently stored provider evidence.

        Returns the projection *and* the boundary result. Only the boundary result may
        be handed to adjudication — the projection is for display, and a dict of strings
        must never be what a verdict is derived from.
        """
        stored = session.field_store.resolve(observation.raw_evidence_ref) or {}
        verdict = accept_field_observation(
            observation,
            context=ObservationCrossingContext(
                store=session.field_store,
                expected_change_id=session.change.change_id,
                expected_source_version=session.change.source_version,
                expected_submission_id=observation.submission_id,
                expected_image_sha256=stored.get("image_sha256", ""),
                authorized_identity=str(AgentIdentity.FIELD_VERIFICATION),
                rejection_ref=f"rej-observation-{session.session_id}",
                known_submission_ids=frozenset(session.known_submission_ids),
            ),
        )
        crossing_id = f"crossing4-{session.session_id}-{len(session.field_attempts)}"
        session.evidence[crossing_id] = {
            "accepted": verdict.accepted,
            "failed_layers": list(verdict.failed_layers),
            "rejections": [str(r) for r in verdict.rejections],
            "rejection_reason": verdict.rejection_reason,
            "evidence_ref": verdict.evidence_ref(),
        }
        self._record(
            session,
            "CROSSING_4_ACCEPTED" if verdict.accepted else "CROSSING_4_REJECTED",
            "FieldObservation validated against stored provider evidence",
        )
        return {
            "verdict": "ACCEPTED" if verdict.accepted else "REJECTED",
            "accepted": verdict.accepted,
            "failed_layers": list(verdict.failed_layers),
            "rejections": [str(r) for r in verdict.rejections],
            "requires_review": verdict.requires_review,
            "evidence_id": crossing_id,
        }, verdict

    def _adjudicate(self, session: _Session, boundary: Any) -> None:
        """Step 10 — hand the validated observation to the deterministic Truth Engine.

        This service supplies inputs and stores the outcome. It computes no verdict, and
        there is no expected/observed comparison anywhere in this file: the expected
        value is read from the approved change by the domain, and the comparison is the
        frozen T038 comparator reached through the frozen ingestion path.
        """
        workflow = session.workflow
        if workflow is None:  # pragma: no cover - a session always has one
            return

        # A second attempt after FAIL/INCONCLUSIVE must reopen the workflow first. The
        # state machine decides whether that is legal; this only asks.
        session.workflow = reopen_for_new_evidence(workflow, occurred_at=datetime.now(UTC))

        outcome = adjudicate_field_verification(
            VerdictContext(
                workflow=session.workflow,
                change=session.change,
                boundary=boundary,
                store=session.field_store,
                event_id=f"vev-{session.session_id}-{len(session.verification_events) + 1:03d}",
                occurred_at=datetime.now(UTC),
                data_classification=_classification(),
                existing_events=tuple(session.verification_events),
            )
        )

        if outcome.event is not None and not outcome.duplicate:
            session.verification_events.append(outcome.event)
            evidence_id = f"verification-event-{outcome.event.event_id}"
            session.evidence[evidence_id] = outcome.event.model_dump(mode="json")
        if outcome.workflow is not None:
            session.workflow = outcome.workflow

        session.verdict = self._verdict_view(session, outcome)
        self._record(
            session,
            f"VERIFICATION_{outcome.result}" if outcome.result else "VERDICT_NOT_REACHED",
            outcome.rejection_reason
            or (
                f"expected {outcome.expected_value} vs observed {outcome.observed_value}"
                f" -> {outcome.result}"
                + (" (already adjudicated)" if outcome.duplicate else "")
            ),
        )

    def _verdict_view(self, session: _Session, outcome: Any) -> dict[str, Any]:
        """Project the deterministic verdict for a UI. Adds no judgement of its own."""
        workflow = session.workflow
        return {
            "status": str(outcome.status),
            "adjudicated": outcome.adjudicated,
            "duplicate": outcome.duplicate,
            "result": str(outcome.result) if outcome.result else None,
            "expected_value": outcome.expected_value,
            "observed_value": outcome.observed_value,
            "authority": VERDICT_AUTHORITY,
            "observation_source": MODEL_OBSERVATION_SOURCE,
            "workflow_state": str(workflow.state) if workflow else None,
            "event_id": outcome.event.event_id if outcome.event else None,
            "event_sequence": outcome.event.event_sequence if outcome.event else None,
            "evidence_id": (
                f"verification-event-{outcome.event.event_id}" if outcome.event else None
            ),
            "rejection_reason": outcome.rejection_reason,
            # Derived from the frozen state table, never asserted by this layer.
            "change_verified": outcome.passed,
            "change_deployed": change_is_deployed(workflow),
            "proof_generated": bool(workflow and workflow.proof_id),
            "remaining_condition": remaining_condition_for(workflow),
            "history": list(verification_history(session.verification_events)),
        }

    def _verdict_state_view(self) -> dict[str, Any]:
        """The verdict panel before any adjudication, or the latest one after."""
        session = self._session
        if session.verdict is not None:
            return session.verdict
        workflow = session.workflow
        field = session.field_verification
        # EVALUATING is a real transient: the image was accepted and observed, but the
        # deterministic step has not produced an authoritative event.
        evaluating = bool(field and field.get("observation") and not session.verdict)
        return {
            "status": "EVALUATING" if evaluating else "AWAITING_EVIDENCE",
            "adjudicated": False,
            "duplicate": False,
            "result": None,
            "expected_value": None,
            "observed_value": None,
            "authority": VERDICT_AUTHORITY,
            "observation_source": MODEL_OBSERVATION_SOURCE,
            "workflow_state": str(workflow.state) if workflow else None,
            "event_id": None,
            "event_sequence": None,
            "evidence_id": None,
            "rejection_reason": None,
            "change_verified": False,
            "change_deployed": change_is_deployed(workflow),
            "proof_generated": False,
            "remaining_condition": remaining_condition_for(workflow),
            "history": list(verification_history(session.verification_events)),
        }

    def _field_config(self) -> FieldProviderConfig:
        """Read configuration fresh, so live mode can be enabled without a rebuild."""
        return DriftZeroConfig.from_env().field_provider

    def _field_static_view(self) -> dict[str, Any]:
        """Field-verification facts that do not depend on a particular submission."""
        session = self._session
        config = self._field_config()
        return {
            "provider_configured": config.enabled and has_field_observation_provider(),
            "provider": config.as_disclosure(),
            "accepted_mime_types": sorted(
                MIME_BY_CONTAINER[c] for c in ACCEPTED_CONTAINERS
            ),
            "max_bytes": MAX_IMAGE_BYTES,
            # Restated on every payload so no surface can imply otherwise.
            # The approved expected value, read through the one accessor the domain
            # exposes. A surface may display it; nothing may supply it.
            "expected_value": authoritative_expected_value(session.change),
            # Read from the deterministic layer, never asserted here.
            "field_verified": bool(session.verdict and session.verdict["change_verified"]),
            "change_deployed": change_is_deployed(session.workflow),
            "deterministic_verdict": (
                session.verdict["result"] if session.verdict else None
            ),
            "verdict_authority": VERDICT_AUTHORITY,
            "observation_source": MODEL_OBSERVATION_SOURCE,
            "verdict_note": (
                "A model observation is not a verdict. The observation says what was "
                "seen; the deterministic comparator decides whether it matches the "
                "approved change."
            ),
        }

    def _field_view(
        self,
        session: _Session,
        *,
        image: Any,
        record: dict[str, Any],
        status: Any,
        crossing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Project one observation attempt for a UI. Adds no judgement of its own.

        ``observation`` is populated only once Crossing 4 accepted it. What the agent
        returned before validation is carried separately as ``observation_claimed``, so
        a surface cannot render an unvalidated claim as an observation by accident.
        """
        observation = record.get("normalized_observation")
        accepted = bool(crossing and crossing["accepted"])
        return {
            "status": str(status),
            "rejected": False,
            "observation": observation if accepted else None,
            "observation_claimed": observation,
            "inconclusive": accepted and observation == "INCONCLUSIVE",
            "crossing_4": crossing,
            "evidence_ref": record.get("evidence_ref"),
            "evidence_id": f"field-observation-{record.get('operation_id')}",
            "operation_id": record.get("operation_id"),
            "submission_id": record.get("submission_id"),
            "attempt_count": record.get("attempt_count"),
            "replayed": str(status) == str(ObservationStatus.REPLAYED),
            "provider_calls": session.provider_calls,
            "model": record.get("model"),
            "provider_name": record.get("provider"),
            "response_id": record.get("response_id"),
            "finish_reason": record.get("finish_reason"),
            "traffic_type": record.get("traffic_type"),
            "total_tokens": record.get("total_tokens"),
            "request_hash": record.get("request_hash"),
            "raw_response_hash": record.get("raw_response_hash"),
            "latency_seconds": record.get("latency_seconds"),
            "latency_label": record.get("latency_label"),
            "history": list(session.field_attempts),
            **image.as_evidence(),
            **self._field_static_view(),
        }

    def _field_verification_view(self) -> dict[str, Any]:
        """The field panel before any submission, or the latest attempt after one."""
        session = self._session
        if session.field_verification is not None:
            return session.field_verification
        return {
            "status": "AWAITING_EVIDENCE",
            "rejected": False,
            "observation": None,
            "crossing_4": None,
            "history": [],
            **self._field_static_view(),
        }

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self._session.evidence.get(evidence_id)
