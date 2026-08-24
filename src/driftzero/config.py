"""T069/T070 — runtime configuration boundary for the semantic layer.

Every value here is a **configurable project default**, not a product SLA and not a
vendor guarantee (plan.md § Retry & Timeout Engineering Policy). Defaults are declared
in code so unit tests run with no environment file, no credentials, and no network.

Credentials are deliberately absent. This module holds *behavioural* configuration —
model id, deadlines, attempt caps. Authentication belongs to whatever adapter is
registered at the composition root, which reads ambient application-default credentials
itself. Nothing here reads, stores, or logs a secret.

Import safety: stdlib + pydantic only. The deterministic core and this configuration
boundary must both stay importable with no model SDK installed and no credentials
present, which the M0 purity guard enforces across the whole package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

DEFAULT_SEMANTIC_MODEL_ID = "gemini-3.5-flash"
"""Semantic model for the agent layer (research.md R-002, plan.md § Cost Model)."""

DEFAULT_SEMANTIC_TIMEOUT_SECONDS = 60.0
DEFAULT_SEMANTIC_MAX_RETRIES = 2
"""1 initial attempt + at most 2 retries, transient conditions only."""

DEFAULT_SIDE_EFFECT_TIMEOUT_SECONDS = 30.0
DEFAULT_GEMMA_TIMEOUT_SECONDS = 60.0

MAX_ALLOWED_SEMANTIC_RETRIES = 2
"""Hard ceiling. Configuration may lower this; it may never raise it."""

ENV_PREFIX = "DRIFTZERO_"


class ConfigurationError(ValueError):
    """Configuration violates a binding engineering-policy bound."""


@dataclass(frozen=True)
class SemanticModelConfig:
    """Bounds for semantic (Gemini / agent) calls.

    ``max_retries`` is retries *in addition to* the initial attempt, so the total
    attempt budget is ``max_retries + 1``. A malformed structured response may consume
    one bounded repair attempt from that same budget — it is not an extra allowance.
    """

    model_id: str = DEFAULT_SEMANTIC_MODEL_ID
    timeout_seconds: float = DEFAULT_SEMANTIC_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_SEMANTIC_MAX_RETRIES
    allow_structured_repair: bool = True

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ConfigurationError("semantic model_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("semantic timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ConfigurationError("semantic max_retries must not be negative")
        if self.max_retries > MAX_ALLOWED_SEMANTIC_RETRIES:
            raise ConfigurationError(
                f"semantic max_retries {self.max_retries} exceeds the binding cap "
                f"of {MAX_ALLOWED_SEMANTIC_RETRIES}"
            )

    @property
    def max_attempts(self) -> int:
        """Total attempt budget: the initial call plus permitted retries."""
        return self.max_retries + 1


@dataclass(frozen=True)
class SideEffectConfig:
    """Bounds for deterministic side-effect tool calls (mutation, delivery).

    ``auto_retry`` is fixed False by policy: a timeout after dispatch is an UNKNOWN
    outcome, never a failure, and must go through reconciliation rather than a blind
    retry (plan.md § Action Identity, Idempotency & Crash Reconciliation).
    """

    timeout_seconds: float = DEFAULT_SIDE_EFFECT_TIMEOUT_SECONDS
    auto_retry: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ConfigurationError("side-effect timeout_seconds must be positive")
        if self.auto_retry:
            raise ConfigurationError(
                "side-effect calls must never auto-retry: a post-dispatch timeout is an "
                "UNKNOWN outcome and requires reconciliation"
            )


@dataclass(frozen=True)
class GemmaConfig:
    """Bounds for field-verification inference. G1 may revise this default."""

    timeout_seconds: float = DEFAULT_GEMMA_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ConfigurationError("gemma timeout_seconds must be positive")


@dataclass(frozen=True)
class DriftZeroConfig:
    """The injectable application configuration.

    Construct directly in tests; construct via :meth:`from_env` at the composition
    root. Both paths work with no environment set at all.
    """

    semantic: SemanticModelConfig = SemanticModelConfig()
    side_effect: SideEffectConfig = SideEffectConfig()
    gemma: GemmaConfig = GemmaConfig()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DriftZeroConfig:
        """Build configuration from a mapping, defaulting every unset value.

        ``env`` defaults to the process environment. Passing ``{}`` yields pure
        defaults, which is what unit tests use — no ``.env`` file is ever required.
        """
        source = os.environ if env is None else env

        def _float(name: str, fallback: float) -> float:
            raw = source.get(f"{ENV_PREFIX}{name}")
            if raw is None or not raw.strip():
                return fallback
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{ENV_PREFIX}{name} is not a number: {raw!r}") from exc

        def _int(name: str, fallback: int) -> int:
            raw = source.get(f"{ENV_PREFIX}{name}")
            if raw is None or not raw.strip():
                return fallback
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{ENV_PREFIX}{name} is not an integer: {raw!r}") from exc

        model_id = source.get(f"{ENV_PREFIX}SEMANTIC_MODEL_ID") or DEFAULT_SEMANTIC_MODEL_ID
        return cls(
            semantic=SemanticModelConfig(
                model_id=model_id,
                timeout_seconds=_float(
                    "SEMANTIC_TIMEOUT_SECONDS", DEFAULT_SEMANTIC_TIMEOUT_SECONDS
                ),
                max_retries=_int("SEMANTIC_MAX_RETRIES", DEFAULT_SEMANTIC_MAX_RETRIES),
            ),
            side_effect=SideEffectConfig(
                timeout_seconds=_float(
                    "SIDE_EFFECT_TIMEOUT_SECONDS", DEFAULT_SIDE_EFFECT_TIMEOUT_SECONDS
                )
            ),
            gemma=GemmaConfig(
                timeout_seconds=_float("GEMMA_TIMEOUT_SECONDS", DEFAULT_GEMMA_TIMEOUT_SECONDS)
            ),
        )

    def with_semantic(self, **overrides: object) -> DriftZeroConfig:
        """Return a copy with semantic overrides applied — convenient in tests."""
        return replace(self, semantic=replace(self.semantic, **overrides))  # type: ignore[arg-type]
