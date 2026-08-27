"""Explicit encode/decode between domain records and Firestore documents.

No pickle, no ``repr``, no Python-specific encoding. Every document is a plain
JSON-compatible mapping produced by Pydantic's ``model_dump(mode="json")``, which turns
datetimes into ISO-8601 strings and enums into their values, and every read-back goes
through ``model_validate``. A document written by one process is therefore readable by
any other process, and by a human.

Each document carries ``schema_version``. A reader that meets a version it does not
understand refuses rather than guessing at the shape.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from driftzero.models.action import ActionExecution
from driftzero.models.proof import ChangeProof
from driftzero.models.workflow import Workflow
from driftzero.truth_engine.evidence import canonical_json
from driftzero_cloud.errors import CloudAdapterError, IdentifierRejected

DOCUMENT_SCHEMA_VERSION = 1
"""Bumped only when an existing document's shape changes incompatibly."""

_SAFE_IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
"""Conservative by design.

Firestore document ids may not be ``.`` or ``..``, may not contain ``/``, and are capped
at 1500 bytes; Cloud Storage object names accept far more, including ``..`` segments and
control characters. Rather than encode two different rule sets, both adapters share this
single narrow allow-list. Every identifier DRIFTZERO actually mints — workflow ids,
action ids, proof ids, submission ids, content hashes — already satisfies it.
"""

_RESERVED = frozenset({".", ".."})


def safe_identifier(value: str, *, kind: str = "identifier") -> str:
    """Return ``value`` if it is safe as a document id or a single path segment.

    Rejects traversal (``..``), separators, leading punctuation, empty strings and
    anything that could escape the prefix an adapter believes it is writing under.
    """
    if not isinstance(value, str) or not value:
        raise IdentifierRejected(str(value), f"{kind} must be a non-empty string")
    if value in _RESERVED:
        raise IdentifierRejected(value, f"{kind} is a reserved path segment")
    if "/" in value or "\\" in value:
        raise IdentifierRejected(value, f"{kind} may not contain a path separator")
    if ".." in value:
        raise IdentifierRejected(value, f"{kind} may not contain a parent reference")
    if not _SAFE_IDENTIFIER.match(value):
        raise IdentifierRejected(
            value,
            f"{kind} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,254}}",
        )
    return value


def _envelope(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": DOCUMENT_SCHEMA_VERSION, "kind": kind, "payload": payload}


def _open_envelope(document: dict[str, Any], kind: str) -> dict[str, Any]:
    version = document.get("schema_version")
    if version != DOCUMENT_SCHEMA_VERSION:
        raise CloudAdapterError(
            f"unsupported {kind} document schema_version {version!r}; "
            f"this build reads {DOCUMENT_SCHEMA_VERSION}"
        )
    if document.get("kind") != kind:
        raise CloudAdapterError(
            f"expected a {kind!r} document, found {document.get('kind')!r}"
        )
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise CloudAdapterError(f"{kind} document has no payload mapping")
    return payload


# ============================ workflows ===============================================


@dataclass(frozen=True)
class WorkflowRecord:
    """What must survive a restart: the aggregate *and* the chronology beside it.

    ``Workflow`` carries impact resolution, remediation evidence, delivery state, the
    verification chronology and the proof reference. It does **not** carry the ordered
    list of states the workflow has occupied — the application holds that alongside the
    aggregate, and proof condition 7 reads it. Persisting the model alone would silently
    drop it, so the durable record is explicitly the pair.

    ``revision`` is the adapter's own optimistic-concurrency counter. The domain's
    ``event_sequence`` is a verification-event position, not a document version, so it
    cannot serve this purpose.
    """

    workflow: Workflow
    state_history: tuple[str, ...] = ()
    revision: int = 0


def encode_workflow(
    workflow: Workflow,
    *,
    state_history: Sequence[str] = (),
    revision: int = 0,
) -> dict[str, Any]:
    """Encode the full authoritative workflow record."""
    payload = workflow.model_dump(mode="json")
    document = _envelope("workflow", payload)
    document["state_history"] = [str(state) for state in state_history]
    document["revision"] = revision
    # Denormalised for querying and for cheap conflict checks. The payload stays the
    # single source of truth; these are copies, never a second authority.
    document["workflow_id"] = workflow.workflow_id
    document["change_id"] = workflow.change_id
    document["state"] = str(workflow.state)
    return document


def decode_workflow(document: dict[str, Any]) -> Workflow:
    """Reconstruct a ``Workflow``, validating it exactly as the domain would."""
    return Workflow.model_validate(_open_envelope(document, "workflow"))


def decode_workflow_record(document: dict[str, Any]) -> WorkflowRecord:
    """Reconstruct the aggregate together with its chronology and revision."""
    workflow = decode_workflow(document)
    history = document.get("state_history") or []
    if not isinstance(history, list):
        raise CloudAdapterError("workflow document state_history is not a list")
    return WorkflowRecord(
        workflow=workflow,
        state_history=tuple(str(item) for item in history),
        revision=int(document.get("revision", 0)),
    )


# ============================ action ledger ===========================================


def encode_action(action: ActionExecution) -> dict[str, Any]:
    payload = action.model_dump(mode="json")
    document = _envelope("action", payload)
    document["action_id"] = action.action_id
    document["workflow_id"] = action.workflow_id
    document["status"] = str(action.status)
    document["attempt_count"] = action.attempt_count
    return document


def decode_action(document: dict[str, Any]) -> ActionExecution:
    return ActionExecution.model_validate(_open_envelope(document, "action"))


# ============================ proofs ==================================================


def encode_proof(proof: ChangeProof) -> dict[str, Any]:
    """Encode a proof together with the exact bytes its identity is defined by.

    ``canonical_bytes`` is computed the same way ``ProofStore.record`` computes it —
    ``canonical_json`` over the full model dump, ``content_hash`` field included. It is
    stored verbatim so a read-back can be compared byte for byte, and so a second write
    under the same ``proof_id`` can be detected as conflicting rather than accepted.

    The adapter never computes a proof hash of its own. ``content_hash`` is copied from
    the proof, where the Truth Engine put it.
    """
    payload = proof.model_dump(mode="json")
    document = _envelope("proof", payload)
    document["proof_id"] = proof.proof_id
    document["workflow_id"] = proof.workflow_id
    document["content_hash"] = proof.content_hash
    document["canonical_bytes"] = canonical_json(payload)
    return document


def decode_proof(document: dict[str, Any]) -> ChangeProof:
    """Reconstruct a ``ChangeProof`` and verify it survived the round trip intact.

    Two independent checks, because they catch different corruptions: the recomputed
    canonical bytes must equal the stored bytes, and the proof's own ``content_hash``
    must equal the one recorded beside it. Neither recomputes the hash *semantics* —
    that stays in the Truth Engine.
    """
    payload = _open_envelope(document, "proof")
    proof = ChangeProof.model_validate(payload)

    stored_bytes = document.get("canonical_bytes")
    if stored_bytes is not None:
        recomputed = canonical_json(proof.model_dump(mode="json"))
        if recomputed != stored_bytes:
            raise CloudAdapterError(
                f"proof {proof.proof_id} does not round-trip: canonical bytes differ "
                f"({len(recomputed)} vs {len(stored_bytes)} chars)"
            )

    stored_hash = document.get("content_hash")
    if stored_hash is not None and stored_hash != proof.content_hash:
        raise CloudAdapterError(
            f"proof {proof.proof_id} content_hash mismatch: document records "
            f"{stored_hash!r}, proof carries {proof.content_hash!r}"
        )
    return proof
