"""T095 — the Pub/Sub push handler for approved change ingestion.

A Pub/Sub message is **input**, never authority. The adapter decodes the envelope and
hands the payload to the same ingestion path the HTTP route uses; server-side source
derivation and Crossing 1 still own trust. A message may carry source identifiers and
event metadata. It may not carry an impact result, an affected target, an authorization
result, a verification result, a proof state, or a workflow state — those are refused by
name, exactly as they are over HTTP.

**Idempotency.** Pub/Sub delivers at least once, so duplicate delivery must be safe.
Deduplication is on the logical ``change_id`` via T029's ``classify_change_event``,
backed by T092's durable claim — not by in-memory state and not by ``messageId``. A
redelivery after a restart therefore still resolves to the original workflow: no second
remediation, no second delivery, no second proof.

**Authentication.** Cloud Run performs it. See :data:`TRUST_BOUNDARY`.

No Pub/Sub *client* SDK is imported here. A push subscription delivers an ordinary HTTP
POST, so the publisher library would be dead weight in the request path; the envelope is
parsed from JSON that FastAPI has already decoded.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from driftzero_api.runtime import ApiRuntime, WorkflowNotFound
from driftzero_console.workflows import FORBIDDEN_FIXTURE_KEYS, FixtureRejected

router = APIRouter()

PUSH_PATH = "/api/v1/pubsub/push"
"""The endpoint a push subscription will target. T089 creates that subscription once
T096 has produced a Cloud Run URL; nothing here guesses one."""

TRUST_BOUNDARY = (
    "Authentication is Cloud Run's. The push subscription is created with "
    "--push-auth-service-account, Google signs each request with an OIDC token, and "
    "Cloud Run validates it before the request reaches this process — so only an "
    "authenticated caller with roles/run.invoker arrives here. This handler therefore "
    "does not re-validate the OIDC token: duplicating Cloud Run's authentication layer "
    "in application code would add a second, weaker implementation of a check that has "
    "already passed. What this handler does own is authorization of CONTENT: a "
    "correctly authenticated caller still may not state a conclusion."
)


class EnvelopeRejected(ValueError):
    """The push envelope is permanently invalid. Retrying it cannot help."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def decode_envelope(body: Any) -> dict[str, Any]:
    """Validate a Google Pub/Sub push envelope and return the decoded payload.

    Every failure below is permanent — the same bytes will fail identically forever —
    so each raises rather than being repaired. Nothing here manufactures a valid change
    from an invalid message.
    """
    if not isinstance(body, dict):
        raise EnvelopeRejected("MALFORMED_ENVELOPE", "the body must be a JSON object")

    message = body.get("message")
    if not isinstance(message, dict):
        raise EnvelopeRejected("MISSING_MESSAGE", "no 'message' object in the envelope")

    data = message.get("data")
    if data is None or data == "":
        raise EnvelopeRejected("MISSING_DATA", "the message carries no data")
    if not isinstance(data, str):
        raise EnvelopeRejected("MALFORMED_DATA", "message.data must be a base64 string")

    try:
        # validate=True so stray characters are rejected rather than silently dropped.
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EnvelopeRejected("MALFORMED_BASE64", str(exc)[:200]) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise EnvelopeRejected("MALFORMED_UTF8", str(exc)[:200]) from exc
    except json.JSONDecodeError as exc:
        raise EnvelopeRejected("MALFORMED_JSON", str(exc)[:200]) from exc

    if not isinstance(payload, dict):
        raise EnvelopeRejected("MALFORMED_PAYLOAD", "the decoded payload must be an object")

    offending = sorted(set(payload) & FORBIDDEN_FIXTURE_KEYS)
    if offending:
        raise EnvelopeRejected(
            "AUTHORITATIVE_FIELD_REFUSED",
            "an event may describe what changed at the source, never what the system "
            "concluded; refused: " + ", ".join(offending),
        )

    if not str(payload.get("change_id", "")).strip():
        raise EnvelopeRejected("MISSING_CHANGE_ID", "change_id is required")

    return {
        "payload": payload,
        "message_id": message.get("messageId"),
        "publish_time": message.get("publishTime"),
        "attributes": dict(message.get("attributes") or {}),
        "subscription": body.get("subscription"),
    }


def ingest(runtime: ApiRuntime, envelope: dict[str, Any]) -> dict[str, Any]:
    """Hand a decoded event to the same ingestion path the HTTP route uses."""
    accepted = runtime.accept_change(envelope["payload"])
    return {
        "workflow_id": accepted["workflow_id"],
        "state": accepted["state"],
        "outcome": accepted["outcome"],
        "duplicate_of": accepted["duplicate_of"],
        "message_id": envelope["message_id"],
    }


@router.post(PUSH_PATH, tags=["pubsub"])
async def push(request: Request) -> Response:
    """Receive one Pub/Sub push delivery.

    Status mapping, and why each one:

    * **200** — accepted, or a duplicate resolved to its existing workflow. Acked.
    * **200 with ``rejected``** — permanently invalid (bad base64, bad JSON, missing
      ``change_id``, a refused authoritative field). Pub/Sub retries *any* non-2xx, and
      no dead-letter topic is configured yet, so returning 4xx here would redeliver a
      message that can never succeed until it expires. The rejection is explicit in the
      body and in the logs rather than silently swallowed. **Revisit when T089 creates
      the subscription**: with a dead-letter topic attached, 400 becomes the better
      answer and this branch should change with it.
    * **503** — a transient failure (durable storage unavailable). Not acked, so
      Pub/Sub retries. Never 200: reporting success for a write that did not happen is
      how a change silently disappears.
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - a misconfigured app
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the application was created without a runtime",
        )

    try:
        body = await request.json()
    except Exception as exc:
        return _rejected("MALFORMED_JSON", str(exc)[:200])

    try:
        envelope = decode_envelope(body)
    except EnvelopeRejected as exc:
        return _rejected(exc.reason, exc.detail)

    try:
        result = ingest(runtime, envelope)
    except FixtureRejected as exc:
        # The envelope was well formed but the change is not ingestible. Permanent.
        return _rejected("FIXTURE_REJECTED", str(exc))
    except WorkflowNotFound as exc:  # pragma: no cover - defensive
        return _rejected("WORKFLOW_NOT_FOUND", str(exc))
    except Exception as exc:
        # Anything else is treated as transient so the message is redelivered rather
        # than lost. A persistence failure must never be acked as success.
        return Response(
            content=json.dumps(
                {
                    "accepted": False,
                    "retryable": True,
                    "error": "TRANSIENT_FAILURE",
                    "detail": type(exc).__name__,
                }
            ),
            media_type="application/json",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response(
        content=json.dumps({"accepted": True, **result}),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )


def _rejected(reason: str, detail: str = "") -> Response:
    """Ack a permanently invalid message, recording why rather than hiding it."""
    return Response(
        content=json.dumps(
            {
                "accepted": False,
                "rejected": True,
                "retryable": False,
                "error": reason,
                "detail": detail,
                "note": (
                    "acknowledged to stop endless redelivery of a permanently invalid "
                    "message; no dead-letter topic is configured yet (T089)"
                ),
            }
        ),
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )
