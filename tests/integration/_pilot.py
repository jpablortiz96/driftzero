"""Offline harness for driving the console through impact analysis.

Remediation is gated on a deterministically qualified target (T080 steps 1–3), so every
suite that deploys must first run analysis. These helpers register a **real Google ADK
runtime** driven by a stub ``BaseLlm``: the ADK agent, runner, session service, and event
stream are genuine, only the model is substituted. No network, no credentials, no cost.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.models.change import ChangeSet  # noqa: E402

PILOT_CATALOG_IDS = ("WI-114", "WI-118", "WI-207", "WI-330", "WI-402")
"""Every artifact in the pilot catalog. Four of them are decoys."""


def proposal_payload(
    change: Any,
    artifact_ids: tuple[str, ...] = PILOT_CATALOG_IDS,
    **over: Any,
) -> dict[str, Any]:
    """A ChangeSet naming every catalog artifact as a candidate.

    Deliberately maximal: the stub claims *everything* is affected, so any test that ends
    with a single target proves the deterministic gate did the narrowing, not the model.
    """
    payload: dict[str, Any] = {
        "change_id": change.change_id,
        "source_procedure_id": change.source_procedure_id,
        "source_version": change.source_version,
        "operation_id": change.operation_id,
        "requirement_id": change.requirement_id,
        "previous_value": change.previous_value,
        "current_value": change.current_value,
        "authorized_scope": list(change.authorized_scope),
        "candidate_affected_artifacts": [
            {
                "artifact_id": artifact_id,
                "impact_reason": "mentions the changed requirement",
                "operation_match": True,
                "instruction_correspondence": True,
                "value_conflict": True,
                "in_authorized_scope": True,
                "is_affected": True,
            }
            for artifact_id in artifact_ids
        ],
    }
    payload.update(over)
    return payload


class StubHandle:
    """Inspection surface for a stub model.

    ``BaseLlm`` is a pydantic model, so counters declared on the subclass become
    *instance fields* and an assignment to the class is shadowed on read. Keeping the
    counters on this plain object avoids that entirely.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[Any] = []
        self.holder: dict[str, Any] = {}
        self.llm: Any = None


def make_stub_llm(payload_for: Any) -> StubHandle:
    """A real ``BaseLlm`` returning a fixed structured response.

    ``payload_for`` is a callable receiving the ADK ``LlmRequest`` so a test can vary the
    response, inspect what ADK actually sent, or raise.
    """
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    handle = StubHandle()

    class StubLlm(BaseLlm):
        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            handle.calls += 1
            handle.seen.append(llm_request)
            payload = payload_for(llm_request)
            if isinstance(payload, BaseException):
                raise payload
            text = payload if isinstance(payload, str) else json.dumps(payload)
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text=text)]
                ),
                finish_reason="STOP",
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=812,
                    candidates_token_count=190,
                    total_token_count=1002,
                ),
            )

    handle.llm = StubLlm(model="stub-gemini")
    return handle


LIVE_ENV = {
    "DRIFTZERO_SEMANTIC_PROVIDER": "google_adk",
    "DRIFTZERO_GCP_PROJECT": "driftzero-runtime-2026",
    "DRIFTZERO_GEMINI_MODEL": "gemini-3.5-flash",
    "DRIFTZERO_GEMINI_LOCATION": "global",
}
"""Configuration the service checks before it will analyse anything.

Set by :func:`arm_change_intelligence` and removed by
:func:`clear_change_intelligence`, so a suite that arms analysis cannot leave live
configuration behind for one that deliberately tests the unconfigured path.
"""


def arm_change_intelligence(payload_for: Any = None) -> StubHandle:
    """Register the real ADK client with a stub model. Returns the stub for inspection."""
    import os

    from driftzero_adk.change_intel_runtime import GoogleAdkSemanticClient

    os.environ.update(LIVE_ENV)
    handle = make_stub_llm(payload_for or (lambda _req: {}))

    def factory(config: Any) -> GoogleAdkSemanticClient:
        client = GoogleAdkSemanticClient(
            config=config,
            output_schema=ChangeSet,
            model_override=handle.llm,
            # Never touch the Vertex env contract from a test.
            use_vertex=False,
        )
        handle.holder["client"] = client
        return client

    mc.register_model_client_provider(factory)
    return handle


def arm_pilot_analysis(change: Any, **over: Any) -> StubHandle:
    """Arm the default pilot proposal: every catalog artifact proposed as a candidate."""
    return arm_change_intelligence(lambda _req: proposal_payload(change, **over))


def arm_for_service(service: Any, **over: Any) -> StubHandle:
    """Arm analysis for whatever change and catalog *this* service actually loaded.

    Reads the candidate ids off the service's own catalog, so an arbitrary second case
    is driven the same way as the pilot with nothing case-specific written down here.
    """
    catalog_ids = tuple(sorted(service.current_catalog.artifact_ids))
    return arm_change_intelligence(
        lambda _req: proposal_payload(
            service.current_change, artifact_ids=catalog_ids, **over
        )
    )


def analyze_and_deploy(client: Any) -> Any:
    """Advance a console client through analysis into remediation."""
    client.post("/api/hero/analyze")
    return client.post("/api/hero/deploy")


def clear_change_intelligence() -> None:
    """Unregister the client and remove the live configuration it needed."""
    import os

    mc.clear_model_client_provider()
    for key in LIVE_ENV:
        os.environ.pop(key, None)
