"""T093 — the Cloud Storage evidence adapter.

Immutable storage for raw evidence, before/after artifacts and rendered proofs.

Immutability here is the operational definition plan.md gives: write-once application
semantics enforced by a generation precondition, inside one project's trust boundary. It
is not an append-only ledger and not tamper-proof against an actor holding project write
credentials, and this module does not pretend otherwise.

Two hashes appear in this system and must never be conflated:

* the SHA-256 of an object's raw bytes, returned here as ``content_hash`` — file identity
* ``ChangeProof.content_hash`` — SHA-256 over the proof's canonical JSON *excluding its
  own content_hash field*, computed by the Truth Engine

Storing a rendered proof produces the first. It never recomputes, replaces, or is
compared against the second.

Authentication is Application Default Credentials locally and the attached
``driftzero-run-sa`` on Cloud Run, which holds ``roles/storage.objectAdmin`` scoped to
the evidence bucket alone. This module never changes IAM, never sets an object ACL, and
never mints a signed URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import storage

from driftzero.truth_engine.evidence import content_hash as sha256_hex
from driftzero_cloud.errors import CloudAdapterError, ConflictingRecord
from driftzero_cloud.serialization import safe_identifier

EVIDENCE_PREFIX = "workflows"
PROOF_PREFIX = "proofs"

LEGACY_PROJECT = "driftzero-agentic-2026"
"""Quarantined, exactly as in the Firestore adapter."""


@dataclass(frozen=True)
class StoredObject:
    """What every successful write returns.

    ``created`` distinguishes a first write from an idempotent re-write of identical
    bytes, so a caller can tell "I stored this" from "this was already stored".
    """

    object_ref: str
    content_hash: str
    """SHA-256 of the raw object bytes. File identity — NOT ChangeProof.content_hash."""
    size: int
    content_type: str
    generation: int | None
    created: bool


def build_client(*, project: str, credentials: Any | None = None) -> storage.Client:
    """Construct a Cloud Storage client, refusing the quarantined legacy project."""
    if not project or not project.strip():
        raise CloudAdapterError("a Cloud Storage client requires an explicit project")
    if project == LEGACY_PROJECT:
        raise CloudAdapterError(
            f"refusing to open a Cloud Storage client against the quarantined project "
            f"{LEGACY_PROJECT!r}"
        )
    return storage.Client(project=project, credentials=credentials)


def evidence_path(workflow_id: str, evidence_id: str) -> str:
    """``workflows/{workflow_id}/evidence/{evidence_id}`` — deterministic and checked.

    Both segments go through :func:`safe_identifier`, so an id that arrived from an
    upload filename, a model response or an HTTP route cannot escape the prefix.
    """
    return (
        f"{EVIDENCE_PREFIX}/{safe_identifier(workflow_id, kind='workflow_id')}"
        f"/evidence/{safe_identifier(evidence_id, kind='evidence_id')}"
    )


def proof_path(proof_id: str, filename: str = "proof.json") -> str:
    """``proofs/{proof_id}/{filename}`` — deterministic and checked."""
    return (
        f"{PROOF_PREFIX}/{safe_identifier(proof_id, kind='proof_id')}"
        f"/{safe_identifier(filename, kind='filename')}"
    )


class GcsEvidenceStore:
    """Append-only evidence objects in the configured bucket.

    Every write uses ``if_generation_match=0``, which Cloud Storage evaluates as "create
    only if this object does not exist". Losing that race is not an error condition on
    its own: the bytes are compared, and an identical re-write is an idempotent success
    while a differing one fails closed.
    """

    def __init__(self, client: storage.Client, *, bucket: str) -> None:
        if not bucket or not bucket.strip():
            raise CloudAdapterError("an evidence store requires an explicit bucket name")
        self._client = client
        self._bucket_name = bucket
        self._bucket = client.bucket(bucket)

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    def object_ref(self, path: str) -> str:
        return f"gs://{self._bucket_name}/{path}"

    def put(
        self,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Write once. Identical bytes succeed idempotently; different bytes raise."""
        if not isinstance(data, bytes):
            raise CloudAdapterError(f"evidence must be bytes, got {type(data).__name__}")

        digest = sha256_hex(data)
        blob = self._bucket.blob(path)
        try:
            blob.upload_from_string(
                data, content_type=content_type, if_generation_match=0
            )
        except gcloud_exceptions.PreconditionFailed:
            existing = self._bucket.blob(path)
            current = existing.download_as_bytes()
            if current != data:
                raise ConflictingRecord(
                    self.object_ref(path),
                    f"stored object is {len(current)} bytes with hash "
                    f"{sha256_hex(current)[:12]}…, refusing to overwrite immutable "
                    f"evidence",
                ) from None
            existing.reload()
            return StoredObject(
                object_ref=self.object_ref(path),
                content_hash=digest,
                size=len(data),
                content_type=existing.content_type or content_type,
                generation=existing.generation,
                created=False,
            )
        return StoredObject(
            object_ref=self.object_ref(path),
            content_hash=digest,
            size=len(data),
            content_type=content_type,
            generation=blob.generation,
            created=True,
        )

    def put_evidence(
        self,
        *,
        workflow_id: str,
        evidence_id: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> StoredObject:
        """Store one raw evidence object under its workflow."""
        return self.put(
            evidence_path(workflow_id, evidence_id), data, content_type=content_type
        )

    def put_rendered_proof(self, *, proof_id: str, data: bytes) -> StoredObject:
        """Store a rendered proof document.

        The returned ``content_hash`` is the SHA-256 of these exact bytes — the file's
        identity. A rendered proof file contains ``content_hash`` as a field, so the
        file hash and ``ChangeProof.content_hash`` are expected to differ, and neither
        is evidence about the other.
        """
        return self.put(proof_path(proof_id), data, content_type="application/json")

    def get(self, path: str) -> bytes | None:
        """Return the object's bytes, or ``None`` when there is no such object."""
        try:
            return self._bucket.blob(path).download_as_bytes()
        except gcloud_exceptions.NotFound:
            return None

    def describe(self, path: str) -> StoredObject | None:
        """Metadata for a stored object without downloading it twice."""
        blob = self._bucket.get_blob(path)
        if blob is None:
            return None
        data = blob.download_as_bytes()
        return StoredObject(
            object_ref=self.object_ref(path),
            content_hash=sha256_hex(data),
            size=blob.size if blob.size is not None else len(data),
            content_type=blob.content_type or "application/octet-stream",
            generation=blob.generation,
            created=False,
        )

    def delete(self, path: str) -> bool:
        """Remove one object. Present for test cleanup only.

        Production evidence is immutable and is never deleted by the application; the
        bucket's lifecycle policy transitions storage class and has no delete rule.
        """
        try:
            self._bucket.blob(path).delete()
        except gcloud_exceptions.NotFound:
            return False
        return True
