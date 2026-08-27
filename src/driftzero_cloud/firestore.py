"""T092 — the Firestore persistence adapter.

Durable storage for workflows, the action ledger, proofs and idempotency keys.

What this module is *not*: an authority. It stores records the Truth Engine has already
decided and returns them unchanged. It never adjudicates a verdict, never advances a
state, never decides whether a retry is safe, and never computes a proof hash. Where a
question of correctness arises — a conflicting proof, a contested idempotency key — it
fails closed and raises, because the alternative is a persistence layer quietly choosing
which version of history is true.

Collection layout::

    workflows/{workflow_id}
    workflows/{workflow_id}/actions/{action_id}
    workflows/{workflow_id}/proofs/{proof_id}
    idempotency_keys/{stable_key}

The spec defines no collection names, so these follow the shape the M2 task brief gives.
Every path segment is checked by :func:`safe_identifier` before it reaches Firestore.

Authentication is Application Default Credentials locally and the attached
``driftzero-run-sa`` on Cloud Run. This module never reads a credential file, never
shells out to ``gcloud``, and never constructs a service-account key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import firestore

from driftzero.models.action import ActionExecution
from driftzero.models.proof import ChangeProof
from driftzero.models.workflow import Workflow
from driftzero_cloud.errors import CloudAdapterError, ConflictingRecord
from driftzero_cloud.serialization import (
    WorkflowRecord,
    decode_action,
    decode_proof,
    decode_workflow_record,
    encode_action,
    encode_proof,
    encode_workflow,
    safe_identifier,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

WORKFLOWS = "workflows"
ACTIONS = "actions"
PROOFS = "proofs"
IDEMPOTENCY_KEYS = "idempotency_keys"

LEGACY_PROJECT = "driftzero-agentic-2026"
"""Quarantined. The adapter refuses to open a client against it — a stale environment
variable must not be able to point durable writes at the wrong project."""


def build_client(
    *, project: str, database: str = "(default)", credentials: Any | None = None
) -> firestore.Client:
    """Construct a Firestore client, refusing the quarantined legacy project.

    ``credentials`` is a seam for tests and for explicitly-passed ADC. Left as ``None``,
    the SDK resolves Application Default Credentials itself; no key file is involved.
    """
    if not project or not project.strip():
        raise CloudAdapterError("a Firestore client requires an explicit project")
    if project == LEGACY_PROJECT:
        raise CloudAdapterError(
            f"refusing to open a Firestore client against the quarantined project "
            f"{LEGACY_PROJECT!r}"
        )
    return firestore.Client(project=project, database=database, credentials=credentials)


# ============================ workflows ===============================================


class FirestoreWorkflowStore:
    """Durable authoritative workflow state.

    A missing workflow reads back as ``None``. It is never invented: the whole point of
    durable state is that "I have no record of this" and "here is a fresh default" are
    different answers, and only one of them is true.
    """

    def __init__(self, client: firestore.Client) -> None:
        self._client = client

    def _ref(self, workflow_id: str) -> firestore.DocumentReference:
        return self._client.collection(WORKFLOWS).document(
            safe_identifier(workflow_id, kind="workflow_id")
        )

    def save(
        self,
        workflow: Workflow,
        *,
        state_history: Sequence[str] = (),
        expected_revision: int | None = None,
    ) -> int:
        """Persist the workflow and return the new revision.

        When ``expected_revision`` is supplied this is an optimistic-concurrency write:
        if the stored revision has moved on, the write is refused rather than clobbering
        whatever the other writer recorded.
        """
        ref = self._ref(workflow.workflow_id)

        if expected_revision is None:
            snapshot = ref.get()
            current = snapshot.to_dict().get("revision", 0) if snapshot.exists else 0
            revision = int(current) + 1
            ref.set(encode_workflow(workflow, state_history=state_history, revision=revision))
            return revision

        transaction = self._client.transaction()
        return _save_workflow_checked(
            transaction,
            ref=ref,
            workflow=workflow,
            state_history=state_history,
            expected_revision=expected_revision,
        )

    def load(self, workflow_id: str) -> Workflow | None:
        record = self.load_record(workflow_id)
        return None if record is None else record.workflow

    def load_record(self, workflow_id: str) -> WorkflowRecord | None:
        """The aggregate plus its state chronology and revision, or ``None``."""
        snapshot = self._ref(workflow_id).get()
        if not snapshot.exists:
            return None
        return decode_workflow_record(snapshot.to_dict())


@firestore.transactional
def _save_workflow_checked(
    transaction: firestore.Transaction,
    *,
    ref: firestore.DocumentReference,
    workflow: Workflow,
    state_history: Sequence[str],
    expected_revision: int,
) -> int:
    """Compare-and-set the workflow document inside a Firestore transaction.

    The read and the write are in one transaction, so two concurrent writers cannot both
    observe the same revision and both succeed.
    """
    snapshot = ref.get(transaction=transaction)
    current = int(snapshot.to_dict().get("revision", 0)) if snapshot.exists else 0
    if current != expected_revision:
        raise ConflictingRecord(
            f"{WORKFLOWS}/{workflow.workflow_id}",
            f"expected revision {expected_revision}, stored revision is {current}",
        )
    revision = current + 1
    transaction.set(
        ref, encode_workflow(workflow, state_history=state_history, revision=revision)
    )
    return revision


# ============================ action ledger ===========================================


class FirestoreActionLedger:
    """Durable action-execution records, one document per stable ``action_id``.

    Statuses are written as the Truth Engine reports them. This class does not decide
    whether ``PLANNED -> ATTEMPTED -> COMPLETED`` is legal, and it does not decide
    whether a retry is safe — ``truth_engine.actions`` owns both, and storing that
    decision here would create a second authority that could disagree with the first.
    """

    def __init__(self, client: firestore.Client, *, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = safe_identifier(workflow_id, kind="workflow_id")

    def _collection(self) -> firestore.CollectionReference:
        return (
            self._client.collection(WORKFLOWS)
            .document(self._workflow_id)
            .collection(ACTIONS)
        )

    def save(self, action: ActionExecution) -> None:
        """Write the record. Re-saving the same ``action_id`` updates its status.

        This is an update, not a duplicate: the document id *is* the action identity, so
        persisting the same action twice can never produce two ledger entries.
        """
        if action.workflow_id != self._workflow_id:
            raise CloudAdapterError(
                f"action {action.action_id} belongs to workflow "
                f"{action.workflow_id!r}, not {self._workflow_id!r}"
            )
        doc = self._collection().document(safe_identifier(action.action_id, kind="action_id"))
        doc.set(encode_action(action))

    def get(self, action_id: str) -> ActionExecution | None:
        snapshot = self._collection().document(
            safe_identifier(action_id, kind="action_id")
        ).get()
        if not snapshot.exists:
            return None
        return decode_action(snapshot.to_dict())

    def all_records(self) -> tuple[ActionExecution, ...]:
        return tuple(
            decode_action(snapshot.to_dict()) for snapshot in self._collection().stream()
        )


# ============================ proofs ==================================================


@dataclass(frozen=True)
class StoredProofRef:
    """What a durable proof write returns."""

    proof_ref: str
    content_hash: str
    created: bool
    """False when an identical proof was already stored — an idempotent no-op."""


class FirestoreProofStore:
    """Write-once Change Proof records.

    Mirrors ``driftzero.proof.store.ProofStore`` exactly: a byte-identical re-record
    succeeds idempotently, and a *differing* proof under the same ``proof_id`` raises
    instead of overwriting. The comparison is on the stored canonical bytes, which is the
    same string the in-memory store compares.
    """

    def __init__(self, client: firestore.Client) -> None:
        self._client = client

    @staticmethod
    def proof_ref(workflow_id: str, proof_id: str) -> str:
        return f"{WORKFLOWS}/{workflow_id}/{PROOFS}/{proof_id}"

    def _doc(self, workflow_id: str, proof_id: str) -> firestore.DocumentReference:
        return (
            self._client.collection(WORKFLOWS)
            .document(safe_identifier(workflow_id, kind="workflow_id"))
            .collection(PROOFS)
            .document(safe_identifier(proof_id, kind="proof_id"))
        )

    def record(self, proof: ChangeProof) -> StoredProofRef:
        """Persist a proof exactly once.

        ``create`` carries an implicit "must not exist" precondition, so the write is
        atomic: two concurrent writers cannot both create the document, and the loser
        falls through to the byte comparison rather than overwriting the winner.
        """
        document = encode_proof(proof)
        ref = self._doc(proof.workflow_id, proof.proof_id)
        stored_ref = self.proof_ref(proof.workflow_id, proof.proof_id)
        try:
            ref.create(document)
        except gcloud_exceptions.AlreadyExists:
            existing = ref.get().to_dict() or {}
            if existing.get("canonical_bytes") != document["canonical_bytes"]:
                raise ConflictingRecord(
                    stored_ref, "a different proof is already stored under this proof_id"
                ) from None
            return StoredProofRef(stored_ref, proof.content_hash, created=False)
        return StoredProofRef(stored_ref, proof.content_hash, created=True)

    def resolve(self, workflow_id: str, proof_id: str) -> ChangeProof | None:
        snapshot = self._doc(workflow_id, proof_id).get()
        if not snapshot.exists:
            return None
        return decode_proof(snapshot.to_dict())

    def find_workflow(self, workflow_id: str) -> ChangeProof | None:
        """The proof for a workflow, or ``None``. At most one is ever generated."""
        collection = (
            self._client.collection(WORKFLOWS)
            .document(safe_identifier(workflow_id, kind="workflow_id"))
            .collection(PROOFS)
        )
        for snapshot in collection.limit(1).stream():
            return decode_proof(snapshot.to_dict())
        return None


# ============================ idempotency keys ========================================


@dataclass(frozen=True)
class ClaimResult:
    """The outcome of attempting to claim a stable idempotency key."""

    key: str
    owner: str
    granted: bool
    """True only for the call that took ownership. A re-claim by the same owner is an
    idempotent success with ``granted`` False."""


class FirestoreIdempotencyKeys:
    """Durable single-owner claims on stable idempotency keys.

    The claim is a single ``create`` call, whose "document must not exist" precondition
    is enforced by Firestore itself. It is deliberately **not** read-then-write: under
    two concurrent writers that pattern lets both observe "absent" and both proceed,
    which is precisely the duplicate-side-effect this store exists to prevent.
    """

    def __init__(self, client: firestore.Client) -> None:
        self._client = client

    def _doc(self, key: str) -> firestore.DocumentReference:
        return self._client.collection(IDEMPOTENCY_KEYS).document(
            safe_identifier(key, kind="idempotency key")
        )

    def claim(self, key: str, owner: str) -> ClaimResult:
        """Take ownership of ``key``, or fail closed if someone else already holds it."""
        ref = self._doc(key)
        try:
            ref.create(
                {
                    "key": key,
                    "owner": owner,
                    "claimed_at": firestore.SERVER_TIMESTAMP,
                }
            )
        except gcloud_exceptions.AlreadyExists:
            existing = (ref.get().to_dict() or {}).get("owner")
            if existing == owner:
                return ClaimResult(key=key, owner=owner, granted=False)
            raise ConflictingRecord(
                f"{IDEMPOTENCY_KEYS}/{key}", f"already claimed by {existing!r}"
            ) from None
        return ClaimResult(key=key, owner=owner, granted=True)

    def owner_of(self, key: str) -> str | None:
        snapshot = self._doc(key).get()
        if not snapshot.exists:
            return None
        return (snapshot.to_dict() or {}).get("owner")


# ============================ facade ==================================================


@dataclass(frozen=True)
class FirestorePersistence:
    """The four T092 stores over one client, for the composition root to hand around."""

    client: firestore.Client
    workflows: FirestoreWorkflowStore
    proofs: FirestoreProofStore
    idempotency: FirestoreIdempotencyKeys

    @classmethod
    def connect(
        cls, *, project: str, database: str = "(default)", credentials: Any | None = None
    ) -> FirestorePersistence:
        client = build_client(project=project, database=database, credentials=credentials)
        return cls.over(client)

    @classmethod
    def over(cls, client: firestore.Client) -> FirestorePersistence:
        """Wrap an existing client — the seam the tests use."""
        return cls(
            client=client,
            workflows=FirestoreWorkflowStore(client),
            proofs=FirestoreProofStore(client),
            idempotency=FirestoreIdempotencyKeys(client),
        )

    def ledger_for(self, workflow_id: str) -> FirestoreActionLedger:
        """The action ledger scoped to one workflow."""
        return FirestoreActionLedger(self.client, workflow_id=workflow_id)
