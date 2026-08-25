"""T079 — the live Vertex AI MaaS field observation provider.

Calls the OpenAI-compatible Model-as-a-Service endpoint G1 empirically validated:

    POST https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}
         /endpoints/openapi/chat/completions

Serverless and on-demand. No endpoint is created, no model is deployed, and no GPU
serving resource remains running after a call — which is why this route was selected
over self-deployment in the first place.

Credentials
-----------
Application Default Credentials via ``google.auth``. Never ``gcloud auth
print-access-token`` from application code: shelling out to a CLI is not an
authentication strategy, it does not work on Cloud Run, and it leaks a token through a
process argument list and shell history.

The same code path serves local development (operator-configured ADC) and Cloud Run (the
service account's metadata-server ADC). ``google.auth.default()` resolves both.

The access token exists only inside the request headers. It is never logged, never
returned, never stored in evidence, and never rendered — a test asserts it appears in no
evidence document this provider produces.

Authority boundary
------------------
This provider returns the model's raw output verbatim. It does not normalize, interpret,
compare against an expected value, or produce a verdict. Everything it returns is
untrusted until the agent normalizes it and Crossing 4 validates it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

from driftzero.agents.field_verify import (
    ObservationContext,
    ProviderObservation,
    register_field_observation_provider,
)
from driftzero.config import FieldProviderConfig
from driftzero.retry import NonTransientModelError, TransientModelError

PROVIDER_NAME = "vertex_ai_maas"
"""Recorded in every evidence document this provider produces."""

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
"""Retry-eligible. Everything else — 400, 401, 403, 404 — is a deterministic failure.

Retrying an authorization or malformed-request error just burns budget and money.
"""

REDACTED = "[REDACTED]"


class VertexMaaSGemmaObservationProvider:
    """Live Gemma 4 multimodal observation over Vertex AI MaaS.

    One call per :meth:`observe`. Retry policy is owned by the caller
    (:func:`driftzero.retry.run_semantic_call`), so this class never loops: it makes one
    attempt, classifies the failure, and raises. That keeps the attempt budget in one
    place instead of being multiplied by a hidden inner loop.
    """

    def __init__(
        self,
        config: FieldProviderConfig,
        *,
        credentials: Any | None = None,
        transport: Any | None = None,
    ) -> None:
        """``credentials`` and ``transport`` are injectable purely for testing.

        Left unset, both are resolved lazily from the ambient environment, so importing
        this module never requires credentials or a network.
        """
        self._config = config.validated()
        self._credentials = credentials
        self._transport = transport

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    # ------------------------------------------------------------------ credentials

    def _access_token(self) -> str:
        """Resolve an ADC access token, refreshing it when stale.

        The token is returned to the caller *inside this module only* and goes straight
        into a request header. It is never stored on the instance beyond the credentials
        object google-auth itself manages.
        """
        credentials = self._credentials
        if credentials is None:
            import google.auth  # noqa: PLC0415 - kept out of import time on purpose

            credentials, _project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            self._credentials = credentials

        if not getattr(credentials, "valid", False):
            from google.auth.transport.requests import (  # noqa: PLC0415
                Request as AuthRequest,
            )

            credentials.refresh(AuthRequest())

        token = getattr(credentials, "token", None)
        if not token:
            raise NonTransientModelError(
                "Application Default Credentials produced no access token; run "
                "'gcloud auth application-default login' or attach a service account"
            )
        return str(token)

    # ------------------------------------------------------------------ transport

    def _post(self, url: str, payload: dict[str, Any], deadline: float) -> Any:
        """One HTTP POST. Injectable so tests never touch the network."""
        if self._transport is not None:
            return self._transport(url, payload, self._access_token(), deadline)

        import httpx  # noqa: PLC0415 - deliberately not a core dependency

        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        try:
            return httpx.post(url, json=payload, headers=headers, timeout=deadline)
        except httpx.TimeoutException as exc:
            raise TransientModelError(f"request timed out after {deadline}s") from exc
        except httpx.HTTPError as exc:
            raise TransientModelError(f"transport error: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------ observation

    def build_request(
        self, *, image_bytes: bytes, mime_type: str, context: ObservationContext
    ) -> dict[str, Any]:
        """The exact request body.

        The image is inlined as a data URI under its **sniffed** MIME type, not the type
        a client claimed. Sending HEIC bytes labelled ``image/jpeg`` is how a real
        iPhone capture gets silently rejected or misread.

        ``temperature`` is 0 so repeated observation of the same image is as stable as
        the model allows — G1 measured that stability, and a non-zero temperature would
        invalidate it.
        """
        encoded = base64.b64encode(image_bytes).decode("ascii")
        return {
            "model": self._config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": context.prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "max_tokens": context.max_output_tokens,
            "temperature": 0,
            "stream": False,
        }

    def observe(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        context: ObservationContext,
        deadline_seconds: float,
    ) -> ProviderObservation:
        """Make one live MaaS call and return the raw material, uninterpreted."""
        payload = self.build_request(
            image_bytes=image_bytes, mime_type=mime_type, context=context
        )
        request_hash = _hash_request(payload, image_bytes)

        started = time.perf_counter()
        response = self._post(self._config.endpoint, payload, deadline_seconds)
        latency = time.perf_counter() - started

        status = int(getattr(response, "status_code", 0))
        if status in TRANSIENT_STATUS:
            raise TransientModelError(f"MaaS returned retry-eligible HTTP {status}")
        if status != 200:
            raise NonTransientModelError(
                f"MaaS returned HTTP {status}: {_safe_detail(response)}"
            )

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - any parse failure is non-transient
            raise NonTransientModelError(
                f"MaaS response was not JSON: {type(exc).__name__}"
            ) from exc

        return _to_observation(
            body,
            provider=self.name,
            fallback_model=self._config.model,
            request_hash=request_hash,
            http_status=status,
            latency_seconds=round(latency, 4),
        )


def _hash_request(payload: dict[str, Any], image_bytes: bytes) -> str:
    """Hash the request without embedding the image in the evidence.

    The base64 image is replaced by its own SHA-256 before hashing, so the recorded
    value is stable, reproducible from the stored image hash, and small.
    """
    skeleton = json.loads(json.dumps(payload))
    for message in skeleton.get("messages", []):
        for part in message.get("content", []):
            if part.get("type") == "image_url":
                part["image_url"] = {
                    "sha256": hashlib.sha256(image_bytes).hexdigest()
                }
    canonical = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_detail(response: Any) -> str:
    """A short, credential-free error detail.

    Truncated and stripped of any bearer token that could be echoed back in an error
    body. An error message is not worth leaking a credential for.
    """
    try:
        text = str(getattr(response, "text", ""))[:200]
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return REDACTED
    lowered = text.lower()
    if "bearer " in lowered or "authorization" in lowered:
        return REDACTED
    return text


def extract_raw_output(body: dict[str, Any]) -> str:
    """Pull the model's text out of an OpenAI-compatible body without interpreting it."""
    choices = body.get("choices") or []
    if not choices:
        raise NonTransientModelError("MaaS response contained no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        raise NonTransientModelError("MaaS response contained no message content")
    return str(content)


def _to_observation(
    body: dict[str, Any],
    *,
    provider: str,
    fallback_model: str,
    request_hash: str,
    http_status: int,
    latency_seconds: float,
) -> ProviderObservation:
    """Map a MaaS body onto :class:`ProviderObservation`, recording only what is there."""
    usage = body.get("usage") or {}
    google_extras = (usage.get("extra_properties") or {}).get("google") or {}
    choices = body.get("choices") or [{}]
    return ProviderObservation(
        raw_output=extract_raw_output(body),
        provider=provider,
        model=str(body.get("model") or fallback_model),
        response_id=body.get("id"),
        created=body.get("created"),
        finish_reason=choices[0].get("finish_reason"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        traffic_type=google_extras.get("traffic_type"),
        http_status=http_status,
        request_hash=request_hash,
        raw_response_hash=hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        latency_seconds=latency_seconds,
    )


def install() -> None:
    """Register this provider at the composition root.

    Called by the application entry point when ``DRIFTZERO_FIELD_PROVIDER=vertex_maas``.
    Importing this module does not by itself enable live calls.
    """
    register_field_observation_provider(VertexMaaSGemmaObservationProvider)
