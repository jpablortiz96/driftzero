"""T095 — the Pub/Sub push handler.

A push delivery is an ordinary authenticated HTTP POST carrying a base64 payload. These
tests exercise the two properties that matter at that boundary: a message is input and
never authority, and at-least-once delivery must be safe — including across a restart of
the process, which is the case in-memory deduplication silently fails.

Offline throughout. No Pub/Sub client, no model call, no cloud.
"""

from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from driftzero_api.app import create_app
from driftzero_api.pubsub import PUSH_PATH, TRUST_BOUNDARY, EnvelopeRejected, decode_envelope
from driftzero_api.runtime import ApiRuntime
from driftzero_cloud.composition import FirestoreSink
from driftzero_cloud.firestore import FirestorePersistence
from driftzero_console.workflows import FORBIDDEN_FIXTURE_KEYS
from tests.integration._fake_gcp import FakeFirestoreClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"


def hero_payload() -> dict[str, Any]:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def envelope(payload: Any, *, message_id: str = "msg-1", **over: Any) -> dict[str, Any]:
    """A Google Pub/Sub push envelope."""
    raw = payload if isinstance(payload, str | bytes) else json.dumps(payload)
    data = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    message: dict[str, Any] = {
        "data": base64.b64encode(data.encode("utf-8")).decode("ascii"),
        "messageId": message_id,
        "publishTime": "2026-08-27T12:00:00Z",
        "attributes": {"source": "test"},
    }
    message.update(over.pop("message", {}))
    return {
        "message": message,
        "subscription": "projects/driftzero-runtime-2026/subscriptions/test",
        **over,
    }


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(ApiRuntime(fixtures_dir=FIXTURES)))


@pytest.fixture
def durable() -> Any:
    database = FakeFirestoreClient()

    def build() -> TestClient:
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

    return {"database": database, "build": build}


# ============================ the happy path ==========================================


def test_a_valid_push_is_ingested(client: TestClient) -> None:
    response = client.post(PUSH_PATH, json=envelope(hero_payload()))
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["outcome"] == "NEW_LOGICAL_CHANGE"
    assert body["workflow_id"]
    assert body["message_id"] == "msg-1"


def test_the_ingested_workflow_is_then_readable_over_the_api(client: TestClient) -> None:
    workflow_id = client.post(PUSH_PATH, json=envelope(hero_payload())).json()["workflow_id"]
    status = client.get(f"/api/v1/workflows/{workflow_id}")
    assert status.status_code == 200
    assert status.json()["state"] == "CHANGE_RECEIVED"


def test_message_metadata_is_carried_but_never_becomes_input(client: TestClient) -> None:
    decoded = decode_envelope(envelope(hero_payload(), message_id="msg-42"))
    assert decoded["message_id"] == "msg-42"
    assert decoded["attributes"] == {"source": "test"}
    assert decoded["subscription"].startswith("projects/driftzero-runtime-2026/")
    assert "attributes" not in decoded["payload"]
    assert "messageId" not in decoded["payload"]


# ============================ envelope validation =====================================


def test_a_missing_message_is_refused() -> None:
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"subscription": "projects/x/subscriptions/y"})
    assert exc.value.reason == "MISSING_MESSAGE"


def test_a_non_object_envelope_is_refused() -> None:
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope(["not", "an", "envelope"])
    assert exc.value.reason == "MALFORMED_ENVELOPE"


def test_missing_data_is_refused() -> None:
    for message in ({}, {"data": ""}, {"data": None}):
        with pytest.raises(EnvelopeRejected) as exc:
            decode_envelope({"message": message})
        assert exc.value.reason == "MISSING_DATA"


def test_non_string_data_is_refused() -> None:
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": {"nested": "object"}}})
    assert exc.value.reason == "MALFORMED_DATA"


@pytest.mark.parametrize(
    "data", ["not base64 at all!", "###", "YWJj=====", "SGVsbG8gV29ybGQ", "  "]
)
def test_malformed_base64_is_refused(data: str) -> None:
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": data}})
    assert exc.value.reason in {"MALFORMED_BASE64", "MALFORMED_JSON", "MALFORMED_UTF8"}


def test_base64_that_decodes_to_malformed_json_is_refused() -> None:
    data = base64.b64encode(b"{not json at all").decode()
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": data}})
    assert exc.value.reason == "MALFORMED_JSON"


def test_base64_that_decodes_to_invalid_utf8_is_refused() -> None:
    data = base64.b64encode(b"\xff\xfe\x00 invalid").decode()
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": data}})
    assert exc.value.reason == "MALFORMED_UTF8"


def test_a_payload_that_is_not_an_object_is_refused() -> None:
    data = base64.b64encode(json.dumps(["a", "list"]).encode()).decode()
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": data}})
    assert exc.value.reason == "MALFORMED_PAYLOAD"


def test_a_payload_without_a_change_id_is_refused() -> None:
    payload = hero_payload()
    del payload["change_id"]
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": data}})
    assert exc.value.reason == "MISSING_CHANGE_ID"


def test_nothing_manufactures_a_valid_change_from_an_invalid_message(
    client: TestClient,
) -> None:
    """A refused message must leave no workflow behind."""
    before = client.post(PUSH_PATH, json={"message": {"data": "###"}})
    assert before.json()["rejected"] is True
    # No workflow was created, so no id could possibly resolve.
    assert client.get("/api/v1/workflows/wf-api-001-001").status_code == 404


# ============================ event authority =========================================


@pytest.mark.parametrize("field", sorted(FORBIDDEN_FIXTURE_KEYS))
def test_no_conclusion_can_arrive_by_pubsub(field: str) -> None:
    """The same refusal list as HTTP. A different transport is not a different trust."""
    payload = hero_payload()
    payload[field] = "PASS"
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(EnvelopeRejected) as exc:
        decode_envelope({"message": {"data": data}})
    assert exc.value.reason == "AUTHORITATIVE_FIELD_REFUSED"
    assert field in exc.value.detail


def test_an_injected_verdict_is_refused_over_the_wire(client: TestClient) -> None:
    payload = hero_payload()
    payload["verification_result"] = "PASS"
    payload["workflow_state"] = "PROOF_COMPLETE"
    response = client.post(PUSH_PATH, json=envelope(payload))
    assert response.json()["error"] == "AUTHORITATIVE_FIELD_REFUSED"
    assert response.json()["accepted"] is False


def test_an_injected_target_is_refused_over_the_wire(client: TestClient) -> None:
    payload = hero_payload()
    payload["affected_artifact_id"] = "wi-forklift-turn-014"
    response = client.post(PUSH_PATH, json=envelope(payload))
    assert response.json()["error"] == "AUTHORITATIVE_FIELD_REFUSED"


# ============================ duplicate delivery ======================================


def test_a_duplicate_delivery_resolves_to_the_same_workflow(client: TestClient) -> None:
    """Pub/Sub is at-least-once. The same message twice is one logical ingestion."""
    first = client.post(PUSH_PATH, json=envelope(hero_payload(), message_id="m-1")).json()
    second = client.post(PUSH_PATH, json=envelope(hero_payload(), message_id="m-1")).json()

    assert second["workflow_id"] == first["workflow_id"]
    assert second["outcome"] == "TRANSPORT_DUPLICATE"
    assert second["duplicate_of"] == first["workflow_id"]


def test_deduplication_is_on_change_id_not_message_id(client: TestClient) -> None:
    """Pub/Sub can redeliver the same change under a new messageId."""
    first = client.post(PUSH_PATH, json=envelope(hero_payload(), message_id="m-1")).json()
    second = client.post(PUSH_PATH, json=envelope(hero_payload(), message_id="m-999")).json()

    assert second["workflow_id"] == first["workflow_id"]
    assert second["outcome"] == "TRANSPORT_DUPLICATE"


def test_a_duplicate_creates_no_second_workflow(client: TestClient) -> None:
    for _ in range(5):
        client.post(PUSH_PATH, json=envelope(hero_payload()))
    runtime: ApiRuntime = client.app.state.runtime
    assert len(runtime.registry) == 1, "at-least-once delivery fanned out into workflows"


def test_a_duplicate_is_idempotent_across_a_restart(durable: Any) -> None:
    """The property in-memory deduplication silently fails.

    The second delivery arrives at a process that has never seen the change before. Only
    the durable claim can tell it this is a redelivery rather than a new change.
    """
    first = durable["build"]().post(PUSH_PATH, json=envelope(hero_payload())).json()
    assert first["outcome"] == "NEW_LOGICAL_CHANGE"

    restarted = durable["build"]()
    second = restarted.post(PUSH_PATH, json=envelope(hero_payload())).json()

    assert second["outcome"] == "TRANSPORT_DUPLICATE"
    assert second["workflow_id"] == first["workflow_id"]
    assert len(restarted.app.state.runtime.registry) == 0, (
        "the restarted process re-created a workflow instead of recognising a duplicate"
    )


def test_the_durable_claim_is_what_survives_the_restart(durable: Any) -> None:
    durable["build"]().post(PUSH_PATH, json=envelope(hero_payload()))
    keys = [path for path in durable["database"].documents if "idempotency_keys/" in path]
    assert keys, "no durable change claim was written"
    assert any("change-" in key for key in keys)


def test_deduplication_does_not_rely_on_memory_alone() -> None:
    """The in-process map must be a cache in front of the durable claim, not the truth."""
    source = (SRC / "driftzero_api" / "runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    classify = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "classify"
    )
    body = ast.unparse(classify)
    assert "_durable_owner" in body, "classify never consults durable storage"
    assert "classify_change_event" in body, "the decision must stay in the Truth Engine"


# ============================ ack / retry semantics ===================================


def test_a_valid_message_is_acked(client: TestClient) -> None:
    assert client.post(PUSH_PATH, json=envelope(hero_payload())).status_code == 200


def test_a_duplicate_is_acked(client: TestClient) -> None:
    client.post(PUSH_PATH, json=envelope(hero_payload()))
    assert client.post(PUSH_PATH, json=envelope(hero_payload())).status_code == 200


def test_a_permanently_invalid_message_is_acked_with_an_explicit_rejection(
    client: TestClient,
) -> None:
    """Pub/Sub retries every non-2xx and no dead-letter topic exists yet.

    Acking stops an infinite redelivery loop for a message that can never succeed. The
    rejection is explicit in the body rather than silently swallowed, and the note says
    when this should change.
    """
    response = client.post(PUSH_PATH, json={"message": {"data": "###"}})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert body["rejected"] is True
    assert body["retryable"] is False
    assert "dead-letter" in body["note"]


def test_a_transient_failure_is_not_acked(client: TestClient) -> None:
    """A persistence failure must be retried, never reported as success."""

    class ExplodingRuntime(ApiRuntime):
        def accept_change(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise ConnectionError("firestore unavailable")

    failing = TestClient(create_app(ExplodingRuntime(fixtures_dir=FIXTURES)))
    response = failing.post(PUSH_PATH, json=envelope(hero_payload()))

    assert response.status_code == 503, "a transient failure must permit Pub/Sub retry"
    assert response.json()["retryable"] is True
    assert response.json()["accepted"] is False


def test_a_transient_failure_is_never_reported_as_success() -> None:
    """Guard against the shortcut of returning 200 to make a test green."""
    source = (SRC / "driftzero_api" / "pubsub.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    push = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "push"
    )
    body = ast.unparse(push)
    assert "HTTP_503_SERVICE_UNAVAILABLE" in body
    assert "TRANSIENT_FAILURE" in body


def test_malformed_body_json_is_acked_not_retried(client: TestClient) -> None:
    response = client.post(
        PUSH_PATH, content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json()["error"] == "MALFORMED_JSON"
    assert response.json()["retryable"] is False


# ============================ authentication boundary =================================


def test_the_trust_boundary_is_documented() -> None:
    assert "Cloud Run" in TRUST_BOUNDARY
    assert "push-auth-service-account" in TRUST_BOUNDARY
    assert "OIDC" in TRUST_BOUNDARY
    assert "run.invoker" in TRUST_BOUNDARY


def test_the_handler_does_not_duplicate_cloud_runs_authentication() -> None:
    """Re-implementing OIDC validation here would be a second, weaker check."""
    source = (SRC / "driftzero_api" / "pubsub.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            node.body = [
                n
                for n in node.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ] or [ast.Pass()]
    code = ast.unparse(tree)
    for forbidden in ("verify_oauth2_token", "id_token", "jwt", "decode_token"):
        assert forbidden not in code, f"pubsub.py re-implements auth via {forbidden!r}"


def test_no_cloud_run_url_is_guessed() -> None:
    source = (SRC / "driftzero_api" / "pubsub.py").read_text(encoding="utf-8")
    assert ".run.app" not in source, "a Cloud Run URL was guessed before T096"
    assert PUSH_PATH == "/api/v1/pubsub/push"


def test_no_pubsub_subscription_is_created_by_the_application() -> None:
    for path in sorted((SRC / "driftzero_api").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("create_subscription", "SubscriberClient", "create_topic"):
            assert forbidden not in source, f"{path.name} provisions Pub/Sub"


# ============================ boundaries ==============================================


def test_the_handler_imports_no_pubsub_client_sdk() -> None:
    """A push delivery is plain HTTP; the client library has no place in the path."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse((SRC / "driftzero_api" / "pubsub.py").read_text("utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert "google" not in roots


def test_no_pubsub_import_inside_the_purity_boundary() -> None:
    offenders = []
    for path in sorted((SRC / "driftzero").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            if "google" in names or "fastapi" in names:
                offenders.append(path.name)
    assert offenders == []


def test_the_legacy_project_is_never_a_target() -> None:
    for path in sorted((SRC / "driftzero_api").glob("*.py")):
        assert "driftzero-agentic-2026" not in path.read_text(encoding="utf-8"), path.name
