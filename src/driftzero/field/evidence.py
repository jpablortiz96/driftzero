"""T079 — accepting a physical field image, and storing what actually happened.

Two responsibilities, both deliberately dumb:

* **Acceptance** — decide whether some bytes are an image this product will send to a
  model at all, and derive their authoritative identity (SHA-256, container, MIME, byte
  count) from the bytes themselves.
* **Storage** — keep every observation attempt under a reference that independently
  resolves, permanently.

The store is append-only for the reason T073 and T078 both taught: a reference that stops
resolving to the thing it described is not evidence. An INCONCLUSIVE first attempt must
still resolve after a second, better photograph has been submitted, or the history is a
story rather than a record.

Nothing here talks to a model, decides PASS/FAIL, or knows what a label position is.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from driftzero.media.container import ContainerFormat, sniff_container, sniff_mime_type

MAX_IMAGE_BYTES = 12 * 1024 * 1024
"""12 MiB. Comfortably above a full-resolution phone capture, far below a memory risk."""

MIN_IMAGE_BYTES = 64
"""Below this nothing is a real photograph; it is a probe or a truncated upload."""

ACCEPTED_CONTAINERS: frozenset[ContainerFormat] = frozenset(
    {
        ContainerFormat.JPEG,
        ContainerFormat.PNG,
        ContainerFormat.HEIC,
        ContainerFormat.HEIF,
    }
)
"""The narrow allowlist the product needs: phone captures and screenshots.

An allowlist, never a blocklist. A container absent from this set is refused even if it
is a perfectly valid image — the question is what this pipeline supports, not what
exists.
"""


class ImageRejection(StrEnum):
    """Why a submission was refused. Deterministic and safe to show a user."""

    EMPTY = "EMPTY"
    TOO_SMALL = "TOO_SMALL"
    TOO_LARGE = "TOO_LARGE"
    UNRECOGNIZED_CONTAINER = "UNRECOGNIZED_CONTAINER"
    UNSUPPORTED_CONTAINER = "UNSUPPORTED_CONTAINER"


class ImageRejected(Exception):
    """The submitted bytes are not an acceptable field image."""

    def __init__(self, reason: ImageRejection, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class FieldImage:
    """An accepted image, identified entirely by its own bytes.

    ``declared_filename`` and ``declared_content_type`` are retained **only** so the
    evidence can show what the client claimed alongside what was actually true. Nothing
    reads them for a decision, and a test proves a lying client changes no authoritative
    field.
    """

    sha256: str
    container: ContainerFormat
    mime_type: str
    byte_count: int
    declared_filename: str | None = None
    declared_content_type: str | None = None

    @property
    def client_claim_was_wrong(self) -> bool:
        """True when the declared type disagreed with the actual bytes.

        Reported, never enforced: the ordinary iPhone case (HEIC named ``.jpg``) is a
        mislabelling, not an attack, and refusing it would reject real field evidence.
        """
        declared = (self.declared_content_type or "").split(";")[0].strip().lower()
        return bool(declared) and declared != self.mime_type

    def as_evidence(self) -> dict[str, Any]:
        return {
            "image_sha256": self.sha256,
            "container": str(self.container),
            "mime_type": self.mime_type,
            "byte_count": self.byte_count,
            "declared_filename": self.declared_filename,
            "declared_content_type": self.declared_content_type,
            "declared_type_matched_bytes": not self.client_claim_was_wrong,
            "mime_authority": "DERIVED_FROM_BYTES",
        }


def accept_field_image(
    raw: bytes,
    *,
    declared_filename: str | None = None,
    declared_content_type: str | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> FieldImage:
    """Accept ``raw`` as field evidence, or raise :class:`ImageRejected`.

    The size check runs before the container check so an oversized payload is refused
    without inspecting it further. Everything authoritative is derived from the bytes;
    the declared values are carried along as claims only.
    """
    if not raw:
        raise ImageRejected(ImageRejection.EMPTY, "no bytes were submitted")
    if len(raw) > max_bytes:
        raise ImageRejected(
            ImageRejection.TOO_LARGE,
            f"{len(raw)} bytes exceeds the {max_bytes}-byte limit",
        )
    if len(raw) < MIN_IMAGE_BYTES:
        raise ImageRejected(
            ImageRejection.TOO_SMALL, f"{len(raw)} bytes is too small to be a capture"
        )

    container = sniff_container(raw)
    if container is ContainerFormat.UNKNOWN:
        raise ImageRejected(
            ImageRejection.UNRECOGNIZED_CONTAINER,
            "the submitted bytes are not a recognized image container",
        )
    if container not in ACCEPTED_CONTAINERS:
        raise ImageRejected(
            ImageRejection.UNSUPPORTED_CONTAINER,
            f"{container} is not accepted; supported: "
            + ", ".join(sorted(str(c) for c in ACCEPTED_CONTAINERS)),
        )

    mime_type = sniff_mime_type(raw)
    if mime_type is None:  # pragma: no cover - unreachable given the allowlist above
        raise ImageRejected(
            ImageRejection.UNRECOGNIZED_CONTAINER, "no MIME type for the container"
        )
    return FieldImage(
        sha256=hashlib.sha256(raw).hexdigest(),
        container=container,
        mime_type=mime_type,
        byte_count=len(raw),
        declared_filename=declared_filename,
        declared_content_type=declared_content_type,
    )


def derive_submission_id(
    *, change_id: str, source_version: str, image_sha256: str
) -> str:
    """Stable logical identity for one field-evidence submission.

    Bound to the change, the version in force, and the exact image. The same photograph
    resubmitted against the same change is the *same* submission — that is what makes the
    replay guard principled rather than a cache with a guessed key. A different
    photograph is a genuinely different attempt and gets a different identity.
    """
    digest = hashlib.sha256(
        "\x1f".join((change_id, source_version, image_sha256)).encode("utf-8")
    ).hexdigest()
    return f"fev-{digest[:32]}"


def derive_observation_operation_id(
    *, change_id: str, source_version: str, image_sha256: str
) -> str:
    """Stable identity of the *billable model operation* for one submission.

    Deliberately the same inputs as :func:`derive_submission_id`: the operation that
    costs money is "ask the model about this image, under this change, at this version".
    Repeating exactly that must be free.
    """
    submission = derive_submission_id(
        change_id=change_id, source_version=source_version, image_sha256=image_sha256
    )
    return f"obs-{submission[4:]}"


@dataclass
class FieldEvidenceStore:
    """Append-only store of field observation evidence.

    Keyed by evidence reference. Writing over an existing reference raises rather than
    replacing, so the history a later submission produces is additive by construction.
    """

    _records: dict[str, dict[str, Any]] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _by_operation: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def evidence_ref(operation_id: str) -> str:
        return f"field-evidence:{operation_id}"

    def record(
        self,
        *,
        operation_id: str,
        document: Mapping[str, Any],
        recorded_at: datetime,
    ) -> str:
        """Store one observation attempt and return its resolvable reference."""
        ref = self.evidence_ref(operation_id)
        if ref in self._records:
            raise ValueError(f"refusing to overwrite field evidence at {ref}")
        stored = dict(document)
        stored["recorded_at"] = recorded_at.isoformat()
        stored["evidence_ref"] = ref
        self._records[ref] = stored
        self._order.append(ref)
        self._by_operation[operation_id] = ref
        return ref

    def resolve(self, evidence_ref: str) -> dict[str, Any] | None:
        """Retrieve one record. Resolving never mutates the store."""
        record = self._records.get(evidence_ref)
        return dict(record) if record is not None else None

    def find_operation(self, operation_id: str) -> dict[str, Any] | None:
        """The completed record for ``operation_id``, if this operation already ran."""
        ref = self._by_operation.get(operation_id)
        return self.resolve(ref) if ref else None

    def resolvable_refs(self) -> frozenset[str]:
        return frozenset(self._records)

    def history(self) -> tuple[dict[str, Any], ...]:
        """Every attempt, oldest first. Inconclusive attempts are kept, not pruned."""
        return tuple(dict(self._records[ref]) for ref in self._order)

    def __len__(self) -> int:
        return len(self._records)
