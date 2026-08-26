"""T078 — delivery mechanism, resolvable receipt, and Crossing 3.

The rule this file exists to enforce: a UI render is not delivery, an agent saying
"delivered" is not delivery, and an acknowledgment is not delivery. ``delivery_established``
turns true only when a receipt the mechanism can actually resolve survives Crossing 3.

Fully offline: local pilot channel, injected clock, no model, no cloud.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents.enablement import (  # noqa: E402
    DeltaInstruction,
    FrontlineEnablementAgent,
    delivery_payload,
)
from driftzero.capabilities import (  # noqa: E402
    AUTHORIZATION_POLICY,
    AgentIdentity,
    CapabilityBroker,
    CapabilityDenied,
    MutationCapabilityBroker,
    ToolCapability,
    is_authorized,
)
from driftzero.delivery.local_channel import (  # noqa: E402
    CHANNEL_LOCAL_PILOT,
    DeliveryChannel,
    DeliveryReceipt,
    DeliveryStatus,
    LocalPilotDeliveryChannel,
    payload_hash,
)
from driftzero.models.delivery import DeliveryResult  # noqa: E402
from driftzero.orchestration import (  # noqa: E402
    DeliveryBoundaryResult,
    DeliveryCrossingContext,
    DeliveryRejection,
    accept_delivery_result,
)
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.service import HeroConsoleService  # noqa: E402

from ._pilot import (  # noqa: E402
    analyze_and_deploy,
    arm_for_service,
    clear_change_intelligence,
)
from .test_frontline_enablement import TORQUE_CASE  # noqa: E402

DESTINATION = "frontline:pilot-surface"
MOMENT = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def make_instruction(**overrides: object) -> DeltaInstruction:
    defaults: dict[str, object] = {
        "instruction_id": "delta-001",
        "change_id": "DZ-001",
        "artifact_id": "WI-114",
        "requirement_id": "label_position",
        "before_value": "LEFT",
        "after_value": "TOP_RIGHT",
        "concise_instruction": "Set label position to TOP_RIGHT. It was previously LEFT.",
        "unchanged_context": {"instructions": "Keep the LEFT support arm attached"},
        "source_procedure_id": "PACKING-SOP",
        "source_version": "v14",
        "previous_version": "v13",
        "source_evidence_ref": "local://changes/DZ-001",
    }
    defaults.update(overrides)
    return DeltaInstruction(**defaults)  # type: ignore[arg-type]


def delivery_grant(broker: CapabilityBroker, instruction: DeltaInstruction, **over: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "holder": AgentIdentity.ENABLEMENT,
        "tool": ToolCapability.FRONTLINE_DELIVERY,
        "scope_ref": DESTINATION,
        "change_id": instruction.change_id,
        "source_version": instruction.source_version,
    }
    kwargs.update(over)
    return broker.issue_grant(**kwargs)  # type: ignore[arg-type]


def deliver(channel: LocalPilotDeliveryChannel, instruction: DeltaInstruction):  # type: ignore[no-untyped-def]
    broker = CapabilityBroker()
    return FrontlineEnablementAgent().deliver_delta(
        instruction,
        channel=channel,
        destination_ref=DESTINATION,
        occurred_at=MOMENT,
        grant=delivery_grant(broker, instruction),
        grant_verifier=broker.grant_verifier(ToolCapability.FRONTLINE_DELIVERY),
    )


def crossing(channel: DeliveryChannel, instruction: DeltaInstruction, **overrides: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "channel": channel,
        "instruction": instruction,
        "expected_destination_ref": DESTINATION,
        "rejection_ref": "rej-delivery-001",
    }
    kwargs.update(overrides)
    return DeliveryCrossingContext(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client() -> TestClient:
    service = app_module.get_service()
    service.reset_demo()
    arm_for_service(service)
    with TestClient(app_module.app) as test_client:
        yield test_client
    clear_change_intelligence()


def deploy_and_deliver(client: TestClient) -> dict:
    analyze_and_deploy(client)
    return client.post("/api/hero/deliver").json()


# ============================ delivery boundary =======================================


def test_the_channel_satisfies_the_transport_protocol() -> None:
    assert isinstance(LocalPilotDeliveryChannel(), DeliveryChannel)


def test_the_agent_is_not_coupled_to_a_transport() -> None:
    """The agent names no HTTP, mail, queue, or datastore."""
    source = (REPO_ROOT / "src" / "driftzero" / "agents" / "enablement.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
    )
    for transport in ("http", "requests", "smtp", "pubsub", "firestore", "slack", "socket"):
        assert transport not in code.lower()


def test_delivery_produces_a_typed_receipt() -> None:
    channel = LocalPilotDeliveryChannel()
    dispatch = deliver(channel, make_instruction())
    receipt = dispatch.receipt

    assert isinstance(receipt, DeliveryReceipt)
    assert receipt.instruction_id == "delta-001"
    assert receipt.change_id == "DZ-001"
    assert receipt.channel == CHANNEL_LOCAL_PILOT
    assert receipt.destination_ref == DESTINATION
    assert receipt.status is DeliveryStatus.DELIVERED
    assert receipt.issued_at == MOMENT
    assert len(receipt.payload_hash) == 64


def test_the_receipt_is_honest_about_identity() -> None:
    receipt = deliver(LocalPilotDeliveryChannel(), make_instruction()).receipt
    assert "UNAUTHENTICATED_LOCAL_SESSION" in receipt.identity_basis
    assert "not a named employee" in receipt.identity_basis


def test_no_workforce_reach_is_fabricated() -> None:
    """No email, push, read receipt, or employee identity is *claimed*.

    The identity basis mentions employees only to deny having one, which is the honest
    form. What must not appear is a transport or reach the pilot does not have.
    """
    receipt = deliver(LocalPilotDeliveryChannel(), make_instruction()).receipt
    reach = f"{receipt.destination_ref} {receipt.channel}".lower()
    for fabricated in ("email", "sms", "push", "employee", "workforce", "@"):
        assert fabricated not in reach

    basis = receipt.identity_basis.lower()
    assert "not a named employee" in basis
    assert "no enterprise workforce identity system is integrated" in basis


# ============================ receipt resolution ======================================


def test_the_receipt_resolves_independently() -> None:
    channel = LocalPilotDeliveryChannel()
    dispatch = deliver(channel, make_instruction())
    resolved = channel.resolve(dispatch.evidence_ref)

    assert resolved is not None
    assert resolved == dispatch.receipt
    assert dispatch.evidence_ref in channel.resolvable_refs()


def test_an_unknown_receipt_ref_resolves_to_none() -> None:
    channel = LocalPilotDeliveryChannel()
    deliver(channel, make_instruction())
    assert channel.resolve("local_pilot_frontline:receipt:invented") is None


def test_repeated_reads_return_the_same_receipt() -> None:
    """The resolver survives independent repeated reads."""
    channel = LocalPilotDeliveryChannel()
    ref = deliver(channel, make_instruction()).evidence_ref
    reads = [channel.resolve(ref) for _ in range(5)]
    assert all(r == reads[0] for r in reads)
    assert channel.dispatch_count == 1


def test_historical_receipts_are_never_overwritten() -> None:
    """Append-only: a later delivery cannot rewrite an earlier receipt.

    The lesson T073 taught: a reference that stops resolving to what it described is not
    evidence at all.
    """
    channel = LocalPilotDeliveryChannel()
    first = deliver(channel, make_instruction()).receipt
    second = deliver(channel, make_instruction(instruction_id="delta-002")).receipt

    assert first.evidence_ref != second.evidence_ref
    assert channel.resolve(first.evidence_ref) == first
    assert channel.resolve(second.evidence_ref) == second
    assert len(channel.resolvable_refs()) == 2


def test_the_delivered_payload_is_retained_for_audit() -> None:
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    ref = deliver(channel, instruction).evidence_ref
    assert channel.resolve_payload(ref) == delivery_payload(instruction)


# ============================ payload integrity =======================================


def test_the_receipt_binds_the_exact_instruction() -> None:
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    receipt = deliver(channel, instruction).receipt
    assert receipt.payload_hash == payload_hash(delivery_payload(instruction))


def test_a_different_payload_produces_a_different_hash() -> None:
    """Binding the id alone would let content differ silently."""
    base = delivery_payload(make_instruction())
    altered = delivery_payload(make_instruction(after_value="DIAGONAL"))
    assert base["instruction_id"] == altered["instruction_id"]
    assert payload_hash(base) != payload_hash(altered)


def test_the_hash_helper_reuses_the_frozen_canonical_hash() -> None:
    from driftzero.truth_engine.evidence import canonical_hash

    payload = delivery_payload(make_instruction())
    assert payload_hash(payload) == canonical_hash(payload)


# ============================ Crossing 3 ==============================================


def test_crossing_3_accepts_a_valid_receipt() -> None:
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    dispatch = deliver(channel, instruction)
    verdict = accept_delivery_result(dispatch.result, context=crossing(channel, instruction))

    assert verdict.accepted is True
    assert verdict.delivery_established is True
    assert verdict.receipt == dispatch.receipt
    assert verdict.rejections == ()
    assert verdict.requires_review is False


def test_an_agent_asserting_delivered_without_a_receipt_is_rejected() -> None:
    """The exact failure T078 names: delivered=true is not evidence."""
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    forged = DeliveryResult(
        worker_id=DESTINATION,
        delivery_mechanism=CHANNEL_LOCAL_PILOT,
        delta_content="I delivered it, honestly",
        delivered=True,
        delivery_evidence_ref="local_pilot_frontline:receipt:invented",
    )
    verdict = accept_delivery_result(forged, context=crossing(channel, instruction))

    assert verdict.accepted is False
    assert verdict.delivery_established is False
    assert DeliveryRejection.RECEIPT_NOT_RESOLVABLE in verdict.rejections
    assert "POSITIVE_RECEIPT" in verdict.failed_layers


def test_a_tampered_payload_is_rejected() -> None:
    """The receipt must bind the instruction actually delivered."""
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    dispatch = deliver(channel, instruction)
    tampered = make_instruction(after_value="DIAGONAL")

    verdict = accept_delivery_result(dispatch.result, context=crossing(channel, tampered))
    assert verdict.accepted is False
    assert DeliveryRejection.PAYLOAD_HASH_MISMATCH in verdict.rejections


def test_a_wrong_instruction_id_is_rejected() -> None:
    channel = LocalPilotDeliveryChannel()
    dispatch = deliver(channel, make_instruction())
    other = make_instruction(instruction_id="delta-999")
    verdict = accept_delivery_result(dispatch.result, context=crossing(channel, other))

    assert verdict.accepted is False
    assert DeliveryRejection.INSTRUCTION_MISMATCH in verdict.rejections


def test_a_wrong_change_id_is_rejected() -> None:
    channel = LocalPilotDeliveryChannel()
    dispatch = deliver(channel, make_instruction())
    other = make_instruction(change_id="DZ-999")
    verdict = accept_delivery_result(dispatch.result, context=crossing(channel, other))

    assert verdict.accepted is False
    assert DeliveryRejection.CHANGE_MISMATCH in verdict.rejections


def test_a_wrong_destination_is_rejected() -> None:
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    dispatch = deliver(channel, instruction)
    verdict = accept_delivery_result(
        dispatch.result,
        context=crossing(channel, instruction, expected_destination_ref="frontline:other"),
    )
    assert verdict.accepted is False
    assert "PROVENANCE" in verdict.failed_layers


def test_a_receipt_predating_the_instruction_is_rejected() -> None:
    """A receipt older than the instruction cannot be evidence of delivering it."""
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    dispatch = deliver(channel, instruction)
    verdict = accept_delivery_result(
        dispatch.result,
        context=crossing(channel, instruction, composed_at=MOMENT + timedelta(minutes=5)),
    )
    assert verdict.accepted is False
    assert DeliveryRejection.RECEIPT_PREDATES_INSTRUCTION in verdict.rejections


def test_a_non_delivered_receipt_is_rejected() -> None:
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    dispatch = deliver(channel, instruction)
    failed = replace(dispatch.receipt, status=DeliveryStatus.FAILED)
    channel._receipts[failed.evidence_ref] = failed  # noqa: SLF001

    verdict = accept_delivery_result(dispatch.result, context=crossing(channel, instruction))
    assert verdict.accepted is False
    assert DeliveryRejection.NOT_DELIVERED in verdict.rejections


def test_the_boundary_result_carries_no_workflow_authority() -> None:
    fields = set(DeliveryBoundaryResult.__dataclass_fields__)
    for forbidden in ("workflow_state", "next_state", "verdict", "passed",
                      "proof", "change_proof", "deployed", "field_verified"):
        assert forbidden not in fields


def test_a_rejection_produces_a_deterministic_evidence_reference() -> None:
    channel = LocalPilotDeliveryChannel()
    instruction = make_instruction()
    dispatch = deliver(channel, instruction)
    verdict = accept_delivery_result(
        dispatch.result, context=crossing(channel, make_instruction(change_id="DZ-999"))
    )
    ref = verdict.evidence_ref()
    assert ref is not None and ref.startswith("crossing3-rejected:")
    assert accept_delivery_result(
        dispatch.result, context=crossing(channel, instruction)
    ).evidence_ref() is None


# ============================ pilot flow + idempotency ================================


def test_delta_exists_before_delivery(client: TestClient) -> None:
    body = analyze_and_deploy(client).json()
    assert body["frontline"]["composed"] is True
    assert body["frontline"]["available"] is False
    assert body["delivery"] is None


def test_delivery_establishes_only_after_an_accepted_crossing(client: TestClient) -> None:
    before = analyze_and_deploy(client).json()
    assert before["frontline"]["delivery_established"] is False

    after = client.post("/api/hero/deliver").json()
    delivery = after["delivery"]
    assert delivery["crossing_3"] == "ACCEPTED"
    assert delivery["delivery_established"] is True
    assert delivery["receipt_integrity"] == "VALIDATED"
    assert after["frontline"]["delivery_established"] is True


def test_repeated_delivery_does_not_duplicate_dispatch(client: TestClient) -> None:
    first = deploy_and_deliver(client)["delivery"]
    assert first["last_request"] == "DELIVERED"
    assert first["dispatch_count"] == 1

    second = client.post("/api/hero/deliver").json()["delivery"]
    assert second["last_request"] == "ALREADY_DELIVERED"
    assert second["dispatch_count"] == 1
    assert second["delivery_established"] is True


def test_the_replay_is_visible_in_the_timeline(client: TestClient) -> None:
    deploy_and_deliver(client)
    events = [e["event"] for e in client.post("/api/hero/deliver").json()["timeline"]]
    assert "DELIVERY_ESTABLISHED" in events
    assert "DELIVERY_ALREADY_ESTABLISHED" in events


def test_delivery_uses_the_stable_delivery_action_identity(client: TestClient) -> None:
    deploy_and_deliver(client)
    service = app_module.get_service()
    ledger = service._session.ledger  # noqa: SLF001
    record = ledger.require(service._session.delivery_action_id)  # noqa: SLF001
    assert str(record.action_type) == "DELIVER_DELTA"
    assert str(record.status) == "COMPLETED"
    assert record.receipt_ref


# ============================ semantics stay distinct =================================


def test_delivery_is_not_deployment_or_verification(client: TestClient) -> None:
    delivery = deploy_and_deliver(client)["delivery"]
    assert delivery["change_deployed"] is False
    assert delivery["field_verified"] is False


def test_delivery_creates_no_change_proof(client: TestClient) -> None:
    body = deploy_and_deliver(client)
    # The panel exists and correctly reports that no proof was earned.
    assert body["proof"]["generated"] is False
    assert body["proof"]["proof_id"] is None
    assert body["proof"]["change_deployed"] is False
    assert not any(e["event"] == "PROOF_COMPLETE" for e in body["timeline"])
    assert not any("PASS" in e["event"] for e in body["timeline"])


def test_acknowledgment_remains_independent_of_delivery(client: TestClient) -> None:
    body = deploy_and_deliver(client)
    assert body["frontline"]["delivery_established"] is True
    assert body["frontline"]["acknowledged"] is False

    acknowledged = client.post("/api/hero/frontline/DZ-001/acknowledge").json()
    assert acknowledged["acknowledged"] is True
    assert acknowledged["acknowledgment"]["establishes_delivery"] is False
    assert acknowledged["acknowledgment"]["establishes_verification"] is False


def test_delivery_does_not_alter_the_mutation_evidence(client: TestClient) -> None:
    body = deploy_and_deliver(client)
    assert body["validated_execution"]["dispatch_count"] == 1
    assert body["validated_execution"]["remediation_type"] == "MUTATION"


# ============================ worker route gating =====================================


def test_the_worker_route_is_unavailable_before_delivery(client: TestClient) -> None:
    analyze_and_deploy(client)
    assert client.get("/api/hero/frontline/DZ-001").status_code == 404
    assert client.post("/api/hero/frontline/DZ-001/acknowledge").status_code == 404


def test_the_worker_route_opens_after_validated_delivery(client: TestClient) -> None:
    deploy_and_deliver(client)
    view = client.get("/api/hero/frontline/DZ-001").json()
    assert view["available"] is True
    assert view["delivery_established"] is True
    assert view["instruction"]["after_value"] == "TOP_RIGHT"


# ============================ security ================================================


def test_the_deliver_endpoint_accepts_no_request_body(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/hero/deliver"]["post"]
    assert "requestBody" not in operation
    assert operation.get("parameters", []) == []


def test_the_frontend_cannot_forge_a_receipt_or_choose_identity(client: TestClient) -> None:
    analyze_and_deploy(client)
    for payload in (
        {"delivery_evidence_ref": "local_pilot_frontline:receipt:forged"},
        {"identity": "driftzero-remediation"},
        {"destination_ref": "/etc/passwd"},
        {"payload_hash": "0" * 64},
        {"instruction": {"after_value": "DIAGONAL"}},
    ):
        response = client.post("/api/hero/deliver", json=payload)
        assert response.status_code == 200

    delivery = client.get("/api/hero/state").json()["delivery"]
    # The body is ignored entirely: the server derived everything itself.
    assert delivery["destination_ref"] == DESTINATION
    assert delivery["channel"] == CHANNEL_LOCAL_PILOT
    assert delivery["delivery_established"] is True
    assert delivery["dispatch_count"] == 1


def test_no_receipt_secret_leaks_through_the_api(client: TestClient) -> None:
    deploy_and_deliver(client)
    for body in (
        client.get("/api/hero/state").text,
        client.get("/api/hero/frontline/DZ-001").text,
    ):
        for secret in ("grant_token", "_secret", "capability_id"):
            assert secret not in body


def test_enablement_remains_denied_artifact_mutation() -> None:
    """Delivery granted the agent nothing new."""
    assert is_authorized(AgentIdentity.ENABLEMENT, ToolCapability.ARTIFACT_MUTATION) is False
    with pytest.raises(CapabilityDenied):
        MutationCapabilityBroker().issue(
            holder=AgentIdentity.ENABLEMENT,
            artifact_id="WI-114",
            change_id="DZ-001",
            source_version="v14",
        )


def test_delivery_is_authorized_through_the_single_policy_table() -> None:
    """T079 closed the gap T078 reported: delivery is now a named capability."""
    assert (
        AgentIdentity.ENABLEMENT,
        ToolCapability.FRONTLINE_DELIVERY,
    ) in AUTHORIZATION_POLICY
    assert is_authorized(AgentIdentity.ENABLEMENT, ToolCapability.FRONTLINE_DELIVERY)
    for identity in AgentIdentity:
        if identity is not AgentIdentity.ENABLEMENT:
            assert not is_authorized(identity, ToolCapability.FRONTLINE_DELIVERY)


# ============================ genericity + readiness ==================================


def test_delivery_works_for_an_arbitrary_second_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = HeroConsoleService(case=TORQUE_CASE)
    arm_for_service(service)
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        analyze_and_deploy(test_client)
        delivery = test_client.post("/api/hero/deliver").json()["delivery"]
        assert delivery["delivery_established"] is True
        assert delivery["crossing_3"] == "ACCEPTED"

        view = test_client.get("/api/hero/frontline/DZ-114").json()
        assert view["instruction"]["requirement_id"] == "torque_spec"
        assert view["instruction"]["after_value"] == "18 Nm"


def test_no_pilot_value_is_hard_coded_in_the_delivery_layer() -> None:
    """Code lines only — a docstring may name the pilot to say it is not special."""
    for module in ("delivery/local_channel.py", "agents/enablement.py"):
        source = (REPO_ROOT / "src" / "driftzero" / module).read_text(encoding="utf-8")
        code = chr(10).join(
            line
            for line in source.splitlines()
            if not line.strip().startswith(("#", '"', "'", "``", "*", "-"))
        )
        for pilot in ("DZ-001", "TOP_RIGHT", "Packing SOP", "WI-114"):
            assert pilot not in code, f"{module} branches on {pilot}"


def test_runtime_readiness_is_not_production_ready(client: TestClient) -> None:
    environment = client.get("/api/hero/state").json()["environment"]
    assert environment["runtime_readiness"] == "LOCAL_PILOT"
    assert environment["production_ready"] is False


def test_presentation_mode_is_distinct_from_runtime_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRIFTZERO_ENV selects presentation. It is not evidence of readiness."""
    monkeypatch.setenv("DRIFTZERO_ENV", "production")
    service = HeroConsoleService()
    arm_for_service(service)
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        environment = test_client.get("/api/hero/state").json()["environment"]

    assert environment["presentation_environment"] == "production"
    assert environment["is_production"] is True
    assert environment["runtime_readiness"] == "LOCAL_PILOT"
    assert environment["production_ready"] is False


def test_production_delivery_payload_has_no_demo_terminology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRIFTZERO_ENV", "production")
    service = HeroConsoleService()
    arm_for_service(service)
    monkeypatch.setattr(app_module, "_service", service)

    with TestClient(app_module.app) as test_client:
        analyze_and_deploy(test_client)
        payload = test_client.post("/api/hero/deliver").text

    for banned in ("Demo", "DEMO", "mock", "fake", "NOT WIRED", "COMING NEXT"):
        assert banned not in payload
