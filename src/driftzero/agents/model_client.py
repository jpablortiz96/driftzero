"""T071 — the semantic model boundary.

Defines *what* the agent layer needs from a model without naming *which* model library
provides it. A structural :class:`SemanticModelClient` protocol lets a fake stand in
during tests and a real client be registered later at the composition root.

The inversion is deliberate rather than decorative. The M0 purity guard scans every file
under ``src/driftzero`` and rejects any cloud or model SDK import, so a concrete adapter
cannot live here even behind a lazy import. Registering a provider from outside the
package keeps the guard green and keeps the distribution installable with nothing but
pydantic.

Authority boundary: a client returns **raw structured material**. It returns no verdict,
no workflow state, and no authorization. Whatever it returns is untrusted until the
deterministic layer validates it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from driftzero.config import SemanticModelConfig


class ModelClientUnavailable(RuntimeError):
    """No semantic client is registered.

    Raised instead of silently degrading: a missing model must surface as a failure the
    caller handles, never as an empty proposal that looks like a real answer.
    """


@dataclass(frozen=True)
class SemanticRequest:
    """One structured-output request.

    ``untrusted_artifact_text`` is kept in its own field precisely so it can never be
    confused with instruction text. It is enterprise source material: data to be read,
    never a source of instructions (see :mod:`driftzero.agents.change_intel`).
    """

    system_instruction: str
    task_instruction: str
    untrusted_artifact_text: str
    schema_name: str
    model_id: str
    deadline_seconds: float
    repair_hint: str | None = None
    """Set only on a bounded repair attempt, describing how the prior response failed."""


@runtime_checkable
class SemanticModelClient(Protocol):
    """Minimal structural contract for a semantic model.

    Implementations MUST:

    * honour ``request.deadline_seconds``;
    * return a mapping of raw structured fields;
    * raise :class:`driftzero.retry.TransientModelError` for retry-eligible conditions,
      :class:`driftzero.retry.NonTransientModelError` otherwise, and
      :class:`driftzero.retry.MalformedStructuredOutput` when the response cannot be
      parsed into a structure at all.

    Implementations MUST NOT expose tool-calling driven by model output, decide any
    workflow outcome, or return a PASS/FAIL verdict.
    """

    def generate_structured(self, request: SemanticRequest) -> Mapping[str, Any]:
        """Return raw structured fields for ``request``. Never a verdict."""
        ...


_provider: Callable[[SemanticModelConfig], SemanticModelClient] | None = None


def register_model_client_provider(
    provider: Callable[[SemanticModelConfig], SemanticModelClient],
) -> None:
    """Register the factory that builds the concrete client.

    Called by the composition root — a test registering a fake, or a deployment entry
    point registering a real client. Keeping this out of the package is what allows the
    real SDK to be introduced later without touching the deterministic core.
    """
    global _provider
    _provider = provider


def clear_model_client_provider() -> None:
    """Remove any registered provider. Tests use this to restore isolation."""
    global _provider
    _provider = None


def get_model_client(config: SemanticModelConfig) -> SemanticModelClient:
    """Build the registered client, or fail loudly."""
    if _provider is None:
        raise ModelClientUnavailable(
            "no semantic model client is registered; call "
            "register_model_client_provider() at the composition root"
        )
    return _provider(config)
