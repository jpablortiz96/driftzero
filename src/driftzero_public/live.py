"""The live-pilot client: the only place the public surface may change anything.

Everything here is deliberately shaped so that the public service cannot become a
general proxy for the private API:

* **The change is the server's.** :func:`canonical_change` builds the payload from the
  controlled pilot fixture shipped in this image. A visitor supplies no procedure, no
  requirement, no values, no target artifact and no authorization — only a fresh
  ``change_id``, which the server generates. There is no request field a caller can use
  to reach the model with words of their own.
* **The workflow is the capability's.** Every operation takes a
  :class:`~driftzero_public.capability.Capability` and derives the workflow id from it.
  No method accepts a workflow id, so no caller can name one.
* **The verbs are enumerated.** Start, advance, read, verify, read proof. There is no
  generic request method to call with a path.

The image bytes a visitor uploads are the one piece of caller-supplied data that reaches
the backend, and they reach it as an image on the existing verification endpoint, where
the field provider observes and the frozen comparator decides.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from driftzero_public.backend import PrivateBackend
from driftzero_public.capability import Capability

logger = logging.getLogger("driftzero.public.live")

ASSETS = Path(__file__).resolve().parent / "static"

#: The controlled source change every public pilot runs. Shipped with the image, owned by
#: the server, identical for every visitor except its freshly minted id.
CANONICAL_CHANGE_FILE: Final[Path] = ASSETS / "canonical_change.json"

#: Real physical photographs captured for DRIFTZERO, shipped with this image. Selecting
#: one of these is a choice between two server-owned files, not a file upload.
PILOT_PHOTOS: Final[dict[str, str]] = {
    "current": "driftzero-photo-left.jpg",
    "corrected": "driftzero-photo-top-right.jpg",
}

#: Uploads are bounded before anything else looks at them.
MAX_UPLOAD_BYTES: Final[int] = 8 * 1024 * 1024

#: Magic-number prefixes for the image types the pilot accepts. The declared filename and
#: browser Content-Type are claims; these bytes are not.
IMAGE_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),
)
HEIF_BRANDS: Final[tuple[bytes, ...]] = (b"heic", b"heif", b"heix", b"hevc", b"mif1")


class LivePilotError(RuntimeError):
    """The private backend refused or could not be reached."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class UnsupportedEvidence(ValueError):
    """The submitted bytes are not an image the pilot can carry."""


def canonical_change(change_id: str) -> dict[str, Any]:
    """The server-owned source change, with a fresh id.

    Underscore-prefixed provenance keys are stripped: the API forbids unknown fields, and
    those keys document the fixture rather than describing the change.
    """
    fixture = json.loads(CANONICAL_CHANGE_FILE.read_text(encoding="utf-8"))
    payload = {key: value for key, value in fixture.items() if not key.startswith("_")}
    payload["change_id"] = change_id
    return payload


def new_change_id() -> str:
    """A fresh id per run, so every visitor gets a genuinely new workflow.

    Reusing one would make the second visitor a transport duplicate of the first and hand
    them somebody else's run.
    """
    return f"dz-live-{uuid.uuid4().hex[:12]}"


def sniff_image(raw: bytes) -> str:
    """Return the media type the *bytes* are, or refuse.

    The authoritative type is derived here rather than taken from the upload's headers,
    for the same reason the backend derives submission identity from content: a client
    claim about its own evidence is not evidence.
    """
    if not raw:
        raise UnsupportedEvidence("the submitted file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise UnsupportedEvidence(
            f"the image is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        )
    for prefix, media_type in IMAGE_MAGIC:
        if raw.startswith(prefix):
            return media_type
    # HEIC/HEIF: the brand sits in the ftyp box rather than at offset zero. Phones
    # photograph in it by default, so refusing it would refuse the most likely upload.
    if len(raw) > 12 and raw[4:8] == b"ftyp" and raw[8:12] in HEIF_BRANDS:
        return "image/heic"
    raise UnsupportedEvidence("that file is not an image the pilot can read")


@dataclass(frozen=True)
class StartedPilot:
    workflow_id: str
    change_id: str


class LivePilot:
    """Drives one canonical pilot against the private API, on behalf of one capability."""

    def __init__(self, backend: PrivateBackend, *, timeout: float = 120.0) -> None:
        self._backend = backend
        # Generous: a live Gemini analysis plus remediation and delivery is one request,
        # and cutting it short would abandon a run the backend is still completing.
        self._timeout = timeout

    # ---------------------------------------------------------------- transport

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue one authenticated request. Callers pass a path this module built."""
        token = self._backend._identity_token()  # noqa: SLF001 - the boundary owns this
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self._backend.audience}{path}"
        try:
            return httpx.request(
                method, url, headers=headers, timeout=self._timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            raise LivePilotError(
                f"the pilot backend did not answer ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise LivePilotError(
                f"the pilot backend refused the request ({response.status_code})",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LivePilotError("the pilot backend returned an unreadable response") from exc

    # ------------------------------------------------------------------- verbs

    def start(self) -> StartedPilot:
        """Create a fresh workflow from the canonical change."""
        change_id = new_change_id()
        payload = canonical_change(change_id)
        body = self._json(self._request("POST", "/api/v1/changes", json=payload))
        workflow_id = body.get("workflow_id", "")
        if not workflow_id:
            raise LivePilotError("the pilot backend accepted the change without a workflow")
        logger.info(
            "live pilot started", extra={"workflow_id": workflow_id, "change_id": change_id}
        )
        return StartedPilot(workflow_id=workflow_id, change_id=change_id)

    def advance(self, capability: Capability) -> dict[str, Any]:
        """Run Change Intelligence, remediation and frontline delivery.

        One request, because that is one backend call: the private API advances the
        workflow through the three application steps and returns the resulting state.
        """
        return self._json(
            self._request("POST", f"/api/v1/workflows/{capability.workflow_id}/advance")
        )

    def status(self, capability: Capability) -> dict[str, Any]:
        return self._json(
            self._request("GET", f"/api/v1/workflows/{capability.workflow_id}")
        )

    def verify(
        self, capability: Capability, image: bytes, *, filename: str, media_type: str
    ) -> dict[str, Any]:
        """Submit field evidence. The observation and the verdict are the backend's."""
        return self._json(
            self._request(
                "POST",
                f"/api/v1/workflows/{capability.workflow_id}/verify",
                files={"file": (filename, image, media_type)},
            )
        )

    @staticmethod
    def _document(envelope: dict[str, Any]) -> dict[str, Any]:
        """The proof itself, out of the API's envelope.

        The API wraps the proof with its hash meaning and a canonical rendering. The
        thing to display and to re-hash is the nested document; hashing the envelope
        would silently verify the wrong bytes.
        """
        document = envelope.get("document")
        return document if isinstance(document, dict) else envelope

    def proof(self, capability: Capability) -> dict[str, Any] | None:
        """This pilot's own Change Proof, or ``None`` while it has not earned one."""
        response = self._request("GET", f"/api/v1/workflows/{capability.workflow_id}/proof")
        if response.status_code == 404:
            return None
        return self._document(self._json(response))

    def ensure_proof(self, capability: Capability) -> dict[str, Any] | None:
        """Ask the Truth Engine to generate this run's proof, then read it back.

        A request, not an assertion. The backend answers 409 when the seven completion
        conditions do not all hold, and that becomes ``None`` here — the page then says no
        proof exists, which is the truth.
        """
        response = self._request("POST", f"/api/v1/workflows/{capability.workflow_id}/proof")
        if response.status_code >= 400:
            return None
        return self._document(self._json(response))

    # -------------------------------------------------------------- pilot photo

    @staticmethod
    def pilot_photo(which: str) -> tuple[bytes, str, str]:
        """One of the two real pilot photographs, by role rather than by path.

        Callers pass ``current`` or ``corrected``; anything else is refused before it
        touches the filesystem, so this cannot be turned into an arbitrary file read.
        """
        name = PILOT_PHOTOS.get(which)
        if name is None:
            raise UnsupportedEvidence("unknown pilot photograph")
        raw = (ASSETS / name).read_bytes()
        # Sniffed, not assumed. These files were captured on a phone and carry a .jpg
        # name while actually being HEIC — the backend derives the true type from the
        # bytes either way, and this surface should not assert an extension it did not
        # verify. The hero-run evidence records exactly this mismatch.
        return raw, name, sniff_image(raw)


def recompute_content_hash(document: Mapping[str, Any]) -> dict[str, Any]:
    """Independently recompute a Change Proof's content hash.

    Reimplements the stated rule rather than importing the generator: SHA-256 over the
    proof's canonical JSON with its own ``content_hash`` field removed. Canonical JSON is
    sorted keys, no whitespace, non-ASCII preserved, UTF-8.

    Recomputing it here is the point — a verifier that calls the same code that produced
    the value proves only that the function is deterministic.
    """
    stated = str(document.get("content_hash", ""))
    if not stated:
        return {"matches": False, "detail": "the proof carries no content hash"}
    body = {key: value for key, value in document.items() if key != "content_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "matches": hmac.compare_digest(recomputed, stated),
        "stated": stated,
        "recomputed": recomputed,
        "detail": (
            "recomputed from the proof's canonical JSON, excluding its own content_hash"
            if recomputed == stated
            else "the recomputed hash does not match the value the proof carries"
        ),
    }
