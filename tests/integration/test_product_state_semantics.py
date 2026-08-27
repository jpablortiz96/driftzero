"""Product-state semantics: what the console may claim, and when.

A post-live correction pass. The first real Google ADK + Gemini run produced a correct
semantic result and a correct backend state, but the console *mapped* that state onto
three claims it had not earned:

1. an Authorization stage reading ``GRANTED`` from policy eligibility alone,
2. a refused historical request rendered as the current remediation state,
3. a wired deterministic comparator advertised as ``NOT WIRED``.

These tests pin the corrected mapping. Fully offline: the real ADK runtime with a stub
model, no Gemini, no Gemma, no Vertex.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.capabilities import (  # noqa: E402
    AgentIdentity,
    ToolCapability,
    is_authorized,
)
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.service import HeroConsoleService  # noqa: E402

from ._pilot import (  # noqa: E402
    arm_for_service,
    clear_change_intelligence,
)


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    yield
    clear_change_intelligence()
    fv.clear_field_observation_provider()


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> HeroConsoleService:
    svc = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", svc)
    arm_for_service(svc)
    return svc


@pytest.fixture
def client(service: HeroConsoleService) -> Any:
    with TestClient(app_module.app) as test_client:
        yield test_client


def capability(state: dict[str, Any], capability_id: str) -> dict[str, Any]:
    return next(c for c in state["capability_status"] if c["id"] == capability_id)


# ============================ 1-3. authorization is not eligibility ===================


def test_impact_qualification_alone_does_not_grant_authorization(client: Any) -> None:
    """Eligibility to hold a capability is not a capability that was obtained."""
    state = client.post("/api/hero/analyze").json()
    assert state["impact"]["outcome"] == "SINGLE_QUALIFIED_TARGET"

    auth = state["authorization_stage"]
    assert auth["status"] == "PENDING"
    assert auth["granted"] is False
    assert auth["identity"] is None
    assert auth["capability"] is None
    assert state["remediation"] is None, "nothing has executed"


def test_no_authorization_granted_event_before_deploy(client: Any) -> None:
    events = [e["event"] for e in client.post("/api/hero/analyze").json()["timeline"]]
    assert "AUTHORIZATION_GRANTED" not in events
    assert "IMPACT_QUALIFIED" in events

    events = [e["event"] for e in client.post("/api/hero/deploy").json()["timeline"]]
    assert "AUTHORIZATION_GRANTED" in events, "the real grant happens on remediation"


def test_authorization_never_renders_a_null_identity(client: Any) -> None:
    """The exact live defect: ``null -> undefined`` beside a GRANTED chip."""
    client.post("/api/hero/deploy")  # refused: no qualified target yet
    state = client.post("/api/hero/analyze").json()

    auth = state["authorization_stage"]
    assert auth["status"] == "PENDING"
    assert auth["granted"] is False
    # A grant is rendered only when both an identity and a capability stand behind it,
    # so there is no state in which one is present and the other is null.
    assert (auth["identity"] is None) == (auth["capability"] is None)
    assert auth["identity"] is None and auth["capability"] is None
    assert auth["detail"], "PENDING must explain itself rather than render blanks"

    # The stage never reaches a renderer that would print null/undefined beside GRANTED.
    assert auth["status"] != "GRANTED"


def test_authorization_is_granted_only_after_remediation_executes(client: Any) -> None:
    client.post("/api/hero/analyze")
    auth = client.post("/api/hero/deploy").json()["authorization_stage"]
    assert auth["status"] == "GRANTED"
    assert auth["granted"] is True
    assert auth["identity"] == str(AgentIdentity.REMEDIATION)
    assert auth["capability"] == str(ToolCapability.ARTIFACT_MUTATION)


def test_policy_eligibility_still_lives_in_the_agent_fleet(client: Any) -> None:
    """Eligibility is shown — just not as an operational grant."""
    state = client.post("/api/hero/analyze").json()
    remediation = next(
        a for a in state["fleet"] if a["identity"] == str(AgentIdentity.REMEDIATION)
    )
    assert remediation["artifact_mutation"] == "ALLOWED"
    assert is_authorized(AgentIdentity.REMEDIATION, ToolCapability.ARTIFACT_MUTATION)
    # Eligible in the matrix, still PENDING in the pipeline.
    assert state["authorization_stage"]["status"] == "PENDING"


def test_the_authorization_stage_is_not_read_from_the_remediation_slot() -> None:
    """Structural: the UI stage reads its own projection, not any truthy record."""
    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    stages = app_js[app_js.index("function renderStages") : app_js.index("function fieldStageSub")]
    authorization = stages[stages.index('label: "Authorization"') :]
    authorization = authorization[: authorization.index("},")]
    assert "auth." in authorization
    assert "state.remediation" not in authorization
    assert "r.identity" not in authorization


# ============================ 4-6. historical request vs current state ================


def test_a_blocked_request_is_preserved_as_history(client: Any) -> None:
    blocked = client.post("/api/hero/deploy").json()
    assert blocked["remediation"] is None, "a refusal is not a remediation"

    rem = blocked["remediation_state"]
    assert rem["executed"] is False
    assert rem["blocked_request_count"] == 1
    assert rem["last_request"]["outcome"] == "BLOCKED_NO_QUALIFIED_TARGET"
    assert rem["last_request"]["executed"] is False
    assert "impact had not yet been qualified" in rem["last_request"]["reason"]


def test_the_blocked_request_survives_a_later_qualification(client: Any) -> None:
    client.post("/api/hero/deploy")
    state = client.post("/api/hero/analyze").json()

    rem = state["remediation_state"]
    # Preserved...
    assert rem["blocked_request_count"] == 1
    assert rem["request_history"][0]["outcome"] == "BLOCKED_NO_QUALIFIED_TARGET"
    # ...but it is not the current state.
    assert rem["state"] == "AWAITING_REMEDIATION"
    assert rem["executed"] is False
    assert state["remediation"] is None
    assert state["crossing_2"] is None
    assert state["validated_execution"] is None


def test_the_timeline_still_records_the_refusal(client: Any) -> None:
    client.post("/api/hero/deploy")
    events = [e["event"] for e in client.post("/api/hero/analyze").json()["timeline"]]
    assert events.index("REMEDIATION_BLOCKED") < events.index("IMPACT_QUALIFIED")


def test_current_remediation_state_before_and_after_qualification(client: Any) -> None:
    assert (
        client.get("/api/hero/state").json()["remediation_state"]["state"]
        == "AWAITING_IMPACT_QUALIFICATION"
    )
    assert (
        client.post("/api/hero/analyze").json()["remediation_state"]["state"]
        == "AWAITING_REMEDIATION"
    )
    assert client.post("/api/hero/deploy").json()["remediation_state"]["state"] == "MUTATED"


def test_the_request_history_is_append_only_and_ordered(client: Any) -> None:
    client.post("/api/hero/deploy")
    client.post("/api/hero/analyze")
    state = client.post("/api/hero/deploy").json()

    history = state["remediation_state"]["request_history"]
    assert [h["sequence"] for h in history] == [1, 2]
    assert [h["outcome"] for h in history] == ["BLOCKED_NO_QUALIFIED_TARGET", "MUTATED"]
    assert [h["executed"] for h in history] == [False, True]


def test_the_execution_evidence_panel_shows_nothing_executed_while_only_blocked(
    client: Any,
) -> None:
    client.post("/api/hero/deploy")
    client.post("/api/hero/analyze")
    state = client.get("/api/hero/state").json()
    # renderEvidenceSummary keys on state.remediation, which stays None until execution.
    assert state["remediation"] is None
    assert state["remediation_state"]["last_request"]["executed"] is False


# ============================ 7-10. capability status model ===========================


def test_the_deterministic_verdict_reports_implemented(client: Any) -> None:
    verdict = capability(client.get("/api/hero/state").json(), "deterministic_verdict")
    assert verdict["implementation"] == "IMPLEMENTED"
    assert verdict["runtime"] == "DETERMINISTIC"
    assert "NOT_WIRED" not in json.dumps(verdict)


def test_the_deterministic_verdict_awaits_an_observation_when_none_exists(
    client: Any,
) -> None:
    verdict = capability(client.post("/api/hero/analyze").json(), "deterministic_verdict")
    assert verdict["operation"] == "AWAITING_FIELD_OBSERVATION"
    assert "validated FieldObservation" in verdict["runtime_detail"]


def test_a_disabled_field_provider_does_not_unwire_the_comparator(client: Any) -> None:
    """Runtime configuration and implementation are different questions."""
    state = client.get("/api/hero/state").json()
    observation = capability(state, "field_observation")
    verdict = capability(state, "deterministic_verdict")

    assert observation["implementation"] == "IMPLEMENTED"
    assert observation["runtime"] == "DISABLED_THIS_SESSION"
    assert observation["operation"] == "AWAITING_EVIDENCE"

    # The comparator is unaffected by the provider being off.
    assert verdict["implementation"] == "IMPLEMENTED"
    assert verdict["runtime"] == "DETERMINISTIC"


def test_a_configured_field_provider_reports_configured(
    service: HeroConsoleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "driftzero-runtime-2026")
    fv.register_field_observation_provider(lambda _c: object())
    with TestClient(app_module.app) as client:
        observation = capability(client.get("/api/hero/state").json(), "field_observation")
    assert observation["runtime"] == "CONFIGURED"
    assert "Vertex AI MaaS" in observation["runtime_detail"]


def test_change_proof_is_implemented_but_not_yet_earned(client: Any) -> None:
    """Wired since T080 step 11 — and correctly reporting that nothing earned it yet."""
    state = client.post("/api/hero/analyze").json()
    proof = capability(state, "change_proof")
    assert proof["implementation"] == "IMPLEMENTED"
    assert proof["runtime"] == "DETERMINISTIC"
    assert "seven frozen completion conditions" in proof["runtime_detail"]
    assert state["proof"]["generated"] is False
    assert state["proof"]["change_deployed"] is False


def test_the_roadmap_no_longer_calls_the_comparator_unwired(client: Any) -> None:
    state = client.get("/api/hero/state").json()
    for group in state["future_capabilities"]:
        if group["group"] == "Field Verification":
            assert group["status"] == "IMPLEMENTED"
            assert not [i for i in group["items"] if "PASS / FAIL — NOT WIRED" in i]
        if group["group"] == "Change Proof":
            assert "Expected-vs-observed comparison" not in group["items"]


# ============================ 11. the three dimensions never contradict ===============


def test_implementation_runtime_and_operation_are_separate_fields(client: Any) -> None:
    for entry in client.get("/api/hero/state").json()["capability_status"]:
        assert {"implementation", "runtime", "operation"} <= set(entry)
        # Three distinct dimensions, never collapsed into one status string.
        assert "status" not in entry


def test_the_dimensions_do_not_contradict_each_other(client: Any) -> None:
    """An unimplemented capability cannot be running, and vice versa."""
    for entry in client.post("/api/hero/analyze").json()["capability_status"]:
        if entry["implementation"] == "NOT_YET_WIRED":
            assert entry["runtime"] == "UNAVAILABLE"
            assert entry["operation"] == "UNAVAILABLE"
        else:
            assert entry["runtime"] != "UNAVAILABLE"


def test_a_disabled_runtime_never_claims_a_completed_operation(client: Any) -> None:
    observation = capability(client.get("/api/hero/state").json(), "field_observation")
    assert observation["runtime"] == "DISABLED_THIS_SESSION"
    assert observation["operation"] in {"AWAITING_EVIDENCE", "PROVIDER_DISABLED"}
    assert observation["operation"] not in {"OBSERVED", "REPLAYED"}


def test_the_pipeline_claims_nothing_completed_after_impact_analysis(
    client: Any,
) -> None:
    """No stage after Impact Analysis may read as operationally complete."""
    state = client.post("/api/hero/analyze").json()
    assert state["impact"]["qualified"] is True

    assert state["authorization_stage"]["granted"] is False
    assert state["remediation"] is None
    assert state["crossing_2"] is None
    assert state["validated_execution"] is None
    assert state["delivery"] is None
    assert state["frontline"]["available"] is False
    assert state["field_verification"]["observation"] is None
    assert state["verdict"]["result"] is None
    assert state["verdict"]["change_deployed"] is False
    assert state["proof"]["generated"] is False
    assert state["proof"]["eligible"] is False


def test_no_frontend_asset_states_a_capability_status_of_its_own() -> None:
    """Every status shown comes from the server.

    Comparing against a server-supplied enum value to pick a colour is fine; writing a
    status *claim* into markup is not, because it would survive the backend changing.
    """
    static = REPO_ROOT / "src" / "driftzero_console" / "static"
    for path in sorted(static.iterdir()):
        if path.suffix == ".html":
            source = path.read_text(encoding="utf-8")
            for claim in ("NOT WIRED", "IMPLEMENTED", "DISABLED", "AWAITING_"):
                assert claim not in source, f"{path.name} states {claim!r} in markup"

    app_js = (static / "app.js").read_text(encoding="utf-8")
    renderer = app_js[app_js.index("function renderCapabilityStatus") :]
    renderer = renderer[: renderer.index("\nfunction ")]
    # Every rendered value is read off the server payload.
    for source_field in ("c.implementation", "c.runtime", "c.operation", "c.label"):
        assert source_field in renderer
    assert "state.capability_status" in renderer
    # The display text is never a literal in the renderer.
    assert '"NOT WIRED"' not in renderer
    assert '"AWAITING_FIELD_OBSERVATION"' not in renderer


# ============================ 12. no model or cloud calls =============================


def test_no_live_provider_is_reachable_from_this_suite() -> None:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    assert mc.has_model_client_provider() is False
    assert fv.has_field_observation_provider() is False
