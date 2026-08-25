"""T075 — single authorization authority, tool binding, and denial evidence.

Every allow/deny is driven through the **real** policy, the **real** broker, and the
**real** T073 tool boundary. No test asserts authorization by comparing a string.

Fully offline: in-memory repository, injected clock, no model, no cloud.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents.remediation import (  # noqa: E402
    RemediationAgent,
    RemediationStatus,
)
from driftzero.capabilities import (  # noqa: E402
    AUTHORIZATION_POLICY,
    ENFORCEMENT_MODEL,
    MUTATION_AUTHORIZED_IDENTITIES,
    PLATFORM_ENFORCED_PER_AGENT_IDENTITY,
    SHARED_RUNTIME_SERVICE_ACCOUNT,
    AgentIdentity,
    CapabilityDenied,
    DenialEvidence,
    DenialReason,
    MutationCapabilityBroker,
    ToolCapability,
    authorized_identities_for,
    is_authorized,
)
from driftzero.models.remediation import MutationEvidence  # noqa: E402
from driftzero.tools.artifact_mutation import (  # noqa: E402
    TOOL_CAPABILITY,
    MutationCapability,
    MutationOutcome,
    MutationRejection,
    MutationToolContext,
    apply_authorized_artifact_patch,
    artifact_content_hash,
)

from ._fakes import ARTIFACT_ID, CHANGE_ID  # noqa: E402
from .test_artifact_mutation import (  # noqa: E402
    UNRELATED_PROSE,
    make_hero_artifact,
)
from .test_remediation_agent import (  # noqa: E402
    UNAUTHORIZED_IDENTITIES,
    make_intent,
    make_unscoped_context,
)

CAPABILITIES_SRC = REPO_ROOT / "src" / "driftzero" / "capabilities.py"


def issue(broker: MutationCapabilityBroker, **overrides: str) -> MutationCapability:
    kwargs: dict[str, object] = {
        "holder": AgentIdentity.REMEDIATION,
        "artifact_id": ARTIFACT_ID,
        "change_id": CHANGE_ID,
        "source_version": "v3",
    }
    kwargs.update(overrides)
    return broker.issue(**kwargs)  # type: ignore[arg-type]


# ============================ single policy authority =================================


def test_the_policy_allows_only_remediation_to_mutate() -> None:
    assert AUTHORIZATION_POLICY == frozenset(
        {(AgentIdentity.REMEDIATION, ToolCapability.ARTIFACT_MUTATION)}
    )


def test_policy_lookup_allows_the_authorized_pair() -> None:
    assert is_authorized(AgentIdentity.REMEDIATION, ToolCapability.ARTIFACT_MUTATION) is True


@pytest.mark.parametrize("identity", UNAUTHORIZED_IDENTITIES, ids=lambda i: str(i))
def test_policy_denies_every_other_identity(identity: AgentIdentity) -> None:
    assert is_authorized(identity, ToolCapability.ARTIFACT_MUTATION) is False


def test_policy_fails_closed_on_unknown_identity_and_unknown_tool() -> None:
    """No wildcard, no default-allow, no implicit inheritance."""
    assert is_authorized("driftzero-console", ToolCapability.ARTIFACT_MUTATION) is False
    assert is_authorized("driftzero-admin", ToolCapability.ARTIFACT_MUTATION) is False
    assert is_authorized(AgentIdentity.REMEDIATION, "FRONTLINE_DELIVERY") is False
    assert is_authorized(AgentIdentity.REMEDIATION, "ANYTHING_ELSE") is False
    assert is_authorized("", "") is False


def test_mutation_authorized_identities_is_derived_not_declared() -> None:
    """There must be exactly one policy source of truth.

    The compatibility view is computed from the policy, so it cannot drift from it.
    """
    assert MUTATION_AUTHORIZED_IDENTITIES == authorized_identities_for(
        ToolCapability.ARTIFACT_MUTATION
    )
    source = CAPABILITIES_SRC.read_text(encoding="utf-8")
    assignment = next(
        line for line in source.splitlines()
        if line.startswith("MUTATION_AUTHORIZED_IDENTITIES")
    )
    assert "authorized_identities_for" in assignment, "must be derived, not a literal set"
    assert "frozenset({" not in assignment


def test_only_one_policy_table_exists_in_the_codebase() -> None:
    """Structural: no second module declares an agent->tool allow table."""
    src = REPO_ROOT / "src" / "driftzero"
    declarations = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                name = getattr(target, "id", "")
                if name in {"AUTHORIZATION_POLICY", "MUTATION_AUTHORIZED_IDENTITIES"}:
                    declarations.append((path.name, name))
    assert sorted(declarations) == [
        ("capabilities.py", "AUTHORIZATION_POLICY"),
        ("capabilities.py", "MUTATION_AUTHORIZED_IDENTITIES"),
    ]


def test_the_broker_delegates_policy_rather_than_owning_it() -> None:
    source = CAPABILITIES_SRC.read_text(encoding="utf-8")
    broker_body = source[source.index("class MutationCapabilityBroker"):]
    assert "is_authorized(" in broker_body, "the adapter must consult the shared policy"


# ============================ tool capability model ===================================


def test_the_tool_capability_vocabulary_is_minimal() -> None:
    """Only the tool T075 actually needs. Future tools are added here, not elsewhere."""
    assert [t.value for t in ToolCapability] == ["ARTIFACT_MUTATION"]


def test_future_tools_are_not_prematurely_authorized() -> None:
    for speculative in ("FRONTLINE_DELIVERY", "FIELD_OBSERVATION"):
        assert speculative not in {t.value for t in ToolCapability}
        for identity in AgentIdentity:
            assert is_authorized(identity, speculative) is False


def test_the_tool_module_and_the_policy_agree_on_the_capability_name() -> None:
    """The tool holds the name as a string to avoid a capabilities->tools cycle."""
    assert TOOL_CAPABILITY == ToolCapability.ARTIFACT_MUTATION.value


# ============================ capability tool binding =================================


def test_an_issued_capability_is_bound_to_its_tool() -> None:
    broker = MutationCapabilityBroker()
    capability = issue(broker)
    assert capability.tool == ToolCapability.ARTIFACT_MUTATION.value
    assert broker.verify(capability) is True
    assert broker.verify_for_tool(capability, ToolCapability.ARTIFACT_MUTATION) is True


def test_the_tool_participates_in_the_integrity_payload() -> None:
    """Tampering with the tool field breaks the HMAC, not merely a field comparison."""
    broker = MutationCapabilityBroker()
    capability = issue(broker)
    retooled = replace(capability, tool="FRONTLINE_DELIVERY")
    assert broker.verify(retooled) is False


def test_a_capability_cannot_cross_a_tool_boundary() -> None:
    broker = MutationCapabilityBroker()
    capability = issue(broker)
    assert broker.verify_for_tool(capability, "FRONTLINE_DELIVERY") is False
    assert broker.verify_for_tool(capability, "FIELD_OBSERVATION") is False


def test_the_real_tool_boundary_rejects_a_wrong_tool_capability() -> None:
    """Enforced in T073 itself, not only in the broker."""
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    wrong = replace(issue(broker), tool="FRONTLINE_DELIVERY")
    result = _invoke(context, broker, wrong)

    assert result.rejection is MutationRejection.CAPABILITY_WRONG_TOOL
    assert result.dispatched is False
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def _invoke(
    context: MutationToolContext, broker: MutationCapabilityBroker, capability: object
):  # type: ignore[no-untyped-def]
    intent = make_intent()
    scoped = MutationToolContext(
        ledger=context.ledger,
        repository=context.repository,
        capability=capability,  # type: ignore[arg-type]
        capability_verifier=broker.verify,
        workflow_id=context.workflow_id,
        change=context.change,
        source_version_applicable=True,
        data_classification=context.data_classification,
        clock=context.clock,
    )
    return apply_authorized_artifact_patch(
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


# ============================ denial evidence =========================================


def test_one_denial_record_per_refused_request() -> None:
    broker = MutationCapabilityBroker()
    for identity in UNAUTHORIZED_IDENTITIES:
        with pytest.raises(CapabilityDenied):
            issue(broker, holder=identity)  # type: ignore[arg-type]
    assert len(broker.denials) == len(UNAUTHORIZED_IDENTITIES)
    assert broker.denied_count == len(UNAUTHORIZED_IDENTITIES)
    assert broker.issued_count == 0


def test_the_denial_record_carries_deterministic_fields() -> None:
    broker = MutationCapabilityBroker(clock=lambda: datetime(2026, 8, 25, tzinfo=UTC))
    with pytest.raises(CapabilityDenied) as raised:
        issue(broker, holder=AgentIdentity.ENABLEMENT)  # type: ignore[arg-type]

    record = raised.value.record
    assert isinstance(record, DenialEvidence)
    assert record.requested_by == "driftzero-enablement"
    assert record.requested_tool == "ARTIFACT_MUTATION"
    assert record.decision == "DENIED"
    assert record.reason_code is DenialReason.IDENTITY_NOT_AUTHORIZED_FOR_TOOL
    assert record.artifact_id == ARTIFACT_ID
    assert record.change_id == CHANGE_ID
    assert record.source_version == "v3"
    assert record.policy_basis
    assert record.occurred_at == datetime(2026, 8, 25, tzinfo=UTC)


def test_the_denial_record_declares_application_level_enforcement() -> None:
    broker = MutationCapabilityBroker()
    with pytest.raises(CapabilityDenied) as raised:
        issue(broker, holder=AgentIdentity.FIELD_VERIFICATION)  # type: ignore[arg-type]
    record = raised.value.record

    assert record.enforcement_model == ENFORCEMENT_MODEL == "APPLICATION_LEVEL_ENFORCEMENT"
    assert record.platform_enforced_per_agent_identity is False
    assert PLATFORM_ENFORCED_PER_AGENT_IDENTITY is False
    assert record.shared_runtime_service_account == SHARED_RUNTIME_SERVICE_ACCOUNT


def test_the_denial_record_proves_non_effect() -> None:
    broker = MutationCapabilityBroker()
    with pytest.raises(CapabilityDenied) as raised:
        issue(broker, holder=AgentIdentity.ORCHESTRATOR)  # type: ignore[arg-type]
    record = raised.value.record
    assert record.dispatch_count_delta == 0
    assert record.no_state_transition is True


def test_denial_evidence_has_no_workflow_or_verdict_authority() -> None:
    """Structural: a denial records that nothing happened; it cannot make anything happen."""
    fields = set(DenialEvidence.__dataclass_fields__)
    for forbidden in (
        "workflow_state", "next_state", "verdict", "passed", "failed",
        "proof", "change_proof", "proof_complete", "authorized", "transition",
    ):
        assert forbidden not in fields


def test_denial_evidence_never_claims_platform_enforcement() -> None:
    disclosure = MutationCapabilityBroker().enforcement_disclosure()
    for claim in (
        "cloud_iam_enforcement",
        "agent_identity_enforcement",
        "agent_gateway_enforcement",
        "model_armor_enforcement",
    ):
        assert disclosure[claim] is False


def test_denial_records_are_referenceable_as_rejected_results() -> None:
    """Reuses the existing EvidenceManifest.rejected_result_refs shape."""
    broker = MutationCapabilityBroker()
    with pytest.raises(CapabilityDenied):
        issue(broker, holder=AgentIdentity.ENABLEMENT)  # type: ignore[arg-type]
    refs = broker.denial_evidence_refs()
    assert len(refs) == 1
    assert refs[0].startswith("authorization-denial:")
    assert "driftzero-enablement" in refs[0]
    assert "ARTIFACT_MUTATION" in refs[0]


def test_the_agent_surfaces_the_denial_record() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker, identity=AgentIdentity.ENABLEMENT)
    result = agent.remediate(make_intent(), context)

    assert result.status is RemediationStatus.CAPABILITY_DENIED
    assert result.denial_evidence is broker.denials[0]
    assert result.evidence is None


# ============================ hero security case ======================================


def test_hero_security_case_enablement_denied_with_non_effect_evidence() -> None:
    """The future Hero Console security panel, proven end to end.

    Frontline Enablement attempts label_position LEFT -> TOP_RIGHT and is denied, with
    the artifact provably unchanged and nothing dispatched.
    """
    broker = MutationCapabilityBroker(clock=lambda: datetime(2026, 8, 25, tzinfo=UTC))
    artifact = make_hero_artifact()
    context = make_unscoped_context(broker, artifact=artifact)
    before_hash = artifact_content_hash(context.repository.read(ARTIFACT_ID))  # type: ignore[union-attr]

    agent = RemediationAgent(broker=broker, identity=AgentIdentity.ENABLEMENT)
    result = agent.remediate(make_intent(), context)

    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    after_hash = artifact_content_hash(after)

    assert result.status is RemediationStatus.CAPABILITY_DENIED
    record = result.denial_evidence
    assert record.requested_by == "driftzero-enablement"
    assert record.requested_tool == "ARTIFACT_MUTATION"
    assert record.decision == "DENIED"
    assert record.enforcement_model == "APPLICATION_LEVEL_ENFORCEMENT"
    assert record.platform_enforced_per_agent_identity is False
    assert record.dispatch_count_delta == 0
    assert record.no_state_transition is True

    # Measured non-effect, not merely asserted.
    assert before_hash == after_hash
    assert after.requirements["label_position"] == "LEFT"
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]
    assert len(context.ledger.all_records()) == 0


def test_the_denial_path_reads_no_artifact_before_refusing() -> None:
    """Authorization is checked as early as practical: refusal precedes repository work."""
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    reads_before = context.repository.read_count  # type: ignore[union-attr]
    agent = RemediationAgent(broker=broker, identity=AgentIdentity.ENABLEMENT)
    agent.remediate(make_intent(), context)
    assert context.repository.read_count == reads_before  # type: ignore[union-attr]


# ============================ authorized hero regression ==============================


def test_the_authorized_hero_path_still_mutates_exactly_once() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)

    result = agent.remediate(make_intent(), context)
    assert result.status is RemediationStatus.MUTATED
    assert isinstance(result.evidence, MutationEvidence)
    assert result.evidence.before_value == "LEFT"
    assert result.evidence.after_value == "TOP_RIGHT"
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]

    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    assert after.requirements["label_position"] == "TOP_RIGHT"
    assert after.requirements["instructions"] == UNRELATED_PROSE
    assert "LEFT" in after.requirements["instructions"]


def test_replay_after_the_authorized_mutation_does_not_redispatch() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)

    agent.remediate(make_intent(), context)
    second = agent.remediate(make_intent(), context)
    assert second.status is RemediationStatus.ALREADY_COMPLETED
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]


def test_same_action_id_different_payload_still_fails_closed() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    agent = RemediationAgent(broker=broker)

    agent.remediate(make_intent(), context)
    conflicting = agent.remediate(make_intent(expected_after_value="DIAGONAL"), context)
    assert conflicting.status is RemediationStatus.TOOL_REJECTED
    assert conflicting.tool_result.rejection is MutationRejection.ACTION_PAYLOAD_CONFLICT
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]


def test_the_authorized_outcome_is_unchanged_by_t075() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    result = RemediationAgent(broker=broker).remediate(make_intent(), context)
    assert result.tool_result.outcome is MutationOutcome.MUTATED
    assert result.evidence.reconciled is False
    assert result.evidence.remediation_type == "MUTATION"


# ============================ trust boundary matrix ===================================


def test_a_capability_from_another_broker_is_refused_at_the_tool() -> None:
    issuer, other = MutationCapabilityBroker(), MutationCapabilityBroker()
    context = make_unscoped_context(other)
    result = _invoke(context, other, issue(issuer))
    assert result.rejection is MutationRejection.CAPABILITY_NOT_ISSUED
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_revoked_capability_is_refused_at_the_tool() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    capability = issue(broker)
    broker.revoke(capability.capability_id)
    result = _invoke(context, broker, capability)
    assert result.rejection is MutationRejection.CAPABILITY_NOT_ISSUED
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_forged_capability_is_refused_at_the_tool() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    forged = MutationCapability(
        capability_id="cap-forged",
        holder=str(AgentIdentity.REMEDIATION),
        tool=TOOL_CAPABILITY,
        authorized_artifact_ids=frozenset({ARTIFACT_ID}),
        change_id=CHANGE_ID,
        source_version="v3",
        grant_token="i-made-this-up",
    )
    result = _invoke(context, broker, forged)
    assert result.rejection is MutationRejection.CAPABILITY_NOT_ISSUED
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_authorization_by_holder_string_alone_remains_impossible() -> None:
    """A privileged-looking holder confers nothing without broker integrity."""
    broker = MutationCapabilityBroker()
    for capability_id in ("cap-0001-deadbeef", "cap-forged", ""):
        forged = MutationCapability(
            capability_id=capability_id,
            holder="driftzero-remediation",
            tool=TOOL_CAPABILITY,
            authorized_artifact_ids=frozenset({ARTIFACT_ID}),
            change_id=CHANGE_ID,
            source_version="v3",
            grant_token="x" * 64,
        )
        assert broker.verify(forged) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("change_id", "CHG-OTHER"), ("source_version", "v9"), ("holder", "driftzero-enablement")],
)
def test_tampering_with_a_bound_field_breaks_verification(field: str, value: str) -> None:
    broker = MutationCapabilityBroker()
    assert broker.verify(replace(issue(broker), **{field: value})) is False


def test_a_capability_for_artifact_a_cannot_mutate_artifact_b() -> None:
    broker = MutationCapabilityBroker()
    context = make_unscoped_context(broker)
    result = _invoke(context, broker, issue(broker, artifact_id="WI-999"))
    assert result.rejection is MutationRejection.CAPABILITY_SCOPE_VIOLATION
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_verification_rechecks_policy_not_just_the_signature() -> None:
    """A holder that lost authorization must stop verifying, not coast on an old token."""
    broker = MutationCapabilityBroker()
    capability = issue(broker)
    assert broker.verify(capability) is True
    grant = broker._grants[capability.capability_id]  # noqa: SLF001
    broker._grants[capability.capability_id] = replace(  # noqa: SLF001
        grant, holder=str(AgentIdentity.ENABLEMENT)
    )
    tampered = replace(capability, holder=str(AgentIdentity.ENABLEMENT))
    assert broker.verify(tampered) is False


# ============================ M0 dependency boundary ==================================


def test_truth_engine_imports_no_m1_module() -> None:
    """M1 -> M0 only. The reverse would invert the dependency direction."""
    forbidden = {"driftzero.tools", "driftzero.agents", "driftzero.capabilities",
                 "driftzero.orchestration", "driftzero.config", "driftzero.retry"}
    engine = REPO_ROOT / "src" / "driftzero" / "truth_engine"
    for path in sorted(engine.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = getattr(node, "module", None)
            if isinstance(node, ast.ImportFrom) and module:
                assert not any(module.startswith(f) for f in forbidden), f"{path.name}: {module}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(alias.name.startswith(f) for f in forbidden), path.name


def test_no_authz_broker_module_was_created_under_truth_engine() -> None:
    assert not (REPO_ROOT / "src" / "driftzero" / "truth_engine" / "authz_broker.py").exists()


def test_capability_verifier_remains_required() -> None:
    """An optional authorization check is one forgotten argument from no check."""
    field = MutationToolContext.__dataclass_fields__["capability_verifier"]
    import dataclasses

    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING  # type: ignore[misc]


def test_the_authorization_layer_introduces_no_cloud_or_model_dependency() -> None:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(CAPABILITIES_SRC.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "hmac", "secrets", "collections", "dataclasses",
                     "datetime", "enum", "driftzero"}


def test_the_hero_console_cannot_be_an_authorized_identity() -> None:
    """A future UI requests remediation; it never holds the capability."""
    for name in ("console", "frontend", "operator", "admin", "ui"):
        assert not any(name in str(i) for i in MUTATION_AUTHORIZED_IDENTITIES)
        assert is_authorized(f"driftzero-{name}", ToolCapability.ARTIFACT_MUTATION) is False


def test_the_denial_record_is_stable_enough_for_a_ui_panel() -> None:
    """The security-demo panel needs identity, tool, decision, and proof of non-effect."""
    broker = MutationCapabilityBroker()
    with pytest.raises(CapabilityDenied) as raised:
        issue(broker, holder=AgentIdentity.ENABLEMENT)  # type: ignore[arg-type]
    record = raised.value.record
    for attribute in ("requested_by", "requested_tool", "decision", "reason_code",
                      "enforcement_model", "dispatch_count_delta", "no_state_transition"):
        assert getattr(record, attribute) is not None
    assert hashlib.sha256(record.denial_id.encode()).hexdigest()
