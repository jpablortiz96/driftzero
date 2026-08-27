"""Structural ports for durable persistence.

These Protocols are a typing aid for the adapters and the composition root. They are
deliberately declared *here*, outside the M0 purity boundary, and they are
``runtime_checkable`` structural types — so the existing frozen in-memory stores satisfy
them exactly as they are, with no base class, no registration and no edit to a single
M0 file.

Nothing under ``src/driftzero/`` imports this module, and nothing ever should: the
dependency arrow points from the adapter to the domain, never back.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from driftzero.models.action import ActionExecution
from driftzero.models.proof import ChangeProof
from driftzero.models.workflow import Workflow


@runtime_checkable
class WorkflowStore(Protocol):
    """Durable authoritative workflow state."""

    def save(self, workflow: Workflow) -> None:
        """Persist the workflow as the current authoritative record."""

    def load(self, workflow_id: str) -> Workflow | None:
        """Return the persisted workflow, or ``None`` if there is no such workflow.

        Returning ``None`` is the whole contract. An unknown id must never produce a
        freshly-minted default workflow: that would be the store inventing state the
        product then reports as real.
        """


@runtime_checkable
class ActionLedgerPort(Protocol):
    """Durable action-execution ledger.

    Mirrors the surface of the in-memory ``ActionLedger`` that matters for persistence.
    Status transitions stay in the Truth Engine; this only stores and returns records.
    """

    def save(self, action: ActionExecution) -> None: ...

    def get(self, action_id: str) -> ActionExecution | None: ...

    def all_records(self) -> tuple[ActionExecution, ...]: ...


@runtime_checkable
class ProofStorePort(Protocol):
    """Durable Change Proof records, write-once per ``proof_id``."""

    def record(self, proof: ChangeProof) -> str:
        """Persist the proof and return its ref.

        Re-recording a byte-identical proof succeeds idempotently. A *differing* proof
        under the same ``proof_id`` must raise rather than overwrite.
        """

    def resolve(self, proof_ref: str) -> ChangeProof | None: ...

    def find_workflow(self, workflow_id: str) -> ChangeProof | None: ...


@runtime_checkable
class IdempotencyKeyStore(Protocol):
    """Durable single-owner claims on stable idempotency keys."""

    def claim(self, key: str, owner: str) -> bool:
        """Atomically claim ``key`` for ``owner``.

        Returns True if this call took ownership, False if ``owner`` already held it.
        Raises if a *different* owner holds it. The implementation must not be a
        read-then-write: two concurrent writers must not both observe "absent".
        """

    def owner_of(self, key: str) -> str | None: ...


@runtime_checkable
class EvidenceObjectStore(Protocol):
    """Immutable evidence objects addressed by a deterministic path."""

    def put(
        self, path: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> object:
        """Write once. Identical bytes at an existing path succeed idempotently;
        differing bytes raise."""

    def get(self, path: str) -> bytes | None: ...
