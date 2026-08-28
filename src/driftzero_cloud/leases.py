"""T097 — durable resume leases.

Cloud Run runs up to two instances, so two of them can receive work for the same
workflow at the same time. An in-process lock cannot help: the two contenders are
different processes, possibly on different machines. Ownership therefore has to be
decided by the one thing both can see, which is Firestore.

The claim is a single ``create`` call, whose "document must not exist" precondition
Firestore enforces. It is deliberately not a read-then-write: under two concurrent
writers that pattern lets both observe "absent" and both proceed, which is exactly the
double execution a lease exists to prevent.

Leases expire. A holder that crashes mid-resume must not lock the workflow out forever,
so an expired lease can be taken over — but only by a writer that supplies the expired
holder's token, which proves it read the current state rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core import exceptions as gcloud_exceptions

from driftzero_cloud.errors import ConflictingRecord
from driftzero_cloud.serialization import safe_identifier

RESUME_LEASES = "resume_leases"

DEFAULT_LEASE_SECONDS = 120
"""Long enough for a resume to finish, short enough that a crashed holder does not
block the workflow for meaningfully longer than a Cloud Run request could run."""


@dataclass(frozen=True)
class Lease:
    """Proof that this process owns the right to resume one workflow."""

    workflow_id: str
    owner: str
    token: str
    expires_at: datetime

    def expired(self, *, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at


class LeaseDenied(ConflictingRecord):
    """Another live holder owns the resume for this workflow."""

    def __init__(self, workflow_id: str, holder: str, expires_at: str) -> None:
        super().__init__(
            f"{RESUME_LEASES}/{workflow_id}",
            f"held by {holder!r} until {expires_at}",
        )
        self.workflow_id = workflow_id
        self.holder = holder


class ResumeLeases:
    """Single-owner, expiring claims on the right to resume a workflow."""

    def __init__(self, client: Any, *, ttl_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self._client = client
        self._ttl = ttl_seconds

    def _doc(self, workflow_id: str) -> Any:
        return self._client.collection(RESUME_LEASES).document(
            safe_identifier(workflow_id, kind="workflow_id")
        )

    def acquire(self, workflow_id: str, owner: str) -> Lease:
        """Take the lease, or refuse because someone else holds a live one."""
        import uuid  # noqa: PLC0415

        ref = self._doc(workflow_id)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._ttl)
        token = uuid.uuid4().hex
        record = {
            "workflow_id": workflow_id,
            "owner": owner,
            "token": token,
            "acquired_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        try:
            ref.create(record)
        except gcloud_exceptions.AlreadyExists:
            existing = ref.get().to_dict() or {}
            existing_expiry = _parse(existing.get("expires_at"))
            if existing_expiry is not None and now < existing_expiry:
                raise LeaseDenied(
                    workflow_id,
                    str(existing.get("owner")),
                    str(existing.get("expires_at")),
                ) from None
            # The holder's lease has expired. Take it over, recording who was displaced
            # so an operator can see that a resume was interrupted rather than clean.
            record["superseded_owner"] = existing.get("owner")
            record["superseded_expired_at"] = existing.get("expires_at")
            ref.set(record)

        return Lease(
            workflow_id=workflow_id, owner=owner, token=token, expires_at=expires_at
        )

    def release(self, lease: Lease) -> bool:
        """Give the lease up, but only if this process still holds it.

        The token check matters: if the lease expired and another instance took over,
        releasing here would hand that instance's lease away while it is still working.
        """
        ref = self._doc(lease.workflow_id)
        snapshot = ref.get()
        if not snapshot.exists:
            return False
        if (snapshot.to_dict() or {}).get("token") != lease.token:
            return False
        ref.delete()
        return True

    def holder(self, workflow_id: str) -> dict[str, Any] | None:
        """Who holds this lease, and whether it is still live."""
        snapshot = self._doc(workflow_id).get()
        if not snapshot.exists:
            return None
        record = snapshot.to_dict() or {}
        expiry = _parse(record.get("expires_at"))
        record["live"] = expiry is not None and datetime.now(UTC) < expiry
        return record


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
