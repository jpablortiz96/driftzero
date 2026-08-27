"""Hero Console v0.1 — integration tests over the real domain path.

The console is driven through its HTTP API, and every assertion is about what the real
components actually did: dispatch counts, authoritative hashes, and policy decisions.
Nothing is stubbed, and no label is trusted because the UI would render it.

Fully offline: no model, no cloud, no network beyond the in-process test client.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero_console.app import app, get_service  # noqa: E402
from driftzero_console.service import UNRELATED_INSTRUCTIONS  # noqa: E402

REQUIREMENT = "label_position"

from ._pilot import (  # noqa: E402
    analyze_and_deploy,
    arm_pilot_analysis,
    clear_change_intelligence,
)


@pytest.fixture
def client() -> TestClient:
    """A fresh demo session per test, so ordering never leaks between cases."""
    service = get_service()
    service.reset_demo()
    # Remediation is gated on a qualified impact target, so every session needs a real
    # analysis first. The ADK runtime is real; only the model is a stub.
    arm_pilot_analysis(service.current_change)
    with TestClient(app) as test_client:
        yield test_client
    clear_change_intelligence()


def analyze(client: TestClient) -> dict:
    response = client.post("/api/hero/analyze")
    assert response.status_code == 200
    return response.json()


def state(client: TestClient) -> dict:
    response = client.get("/api/hero/state")
    assert response.status_code == 200
    return response.json()


def deploy(client: TestClient) -> dict:
    """Analyse, then deploy. Remediation without a qualified target is refused."""
    response = analyze_and_deploy(client)
    assert response.status_code == 200
    return response.json()


def security_test(client: TestClient) -> dict:
    response = client.post("/api/hero/security-test")
    assert response.status_code == 200
    return response.json()


# ============================ console loads ===========================================


def test_the_console_page_loads(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "DRIFT" in response.text
    assert "/static/app.js" in response.text


def test_static_assets_are_served_locally(client: TestClient) -> None:
    for asset in ("/static/styles.css", "/static/app.js"):
        assert client.get(asset).status_code == 200


def test_no_external_origin_is_referenced(client: TestClient) -> None:
    """The console must work with no internet connection."""
    markup = client.get("/").text + client.get("/static/app.js").text
    markup += client.get("/static/styles.css").text
    for remote in ("http://", "https://", "cdn.", "//unpkg", "//cdnjs"):
        assert remote not in markup.replace("http://127.0.0.1", "")


# ============================ initial state ===========================================


def test_initial_state_is_the_canonical_left_scenario(client: TestClient) -> None:
    """The change is derived from real source versions; the target is not yet known."""
    body = state(client)
    scenario = body["scenario"]

    assert scenario["change_id"] == "DZ-001"
    assert scenario["previous_version"] == "v13"
    assert scenario["source_version"] == "v14"
    assert scenario["requirement_id"] == REQUIREMENT
    assert scenario["previous_value"] == "LEFT"
    assert scenario["current_value"] == "TOP_RIGHT"

    # Impact is undetermined at boot. Nothing here knows WI-114 exists as a target.
    assert scenario["affected_artifact_id"] is None
    assert scenario["impact_determined"] is False
    assert scenario["remediation_available"] is False
    assert body["artifact"] is None
    assert body["impact"] is None

    # It becomes known only after analysis and the deterministic gate.
    analyzed = analyze(client)
    assert analyzed["impact"]["affected_artifact_id"] == "WI-114"
    assert analyzed["artifact"]["requirements"][REQUIREMENT] == "LEFT"


def test_nothing_is_claimed_before_deployment(client: TestClient) -> None:
    body = state(client)
    assert body["remediation"] is None
    assert body["crossing_2"] is None
    assert body["security"] is None
    assert body["evidence_ids"] == [f"source-change-{body['session_id']}"]


# ============================ deploy ==================================================


def test_deploy_mutates_the_requirement_to_top_right(client: TestClient) -> None:
    body = deploy(client)
    assert body["artifact"]["requirements"][REQUIREMENT] == "TOP_RIGHT"
    assert body["remediation"]["status"] == "MUTATED"
    assert body["remediation"]["remediation_type"] == "MUTATION"
    assert body["remediation"]["reconciled"] is False


def test_deploy_leaves_the_unrelated_lexical_left_untouched(client: TestClient) -> None:
    body = deploy(client)
    requirements = body["artifact"]["requirements"]
    assert requirements["instructions"] == UNRELATED_INSTRUCTIONS
    assert "LEFT" in requirements["instructions"]
    assert requirements["packing_mode"] == "STANDARD"


def test_deploy_dispatches_exactly_once(client: TestClient) -> None:
    body = deploy(client)
    assert body["remediation"]["dispatch_count"] == 1
    assert body["remediation"]["dispatched"] is True


def test_deploy_is_accepted_at_crossing_2(client: TestClient) -> None:
    crossing = deploy(client)["crossing_2"]
    assert crossing["verdict"] == "ACCEPTED"
    assert crossing["accepted"] is True
    assert crossing["failed_layers"] == []
    assert crossing["rejections"] == []
    assert crossing["requires_review"] is False


def test_crossing_2_reports_authoritative_hashes(client: TestClient) -> None:
    crossing = deploy(client)["crossing_2"]
    before, after = crossing["authoritative_before_hash"], crossing["authoritative_after_hash"]
    assert len(before) == 64 and len(after) == 64
    assert before != after


def test_the_timeline_records_real_events(client: TestClient) -> None:
    events = [e["event"] for e in deploy(client)["timeline"]]
    assert events[0] == "SOURCE_CHANGE_RECEIVED"
    for expected in ("REMEDIATION_REQUESTED", "AUTHORIZATION_GRANTED", "ARTIFACT_MUTATED",
                     "CROSSING_2_ACCEPTED"):
        assert expected in events


# ============================ idempotency =============================================


def test_a_second_deploy_creates_no_second_dispatch(client: TestClient) -> None:
    first = deploy(client)
    assert first["remediation"]["dispatch_count"] == 1

    second = deploy(client)
    assert second["remediation"]["status"] == "ALREADY_COMPLETED"
    assert second["remediation"]["dispatch_count"] == 1
    assert second["remediation"]["dispatched"] is False
    assert second["artifact"]["requirements"][REQUIREMENT] == "TOP_RIGHT"


def test_the_idempotent_replay_is_visible_in_the_timeline(client: TestClient) -> None:
    deploy(client)
    events = [e["event"] for e in deploy(client)["timeline"]]
    assert "IDEMPOTENT_REPLAY" in events


# ============================ reset ===================================================


def test_reset_creates_a_fresh_context_rather_than_undoing(client: TestClient) -> None:
    deployed = deploy(client)
    assert deployed["artifact"]["requirements"][REQUIREMENT] == "TOP_RIGHT"

    reset = client.post("/api/hero/session").json()
    # A new session re-ingests the source and forgets the target entirely.
    assert reset["artifact"] is None
    assert reset["scenario"]["impact_determined"] is False
    assert analyze(client)["artifact"]["requirements"][REQUIREMENT] == "LEFT"
    assert reset["session_id"] != deployed["session_id"]
    assert reset["remediation"] is None
    assert reset["evidence_ids"] == [f"source-change-{reset['session_id']}"]
    assert [e["event"] for e in reset["timeline"]] == ["SOURCE_CHANGE_RECEIVED"]


def test_reset_issues_a_new_action_identity(client: TestClient) -> None:
    """An action identity exists only once a target does, and differs per session."""
    assert state(client)["scenario"]["action_id"] is None, "no target, no action"
    before = analyze(client)["scenario"]["action_id"]

    client.post("/api/hero/session")
    assert state(client)["scenario"]["action_id"] is None
    after = analyze(client)["scenario"]["action_id"]

    assert before and after
    assert before != after


# ============================ security ================================================


def test_the_security_test_is_denied(client: TestClient) -> None:
    security = security_test(client)["security"]
    assert security["denied"] is True
    assert security["status"] == "CAPABILITY_DENIED"

    denial = security["denial"]
    assert denial["requested_by"] == "driftzero-enablement"
    assert denial["requested_tool"] == "ARTIFACT_MUTATION"
    assert denial["decision"] == "DENIED"
    assert denial["reason_code"] == "IDENTITY_NOT_AUTHORIZED_FOR_TOOL"


def test_the_security_test_causes_zero_mutation(client: TestClient) -> None:
    body = security_test(client)
    # Measured on the probe's own target, which needs no impact qualification.
    assert body["security"]["artifact_hash_unchanged"] is True
    assert body["security"]["dispatch_count_before"] == 0
    assert body["security"]["dispatch_count_after"] == 0
    assert body["security"]["dispatch_count_unchanged"] is True
    assert body["security"]["denial"]["dispatch_count_delta"] == 0


def test_the_security_test_leaves_the_artifact_hash_unchanged(client: TestClient) -> None:
    """Measured independently by the service, not copied from the denial record."""
    security = security_test(client)["security"]
    assert security["artifact_hash_before"] == security["artifact_hash_after"]
    assert security["artifact_hash_unchanged"] is True
    assert security["denial"]["no_state_transition"] is True


def test_the_security_test_does_not_disturb_a_completed_deployment(client: TestClient) -> None:
    deploy(client)
    body = security_test(client)
    assert body["artifact"]["requirements"][REQUIREMENT] == "TOP_RIGHT"
    assert body["security"]["dispatch_count_unchanged"] is True
    assert body["remediation"]["dispatch_count"] == 1


def test_the_denial_declares_application_level_enforcement(client: TestClient) -> None:
    denial = security_test(client)["security"]["denial"]
    assert denial["enforcement_model"] == "APPLICATION_LEVEL_ENFORCEMENT"
    assert denial["platform_enforced_per_agent_identity"] is False
    assert denial["shared_runtime_service_account"] == "driftzero-run-sa"


# ============================ agent fleet =============================================


def test_only_remediation_is_allowed_the_mutation_tool(client: TestClient) -> None:
    fleet = state(client)["fleet"]
    allowed = {a["identity"] for a in fleet if a["artifact_mutation"] == "ALLOWED"}
    denied = {a["identity"] for a in fleet if a["artifact_mutation"] == "DENIED"}

    assert allowed == {"driftzero-remediation"}
    assert denied == {
        "driftzero-change-intel",
        "driftzero-enablement",
        "driftzero-field-verify",
    }


def test_the_fleet_reports_the_four_product_agents(client: TestClient) -> None:
    fleet = state(client)["fleet"]
    assert [a["role"] for a in fleet] == [
        "READ / ANALYZE",
        "SCOPED WRITE",
        "DELIVER",
        "OBSERVE",
    ]


def test_no_platform_enforcement_is_claimed(client: TestClient) -> None:
    authorization = state(client)["authorization"]
    assert authorization["enforcement_model"] == "APPLICATION_LEVEL_ENFORCEMENT"
    assert authorization["platform_enforced_per_agent_identity"] is False
    assert "Not Google Cloud IAM" in authorization["note"]


# ============================ secrets never leak ======================================


def test_no_capability_or_grant_token_is_ever_serialized(client: TestClient) -> None:
    bodies = [
        client.get("/api/hero/state").text,
        analyze_and_deploy(client).text,
        client.post("/api/hero/security-test").text,
    ]
    for evidence_id in client.get("/api/hero/state").json()["evidence_ids"]:
        bodies.append(client.get(f"/api/hero/evidence/{evidence_id}").text)

    for body in bodies:
        for secret in ("grant_token", "capability_id", "_secret", "authorized_artifact_ids"):
            assert secret not in body, f"{secret} leaked into an API response"


# ============================ the frontend cannot escalate ============================


def test_no_endpoint_accepts_a_request_body(client: TestClient) -> None:
    """The browser cannot name an identity, tool, action, path, or patch."""
    schema = client.get("/openapi.json").json()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method in {"get", "post"}:
                assert "requestBody" not in operation, f"{method.upper()} {path} accepts a body"


def test_a_frontend_cannot_authorize_itself(client: TestClient) -> None:
    """Supplying a privileged identity is not even expressible."""
    for payload in (
        {"identity": "driftzero-remediation"},
        {"holder": "driftzero-remediation", "tool": "ARTIFACT_MUTATION"},
        {"action_id": "act-anything"},
    ):
        response = client.post("/api/hero/security-test", json=payload)
        # The body is ignored entirely; the identity remains the denied one.
        assert response.status_code == 200
        assert response.json()["security"]["denial"]["requested_by"] == "driftzero-enablement"


def test_no_endpoint_accepts_a_filesystem_path_or_patch(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    parameters = [
        parameter["name"]
        for route, operations in schema["paths"].items()
        for operation in operations.values()
        for parameter in operation.get("parameters", [])
    ]
    # workflow_id addresses the T081 CLI adapter; it is an opaque server-issued
    # identifier, not a path, a patch, or a value the caller may choose meaningfully.
    assert sorted(set(parameters)) == ["change_id", "evidence_id", "workflow_id"]
    for forbidden in ("path", "file", "dir", "uri", "url", "patch", "value", "identity"):
        assert not any(forbidden in name.lower() for name in parameters)


def test_an_unknown_evidence_id_is_not_fabricated(client: TestClient) -> None:
    assert client.get("/api/hero/evidence/does-not-exist").status_code == 404


# ============================ evidence inspector ======================================


def test_evidence_records_are_retrievable_after_deployment(client: TestClient) -> None:
    body = deploy(client)
    ids = body["evidence_ids"]
    assert any(i.startswith("remediation-evidence-") for i in ids)
    assert any(i.startswith("crossing2-") for i in ids)

    for evidence_id in ids:
        document = client.get(f"/api/hero/evidence/{evidence_id}").json()
        assert document["evidence_id"] == evidence_id
        assert document["document"]


def test_the_mutation_evidence_document_is_the_real_discriminated_record(
    client: TestClient,
) -> None:
    body = deploy(client)
    evidence_id = next(i for i in body["evidence_ids"] if i.startswith("remediation-evidence-"))
    document = client.get(f"/api/hero/evidence/{evidence_id}").json()["document"]

    assert document["remediation_type"] == "MUTATION"
    assert document["before_value"] == "LEFT"
    assert document["after_value"] == "TOP_RIGHT"
    assert document["reconciled"] is False
    assert document["before_ref"] != document["after_ref"]


def test_the_denial_evidence_document_is_retrievable(client: TestClient) -> None:
    body = security_test(client)
    evidence_id = body["security"]["evidence_id"]
    document = client.get(f"/api/hero/evidence/{evidence_id}").json()["document"]
    assert document["decision"] == "DENIED"
    assert document["dispatch_count_delta"] == 0


# ============================ future modules are honest ===============================


def test_future_modules_are_marked_not_ready_rather_than_fake_success(
    client: TestClient,
) -> None:
    body = state(client)
    statuses = {m["label"]: m["status"] for m in body["modules"]}

    assert statuses["Mission Control"] == "ACTIVE"
    assert statuses["Agent Fleet"] == "ACTIVE"
    assert statuses["Security"] == "ACTIVE"
    assert statuses["Evidence"] == "PARTIAL"
    assert statuses["Frontline"] == "ACTIVE"
    assert statuses["Change Proof"] == "ACTIVE"
    assert statuses["Coverage"] == "NOT WIRED"


def test_future_capabilities_carry_no_fabricated_numbers(client: TestClient) -> None:
    """Roadmap panels advertise scope, never invented operational metrics.

    Coverage is the tempting one: "82% deployed" would look great and mean nothing,
    because no worker has been reached and no verification has run.
    """
    allowed = {"COMING NEXT", "NOT WIRED", "AWAITING MILESTONE", "PARTIAL", "IMPLEMENTED"}
    body = state(client)
    coverage = next(g for g in body["future_capabilities"] if g["group"] == "Deployment Coverage")

    assert coverage["status"] == "NOT WIRED"
    for group in body["future_capabilities"]:
        assert group["status"] in allowed
        assert group["milestone"]
        for item in group["items"]:
            # Capability *names* may say "Deployment %"; what must never appear is a
            # populated value like "82%" or "17 workers verified".
            assert not re.search(r"\d+\s*%", item), item
            assert not re.search(r"\d+\s*(workers|artifacts|verified|reached)", item), item


def test_change_proof_and_coverage_are_never_reported_as_complete(
    client: TestClient,
) -> None:
    body = deploy(client)
    assert "coverage" not in body
    # Remediation alone earns no proof: physical verification has not run.
    assert body["proof"]["generated"] is False
    assert body["proof"]["eligible"] is False
    assert body["proof"]["change_deployed"] is False


def test_the_timeline_is_labelled_as_ui_activity_not_workflow_state(
    client: TestClient,
) -> None:
    markup = client.get("/").text
    assert "UI ACTIVITY" in markup
    assert "NOT WORKFLOW STATE" in markup
    body = deploy(client)
    for event in body["timeline"]:
        assert "PROOF_COMPLETE" not in event["event"]
        assert "VERIFIED" not in event["event"]
