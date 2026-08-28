"""Selecting a persistence backend from configuration.

The choice is made from :class:`~driftzero.config.PersistenceConfig`, never from ad-hoc
environment sniffing inside the adapter. ``DRIFTZERO_PERSISTENCE`` defaults to
``memory``, so an unconfigured process — every offline test, the M1 exit gate, the CLI
against LOCAL_PILOT — keeps its existing behaviour and never reaches Google Cloud.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from driftzero.config import PERSISTENCE_FIRESTORE, DriftZeroConfig, PersistenceConfig
from driftzero.models.action import ActionExecution
from driftzero.models.proof import ChangeProof
from driftzero.models.workflow import Workflow
from driftzero_cloud.firestore import FirestorePersistence
from driftzero_console.persistence import DurableSink, NullSink

RESUME_SNAPSHOTS = "resume_snapshots"


class FirestoreSink:
    """A :class:`DurableSink` backed by Firestore.

    Holds one revision per workflow so successive writes from this process use
    compare-and-set. A conflicting write from another writer surfaces as
    ``ConflictingRecord`` rather than silently winning.
    """

    durable = True

    def __init__(self, persistence: FirestorePersistence) -> None:
        self._persistence = persistence
        self._revisions: dict[str, int] = {}

    @property
    def firestore(self) -> FirestorePersistence:
        return self._persistence

    def record_workflow(self, workflow: Workflow, state_history: Sequence[str]) -> None:
        expected = self._revisions.get(workflow.workflow_id)
        revision = self._persistence.workflows.save(
            workflow, state_history=state_history, expected_revision=expected
        )
        self._revisions[workflow.workflow_id] = revision

    def record_action(self, action: ActionExecution) -> None:
        self._persistence.ledger_for(action.workflow_id).save(action)

    def record_proof(self, proof: ChangeProof) -> None:
        self._persistence.proofs.record(proof)

    def record_session(self, workflow_id: str, session: object) -> None:
        from driftzero_cloud.resume_snapshot import encode_snapshot  # noqa: PLC0415
        from driftzero_cloud.serialization import safe_identifier  # noqa: PLC0415

        self._persistence.client.collection(RESUME_SNAPSHOTS).document(
            safe_identifier(workflow_id, kind="workflow_id")
        ).set(encode_snapshot(session))


def build_sink(
    config: PersistenceConfig | DriftZeroConfig | None = None,
    *,
    credentials: Any | None = None,
) -> DurableSink:
    """Return the sink the configuration asks for.

    ``memory`` (the default) yields :class:`NullSink`, which stores nothing and reports
    ``durable`` False. ``firestore`` yields a live :class:`FirestoreSink` — and only
    after :meth:`PersistenceConfig.validated` has confirmed the configuration is
    complete, so a half-configured process fails loudly instead of writing somewhere
    unintended.
    """
    if config is None:
        config = DriftZeroConfig.from_env()
    persistence = config.persistence if isinstance(config, DriftZeroConfig) else config

    if persistence.backend != PERSISTENCE_FIRESTORE:
        return NullSink()

    persistence.validated()
    return FirestoreSink(
        FirestorePersistence.connect(
            project=persistence.project,
            database=persistence.database,
            credentials=credentials,
        )
    )
