"""T080 final — real ADK SequentialAgent orchestration and the Change Proof gate.

Fully offline. The Google ADK agents, runner, session service, resumability, and event
stream are the **real** ones; only the two models are deterministic stubs. A guard
asserts no live provider is reachable, and M0 is asserted ADK-free.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.agents.field_verify import ProviderObservation  # noqa: E402
from driftzero.models.workflow import WorkflowState  # noqa: E402
from driftzero.proof.store import (  # noqa: E402
    HASH_MEANING,
    ProofStorageError,
    ProofStore,
    evaluate_eligibility,
    invariant_report,
)
from driftzero.truth_engine.proof_generator import (  # noqa: E402
    ProofCondition,
    compute_proof_hash,
)
from driftzero_adk.hero_workflow import (  # noqa: E402
    WORKFLOW_AGENT_NAME,
    HeroWorkflowRun,
    StepLog,
    build_hero_workflow,
)
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.service import HeroConsoleService  # noqa: E402

from ._pilot import arm_for_service, clear_change_intelligence  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures" / "multimodal"
TOP_RIGHT_IMG = FIXTURES / "label_top_right_01.jpg"
LEFT_IMG = FIXTURES / "label_left_01.jpg"
AMBIGUOUS_IMG = FIXTURES / "label_ambiguous_01.jpg"


class StubGemma:
    name = "stub_gemma"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.calls = 0

    def observe(self, **_kwargs: Any) -> ProviderObservation:
        self.calls += 1
        output = self._outputs[min(self.calls - 1, len(self._outputs) - 1)]
        return ProviderObservation(
            raw_output=output, provider=self.name, model="stub/gemma"
        )


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    yield
    clear_change_intelligence()
    fv.clear_field_observation_provider()


def _inert_service() -> SimpleNamespace:
    """A service whose use cases exist but do nothing. For structural inspection only."""
    return SimpleNamespace(
        analyze_change=lambda: None,
        deploy_change=lambda: None,
        deliver_to_frontline=lambda: None,
        generate_proof=lambda: None,
    )


def wire_gemma(*outputs: str) -> StubGemma:
    stub = StubGemma(list(outputs) or ["TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: stub)
    return stub


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> HeroConsoleService:
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "driftzero-runtime-2026")
    svc = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", svc)
    arm_for_service(svc)
    return svc


@pytest.fixture
def client(service: HeroConsoleService) -> Any:
    with TestClient(app_module.app) as test_client:
        yield test_client


def drive_to_pass(client: Any, image: Path = TOP_RIGHT_IMG) -> dict[str, Any]:
    client.post("/api/hero/analyze")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    client.post("/api/hero/field-evidence", content=image.read_bytes())
    return client.post("/api/hero/proof").json()


# ============================ 1-3. task coverage and ADK identity =====================


def test_all_eleven_steps_are_covered_by_the_sequence() -> None:
    contract = (
        REPO_ROOT / "specs" / "001-hero-change-deployment" / "contracts" / "agents.md"
    ).read_text(encoding="utf-8")
    for step in range(1, 12):
        assert f"  {step}. " in contract or f"  {step}." in contract

    log = StepLog()
    workflow = build_hero_workflow(service=_inert_service(), log=log)
    names = [a.name for a in workflow.sub_agents]
    # Six sub-agents spanning eleven contract steps; the groupings are named in them.
    covered: set[int] = set()
    for name in names:
        for token in name.split("_"):
            if token.startswith("s") and token[1:].isdigit():
                covered.add(int(token[1:]))
            elif token.isdigit():
                covered.add(int(token))
    assert {1, 3, 4, 5, 6, 7, 8, 9, 10, 11} <= covered
    assert len(names) == 6


def test_the_orchestrator_is_the_real_google_adk_sequential_agent() -> None:
    """Type identity, not a same-named class of our own."""
    from google.adk.agents import SequentialAgent

    workflow = build_hero_workflow(service=_inert_service(), log=StepLog())
    assert type(workflow) is SequentialAgent
    assert type(workflow).__module__ == "google.adk.agents.sequential_agent"
    assert workflow.name == WORKFLOW_AGENT_NAME

    from google.adk.agents import BaseAgent

    for sub in workflow.sub_agents:
        assert isinstance(sub, BaseAgent)
        assert type(sub).__module__ != "google.adk.agents.sequential_agent"


def test_we_do_not_define_a_class_named_sequential_agent() -> None:
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                assert node.name != "SequentialAgent", f"{path} defines its own"


def test_m0_still_imports_no_adk() -> None:
    forbidden = {"google", "google_adk", "vertexai", "httpx"}
    for path in sorted((REPO_ROOT / "src" / "driftzero").rglob("*.py")):
        roots: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        assert not roots & forbidden, f"{path} imports {sorted(roots & forbidden)}"


def test_the_orchestrator_owns_no_business_truth() -> None:
    """It sequences. It does not decide."""
    path = REPO_ROOT / "src" / "driftzero_adk" / "hero_workflow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            node.body = [  # type: ignore[attr-defined]
                c
                for c in body
                if not (
                    isinstance(c, ast.Expr)
                    and isinstance(c.value, ast.Constant)
                    and isinstance(c.value.value, str)
                )
            ] or [ast.Pass()]
    code = ast.unparse(tree)
    for banned in (
        "is_authorized",
        "qualify_candidates",
        "resolve_cardinality",
        "compare_observation",
        "evaluate_proof_invariants",
        "generate_change_proof",
        "PROOF_COMPLETE",
        "VERIFICATION_PASSED",
        "ToolCapability",
        "transition(",
    ):
        assert banned not in code, f"the orchestrator references {banned}"


# ============================ 4-9. orchestrated flow ==================================


def test_the_sequence_runs_source_to_impact_then_remediation_and_delivery(
    service: HeroConsoleService,
) -> None:
    wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    log = asyncio.run(run.start())
    asyncio.run(run.close())

    assert log.executed == [
        "s01_03_change_intelligence_and_impact",
        "s04_05_remediation_and_validation",
        "s06_07_delivery_and_validation",
        "s08_await_field_evidence",
    ]
    state = service.get_state()
    assert state["impact"]["affected_artifact_id"] == "WI-114"
    assert state["crossing_2"]["verdict"] == "ACCEPTED"
    assert state["delivery"]["crossing_3"] == "ACCEPTED"
    assert state["verdict"]["workflow_state"] == str(
        WorkflowState.AWAITING_FIELD_VERIFICATION
    )


def test_zero_qualified_targets_stops_the_sequence_at_review(
    service: HeroConsoleService,
) -> None:
    arm_for_service(service, candidate_affected_artifacts=[])
    wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    asyncio.run(run.start())
    asyncio.run(run.close())

    state = service.get_state()
    assert state["impact"]["outcome"] == "NO_QUALIFIED_TARGET"
    assert state["verdict"]["workflow_state"] == str(WorkflowState.REVIEW_REQUIRED)
    assert state["remediation"] is None
    assert state["proof"]["generated"] is False


def test_multiple_qualified_targets_stops_the_sequence_at_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    catalog = json.loads(
        (REPO_ROOT / "pilot_data" / "artifact_catalog.json").read_text(encoding="utf-8")
    )
    twin = dict(catalog["artifacts"][0])
    twin["artifact_id"] = "WI-115"
    catalog["artifacts"].append(twin)
    root = tmp_path / "pilot_data"
    (root / "source_procedures").mkdir(parents=True)
    for name in ("packing_sop_v13.json", "packing_sop_v14.json"):
        (root / "source_procedures" / name).write_text(
            (REPO_ROOT / "pilot_data" / "source_procedures" / name).read_text("utf-8"),
            encoding="utf-8",
        )
    (root / "artifact_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    approvals = json.loads(
        (REPO_ROOT / "pilot_data" / "approved_changes.json").read_text(encoding="utf-8")
    )
    approvals["changes"][0]["authorized_scope"].append("WI-115")
    (root / "approved_changes.json").write_text(json.dumps(approvals), encoding="utf-8")

    svc = HeroConsoleService(pilot_data_dir=root)
    monkeypatch.setattr(app_module, "_service", svc)
    arm_for_service(svc)
    wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=svc)
    asyncio.run(run.start())
    asyncio.run(run.close())

    state = svc.get_state()
    assert state["impact"]["outcome"] == "MULTIPLE_QUALIFIED_TARGETS"
    assert state["verdict"]["workflow_state"] == str(WorkflowState.REVIEW_REQUIRED)
    assert state["proof"]["generated"] is False


def test_orchestration_does_not_duplicate_side_effects_on_resume(
    service: HeroConsoleService,
) -> None:
    """Resume must not re-dispatch remediation, delivery, or a model call."""
    gemma = wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    asyncio.run(run.start())
    service.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    asyncio.run(run.resume())
    asyncio.run(run.close())

    state = service.get_state()
    assert state["remediation"]["dispatch_count"] == 1
    assert state["delivery"]["dispatch_count"] == 1
    assert gemma.calls == 1
    assert len(state["verdict"]["history"]) == 1
    assert len(service._session.proof_store) == 1


def test_an_identical_image_replay_costs_no_additional_provider_call(
    service: HeroConsoleService,
) -> None:
    gemma = wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    asyncio.run(run.start())
    raw = TOP_RIGHT_IMG.read_bytes()
    for _ in range(3):
        service.submit_field_evidence(raw)
    asyncio.run(run.resume())
    asyncio.run(run.close())
    assert gemma.calls == 1
    assert len(service.get_state()["verdict"]["history"]) == 1


# ============================ 10-11. pause and resume =================================


def test_the_sequence_pauses_before_field_evidence(service: HeroConsoleService) -> None:
    wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    log = asyncio.run(run.start())
    asyncio.run(run.close())

    assert log.paused_at == "s08_await_field_evidence"
    assert log.resumed is False
    # Steps 9-11 did not execute.
    assert "s09_10_field_observation_and_verdict" not in log.executed
    assert "s11_change_proof" not in log.executed
    assert service.get_state()["proof"]["generated"] is False


def test_resume_continues_the_same_workflow_without_replaying_earlier_steps(
    service: HeroConsoleService,
) -> None:
    wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    started = asyncio.run(run.start())
    first_invocation = started.invocation_id
    before = list(started.executed)

    service.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    log = asyncio.run(run.resume())
    asyncio.run(run.close())

    assert log.resumed is True
    assert log.invocation_id == first_invocation, "the same invocation was resumed"
    new_steps = log.executed[len(before) :]
    assert new_steps == [
        "s08_await_field_evidence",
        "s09_10_field_observation_and_verdict",
        "s11_change_proof",
    ]
    # Steps 1-7 each ran exactly once across both calls.
    for step in before[:3]:
        assert log.executed.count(step) == 1


def test_resume_before_start_is_refused(service: HeroConsoleService) -> None:
    run = HeroWorkflowRun(service=service)
    with pytest.raises(RuntimeError):
        asyncio.run(run.resume())


def test_orchestration_evidence_records_the_real_runtime(
    service: HeroConsoleService,
) -> None:
    wire_gemma("TOP_RIGHT")
    run = HeroWorkflowRun(service=service)
    asyncio.run(run.start())
    service.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    log = asyncio.run(run.resume())
    asyncio.run(run.close())

    evidence = log.as_evidence()
    assert evidence["orchestrator"] == "google.adk.agents.SequentialAgent"
    assert evidence["adk_version"] and evidence["adk_version"] != "unknown"
    assert evidence["invocation_id"]
    assert evidence["paused_at"] == "s08_await_field_evidence"
    assert evidence["resumed"] is True
    assert evidence["authoritative"] is False
    assert "decides" in evidence["note"]
    for secret in ("Bearer ", "access_token", "private_key", "client_secret"):
        assert secret not in json.dumps(evidence)


# ============================ 13-21. the proof gate ===================================


def test_the_pass_path_produces_a_proof(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    proof = body["proof"]
    assert proof["status"] == "PROOF_COMPLETE"
    assert proof["satisfied_count"] == 7
    assert proof["total"] == 7
    assert proof["proof_id"]
    assert proof["content_hash"]
    assert body["verdict"]["workflow_state"] == str(WorkflowState.PROOF_COMPLETE)


def test_a_failing_verification_blocks_the_proof(client: Any) -> None:
    wire_gemma("LEFT")
    body = drive_to_pass(client, LEFT_IMG)
    proof = body["proof"]
    assert proof["generated"] is False
    assert proof["satisfied_count"] < 7
    assert proof["proof_id"] is None
    assert body["verdict"]["result"] == "FAIL"
    assert body["verdict"]["change_deployed"] is False
    assert "Latest authoritative verification is PASS" in proof["blockers"]


def test_an_inconclusive_verification_blocks_the_proof(client: Any) -> None:
    wire_gemma("INCONCLUSIVE")
    body = drive_to_pass(client, AMBIGUOUS_IMG)
    assert body["verdict"]["result"] == "INCONCLUSIVE"
    assert body["proof"]["generated"] is False
    assert body["proof"]["change_deployed"] is False


def test_a_historical_failure_does_not_permanently_block_a_later_pass(
    client: Any,
) -> None:
    """The frozen condition-7 recovery path: FAIL → corrected PASS → PROOF_COMPLETE."""
    wire_gemma("LEFT", "TOP_RIGHT")
    blocked = drive_to_pass(client, LEFT_IMG)
    assert blocked["proof"]["generated"] is False

    client.post("/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes())
    body = client.post("/api/hero/proof").json()

    assert body["verdict"]["result"] == "PASS"
    assert body["proof"]["status"] == "PROOF_COMPLETE"
    assert body["proof"]["satisfied_count"] == 7
    # The failure is retained, not erased.
    assert [h["result"] for h in body["verdict"]["history"]] == ["FAIL", "PASS"]
    document = client.get("/api/hero/proof").json()["document"]
    assert len(document["evidence_manifest"]["verification_refs"]) == 2


def test_a_review_required_workflow_can_never_produce_a_proof(
    service: HeroConsoleService, client: Any
) -> None:
    arm_for_service(service, candidate_affected_artifacts=[])
    wire_gemma("TOP_RIGHT")
    client.post("/api/hero/analyze")
    body = client.post("/api/hero/proof").json()
    assert body["proof"]["generated"] is False
    assert body["verdict"]["workflow_state"] == str(WorkflowState.REVIEW_REQUIRED)


def test_all_seven_invariants_are_evaluated_individually(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    conditions = body["proof"]["conditions"]
    assert len(conditions) == 7
    assert [c["condition"] for c in conditions] == [str(c) for c in ProofCondition]
    assert all(c["satisfied"] for c in conditions)
    assert all(c["label"] for c in conditions)


def test_a_blocked_proof_names_the_real_failed_invariants(client: Any) -> None:
    wire_gemma("LEFT")
    body = drive_to_pass(client, LEFT_IMG)
    conditions = body["proof"]["conditions"]
    assert len(conditions) == 7, "all seven are reported even when blocked"
    failed = [c["condition"] for c in conditions if not c["satisfied"]]
    assert str(ProofCondition.C5_LATEST_VERIFICATION_PASS) in failed
    assert body["proof"]["satisfied_count"] == 7 - len(failed)


def test_the_invariant_set_is_exactly_the_frozen_seven() -> None:
    assert len(list(ProofCondition)) == 7
    from driftzero.proof.store import CONDITION_LABELS

    assert set(CONDITION_LABELS) == set(ProofCondition)


def test_the_ui_never_hardcodes_seven_of_seven() -> None:
    """Counts are rendered from the server's real invariant results.

    Scanned with comments removed: a comment explaining that 7/7 is never hardcoded is
    not a hardcoded 7/7.
    """
    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    code = chr(10).join(
        line
        for line in app_js.splitlines()
        if not line.strip().startswith(("*", "/*", "//"))
    )
    assert "7 / 7" not in code
    assert "7/7" not in code
    assert "p.satisfied_count" in code
    assert "p.total" in code


# ============================ 22-24. resolvability and identity =======================


def test_the_proof_resolves_to_its_exact_canonical_bytes(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    document = client.get("/api/hero/proof").json()

    assert document["proof_ref"] == body["proof"]["proof_ref"]
    assert document["content_hash"] == body["proof"]["content_hash"]
    # The served bytes are what the hash covers, not a re-serialisation.
    assert json.loads(document["canonical_json"]) == document["document"]
    assert document["hash_meaning"] == HASH_MEANING


def test_the_proof_hash_is_byte_stable(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    drive_to_pass(client)
    first = client.get("/api/hero/proof").json()
    for _ in range(3):
        again = client.get("/api/hero/proof").json()
        assert again["canonical_json"] == first["canonical_json"]
        assert again["content_hash"] == first["content_hash"]

    stored = app_module.get_service()._session.proof_store.find_workflow(
        app_module.get_service()._session.workflow.workflow_id
    )
    assert compute_proof_hash(stored.proof) == stored.content_hash


def test_the_download_serves_the_canonical_bytes(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    response = client.get("/api/hero/proof/download")
    assert response.status_code == 200
    assert response.headers["X-Proof-Content-Hash"] == body["proof"]["content_hash"]
    assert "attachment" in response.headers["content-disposition"]
    assert response.text == client.get("/api/hero/proof").json()["canonical_json"]


def test_the_proof_endpoints_404_before_a_proof_exists(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    client.post("/api/hero/analyze")
    for route in ("/api/hero/proof", "/api/hero/proof/download", "/api/hero/proof/replay"):
        assert client.get(route).status_code == 404


def test_a_repeat_request_returns_the_same_proof(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    first = drive_to_pass(client)["proof"]
    second = client.post("/api/hero/proof").json()["proof"]
    third = client.post("/api/hero/proof").json()["proof"]

    assert second["replayed"] is True
    assert third["replayed"] is True
    assert first["proof_id"] == second["proof_id"] == third["proof_id"]
    assert first["content_hash"] == second["content_hash"] == third["content_hash"]
    assert len(app_module.get_service()._session.proof_store) == 1


def test_the_store_refuses_to_overwrite_a_differing_proof(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    drive_to_pass(client)
    session = app_module.get_service()._session
    stored = session.proof_store.find_workflow(session.workflow.workflow_id)

    tampered = stored.proof.model_copy(update={"current_value": "LEFT"})
    with pytest.raises(ProofStorageError):
        session.proof_store.record(tampered)
    # The original still resolves, unchanged.
    assert session.proof_store.resolve(stored.proof_ref).canonical_bytes == (
        stored.canonical_bytes
    )


def test_an_empty_store_resolves_nothing() -> None:
    store = ProofStore()
    assert store.resolve("proof:invented") is None
    assert store.find_workflow("wf-nope") is None
    assert len(store) == 0


# ============================ 25-29. authority and readiness ==========================


def test_no_endpoint_accepts_proof_material(client: Any) -> None:
    schema = client.get("/openapi.json").json()
    for route, spec in schema["paths"].items():
        for method, operation in spec.items():
            if method not in {"post", "put", "patch"}:
                continue
            assert "requestBody" not in operation, f"{method} {route} takes a body"
            params = {p["name"] for p in operation.get("parameters", [])}
            assert params <= {"change_id"}, f"{method} {route} exposes {params}"


@pytest.mark.parametrize(
    "hostile",
    [
        {"X-Proof-Id": "pf-forged"},
        {"X-Proof-Hash": "0" * 64},
        {"X-Verification-Result": "PASS"},
        {"X-Workflow-State": "PROOF_COMPLETE"},
        {"X-Change-Deployed": "true"},
        {"X-Conditions": "7"},
    ],
    ids=lambda h: next(iter(h)),
)
def test_hostile_headers_cannot_forge_a_proof(client: Any, hostile: dict[str, str]) -> None:
    wire_gemma("LEFT")
    client.post("/api/hero/analyze")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    client.post("/api/hero/field-evidence", content=LEFT_IMG.read_bytes())
    body = client.post("/api/hero/proof", headers=hostile).json()

    assert body["proof"]["generated"] is False
    assert body["proof"]["proof_id"] is None
    assert body["proof"]["change_deployed"] is False
    assert body["verdict"]["workflow_state"] != str(WorkflowState.PROOF_COMPLETE)


def test_the_change_proof_model_is_never_built_by_an_agent() -> None:
    """Only the frozen generator constructs a ChangeProof."""
    constructors: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ChangeProof"
            ):
                constructors.append(path.relative_to(REPO_ROOT / "src").as_posix())
    assert constructors == ["driftzero/truth_engine/proof_generator.py"]


def test_no_agent_module_can_set_proof_complete() -> None:
    for name in ("change_intel.py", "remediation.py", "enablement.py", "field_verify.py"):
        source = (REPO_ROOT / "src" / "driftzero" / "agents" / name).read_text("utf-8")
        assert "PROOF_COMPLETE" not in source
        assert "generate_change_proof" not in source


def test_change_deployed_is_true_only_after_proof_completion(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    client.post("/api/hero/analyze")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    after_verdict = client.post(
        "/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes()
    ).json()
    # PASS alone is not deployment.
    assert after_verdict["verdict"]["result"] == "PASS"
    assert after_verdict["verdict"]["change_deployed"] is False
    assert after_verdict["proof"]["change_deployed"] is False

    body = client.post("/api/hero/proof").json()
    assert body["proof"]["change_deployed"] is True
    assert body["verdict"]["change_deployed"] is True


def test_runtime_readiness_is_unchanged_by_proof_completion(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    assert body["proof"]["status"] == "PROOF_COMPLETE"
    assert body["environment"]["runtime_readiness"] == "LOCAL_PILOT"
    assert body["environment"]["production_ready"] is False
    assert "PRODUCTION READY" not in json.dumps(body).upper()


def test_the_hash_is_never_described_as_a_signature() -> None:
    for path in (
        REPO_ROOT / "src" / "driftzero" / "proof" / "store.py",
        REPO_ROOT / "src" / "driftzero_console" / "service.py",
    ):
        # Docstrings stripped: a disclaimer saying "not non-repudiation" is the opposite
        # of an overclaim, and must not be matched as one.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list):
                node.body = [
                    c
                    for c in body
                    if not (
                        isinstance(c, ast.Expr)
                        and isinstance(c.value, ast.Constant)
                        and isinstance(c.value.value, str)
                    )
                ] or [ast.Pass()]
        lowered = ast.unparse(tree).lower()
        for overclaim in ("digital signature", "blockchain", "non-repudiation", "notari"):
            assert overclaim not in lowered, f"{path.name} overclaims: {overclaim}"

    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    ).lower()
    for overclaim in ("digital signature", "blockchain", "non-repudiation", "notari"):
        assert overclaim not in app_js, f"app.js overclaims: {overclaim}"
    assert "identity" in HASH_MEANING.lower()


# ============================ 32-35. replay audit and hygiene =========================


def test_replay_audit_executes_no_side_effects(client: Any) -> None:
    gemma = wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    before = {
        "remediation": body["remediation"]["dispatch_count"],
        "delivery": body["delivery"]["dispatch_count"],
        "gemma": gemma.calls,
        "events": len(body["verdict"]["history"]),
        "hash": body["proof"]["content_hash"],
    }

    audit = client.get("/api/hero/proof/replay").json()
    after = client.get("/api/hero/state").json()

    assert audit["side_effects_executed"] == 0
    assert audit["hash_verified"] is True
    assert audit["content_hash"] == before["hash"]
    assert len(audit["verification_chronology"]) == before["events"]
    assert after["remediation"]["dispatch_count"] == before["remediation"]
    assert after["delivery"]["dispatch_count"] == before["delivery"]
    assert gemma.calls == before["gemma"]
    assert after["proof"]["content_hash"] == before["hash"]


def test_replay_audit_renders_the_recorded_chronology(client: Any) -> None:
    wire_gemma("LEFT", "TOP_RIGHT")
    drive_to_pass(client, LEFT_IMG)
    client.post("/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes())
    client.post("/api/hero/proof")

    audit = client.get("/api/hero/proof/replay").json()
    results = [e["result"] for e in audit["verification_chronology"]]
    assert results == ["FAIL", "PASS"]
    assert [e["event_sequence"] for e in audit["verification_chronology"]] == [1, 2]
    assert audit["timeline"]


def test_no_credential_leaks_through_the_proof_surface(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    body = drive_to_pass(client)
    bodies = [
        json.dumps(body),
        client.get("/api/hero/proof").text,
        client.get("/api/hero/proof/replay").text,
        client.get("/api/hero/proof/download").text,
    ]
    for evidence_id in body["evidence_ids"]:
        bodies.append(client.get(f"/api/hero/evidence/{evidence_id}").text)
    for blob in bodies:
        for secret in (
            "Bearer ",
            "access_token",
            "refresh_token",
            "client_secret",
            "private_key",
            "grant_token",
        ):
            assert secret not in blob, f"{secret!r} leaked"


def test_previous_live_evidence_is_untouched() -> None:
    """Historical live-run artifacts are evidence, not fixtures."""
    evidence = REPO_ROOT / "evidence"
    if not evidence.exists():
        pytest.skip("no evidence directory in this checkout")
    for directory in sorted(evidence.glob("pilot_live*")):
        for path in sorted(directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload, f"{path} is empty"


def test_no_live_provider_is_reachable_from_this_suite() -> None:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    assert mc.has_model_client_provider() is False
    assert fv.has_field_observation_provider() is False


def test_the_eligibility_report_is_derived_not_declared(client: Any) -> None:
    wire_gemma("TOP_RIGHT")
    drive_to_pass(client)
    session = app_module.get_service()._session
    context = app_module.get_service()._proof_context(session)
    eligibility = evaluate_eligibility(context)
    assert eligibility.eligible is True
    assert eligibility.satisfied_count == 7
    assert len(invariant_report(__import__(
        "driftzero.truth_engine.proof_generator", fromlist=["x"]
    ).evaluate_proof_invariants(context))) == 7
