"""T080 steps 2–3 — the real Google ADK execution path for Change Intelligence.

This is an actual ADK runtime, not a wrapper wearing the name. Every call constructs a
real :class:`google.adk.agents.LlmAgent`, hands it to a real
:class:`google.adk.runners.Runner` with a real ``SessionService``, and consumes the real
``Event`` stream the runner yields. Structured output is enforced by ADK itself through
``output_schema``, which it translates into a ``response_schema`` +
``response_mime_type=application/json`` on the underlying Gemini request.

Why that matters
----------------
The ADK is what supplies the agent loop, the session, the invocation identity, the event
stream, and the schema-constrained decoding. Reimplementing those and calling the result
"ADK" would leave the evidence claiming a runtime that was never used. A test asserts the
concrete ADK classes are the ones executing.

Authority boundary
------------------
``tools=[]``, always. The Change Intelligence Agent is READ/ANALYZE, its reads happen
*before* the model call in :mod:`driftzero.agents.change_intel`, and their results arrive
as data. Model output cannot select, parameterize, or trigger a tool because no tool is
registered with the runtime at all — which is what makes "call this tool" inert rather
than merely filtered.

This module returns raw structured fields. It validates no business rule, decides no
impact, and produces no verdict. Everything it returns is untrusted until Crossing 1.

Credentials
-----------
Application Default Credentials, resolved by the GenAI SDK from the ambient environment.
No token is read, stored, logged, or returned here, and nothing shells out to ``gcloud``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from driftzero.agents.model_client import SemanticRequest
from driftzero.config import SemanticModelConfig
from driftzero.retry import (
    MalformedStructuredOutput,
    NonTransientModelError,
    TransientModelError,
)

ADK_APP_NAME = "driftzero"
CHANGE_INTEL_AGENT_NAME = "driftzero_change_intel"
"""ADK agent name. Mirrors the logical identity in contracts/agents.md."""

OUTPUT_KEY = "change_set"

TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
"""Retry-eligible HTTP classes. 400/401/403/404 are deterministic and never retried."""

USER_ID = "driftzero-pilot"
"""Opaque ADK session principal. Not a person, and carries no PII."""


def adk_version() -> str:
    """The ADK version actually executing, recorded in evidence.

    ``google.adk.version`` is a *module*, not a string — read its ``__version__`` and
    fall back to distribution metadata rather than stringifying the module object.
    """
    try:
        from google.adk.version import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return _dist_version("google-adk")


def _dist_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version(name)
    except PackageNotFoundError:  # pragma: no cover - depends on the optional extra
        return "unknown"


@dataclass
class AdkCallEvidence:
    """What actually happened on one ADK invocation. Only measured values."""

    invocation_id: str | None = None
    model: str = ""
    model_version: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    event_count: int = 0
    author: str | None = None
    latency_seconds: float | None = None
    prompt_hash: str = ""
    request_hash: str = ""
    raw_response_hash: str = ""
    adk_version: str = ""
    adk_agent_class: str = ""
    adk_runner_class: str = ""
    session_service_class: str = ""
    tools_registered: int = 0
    output_schema: str = ""
    error: str | None = None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "provider": "google_adk",
            "adk_version": self.adk_version,
            "adk_agent_class": self.adk_agent_class,
            "adk_runner_class": self.adk_runner_class,
            "session_service_class": self.session_service_class,
            "invocation_id": self.invocation_id,
            "model": self.model,
            "model_version": self.model_version,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "event_count": self.event_count,
            "author": self.author,
            "tools_registered": self.tools_registered,
            "output_schema": self.output_schema,
            "prompt_hash": self.prompt_hash,
            "request_hash": self.request_hash,
            "raw_response_hash": self.raw_response_hash,
            "latency_seconds": self.latency_seconds,
            "latency_label": (
                "ACTUAL_OBSERVED" if self.latency_seconds is not None else "NOT_RECORDED"
            ),
            "error": self.error,
        }


@dataclass
class GoogleAdkSemanticClient:
    """A ``SemanticModelClient`` backed by a real ADK ``Runner``.

    Implements the frozen structural protocol, so
    :class:`~driftzero.agents.change_intel.ChangeIntelligenceAgent` is unchanged and
    unaware of the ADK. One call, one invocation: retries are the caller's frozen policy,
    never an inner loop here.
    """

    config: SemanticModelConfig
    output_schema: type
    """The pydantic model ADK constrains decoding to. Passed straight to ``LlmAgent``."""
    project: str = ""
    location: str = ""
    use_vertex: bool = True
    model_override: Any = None
    """Injectable ``BaseLlm`` for offline tests. Unset in production."""
    last_call_evidence: AdkCallEvidence | None = field(default=None, init=False)

    # ------------------------------------------------------------------ protocol

    def generate_structured(self, request: SemanticRequest) -> Mapping[str, Any]:
        """Run one real ADK invocation and return the raw structured fields."""
        prompt = build_user_message(request)
        instruction = build_instruction(request)
        evidence = AdkCallEvidence(
            model=str(self.model_override or request.model_id),
            adk_version=adk_version(),
            output_schema=self.output_schema.__name__,
            prompt_hash=_sha256(instruction + "\x1f" + prompt),
        )
        self.last_call_evidence = evidence

        try:
            payload, evidence.request_hash = self._invoke(
                instruction, prompt, request, evidence
            )
        except (TransientModelError, NonTransientModelError, MalformedStructuredOutput):
            raise
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            evidence.error = f"{type(exc).__name__}: {exc}"
            raise classify_google_error(exc) from exc

        return payload

    # ------------------------------------------------------------------ internals

    def _invoke(
        self,
        instruction: str,
        prompt: str,
        request: SemanticRequest,
        evidence: AdkCallEvidence,
    ) -> tuple[Mapping[str, Any], str]:
        from google.adk.agents import LlmAgent  # noqa: PLC0415
        from google.adk.runners import Runner  # noqa: PLC0415
        from google.adk.sessions import InMemorySessionService  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        self._configure_backend()

        agent = LlmAgent(
            name=CHANGE_INTEL_AGENT_NAME,
            model=self.model_override or request.model_id,
            instruction=instruction,
            description="Extracts a structured ChangeSet. Read and analyse only.",
            output_schema=self.output_schema,
            output_key=OUTPUT_KEY,
            # No tool is registered with the runtime, so model output has nothing to
            # invoke. This is the injection boundary, and it is structural.
            tools=[],
        )
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=ADK_APP_NAME, agent=agent, session_service=session_service
        )

        evidence.adk_agent_class = type(agent).__name__
        evidence.adk_runner_class = type(runner).__name__
        evidence.session_service_class = type(session_service).__name__
        evidence.tools_registered = len(agent.tools)

        message = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        request_hash = _sha256(
            json.dumps(
                {
                    "model": evidence.model,
                    "instruction": instruction,
                    "prompt": prompt,
                    "output_schema": self.output_schema.__name__,
                    "tools": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

        session_id = f"sess-{uuid.uuid4().hex[:12]}"
        started = time.perf_counter()
        try:
            text = asyncio.run(
                _run_agent(
                    runner=runner,
                    session_service=session_service,
                    session_id=session_id,
                    message=message,
                    evidence=evidence,
                    deadline=request.deadline_seconds,
                )
            )
        finally:
            evidence.latency_seconds = round(time.perf_counter() - started, 4)

        evidence.raw_response_hash = _sha256(text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedStructuredOutput(
                f"ADK returned non-JSON output: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise MalformedStructuredOutput(
                f"ADK returned {type(payload).__name__}, not an object"
            )
        return payload, request_hash

    def _configure_backend(self) -> None:
        """Point the GenAI SDK at Vertex AI with ADC, via its documented env contract.

        Set only when configured, and never overwriting a value the operator already
        exported. No credential is read or written here — the SDK resolves ADC itself.
        """
        if not self.use_vertex:
            return
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")
        if self.project:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", self.project)
        if self.location:
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", self.location)


async def _run_agent(
    *,
    runner: Any,
    session_service: Any,
    session_id: str,
    message: Any,
    evidence: AdkCallEvidence,
    deadline: float,
) -> str:
    """Drive the real ADK event stream and return the final response text.

    ``run_async`` is used rather than the sync ``Runner.run``, which the ADK documents as
    a local-testing convenience. The whole invocation is bounded by the caller's deadline
    so a hung stream cannot outlive the frozen timeout policy.
    """
    await session_service.create_session(
        app_name=ADK_APP_NAME, user_id=USER_ID, session_id=session_id
    )

    async def _consume() -> str:
        final_text = ""
        async for event in runner.run_async(
            user_id=USER_ID, session_id=session_id, new_message=message
        ):
            evidence.event_count += 1
            evidence.invocation_id = event.invocation_id or evidence.invocation_id
            evidence.author = event.author or evidence.author
            if event.error_code or event.error_message:
                # ADK converts an upstream failure into an error *event* rather than
                # letting the exception escape. Classifying every one of those as
                # non-transient would mean a 503 is never retried, so the code and
                # message are mapped back onto the frozen retry vocabulary.
                raise classify_event_error(event.error_code, event.error_message)
            if event.finish_reason is not None:
                evidence.finish_reason = str(event.finish_reason)
            if event.model_version:
                evidence.model_version = event.model_version
            usage = event.usage_metadata
            if usage is not None:
                evidence.prompt_tokens = usage.prompt_token_count
                evidence.completion_tokens = usage.candidates_token_count
                evidence.total_tokens = usage.total_token_count
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(
                    part.text or "" for part in event.content.parts
                )
        return final_text

    try:
        text = await asyncio.wait_for(_consume(), timeout=deadline)
    except TimeoutError as exc:
        raise TransientModelError(
            f"the ADK invocation exceeded its {deadline}s deadline"
        ) from exc
    finally:
        await runner.close()

    if not text.strip():
        raise MalformedStructuredOutput(
            "the ADK run produced no final structured response"
        )
    return text


def build_instruction(request: SemanticRequest) -> str:
    """The system instruction. Server-controlled and never caller-supplied.

    ADK passes this to Gemini as ``system_instruction``, structurally separate from the
    user turn that carries untrusted artifact text.
    """
    parts = [request.system_instruction, request.task_instruction]
    if request.repair_hint:
        parts.append(request.repair_hint)
    return "\n\n".join(part for part in parts if part)


def build_user_message(request: SemanticRequest) -> str:
    """The user turn: untrusted material, fenced and labelled as data.

    The fence is honesty about provenance for a human reading the transcript, not a
    security control. The actual boundary is that no tool exists and the output schema
    has no field capable of carrying authority.
    """
    return (
        "<ARTIFACT_DATA>\n"
        f"{request.untrusted_artifact_text}\n"
        "</ARTIFACT_DATA>"
    )


TRANSIENT_MARKERS = (
    "transient",
    "timeout",
    "deadline",
    "unavailable",
    "resource_exhausted",
    "resourceexhausted",
    "too many requests",
    "rate limit",
    "overloaded",
    "internal",
)
"""Substrings that identify a retry-eligible ADK error event.

Matched case-insensitively against the event's code and message. Anything unmatched is
non-transient: failing closed on an error we do not recognise beats retrying it and
paying for the privilege.
"""


NON_TRANSIENT_MARKERS = (
    "nontransient",
    "non_transient",
    "non-transient",
    "permission",
    "unauthenticated",
    "unauthorized",
    "invalid_argument",
    "not_found",
)
"""Checked **before** :data:`TRANSIENT_MARKERS`, which it overlaps by substring.

``NonTransientModelError`` contains the literal text ``transient``; without this
precedence a permanent authorization failure would be retried twice at the operator's
expense.
"""

SCHEMA_MARKERS = ("validation", "schema", "pydantic", "parse", "decode")
"""A model that broke the contract gets the frozen policy's single bounded repair."""


def classify_event_error(code: object, message: object) -> Exception:
    """Map an ADK error event onto the frozen retry vocabulary."""
    blob = f"{code or ''} {message or ''}".lower()
    detail = f"ADK reported {code}: {message}"
    if any(marker in blob for marker in NON_TRANSIENT_MARKERS):
        return NonTransientModelError(detail)
    if any(marker in blob for marker in SCHEMA_MARKERS):
        return MalformedStructuredOutput(detail)
    for status in TRANSIENT_STATUS:
        if str(status) in blob:
            return TransientModelError(detail)
    if any(marker in blob for marker in TRANSIENT_MARKERS):
        return TransientModelError(detail)
    return NonTransientModelError(detail)


def classify_google_error(exc: BaseException) -> Exception:
    """Map a GenAI/ADK failure onto the frozen retry vocabulary.

    Unknown failures are classified non-transient: failing closed on something we do not
    understand beats retrying it and paying for the privilege.
    """
    code = getattr(exc, "code", None)
    if code is None:
        status = getattr(exc, "status", None)
        code = status if isinstance(status, int) else None
    if isinstance(code, int):
        if code in TRANSIENT_STATUS:
            return TransientModelError(f"retry-eligible upstream status {code}")
        return NonTransientModelError(f"deterministic upstream status {code}")
    if isinstance(exc, TimeoutError):
        return TransientModelError(f"timeout: {exc}")
    return NonTransientModelError(f"{type(exc).__name__}: {exc}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
