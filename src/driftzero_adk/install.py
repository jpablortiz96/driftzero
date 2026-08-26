"""Composition root for the Google ADK semantic runtime.

Importing this module pulls in the ADK, so nothing in ``src/driftzero`` may import it.
The console calls :func:`install_change_intelligence` only when the operator has selected
``DRIFTZERO_SEMANTIC_PROVIDER=google_adk``.
"""

from __future__ import annotations

from driftzero.agents.model_client import register_model_client_provider
from driftzero.config import SemanticModelConfig, SemanticProviderConfig
from driftzero.models.change import ChangeSet
from driftzero_adk.change_intel_runtime import GoogleAdkSemanticClient, adk_version


def install_change_intelligence(config: SemanticProviderConfig) -> str:
    """Register the real ADK client as the semantic model provider.

    Returns the ADK version actually installed, so the startup banner reports what is
    running rather than what was intended.

    Registration is not a call: nothing reaches Gemini until an analysis is requested.
    """
    validated = config.validated()

    def factory(semantic: SemanticModelConfig) -> GoogleAdkSemanticClient:
        return GoogleAdkSemanticClient(
            config=semantic,
            # ADK constrains decoding to this exact frozen model. The schema the Truth
            # Engine validates against and the schema the model is given are the same
            # object, so they cannot drift apart.
            output_schema=ChangeSet,
            project=validated.project,
            location=validated.location,
        )

    register_model_client_provider(factory)
    return adk_version()
