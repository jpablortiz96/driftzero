"""T079 — the Field Verification Agent.

Physical field evidence goes in; a normalized :class:`FieldObservation` comes out. That
is the whole job.

Authority boundary
------------------
The agent observes. It does **not** decide. ``FieldObservation`` structurally cannot
carry a verdict — it has no ``verification_result`` field — and nothing in this module
compares the observation to an expected value, sets a workflow state, marks a change
deployed, or produces a Change Proof. The deterministic comparator (T038) owns
``observed == expected → PASS``, and it runs later, elsewhere, on data this layer has no
way to influence.

That separation is the reason the model may be wrong without the system being wrong.

Closed observation domain
-------------------------
``LEFT`` | ``TOP_RIGHT`` | ``INCONCLUSIVE`` — the frozen M0 :class:`ObservedPosition`
enum, reused rather than restated. Model output is untrusted text: it is normalized by
exact match after trimming, and anything outside the domain is rejected rather than
optimistically mapped. ``PASS``, ``RIGHT``, ``probably left``, ``0.92``, and prose all
fail. Guessing at an unrecognized answer is exactly the silent conversion of uncertainty
the specification forbids.

``INCONCLUSIVE`` is a first-class successful observation, not an error. It means the
model looked and could not tell, which is a true and useful thing to record.

Transport neutrality
--------------------
:class:`FieldObservationProvider` is the entire coupling surface. Nothing here knows
about Vertex, HTTP, OAuth, or Google. The real provider lives outside this package —
``src/driftzero`` must stay importable with nothing but pydantic, which the M0 purity
guard enforces — and is registered at the composition root.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from driftzero.capabilities import AgentIdentity, ToolCapability, ToolGrant
from driftzero.config import FieldProviderConfig
from driftzero.field.evidence import FieldImage
from driftzero.models.verification import FieldObservation, ObservedPosition
from driftzero.retry import (
    NonTransientModelError,
    RetryOutcome,
    SemanticCallError,
    run_semantic_call,
)

AGENT_IDENTITY = AgentIdentity.FIELD_VERIFICATION
"""``driftzero-field-verify`` — READ + INFERENCE. Never mutation, never delivery."""

REQUIRED_CAPABILITY = ToolCapability.FIELD_OBSERVATION
"""The single capability this agent may hold."""

FIELD_OBSERVATION_PROMPT = (
    "Observe the physical shipping label position on the visible face of the cardboard "
    "box. Return exactly one token and nothing else: LEFT, TOP_RIGHT, or INCONCLUSIVE. "
    "If the available image does not provide enough visual evidence to determine LEFT "
    "or TOP_RIGHT reliably, return INCONCLUSIVE."
)
"""The server-controlled prompt. Fixed, and the only prompt this product sends.

Byte-identical to the prompt G1 validated against the real physical fixtures, so the
empirical result carries over to production rather than being re-earned.

It asks solely for an observation. It never asks the model whether the change is
correct, whether the box passes, or what the expected value is — the model is not told
the expected value at all, so it cannot anchor on it. A prompt that leaked the expected
answer would turn a verification into a confirmation.
"""

MAX_OUTPUT_TOKENS = 8
"""One token is expected. Eight leaves room for a stray delimiter, not for prose."""


class ObservationStatus(StrEnum):
    """How one observation attempt ended."""

    OBSERVED = "OBSERVED"
    """A valid in-domain observation, ``INCONCLUSIVE`` included."""
    OUT_OF_DOMAIN = "OUT_OF_DOMAIN"
    """The model answered something outside the closed domain. Rejected, not mapped."""
    PROVIDER_FAILED = "PROVIDER_FAILED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    REPLAYED = "REPLAYED"
    """This exact operation already ran; the existing evidence was returned unchanged."""


class NormalizationError(ValueError):
    """Raw model output did not map onto the closed observation domain."""


class ObservationNotAuthorized(Exception):
    """No valid ``FIELD_OBSERVATION`` grant accompanied the request."""


def normalize_observation(raw: object) -> ObservedPosition:
    """Map raw model output onto the closed domain, or raise.

    Accepts only an exact case-insensitive match for ``LEFT``, ``TOP_RIGHT``, or
    ``INCONCLUSIVE`` after trimming surrounding whitespace, quotes, and punctuation.
    Internal separators are normalized so ``top-right`` and ``top right`` resolve, which
    is formatting, not interpretation.

    Everything else raises. There is deliberately no fuzzy match, no prefix match, and
    no "it said left somewhere in the sentence" fallback: a model that did not answer in
    the required form has not produced an observation.
    """
    if isinstance(raw, ObservedPosition):
        return raw
    if not isinstance(raw, str):
        raise NormalizationError(f"non-string model output: {raw!r}")
    cleaned = (
        raw.strip().strip("\"'`.,!;: \n\t").upper().replace("-", "_").replace(" ", "_")
    )
    try:
        return ObservedPosition(cleaned)
    except ValueError as exc:
        raise NormalizationError(f"out-of-domain model output: {raw!r}") from exc


@dataclass(frozen=True)
class ObservationContext:
    """What the provider is told about the submission.

    Carries no expected value and no verdict vocabulary — there is nothing here a
    provider could use to guess the "right" answer.
    """

    change_id: str
    source_version: str
    submission_id: str
    prompt: str = FIELD_OBSERVATION_PROMPT
    max_output_tokens: int = MAX_OUTPUT_TOKENS


@dataclass(frozen=True)
class ProviderObservation:
    """Raw material returned by a provider. Untrusted until normalized.

    Every field is what the provider actually reported. Optional fields stay ``None``
    when the provider did not supply them rather than being filled with a plausible
    value — an unmeasured latency recorded as a number is a fabrication.
    """

    raw_output: str
    provider: str
    model: str
    response_id: str | None = None
    created: int | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    traffic_type: str | None = None
    http_status: int | None = None
    request_hash: str | None = None
    raw_response_hash: str | None = None
    latency_seconds: float | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "raw_output": self.raw_output,
            "response_id": self.response_id,
            "created": self.created,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "traffic_type": self.traffic_type,
            "http_status": self.http_status,
            "request_hash": self.request_hash,
            "raw_response_hash": self.raw_response_hash,
            "latency_seconds": self.latency_seconds,
            "latency_label": (
                "ACTUAL_OBSERVED" if self.latency_seconds is not None else "NOT_RECORDED"
            ),
        }


@runtime_checkable
class FieldObservationProvider(Protocol):
    """The narrowest contract for a multimodal observation call.

    Implementations MUST honour ``deadline_seconds``, MUST return the model's raw output
    without interpreting it, and MUST raise
    :class:`~driftzero.retry.TransientModelError` /
    :class:`~driftzero.retry.NonTransientModelError` to classify failures.

    Implementations MUST NOT normalize, adjudicate, compare against an expected value,
    or return a verdict.
    """

    @property
    def name(self) -> str:
        """Provider identity recorded in evidence."""
        ...

    def observe(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        context: ObservationContext,
        deadline_seconds: float,
    ) -> ProviderObservation:
        """Return the provider's raw observation material. Never a verdict."""
        ...


_provider_factory: Callable[[FieldProviderConfig], FieldObservationProvider] | None = None


def register_field_observation_provider(
    factory: Callable[[FieldProviderConfig], FieldObservationProvider],
) -> None:
    """Register the factory that builds the concrete provider.

    Called by the composition root — a test registering a deterministic fake, or a
    deployment entry point registering the real Vertex client. Keeping the concrete
    provider outside this package is what lets the deterministic core stay installable
    with nothing but pydantic.
    """
    global _provider_factory
    _provider_factory = factory


def clear_field_observation_provider() -> None:
    """Remove any registered factory. Tests use this to restore isolation."""
    global _provider_factory
    _provider_factory = None


def has_field_observation_provider() -> bool:
    return _provider_factory is not None


class FieldProviderUnavailable(RuntimeError):
    """Live observation was requested with no usable provider.

    Raised rather than degrading: a missing provider must surface as a failure the
    operator handles, never as a fabricated observation. There is no offline default
    answer, because there is no honest one.
    """


def get_field_observation_provider(
    config: FieldProviderConfig,
) -> FieldObservationProvider:
    """Build the registered provider, or fail loudly."""
    if not config.enabled:
        raise FieldProviderUnavailable(
            f"field observation is disabled (DRIFTZERO_FIELD_PROVIDER={config.provider})"
        )
    if _provider_factory is None:
        raise FieldProviderUnavailable(
            "no field observation provider is registered; call "
            "register_field_observation_provider() at the composition root"
        )
    return _provider_factory(config)


@dataclass(frozen=True)
class ObservationResult:
    """The outcome of one field observation attempt.

    Structurally incapable of carrying a verdict: there is no ``verification_result``,
    no ``passed``, no ``deployed``, and no ``proof`` field, and a test asserts that.
    """

    status: ObservationStatus
    observation: FieldObservation | None = None
    provider_observation: ProviderObservation | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    attempt_count: int = 0
    failure_reason: str | None = None
    identity: str = str(AGENT_IDENTITY)

    @property
    def succeeded(self) -> bool:
        return self.status is ObservationStatus.OBSERVED and self.observation is not None


def _request_hash(image_sha256: str, context: ObservationContext, model: str) -> str:
    """Stable hash of everything that determined the request.

    Lets an auditor confirm which prompt and which image produced a stored response
    without the evidence having to embed either.
    """
    payload = json.dumps(
        {
            "image_sha256": image_sha256,
            "prompt": context.prompt,
            "model": model,
            "max_output_tokens": context.max_output_tokens,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class FieldVerificationAgent:
    """Derives a normalized observation from a physical evidence image.

    Holds no write capability, no delivery capability, and no verdict authority. It
    presents a ``FIELD_OBSERVATION`` grant it cannot forge and lets the boundary verify
    it, exactly as the Enablement Agent does for delivery.
    """

    identity: AgentIdentity = AGENT_IDENTITY

    def observe(
        self,
        image: FieldImage,
        image_bytes: bytes,
        *,
        provider: FieldObservationProvider,
        context: ObservationContext,
        config: FieldProviderConfig,
        grant: ToolGrant,
        grant_verifier: Callable[[ToolGrant], bool],
        raw_evidence_ref: str,
    ) -> ObservationResult:
        """Run one bounded observation attempt and return what actually happened.

        Authorization is checked before any billable call. Retries follow the frozen
        semantic policy — one initial attempt plus at most two retries, transient
        conditions only — and the attempt count is recorded rather than hidden.
        """
        try:
            self._require_authorization(grant, grant_verifier, context=context)
        except ObservationNotAuthorized as exc:
            return ObservationResult(
                status=ObservationStatus.NOT_AUTHORIZED,
                failure_reason=str(exc),
                attempt_count=0,
                identity=str(self.identity),
            )

        def call(deadline: float) -> ProviderObservation:
            return provider.observe(
                image_bytes=image_bytes,
                mime_type=image.mime_type,
                context=context,
                deadline_seconds=deadline,
            )

        outcome = run_semantic_call(call, config.semantic)
        attempt_count = outcome.attempt_count

        if not outcome.succeeded or outcome.value is None:
            return ObservationResult(
                status=ObservationStatus.PROVIDER_FAILED,
                attempt_count=attempt_count,
                failure_reason=(
                    f"provider call ended {outcome.outcome}"
                    + (f": {outcome.final_error}" if outcome.final_error else "")
                ),
                evidence=self._base_evidence(
                    image, context, attempt_count, provider_observation=None
                ),
                identity=str(self.identity),
            )

        raw = outcome.value
        evidence = self._base_evidence(
            image, context, attempt_count, provider_observation=raw
        )

        try:
            normalized = normalize_observation(raw.raw_output)
        except NormalizationError as exc:
            evidence["normalized_observation"] = None
            evidence["normalization_succeeded"] = False
            evidence["normalization_error"] = str(exc)
            return ObservationResult(
                status=ObservationStatus.OUT_OF_DOMAIN,
                provider_observation=raw,
                attempt_count=attempt_count,
                failure_reason=str(exc),
                evidence=evidence,
                identity=str(self.identity),
            )

        evidence["normalized_observation"] = str(normalized)
        evidence["normalization_succeeded"] = True
        observation = FieldObservation(
            submission_id=context.submission_id,
            raw_evidence_ref=raw_evidence_ref,
            observed_label_position=normalized,
            confidence_note=(
                f"Model {raw.model} returned {raw.raw_output.strip()!r}. "
                "Informational only — never authoritative."
            ),
        )
        return ObservationResult(
            status=ObservationStatus.OBSERVED,
            observation=observation,
            provider_observation=raw,
            attempt_count=attempt_count,
            evidence=evidence,
            identity=str(self.identity),
        )

    def _require_authorization(
        self,
        grant: ToolGrant,
        grant_verifier: Callable[[ToolGrant], bool],
        *,
        context: ObservationContext,
    ) -> None:
        """Fail closed before any billable call leaves the process."""
        if grant is None or grant_verifier is None:
            raise ObservationNotAuthorized(
                f"a broker-issued {REQUIRED_CAPABILITY} grant is required to observe"
            )
        if not grant_verifier(grant):
            raise ObservationNotAuthorized(
                f"the supplied grant is not a valid broker-issued {REQUIRED_CAPABILITY} "
                "capability"
            )
        if not grant.covers(context.change_id):
            raise ObservationNotAuthorized(
                f"the grant does not cover change {context.change_id!r}"
            )
        if grant.source_version != context.source_version:
            raise ObservationNotAuthorized(
                f"the grant was issued for source version {grant.source_version!r}, "
                f"not {context.source_version!r}"
            )

    def _base_evidence(
        self,
        image: FieldImage,
        context: ObservationContext,
        attempt_count: int,
        *,
        provider_observation: ProviderObservation | None,
    ) -> dict[str, Any]:
        """Everything provable about this attempt, and nothing beyond it."""
        document: dict[str, Any] = {
            "change_id": context.change_id,
            "source_version": context.source_version,
            "submission_id": context.submission_id,
            "identity": str(self.identity),
            "capability": str(REQUIRED_CAPABILITY),
            "prompt_sha256": hashlib.sha256(
                context.prompt.encode("utf-8")
            ).hexdigest(),
            "attempt_count": attempt_count,
            **image.as_evidence(),
        }
        if provider_observation is not None:
            document.update(provider_observation.as_evidence())
            document["request_hash"] = provider_observation.request_hash or _request_hash(
                image.sha256, context, provider_observation.model
            )
        return document


def observation_failure_is_retryable(exc: BaseException) -> bool:
    """Whether a provider failure may be retried at all.

    Exposed so a caller cannot decide on its own that an authorization or malformed
    request error is worth another billable attempt.
    """
    return isinstance(exc, SemanticCallError) and not isinstance(
        exc, NonTransientModelError
    )


def evidence_is_replayable(record: Mapping[str, Any]) -> bool:
    """Whether a stored record may be returned instead of calling the model again.

    Only a completed, normalized observation qualifies. A failed or out-of-domain
    attempt is preserved as history but never replayed as an answer.
    """
    return bool(record.get("normalization_succeeded")) and bool(
        record.get("normalized_observation")
    )


__all__ = [
    "AGENT_IDENTITY",
    "FIELD_OBSERVATION_PROMPT",
    "REQUIRED_CAPABILITY",
    "FieldObservationProvider",
    "FieldProviderUnavailable",
    "FieldVerificationAgent",
    "NormalizationError",
    "ObservationContext",
    "ObservationNotAuthorized",
    "ObservationResult",
    "ObservationStatus",
    "ProviderObservation",
    "RetryOutcome",
    "clear_field_observation_provider",
    "evidence_is_replayable",
    "get_field_observation_provider",
    "has_field_observation_provider",
    "normalize_observation",
    "observation_failure_is_retryable",
    "register_field_observation_provider",
]
