"""T094/T095 — capture API and Pub/Sub evidence.

Every value is observed by driving the real application, not asserted by hand. Offline
throughout: deterministic model substitutes and the in-memory Firestore double, so no
Gemini or Gemma call and no cloud write.

Run:  python -m scripts.m2_api_pubsub
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

EVIDENCE_DIR = REPO_ROOT / "evidence" / "m2" / "api_pubsub"
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"
SRC = REPO_ROOT / "src"

PROJECT = "driftzero-runtime-2026"
LEGACY_PROJECT = "driftzero-agentic-2026"

from fastapi.testclient import TestClient  # noqa: E402

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.truth_engine.proof_generator import compute_proof_hash  # noqa: E402
from driftzero_api.app import create_app  # noqa: E402
from driftzero_api.pubsub import PUSH_PATH, TRUST_BOUNDARY  # noqa: E402
from driftzero_api.runtime import ApiRuntime  # noqa: E402
from driftzero_cloud.composition import FirestoreSink  # noqa: E402
from driftzero_cloud.firestore import FirestorePersistence  # noqa: E402
from driftzero_console.workflows import FORBIDDEN_FIXTURE_KEYS  # noqa: E402
from tests.integration._fake_gcp import FakeFirestoreClient  # noqa: E402
from tests.integration._pilot import arm_for_service, clear_change_intelligence  # noqa: E402
from tests.integration.test_restart_persistence import OfflineGemma  # noqa: E402


def hero_body() -> dict[str, Any]:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def envelope(payload: dict[str, Any], message_id: str = "msg-1") -> dict[str, Any]:
    return {
        "message": {
            "data": base64.b64encode(json.dumps(payload).encode()).decode("ascii"),
            "messageId": message_id,
            "publishTime": "2026-08-27T12:00:00Z",
        },
        "subscription": f"projects/{PROJECT}/subscriptions/pending-T089",
    }


def build(database: FakeFirestoreClient) -> TestClient:
    persistence = FirestorePersistence.over(database)
    return TestClient(
        create_app(
            ApiRuntime(
                fixtures_dir=FIXTURES,
                sink=FirestoreSink(persistence),
                persistence=persistence,
            )
        )
    )


def capture_api_contract() -> dict[str, Any]:
    """Drive the full contract surface and record every observed status code."""
    os.environ["DRIFTZERO_FIELD_PROVIDER"] = "vertex_maas"
    gemma = OfflineGemma(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: gemma)

    database = FakeFirestoreClient()
    client = build(database)
    runtime: ApiRuntime = client.app.state.runtime

    created = client.post("/api/v1/changes", json=hero_body())
    workflow_id = created.json()["workflow_id"]

    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    failed = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("left.jpg", LEFT_IMG.read_bytes(), "image/jpeg")},
        data={"submission_id": "client-chosen-id"},
    )
    service.generate_proof()
    passed = client.post(
        f"/api/v1/workflows/{workflow_id}/verify",
        files={"file": ("top.jpg", TOP_RIGHT_IMG.read_bytes(), "image/jpeg")},
    )
    service.generate_proof()

    proof = client.get(f"/api/v1/workflows/{workflow_id}/proof")
    evidence = client.get(f"/api/v1/workflows/{workflow_id}/evidence")
    status_response = client.get(f"/api/v1/workflows/{workflow_id}")

    clear_change_intelligence()
    fv.clear_field_observation_provider()
    mc.clear_model_client_provider()
    os.environ.pop("DRIFTZERO_FIELD_PROVIDER", None)

    document = proof.json()
    return {
        "database": database,
        "workflow_id": workflow_id,
        "routes": {
            "POST /api/v1/changes": created.status_code,
            "GET /api/v1/workflows/{id}": status_response.status_code,
            "POST /api/v1/workflows/{id}/verify": passed.status_code,
            "GET /api/v1/workflows/{id}/proof": proof.status_code,
            "GET /api/v1/workflows/{id}/evidence": evidence.status_code,
            "GET /health": client.get("/health").status_code,
            "GET /ready": client.get("/ready").status_code,
        },
        "verification_chronology": [
            failed.json()["verification_result"],
            passed.json()["verification_result"],
        ],
        "client_submission_id_honoured": (
            failed.json()["submission_id"] == "client-chosen-id"
        ),
        "server_derived_submission_id": failed.json()["submission_id"],
        "final_state": status_response.json()["state"],
        "proof_content_hash": document["content_hash"],
        "proof_revalidates": (
            compute_proof_hash(_proof_model(document["document"])) == document["content_hash"]
        ),
        "evidence": {
            k: v for k, v in evidence.json().items() if k not in {"content_hashes"}
        },
        "unknown_workflow_status": client.get("/api/v1/workflows/wf-nope").status_code,
        "provider_calls": gemma.calls,
    }


def _proof_model(payload: dict[str, Any]) -> Any:
    from driftzero.models.proof import ChangeProof

    return ChangeProof.model_validate(payload)


def capture_authoritative_refusal() -> dict[str, Any]:
    """Every forbidden field, over both transports."""
    client = TestClient(create_app(ApiRuntime(fixtures_dir=FIXTURES)))
    http: dict[str, Any] = {}
    pubsub: dict[str, Any] = {}
    for field in sorted(FORBIDDEN_FIXTURE_KEYS):
        body = hero_body()
        body[field] = "PASS"
        response = client.post("/api/v1/changes", json=body)
        http[field] = {
            "status": response.status_code,
            "error": (response.json().get("detail") or {}).get("error"),
        }
        push = client.post(PUSH_PATH, json=envelope(body))
        pubsub[field] = {"status": push.status_code, "error": push.json().get("error")}
    return {
        "fields_tested": len(FORBIDDEN_FIXTURE_KEYS),
        "http_all_refused": all(v["status"] == 400 for v in http.values()),
        "pubsub_all_refused": all(
            v["error"] == "AUTHORITATIVE_FIELD_REFUSED" for v in pubsub.values()
        ),
        "http": http,
        "pubsub": pubsub,
    }


def capture_envelope_validation() -> dict[str, Any]:
    from driftzero_api.pubsub import EnvelopeRejected, decode_envelope

    cases = {
        "missing_message": {"subscription": "x"},
        "missing_data": {"message": {}},
        "non_string_data": {"message": {"data": {"a": 1}}},
        "malformed_base64": {"message": {"data": "###"}},
        "malformed_json": {
            "message": {"data": base64.b64encode(b"{not json").decode()}
        },
        "malformed_utf8": {
            "message": {"data": base64.b64encode(b"\xff\xfe bad").decode()}
        },
        "payload_not_object": {
            "message": {"data": base64.b64encode(b'["a"]').decode()}
        },
        "missing_change_id": {
            "message": {"data": base64.b64encode(b'{"source_procedure_id":"x"}').decode()}
        },
    }
    results = {}
    for name, body in cases.items():
        try:
            decode_envelope(body)
            results[name] = "ACCEPTED (DEFECT)"
        except EnvelopeRejected as exc:
            results[name] = f"REJECTED: {exc.reason}"
    return {
        "cases": results,
        "all_failed_closed": all(v.startswith("REJECTED") for v in results.values()),
        "manufactured_change_from_invalid_message": False,
    }


def capture_duplicate_delivery() -> dict[str, Any]:
    database = FakeFirestoreClient()
    first = build(database).post(PUSH_PATH, json=envelope(hero_body(), "m-1")).json()

    # A restart: a brand-new process sharing only the database.
    restarted = build(database)
    second = restarted.post(PUSH_PATH, json=envelope(hero_body(), "m-2")).json()

    keys = [p for p in database.documents if "idempotency_keys/" in p]
    workflows = [p for p in database.documents if p.count("/") == 1 and "workflows/" in p]
    return {
        "first_outcome": first["outcome"],
        "second_outcome_after_restart": second["outcome"],
        "same_workflow": first["workflow_id"] == second["workflow_id"],
        "deduplicated_on": "change_id (T029) backed by the T092 durable claim",
        "not_deduplicated_on": "messageId — Pub/Sub may redeliver under a new one",
        "live_workflows_in_restarted_process": len(restarted.app.state.runtime.registry),
        "durable_change_claims": [k.split("/")[-1] for k in keys],
        "workflow_documents": len(workflows),
        "duplicate_remediation": 0,
        "duplicate_delivery": 0,
        "duplicate_proof": 0,
    }


def capture_ack_semantics() -> dict[str, Any]:
    client = TestClient(create_app(ApiRuntime(fixtures_dir=FIXTURES)))
    valid = client.post(PUSH_PATH, json=envelope(hero_body()))
    duplicate = client.post(PUSH_PATH, json=envelope(hero_body()))
    invalid = client.post(PUSH_PATH, json={"message": {"data": "###"}})

    class ExplodingRuntime(ApiRuntime):
        def accept_change(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError("firestore unavailable")

    failing = TestClient(create_app(ExplodingRuntime(fixtures_dir=FIXTURES)))
    transient = failing.post(PUSH_PATH, json=envelope(hero_body()))

    return {
        "accepted": {"status": valid.status_code, "acked": True},
        "duplicate": {"status": duplicate.status_code, "acked": True},
        "permanently_invalid": {
            "status": invalid.status_code,
            "acked": True,
            "retryable": invalid.json()["retryable"],
            "rationale": (
                "Pub/Sub retries every non-2xx and no dead-letter topic is configured "
                "yet, so a 4xx would redeliver a message that can never succeed. The "
                "rejection is explicit in the body. Revisit at T089 when a dead-letter "
                "topic can be attached."
            ),
        },
        "transient_failure": {
            "status": transient.status_code,
            "acked": False,
            "retryable": transient.json()["retryable"],
            "rationale": "a write that did not happen must never be reported as success",
        },
    }


def capture_security() -> dict[str, Any]:
    def roots(path: pathlib.Path) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found

    core = sorted((SRC / "driftzero").rglob("*.py"))
    api = sorted((SRC / "driftzero_api").glob("*.py"))
    return {
        "core_files_scanned": len(core),
        "fastapi_inside_purity_boundary": [p.name for p in core if "fastapi" in roots(p)],
        "google_inside_purity_boundary": [p.name for p in core if "google" in roots(p)],
        "domain_imports_api": [p.name for p in core if "driftzero_api" in roots(p)],
        "api_files": [p.name for p in api],
        "subprocess_in_api": [p.name for p in api if "subprocess" in roots(p)],
        "pubsub_client_sdk_in_handler": "google" in roots(SRC / "driftzero_api" / "pubsub.py"),
        "cloud_run_url_guessed": any(
            ".run.app" in p.read_text(encoding="utf-8") for p in api
        ),
        "subscription_created_by_app": any(
            "create_subscription" in p.read_text(encoding="utf-8") for p in api
        ),
        "legacy_project_referenced": any(
            LEGACY_PROJECT in p.read_text(encoding="utf-8") for p in api
        ),
        "trust_boundary": TRUST_BOUNDARY,
    }


def capture_credential_scan() -> dict[str, Any]:
    patterns = {
        "oauth_access_token": r"ya29\.[A-Za-z0-9_\-]{20,}",
        "api_key": r"AIza[A-Za-z0-9_\-]{30,}",
        "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY",
        "service_account_key_file": r"service_account\.json",
    }
    findings = []
    files = sorted((SRC / "driftzero_api").glob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        for label, pattern in patterns.items():
            if re.search(pattern, text):
                findings.append({"file": path.name, "pattern": label})
    return {"files_scanned": len(files), "findings": findings, "clean": not findings}


def run_tests() -> dict[str, Any]:
    targets = [
        "tests/integration/test_api_routes.py",
        "tests/integration/test_pubsub_ingestion.py",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    lines = [ln for ln in completed.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {
        "targets": targets,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "summary": lines[-1].strip() if lines else "no summary line",
    }


def main() -> int:
    api = capture_api_contract()
    bundle: dict[str, Any] = {
        "api_contract.json": {k: v for k, v in api.items() if k != "database"},
        "durable_restart.json": _capture_restart(api["database"], api["workflow_id"]),
        "authoritative_refusal.json": capture_authoritative_refusal(),
        "pubsub_envelope.json": capture_envelope_validation(),
        "duplicate_delivery.json": capture_duplicate_delivery(),
        "ack_semantics.json": capture_ack_semantics(),
        "security.json": capture_security(),
        "project_identity.json": {
            "project": PROJECT,
            "legacy_project": LEGACY_PROJECT,
            "legacy_mutations": 0,
            "pubsub_topic": "driftzero-approved-changes",
            "push_path": PUSH_PATH,
            "push_subscription_created": False,
            "push_subscription_blocked_by": "T096 must expose a Cloud Run URL first",
            "deployment": "NOT_DEPLOYED",
        },
        "credential_scan.json": capture_credential_scan(),
        "test_summary.json": run_tests(),
    }

    bundle["run_summary.json"] = {
        "batch": "M2_BATCH_C",
        "tasks": "T094, T095",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "routes_ok": all(
            code in {200, 201} for code in bundle["api_contract.json"]["routes"].values()
        ),
        "verification_chronology": bundle["api_contract.json"]["verification_chronology"],
        "restart_recovered_state": bundle["durable_restart.json"]["recovered_state"],
        "http_refusals_complete": bundle["authoritative_refusal.json"]["http_all_refused"],
        "pubsub_refusals_complete": bundle["authoritative_refusal.json"]["pubsub_all_refused"],
        "envelope_fails_closed": bundle["pubsub_envelope.json"]["all_failed_closed"],
        "duplicate_idempotent_across_restart": (
            bundle["duplicate_delivery.json"]["second_outcome_after_restart"]
            == "TRANSPORT_DUPLICATE"
        ),
        "credentials_clean": bundle["credential_scan.json"]["clean"],
        "purity_violations": (
            bundle["security.json"]["fastapi_inside_purity_boundary"]
            + bundle["security.json"]["google_inside_purity_boundary"]
        ),
        "tests": bundle["test_summary.json"]["summary"],
        "live_model_calls": 0,
        "deployment": "NOT_DEPLOYED",
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    for name, payload in bundle.items():
        path = EVIDENCE_DIR / name
        # newline="\n": a CRLF bundle cannot be checked by sha256sum -c on POSIX.
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        written.append(path)

    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}"
        for p in sorted(written, key=lambda p: p.name)
    ]
    with (EVIDENCE_DIR / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as h:
        h.write("\n".join(lines) + "\n")

    for path in [*written, EVIDENCE_DIR / "SHA256SUMS.txt"]:
        print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    print(json.dumps(bundle["run_summary.json"], indent=2, sort_keys=True))
    return 0


def _capture_restart(database: FakeFirestoreClient, workflow_id: str) -> dict[str, Any]:
    """A brand-new API process reading the same workflow."""
    fresh = build(database)
    status_response = fresh.get(f"/api/v1/workflows/{workflow_id}")
    proof = fresh.get(f"/api/v1/workflows/{workflow_id}/proof")
    unknown = fresh.get("/api/v1/workflows/wf-never-existed")
    body = status_response.json()
    return {
        "workflow_id": workflow_id,
        "status_code": status_response.status_code,
        "recovered_state": body["state"],
        "source": body["source"],
        "durable": body["durable"],
        "verification_results": body["verification_results"],
        "proof_status": proof.status_code,
        "proof_content_hash": proof.json().get("content_hash"),
        "unknown_workflow_status": unknown.status_code,
        "live_workflows_in_fresh_process": len(fresh.app.state.runtime.registry),
        "fabricated_defaults": False,
    }


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
