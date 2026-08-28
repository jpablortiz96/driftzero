"""T094 — the production HTTP surface.

Offline throughout. The two models are deterministic substitutes and durable persistence
runs against the in-memory Firestore double, so nothing here reaches Google Cloud.

The theme of most of these tests is a single question: can a client, by sending a
request, cause the system to believe something it did not derive? The answer has to be
no for every authoritative field, over every route.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from driftzero.agents import field_verify as fv
from driftzero.agents import model_client as mc
from driftzero.config import DriftZeroConfig, PersistenceConfig
from driftzero_api.app import create_app
from driftzero_api.runtime import ApiRuntime
from driftzero_cloud.composition import FirestoreSink
from driftzero_cloud.firestore import FirestorePersistence
from driftzero_console.workflows import FORBIDDEN_FIXTURE_KEYS
from tests.integration._fake_gcp import FakeFirestoreClient
from tests.integration._pilot import arm_for_service, clear_change_intelligence
from tests.integration.test_restart_persistence import OfflineGemma

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"


def hero_body() -> dict[str, Any]:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    return {k: v for k, v in payload.items() if not k.startswith("_")}


@pytest.fixture
def runtime() -> ApiRuntime:
    return ApiRuntime(fixtures_dir=FIXTURES)


@pytest.fixture
def client(runtime: ApiRuntime) -> TestClient:
    return TestClient(create_app(runtime))


@pytest.fixture
def durable() -> Any:
    """A runtime whose writes land in a shared in-memory database."""
    database = FakeFirestoreClient()
    persistence = FirestorePersistence.over(database)
    runtime = ApiRuntime(
        fixtures_dir=FIXTURES,
        config=DriftZeroConfig.from_env({}),
        sink=FirestoreSink(persistence),
        persistence=persistence,
    )
    return {"database": database, "runtime": runtime, "client": TestClient(create_app(runtime))}


@pytest.fixture
def providers() -> Any:
    """Deterministic model substitutes, torn down so nothing leaks into the suite."""
    import os

    previous = os.environ.get("DRIFTZERO_FIELD_PROVIDER")
    os.environ["DRIFTZERO_FIELD_PROVIDER"] = "vertex_maas"
    gemma = OfflineGemma(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: gemma)
    yield gemma
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    mc.clear_model_client_provider()
    if previous is None:
        os.environ.pop("DRIFTZERO_FIELD_PROVIDER", None)
    else:
        os.environ["DRIFTZERO_FIELD_PROVIDER"] = previous


# ============================ the contract surface ====================================


def test_every_route_the_contract_names_exists(client: TestClient) -> None:
    """contracts/agents.md § API Contract, route for route."""
    created = client.post("/api/v1/changes", json=hero_body())
    assert created.status_code == 201
    workflow_id = created.json()["workflow_id"]

    assert client.get(f"/api/v1/workflows/{workflow_id}").status_code == 200
    assert client.get(f"/api/v1/workflows/{workflow_id}/proof").status_code == 404
    assert client.get(f"/api/v1/workflows/{workflow_id}/evidence").status_code == 200
    # verify is exercised end to end below; here only that it is routed, not 404-missing.
    response = client.post(f"/api/v1/workflows/{workflow_id}/verify")
    assert response.status_code == 422, "the route exists and requires its multipart file"


def test_the_change_response_matches_the_contract_shape(client: TestClient) -> None:
    body = client.post("/api/v1/changes", json=hero_body()).json()
    assert set(body) == {"workflow_id", "state", "duplicate_of"}
    assert body["state"] == "CHANGE_RECEIVED"


def test_a_duplicate_change_returns_the_existing_workflow(client: TestClient) -> None:
    first = client.post("/api/v1/changes", json=hero_body())
    second = client.post("/api/v1/changes", json=hero_body())

    assert first.status_code == 201
    assert second.status_code == 200, "a duplicate created nothing, so not 201"
    assert second.json()["workflow_id"] == first.json()["workflow_id"]
    assert second.json()["duplicate_of"] == first.json()["workflow_id"]


def test_an_unknown_workflow_is_404_and_is_never_fabricated(client: TestClient) -> None:
    for suffix in ("", "/proof", "/evidence"):
        response = client.get(f"/api/v1/workflows/wf-never-existed{suffix}")
        assert response.status_code == 404, suffix
        assert response.json()["detail"]["error"] == "WORKFLOW_NOT_FOUND"


def test_a_workflow_without_a_proof_is_404_not_an_empty_proof(client: TestClient) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    response = client.get(f"/api/v1/workflows/{workflow_id}/proof")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "PROOF_NOT_COMPLETE"


# ============================ authoritative-field refusal =============================


@pytest.mark.parametrize("field", sorted(FORBIDDEN_FIXTURE_KEYS))
def test_no_conclusion_can_be_submitted_over_http(client: TestClient, field: str) -> None:
    """Every field that states an answer is refused by name."""
    body = hero_body()
    body[field] = "PASS"
    response = client.post("/api/v1/changes", json=body)

    assert response.status_code == 400, field
    detail = response.json()["detail"]
    assert detail["error"] == "AUTHORITATIVE_FIELD_REFUSED"
    assert field in detail["refused_fields"]


def test_the_specific_dangerous_values_are_refused(client: TestClient) -> None:
    for field, value in (
        ("affected_artifact_id", "wi-packing-standard-001"),
        ("workflow_state", "PROOF_COMPLETE"),
        ("verification_result", "PASS"),
        ("verdict", "FAIL"),
        ("proof_id", "act-generate_proof-forged"),
        ("content_hash", "0" * 64),
    ):
        body = hero_body()
        body[field] = value
        assert client.post("/api/v1/changes", json=body).status_code == 400, field


def test_an_unknown_field_is_rejected_rather_than_ignored(client: TestClient) -> None:
    body = hero_body()
    body["capabilities"] = ["ARTIFACT_MUTATION"]
    response = client.post("/api/v1/changes", json=body)
    assert response.status_code in {400, 422}
    assert response.status_code != 201, "an unrecognised field must never be accepted"


def test_no_capability_is_minted_from_a_request_body(client: TestClient) -> None:
    body = hero_body()
    body["authorization"] = "GRANTED"
    assert client.post("/api/v1/changes", json=body).status_code == 400


def test_malformed_json_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/v1/changes",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "MALFORMED_JSON"


def test_a_non_object_body_is_refused(client: TestClient) -> None:
    response = client.post("/api/v1/changes", json=["a", "list"])
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "MALFORMED_BODY"


def test_a_missing_required_field_is_refused(client: TestClient) -> None:
    body = hero_body()
    del body["change_id"]
    assert client.post("/api/v1/changes", json=body).status_code == 422


# ============================ end to end over HTTP ====================================


def drive_to_proof(client: TestClient, runtime: ApiRuntime, workflow_id: str) -> Any:
    """Advance the workflow using the same service the console uses."""
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    failed = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("left.jpg", LEFT_IMG.read_bytes(), "image/jpeg")},
    )
    service.generate_proof()
    passed = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("top.jpg", TOP_RIGHT_IMG.read_bytes(), "image/jpeg")},
    )
    service.generate_proof()
    return failed, passed


def test_the_verify_route_reports_the_comparators_verdict(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    failed, passed = drive_to_proof(client, runtime, workflow_id)

    assert failed.status_code == 200
    assert failed.json()["verification_result"] == "FAIL"
    assert passed.json()["verification_result"] == "PASS"
    assert passed.json()["workflow_state"] in {"VERIFICATION_PASSED", "PROOF_COMPLETE"}


def test_the_submission_id_is_server_derived_not_client_supplied(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    """A client-chosen idempotency key would let a caller suppress or force events."""
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    response = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("left.jpg", LEFT_IMG.read_bytes(), "image/jpeg")},
        data={"submission_id": "attacker-chosen-id"},
    )
    assert response.status_code == 200
    assert response.json()["submission_id"] != "attacker-chosen-id"
    assert response.json()["submission_id"], "a submission id was still derived"


def test_the_proof_route_returns_the_stored_proof_unchanged(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    from driftzero.models.proof import ChangeProof
    from driftzero.truth_engine.proof_generator import compute_proof_hash

    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    drive_to_proof(client, runtime, workflow_id)

    response = client.get(f"/api/v1/workflows/{workflow_id}/proof")
    assert response.status_code == 200
    proof = ChangeProof.model_validate(response.json()["document"])
    assert compute_proof_hash(proof) == proof.content_hash, (
        "the route altered the proof between storage and the wire"
    )


def test_the_evidence_route_lists_the_manifest(
    client: TestClient, runtime: ApiRuntime, providers: Any
) -> None:
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    drive_to_proof(client, runtime, workflow_id)

    listing = client.get(f"/api/v1/workflows/{workflow_id}/evidence").json()
    assert listing["workflow_id"] == workflow_id
    assert listing["complete"] is True
    assert listing["source_change_ref"]
    # A content reference, not the artifact id — the manifest addresses bytes.
    assert listing["affected_artifact_ref"]
    assert listing["delivery_ref"]
    assert listing["content_hashes"]
    # Both attempts are listed. The FAIL is evidence, not something to tidy away.
    assert len(listing["verification_refs"]) == 2
    assert len(listing["verification_event_ids"]) == 2


# ============================ durability over the API =================================


def test_status_survives_a_restart_of_the_api_process(durable: Any, providers: Any) -> None:
    """The T081 limitation, closed at the HTTP boundary."""
    client, runtime, database = durable["client"], durable["runtime"], durable["database"]
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    drive_to_proof(client, runtime, workflow_id)
    before = client.get(f"/api/v1/workflows/{workflow_id}").json()
    assert before["source"] == "LIVE_RUNTIME"

    # A restart: a brand-new process sharing only the database.
    persistence = FirestorePersistence.over(database)
    restarted = ApiRuntime(
        fixtures_dir=FIXTURES,
        sink=FirestoreSink(persistence),
        persistence=persistence,
    )
    fresh = TestClient(create_app(restarted))

    after = fresh.get(f"/api/v1/workflows/{workflow_id}").json()
    assert after["source"] == "DURABLE_STORE"
    assert after["state"] == before["state"] == "PROOF_COMPLETE"
    assert after["verification_results"] == ["FAIL", "PASS"]
    assert after["proof_id"] == before["proof_id"]
    assert after["durable"] is True


def test_a_restarted_api_still_refuses_an_unknown_workflow(durable: Any) -> None:
    database = durable["database"]
    persistence = FirestorePersistence.over(database)
    fresh = TestClient(
        create_app(
            ApiRuntime(
                fixtures_dir=FIXTURES,
                sink=FirestoreSink(persistence),
                persistence=persistence,
            )
        )
    )
    assert fresh.get("/api/v1/workflows/wf-never-existed").status_code == 404


def test_the_proof_survives_a_restart_with_an_identical_hash(
    durable: Any, providers: Any
) -> None:
    from driftzero.models.proof import ChangeProof
    from driftzero.truth_engine.proof_generator import compute_proof_hash

    client, runtime, database = durable["client"], durable["runtime"], durable["database"]
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    drive_to_proof(client, runtime, workflow_id)
    before = client.get(f"/api/v1/workflows/{workflow_id}/proof").json()["document"]

    persistence = FirestorePersistence.over(database)
    fresh = TestClient(
        create_app(
            ApiRuntime(
                fixtures_dir=FIXTURES,
                sink=FirestoreSink(persistence),
                persistence=persistence,
            )
        )
    )
    after = fresh.get(f"/api/v1/workflows/{workflow_id}/proof").json()["document"]
    assert after["content_hash"] == before["content_hash"]
    assert compute_proof_hash(ChangeProof.model_validate(after)) == after["content_hash"]


def test_a_terminal_workflow_is_never_resumed(durable: Any, providers: Any) -> None:
    """Since T097 an eligible workflow resumes — a completed one still must not.

    PROOF_COMPLETE is TERMINAL_SUCCESS in the frozen state model. Resuming it would
    reopen a change that has already been proven done.
    """
    client, runtime, database = durable["client"], durable["runtime"], durable["database"]
    workflow_id = client.post("/api/v1/changes", json=hero_body()).json()["workflow_id"]
    drive_to_proof(client, runtime, workflow_id)

    persistence = FirestorePersistence.over(database)
    fresh = TestClient(
        create_app(
            ApiRuntime(
                fixtures_dir=FIXTURES,
                sink=FirestoreSink(persistence),
                persistence=persistence,
            )
        )
    )
    response = fresh.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("x.jpg", LEFT_IMG.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "WORKFLOW_NOT_RESUMABLE"
    assert "TERMINAL_SUCCESS" in response.json()["detail"]["detail"]


# ============================ operational =============================================


def test_liveness_and_readiness_are_distinct(client: TestClient) -> None:
    health = client.get("/health").json()
    ready = client.get("/ready").json()
    assert health == {"status": "ok", "service": "driftzero-api"}
    assert "persistence_backend" in ready and "persistence_backend" not in health


def test_readiness_reports_real_configuration_not_aspiration(client: TestClient) -> None:
    ready = client.get("/ready").json()
    assert ready["persistence_backend"] == "memory"
    assert ready["durable"] is False
    assert ready["deployment"] == "NOT_DEPLOYED", "T096 has not happened"


def test_readiness_reports_durability_when_it_is_real(durable: Any) -> None:
    ready = durable["client"].get("/ready").json()
    assert ready["durable"] is True
    assert ready["deployment"] == "NOT_DEPLOYED", "durable storage is not a deployment"


def test_a_half_configured_durable_backend_is_visible_in_readiness() -> None:
    config = DriftZeroConfig(persistence=PersistenceConfig(backend="firestore"))
    runtime = ApiRuntime(fixtures_dir=FIXTURES, config=config)
    ready = TestClient(create_app(runtime)).get("/ready").json()
    assert ready["durable"] is False
    assert "DRIFTZERO_GCP_PROJECT" in ready["missing_settings"]


# ============================ boundaries ==============================================


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_fastapi_or_google_import_inside_the_purity_boundary() -> None:
    offenders = {
        path.name: sorted(_imported_roots(path) & {"fastapi", "google", "starlette"})
        for path in sorted((SRC / "driftzero").rglob("*.py"))
        if _imported_roots(path) & {"fastapi", "google", "starlette"}
    }
    assert offenders == {}, f"transport dependency inside src/driftzero/: {offenders}"


def test_the_domain_never_imports_the_api_package() -> None:
    offenders = [
        path.name
        for path in sorted((SRC / "driftzero").rglob("*.py"))
        if "driftzero_api" in _imported_roots(path)
    ]
    assert offenders == [], f"domain imports driftzero_api: {offenders}"


def test_the_api_owns_no_business_logic() -> None:
    """Every authoritative decision must be delegated, never re-implemented here."""
    for name in ("routes.py", "pubsub.py", "runtime.py"):
        source = (SRC / "driftzero_api" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                node.body = [
                    n
                    for n in node.body
                    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
                ] or [ast.Pass()]
        names = set(ast.unparse(tree).replace(".", " ").split())
        for forbidden in (
            "generate_change_proof",
            "compute_proof_hash",
            "qualify_impact",
            "adjudicate_field_verification",
            "normalize_observation",
            "issue_grant",
            "transition",
        ):
            assert forbidden not in names, f"{name} re-implements {forbidden!r}"


def test_no_static_credential_or_shell_out_in_the_api_package() -> None:
    for path in sorted((SRC / "driftzero_api").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "subprocess" not in _imported_roots(path), f"{path.name} imports subprocess"
        for marker in ("ya29.", "AIza", "BEGIN PRIVATE KEY", "service_account.json"):
            assert marker not in source, f"{path.name} embeds {marker!r}"
        assert "gcloud " not in source, f"{path.name} appears to shell to gcloud"


def test_the_legacy_project_is_never_named_as_a_target() -> None:
    for path in sorted((SRC / "driftzero_api").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "driftzero-agentic-2026" not in source, path.name


def test_the_api_module_imports_without_any_cloud_extra() -> None:
    """Importing the app must not require google-cloud-* to be installed."""
    source = (SRC / "driftzero_api" / "runtime.py").read_text(encoding="utf-8")
    module_level: set[str] = set()
    for node in ast.parse(source).body:  # top level only, not nested in a function
        if isinstance(node, ast.Import):
            module_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module.split(".")[0])
    assert "google" not in module_level
    assert "driftzero_cloud" not in module_level, (
        "the durable import must stay inside the branch that needs it"
    )
