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


DEFAULT_SEMANTIC_PROVIDER = "disabled"
"""Live semantic analysis is opt-in. An unconfigured instance makes no billable call."""

SEMANTIC_PROVIDER_GOOGLE_ADK = "google_adk"
SEMANTIC_PROVIDER_DISABLED = "disabled"

DEFAULT_GEMINI_LOCATION = "global"
"""Vertex location for Gemini. ``global`` is the route this deployment uses."""

DEFAULT_FIELD_PROVIDER = "disabled"
"""Live inference is opt-in. An unconfigured instance makes no billable call."""

FIELD_PROVIDER_VERTEX_MAAS = "vertex_maas"
FIELD_PROVIDER_DISABLED = "disabled"

DEFAULT_GCP_LOCATION = "global"
DEFAULT_GEMMA_MODEL = "google/gemma-4-26b-a4b-it-maas"
"""The model G1 empirically validated. Overridable; never silently substituted."""

MAAS_ENDPOINT_TEMPLATE = (
    "https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
    "/endpoints/openapi/chat/completions"
)
"""The Vertex AI MaaS OpenAI-compatible endpoint G1 actually reached."""


@dataclass(frozen=True)
class SemanticProviderConfig:
    """T080 steps 2-3 — how (and whether) change intelligence reaches a live model.

    Deployment configuration lives here rather than inside agent logic, so switching a
    project, a region, or a model is configuration and never a code change.

    Fails closed: :attr:`enabled` is false unless a provider was explicitly selected, and
    :meth:`validated` refuses an incomplete live configuration. A half-configured instance
    must error rather than quietly fall back to a fabricated proposal.
    """

    provider: str = DEFAULT_SEMANTIC_PROVIDER
    project: str = ""
    location: str = DEFAULT_GEMINI_LOCATION
    model: str = DEFAULT_SEMANTIC_MODEL_ID
    semantic: SemanticModelConfig = SemanticModelConfig()

    @property
    def enabled(self) -> bool:
        return self.provider not in ("", SEMANTIC_PROVIDER_DISABLED)

    @property
    def is_live(self) -> bool:
        return self.provider == SEMANTIC_PROVIDER_GOOGLE_ADK

    def missing_settings(self) -> tuple[str, ...]:
        """Configuration a live ADK call needs but does not have."""
        if not self.is_live:
            return ()
        missing = []
        if not self.project.strip():
            missing.append(f"{ENV_PREFIX}GCP_PROJECT")
        if not self.location.strip():
            missing.append(f"{ENV_PREFIX}GEMINI_LOCATION")
        if not self.model.strip():
            missing.append(f"{ENV_PREFIX}GEMINI_MODEL")
        return tuple(missing)

    def validated(self) -> SemanticProviderConfig:
        """Return self, or raise if live analysis is requested but unusable."""
        missing = self.missing_settings()
        if missing:
            raise ConfigurationError(
                "live change intelligence requires " + ", ".join(missing)
            )
        return self

    def as_disclosure(self) -> dict[str, object]:
        """What a UI may honestly say about this configuration. Never a credential."""
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "live": self.is_live,
            "runtime": "Google ADK" if self.is_live else None,
            "project": self.project or None,
            "location": self.location or None,
            "model": self.model or None,
            "timeout_seconds": self.semantic.timeout_seconds,
            "max_attempts": self.semantic.max_attempts,
            "missing_settings": list(self.missing_settings()),
            "credentials": "APPLICATION_DEFAULT_CREDENTIALS",
        }


@dataclass(frozen=True)
class FieldProviderConfig:
    """T079 — how (and whether) field observation reaches a live model.

    Deployment configuration lives here rather than inside the agent, so switching a
    project, a region, or a model is a configuration change and never a code change.

    Fails closed: :attr:`enabled` is false unless a provider was explicitly selected,
    and :meth:`validated` refuses to hand back an incomplete live configuration. A
    half-configured instance must error, never quietly fall back to a fake observation.
    """

    provider: str = DEFAULT_FIELD_PROVIDER
    project: str = ""
    location: str = DEFAULT_GCP_LOCATION
    model: str = DEFAULT_GEMMA_MODEL
    semantic: SemanticModelConfig = SemanticModelConfig(
        model_id=DEFAULT_GEMMA_MODEL,
        timeout_seconds=DEFAULT_GEMMA_TIMEOUT_SECONDS,
        allow_structured_repair=False,
    )
    """Call bounds. Structured repair is off: there is no structure to repair, only a
    single token, and a "repair" round would just be a second billable guess."""

    @property
    def enabled(self) -> bool:
        return self.provider not in ("", FIELD_PROVIDER_DISABLED)

    @property
    def is_live(self) -> bool:
        return self.provider == FIELD_PROVIDER_VERTEX_MAAS

    @property
    def endpoint(self) -> str:
        """The resolved MaaS endpoint. Empty until a project is configured."""
        if not self.project:
            return ""
        return MAAS_ENDPOINT_TEMPLATE.format(
            project=self.project, location=self.location or DEFAULT_GCP_LOCATION
        )

    def missing_settings(self) -> tuple[str, ...]:
        """Configuration a live call needs but does not have."""
        if not self.is_live:
            return ()
        missing = []
        if not self.project.strip():
            missing.append(f"{ENV_PREFIX}GCP_PROJECT")
        if not self.location.strip():
            missing.append(f"{ENV_PREFIX}GCP_LOCATION")
        if not self.model.strip():
            missing.append(f"{ENV_PREFIX}GEMMA_MODEL")
        return tuple(missing)

    def validated(self) -> FieldProviderConfig:
        """Return self, or raise if live observation is requested but unusable."""
        missing = self.missing_settings()
        if missing:
            raise ConfigurationError(
                "live field observation requires " + ", ".join(missing)
            )
        return self

    def as_disclosure(self) -> dict[str, object]:
        """What a UI may honestly say about this configuration. Never a credential."""
        return {
            "provider": self.provider,
            "enabled": self.enabled,
            "live": self.is_live,
            "project": self.project or None,
            "location": self.location or None,
            "model": self.model or None,
            "endpoint": self.endpoint or None,
            "timeout_seconds": self.semantic.timeout_seconds,
            "max_attempts": self.semantic.max_attempts,
            "missing_settings": list(self.missing_settings()),
            "credentials": "APPLICATION_DEFAULT_CREDENTIALS",
        }


DEFAULT_PERSISTENCE = "memory"
"""Durable persistence is opt-in. An unconfigured process stays in-memory, so no test
and no offline run can reach Google Cloud by accident."""

PERSISTENCE_MEMORY = "memory"
PERSISTENCE_FIRESTORE = "firestore"

DEFAULT_FIRESTORE_DATABASE = "(default)"


@dataclass(frozen=True)
class PersistenceConfig:
    """T092/T093 — where durable state and evidence live.

    Configuration only: this module is inside the M0 purity boundary and must never
    import a Google SDK. The adapters in ``driftzero_cloud`` read these values; nothing
    here opens a connection.

    Fails closed the same way the model providers do. ``backend`` is ``memory`` unless a
    durable backend is explicitly selected, and :meth:`validated` refuses a
    half-configured cloud backend rather than silently writing somewhere unintended.
    """

    backend: str = DEFAULT_PERSISTENCE
    project: str = ""
    database: str = DEFAULT_FIRESTORE_DATABASE
    evidence_bucket: str = ""
    region: str = ""

    @property
    def is_durable(self) -> bool:
        return self.backend == PERSISTENCE_FIRESTORE

    def missing_settings(self) -> tuple[str, ...]:
        """Configuration a durable backend needs but does not have."""
        if not self.is_durable:
            return ()
        missing = []
        if not self.project.strip():
            missing.append(f"{ENV_PREFIX}GCP_PROJECT")
        if not self.database.strip():
            missing.append(f"{ENV_PREFIX}FIRESTORE_DATABASE")
        if not self.evidence_bucket.strip():
            missing.append(f"{ENV_PREFIX}EVIDENCE_BUCKET")
        return tuple(missing)

    def validated(self) -> PersistenceConfig:
        """Return self, or raise if durable persistence is requested but unusable."""
        missing = self.missing_settings()
        if missing:
            raise ConfigurationError(
                "durable persistence requires " + ", ".join(missing)
            )
        return self

    def as_disclosure(self) -> dict[str, object]:
        """What a UI may honestly say about persistence. Never a credential."""
        return {
            "backend": self.backend,
            "durable": self.is_durable,
            "project": self.project or None,
            "database": self.database if self.is_durable else None,
            "evidence_bucket": self.evidence_bucket or None,
            "missing_settings": list(self.missing_settings()),
        }


@dataclass(frozen=True)
class DriftZeroConfig:
    """The injectable application configuration.

    Construct directly in tests; construct via :meth:`from_env` at the composition
    root. Both paths work with no environment set at all.
    """

    semantic: SemanticModelConfig = SemanticModelConfig()
    side_effect: SideEffectConfig = SideEffectConfig()
    gemma: GemmaConfig = GemmaConfig()
    field_provider: FieldProviderConfig = FieldProviderConfig()
    semantic_provider: SemanticProviderConfig = SemanticProviderConfig()
    persistence: PersistenceConfig = PersistenceConfig()

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

        def _text(name: str, fallback: str) -> str:
            raw = source.get(f"{ENV_PREFIX}{name}")
            return raw.strip() if raw and raw.strip() else fallback

        model_id = source.get(f"{ENV_PREFIX}SEMANTIC_MODEL_ID") or DEFAULT_SEMANTIC_MODEL_ID
        gemma_timeout = _float("GEMMA_TIMEOUT_SECONDS", DEFAULT_GEMMA_TIMEOUT_SECONDS)
        gemma_model = _text("GEMMA_MODEL", DEFAULT_GEMMA_MODEL)
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
            gemma=GemmaConfig(timeout_seconds=gemma_timeout),
            semantic_provider=SemanticProviderConfig(
                provider=_text("SEMANTIC_PROVIDER", DEFAULT_SEMANTIC_PROVIDER),
                project=_text("GCP_PROJECT", ""),
                location=_text("GEMINI_LOCATION", DEFAULT_GEMINI_LOCATION),
                model=_text("GEMINI_MODEL", model_id),
                semantic=SemanticModelConfig(
                    model_id=_text("GEMINI_MODEL", model_id),
                    timeout_seconds=_float(
                        "SEMANTIC_TIMEOUT_SECONDS", DEFAULT_SEMANTIC_TIMEOUT_SECONDS
                    ),
                    max_retries=_int("SEMANTIC_MAX_RETRIES", DEFAULT_SEMANTIC_MAX_RETRIES),
                ),
            ),
            field_provider=FieldProviderConfig(
                provider=_text("FIELD_PROVIDER", DEFAULT_FIELD_PROVIDER),
                project=_text("GCP_PROJECT", ""),
                location=_text("GCP_LOCATION", DEFAULT_GCP_LOCATION),
                model=gemma_model,
                semantic=SemanticModelConfig(
                    model_id=gemma_model,
                    timeout_seconds=gemma_timeout,
                    max_retries=_int("SEMANTIC_MAX_RETRIES", DEFAULT_SEMANTIC_MAX_RETRIES),
                    allow_structured_repair=False,
                ),
            ),
            persistence=PersistenceConfig(
                backend=_text("PERSISTENCE", DEFAULT_PERSISTENCE),
                project=_text("GCP_PROJECT", ""),
                database=_text("FIRESTORE_DATABASE", DEFAULT_FIRESTORE_DATABASE),
                evidence_bucket=_text("EVIDENCE_BUCKET", ""),
                region=_text("GCP_REGION", ""),
            ),
        )

    def with_semantic(self, **overrides: object) -> DriftZeroConfig:
        """Return a copy with semantic overrides applied — convenient in tests."""
        return replace(self, semantic=replace(self.semantic, **overrides))  # type: ignore[arg-type]
