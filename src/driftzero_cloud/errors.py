"""Failure modes shared by both cloud adapters.

Every one of these means *fail closed*. None of them is recoverable by retrying the same
write, and none should ever be caught and turned into a default value: a persistence
layer that silently substitutes something plausible is exactly the failure the Truth
Engine exists to prevent.
"""

from __future__ import annotations


class CloudAdapterError(RuntimeError):
    """Base class for every adapter failure."""


class ConflictingRecord(CloudAdapterError):
    """A record already exists under this identity with different content.

    Raised rather than overwriting. Applies to proofs (a second, differing proof under
    one ``proof_id``), to evidence objects (different bytes at an immutable ref), and to
    idempotency keys claimed by a different owner.
    """

    def __init__(self, ref: str, detail: str = "") -> None:
        message = f"refusing to overwrite {ref}: existing record differs"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.ref = ref


class IdentifierRejected(CloudAdapterError):
    """An identifier is unsafe to use as a document id or object path.

    Firestore document ids and Cloud Storage object names are both path-shaped, so an
    unsanitised identifier is a traversal primitive. Identifiers arriving from a model,
    an upload filename, or an HTTP route are never authority.
    """

    def __init__(self, value: str, reason: str) -> None:
        super().__init__(f"rejected identifier {value!r}: {reason}")
        self.value = value
        self.reason = reason
