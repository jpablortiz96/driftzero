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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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

WORKFLOW_ID = "wf-dz-001"
ARTIFACT_ID = "WI-114"
REQUIREMENT_ID = "label_position"
UNRELATED_INSTRUCTIONS = "Keep the LEFT support arm attached"

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
    {"id": "frontline", "label": "Frontline", "status": "NOT WIRED"},
    {"id": "proof", "label": "Change Proof", "status": "NOT WIRED"},
    {"id": "coverage", "label": "Coverage", "status": "NOT WIRED"},
)

FUTURE_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "group": "Frontline",
        "status": "COMING NEXT",
        "milestone": "T077 — Frontline Enablement Agent",
        "items": ["Teach the Delta", "Worker acknowledgment", "Micro-training"],
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


def _canonical_change() -> ApprovedChange:
    return ApprovedChange(
        change_id="DZ-001",
        source_procedure_id="PACKING-SOP",
        source_version="v14",
        previous_version="v13",
        operation_id="OP-PACK-01",
        requirement_id=REQUIREMENT_ID,
        previous_value="LEFT",
        current_value="TOP_RIGHT",
        authorized_scope=[ARTIFACT_ID],
        approved_status="APPROVED",
        source_evidence_ref="local://changes/DZ-001",
        received_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        data_classification=_classification(),
    )


def _canonical_artifact() -> DownstreamArtifact:
    return DownstreamArtifact(
        artifact_id=ARTIFACT_ID,
        artifact_type="work_instruction",
        operation_id="OP-PACK-01",
        requirement_id=REQUIREMENT_ID,
        current_value="LEFT",
        content_ref=f"local://artifacts/{ARTIFACT_ID}",
        authorized_for_remediation=True,
        requirements={
            REQUIREMENT_ID: "LEFT",
            "instructions": UNRELATED_INSTRUCTIONS,
            "packing_mode": "STANDARD",
        },
        data_classification=_classification(),
    )


@dataclass
class _Session:
    """One demo session. Reset creates a new one rather than undoing side effects."""

    session_id: str
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


class HeroConsoleService:
    """Safe, narrow use cases over the real DRIFTZERO path.

    Every method returns plain projections. The service exposes no capability minting,
    no tool invocation, and no ledger primitive to its caller.
    """

    def __init__(self) -> None:
        self._sequence = 0
        self._session = self._new_session()

    # ------------------------------------------------------------------ session

    def _new_session(self) -> _Session:
        self._sequence += 1
        change = _canonical_change()
        artifact = _canonical_artifact()
        session = _Session(
            session_id=f"session-{self._sequence:03d}",
            change=change,
            ledger=ActionLedger(),
            repository=InMemoryArtifactRepository({ARTIFACT_ID: artifact}),
            broker=MutationCapabilityBroker(clock=lambda: datetime.now(UTC)),
            action_id=derive_remediation_action_id(
                workflow_id=f"{WORKFLOW_ID}-{self._sequence:03d}",
                change=change,
                artifact_id=ARTIFACT_ID,
            ),
        )
        self._record(session, "DEMO_INITIALIZED", "Canonical DZ-001 scenario loaded")
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

    def reset_demo(self) -> dict[str, Any]:
        """Start a fresh session. Never claims a dispatched action was undone."""
        self._session = self._new_session()
        return self.get_state()

    # ------------------------------------------------------------------ projections

    def _artifact_view(self, session: _Session) -> dict[str, Any]:
        artifact = session.repository.read(ARTIFACT_ID)
        return {
            "artifact_id": artifact.artifact_id,
            "content_ref": artifact.content_ref,
            "content_hash": artifact_content_hash(artifact),
            "requirements": dict(artifact.requirements),
            "authorized_for_remediation": artifact.authorized_for_remediation,
        }

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
            "scenario": {
                "change_id": change.change_id,
                "source": "Packing SOP",
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
            "crossing_2": session.crossing,
            "security": session.security,
            "timeline": list(session.events),
            "modules": [dict(m) for m in PRODUCT_MODULES],
            "future_capabilities": [dict(c) for c in FUTURE_CAPABILITIES],
            "evidence_ids": sorted(session.evidence),
        }

    # ------------------------------------------------------------------ use cases

    def deploy_change(self) -> dict[str, Any]:
        """Run the real remediation path, then the real Crossing 2 validation."""
        session = self._session
        self._record(session, "REMEDIATION_REQUESTED", "Approved DZ-001 intent submitted")

        artifact = session.repository.read(ARTIFACT_ID)
        intent = RemediationIntent(
            action_id=session.action_id,
            artifact_id=ARTIFACT_ID,
            requirement_id=REQUIREMENT_ID,
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
                expected_artifact_id=ARTIFACT_ID,
                expected_requirement_id=REQUIREMENT_ID,
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

        artifact = session.repository.read(ARTIFACT_ID)
        intent = RemediationIntent(
            action_id=f"{session.action_id}-security-probe",
            artifact_id=ARTIFACT_ID,
            requirement_id=REQUIREMENT_ID,
            expected_before_value=artifact.requirements[REQUIREMENT_ID],
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

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self._session.evidence.get(evidence_id)
