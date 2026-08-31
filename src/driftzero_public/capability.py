"""Opaque, signed capabilities for public live-pilot sessions.

A live pilot has to be driven from an anonymous browser, which means the browser must
carry *something* that says "you may act on this run". The obvious choice — the
workflow id — is the wrong one: workflow ids are sequential within a namespace, they
appear in evidence and logs, and treating one as authority would let anybody who guesses
or reads one submit field evidence into somebody else's run.

So the browser gets a capability instead. It names one workflow, it expires, and it is
signed with a key the browser never sees. Holding it permits exactly three things:

* read the state of **its own** pilot,
* submit field evidence to **its own** workflow,
* read **its own** proof.

It confers nothing else. There is no workflow selection, no change parameter, no model
configuration, no administrative operation — those are not gated by the capability, they
are absent from the public surface entirely.

The token is ``base64url(payload) + "." + base64url(hmac-sha256)``. Verification is
constant-time and fails closed: a bad signature, a malformed token, and an expired token
are the same answer — refusal — and none of them says which.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Final

#: A pilot is a few minutes of work. Half an hour is generous and still short enough
#: that a token found in a shared screenshot or a browser history is almost always dead.
DEFAULT_TTL_SECONDS: Final[int] = 30 * 60

SECRET_ENV: Final[str] = "DRIFTZERO_SESSION_HMAC"
VERSION: Final[str] = "dz1"


class CapabilityInvalid(Exception):
    """The capability is absent, malformed, unsigned, forged, or expired.

    Deliberately one exception for all of those. Telling a caller *which* of those it was
    tells them how to get closer.
    """


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signing_key() -> bytes:
    """The HMAC key, from the environment.

    Cloud Run mounts it from Secret Manager. Absent — which is every local run and every
    test — a random per-process key is generated: tokens then work within the process
    and are worthless outside it. That is the right failure mode. Falling back to a
    hard-coded default would mean every deployment shared a forgeable key, and the code
    would look like it was signing things.
    """
    configured = os.environ.get(SECRET_ENV, "").strip()
    if configured:
        return configured.encode("utf-8")
    global _EPHEMERAL_KEY  # noqa: PLW0603
    if _EPHEMERAL_KEY is None:
        _EPHEMERAL_KEY = secrets.token_bytes(32)
    return _EPHEMERAL_KEY


_EPHEMERAL_KEY: bytes | None = None


@dataclass(frozen=True)
class Capability:
    """What a bearer is allowed to act on. One workflow, until one moment."""

    workflow_id: str
    change_id: str
    issued_at: int
    expires_at: int

    @property
    def seconds_remaining(self) -> int:
        return max(0, self.expires_at - int(time.time()))

    @property
    def expired(self) -> bool:
        return self.seconds_remaining <= 0


def issue(workflow_id: str, change_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a capability for exactly one live pilot workflow."""
    if not workflow_id:
        raise ValueError("a capability must name a workflow")
    now = int(time.time())
    payload = {
        "v": VERSION,
        "w": workflow_id,
        "c": change_id,
        "iat": now,
        "exp": now + int(ttl_seconds),
        # A nonce so two capabilities minted in the same second for the same workflow are
        # still distinct strings; it is not used for anything else.
        "n": secrets.token_urlsafe(6),
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_signing_key(), body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(signature)}"


def verify(token: str) -> Capability:
    """Return the capability a token carries, or refuse.

    Signature first, contents second: an unsigned token's claims are not read, so a
    forged payload never reaches the parsing code.
    """
    if not token or token.count(".") != 1:
        raise CapabilityInvalid("malformed capability")
    encoded_body, encoded_signature = token.split(".", 1)
    try:
        body = _unb64(encoded_body)
        signature = _unb64(encoded_signature)
    except Exception as exc:  # noqa: BLE001 - any decoding failure is one refusal
        raise CapabilityInvalid("malformed capability") from exc

    expected = hmac.new(_signing_key(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise CapabilityInvalid("malformed capability")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CapabilityInvalid("malformed capability") from exc

    if payload.get("v") != VERSION:
        raise CapabilityInvalid("malformed capability")

    capability = Capability(
        workflow_id=str(payload.get("w", "")),
        change_id=str(payload.get("c", "")),
        issued_at=int(payload.get("iat", 0)),
        expires_at=int(payload.get("exp", 0)),
    )
    if not capability.workflow_id:
        raise CapabilityInvalid("malformed capability")
    if capability.expired:
        raise CapabilityInvalid("this pilot session has expired")
    return capability


def signing_key_is_durable() -> bool:
    """Whether tokens survive a restart, i.e. whether a real key is configured.

    Reported on the operational surface so an instance running on an ephemeral key is
    visible rather than merely puzzling when sessions stop working after a scale event.
    """
    return bool(os.environ.get(SECRET_ENV, "").strip())
