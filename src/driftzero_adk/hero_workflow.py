"""T080 — the eleven-step boundary sequence, orchestrated by a real ADK SequentialAgent.

The orchestrator coordinates. It does **not** own truth.

``google.adk.agents.SequentialAgent`` sequences the steps, holds the resumable run state,
and provides the pause/resume boundary. Every consequential decision stays where it
already lived: authorization in the policy table, impact in the Truth Engine, PASS/FAIL
in the frozen comparator, the seven invariants in the frozen proof generator, and
idempotency in the ``ActionLedger``. A test asserts this module contains no such logic.

Delegation, not duplication
---------------------------
Each step is a thin :class:`google.adk.agents.BaseAgent` that calls one existing
application use case. Nothing is reimplemented here, and no step invents an operation id:
the stable action identities the lower layers already derive are what make a resumed or
replayed run free of duplicate side effects.

Deterministic steps stay deterministic
--------------------------------------
Only step 2 involves a model, and it reaches it through the ADK ``LlmAgent`` runtime
already built for steps 1–3. Wrapping the Truth Engine in an LLM agent to make the
diagram look agentic would add a hallucination surface to arithmetic.

Pause and resume
----------------
Step 8 emits a long-running function call, which is how ADK pauses a sequence. The run
stops there with steps 9–11 unexecuted. When field evidence arrives, the same invocation
is resumed and the sequence continues from step 8 without re-running steps 1–7.

``ResumabilityConfig`` is marked EXPERIMENTAL by the ADK; it is what the contract
specifies (contracts/agents.md § Async boundary) and it is exercised by tests here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from driftzero_adk.change_intel_runtime import ADK_APP_NAME, adk_version

WORKFLOW_AGENT_NAME = "driftzero_hero_workflow"
"""The name contracts/agents.md gives the SequentialAgent."""

AWAIT_FIELD_EVIDENCE_CALL_ID = "await-field-evidence"
FIELD_EVIDENCE_READY_KEY = "field_evidence_ready"

INVOCATION_KEY = "driftzero_invocation_id"
"""Where the resumable invocation identity is persisted.

It lives in the ADK session state rather than in this object, because the process
holding this object is exactly what a restart destroys. A fresh instance reads the
identity back out of the durable session and continues the SAME invocation instead of
starting a second one for the same logical workflow."""
"""Session-state flag the resume sets. Presence means "evidence arrived", nothing more."""

USER_ID = "driftzero-pilot"


@dataclass
class StepLog:
    """Non-authoritative execution evidence: what ran, in order, and what paused.

    Deliberately records **execution**, never outcomes. The orchestrator observing that
    step 10 ran says nothing about what step 10 decided.
    """

    executed: list[str] = field(default_factory=list)
    paused_at: str | None = None
    resumed: bool = False
    invocation_id: str | None = None
    session_id: str | None = None
    event_count: int = 0

    def as_evidence(self) -> dict[str, Any]:
        return {
            "orchestrator": "google.adk.agents.SequentialAgent",
            "orchestrator_agent_name": WORKFLOW_AGENT_NAME,
            "adk_version": adk_version(),
            "steps_executed": list(self.executed),
            "paused_at": self.paused_at,
            "resumed": self.resumed,
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "event_count": self.event_count,
            "authoritative": False,
            "note": (
                "Execution evidence only. The orchestrator sequences steps; it decides "
                "no authorization, impact, verdict, or proof."
            ),
        }


def _step_agent(name: str, action: Callable[[], Any], log: StepLog) -> Any:
    """A deterministic ADK step that delegates to one existing use case."""
    from google.adk.agents import BaseAgent
    from google.adk.events import Event
    from google.genai import types

    class _Step(BaseAgent):
        async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Event, None]:
            log.executed.append(self.name)
            log.invocation_id = ctx.invocation_id or log.invocation_id
            # The use cases are synchronous and one of them drives its own event loop
            # (the ADK LlmAgent runtime behind Change Intelligence). Running them on a
            # worker thread keeps that loop from nesting inside this one, and keeps a
            # blocking call off the orchestrator's loop.
            await asyncio.to_thread(action)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=f"{self.name} completed")],
                ),
            )

    return _Step(name=name, description=f"DRIFTZERO {name}")


def _pause_agent(name: str, log: StepLog) -> Any:
    """Step 8 — the async boundary.

    Emits a long-running function call so ADK pauses the sequence. On resume the flag is
    present in session state, so this step completes instead of re-arming and steps 9–11
    proceed. A human being asked for a photograph must not hold an HTTP request open.
    """
    from google.adk.agents import BaseAgent
    from google.adk.events import Event
    from google.genai import types

    class _Pause(BaseAgent):
        async def _run_async_impl(self, ctx: Any) -> AsyncGenerator[Event, None]:
            log.executed.append(self.name)
            log.invocation_id = ctx.invocation_id or log.invocation_id
            ready = bool((ctx.session.state or {}).get(FIELD_EVIDENCE_READY_KEY))
            if ready:
                log.resumed = True
                yield Event(
                    author=self.name,
                    invocation_id=ctx.invocation_id,
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text="field evidence received")],
                    ),
                )
                return
            log.paused_at = self.name
            call = types.FunctionCall(
                id=AWAIT_FIELD_EVIDENCE_CALL_ID, name="await_field_evidence", args={}
            )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                content=types.Content(role="model", parts=[types.Part(function_call=call)]),
                long_running_tool_ids={AWAIT_FIELD_EVIDENCE_CALL_ID},
            )

    return _Pause(name=name, description="Await physical field evidence")


def build_hero_workflow(service: Any, log: StepLog) -> Any:
    """Compose the real ``SequentialAgent`` over the eleven-step boundary sequence.

    Steps are grouped exactly as the existing use cases already draw the boundaries: the
    Truth Engine validation that follows each agent step is performed *inside* that use
    case, which is where it must stay — a step that could choose to skip its own
    validation would defeat the point of having one.
    """
    from google.adk.agents import SequentialAgent

    return SequentialAgent(
        name=WORKFLOW_AGENT_NAME,
        description="DRIFTZERO operational change deployment",
        sub_agents=[
            # Steps 1-3: source ingestion is done at session start; analysis runs the
            # Change Intelligence LlmAgent, Crossing 1, and the impact gate.
            _step_agent("s01_03_change_intelligence_and_impact", service.analyze_change, log),
            # Steps 4-5: remediation through the capability broker, then Crossing 2.
            _step_agent("s04_05_remediation_and_validation", service.deploy_change, log),
            # Steps 6-7: compose the delta, deliver it, then Crossing 3.
            _step_agent("s06_07_delivery_and_validation", service.deliver_to_frontline, log),
            # Step 8: the async boundary.
            _pause_agent("s08_await_field_evidence", log),
            # Steps 9-10: field observation, Crossing 4, deterministic verdict. The
            # evidence itself arrives out of band and is already recorded by resume time.
            _step_agent("s09_10_field_observation_and_verdict", lambda: None, log),
            # Step 11: the frozen seven-invariant proof gate.
            _step_agent("s11_change_proof", service.generate_proof, log),
        ],
    )


@dataclass
class HeroWorkflowRun:
    """One ADK invocation of the hero workflow, resumable across requests."""

    service: Any
    log: StepLog = field(default_factory=StepLog)
    session_service: Any = None
    """Injected. ``None`` keeps the in-memory service, which is the LOCAL_PILOT path.
    A ``FirestoreSessionService`` makes the invocation survive the process."""
    _runner: Any = None
    _sessions: Any = None
    _session_id: str | None = None

    @property
    def session_id(self) -> str:
        """One ADK session per workflow.

        This used to be the constant ``"hero-workflow"``. With a per-instance in-memory
        session service that was invisible; against a shared durable store every
        workflow would have collided into a single session, and one workflow's events
        would have become another's history.
        """
        if self._session_id is None:
            workflow = getattr(getattr(self.service, "_session", None), "workflow", None)
            self._session_id = (
                getattr(workflow, "workflow_id", None) or ADK_APP_NAME
            )
        return self._session_id

    @property
    def durable(self) -> bool:
        return self.session_service is not None

    async def _ensure_runner(self) -> None:
        if self._runner is not None:
            return
        from google.adk.apps import App, ResumabilityConfig
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        app = App(
            name=ADK_APP_NAME,
            root_agent=build_hero_workflow(self.service, self.log),
            # The contract specifies ResumabilityConfig for the step-8 boundary.
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        self._sessions = self.session_service or InMemorySessionService()
        self._runner = Runner(app=app, session_service=self._sessions)
        # create_session on the durable service returns the STORED session when one
        # exists, so a fresh process rebuilds the runner over the existing history
        # rather than starting a blank one.
        await self._sessions.create_session(
            app_name=ADK_APP_NAME, user_id=USER_ID, session_id=self.session_id
        )
        self.log.session_id = self.session_id

    async def start(self) -> StepLog:
        """Run steps 1–7 and pause at step 8."""
        from google.genai import types

        await self._ensure_runner()
        async for event in self._runner.run_async(
            user_id=USER_ID,
            session_id=self.session_id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text="deploy change")]
            ),
        ):
            self.log.event_count += 1
            self.log.invocation_id = event.invocation_id or self.log.invocation_id
        await self._persist_invocation()
        return self.log

    async def _persist_invocation(self) -> None:
        """Record the resumable invocation identity in the durable session state."""
        if not self.durable or not self.log.invocation_id:
            return
        session = await self._sessions.get_session(
            app_name=ADK_APP_NAME, user_id=USER_ID, session_id=self.session_id
        )
        if session is None:  # pragma: no cover - the session was just created
            return
        session.state[INVOCATION_KEY] = self.log.invocation_id
        # set() through the service so the write goes through one code path.
        self._sessions.persist_state(session)

    async def recover(self) -> str | None:
        """Rebuild this run over the stored session, returning the invocation it owns.

        This is the operation a fresh Cloud Run instance performs. It does not replay
        steps 1-7 and does not create a second invocation: it reattaches to the one
        already recorded, so a later ``resume`` continues the same logical execution.
        """
        if not self.durable:
            return None
        await self._ensure_runner()
        session = await self._sessions.get_session(
            app_name=ADK_APP_NAME, user_id=USER_ID, session_id=self.session_id
        )
        if session is None:
            return None
        invocation = (session.state or {}).get(INVOCATION_KEY)
        if invocation:
            self.log.invocation_id = str(invocation)
            self.log.event_count = len(session.events)
        return self.log.invocation_id or None

    async def resume(self) -> StepLog:
        """Resume the *same* invocation once field evidence exists.

        Steps 1–7 are not re-run: ADK restarts the sequence at the paused sub-agent.
        """
        from google.genai import types

        if self._runner is None or not self.log.invocation_id:
            # A durable run can recover the invocation it does not yet hold in memory.
            recovered = await self.recover() if self.durable else None
            if not recovered:
                raise RuntimeError(
                    "the workflow has not been started, so it cannot resume"
                )
        async for _event in self._runner.run_async(
            user_id=USER_ID,
            session_id=self.session_id,
            invocation_id=self.log.invocation_id,
            state_delta={FIELD_EVIDENCE_READY_KEY: True},
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text="field evidence submitted")]
            ),
        ):
            self.log.event_count += 1
        return self.log

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.close()
