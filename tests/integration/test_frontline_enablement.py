"""T077 — Frontline Enablement Agent, delta composition, and the production surface.

Two things are proven here that a demo would not bother proving:

* the enablement path works for an **arbitrary** structured requirement, so the packing
  label is data rather than a special case in the code;
* production mode hides unfinished modules instead of advertising them, while
  development stays honest about what is not built.

Fully offline: no model, no cloud, no network beyond the in-process test client.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents.enablement import (  # noqa: E402
    AGENT_IDENTITY,
    DeltaInstruction,
    DeltaStatus,
    FrontlineAcknowledgment,
    FrontlineEnablementAgent,
)
from driftzero.capabilities import (  # noqa: E402
    AgentIdentity,
    CapabilityDenied,
    MutationCapabilityBroker,
    ToolCapability,
    is_authorized,
)
from driftzero.models.artifact import DownstreamArtifact  # noqa: E402
from driftzero.models.change import ApprovedChange  # noqa: E402
from driftzero.models.classification import (  # noqa: E402
    ClassificationLabel,
    DataClassification,
)
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.service import (  # noqa: E402
    ChangeCase,
    Environment,
    HeroConsoleService,
    current_environment,
)

STATIC = REPO_ROOT / "src" / "driftzero_console" / "static"

TORQUE_CASE = ChangeCase(
    change_id="DZ-114",
    source_name="Torque Procedure",
    source_procedure_id="TORQUE-SOP",
    operation_id="OP-ASSY-07",
    previous_version="v4",
    source_version="v5",
    requirement_id="torque_spec",
    previous_value="12 Nm",
    current_value="18 Nm",
    artifact_id="WI-902",
    artifact_type="work_instruction",
    requirements={
        "torque_spec": "12 Nm",
        "tool": "TQ-400 calibrated driver",
        "shift": "NIGHT",
    },
    source_evidence_ref="local://changes/DZ-114",
)
"""A second, completely unrelated structured case. Test fixture only."""


def _classification() -> DataClassification:
    return DataClassification(labels=[ClassificationLabel.SYNTHETIC])


def make_change(**overrides: object) -> ApprovedChange:
    defaults: dict[str, object] = {
        "change_id": "DZ-001",
        "source_procedure_id": "PACKING-SOP",
        "source_version": "v14",
        "previous_version": "v13",
        "operation_id": "OP-PACK-01",
        "requirement_id": "label_position",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": ["WI-114"],
        "approved_status": "APPROVED",
        "source_evidence_ref": "local://changes/DZ-001",
        "received_at": datetime(2026, 8, 25, tzinfo=UTC),
        "data_classification": _classification(),
    }
    defaults.update(overrides)
    return ApprovedChange(**defaults)  # type: ignore[arg-type]


def make_artifact(**overrides: object) -> DownstreamArtifact:
    defaults: dict[str, object] = {
        "artifact_id": "WI-114",
        "artifact_type": "work_instruction",
        "operation_id": "OP-PACK-01",
        "requirement_id": "label_position",
        "current_value": "LEFT",
        "content_ref": "local://artifacts/WI-114",
        "authorized_for_remediation": True,
        "requirements": {
            "label_position": "LEFT",
            "instructions": "Keep the LEFT support arm attached",
            "packing_mode": "STANDARD",
        },
        "data_classification": _classification(),
    }
    defaults.update(overrides)
    return DownstreamArtifact(**defaults)  # type: ignore[arg-type]


def compose(**overrides: object):  # type: ignore[no-untyped-def]
    return FrontlineEnablementAgent().compose_delta(
        change=overrides.pop("change", make_change()),  # type: ignore[arg-type]
        artifact=overrides.pop("artifact", make_artifact()),  # type: ignore[arg-type]
        instruction_id="delta-001",
        **overrides,  # type: ignore[arg-type]
    )


def deploy_and_deliver(client: TestClient) -> dict:
    """The full pilot chain up to an established delivery."""
    client.post("/api/hero/deploy")
    return client.post("/api/hero/deliver").json()


@pytest.fixture
def client() -> TestClient:
    app_module.get_service().reset_demo()
    with TestClient(app_module.app) as test_client:
        yield test_client


# ============================ T077 semantics ==========================================


def test_the_agent_uses_the_enablement_identity() -> None:
    assert str(AGENT_IDENTITY) == "driftzero-enablement"
    assert AGENT_IDENTITY is AgentIdentity.ENABLEMENT


def test_the_delta_carries_the_real_before_and_after() -> None:
    instruction = compose().instruction
    assert instruction.requirement_id == "label_position"
    assert instruction.before_value == "LEFT"
    assert instruction.after_value == "TOP_RIGHT"
    assert instruction.source_version == "v14"
    assert instruction.previous_version == "v13"


def test_the_delta_preserves_unchanged_context() -> None:
    """Telling a worker what changed without what stayed is how the wrong thing moves."""
    instruction = compose().instruction
    assert instruction.unchanged_context == {
        "instructions": "Keep the LEFT support arm attached",
        "packing_mode": "STANDARD",
    }
    assert "label_position" not in instruction.unchanged_context
    assert "LEFT" in instruction.unchanged_context["instructions"]


def test_the_instruction_is_structured_not_prose_only() -> None:
    instruction = compose().instruction
    assert isinstance(instruction, DeltaInstruction)
    for field in ("change_id", "artifact_id", "requirement_id", "before_value",
                  "after_value", "concise_instruction", "unchanged_context",
                  "source_version", "source_evidence_ref"):
        assert field in DeltaInstruction.model_fields
    assert instruction.concise_instruction.strip()


def test_no_rationale_is_invented_when_the_source_states_none() -> None:
    assert compose().instruction.rationale is None


def test_a_rationale_is_carried_only_when_supplied() -> None:
    instruction = compose(rationale="Reduces mis-scans at the outbound gate").instruction
    assert instruction.rationale == "Reduces mis-scans at the outbound gate"


def test_the_agent_never_claims_delivery() -> None:
    """T078 owns delivery and its receipt; composing is not delivering."""
    assert compose().instruction.delivery_established is False


def test_composition_fails_closed_outside_authorized_scope() -> None:
    result = compose(change=make_change(authorized_scope=["WI-OTHER"]))
    assert result.status is DeltaStatus.NOT_IN_AUTHORIZED_SCOPE
    assert result.instruction is None


def test_composition_fails_closed_on_an_operation_mismatch() -> None:
    result = compose(artifact=make_artifact(operation_id="OP-OTHER"))
    assert result.status is DeltaStatus.ARTIFACT_MISMATCH
    assert result.instruction is None


def test_composition_fails_closed_when_the_requirement_is_absent() -> None:
    result = compose(artifact=make_artifact(requirements={"packing_mode": "STANDARD"}))
    assert result.status is DeltaStatus.REQUIREMENT_NOT_PRESENT
    assert result.instruction is None


def test_composition_refuses_a_no_op_change() -> None:
    result = compose(change=make_change(previous_value="LEFT", current_value="LEFT"))
    assert result.status is DeltaStatus.NO_CHANGE
    assert result.instruction is None


# ============================ enablement cannot mutate ================================


def test_enablement_is_denied_the_mutation_capability() -> None:
    assert is_authorized(AGENT_IDENTITY, ToolCapability.ARTIFACT_MUTATION) is False
    with pytest.raises(CapabilityDenied):
        MutationCapabilityBroker().issue(
            holder=AGENT_IDENTITY,
            artifact_id="WI-114",
            change_id="DZ-001",
            source_version="v14",
        )


def test_the_enablement_module_holds_no_write_path() -> None:
    import ast

    source = (REPO_ROOT / "src" / "driftzero" / "agents" / "enablement.py").read_text(
        encoding="utf-8"
    )
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "driftzero.tools.artifact_mutation",
        "driftzero.truth_engine.actions",
        "driftzero.truth_engine.state_machine",
        "driftzero.truth_engine.proof_generator",
    ):
        assert forbidden not in imported

    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
    )
    for forbidden in ("apply_authorized_artifact_patch", "MutationCapability", ".issue("):
        assert forbidden not in code


def test_the_delta_has_no_verdict_or_proof_field() -> None:
    for forbidden in ("passed", "failed", "verdict", "proof", "workflow_state", "delivered"):
        assert forbidden not in DeltaInstruction.model_fields


# ============================ arbitrary second case ===================================


def test_composition_works_for_an_unrelated_requirement() -> None:
    """No branch anywhere reads label_position, LEFT, or TOP_RIGHT."""
    change = make_change(
        change_id="DZ-114",
        source_procedure_id="TORQUE-SOP",
        operation_id="OP-ASSY-07",
        requirement_id="torque_spec",
        previous_value="12 Nm",
        current_value="18 Nm",
        authorized_scope=["WI-902"],
    )
    artifact = make_artifact(
        artifact_id="WI-902",
        operation_id="OP-ASSY-07",
        requirement_id="torque_spec",
        current_value="12 Nm",
        content_ref="local://artifacts/WI-902",
        requirements={"torque_spec": "12 Nm", "tool": "TQ-400", "shift": "NIGHT"},
    )
    instruction = compose(change=change, artifact=artifact).instruction

    assert instruction.before_value == "12 Nm"
    assert instruction.after_value == "18 Nm"
    assert "18 Nm" in instruction.concise_instruction
    assert instruction.unchanged_context == {"tool": "TQ-400", "shift": "NIGHT"}


def test_the_frontend_renders_a_second_case_without_source_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console derives everything from application state, not hard-coded markup."""
    service = HeroConsoleService(case=TORQUE_CASE)
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        body = test_client.post("/api/hero/deploy").json()

        assert body["scenario"]["change_id"] == "DZ-114"
        assert body["scenario"]["source"] == "Torque Procedure"
        assert body["scenario"]["requirement_id"] == "torque_spec"
        assert body["scenario"]["previous_value"] == "12 Nm"
        assert body["artifact"]["requirements"]["torque_spec"] == "18 Nm"
        assert body["artifact"]["requirements"]["tool"] == "TQ-400 calibrated driver"
        assert body["crossing_2"]["verdict"] == "ACCEPTED"

        delta = body["frontline"]["instruction"]
        assert delta["requirement_id"] == "torque_spec"
        assert delta["before_value"] == "12 Nm"
        assert delta["after_value"] == "18 Nm"

        test_client.post("/api/hero/deliver")
        worker = test_client.get("/api/hero/frontline/DZ-114").json()
        assert worker["source_name"] == "Torque Procedure"
        assert worker["instruction"]["after_value"] == "18 Nm"


def test_no_pilot_value_is_hard_coded_in_the_frontend() -> None:
    """A grep the product must keep passing as more cases are onboarded."""
    for asset in ("app.js", "frontline.js", "index.html", "frontline.html"):
        source = (STATIC / asset).read_text(encoding="utf-8")
        for pilot_value in ("TOP_RIGHT", "DZ-001", "label_position", "Packing SOP", "WI-114"):
            assert pilot_value not in source, f"{asset} hard-codes {pilot_value}"


# ============================ frontline endpoint + mobile =============================


def test_the_frontline_page_loads(client: TestClient) -> None:
    deploy_and_deliver(client)
    response = client.get("/frontline/DZ-001")
    assert response.status_code == 200
    assert "Process Update" in response.text


def test_the_frontline_page_is_mobile_friendly(client: TestClient) -> None:
    markup = client.get("/frontline/DZ-001").text
    assert 'name="viewport"' in markup
    assert "width=device-width" in markup
    assert "initial-scale=1" in markup
    assert 'name="theme-color"' in markup


def test_the_frontline_api_returns_the_real_delta(client: TestClient) -> None:
    deploy_and_deliver(client)
    view = client.get("/api/hero/frontline/DZ-001").json()

    assert view["available"] is True
    assert view["instruction"]["before_value"] == "LEFT"
    assert view["instruction"]["after_value"] == "TOP_RIGHT"
    assert view["instruction"]["unchanged_context"]["instructions"] == (
        "Keep the LEFT support arm attached"
    )


def test_the_delta_is_unavailable_before_the_change_is_validated(client: TestClient) -> None:
    """Never teach an unvalidated, undelivered change."""
    assert client.get("/api/hero/frontline/DZ-001").status_code == 404
    assert client.get("/api/hero/state").json()["frontline"]["available"] is False


def test_an_unknown_change_has_no_frontline_view(client: TestClient) -> None:
    deploy_and_deliver(client)
    assert client.get("/api/hero/frontline/DZ-NOPE").status_code == 404


# ============================ acknowledgment ==========================================


def test_acknowledgment_is_recorded(client: TestClient) -> None:
    deploy_and_deliver(client)
    view = client.post("/api/hero/frontline/DZ-001/acknowledge").json()

    assert view["acknowledged"] is True
    ack = view["acknowledgment"]
    assert ack["change_id"] == "DZ-001"
    assert ack["acknowledged"] is True
    assert ack["acknowledged_at"]
    assert ack["operator_ref"].startswith("local-session:")


def test_acknowledgment_is_honest_about_missing_worker_identity(client: TestClient) -> None:
    deploy_and_deliver(client)
    ack = client.post("/api/hero/frontline/DZ-001/acknowledge").json()["acknowledgment"]
    assert "UNAUTHENTICATED_LOCAL_SESSION" in ack["identity_basis"]
    assert "not a named worker" in ack["identity_basis"]


def test_acknowledgment_does_not_mean_pass_or_delivery(client: TestClient) -> None:
    deploy_and_deliver(client)
    view = client.post("/api/hero/frontline/DZ-001/acknowledge").json()

    # Delivery is established by the receipt, never by this acknowledgment.
    ack = view["acknowledgment"]
    assert ack["establishes_delivery"] is False
    assert ack["establishes_verification"] is False
    for forbidden in ("passed", "verdict", "proof", "workflow_state"):
        assert forbidden not in FrontlineAcknowledgment.model_fields


def test_acknowledgment_produces_no_change_proof(client: TestClient) -> None:
    deploy_and_deliver(client)
    client.post("/api/hero/frontline/DZ-001/acknowledge")
    body = client.get("/api/hero/state").json()
    assert "proof" not in body
    assert not any("PROOF" in e["event"] for e in body["timeline"])


def test_acknowledgment_dispatches_no_write(client: TestClient) -> None:
    body = deploy_and_deliver(client)
    before = body["validated_execution"]["dispatch_count"]
    client.post("/api/hero/frontline/DZ-001/acknowledge")
    after = client.get("/api/hero/state").json()["validated_execution"]["dispatch_count"]
    assert before == after == 1


def test_the_acknowledgment_clock_is_server_side() -> None:
    instruction = compose().instruction
    moment = datetime(2026, 8, 25, 12, 30, tzinfo=UTC)
    ack = FrontlineEnablementAgent().acknowledge(
        instruction, operator_ref="op-1", identity_basis="TEST", occurred_at=moment
    )
    assert ack.acknowledged_at == moment


# ============================ production mode =========================================


def test_development_is_the_default_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DRIFTZERO_ENV", raising=False)
    assert current_environment() is Environment.DEVELOPMENT


def test_an_unrecognised_environment_fails_to_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured instance must not silently claim to be production."""
    monkeypatch.setenv("DRIFTZERO_ENV", "prod-ish")
    assert current_environment() is Environment.DEVELOPMENT


def test_development_mode_stays_honest(client: TestClient) -> None:
    body = client.get("/api/hero/state").json()
    assert body["environment"]["is_production"] is False
    assert body["environment"]["show_roadmap"] is True
    assert any(m["status"] == "NOT WIRED" for m in body["modules"])


def test_production_mode_hides_unfinished_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIFTZERO_ENV", "production")
    service = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        body = test_client.get("/api/hero/state").json()

    assert body["environment"]["is_production"] is True
    assert body["environment"]["show_roadmap"] is False
    assert body["future_capabilities"] == []
    assert {m["status"] for m in body["modules"]} == {"ACTIVE"}
    assert all(m["status"] != "NOT WIRED" for m in body["modules"])


def test_production_payload_contains_no_demo_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRIFTZERO_ENV", "production")
    service = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        payload = test_client.post("/api/hero/deploy").text

    for banned in ("Demo", "DEMO", "Reset Demo", "NOT WIRED", "COMING NEXT",
                   "Product Roadmap", "LOCAL LIVE"):
        assert banned not in payload, f"production payload contains {banned!r}"


def test_production_terminology_is_used_for_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRIFTZERO_ENV", "production")
    service = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        environment = test_client.get("/api/hero/state").json()["environment"]

    assert environment["session_action_label"] == "New Change"
    assert environment["control_verification_label"] == "Run Control Verification"


def test_the_markup_carries_no_demo_language() -> None:
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    for banned in ("Reset Demo", "Run Security Test", "Product Roadmap", "Local Live"):
        assert banned not in markup


# ============================ agent fleet UX ==========================================


def test_agent_permissions_are_capability_specific(client: TestClient) -> None:
    """DENIED must read as 'not this capability', never as 'agent unavailable'.

    The matrix now carries one column per real capability, so a denial is legible as
    exactly which power the agent lacks.
    """
    from driftzero.capabilities import ToolCapability

    state = client.get("/api/hero/state").json()
    fleet = state["fleet"]
    columns = [str(t) for t in ToolCapability]
    assert state["capability_columns"] == columns

    for agent in fleet:
        assert agent["status"] == "OPERATIONAL"
        assert [c["capability"] for c in agent["capabilities"]] == columns
        for capability in agent["capabilities"]:
            assert capability["permission"] in {"ALLOWED", "DENIED"}

    allowed = {a["identity"] for a in fleet if a["artifact_mutation"] == "ALLOWED"}
    assert allowed == {"driftzero-remediation"}


def test_every_agent_is_operational_even_when_denied_mutation(client: TestClient) -> None:
    fleet = client.get("/api/hero/state").json()["fleet"]
    enablement = next(a for a in fleet if a["identity"] == "driftzero-enablement")
    assert enablement["status"] == "OPERATIONAL"
    assert enablement["role"] == "DELIVER"
    assert enablement["artifact_mutation"] == "DENIED"


# ============================ evidence replay fix =====================================


def test_replay_preserves_the_validated_execution_evidence(client: TestClient) -> None:
    """An idempotent replay must not blank the evidence panel."""
    first = client.post("/api/hero/deploy").json()
    original = first["validated_execution"]
    assert original["remediation_type"] == "MUTATION"

    second = client.post("/api/hero/deploy").json()
    assert second["remediation"]["status"] == "ALREADY_COMPLETED"
    assert second["remediation"]["remediation_type"] is None

    preserved = second["validated_execution"]
    assert preserved == original
    assert preserved["remediation_type"] == "MUTATION"
    assert preserved["crossing_2"] == "ACCEPTED"
    assert preserved["dispatch_count"] == 1
    assert preserved["authoritative_before_hash"] == original["authoritative_before_hash"]


def test_last_request_and_validated_execution_are_separate(client: TestClient) -> None:
    client.post("/api/hero/deploy")
    body = client.post("/api/hero/deploy").json()
    assert body["remediation"]["status"] != body["validated_execution"]["crossing_2"]
    assert body["remediation"]["dispatched"] is False
    assert body["validated_execution"]["dispatch_count"] == 1


# ============================ control verification ====================================


def test_control_verification_remains_a_real_denial(client: TestClient) -> None:
    security = client.post("/api/hero/security-test").json()["security"]
    assert security["denied"] is True
    assert security["denial"]["requested_by"] == "driftzero-enablement"
    assert security["denial"]["reason_code"] == "IDENTITY_NOT_AUTHORIZED_FOR_TOOL"
    assert security["artifact_hash_unchanged"] is True
    assert security["dispatch_count_unchanged"] is True


def test_control_verification_does_not_disturb_the_frontline_delta(
    client: TestClient,
) -> None:
    deploy_and_deliver(client)
    client.post("/api/hero/frontline/DZ-001/acknowledge")
    body = client.post("/api/hero/security-test").json()
    assert body["frontline"]["acknowledged"] is True
    assert body["validated_execution"]["dispatch_count"] == 1


# ============================ honesty and secrets =====================================


def test_no_secret_leaks_through_the_frontline_surface(client: TestClient) -> None:
    deploy_and_deliver(client)
    bodies = [
        client.get("/api/hero/frontline/DZ-001").text,
        client.post("/api/hero/frontline/DZ-001/acknowledge").text,
        client.get("/api/hero/state").text,
    ]
    for body in bodies:
        for secret in ("grant_token", "capability_id", "_secret"):
            assert secret not in body


def test_no_platform_enforcement_is_claimed_anywhere(client: TestClient) -> None:
    body = client.get("/api/hero/state").json()
    assert body["authorization"]["platform_enforced_per_agent_identity"] is False
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "not Google Cloud IAM and not Agent Identity" in markup


def test_the_pilot_case_is_not_labelled_demo_data(client: TestClient) -> None:
    """A real pilot case, presented as such — but never as an external customer's."""
    payload = client.get("/api/hero/state").text
    for banned in ("synthetic data", "fake", "sample data", "dummy"):
        assert banned.lower() not in payload.lower()
