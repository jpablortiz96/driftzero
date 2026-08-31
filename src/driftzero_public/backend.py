"""Server-side client for the private driftzero-api.

The public surface is the only thing on the internet. The backend stays private, and
this module is the single place where the boundary is crossed.

Three properties are structural rather than conventional, because a judge-facing public
page is exactly where a reverse proxy accidentally grows:

1. **An allow-list, not a path parameter.** :data:`READABLE` names every path this
   process may fetch. Anything else raises before a request is built, so no route can
   forward a caller-supplied path to the private API.
2. **Reads only.** There is no method that issues anything but ``GET``.
3. **The token never leaves this module.** :meth:`_identity_token` is private, its result
   is placed directly into an outgoing header, and nothing returns it to a caller or
   writes it to a log. The browser talks to the public service; only the public service
   talks to the backend.

Authentication is the attached Cloud Run service identity. There is no key file, no
credential in the environment, and nothing to leak: the metadata server mints a
Google-signed ID token whose audience is the backend's own URL.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

logger = logging.getLogger("driftzero.public.backend")

#: Every path the public service may read from the private API. Adding to this set is a
#: deliberate act with a security review attached; it is not a configuration knob.
READABLE: Final[frozenset[str]] = frozenset({"/health"})

METADATA_IDENTITY: Final[str] = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)
METADATA_HEADERS: Final[dict[str, str]] = {"Metadata-Flavor": "Google"}

BACKEND_URL_ENV: Final[str] = "DRIFTZERO_BACKEND_URL"


class PathNotReadable(RuntimeError):
    """A path outside :data:`READABLE` was requested.

    Raised rather than fetched, so a proxy cannot be built out of this client by
    accident or by a crafted request.
    """


@dataclass(frozen=True)
class BackendStatus:
    """What the public page is allowed to say about the private backend.

    Deliberately small. It carries a reachability verdict and a short human label —
    never a response body, never a header, never an identifier a visitor has no use for.
    """

    reachable: bool
    label: str
    detail: str
    checked_at: str

    @property
    def tone(self) -> str:
        return "ok" if self.reachable else "warn"

    @staticmethod
    def unknown(detail: str) -> BackendStatus:
        return BackendStatus(
            reachable=False,
            label="UNVERIFIED",
            detail=detail,
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )


class PrivateBackend:
    """A read-only, authenticated client for one private Cloud Run service."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 6.0) -> None:
        self._base_url = (base_url or os.environ.get(BACKEND_URL_ENV, "")).rstrip("/")
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    @property
    def audience(self) -> str:
        """The ID token audience: the backend's own URL, never an invented hostname."""
        return self._base_url

    def _identity_token(self) -> str | None:
        """Mint a Google-signed ID token for the backend audience.

        Uses the metadata server, which is the credential Cloud Run attaches to the
        revision. Returns ``None`` off Cloud Run — a local run then simply reports the
        backend as unverified instead of pretending, and never falls back to a key file.
        """
        try:
            response = httpx.get(
                METADATA_IDENTITY,
                params={"audience": self._base_url, "format": "full"},
                headers=METADATA_HEADERS,
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            # Off Cloud Run the metadata host does not resolve. That is expected, not an
            # error worth a stack trace on every local page render.
            return None
        if response.status_code != 200:
            logger.warning("metadata identity endpoint returned %s", response.status_code)
            return None
        token = response.text.strip()
        return token or None

    def _read(self, path: str) -> httpx.Response:
        if path not in READABLE:
            raise PathNotReadable(
                f"{path!r} is not readable from the public surface; "
                f"readable paths are {sorted(READABLE)}"
            )
        token = self._identity_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.get(f"{self._base_url}{path}", headers=headers, timeout=self._timeout)

    def health(self) -> BackendStatus:
        """Ask the private backend whether it is serving.

        Never raises: a public page that 500s because a downstream call timed out is a
        worse outcome than a page that honestly reports the check did not complete.
        """
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        if not self.configured:
            return BackendStatus.unknown(f"{BACKEND_URL_ENV} is not configured")

        try:
            response = self._read("/health")
        except httpx.HTTPError as exc:
            return BackendStatus(
                reachable=False,
                label="UNREACHABLE",
                detail=f"the private backend did not answer ({type(exc).__name__})",
                checked_at=checked_at,
            )

        if response.status_code == 200:
            return BackendStatus(
                reachable=True,
                label="SERVING",
                detail=(
                    "authenticated server-to-server call to the private Cloud Run "
                    "backend returned 200"
                ),
                checked_at=checked_at,
            )
        if response.status_code in (401, 403):
            # Worth stating plainly: the backend refusing us proves it is private. It
            # also means this public surface is misconfigured, and both are true at once.
            return BackendStatus(
                reachable=False,
                label="FORBIDDEN",
                detail=(
                    "the backend refused this service identity — it is private, and this "
                    "surface is not currently authorised to read it"
                ),
                checked_at=checked_at,
            )
        return BackendStatus(
            reachable=False,
            label=f"HTTP {response.status_code}",
            detail="the private backend answered with an unexpected status",
            checked_at=checked_at,
        )
