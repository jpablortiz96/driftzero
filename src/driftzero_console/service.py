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
from pathlib import Path
from typing import Any

from driftzero.agents.change_intel import (
    ChangeIntelligenceAgent,
    ReadOnlyTools,
)
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
from driftzero.agents.model_client import (
    ModelClientUnavailable,
    get_model_client,
    has_model_client_provider,
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
    BoundaryResult,
    DeliveryCrossingContext,
    ObservationCrossingContext,
    RemediationCrossingContext,
    accept_change_set,
    accept_delivery_result,
    accept_field_observation,
    accept_remediation_evidence,
)
from driftzero.proof.store import (
    DOWNLOAD_HASH_NOTE,
    HASH_MEANING,
    HASH_PREIMAGE_LABEL,
    ProofOutcome,
    ProofStore,
    attempt_proof,
    evaluate_eligibility,
    replay_audit,
)
from driftzero.sources.registry import (
    ArtifactCatalog,
    SourceChangeIngestion,
    SourceProcedureStore,
    SourceVersion,
    ingest_source_change,
    load_approved_change_record,
    load_artifact_catalog,
    load_source_version,
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
from driftzero.truth_engine.evidence import assemble_evidence_manifest, canonical_hash
from driftzero.truth_engine.idempotency import (
    derive_delivery_action_id,
    derive_remediation_action_id,
)
from driftzero.truth_engine.impact import (
    ImpactOutcome,
    qualify_candidates,
    resolve_cardinality,
)
from driftzero.truth_engine.proof_generator import ProofContext
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
    {"id": "proof", "label": "Change Proof", "status": "ACTIVE"},
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
        "group": "Change Intelligence",
        "status": "IMPLEMENTED",
        "milestone": "Google ADK + Gemini, Crossing 1, and the deterministic impact gate",
        "items": [
            "Source version diff — IMPLEMENTED",
            "Google ADK + Gemini proposal — IMPLEMENTED",
            "Crossing 1 validation — IMPLEMENTED",
            "Deterministic impact gate — IMPLEMENTED",
        ],
    },
    {
        "group": "Field Verification",
        "status": "IMPLEMENTED",
        "milestone": "Observation and the deterministic verdict are both implemented",
        "items": [
            "Photo upload — IMPLEMENTED",
            "Actual-bytes MIME detection — IMPLEMENTED",
            "Gemma 4 observation — IMPLEMENTED",
            "LEFT / TOP_RIGHT / INCONCLUSIVE — IMPLEMENTED",
            "Deterministic PASS / FAIL — IMPLEMENTED",
        ],
    },
    {
        "group": "Change Proof",
        "status": "IMPLEMENTED",
        "milestone": "Generated only through the frozen seven completion conditions",
        "items": [
            "Seven proof invariants — IMPLEMENTED",
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


def _proposal_hash(proposal: Any) -> str:
    """Canonical hash of the accepted proposal, via the frozen M0 helper.

    Binds the evidence to the exact structured output that was produced, so an auditor
    can tell two analyses of the same source apart.
    """
    return canonical_hash(proposal.model_dump(mode="json"))


def _classification() -> DataClassification:
    return DataClassification(labels=[ClassificationLabel.SYNTHETIC])


PILOT_DATA_DIR = Path(__file__).resolve().parents[2] / "pilot_data"
"""Where the pilot's real operational records live. Loaded as data, never inlined."""

PILOT_CHANGE_ID = "DZ-001"


@dataclass(frozen=True)
class PilotDataset:
    """Everything one change needs, all of it loaded rather than declared.

    The dataset carries two source versions and the whole downstream catalog. It does
    **not** carry an affected artifact: which artifact a change hits is discovered, not
    configured, and a dataset able to state it would defeat the point of discovering it.
    """

    change_id: str
    source_name: str
    previous: SourceVersion
    current: SourceVersion
    catalog: ArtifactCatalog
    authorized_scope: tuple[str, ...]
    approved_status: str


def load_pilot_dataset(root: Path = PILOT_DATA_DIR) -> PilotDataset:
    """Load the pilot's real source versions, catalog, and approval record."""
    procedures = root / "source_procedures"
    record = load_approved_change_record(root / "approved_changes.json", PILOT_CHANGE_ID)
    previous = load_source_version(
        procedures / f"packing_sop_{record['previous_version']}.json"
    )
    current = load_source_version(
        procedures / f"packing_sop_{record['source_version']}.json"
    )
    catalog = load_artifact_catalog(
        root / "artifact_catalog.json", data_classification=_classification()
    )
    return PilotDataset(
        change_id=record["change_id"],
        source_name=current.title,
        previous=previous,
        current=current,
        catalog=catalog,
        authorized_scope=tuple(record["authorized_scope"]),
        approved_status=record["approved_status"],
    )


def dataset_from_case(case: ChangeCase) -> PilotDataset:
    """Build a dataset from an explicit :class:`ChangeCase`.

    Keeps an arbitrary second case drivable end to end without a second data directory,
    while still going through the same derivation path: two source versions in, change
    derived from the diff, catalog searched for candidates.
    """
    common = {k: v for k, v in case.requirements.items() if k != case.requirement_id}
    previous = SourceVersion(
        source_procedure_id=case.source_procedure_id,
        version=case.previous_version,
        operation_id=case.operation_id,
        title=case.source_name,
        requirements={**common, case.requirement_id: case.previous_value},
    )
    current = SourceVersion(
        source_procedure_id=case.source_procedure_id,
        version=case.source_version,
        operation_id=case.operation_id,
        title=case.source_name,
        requirements={**common, case.requirement_id: case.current_value},
    )
    catalog = ArtifactCatalog(
        catalog_id=f"case-{case.change_id}",
        artifacts=(_artifact_from_case(case),),
    )
    return PilotDataset(
        change_id=case.change_id,
        source_name=case.source_name,
        previous=previous,
        current=current,
        catalog=catalog,
        authorized_scope=(case.artifact_id,),
        approved_status="APPROVED",
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
    dataset: PilotDataset
    change: ApprovedChange
    ingestion: SourceChangeIngestion
    source_store: SourceProcedureStore
    ledger: ActionLedger
    repository: InMemoryArtifactRepository
    broker: CapabilityBroker
    channel: LocalPilotDeliveryChannel
    field_store: FieldEvidenceStore
    delivery_action_id: str
    action_id: str | None = None
    """Derived only once a target qualifies. There is no action without a target."""
    intel: dict[str, Any] | None = None
    crossing_1: dict[str, Any] | None = None
    impact: dict[str, Any] | None = None
    qualified_artifact_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    remediation: dict[str, Any] | None = None
    """Set **only** when remediation actually executed. A refusal never lands here."""
    remediation_requests: list[dict[str, Any]] = field(default_factory=list)
    """Append-only history of every remediation request, refusals included."""
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
    proof_store: ProofStore = field(default_factory=ProofStore)
    proof: dict[str, Any] | None = None
    # The frozen ProofContext takes domain objects, not projections. A dict of strings
    # cannot satisfy an invariant, so the real records are retained alongside the views.
    impact_resolution: Any = None
    remediation_evidence: Any = None
    delivery_receipt_ref: str | None = None
    state_history: list[WorkflowState] = field(default_factory=list)
    rejected_result_refs: list[str] = field(default_factory=list)
    """Trust-boundary rejections, retained for audit. Retention is not endorsement."""
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

    def __init__(
        self,
        case: ChangeCase | None = None,
        *,
        pilot_data_dir: Path = PILOT_DATA_DIR,
    ) -> None:
        # No case given: run the real pilot dataset off disk. A case given: derive an
        # equivalent dataset so an arbitrary second change takes the identical path.
        self._case = case or PACKING_LABEL_PILOT
        self._dataset = (
            dataset_from_case(case) if case is not None else load_pilot_dataset(pilot_data_dir)
        )
        self._sequence = 0
        self._session = self._new_session()

    # ------------------------------------------------------------------ session

    def _new_session(self) -> _Session:
        """Step 1 — ingest a real source change. Impact stays undetermined.

        The change is *derived* from the diff of two retrieved source versions, and the
        repository is loaded from the whole catalog. Nothing here knows which artifact is
        affected: ``affected_artifact_id`` is None and ``delivery_action_id`` cannot be
        derived until a target exists.
        """
        self._sequence += 1
        dataset = self._dataset
        workflow_id = f"{WORKFLOW_ID}-{self._sequence:03d}"
        source_store = SourceProcedureStore()
        ingestion = ingest_source_change(
            change_id=dataset.change_id,
            previous=dataset.previous,
            current=dataset.current,
            authorized_scope=dataset.authorized_scope,
            approved_status=dataset.approved_status,
            received_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
            data_classification=_classification(),
            store=source_store,
        )
        change = ingestion.change
        session = _Session(
            session_id=f"session-{self._sequence:03d}",
            case=self._case,
            dataset=dataset,
            change=change,
            ingestion=ingestion,
            source_store=source_store,
            ledger=ActionLedger(),
            repository=InMemoryArtifactRepository(
                {a.artifact_id: a for a in dataset.catalog.artifacts}
            ),
            broker=CapabilityBroker(clock=lambda: datetime.now(UTC)),
            channel=LocalPilotDeliveryChannel(),
            field_store=FieldEvidenceStore(),
            delivery_action_id=derive_delivery_action_id(
                workflow_id=workflow_id, change=change, worker_id=DESTINATION_REF
            ),
        )
        session.workflow = Workflow(
            workflow_id=workflow_id,
            change_id=change.change_id,
            source_version=change.source_version,
            state=WorkflowState.CHANGE_RECEIVED,
            # Deliberately absent. Impact is a Truth Engine decision that has not been
            # made yet, and asserting it here would be the whole product lying at boot.
            affected_artifact_id=None,
            candidate_artifact_refs=[],
            worker_id=DESTINATION_REF,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data_classification=_classification(),
        )
        session.evidence[f"source-change-{session.session_id}"] = ingestion.as_evidence()
        self._record(
            session,
            "SOURCE_CHANGE_RECEIVED",
            f"{change.source_procedure_id} {ingestion.previous.version} → "
            f"{ingestion.current.version}: {ingestion.delta.requirement_id} "
            f"{ingestion.delta.previous_value} → {ingestion.delta.current_value}",
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
        # Condition 7 reads history, not just the current state, so every state the
        # workflow actually occupied is retained.
        session.state_history.append(workflow.state)
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

    def _artifact_view(self, session: _Session) -> dict[str, Any] | None:
        """The qualified target artifact, or None while impact is undetermined.

        Returning None is the honest answer before analysis: this deployment knows a
        catalog, not a target.
        """
        if session.qualified_artifact_id is None:
            return None
        return self._measure_artifact(session, session.qualified_artifact_id)

    def _measure_artifact(self, session: _Session, artifact_id: str) -> dict[str, Any]:
        """Read and hash one artifact by id, independent of impact qualification.

        The security probe needs a before/after measurement of whatever it targeted,
        which may be a catalog artifact no analysis has qualified.
        """
        artifact = session.repository.read(artifact_id)
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
                # Which runtime an agent uses says nothing about what it may do. Change
                # Intelligence gained a real ADK runtime and no operational capability.
                "semantic_runtime": self._semantic_runtime_for(identity),
            }
            for identity, name, role in AGENT_ROLES
        ]

    def _semantic_runtime_for(self, identity: AgentIdentity) -> str | None:
        """The model runtime an agent actually calls, or None when it calls none."""
        if identity is AgentIdentity.CHANGE_INTELLIGENCE:
            config = DriftZeroConfig.from_env().semantic_provider
            return f"Google ADK + Gemini {config.model}" if config.is_live else None
        if identity is AgentIdentity.FIELD_VERIFICATION:
            config = self._field_config()
            return f"Vertex AI MaaS + {config.model}" if config.is_live else None
        return None

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
                "affected_artifact_id": session.qualified_artifact_id,
                "impact_determined": session.qualified_artifact_id is not None,
                "remediation_available": session.qualified_artifact_id is not None,
            },
            "source": self._source_view(session),
            "intel": self._intel_state_view(),
            "crossing_1": session.crossing_1,
            "impact": session.impact,
            "artifact": self._artifact_view(session),
            "authorization": self._policy_view(),
            "fleet": self._fleet_view(),
            "remediation": session.remediation,
            "remediation_state": self._remediation_view(),
            "authorization_stage": self._authorization_view(),
            "capability_status": self._capability_status(),
            "proof": self._proof_state_view(),
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

    # ------------------------------------------------------------------ steps 2-3

    def analyze_change(self) -> dict[str, Any]:
        """Steps 2–3 — semantic impact proposal, Crossing 1, deterministic qualification.

        The model proposes candidates. The Truth Engine decides which, if any, is the
        authoritative target. Those are different jobs and this method keeps them apart:
        it never reads ``candidate.is_affected`` and never picks between qualified
        candidates.
        """
        session = self._session
        config = DriftZeroConfig.from_env().semantic_provider
        self._record(
            session,
            "IMPACT_ANALYSIS_REQUESTED",
            f"{session.change.source_procedure_id} "
            f"{session.change.previous_version} → {session.change.source_version}",
        )

        if not config.enabled or not has_model_client_provider():
            session.intel = {
                "status": "PROVIDER_DISABLED",
                "detail": (
                    "Live change intelligence is not configured for this instance, so no "
                    "analysis was performed. Nothing was assumed in its place."
                ),
                **self._intel_static_view(config),
            }
            self._record(session, "SEMANTIC_PROVIDER_DISABLED", "no analysis was made")
            return self.get_state()

        try:
            client = get_model_client(config.semantic)
        except (ModelClientUnavailable, Exception) as exc:  # noqa: BLE001
            session.intel = {
                "status": "PROVIDER_UNAVAILABLE",
                "detail": f"{type(exc).__name__}: {exc}",
                **self._intel_static_view(config),
            }
            self._record(session, "SEMANTIC_PROVIDER_UNAVAILABLE", str(exc))
            return self.get_state()

        catalog = session.dataset.catalog
        agent = ChangeIntelligenceAgent(
            client=client,
            config=config.semantic,
            # Both reads run before the model call and their results are inputs. Model
            # output can neither choose nor parameterize them.
            tools=ReadOnlyTools(
                read_approved_change=lambda cid: (
                    session.change if cid == session.change.change_id else None
                ),
                read_artifact_registry=lambda: list(catalog.artifacts),
            ),
        )
        result = agent.propose(session.change.change_id)

        provider_evidence = getattr(client, "last_call_evidence", None)
        session.intel = self._intel_view(session, result, config, provider_evidence)
        evidence_id = f"change-intelligence-{session.session_id}"
        session.evidence[evidence_id] = {
            **(provider_evidence.as_evidence() if provider_evidence else {}),
            "operation_id": f"intel-{session.session_id}",
            "change_id": session.change.change_id,
            "source_previous_hash": session.ingestion.previous.content_hash,
            "source_current_hash": session.ingestion.current.content_hash,
            "catalog_hash": catalog.catalog_hash,
            "catalog_size": len(catalog.artifacts),
            "status": str(result.status),
            "attempts": result.attempts,
            "repair_attempts_used": result.repair_attempts_used,
            "injection_markers_detected": list(result.injection_markers_detected),
            "unknown_artifact_ids": list(result.unknown_artifact_ids),
            "proposal_hash": _proposal_hash(result.proposal) if result.proposal else None,
            "candidate_count": (
                len(result.proposal.candidate_affected_artifacts) if result.proposal else 0
            ),
            "authoritative": False,
        }
        session.intel["evidence_id"] = evidence_id

        if not result.succeeded:
            self._record(
                session, "IMPACT_ANALYSIS_FAILED", result.failure_reason or str(result.status)
            )
            self._review_required(session)
            return self.get_state()

        boundary = self._validate_change_set(session, result)
        if not boundary.accepted:
            self._review_required(session)
            return self.get_state()

        self._qualify_impact(session, boundary)
        return self.get_state()

    def _review_required(self, session: _Session) -> None:
        """Reach REVIEW_REQUIRED the way the frozen table permits.

        There is no CHANGE_RECEIVED -> REVIEW_REQUIRED edge, so the workflow passes
        through IMPACT_DETERMINED: analysis ran and its determination was that no
        autonomous path exists.
        """
        self._advance(session, WorkflowState.IMPACT_DETERMINED)
        self._advance(session, WorkflowState.REVIEW_REQUIRED)

    def _validate_change_set(
        self, session: _Session, result: Any
    ) -> BoundaryResult:
        """Crossing 1 — the frozen validator, against the authoritative change."""
        boundary = accept_change_set(
            result,
            change=session.change,
            known_artifact_ids=session.dataset.catalog.artifact_ids,
            source_version_applicable=True,
            rejection_ref=f"rej-changeset-{session.session_id}",
        )
        crossing_id = f"crossing1-{session.session_id}"
        session.evidence[crossing_id] = {
            "accepted": boundary.accepted,
            "failed_layers": list(boundary.failed_layers),
            "rejection_reason": boundary.rejection_reason,
        }
        session.crossing_1 = {
            "verdict": "ACCEPTED" if boundary.accepted else "REJECTED",
            "accepted": boundary.accepted,
            "failed_layers": list(boundary.failed_layers),
            "rejection_reason": boundary.rejection_reason,
            "evidence_id": crossing_id,
        }
        self._record(
            session,
            "CROSSING_1_ACCEPTED" if boundary.accepted else "CROSSING_1_REJECTED",
            "ChangeSet validated against the authoritative approved change",
        )
        return boundary

    def _qualify_impact(self, session: _Session, boundary: BoundaryResult) -> None:
        """The deterministic impact gate. The model's opinion is not consulted."""
        proposal = boundary.accepted_change_set
        catalog = session.dataset.catalog
        pairs = [
            (candidate, catalog.get(candidate.artifact_id))
            for candidate in proposal.candidate_affected_artifacts
        ]
        known = [(c, a) for c, a in pairs if a is not None]
        qualifications = qualify_candidates(known, session.change)
        resolution = resolve_cardinality(qualifications)

        session.workflow = session.workflow.model_copy(
            update={
                "candidate_artifact_refs": [c.artifact_id for c, _ in known],
                "impact_reason": resolution.outcome.value,
            }
        )

        session.impact_resolution = resolution
        session.impact = {
            "outcome": str(resolution.outcome),
            "qualified": resolution.outcome is ImpactOutcome.SINGLE_QUALIFIED_TARGET,
            "requires_review": resolution.requires_review,
            "affected_artifact_id": resolution.affected_artifact_id,
            "candidate_count": len(known),
            "qualified_count": len(resolution.qualified_artifact_ids),
            "qualified_artifact_ids": list(resolution.qualified_artifact_ids),
            "evaluated": [
                {
                    "artifact_id": q.artifact_id,
                    "qualified": q.qualified,
                    "failed_conditions": [str(c) for c in q.failed_conditions],
                    "agent_proposed_is_affected": q.agent_proposed_is_affected,
                    "agent_proposal_disagreed": q.agent_proposal_disagreed,
                }
                for q in qualifications
            ],
            "authority": "DRIFTZERO TRUTH ENGINE",
        }
        session.evidence[f"impact-{session.session_id}"] = dict(session.impact)

        if resolution.affected_artifact_id is None:
            session.qualified_artifact_id = None
            session.action_id = None
            self._record(
                session,
                "IMPACT_REVIEW_REQUIRED",
                f"{resolution.outcome}: {len(resolution.qualified_artifact_ids)} qualified",
            )
            # Impact *was* determined — the determination is "no unique target" — so the
            # workflow reaches REVIEW_REQUIRED through IMPACT_DETERMINED. The frozen
            # table has no CHANGE_RECEIVED -> REVIEW_REQUIRED edge, and forcing one
            # would be inventing a transition the domain does not define.
            self._advance(session, WorkflowState.IMPACT_DETERMINED)
            self._advance(session, WorkflowState.REVIEW_REQUIRED)
            return

        session.qualified_artifact_id = resolution.affected_artifact_id
        session.workflow = session.workflow.model_copy(
            update={"affected_artifact_id": resolution.affected_artifact_id}
        )
        # The action identity exists only now, because only now is there a target.
        session.action_id = derive_remediation_action_id(
            workflow_id=session.workflow.workflow_id,
            change=session.change,
            artifact_id=resolution.affected_artifact_id,
        )
        self._record(
            session,
            "IMPACT_QUALIFIED",
            f"exactly one qualified target: {resolution.affected_artifact_id}",
        )
        self._advance(session, WorkflowState.IMPACT_DETERMINED)

    # ------------------------------------------------------------------ remediation

    def deploy_change(self) -> dict[str, Any]:
        """Run the real remediation path, then the real Crossing 2 validation.

        Refuses to run before a qualified target exists. This is the server's gate, not
        a disabled button: a direct API call with no impact determination is rejected
        here, where it cannot be bypassed.
        """
        session = self._session
        if session.qualified_artifact_id is None or session.action_id is None:
            self._record(
                session,
                "REMEDIATION_BLOCKED",
                "no qualified impact target; run impact analysis first",
            )
            # A refusal is a *request*, not a remediation. It must never occupy the slot
            # that means "remediation executed" — that is what made a blocked attempt
            # render as an authorization grant with no identity behind it.
            session.remediation_requests.append(
                {
                    "sequence": len(session.remediation_requests) + 1,
                    "outcome": "BLOCKED_NO_QUALIFIED_TARGET",
                    "executed": False,
                    "reason": (
                        "impact had not yet been qualified when this request was made"
                    ),
                    "detail": (
                        "Remediation requires exactly one deterministically qualified "
                        "artifact. Impact analysis had not produced one."
                    ),
                    "dispatch_count": session.repository.dispatch_count,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            return self.get_state()

        target_id = session.qualified_artifact_id
        self._record(
            session,
            "REMEDIATION_REQUESTED",
            f"{session.change.change_id} → qualified target {target_id}",
        )

        artifact = session.repository.read(target_id)
        intent = RemediationIntent(
            action_id=session.action_id,
            artifact_id=target_id,
            requirement_id=session.change.requirement_id,
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
        session.remediation_requests.append(
            {
                "sequence": len(session.remediation_requests) + 1,
                "outcome": str(result.status),
                "executed": True,
                "reason": None,
                "detail": f"{result.identity} requested the mutation capability",
                "dispatch_count": session.repository.dispatch_count,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )

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
            session.remediation_evidence = result.evidence
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
                expected_artifact_id=session.qualified_artifact_id,
                expected_requirement_id=session.change.requirement_id,
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

        target_id = (
            session.qualified_artifact_id
            or session.dataset.catalog.artifacts[0].artifact_id
        )
        before = self._measure_artifact(session, target_id)
        dispatches_before = session.repository.dispatch_count

        artifact = session.repository.read(target_id)
        requirement_id = session.change.requirement_id
        intent = RemediationIntent(
            action_id=f"{session.action_id or 'probe'}-security-probe",
            artifact_id=target_id,
            requirement_id=requirement_id,
            expected_before_value=artifact.requirements.get(
                requirement_id, session.change.previous_value
            ),
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

        after = self._measure_artifact(session, target_id)
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
        artifact = session.repository.read(session.qualified_artifact_id)
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
            session.delivery_receipt_ref = verdict.receipt.evidence_ref
            # C4 reads these off the workflow, and only a Crossing-3-accepted receipt
            # may set them.
            session.workflow = session.workflow.model_copy(
                update={
                    "delivery_status": "DELIVERED",
                    "delivery_ref": verdict.receipt.evidence_ref,
                }
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
        workflow = session.workflow
        if session.verdict is not None:
            # The stored verdict is a record of one adjudication; the workflow keeps
            # moving after it (step 11 advances to PROOF_COMPLETE). State and deployment
            # are therefore re-read live rather than served from the cached projection.
            return {
                **session.verdict,
                "workflow_state": str(workflow.state) if workflow else None,
                "change_deployed": change_is_deployed(workflow),
                "proof_generated": bool(workflow and workflow.proof_id),
                "remaining_condition": remaining_condition_for(workflow),
            }
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

    def _remediation_view(self) -> dict[str, Any]:
        """Current remediation state, kept distinct from the request history.

        Three different questions, three different answers: has remediation executed
        (``remediation``), what was last *asked for* (``last_request``), and is the
        system ready to be asked (``state``). Collapsing them is what made a refused
        request read as a completed authorization.
        """
        session = self._session
        history = list(session.remediation_requests)
        last = history[-1] if history else None
        if session.remediation is not None:
            state = str(session.remediation["status"])
        elif session.qualified_artifact_id is not None:
            state = "AWAITING_REMEDIATION"
        else:
            state = "AWAITING_IMPACT_QUALIFICATION"
        return {
            "state": state,
            "executed": session.remediation is not None,
            "last_request": last,
            "blocked_request_count": sum(1 for r in history if not r["executed"]),
            "request_history": history,
            "note": (
                "A refused request is preserved as history. It is not the current "
                "remediation state and it never authorized anything."
            ),
        }

    def _authorization_view(self) -> dict[str, Any]:
        """The *operational* authorization stage. Eligibility is not a grant.

        Policy eligibility already appears in the Agent Fleet matrix, where it belongs.
        This says only whether a capability was actually obtained and used, which cannot
        be true until the Remediation Agent has run.
        """
        session = self._session
        executed = session.remediation
        if executed is None:
            return {
                "status": "PENDING",
                "granted": False,
                "identity": None,
                "capability": None,
                "detail": "Awaiting a remediation request. Policy eligibility is not a grant.",
            }
        denied = executed["status"] == str(RemediationStatus.CAPABILITY_DENIED)
        return {
            "status": "DENIED" if denied else "GRANTED",
            "granted": not denied,
            "identity": executed["identity"],
            "capability": executed["capability"],
            "detail": (
                "The capability was refused by the authorization policy."
                if denied
                else "A broker-issued capability was obtained and used."
            ),
        }

    def _capability_status(self) -> list[dict[str, Any]]:
        """Implementation, runtime, and operation as three separate dimensions.

        A capability can be built but unconfigured, or configured but not yet exercised.
        One status string cannot say which, and collapsing them is how a wired
        comparator ends up advertised as "NOT WIRED".
        """
        session = self._session
        field_config = self._field_config()
        semantic_config = DriftZeroConfig.from_env().semantic_provider
        verdict = session.verdict
        observation = session.field_verification

        def runtime(config: Any, registered: bool) -> str:
            if not config.enabled:
                return "DISABLED_THIS_SESSION"
            return "CONFIGURED" if registered else "NOT_AVAILABLE"

        return [
            {
                "id": "change_intelligence",
                "label": "Change intelligence",
                "implementation": "IMPLEMENTED",
                "runtime": runtime(semantic_config, has_model_client_provider()),
                "runtime_detail": semantic_config.as_disclosure()["runtime"]
                or "no semantic provider configured",
                "operation": (
                    str(session.intel["status"]) if session.intel else "AWAITING_ANALYSIS"
                ),
            },
            {
                "id": "impact_gate",
                "label": "Deterministic impact gate",
                "implementation": "IMPLEMENTED",
                "runtime": "DETERMINISTIC",
                "runtime_detail": "no model or network dependency",
                "operation": (
                    str(session.impact["outcome"])
                    if session.impact
                    else "AWAITING_PROPOSAL"
                ),
            },
            {
                "id": "field_observation",
                "label": "Field observation",
                "implementation": "IMPLEMENTED",
                "runtime": runtime(field_config, has_field_observation_provider()),
                "runtime_detail": (
                    f"{field_config.model} via Vertex AI MaaS"
                    if field_config.is_live
                    else "no field model provider configured"
                ),
                "operation": (
                    str(observation["status"]) if observation else "AWAITING_EVIDENCE"
                ),
            },
            {
                "id": "deterministic_verdict",
                "label": "Deterministic verdict",
                "implementation": "IMPLEMENTED",
                # Deterministic in itself; it needs a validated observation as *input*,
                # which is a dependency, not a missing implementation.
                "runtime": "DETERMINISTIC",
                "runtime_detail": "depends on a Crossing-4-validated FieldObservation",
                "operation": (
                    str(verdict["result"])
                    if verdict and verdict.get("result")
                    else "AWAITING_FIELD_OBSERVATION"
                ),
            },
            {
                "id": "change_proof",
                "label": "Change Proof",
                "implementation": "IMPLEMENTED",
                "runtime": "DETERMINISTIC",
                "runtime_detail": (
                    "gated by the seven frozen completion conditions"
                ),
                "operation": (
                    str(session.proof["status"]) if session.proof else "AWAITING_CONDITIONS"
                ),
            },
        ]

    def _intel_static_view(self, config: Any) -> dict[str, Any]:
        """Change-intelligence facts independent of any particular analysis."""
        return {
            "provider_configured": config.enabled and has_model_client_provider(),
            "provider": config.as_disclosure(),
            "runtime_label": (
                f"Gemini {config.model} · Google ADK" if config.is_live else None
            ),
            "identity": str(AgentIdentity.CHANGE_INTELLIGENCE),
            "authority": "READ / ANALYZE",
            "authority_note": (
                "The agent proposes candidates. It does not choose the affected "
                "artifact, authorize remediation, or set workflow state."
            ),
        }

    def _intel_view(
        self, session: _Session, result: Any, config: Any, provider_evidence: Any
    ) -> dict[str, Any]:
        """Project one analysis attempt. Adds no judgement of its own."""
        proposal = result.proposal
        return {
            "status": str(result.status),
            "succeeded": result.succeeded,
            "failure_reason": result.failure_reason,
            "attempts": result.attempts,
            "repair_attempts_used": result.repair_attempts_used,
            "injection_markers_detected": list(result.injection_markers_detected),
            "unknown_artifact_ids": list(result.unknown_artifact_ids),
            # Restated on every payload: a proposal is never authoritative.
            "authoritative": False,
            "requirement_id": proposal.requirement_id if proposal else None,
            "previous_value": proposal.previous_value if proposal else None,
            "current_value": proposal.current_value if proposal else None,
            "candidate_count": (
                len(proposal.candidate_affected_artifacts) if proposal else 0
            ),
            "candidates": (
                [
                    {
                        "artifact_id": c.artifact_id,
                        "impact_reason": c.impact_reason,
                        "agent_proposed_is_affected": c.is_affected,
                    }
                    for c in proposal.candidate_affected_artifacts
                ]
                if proposal
                else []
            ),
            "invocation_id": (
                provider_evidence.invocation_id if provider_evidence else None
            ),
            "model": provider_evidence.model if provider_evidence else None,
            "adk_version": provider_evidence.adk_version if provider_evidence else None,
            "total_tokens": (
                provider_evidence.total_tokens if provider_evidence else None
            ),
            "latency_seconds": (
                provider_evidence.latency_seconds if provider_evidence else None
            ),
            **self._intel_static_view(config),
        }

    def _intel_state_view(self) -> dict[str, Any]:
        """The impact panel before any analysis, or the latest attempt after one."""
        session = self._session
        if session.intel is not None:
            return session.intel
        config = DriftZeroConfig.from_env().semantic_provider
        return {
            "status": "PENDING",
            "succeeded": False,
            "authoritative": False,
            "candidate_count": 0,
            "candidates": [],
            **self._intel_static_view(config),
        }

    def _source_view(self, session: _Session) -> dict[str, Any]:
        """The real source material this change was derived from."""
        ingestion = session.ingestion
        return {
            **ingestion.as_evidence(),
            "source_name": session.dataset.source_name,
            "previous_resolves": session.source_store.resolve(
                ingestion.previous.content_ref
            )
            is not None,
            "current_resolves": session.source_store.resolve(
                ingestion.current.content_ref
            )
            is not None,
            "catalog_size": len(session.dataset.catalog.artifacts),
            "catalog_hash": session.dataset.catalog.catalog_hash,
            "evidence_id": f"source-change-{session.session_id}",
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

    # ------------------------------------------------------------------ step 11

    def _proof_context(self, session: _Session) -> ProofContext | None:
        """Assemble the frozen :class:`ProofContext` from authoritative session state.

        Returns ``None`` only when a *structural* input is genuinely absent — no
        workflow, no impact resolution, no remediation evidence. That is not an
        eligibility decision: the seven conditions are evaluated by M0, and this method
        never substitutes a placeholder to get past one.
        """
        workflow = session.workflow
        if workflow is None or session.impact_resolution is None:
            return None
        if session.remediation_evidence is None or not session.delivery_receipt_ref:
            return None

        manifest = assemble_evidence_manifest(
            source_change_ref=session.ingestion.current.content_ref,
            affected_artifact_ref=(
                session.repository.read(session.qualified_artifact_id).content_ref
                if session.qualified_artifact_id
                else ""
            ),
            remediation_evidence=session.remediation_evidence,
            delivery_ref=session.delivery_receipt_ref,
            verification_events=session.verification_events,
            state_transition_refs=[str(state) for state in session.state_history],
            rejected_result_refs=session.rejected_result_refs,
            extra_content_hashes={
                session.ingestion.current.content_ref: (
                    session.ingestion.current.content_hash
                )
            },
        )
        return ProofContext(
            workflow=workflow,
            change=session.change,
            impact=session.impact_resolution,
            remediation_evidence=session.remediation_evidence,
            manifest=manifest,
            verification_events=tuple(session.verification_events),
            state_history=tuple(session.state_history),
            source_version_applicable=True,
            delivery_receipt_ref=session.delivery_receipt_ref,
        )

    def generate_proof(self) -> dict[str, Any]:
        """Step 11 — generate the Change Proof, or explain exactly what blocks it.

        The gate is the frozen seven invariants. This method evaluates none of them: it
        assembles the authoritative context, hands it to :func:`attempt_proof`, and
        records what came back.
        """
        session = self._session
        self._record(session, "PROOF_REQUESTED", "evaluating completion conditions")

        context = self._proof_context(session)
        if context is None:
            session.proof = self._proof_unavailable_view(session)
            self._record(
                session, "PROOF_BLOCKED", "the workflow has not produced the required evidence"
            )
            return self.get_state()

        outcome = attempt_proof(context, store=session.proof_store)
        session.proof = self._proof_view(session, outcome)

        if not outcome.generated:
            self._record(session, "PROOF_BLOCKED", outcome.blocker_detail or "not eligible")
            return self.get_state()

        stored = outcome.stored
        session.evidence[f"change-proof-{stored.proof.proof_id}"] = stored.proof.model_dump(
            mode="json"
        )
        if outcome.replayed:
            self._record(
                session,
                "PROOF_ALREADY_COMPLETE",
                f"{stored.proof.proof_id} returned unchanged; no second proof exists",
            )
            return self.get_state()

        session.workflow = session.workflow.model_copy(
            update={"proof_id": stored.proof.proof_id}
        )
        self._advance(session, WorkflowState.PROOF_COMPLETE)
        session.proof = self._proof_view(session, outcome)
        self._record(
            session,
            "PROOF_COMPLETE",
            f"{stored.proof.proof_id} · {stored.content_hash[:16]}…",
        )
        return self.get_state()

    def _proof_unavailable_view(self, session: _Session) -> dict[str, Any]:
        """The panel before the workflow has produced the inputs a proof needs."""
        missing = []
        if session.impact_resolution is None:
            missing.append("impact has not been qualified")
        if session.remediation_evidence is None:
            missing.append("no remediation evidence exists")
        if not session.delivery_receipt_ref:
            missing.append("delivery has not been established")
        if not session.verification_events:
            missing.append("no authoritative verification has been recorded")
        return {
            "status": "BLOCKED",
            "generated": False,
            "replayed": False,
            "eligible": False,
            "satisfied_count": 0,
            "total": 7,
            "conditions": [],
            "blockers": missing,
            "blocker_detail": "; ".join(missing),
            "proof_ref": None,
            "proof_id": None,
            "content_hash": None,
            "hash_meaning": HASH_MEANING,
            "download_hash_note": DOWNLOAD_HASH_NOTE,
            "hash_preimage": HASH_PREIMAGE_LABEL,
            "change_deployed": False,
        }

    def _proof_view(self, session: _Session, outcome: ProofOutcome) -> dict[str, Any]:
        """Project a proof attempt. The invariant results come from M0, never a constant."""
        eligibility = outcome.eligibility
        stored = outcome.stored
        workflow = session.workflow
        return {
            "status": "PROOF_COMPLETE" if outcome.generated else "BLOCKED",
            "generated": outcome.generated,
            "replayed": outcome.replayed,
            "eligible": eligibility.eligible,
            "satisfied_count": eligibility.satisfied_count,
            "total": eligibility.total,
            "conditions": list(eligibility.conditions),
            "blockers": [
                entry["label"] for entry in eligibility.conditions if not entry["satisfied"]
            ],
            "blocker_detail": outcome.blocker_detail,
            "proof_ref": outcome.proof_ref,
            "proof_id": stored.proof.proof_id if stored else None,
            "content_hash": stored.content_hash if stored else None,
            "hash_meaning": HASH_MEANING,
            "download_hash_note": DOWNLOAD_HASH_NOTE,
            "hash_preimage": HASH_PREIMAGE_LABEL,
            "summary": stored.as_summary() if stored else None,
            "evidence_id": (
                f"change-proof-{stored.proof.proof_id}" if stored else None
            ),
            "change_deployed": change_is_deployed(workflow),
        }

    def _proof_state_view(self) -> dict[str, Any]:
        """The Change Proof panel: eligibility whenever it can honestly be computed."""
        session = self._session
        if session.proof is not None:
            return session.proof
        context = self._proof_context(session)
        if context is None:
            return self._proof_unavailable_view(session)
        eligibility = evaluate_eligibility(context)
        return {
            "status": "ELIGIBLE" if eligibility.eligible else "BLOCKED",
            "generated": False,
            "replayed": False,
            "eligible": eligibility.eligible,
            "satisfied_count": eligibility.satisfied_count,
            "total": eligibility.total,
            "conditions": list(eligibility.conditions),
            "blockers": [
                entry["label"] for entry in eligibility.conditions if not entry["satisfied"]
            ],
            "blocker_detail": None,
            "proof_ref": None,
            "proof_id": None,
            "content_hash": None,
            "hash_meaning": HASH_MEANING,
            "download_hash_note": DOWNLOAD_HASH_NOTE,
            "hash_preimage": HASH_PREIMAGE_LABEL,
            "change_deployed": change_is_deployed(session.workflow),
        }

    def get_proof_document(self) -> dict[str, Any] | None:
        """The stored canonical proof, resolved through its own reference."""
        session = self._session
        if session.workflow is None:
            return None
        stored = session.proof_store.find_workflow(session.workflow.workflow_id)
        if stored is None:
            return None
        return {
            "proof_ref": stored.proof_ref,
            "content_hash": stored.content_hash,
            "hash_meaning": HASH_MEANING,
            "hash_preimage": HASH_PREIMAGE_LABEL,
            "download_hash_note": DOWNLOAD_HASH_NOTE,
            # The stored bytes verbatim — the complete proof, content_hash included.
            # Deliberately NOT the hash preimage, which excludes that field.
            "canonical_json": stored.canonical_bytes,
            "document": stored.proof.model_dump(mode="json"),
        }

    def get_replay_audit(self) -> dict[str, Any] | None:
        """Render the recorded chronology. Executes nothing and dispatches nothing."""
        session = self._session
        if session.workflow is None:
            return None
        stored = session.proof_store.find_workflow(session.workflow.workflow_id)
        if stored is None:
            return None
        return replay_audit(
            stored=stored,
            verification_events=session.verification_events,
            timeline=session.events,
        )

    @property
    def current_change(self) -> ApprovedChange:
        """The approved change this session derived from its source versions."""
        return self._session.change

    @property
    def current_catalog(self) -> ArtifactCatalog:
        """The downstream artifacts this session will search. Never a shortlist."""
        return self._session.dataset.catalog

    def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self._session.evidence.get(evidence_id)
