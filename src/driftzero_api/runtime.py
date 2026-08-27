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
    workflow_namespace: str = "wf-api"
    _sequence: int = 0
    _known_changes: dict[str, str] = field(default_factory=dict)
    """In-process ``change_id`` -> ``workflow_id``. A cache in front of the durable
    claim, never the authority: it is empty after a restart and the durable store is
    consulted regardless."""

    # ------------------------------------------------------------------ properties

    @property
    def durable(self) -> bool:
        return bool(self.sink.durable and self.persistence is not None)

    def readiness(self) -> dict[str, Any]:
        """What this process is actually configured for. Never aspirational."""
        persistence = self.config.persistence
        return {
            "ready": True,
            "persistence_backend": persistence.backend,
            "durable": self.durable,
            "evidence_bucket": persistence.evidence_bucket or None,
            "project": persistence.project or None,
            "missing_settings": list(persistence.missing_settings()),
            "deployment": "NOT_DEPLOYED",
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
        self._claim(dataset.change_id, workflow_id)
        return {
            "workflow_id": workflow_id,
            "state": str(service._session.workflow.state),
            "duplicate_of": None,
            "outcome": str(ChangeEventOutcome.NEW_LOGICAL_CHANGE),
        }

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
