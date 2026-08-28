"""Composition for the production API: live runtime plus durable persistence.

Both the HTTP routes (T094) and the Pub/Sub handler (T095) drive this one object, so
they cannot drift into two competing implementations of the same flow. It owns no
business logic of its own — every consequential step is delegated to the application
service, which delegates to the Truth Engine.

Two sources of workflow state, and the difference is reported rather than hidden:

* the **live runtime** holds services that can still be driven (verify, generate proof)
* the **durable store** (T092) holds state that survives a restart of this process

After a restart the live runtime is empty and the durable store is not, so status and
proof still resolve. An id neither knows is a 404 — never a freshly-minted default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from driftzero.config import DriftZeroConfig
from driftzero.models.workflow import STATE_CATEGORY, StateCategory, WorkflowState
from driftzero.truth_engine.idempotency import (
    ChangeEventDecision,
    ChangeEventOutcome,
    classify_change_event,
)
from driftzero_console.persistence import DurableSink, NullSink
from driftzero_console.service import HeroConsoleService
from driftzero_console.workflows import (
    FixtureRejected,
    WorkflowRegistry,
    dataset_from_fixture,
)

CHANGE_KEY_PREFIX = "change"
"""Namespace for the durable ``change_id`` claim, so an approved-change key can never
collide with an action or delivery key in the same collection."""


WORKFLOW_INPUTS = "workflow_inputs"
INPUT_SCHEMA_VERSION = 1


class NotResumable(RuntimeError):
    """This workflow exists and is durable, but must not be resumed.

    Distinct from :class:`WorkflowNotFound` on purpose. "I have no record of this" and
    "this finished, or needs a human" are different answers, and collapsing them would
    hide a terminal workflow behind a 404.
    """

    def __init__(self, workflow_id: str, reason: str) -> None:
        super().__init__(f"workflow {workflow_id} is not resumable: {reason}")
        self.workflow_id = workflow_id
        self.reason = reason


class ResumeHeldElsewhere(RuntimeError):
    """Another instance holds the resume lease for this workflow.

    Surfaced as the API's own type so the HTTP layer needs no cloud import to handle it,
    and so the transport never has to know that leases live in Firestore.
    """

    def __init__(self, workflow_id: str, holder: str, detail: str) -> None:
        super().__init__(detail)
        self.workflow_id = workflow_id
        self.holder = holder
        self.detail = detail


class WorkflowNotFound(LookupError):
    """No workflow with this id, in the live runtime or in durable storage.

    Raised rather than returning a default. "I have no record of this" and "here is a
    fresh workflow" are different answers and only one of them is true.
    """

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"no workflow {workflow_id!r}")
        self.workflow_id = workflow_id


@dataclass
class ApiRuntime:
    """The application seam both transports share."""

    fixtures_dir: Path
    config: DriftZeroConfig = field(default_factory=DriftZeroConfig.from_env)
    sink: DurableSink = field(default_factory=NullSink)
    persistence: Any | None = None
    """A ``FirestorePersistence`` when durable, otherwise ``None``. Typed loosely so
    this module never imports a Google SDK, keeping it importable with no cloud extra
    installed."""
    registry: WorkflowRegistry = field(default_factory=WorkflowRegistry)
    workflow_namespace: str = ""
    """Per-runtime workflow id prefix, unique by construction.

    It used to be the constant ``"wf-api"`` combined with a per-process counter, which
    made the first workflow of every process ``wf-api-001-001``. With one process that
    was invisible. Across Cloud Run instances — whose counters each start at 1 — two
    unrelated changes were issued the SAME workflow id, and in a shared durable store
    that means one document for two logical workflows."""
    lease_client: Any = None
    """A ``ResumeLeases`` when durable. Built lazily from the Firestore client."""
    instance_id: str = ""
    """Identifies this process in a lease. Cloud Run's revision when deployed."""
    _sequence: int = 0
    _leases_held: dict[str, Any] = field(default_factory=dict)
    _known_changes: dict[str, str] = field(default_factory=dict)
    """In-process ``change_id`` -> ``workflow_id``. A cache in front of the durable
    claim, never the authority: it is empty after a restart and the durable store is
    consulted regardless."""

    # ------------------------------------------------------------------ properties

    def __post_init__(self) -> None:
        import os
        import uuid

        # A revision name is shared by every instance serving it, so it identifies the
        # deployment rather than the process. The random suffix is what makes a lease
        # holder and a workflow namespace distinguishable between two live instances.
        process = uuid.uuid4().hex[:8]
        if not self.instance_id:
            revision = os.environ.get("K_REVISION")
            self.instance_id = f"{revision}-{process}" if revision else f"local-{process}"
        if not self.workflow_namespace:
            self.workflow_namespace = f"wf-{process}"

    @property
    def durable(self) -> bool:
        return bool(self.sink.durable and self.persistence is not None)

    @property
    def leases(self) -> Any:
        """Durable resume leases. An in-process lock could not span two instances."""
        if self.lease_client is None:
            from driftzero_cloud.leases import ResumeLeases  # noqa: PLC0415

            self.lease_client = ResumeLeases(self.persistence.client)
        return self.lease_client

    def readiness(self) -> dict[str, Any]:
        """What this process is actually configured for. Never aspirational.

        Deployment is read from the Cloud Run runtime contract (``K_SERVICE``,
        ``K_REVISION``), which only Cloud Run sets. It is deliberately not inferred from
        configuration: an environment variable saying ``firestore`` proves persistence
        is configured, not that anything was ever deployed.
        """
        import os  # noqa: PLC0415

        persistence = self.config.persistence
        service = os.environ.get("K_SERVICE")
        deployed = bool(service)
        durable = self.durable

        limitations: list[str] = []
        if self.fixtures_dir is not None:
            limitations.append(
                "the source-procedure corpus and artifact catalog are controlled pilot "
                "fixtures shipped with the image, not a live source registry; a "
                "production runtime would read them from Cloud Storage and Firestore"
            )
        if deployed:
            limitations.append(
                "a workflow recovered from durable storage can be read but not resumed "
                "in a new instance; resuming a persisted run is T097"
            )

        return {
            "ready": True,
            "persistence_backend": persistence.backend,
            "durable": durable,
            "evidence_bucket": persistence.evidence_bucket or None,
            "project": persistence.project or None,
            "missing_settings": list(persistence.missing_settings()),
            "deployment": "CLOUD_RUN" if deployed else "NOT_DEPLOYED",
            "revision": os.environ.get("K_REVISION"),
            "runtime_mode": "CLOUD_PILOT" if (deployed and durable) else "LOCAL_PILOT",
            # A deployment is not production readiness, and this must never drift into
            # claiming otherwise because the infrastructure got more real.
            "production_ready": False,
            "pilot_limitations": limitations,
        }

    # ------------------------------------------------------------------ idempotency

    def known_changes(self) -> dict[str, str]:
        """The ``change_id`` -> ``workflow_id`` map T029 classifies against.

        Durable claims win over the in-process cache, so a duplicate delivered after a
        restart still resolves to the original workflow.
        """
        return dict(self._known_changes)

    def _durable_owner(self, change_id: str) -> str | None:
        if not self.durable:
            return None
        return self.persistence.idempotency.owner_of(f"{CHANGE_KEY_PREFIX}-{change_id}")

    def classify(self, change_id: str) -> ChangeEventDecision:
        """Decide whether this ``change_id`` has already been accepted.

        The decision itself is T029's, in the Truth Engine. This method only assembles
        the evidence it judges on — the in-process cache plus the durable claim.
        """
        known = self.known_changes()
        durable_owner = self._durable_owner(change_id)
        if durable_owner is not None:
            known[change_id] = durable_owner

        # A minimal stand-in carrying only the field T029 reads. Constructing a full
        # ApprovedChange here would mean inventing source values we have not derived.
        probe = _ChangeIdProbe(change_id=change_id)
        return classify_change_event(probe, known)

    def _claim(self, change_id: str, workflow_id: str) -> None:
        self._known_changes[change_id] = workflow_id
        if self.durable:
            self.persistence.idempotency.claim(
                f"{CHANGE_KEY_PREFIX}-{change_id}", workflow_id
            )

    # ------------------------------------------------------------------ operations

    def accept_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ingest an approved change, or resolve an already-accepted one.

        Validation is ``dataset_from_fixture``'s (T081), which refuses any field that
        states a conclusion. Duplicate detection is T029's. Neither is reimplemented.
        """
        change_id = str(payload.get("change_id", "")).strip()
        if change_id:
            decision = self.classify(change_id)
            if decision.is_duplicate:
                existing = decision.existing_workflow_id
                return {
                    "workflow_id": existing,
                    "state": self.status(existing)["state"],
                    "duplicate_of": existing,
                    "outcome": str(ChangeEventOutcome.TRANSPORT_DUPLICATE),
                }

        dataset = dataset_from_fixture(payload, directory=self.fixtures_dir)
        self._sequence += 1
        service = HeroConsoleService(
            dataset=dataset,
            workflow_namespace=f"{self.workflow_namespace}-{self._sequence:03d}",
            persistence=self.sink,
        )
        workflow_id = self.registry.register(service)
        # Persist immediately: a workflow that exists only in this process is exactly
        # the T081 limitation durable persistence was added to close.
        self.sink.record_workflow(service._session.workflow, [])
        self._record_input(workflow_id, payload)
        self._claim(dataset.change_id, workflow_id)
        return {
            "workflow_id": workflow_id,
            "state": str(service._session.workflow.state),
            "duplicate_of": None,
            "outcome": str(ChangeEventOutcome.NEW_LOGICAL_CHANGE),
        }

    def _record_input(self, workflow_id: str, payload: dict[str, Any]) -> None:
        """Store the accepted source change beside the workflow.

        Resuming in a fresh instance means rebuilding the application service, and the
        service is built from the source change. Without the original payload a new
        instance could only guess at it, and a guessed input is a different workflow.
        """
        if not self.durable:
            return
        from driftzero_cloud.serialization import safe_identifier  # noqa: PLC0415

        self.persistence.client.collection(WORKFLOW_INPUTS).document(
            safe_identifier(workflow_id, kind="workflow_id")
        ).set(
            {
                "schema_version": INPUT_SCHEMA_VERSION,
                "kind": "workflow_input",
                "workflow_id": workflow_id,
                "payload": payload,
            }
        )

    def _load_input(self, workflow_id: str) -> dict[str, Any] | None:
        if not self.durable:
            return None
        from driftzero_cloud.serialization import safe_identifier  # noqa: PLC0415

        snapshot = (
            self.persistence.client.collection(WORKFLOW_INPUTS)
            .document(safe_identifier(workflow_id, kind="workflow_id"))
            .get()
        )
        if not snapshot.exists:
            return None
        document = snapshot.to_dict() or {}
        version = document.get("schema_version")
        if version != INPUT_SCHEMA_VERSION:
            raise NotResumable(
                workflow_id,
                f"stored input schema_version {version!r} is not readable by this build "
                f"(expects {INPUT_SCHEMA_VERSION})",
            )
        return document.get("payload")

    def resume_eligibility(self, workflow_id: str) -> dict[str, Any]:
        """Whether this workflow may be resumed, and why not when it may not.

        The categories come from the frozen M0 ``STATE_CATEGORY`` map rather than a
        second list maintained here — a duplicate list is how two parts of a system
        start disagreeing about which states are terminal.
        """
        record = self._durable_record(workflow_id)
        if record is None:
            raise WorkflowNotFound(workflow_id)

        state = record.workflow.state
        category = STATE_CATEGORY[state]
        if category is StateCategory.TERMINAL_SUCCESS:
            return {"eligible": False, "reason": "TERMINAL_SUCCESS", "state": str(state)}
        if category is StateCategory.TERMINAL_NON_SUCCESS:
            return {
                "eligible": False, "reason": "TERMINAL_NON_SUCCESS", "state": str(state)
            }
        if category is StateCategory.BLOCKING_GATE:
            return {
                "eligible": False,
                "reason": "BLOCKING_GATE_REQUIRES_HUMAN_REVIEW",
                "state": str(state),
            }
        return {"eligible": True, "reason": None, "state": str(state), "category": str(category)}

    def resume(self, workflow_id: str, *, owner: str | None = None) -> HeroConsoleService:
        """Reattach to a persisted workflow so it can be driven again.

        Returns the live service if this process already holds it. Otherwise rebuilds it
        from durable state under an exclusive lease, so two Cloud Run instances cannot
        both resume the same workflow.
        """
        existing = self.registry._services.get(workflow_id)
        if existing is not None:
            return existing
        if not self.durable:
            raise WorkflowNotFound(workflow_id)

        eligibility = self.resume_eligibility(workflow_id)
        if not eligibility["eligible"]:
            raise NotResumable(
                workflow_id,
                f"state {eligibility['state']} is {eligibility['reason']}",
            )

        payload = self._load_input(workflow_id)
        if payload is None:
            raise NotResumable(
                workflow_id, "no stored source change; the workflow cannot be rebuilt"
            )

        from driftzero_cloud.leases import LeaseDenied  # noqa: PLC0415

        try:
            lease = self.leases.acquire(workflow_id, owner or self.instance_id)
        except LeaseDenied as exc:
            raise ResumeHeldElsewhere(workflow_id, exc.holder, str(exc)) from exc
        try:
            service = self._rehydrate(workflow_id, payload)
        except Exception:
            self.leases.release(lease)
            raise
        self._leases_held[workflow_id] = lease
        return service

    def _rehydrate(self, workflow_id: str, payload: dict[str, Any]) -> HeroConsoleService:
        """Rebuild the service and overlay the persisted authoritative state.

        The service is constructed from the same source change, then its workflow,
        chronology and action ledger are replaced by what durable storage holds. Steps
        already executed are therefore *not* re-executed: the ledger says they happened,
        and the reconciliation rules that consume it are unchanged.
        """
        record = self._durable_record(workflow_id)
        dataset = dataset_from_fixture(payload, directory=self.fixtures_dir)
        service = HeroConsoleService(
            dataset=dataset, workflow_namespace=workflow_id, persistence=self.sink
        )
        session = service._session
        session.workflow = record.workflow
        session.state_history = [
            WorkflowState(state) for state in record.state_history
        ]
        # ActionLedger is frozen M0 and exposes no restore seam. The stored record IS
        # the authoritative one, so it is reinstated verbatim; replaying it through
        # plan()/mark_*() would mint fresh timestamps and produce a different record.
        # This reads an M0 private without modifying a single line of M0.
        for action in self.persistence.ledger_for(workflow_id).all_records():
            session.ledger._records[action.action_id] = action

        from driftzero_cloud.composition import RESUME_SNAPSHOTS  # noqa: PLC0415
        from driftzero_cloud.resume_snapshot import apply_snapshot  # noqa: PLC0415
        from driftzero_cloud.serialization import safe_identifier  # noqa: PLC0415

        snapshot = (
            self.persistence.client.collection(RESUME_SNAPSHOTS)
            .document(safe_identifier(workflow_id, kind="workflow_id"))
            .get()
        )
        if snapshot.exists:
            # Without this the workflow recovers but can never assemble a proof
            # context, which would be a resumability that stops one step short.
            apply_snapshot(session, snapshot.to_dict() or {})

        self.registry._services[workflow_id] = service
        return service

    def release(self, workflow_id: str) -> bool:
        """Give up the resume lease this process holds, if any."""
        lease = self._leases_held.pop(workflow_id, None)
        return self.leases.release(lease) if lease is not None else False

    def live_service(self, workflow_id: str) -> HeroConsoleService:
        """The service still able to be driven, or refuse.

        A workflow known only to durable storage can be *read* but not driven from
        here: resuming a persisted run is T097, and pretending otherwise would let the
        API silently start a second execution of the same logical change.
        """
        try:
            return self.registry.get(workflow_id)
        except Exception as exc:  # UnknownWorkflow — re-raised as the API's own type
            raise WorkflowNotFound(workflow_id) from exc

    def status(self, workflow_id: str) -> dict[str, Any]:
        """Current state, from the live runtime if present, else durable storage."""
        service = self.registry._services.get(workflow_id)
        if service is not None:
            workflow = service._session.workflow
            return _status_of(
                workflow,
                state_history=[str(s) for s in service._session.state_history],
                source="LIVE_RUNTIME",
                durable=self.durable,
            )

        record = self._durable_record(workflow_id)
        if record is None:
            raise WorkflowNotFound(workflow_id)
        return _status_of(
            record.workflow,
            state_history=list(record.state_history),
            source="DURABLE_STORE",
            durable=True,
        )

    def _durable_record(self, workflow_id: str) -> Any | None:
        if not self.durable:
            return None
        return self.persistence.workflows.load_record(workflow_id)

    def proof_document(self, workflow_id: str) -> dict[str, Any] | None:
        """The stored canonical proof, or ``None`` when the workflow has not earned one.

        One shape regardless of where the proof was read from. A route that returned a
        different document for a live workflow than for a recovered one would make the
        API's contract depend on server memory, which is exactly the property durable
        persistence was added to remove.
        """
        service = self.registry._services.get(workflow_id)
        if service is not None:
            document = service.get_proof_document()
            if document is not None:
                return document

        if self.durable:
            proof = self.persistence.proofs.find_workflow(workflow_id)
            if proof is not None:
                return _proof_document_of(proof)

        if service is None and self._durable_record(workflow_id) is None:
            raise WorkflowNotFound(workflow_id)
        return None

    def evidence(self, workflow_id: str) -> dict[str, Any]:
        """Evidence artifacts recorded for a workflow."""
        service = self.registry._services.get(workflow_id)
        workflow = None
        if service is not None:
            workflow = service._session.workflow
        else:
            record = self._durable_record(workflow_id)
            if record is None:
                raise WorkflowNotFound(workflow_id)
            workflow = record.workflow

        document = None
        try:
            document = self.proof_document(workflow_id)
        except WorkflowNotFound:  # pragma: no cover - resolved above
            document = None

        manifest = ((document or {}).get("document") or {}).get("evidence_manifest") or {}
        return {
            "workflow_id": workflow_id,
            "source_change_ref": manifest.get("source_change_ref"),
            "affected_artifact_ref": manifest.get("affected_artifact_ref"),
            "remediation_evidence_refs": list(manifest.get("remediation_evidence_refs", [])),
            "rejected_result_refs": list(manifest.get("rejected_result_refs", [])),
            "delivery_ref": manifest.get("delivery_ref"),
            "verification_refs": list(manifest.get("verification_refs", [])),
            "state_transition_refs": list(manifest.get("state_transition_refs", [])),
            "content_hashes": dict(manifest.get("content_hashes", {})),
            # Available before a proof exists, so an in-flight workflow can still show
            # what has been observed — including the failures.
            "verification_event_ids": [
                event.event_id for event in workflow.verification_events
            ],
            "complete": bool(manifest),
        }


def _proof_document_of(proof: Any) -> dict[str, Any]:
    """Build the same document ``HeroConsoleService.get_proof_document`` returns.

    ``canonical_json`` over the full model dump is by definition the byte string the
    proof was stored under — the Firestore adapter already verified that on read — so
    this reproduces the stored bytes rather than inventing a second encoding.
    """
    from driftzero.proof.store import (  # noqa: PLC0415
        DOWNLOAD_HASH_NOTE,
        HASH_MEANING,
        HASH_PREIMAGE_LABEL,
        ProofStore,
    )
    from driftzero.truth_engine.evidence import canonical_json  # noqa: PLC0415

    payload = proof.model_dump(mode="json")
    return {
        "proof_ref": ProofStore.proof_ref(proof.proof_id),
        "content_hash": proof.content_hash,
        "hash_meaning": HASH_MEANING,
        "hash_preimage": HASH_PREIMAGE_LABEL,
        "download_hash_note": DOWNLOAD_HASH_NOTE,
        "canonical_json": canonical_json(payload),
        "document": payload,
    }


@dataclass(frozen=True)
class _ChangeIdProbe:
    """The single field :func:`classify_change_event` reads.

    Passing this rather than a fabricated ``ApprovedChange`` keeps the boundary honest:
    at duplicate-detection time the only thing known about the delivery is its
    ``change_id``, and inventing source values to satisfy a constructor would be
    manufacturing input the caller never sent.
    """

    change_id: str


def _status_of(
    workflow: Any, *, state_history: list[str], source: str, durable: bool
) -> dict[str, Any]:
    return {
        "workflow_id": workflow.workflow_id,
        "change_id": workflow.change_id,
        "state": str(workflow.state),
        "affected_artifact_id": workflow.affected_artifact_id,
        "delivery_status": workflow.delivery_status,
        "latest_verification_status": (
            str(workflow.latest_verification_status)
            if workflow.latest_verification_status is not None
            else None
        ),
        "verification_results": [
            str(event.verification_result) for event in workflow.verification_events
        ],
        "proof_id": workflow.proof_id,
        "state_history": state_history,
        "source": source,
        "durable": durable,
    }


def build_runtime(
    *,
    fixtures_dir: Path,
    config: DriftZeroConfig | None = None,
    credentials: Any | None = None,
) -> ApiRuntime:
    """Construct the runtime the configuration asks for.

    ``memory`` (the default) builds no cloud client at all, so an offline test and the
    LOCAL_PILOT runtime never reach Google Cloud. The Firestore import happens inside
    the durable branch so the module stays importable without the ``cloud`` extra.
    """
    config = config or DriftZeroConfig.from_env()
    if not config.persistence.is_durable:
        return ApiRuntime(fixtures_dir=fixtures_dir, config=config)

    from driftzero_cloud.composition import build_sink  # noqa: PLC0415

    sink = build_sink(config, credentials=credentials)
    return ApiRuntime(
        fixtures_dir=fixtures_dir,
        config=config,
        sink=sink,
        persistence=getattr(sink, "firestore", None),
    )


__all__ = [
    "ApiRuntime",
    "FixtureRejected",
    "WorkflowNotFound",
    "build_runtime",
]
