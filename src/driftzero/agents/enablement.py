"""T077 — the Frontline Enablement Agent and delta composition.

Turns an approved (and where available, validated) change into a structured
:class:`DeltaInstruction` a frontline worker can act on: what changed, from what, to
what, and what deliberately did **not** change.

Scope boundary with T078
------------------------
This agent **composes**. It does not deliver. The delivery mechanism, its resolvable
receipt, and Crossing 3 belong to T078, so nothing here produces a ``DeliveryResult`` or
sets ``delivered``. Composing a delta and delivering it are different claims, and a
receipt this agent invented would be exactly the "agent asserts delivered" failure the
contract forbids.

Authority boundary
------------------
READ + compose. The agent never mutates an artifact, never obtains
``ARTIFACT_MUTATION`` (the authorization policy denies ``driftzero-enablement``), never
determines PASS/FAIL, never generates a Change Proof, and never alters workflow truth.
Its output is operational content, not a decision.

Generality
----------
Composition is driven entirely by the change data. Nothing here knows about
``label_position``, ``LEFT``, or ``TOP_RIGHT``: any structured requirement and value pair
composes the same way. The packing-label case is the first pilot, not a special case in
the code.

No model call
-------------
Composition is deterministic templating over validated fields. A semantic model could
later improve phrasing, but it must never become the source of the *facts* — those come
from the approved change, and inventing a rationale the source never stated would be a
fabrication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from driftzero.capabilities import AgentIdentity
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange

AGENT_IDENTITY = AgentIdentity.ENABLEMENT
"""``driftzero-enablement`` — READ + NOTIFY, never mutation."""

NO_RATIONALE_PROVIDED = "No rationale was provided with the approved change."
"""Stated plainly rather than invented. The source data carries no 'why' field."""


class DeltaStatus(StrEnum):
    """Outcome of one composition attempt."""

    COMPOSED = "COMPOSED"
    NO_CHANGE = "NO_CHANGE"
    ARTIFACT_MISMATCH = "ARTIFACT_MISMATCH"
    REQUIREMENT_NOT_PRESENT = "REQUIREMENT_NOT_PRESENT"
    NOT_IN_AUTHORIZED_SCOPE = "NOT_IN_AUTHORIZED_SCOPE"


class DeltaInstruction(BaseModel):
    """The structured operational delta shown to a frontline worker.

    Structured, not prose-only: the UI renders the fields, so a phrasing change can
    never alter which requirement changed or what its values were.

    ``unchanged_context`` is carried explicitly. Telling someone what changed without
    showing what stayed the same is how a worker "helpfully" updates the wrong thing —
    the packing case has an unrelated instruction that itself mentions the old value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    before_value: str = Field(min_length=1)
    after_value: str = Field(min_length=1)
    concise_instruction: str = Field(min_length=1, description="One actionable sentence")
    unchanged_context: dict[str, str] = Field(
        default_factory=dict,
        description="Requirements deliberately NOT changed by this delta",
    )
    source_procedure_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    previous_version: str = Field(min_length=1)
    source_evidence_ref: str = Field(min_length=1)
    rationale: str | None = Field(
        default=None,
        description="Only when the source states one. Never inferred or invented.",
    )
    delivery_established: bool = Field(
        default=False,
        description="Always False here: T077 composes, T078 proves delivery.",
    )


class FrontlineAcknowledgment(BaseModel):
    """A worker acknowledgment. An application event, never a verification.

    Acknowledgment means "this instruction was displayed and confirmed read". It does
    **not** mean the change was deployed, does not establish DELIVERED, and is never
    PASS — physical verification is a separate, later step against real evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    acknowledged: bool
    acknowledged_at: datetime
    operator_ref: str = Field(min_length=1, description="Opaque session reference — no PII")
    identity_basis: str = Field(
        min_length=1,
        description="How the operator was identified. Honest about what is not proven.",
    )
    establishes_delivery: bool = Field(default=False)
    establishes_verification: bool = Field(default=False)


@dataclass(frozen=True)
class DeltaCompositionResult:
    """What the agent produced, and why if it produced nothing."""

    status: DeltaStatus
    instruction: DeltaInstruction | None = None
    reason: str | None = None
    identity: str = str(AGENT_IDENTITY)

    @property
    def composed(self) -> bool:
        return self.status is DeltaStatus.COMPOSED and self.instruction is not None


def compose_instruction_text(requirement_id: str, before: str, after: str) -> str:
    """One actionable sentence, generated from data alone.

    Deliberately value-agnostic: no branch anywhere reads ``label_position`` or
    ``LEFT``. A different requirement composes identically.
    """
    readable = requirement_id.replace("_", " ").strip()
    return f"Set {readable} to {after}. It was previously {before}."


@dataclass
class FrontlineEnablementAgent:
    """Composes the operational delta. Holds no write capability of any kind."""

    identity: AgentIdentity = AGENT_IDENTITY

    def compose_delta(
        self,
        *,
        change: ApprovedChange,
        artifact: DownstreamArtifact,
        instruction_id: str,
        rationale: str | None = None,
    ) -> DeltaCompositionResult:
        """Build the delta for one authorized artifact, or explain why it cannot.

        Fails closed on every mismatch. The agent never widens scope to an artifact the
        change does not authorize, and never composes a delta for a requirement the
        artifact does not actually carry.
        """
        if artifact.artifact_id not in change.authorized_scope:
            return DeltaCompositionResult(
                DeltaStatus.NOT_IN_AUTHORIZED_SCOPE,
                reason=f"{artifact.artifact_id} is outside the change's authorized scope",
            )
        if artifact.operation_id != change.operation_id:
            return DeltaCompositionResult(
                DeltaStatus.ARTIFACT_MISMATCH,
                reason=(
                    f"artifact operation {artifact.operation_id!r} does not match the "
                    f"change operation {change.operation_id!r}"
                ),
            )
        if change.requirement_id not in artifact.requirements:
            return DeltaCompositionResult(
                DeltaStatus.REQUIREMENT_NOT_PRESENT,
                reason=(
                    f"{artifact.artifact_id} carries no {change.requirement_id!r} requirement"
                ),
            )
        if change.previous_value == change.current_value:
            return DeltaCompositionResult(
                DeltaStatus.NO_CHANGE,
                reason="the approved change records identical before and after values",
            )

        unchanged = {
            key: value
            for key, value in artifact.requirements.items()
            if key != change.requirement_id
        }
        instruction = DeltaInstruction(
            instruction_id=instruction_id,
            change_id=change.change_id,
            artifact_id=artifact.artifact_id,
            requirement_id=change.requirement_id,
            before_value=change.previous_value,
            after_value=change.current_value,
            concise_instruction=compose_instruction_text(
                change.requirement_id, change.previous_value, change.current_value
            ),
            unchanged_context=unchanged,
            source_procedure_id=change.source_procedure_id,
            source_version=change.source_version,
            previous_version=change.previous_version,
            source_evidence_ref=change.source_evidence_ref,
            rationale=rationale,
        )
        return DeltaCompositionResult(DeltaStatus.COMPOSED, instruction=instruction)

    def acknowledge(
        self,
        instruction: DeltaInstruction,
        *,
        operator_ref: str,
        identity_basis: str,
        occurred_at: datetime,
    ) -> FrontlineAcknowledgment:
        """Record that a worker confirmed reading the instruction.

        The clock is injected; nothing here reads wall time on its own. The returned
        record states explicitly that it establishes neither delivery nor verification.
        """
        return FrontlineAcknowledgment(
            instruction_id=instruction.instruction_id,
            change_id=instruction.change_id,
            acknowledged=True,
            acknowledged_at=occurred_at,
            operator_ref=operator_ref,
            identity_basis=identity_basis,
        )
