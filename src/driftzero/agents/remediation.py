"""T074 — the Remediation Agent.

The only logical identity permitted to invoke the Artifact Mutation Tool
(contracts/agents.md § Remediation Agent). Its job is narrow: take an
**already-qualified** remediation intent, obtain a scoped capability from the broker,
and execute the mutation through T073.

It decides nothing
------------------
Eligibility, impact-target selection, the nine autonomy conditions, workflow state,
PASS/FAIL, and Change Proof all belong to the deterministic Truth Engine and to callers
upstream. This agent does not choose the artifact, invent a ``requirement_id``, widen
scope, or alter the expected before/after values — it forwards exactly what it was
given, and the tool re-checks all of it independently.

No second write path
--------------------
Every mutation, ledger update, compare-and-set, idempotency decision, crash
reconciliation, and evidence construction stays inside T073 and the frozen M0
primitives. This module contains no write, no ledger, and no evidence assembly.

No model call
-------------
Executing an already-qualified mutation requires no semantic judgement, so this agent
performs no inference. Any future semantic assistance for remediation must stay separate
from authoritative mutation execution.

Enforcement honesty
-------------------
Authorization here is ``APPLICATION_LEVEL_ENFORCEMENT`` inside a single process sharing
one runtime service account. It is not GEAP Agent Identity and not per-agent Cloud IAM
isolation. See :mod:`driftzero.capabilities`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from driftzero.capabilities import (
    AgentIdentity,
    CapabilityDenied,
    MutationCapabilityBroker,
)
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.tools.artifact_mutation import (
    MutationOutcome,
    MutationRejection,
    MutationResult,
    MutationToolContext,
    apply_authorized_artifact_patch,
)

AGENT_IDENTITY = AgentIdentity.REMEDIATION
"""``driftzero-remediation`` — the identifier already defined in contracts/agents.md."""


class RemediationStatus(StrEnum):
    """Outcome of one remediation execution."""

    MUTATED = "MUTATED"
    NO_OP = "NO_OP"
    RECONCILED_MUTATION = "RECONCILED_MUTATION"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    MALFORMED_INTENT = "MALFORMED_INTENT"
    TOOL_REJECTED = "TOOL_REJECTED"


_TOOL_OUTCOME_TO_STATUS = {
    MutationOutcome.MUTATED: RemediationStatus.MUTATED,
    MutationOutcome.NO_OP: RemediationStatus.NO_OP,
    MutationOutcome.RECONCILED_MUTATION: RemediationStatus.RECONCILED_MUTATION,
    MutationOutcome.ALREADY_COMPLETED: RemediationStatus.ALREADY_COMPLETED,
}


@dataclass(frozen=True)
class RemediationIntent:
    """A remediation the deterministic boundary has already qualified.

    Every field arrives decided. The agent supplies none of them, which is why it cannot
    broaden scope or retarget the mutation even in principle.
    """

    action_id: str
    artifact_id: str
    requirement_id: str
    expected_before_value: str
    expected_before_hash: str
    expected_after_value: str
    source_procedure_id: str
    source_version: str
    change_id: str
    correlation_id: str

    def missing_fields(self) -> tuple[str, ...]:
        """Blank required fields, if any. Structural check only — no interpretation."""
        return tuple(
            name
            for name, value in vars(self).items()
            if not isinstance(value, str) or not value.strip()
        )


@dataclass(frozen=True)
class RemediationResult:
    """What the agent did, and the evidence the tool produced.

    ``evidence`` is the discriminated ``MutationEvidence | NoOpEvidence`` exactly as T073
    returned it — never repacked into a looser shape that would lose the discriminator.
    """

    status: RemediationStatus
    evidence: MutationEvidence | NoOpEvidence | None = None
    tool_result: MutationResult | None = None
    denial_reason: str | None = None
    identity: str = str(AGENT_IDENTITY)
    enforcement_model: str = "APPLICATION_LEVEL_ENFORCEMENT"
    platform_enforced_per_agent_identity: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status in {
            RemediationStatus.MUTATED,
            RemediationStatus.NO_OP,
            RemediationStatus.RECONCILED_MUTATION,
            RemediationStatus.ALREADY_COMPLETED,
        }

    @property
    def dispatched(self) -> bool:
        """True only when the tool actually sent a write."""
        return bool(self.tool_result and self.tool_result.dispatched)


@dataclass
class RemediationAgent:
    """Executes authorized remediations. Holds no authority of its own.

    ``identity`` defaults to the contract identity. It is settable so the negative
    security tests can drive the *real* broker with a different identity rather than
    asserting a string in a test — an unauthorized identity is denied by the broker, and
    without a broker-issued capability the tool refuses regardless.
    """

    broker: MutationCapabilityBroker
    identity: AgentIdentity = AGENT_IDENTITY

    def remediate(
        self, intent: RemediationIntent, context: MutationToolContext
    ) -> RemediationResult:
        """Obtain a scoped capability and execute the mutation through T073.

        ``context`` arrives without a capability; this agent is the only component that
        may attach one. Denial, malformed intent, and every tool rejection are returned
        as results — none of them escalates into a write.
        """
        missing = intent.missing_fields()
        if missing:
            return RemediationResult(
                status=RemediationStatus.MALFORMED_INTENT,
                denial_reason=f"blank required field(s): {list(missing)}",
            )

        try:
            capability = self.broker.issue(
                holder=self.identity,
                artifact_id=intent.artifact_id,
                change_id=intent.change_id,
                source_version=intent.source_version,
            )
        except CapabilityDenied as denied:
            return RemediationResult(
                status=RemediationStatus.CAPABILITY_DENIED,
                denial_reason=str(denied),
                identity=str(self.identity),
            )

        scoped = MutationToolContext(
            ledger=context.ledger,
            repository=context.repository,
            capability=capability,
            capability_verifier=context.capability_verifier,
            workflow_id=context.workflow_id,
            change=context.change,
            source_version_applicable=context.source_version_applicable,
            data_classification=context.data_classification,
            clock=context.clock,
        )

        result = apply_authorized_artifact_patch(
            action_id=intent.action_id,
            artifact_id=intent.artifact_id,
            requirement_id=intent.requirement_id,
            expected_before_value=intent.expected_before_value,
            expected_before_hash=intent.expected_before_hash,
            new_value=intent.expected_after_value,
            source_procedure_id=intent.source_procedure_id,
            source_version=intent.source_version,
            change_id=intent.change_id,
            correlation_id=intent.correlation_id,
            context=scoped,
        )

        if result.rejected:
            return RemediationResult(
                status=RemediationStatus.TOOL_REJECTED,
                tool_result=result,
                denial_reason=_rejection_detail(result),
                identity=str(self.identity),
            )

        # Evidence is passed through untouched: a reconciled mutation stays
        # MutationEvidence(reconciled=True) and is never reinterpreted as a NO_OP.
        return RemediationResult(
            status=_TOOL_OUTCOME_TO_STATUS[result.outcome],
            evidence=result.evidence,
            tool_result=result,
            identity=str(self.identity),
        )


def _rejection_detail(result: MutationResult) -> str:
    reason: MutationRejection | None = result.rejection
    detail = result.detail or ""
    return f"{reason}: {detail}".strip(": ")
