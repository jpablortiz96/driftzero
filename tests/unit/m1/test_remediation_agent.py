"""T074 — Remediation Agent, capability broker, and the negative identity tests.

Denials are proven through the **real** broker and the **real** tool boundary: an
unauthorized agent is refused a capability, and a forged capability is refused by the
tool. No test asserts authorization by comparing a string in isolation.

Fully offline: in-memory repository, injected clock, no model, no cloud.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents.remediation import (  # noqa: E402
    AGENT_IDENTITY,
    RemediationAgent,
    RemediationIntent,
    RemediationStatus,
)
from driftzero.capabilities import (  # noqa: E402
    ENFORCEMENT_MODEL,
    MUTATION_AUTHORIZED_IDENTITIES,
    PLATFORM_ENFORCED_PER_AGENT_IDENTITY,
    AgentIdentity,
    CapabilityDenied,
    MutationCapabilityBroker,
)
from driftzero.models.action import ActionStatus  # noqa: E402
from driftzero.models.remediation import MutationEvidence, NoOpEvidence  # noqa: E402
from driftzero.tools.artifact_mutation import (  # noqa: E402
    MutationCapability,
    MutationRejection,
    MutationToolContext,
    UncertainWriteError,
    artifact_content_hash,
)
from driftzero.truth_engine.actions import ActionLedger  # noqa: E402

from ._fakes import ARTIFACT_ID, CHANGE_ID, make_change, make_classification  # noqa: E402
from .test_artifact_mutation import (  # noqa: E402
    ACTION_ID,
    CORRELATION_ID,
    UNRELATED_PROSE,
    WORKFLOW_ID,
    InMemoryArtifactRepository,
    _Clock,
    make_hero_artifact,
)

UNAUTHORIZED_IDENTITIES = (
    AgentIdentity.CHANGE_INTELLIGENCE,
    AgentIdentity.ENABLEMENT,
    AgentIdentity.FIELD_VERIFICATION,
    AgentIdentity.ORCHESTRATOR,
)


def make_intent(**overrides: object) -> RemediationIntent:
    artifact = make_hero_artifact()
    defaults: dict[str, object] = {
        "action_id": ACTION_ID,
        "artifact_id": ARTIFACT_ID,
        "requirement_id": "label_position",
        "expected_before_value": "LEFT",
        "expected_before_hash": artifact_content_hash(artifact),
        "expected_after_value": "TOP_RIGHT",
        "source_procedure_id": "PROC-77",
        "source_version": "v3",
        "change_id": CHANGE_ID,
        "correlation_id": CORRELATION_ID,
    }
    defaults.update(overrides)
    return RemediationIntent(**defaults)  # type: ignore[arg-type]


def make_unscoped_context(
    broker: MutationCapabilityBroker,
    *,
    artifact: object | None = None,
    repository: object | None = None,
    ledger: ActionLedger | None = None,
) -> MutationToolContext:
    """A context with **no** capability — the agent is the only thing that attaches one."""
    art = make_hero_artifact() if artifact is None else artifact
    repo = (
        InMemoryArtifactRepository({art.artifact_id: art})  # type: ignore[union-attr]
        if repository is None
        else repository
    )
    return MutationToolContext(
        ledger=ledger or ActionLedger(),
        repository=repo,  # type: ignore[arg-type]
        capability=None,
        capability_verifier=broker.verify,
        workflow_id=WORKFLOW_ID,
        change=make_change(),
        source_version_applicable=True,
        data_classification=make_classification(),
        clock=_Clock(),
    )


# ============================ logical identity and honesty ============================


def test_the_agent_uses_the_contract_identity() -> None:
    assert str(AGENT_IDENTITY) == "driftzero-remediation"
    assert AGENT_IDENTITY is AgentIdentity.REMEDIATION


def test_remediation_is_the_only_mutation_authorized_identity() -> None:
    assert MUTATION_AUTHORIZED_IDENTITIES == frozenset({AgentIdentity.REMEDIATION})


def test_enforcement_is_declared_application_level_not_platform() -> None:
    """Hackathon evidence honesty: no false claim of per-agent IAM isolation."""
    assert ENFORCEMENT_MODEL == "APPLICATION_LEVEL_ENFORCEMENT"
    assert PLATFORM_ENFORCED_PER_AGENT_IDENTITY is False


def test_the_broker_discloses_its_real_enforcement_model() -> None:
    disclosure = MutationCapabilityBroker().enforcement_disclosure()
    assert disclosure["enforcement_model"] == "APPLICATION_LEVEL_ENFORCEMENT"
    assert disclosure["platform_enforced_per_agent_identity"] is False
    assert disclosure["per_agent_iam_principals"] is False
    assert disclosure["shared_runtime_service_account"] == "driftzero-run-sa"


def test_the_result_carries_the_enforcement_disclosure() -> None:
    broker = MutationCapabilityBroker()
    agent = RemediationAgent(broker=broker)
    result = agent.remediate(make_intent(), make_unscoped_context(broker))
    assert result.enforcement_model == "APPLICATION_LEVEL_ENFORCEMENT"
    assert result.platform_enforced_per_agent_identity is False
    assert result.identity == "driftzero-remediation"


# ============================ negative identity tests =================================


@pytest.mark.parametrize("identity", UNAUTHORIZED_IDENTITIES, ids=lambda i: str(i))
def test_unauthorized_identities_are_denied_a_capability(identity: AgentIdentity) -> None:
    """The broker itself refuses — not a check inside the test."""
    with pytest.raises(CapabilityDenied):
        MutationCapabilityBroker().issue(
            holder=identity, artifact_id=ARTIFACT_ID, change_id=CHANGE_ID, source_version="v3"
        )


@pytest.mark.parametrize("identity", UNAUTHORIZED_IDENTITIES, ids=lambda i: str(i))
def test_an_unauthorized_agent_cannot_mutate(identity: AgentIdentity) -> None:
    """Driving the real agent under a denied identity produces zero writes."""
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker, identity=identity)
    result = agent.remediate(make_intent(), context)

    assert result.status is RemediationStatus.CAPABILITY_DENIED
    assert result.evidence is None
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_an_unknown_identity_is_denied() -> None:
    with pytest.raises(CapabilityDenied, match="not a known logical agent identity"):
        MutationCapabilityBroker().issue(
            holder="driftzero-not-a-real-agent",
            artifact_id=ARTIFACT_ID,
            change_id=CHANGE_ID,
            source_version="v3",
        )


def test_the_orchestrator_is_denied_write_authority() -> None:
    """A master layer holding write authority would defeat the boundary."""
    assert AgentIdentity.ORCHESTRATOR not in MUTATION_AUTHORIZED_IDENTITIES


# ============================ capabilities are not forgeable ==========================


def test_a_hand_constructed_capability_is_rejected_by_the_tool() -> None:
    """Passing a privileged-looking holder string confers nothing."""
    broker = MutationCapabilityBroker()
    forged = MutationCapability(
        capability_id="cap-forged",
        holder=str(AgentIdentity.REMEDIATION),
        tool="ARTIFACT_MUTATION",
        authorized_artifact_ids=frozenset({ARTIFACT_ID}),
        change_id=CHANGE_ID,
        source_version="v3",
        grant_token="i-made-this-up",
    )
    assert broker.verify(forged) is False

    context = make_unscoped_context(broker)
    scoped = MutationToolContext(
        ledger=context.ledger,
        repository=context.repository,
        capability=forged,
        capability_verifier=broker.verify,
        workflow_id=context.workflow_id,
        change=context.change,
        source_version_applicable=True,
        data_classification=context.data_classification,
        clock=context.clock,
    )
    from driftzero.tools.artifact_mutation import apply_authorized_artifact_patch

    intent = make_intent()
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
    assert result.rejection is MutationRejection.CAPABILITY_NOT_ISSUED
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_capability_from_another_broker_does_not_verify() -> None:
    """Per-instance secrets make capabilities non-transferable between contexts."""
    issuer = MutationCapabilityBroker()
    other = MutationCapabilityBroker()
    capability = issuer.issue(
        holder=AgentIdentity.REMEDIATION,
        artifact_id=ARTIFACT_ID,
        change_id=CHANGE_ID,
        source_version="v3",
    )
    assert issuer.verify(capability) is True
    assert other.verify(capability) is False


def test_a_tampered_capability_field_breaks_verification() -> None:
    broker = MutationCapabilityBroker()
    capability = broker.issue(
        holder=AgentIdentity.REMEDIATION,
        artifact_id=ARTIFACT_ID,
        change_id=CHANGE_ID,
        source_version="v3",
    )
    from dataclasses import replace

    assert broker.verify(replace(capability, change_id="CHG-OTHER")) is False
    assert broker.verify(replace(capability, source_version="v9")) is False
    assert broker.verify(replace(capability, holder="driftzero-enablement")) is False


def test_a_revoked_capability_stops_verifying() -> None:
    broker = MutationCapabilityBroker()
    capability = broker.issue(
        holder=AgentIdentity.REMEDIATION,
        artifact_id=ARTIFACT_ID,
        change_id=CHANGE_ID,
        source_version="v3",
    )
    assert broker.verify(capability) is True
    broker.revoke(capability.capability_id)
    assert broker.verify(capability) is False


def test_a_capability_for_artifact_a_cannot_mutate_artifact_b() -> None:
    broker = MutationCapabilityBroker()
    other = make_hero_artifact(artifact_id="WI-220")
    repo = InMemoryArtifactRepository({ARTIFACT_ID: make_hero_artifact(), "WI-220": other})
    context = make_unscoped_context(broker, repository=repo)

    # The agent mints for whatever artifact the intent names, so scope violation is
    # driven here by an intent/capability split the tool must catch.
    capability = broker.issue(
        holder=AgentIdentity.REMEDIATION,
        artifact_id=ARTIFACT_ID,
        change_id=CHANGE_ID,
        source_version="v3",
    )
    scoped = MutationToolContext(
        ledger=context.ledger,
        repository=repo,
        capability=capability,
        capability_verifier=broker.verify,
        workflow_id=WORKFLOW_ID,
        change=make_change(),
        source_version_applicable=True,
        data_classification=make_classification(),
        clock=_Clock(),
    )
    from driftzero.tools.artifact_mutation import apply_authorized_artifact_patch

    intent = make_intent(artifact_id="WI-220")
    result = apply_authorized_artifact_patch(
        action_id=intent.action_id,
        artifact_id="WI-220",
        requirement_id=intent.requirement_id,
        expected_before_value="LEFT",
        expected_before_hash=artifact_content_hash(other),
        new_value="TOP_RIGHT",
        source_procedure_id=intent.source_procedure_id,
        source_version="v3",
        change_id=CHANGE_ID,
        correlation_id=CORRELATION_ID,
        context=scoped,
    )
    assert result.rejection is MutationRejection.CAPABILITY_SCOPE_VIOLATION
    assert repo.dispatch_count == 0


def test_a_capability_for_change_a_cannot_mutate_change_b() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)
    result = agent.remediate(make_intent(change_id="CHG-OTHER"), context)

    assert result.status is RemediationStatus.TOOL_REJECTED
    assert result.tool_result.rejection is MutationRejection.CAPABILITY_CONTEXT_MISMATCH
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_wrong_source_version_is_rejected() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)
    result = agent.remediate(make_intent(source_version="v9"), context)

    assert result.status is RemediationStatus.TOOL_REJECTED
    assert result.tool_result.rejection is MutationRejection.CAPABILITY_CONTEXT_MISMATCH
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


# ============================ hero path ===============================================


def test_hero_path_left_to_top_right_through_agent_and_broker() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)

    result = agent.remediate(make_intent(), context)

    assert result.status is RemediationStatus.MUTATED
    assert isinstance(result.evidence, MutationEvidence)
    assert result.evidence.before_value == "LEFT"
    assert result.evidence.after_value == "TOP_RIGHT"
    assert result.evidence.reconciled is False
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]

    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    assert after.requirements["label_position"] == "TOP_RIGHT"
    assert after.requirements["instructions"] == UNRELATED_PROSE
    assert "LEFT" in after.requirements["instructions"]
    assert after.requirements["packing_mode"] == "STANDARD"


def test_replay_through_the_agent_cannot_create_a_second_mutation() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)

    first = agent.remediate(make_intent(), context)
    assert first.status is RemediationStatus.MUTATED
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]

    second = agent.remediate(make_intent(), context)
    assert second.status is RemediationStatus.ALREADY_COMPLETED
    assert second.dispatched is False
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]


# ============================ evidence propagation ====================================


def test_no_op_is_propagated_unchanged() -> None:
    compliant = make_hero_artifact(
        current_value="TOP_RIGHT",
        requirements={
            "label_position": "TOP_RIGHT",
            "instructions": UNRELATED_PROSE,
            "packing_mode": "STANDARD",
        },
    )
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker, artifact=compliant)
    agent = RemediationAgent(broker=broker)

    result = agent.remediate(
        make_intent(expected_before_hash=artifact_content_hash(compliant)), context
    )
    assert result.status is RemediationStatus.NO_OP
    assert isinstance(result.evidence, NoOpEvidence)
    assert result.evidence.remediation_type == "NO_OP"
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_reconciled_mutation_is_propagated_unchanged_and_never_becomes_no_op() -> None:
    class UncertainRepo(InMemoryArtifactRepository):
        fail_next = True

        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            result = super().apply_requirement(
                artifact_id, requirement_id, expected_before, new_value
            )
            if self.fail_next:
                self.fail_next = False
                raise UncertainWriteError("crash after commit")
            return result

    broker = MutationCapabilityBroker()
    repo = UncertainRepo({ARTIFACT_ID: make_hero_artifact()})
    context = make_unscoped_context(broker, repository=repo)
    agent = RemediationAgent(broker=broker)

    first = agent.remediate(make_intent(), context)
    assert first.status is RemediationStatus.TOOL_REJECTED
    assert first.tool_result.rejection is MutationRejection.POST_DISPATCH_UNCERTAIN
    assert repo.dispatch_count == 1

    recovery = agent.remediate(make_intent(), context)
    assert recovery.status is RemediationStatus.RECONCILED_MUTATION
    assert isinstance(recovery.evidence, MutationEvidence)
    assert recovery.evidence.reconciled is True
    assert not isinstance(recovery.evidence, NoOpEvidence)
    assert repo.dispatch_count == 1, "the agent must not redispatch"


def test_evidence_keeps_its_discriminator_and_is_not_repacked() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    result = RemediationAgent(broker=broker).remediate(make_intent(), context)
    assert result.evidence is result.tool_result.evidence
    assert result.evidence.remediation_type == "MUTATION"
    assert not isinstance(result.evidence, dict)


# ============================ failure propagation =====================================


def test_post_dispatch_uncertainty_is_not_retried_by_the_agent() -> None:
    class UncertainRepo(InMemoryArtifactRepository):
        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            super().apply_requirement(artifact_id, requirement_id, expected_before, new_value)
            raise UncertainWriteError("response lost")

    broker = MutationCapabilityBroker()
    repo = UncertainRepo({ARTIFACT_ID: make_hero_artifact()})
    context = make_unscoped_context(broker, repository=repo)

    result = RemediationAgent(broker=broker).remediate(make_intent(), context)
    assert result.status is RemediationStatus.TOOL_REJECTED
    assert result.tool_result.action_status is ActionStatus.FAILED_OR_UNCERTAIN
    assert repo.dispatch_count == 1


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("expected_before_value", MutationRejection.BEFORE_STATE_MISMATCH),
        ("expected_before_hash", MutationRejection.BEFORE_HASH_MISMATCH),
    ],
)
def test_tool_failures_propagate_without_authority_escalation(
    field: str, expected: MutationRejection
) -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    bogus = "DIAGONAL" if field == "expected_before_value" else "0" * 64
    result = RemediationAgent(broker=broker).remediate(make_intent(**{field: bogus}), context)

    assert result.status is RemediationStatus.TOOL_REJECTED
    assert result.tool_result.rejection is expected
    assert result.evidence is None
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_missing_artifact_propagates_as_a_tool_rejection() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker, repository=InMemoryArtifactRepository({}))
    result = RemediationAgent(broker=broker).remediate(make_intent(), context)
    assert result.status is RemediationStatus.TOOL_REJECTED
    assert result.tool_result.rejection is MutationRejection.ARTIFACT_NOT_FOUND


@pytest.mark.parametrize(
    "field", ["action_id", "artifact_id", "requirement_id", "expected_after_value"]
)
def test_malformed_intent_is_refused_before_a_capability_is_issued(field: str) -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    result = RemediationAgent(broker=broker).remediate(make_intent(**{field: "  "}), context)

    assert result.status is RemediationStatus.MALFORMED_INTENT
    assert broker.issued_count == 0, "no capability is minted for a malformed intent"
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


# ============================ the agent holds no authority ============================


def test_the_agent_owns_no_state_proof_or_autonomy_logic() -> None:
    import ast

    source = (REPO_ROOT / "src" / "driftzero" / "agents" / "remediation.py").read_text(
        encoding="utf-8"
    )
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "driftzero.truth_engine.state_machine",
        "driftzero.truth_engine.proof_generator",
        "driftzero.truth_engine.autonomy_gate",
        "driftzero.truth_engine.impact",
        "driftzero.truth_engine.actions",
    ):
        assert forbidden not in imported, f"agent must not import {forbidden}"


def test_the_agent_implements_no_second_write_path() -> None:
    """All mutation stays in T073: no repository write call lives in the agent."""
    source = (REPO_ROOT / "src" / "driftzero" / "agents" / "remediation.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
    )
    for forbidden in ("apply_requirement", "ActionLedger(", "mark_completed", "mark_attempted"):
        assert forbidden not in code, f"agent must not perform {forbidden}"


def test_the_agent_makes_no_model_call() -> None:
    source = (REPO_ROOT / "src" / "driftzero" / "agents" / "remediation.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("generate_structured", "SemanticModelClient", "get_model_client"):
        assert forbidden not in source


def test_the_result_carries_no_verdict_or_workflow_state() -> None:
    from driftzero.agents.remediation import RemediationResult

    fields = set(RemediationResult.__dataclass_fields__)
    for forbidden in ("workflow_state", "verdict", "passed", "proof", "next_state", "authorized"):
        assert forbidden not in fields


def test_the_intent_is_supplied_not_derived() -> None:
    """Every decided field arrives on the intent; the agent adds none of them."""
    fields = set(RemediationIntent.__dataclass_fields__)
    assert fields == {
        "action_id",
        "artifact_id",
        "requirement_id",
        "expected_before_value",
        "expected_before_hash",
        "expected_after_value",
        "source_procedure_id",
        "source_version",
        "change_id",
        "correlation_id",
    }


def test_the_agent_forwards_intent_values_verbatim() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    intent = make_intent()
    result = RemediationAgent(broker=broker).remediate(intent, context)

    assert result.evidence.before_value == intent.expected_before_value
    assert result.evidence.after_value == intent.expected_after_value
    assert result.evidence.artifact_id == intent.artifact_id
    assert result.evidence.action_id == intent.action_id
