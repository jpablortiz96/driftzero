"""Hero Console application service.

The browser never touches the domain. It calls this service, which owns one in-memory
demo session and delegates every consequential step to the **real** components already
built and tested:

* authorization policy and capability broker (T075)
* Remediation Agent (T074)
* Artifact Mutation Tool (T073)
* Crossing 2 validation (T076)

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
    MutationCapabilityBroker,
    ToolCapability,
    is_authorized,
)
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.orchestration import (
    RemediationCrossingContext,
    accept_remediation_evidence,
)
from driftzero.tools.artifact_mutation import (
    InMemoryArtifactRepository,
    MutationToolContext,
    artifact_content_hash,
)
from driftzero.truth_engine.actions import ActionLedger
from driftzero.truth_engine.idempotency import derive_remediation_action_id


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
    {"id": "proof", "label": "Change Proof", "status": "NOT WIRED"},
    {"id": "coverage", "label": "Coverage", "status": "NOT WIRED"},
)

FUTURE_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "group": "Frontline",
        "status": "PARTIAL",
        "milestone": "Delta composition and acknowledgment are live; delivery receipt is not",
        "items": [
            "Teach the Delta — LIVE",
            "Worker acknowledgment — LIVE",
            "Proven delivery receipt — NOT WIRED",
            "Micro-training — NOT WIRED",
        ],
    },
    {
        "group": "Field Verification",
        "status": "AWAITING MILESTONE",
        "milestone": "M3 — physical verification (G1 returned GO)",
        "items": [
            "Photo upload",
            "Gemma observation",
            "LEFT / TOP_RIGHT / INCONCLUSIVE",
            "Resubmission after FAIL",
        ],
    },
    {
        "group": "Change Proof",
        "status": "NOT WIRED",
        "milestone": "Proof generation exists in M0; not wired to this slice",
        "items": [
            "Seven proof invariants",
            "Deterministic PASS / FAIL",
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
    broker: MutationCapabilityBroker
    action_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    remediation: dict[str, Any] | None = None
    crossing: dict[str, Any] | None = None
    security: dict[str, Any] | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    delta: DeltaInstruction | None = None
    acknowledgment: FrontlineAcknowledgment | None = None
    validated_execution: dict[str, Any] | None = None
    """The original validated execution. Never overwritten by an idempotent replay."""


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
            broker=MutationCapabilityBroker(clock=lambda: datetime.now(UTC)),
            action_id=derive_remediation_action_id(
                workflow_id=f"{WORKFLOW_ID}-{self._sequence:03d}",
                change=change,
                artifact_id=case.artifact_id,
            ),
        )
        self._record(
            session, "CHANGE_CASE_LOADED", f"{case.change_id} — {case.source_name}"
        )
        return session

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
            "environment": str(environment),
            "is_production": production,
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
        return [
            {
                "identity": str(identity),
                "name": name,
                "role": role,
                "status": "OPERATIONAL",
                # Capability-specific, so "DENIED" can never read as "agent unavailable".
                "capabilities": [
                    {
                        "capability": str(ToolCapability.ARTIFACT_MUTATION),
                        "permission": (
                            "ALLOWED"
                            if is_authorized(identity, ToolCapability.ARTIFACT_MUTATION)
                            else "DENIED"
                        ),
                    }
                ],
                "artifact_mutation": (
                    "ALLOWED"
                    if is_authorized(identity, ToolCapability.ARTIFACT_MUTATION)
                    else "DENIED"
                ),
            }
            for identity, name, role in AGENT_ROLES
        ]

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
            "frontline": self._frontline_view(session),
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
        return {
            "available": delta is not None,
            "instruction": delta.model_dump(mode="json") if delta else None,
            "acknowledgment": ack.model_dump(mode="json") if ack else None,
            "acknowledged": bool(ack and ack.acknowledged),
            "delivery_established": False,
            "delivery_note": (
                "Acknowledgment records that the instruction was read. It does not "
                "establish delivery and is not a verification — physical verification "
                "is a separate step against real field evidence."
            ),
        }

    def get_frontline(self, change_id: str) -> dict[str, Any] | None:
        """Worker-facing payload for one change, or ``None`` if it is not this session."""
        session = self._session
        if change_id != session.change.change_id or session.delta is None:
            return None
        return {
            "change_id": session.change.change_id,
            "source_name": session.case.source_name,
            "previous_version": session.change.previous_version,
            "source_version": session.change.source_version,
            **self._frontline_view(session),
        }

    def acknowledge(self, change_id: str) -> dict[str, Any] | None:
        """Record a worker acknowledgment. Never a PASS, never a delivery receipt."""
        session = self._session
        if change_id != session.change.change_id or session.delta is None:
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

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self._session.evidence.get(evidence_id)
