"""Composition root for the live model providers.

Selecting a live provider is a deployment decision, so the concrete client is imported
only when configuration asks for it. An instance configured for the deterministic path
never needs ``google-adk``, ``google-auth`` or ``httpx`` installed, and nothing inside
``src/driftzero`` imports them at all.

This module exists because two transports — the Mission Control console and the
production API — both need to install providers, and a composition root duplicated
across two entrypoints is a composition root that drifts. One of them silently not
installing a provider is exactly the failure that makes a deployed service quietly fall
back to "no analysis was performed".

Neither function raises. A missing dependency or an incomplete configuration degrades to
*no observation is possible*, which the product states plainly, rather than to a
fabricated observation or proposal.
"""

from __future__ import annotations

from driftzero.config import DriftZeroConfig

__all__ = ["configure_field_provider", "configure_semantic_provider"]


def configure_field_provider() -> str:
    """Install the live field-observation provider, if one is configured.

    Returns a short ASCII status line for a startup banner or a structured log — ASCII
    because this reaches a terminal, and a Windows console encodes cp1252.
    """
    config = DriftZeroConfig.from_env().field_provider
    if not config.is_live:
        return f"field provider: {config.provider} (no live model call is possible)"
    missing = config.missing_settings()
    if missing:
        return "field provider: MISCONFIGURED - missing " + ", ".join(missing)
    try:
        from driftzero_providers.vertex_maas import install  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        return (
            f"field provider: UNAVAILABLE - {exc}. Install the live extra: "
            'pip install -e ".[live]"'
        )
    install()
    return f"field provider: {config.provider} -> {config.model}"


def configure_semantic_provider() -> str:
    """Install the real Google ADK semantic runtime, if configured.

    Registration is not a call: nothing reaches Gemini until an analysis is requested.
    """
    config = DriftZeroConfig.from_env().semantic_provider
    if not config.is_live:
        return f"semantic provider: {config.provider} (no live model call is possible)"
    missing = config.missing_settings()
    if missing:
        return "semantic provider: MISCONFIGURED - missing " + ", ".join(missing)
    try:
        from driftzero_adk.install import install_change_intelligence  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        return (
            f"semantic provider: UNAVAILABLE - {exc}. Install the live extra: "
            'pip install -e ".[live]"'
        )
    version = install_change_intelligence(config)
    return (
        f"semantic provider: google_adk (ADK {version}) -> {config.model} @ {config.location}"
    )
