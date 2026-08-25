"""T080 step 10 — the deterministic expected-vs-observed verdict, wired to the product.

Advances **T080 only in part**: the async pause boundary and step 10. Step 11 (Change
Proof) and the ADK ``SequentialAgent`` are deliberately absent, so a test here asserts
T080 is still open rather than pretending otherwise.

Fully offline. The comparator under test is the frozen T038 one, reached through the
frozen T037 ingestion path; no test in this file calls a live model, and a guard asserts
the provider registry stays empty.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents.field_verify import ProviderObservation  # noqa: E402
from driftzero.agents.orchestrator import (  # noqa: E402
    VERDICT_STATE,
    VerdictContext,
    VerdictOutcome,
    VerdictStatus,
    adjudicate_field_verification,
    authoritative_expected_value,
    change_is_deployed,
    remaining_condition_for,
    verification_history,
)
from driftzero.capabilities import AgentIdentity, ToolCapability, is_authorized  # noqa: E402
from driftzero.field.evidence import FieldEvidenceStore  # noqa: E402
from driftzero.models.change import ApprovedChange  # noqa: E402
from driftzero.models.classification import (  # noqa: E402
    ClassificationLabel,
    DataClassification,
)
from driftzero.models.verification import (  # noqa: E402
    FieldObservation,
    ObservedPosition,
    VerificationResult,
)
from driftzero.models.workflow import Workflow, WorkflowState  # noqa: E402
from driftzero.orchestration import (  # noqa: E402
    ObservationBoundaryResult,
    ObservationCrossingContext,
    accept_field_observation,
)
from driftzero.truth_engine import verification as frozen_verification  # noqa: E402
from driftzero_console import app as app_module  # noqa: E402
from driftzero_console.service import ChangeCase, HeroConsoleService  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures" / "multimodal"
TOP_RIGHT_IMG = FIXTURES / "label_top_right_01.jpg"
LEFT_IMG = FIXTURES / "label_left_01.jpg"
AMBIGUOUS_IMG = FIXTURES / "label_ambiguous_01.jpg"

CHANGE_ID = "DZ-001"
SOURCE_VERSION = "v14"
WORKFLOW_ID = "wf-verdict-001"
MOMENT = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

TORQUE_CASE = ChangeCase(
    change_id="DZ-114",
    source_name="Assembly Standard",
    source_procedure_id="ASSY-STD",
    operation_id="OP-ASSY-04",
    previous_version="r7",
    source_version="r8",
    requirement_id="torque_spec",
    previous_value="12 Nm",
    current_value="18 Nm",
    artifact_id="WI-880",
    artifact_type="work_instruction",
    requirements={"torque_spec": "12 Nm", "fixture": "J-14"},
    source_evidence_ref="local://changes/DZ-114",
)


# ============================ offline harness =========================================


class StubProvider:
    """Returns a fixed raw output. No network, no credentials, no cost."""

    name = "stub_provider"

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0

    def observe(self, **_kwargs: Any) -> ProviderObservation:
        self.calls += 1
        return ProviderObservation(
            raw_output=self.output,
            provider=self.name,
            model="stub/gemma",
            response_id=f"resp-{self.calls}",
            finish_reason="stop",
            total_tokens=343,
            traffic_type="ON_DEMAND",
        )


@pytest.fixture(autouse=True)
def _isolate_provider():  # type: ignore[no-untyped-def]
    fv.clear_field_observation_provider()
    yield
    fv.clear_field_observation_provider()


def classification() -> DataClassification:
    return DataClassification(labels=[ClassificationLabel.SYNTHETIC])


def make_change(**over: object) -> ApprovedChange:
    defaults: dict[str, object] = {
        "change_id": CHANGE_ID,
        "source_procedure_id": "PACKING-SOP",
        "source_version": SOURCE_VERSION,
        "previous_version": "v13",
        "operation_id": "OP-PACK-01",
        "requirement_id": "label_position",
        "previous_value": "LEFT",
        "current_value": "TOP_RIGHT",
        "authorized_scope": ["WI-114"],
        "approved_status": "APPROVED",
        "source_evidence_ref": "local://changes/DZ-001",
        "received_at": MOMENT,
        "data_classification": classification(),
    }
    defaults.update(over)
    return ApprovedChange(**defaults)  # type: ignore[arg-type]


def make_workflow(
    state: WorkflowState = WorkflowState.AWAITING_FIELD_VERIFICATION, **over: object
) -> Workflow:
    defaults: dict[str, object] = {
        "workflow_id": WORKFLOW_ID,
        "change_id": CHANGE_ID,
        "source_version": SOURCE_VERSION,
        "state": state,
        "affected_artifact_id": "WI-114",
        "worker_id": "frontline:pilot-surface",
        "created_at": MOMENT,
        "updated_at": MOMENT,
        "data_classification": classification(),
    }
    defaults.update(over)
    return Workflow(**defaults)  # type: ignore[arg-type]


def seed_evidence(
    store: FieldEvidenceStore,
    observed: str,
    *,
    operation_id: str = "obs-001",
    change_id: str = CHANGE_ID,
    source_version: str = SOURCE_VERSION,
    image_sha256: str = "a" * 64,
    submission_id: str = "fev-001",
    identity: str = str(AgentIdentity.FIELD_VERIFICATION),
) -> str:
    """Record provider evidence exactly as the T079 agent would."""
    return store.record(
        operation_id=operation_id,
        document={
            "operation_id": operation_id,
            "change_id": change_id,
            "source_version": source_version,
            "submission_id": submission_id,
            "identity": identity,
            "image_sha256": image_sha256,
            "mime_type": "image/heic",
            "model": "stub/gemma",
            "provider": "stub_provider",
            "normalized_observation": observed,
            "normalization_succeeded": True,
            "attempt_count": 1,
        },
        recorded_at=MOMENT,
    )


def accepted_boundary(
    store: FieldEvidenceStore,
    observed: str,
    *,
    submission_id: str = "fev-001",
    image_sha256: str = "a" * 64,
    **seed_over: object,
) -> tuple[ObservationBoundaryResult, FieldObservation]:
    """Produce a genuinely Crossing-4-accepted observation. Never a hand-built one."""
    ref = seed_evidence(
        store,
        observed,
        submission_id=submission_id,
        image_sha256=image_sha256,
        **seed_over,  # type: ignore[arg-type]
    )
    observation = FieldObservation(
        submission_id=submission_id,
        raw_evidence_ref=ref,
        observed_label_position=ObservedPosition(observed),
    )
    boundary = accept_field_observation(
        observation,
        context=ObservationCrossingContext(
            store=store,
            expected_change_id=seed_over.get("change_id", CHANGE_ID),  # type: ignore[arg-type]
            expected_source_version=seed_over.get("source_version", SOURCE_VERSION),  # type: ignore[arg-type]
            expected_submission_id=submission_id,
            expected_image_sha256=image_sha256,
            authorized_identity=str(AgentIdentity.FIELD_VERIFICATION),
            rejection_ref="rej-001",
        ),
    )
    assert boundary.accepted, boundary.rejection_reason
    return boundary, observation


def adjudicate(
    boundary: ObservationBoundaryResult,
    *,
    workflow: Workflow | None = None,
    change: ApprovedChange | None = None,
    store: FieldEvidenceStore,
    events: tuple[Any, ...] = (),
    event_id: str = "vev-001",
) -> VerdictOutcome:
    return adjudicate_field_verification(
        VerdictContext(
            workflow=workflow or make_workflow(),
            change=change or make_change(),
            boundary=boundary,
            store=store,
            event_id=event_id,
            occurred_at=MOMENT,
            data_classification=classification(),
            existing_events=events,
        )
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "driftzero-runtime-2026")
    service = HeroConsoleService()
    monkeypatch.setattr(app_module, "_service", service)
    with TestClient(app_module.app) as test_client:
        yield test_client


def wire(output: str) -> StubProvider:
    provider = StubProvider(output)
    fv.register_field_observation_provider(lambda _c: provider)
    return provider


def run_to_verdict(client: TestClient, image: Path) -> dict[str, Any]:
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    return client.post("/api/hero/field-evidence", content=image.read_bytes()).json()


# ============================ 1. owning-task semantics ================================


def test_t080_owns_this_wiring_and_remains_open() -> None:
    """Step 10 of T080 is implemented; steps 1-3 and 11 are not, so T080 stays open."""
    tasks = (REPO_ROOT / "specs" / "001-hero-change-deployment" / "tasks.md").read_text(
        encoding="utf-8"
    )
    line = next(raw for raw in tasks.splitlines() if raw.startswith("- [ ] T080"))
    assert "orchestrator.py" in line
    assert "11-step boundary sequence" in line
    assert "async pause after delivery" in line
    assert (REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py").exists()

    contract = (
        REPO_ROOT / "specs" / "001-hero-change-deployment" / "contracts" / "agents.md"
    ).read_text(encoding="utf-8")
    assert "10. Truth Engine: deterministic PASS/FAIL" in contract


def test_step_eleven_change_proof_is_not_built() -> None:
    """No proof is generated here, and nothing in this slice reaches the generator."""
    source = (REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    for banned in ("generate_change_proof", "ChangeProof", "PROOF_COMPLETE ="):
        assert banned not in source
    console = (REPO_ROOT / "src" / "driftzero_console" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "generate_change_proof" not in console


# ============================ 2. the comparator is reused =============================


def test_the_frozen_comparator_is_reused_not_duplicated() -> None:
    """Exactly one comparator exists, and it is the frozen M0 one."""
    src = REPO_ROOT / "src"
    definitions = [
        (path.relative_to(src).as_posix(), node.name)
        for path in sorted(src.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and node.name in {"compare_observation", "ingest_observation"}
    ]
    assert sorted(definitions) == [
        ("driftzero/truth_engine/verification.py", "compare_observation"),
        ("driftzero/truth_engine/verification.py", "ingest_observation"),
    ]


def test_step_ten_contains_no_comparison_of_its_own() -> None:
    """The orchestrator binds inputs; it never decides. No expected/observed compare."""
    path = REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    body = functions["adjudicate_field_verification"]
    for node in ast.walk(body):
        if isinstance(node, ast.Compare):
            # Guard comparisons against enum members are fine; comparing an expected
            # value against an observed one is not.
            rendered = ast.unparse(node)
            assert "expected" not in rendered or "observed" not in rendered, rendered


def test_the_console_never_compares_expected_against_observed() -> None:
    console = (REPO_ROOT / "src" / "driftzero_console" / "service.py").read_text(
        encoding="utf-8"
    )
    for node in ast.walk(ast.parse(console)):
        if isinstance(node, ast.Compare):
            rendered = ast.unparse(node)
            assert not ("expected_value" in rendered and "observ" in rendered), rendered


# ============================ 3-6. the closed verdict domain ==========================


def test_matching_observation_yields_passed() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT")
    outcome = adjudicate(boundary, store=store)
    assert outcome.status is VerdictStatus.ADJUDICATED
    assert outcome.result is VerificationResult.PASS
    assert outcome.workflow.state is WorkflowState.VERIFICATION_PASSED
    assert outcome.expected_value == "TOP_RIGHT"
    assert outcome.observed_value == "TOP_RIGHT"


def test_differing_observation_yields_failed() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "LEFT")
    outcome = adjudicate(boundary, store=store)
    assert outcome.result is VerificationResult.FAIL
    assert outcome.workflow.state is WorkflowState.VERIFICATION_FAILED
    assert outcome.expected_value == "TOP_RIGHT"
    assert outcome.observed_value == "LEFT"


def test_inconclusive_is_never_folded_into_failure() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "INCONCLUSIVE")
    outcome = adjudicate(boundary, store=store)
    assert outcome.result is VerificationResult.INCONCLUSIVE
    assert outcome.result is not VerificationResult.FAIL
    assert outcome.workflow.state is WorkflowState.VERIFICATION_INCONCLUSIVE


def test_the_verdict_state_map_covers_every_result_with_no_default() -> None:
    assert set(VERDICT_STATE) == set(VerificationResult)
    assert set(VERDICT_STATE.values()) == {
        WorkflowState.VERIFICATION_PASSED,
        WorkflowState.VERIFICATION_FAILED,
        WorkflowState.VERIFICATION_INCONCLUSIVE,
    }


@pytest.mark.parametrize(
    "malformed", ["PASS", "FAIL", "RIGHT", "probably left", "", "0.92", None, 7]
)
def test_a_malformed_observation_cannot_reach_the_comparator(malformed: object) -> None:
    """Rejected at construction, so it never becomes an observation to adjudicate."""
    with pytest.raises(ValidationError):
        FieldObservation(
            submission_id="fev-x",
            raw_evidence_ref="field-evidence:obs-x",
            observed_label_position=malformed,  # type: ignore[arg-type]
        )
    with pytest.raises(frozen_verification.UnnormalizedObservationError):
        frozen_verification.compare_observation("TOP_RIGHT", malformed)


# ============================ 7-8. authoritative inputs ===============================


def test_expected_value_comes_from_the_approved_change() -> None:
    change = make_change(current_value="TOP_RIGHT")
    assert authoritative_expected_value(change) == "TOP_RIGHT"
    assert authoritative_expected_value(make_change(current_value="LEFT")) == "LEFT"


def test_the_verdict_context_carries_no_expected_value_or_verdict() -> None:
    """A caller supplies inputs to adjudication and structurally cannot supply an answer."""
    fields = set(VerdictContext.__dataclass_fields__)
    for banned in (
        "expected_value",
        "expected",
        "result",
        "verdict",
        "verification_result",
        "observation",
        "observed_value",
        "passed",
    ):
        assert banned not in fields


def test_changing_the_approved_value_changes_the_verdict() -> None:
    """Proof the expected side is read from the change, not from a constant."""
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT")
    assert adjudicate(boundary, store=store).result is VerificationResult.PASS

    store2 = FieldEvidenceStore()
    boundary2, _ = accepted_boundary(store2, "TOP_RIGHT")
    outcome = adjudicate(
        boundary2, store=store2, change=make_change(current_value="LEFT")
    )
    assert outcome.result is VerificationResult.FAIL
    assert outcome.expected_value == "LEFT"


def test_an_unvalidated_observation_is_refused_before_comparison() -> None:
    """A Crossing-4 rejection is not adjudicable, whatever it claims."""
    store = FieldEvidenceStore()
    forged = FieldObservation(
        submission_id="fev-001",
        raw_evidence_ref="field-evidence:invented",
        observed_label_position=ObservedPosition.TOP_RIGHT,
    )
    rejected = accept_field_observation(
        forged,
        context=ObservationCrossingContext(
            store=store,
            expected_change_id=CHANGE_ID,
            expected_source_version=SOURCE_VERSION,
            expected_submission_id="fev-001",
            expected_image_sha256="a" * 64,
            authorized_identity=str(AgentIdentity.FIELD_VERIFICATION),
            rejection_ref="rej-001",
        ),
    )
    assert rejected.accepted is False
    outcome = adjudicate(rejected, store=store)
    assert outcome.status is VerdictStatus.OBSERVATION_NOT_VALIDATED
    assert outcome.result is None
    assert outcome.workflow is None


def test_evidence_that_stops_resolving_is_refused() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT")
    outcome = adjudicate(boundary, store=FieldEvidenceStore())
    assert outcome.status is VerdictStatus.EVIDENCE_NOT_RESOLVABLE
    assert outcome.result is None


def test_evidence_from_a_different_change_is_refused() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT", change_id="DZ-999")
    outcome = adjudicate(
        boundary,
        store=store,
        change=make_change(change_id="DZ-999", authorized_scope=["WI-114"]),
        workflow=make_workflow(change_id="DZ-999"),
    )
    # Workflow and change agree, but the *console's* change does not — swap it back.
    outcome = adjudicate(boundary, store=store)
    assert outcome.status is VerdictStatus.EVIDENCE_CONTEXT_MISMATCH
    assert outcome.result is None


def test_evidence_from_a_superseded_source_version_is_refused() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT", source_version="v13")
    outcome = adjudicate(boundary, store=store)
    assert outcome.status is VerdictStatus.EVIDENCE_CONTEXT_MISMATCH


def test_a_workflow_not_awaiting_verification_cannot_be_adjudicated() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT")
    outcome = adjudicate(
        boundary, store=store, workflow=make_workflow(WorkflowState.CHANGE_RECEIVED)
    )
    assert outcome.status is VerdictStatus.NOT_AWAITING_VERIFICATION
    assert outcome.result is None


# ============================ 9-12. no model or frontend authority ====================


def test_no_endpoint_accepts_an_expected_value_verdict_or_comparison(
    client: TestClient,
) -> None:
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
        {"X-Expected": "LEFT"},
        {"X-Verdict": "PASS"},
        {"X-Verification-Result": "PASS"},
        {"X-Result": "FAIL"},
        {"X-Comparison": "equal"},
        {"X-Change-Deployed": "true"},
        {"X-Workflow-State": "PROOF_COMPLETE"},
    ],
    ids=lambda h: next(iter(h)),
)
def test_hostile_verdict_headers_are_ignored(
    client: TestClient, hostile: dict[str, str]
) -> None:
    """Observed LEFT against expected TOP_RIGHT must FAIL, whatever the caller claims."""
    wire("LEFT")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    body = client.post(
        "/api/hero/field-evidence", content=LEFT_IMG.read_bytes(), headers=hostile
    ).json()
    verdict = body["verdict"]
    assert verdict["result"] == "FAIL"
    assert verdict["expected_value"] == "TOP_RIGHT"
    assert verdict["workflow_state"] == "VERIFICATION_FAILED"
    assert verdict["change_deployed"] is False


def test_the_field_observation_model_cannot_carry_a_verdict() -> None:
    banned = {
        "verification_result",
        "result",
        "passed",
        "failed",
        "verdict",
        "expected_value",
    }
    assert not banned & set(FieldObservation.model_fields)


def test_the_provider_result_cannot_carry_a_verdict() -> None:
    banned = {"verification_result", "verdict", "passed", "result", "expected_value"}
    assert not banned & set(ProviderObservation.__dataclass_fields__)


def test_a_model_returning_pass_produces_no_verdict_at_all(client: TestClient) -> None:
    """The model saying PASS is out-of-domain output, not an authoritative verdict."""
    wire("PASS")
    body = run_to_verdict(client, TOP_RIGHT_IMG)
    assert body["field_verification"]["status"] == "OUT_OF_DOMAIN"
    assert body["field_verification"]["observation"] is None
    assert body["verdict"]["result"] is None
    assert body["verdict"]["change_verified"] is False


def test_the_orchestrator_holds_no_capability_and_makes_no_model_call() -> None:
    """No capability, no provider, no prompt — matched on real code, not on prose."""
    for tool in ToolCapability:
        assert not is_authorized(AgentIdentity.ORCHESTRATOR, tool)

    path = REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # Drop docstrings: describing what this module must not do is not doing it.
        body = getattr(node, "body", None)
        if isinstance(body, list):
            node.body = [  # type: ignore[attr-defined]
                child
                for child in body
                if not (
                    isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)
                )
            ] or [ast.Pass()]
    code = ast.unparse(tree)

    for banned in (
        "provider",
        "prompt",
        "grant",
        "issue_grant",
        "ToolGrant",
        "ToolCapability",
        "observe(",
        "httpx",
        "google",
    ):
        assert banned not in code, f"the verdict layer references {banned}"


# ============================ 13. idempotency =========================================


def test_replaying_the_same_evidence_creates_no_second_authoritative_event() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT")
    first = adjudicate(boundary, store=store)
    assert first.status is VerdictStatus.ADJUDICATED
    assert first.event.event_sequence == 1

    replay = adjudicate(
        boundary,
        store=store,
        workflow=first.workflow.model_copy(
            update={"state": WorkflowState.AWAITING_FIELD_VERIFICATION}
        ),
        events=(first.event,),
        event_id="vev-002",
    )
    assert replay.status is VerdictStatus.DUPLICATE_SUBMISSION
    assert replay.duplicate is True
    assert replay.event.event_id == first.event.event_id
    assert replay.event.event_sequence == 1, "a replay must not allocate a sequence"
    assert replay.result is first.result


def test_the_same_image_produces_one_verification_event_end_to_end(
    client: TestClient,
) -> None:
    provider = wire("TOP_RIGHT")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    raw = TOP_RIGHT_IMG.read_bytes()
    for _ in range(4):
        body = client.post("/api/hero/field-evidence", content=raw).json()
    assert provider.calls == 1, "T079 replay-cost safety must still hold"
    assert len(body["verdict"]["history"]) == 1
    assert body["verdict"]["result"] == "PASS"


# ============================ 14-16. history is append-only ===========================


def test_a_failed_attempt_is_preserved_when_a_later_one_passes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = iter(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: StubProvider(next(outputs)))
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")

    failed = client.post(
        "/api/hero/field-evidence", content=LEFT_IMG.read_bytes()
    ).json()["verdict"]
    assert failed["result"] == "FAIL"

    passed = client.post(
        "/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes()
    ).json()["verdict"]
    assert passed["result"] == "PASS"
    assert passed["workflow_state"] == "VERIFICATION_PASSED"

    history = passed["history"]
    assert [h["result"] for h in history] == ["FAIL", "PASS"]
    assert [h["event_sequence"] for h in history] == [1, 2]
    # The failed attempt still resolves, unchanged.
    assert history[0]["observed"] == "LEFT"
    assert history[0]["expected"] == "TOP_RIGHT"


def test_an_inconclusive_attempt_is_preserved_when_a_later_one_passes(
    client: TestClient,
) -> None:
    outputs = iter(["INCONCLUSIVE", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: StubProvider(next(outputs)))
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    client.post("/api/hero/field-evidence", content=AMBIGUOUS_IMG.read_bytes())
    verdict = client.post(
        "/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes()
    ).json()["verdict"]
    assert [h["result"] for h in verdict["history"]] == ["INCONCLUSIVE", "PASS"]
    assert verdict["result"] == "PASS"


def test_a_later_pass_supersedes_without_deleting_the_failure(
    client: TestClient,
) -> None:
    """T037 chronology: the newest event wins; the old one is retained, not erased."""
    outputs = iter(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: StubProvider(next(outputs)))
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    client.post("/api/hero/field-evidence", content=LEFT_IMG.read_bytes())
    state = client.post(
        "/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes()
    ).json()

    ids = state["evidence_ids"]
    events = [e for e in ids if e.startswith("verification-event-")]
    assert len(events) == 2, "both attempts must remain independently inspectable"
    documents = [
        client.get(f"/api/hero/evidence/{eid}").json()["document"] for eid in events
    ]
    results = sorted(d["verification_result"] for d in documents)
    assert results == ["FAIL", "PASS"]


def test_verification_history_is_ordered_by_sequence_not_arrival() -> None:
    store = FieldEvidenceStore()
    first, _ = accepted_boundary(store, "LEFT", submission_id="fev-1", operation_id="obs-1")
    outcome1 = adjudicate(first, store=store)
    second, _ = accepted_boundary(
        store, "TOP_RIGHT", submission_id="fev-2", operation_id="obs-2", image_sha256="b" * 64
    )
    outcome2 = adjudicate(
        second,
        store=store,
        workflow=outcome1.workflow.model_copy(
            update={"state": WorkflowState.AWAITING_FIELD_VERIFICATION}
        ),
        events=(outcome1.event,),
        event_id="vev-002",
    )
    history = verification_history([outcome2.event, outcome1.event])
    assert [h["event_sequence"] for h in history] == [1, 2]


# ============================ 17-19. proof and deployment gates =======================


def test_a_pass_does_not_generate_a_change_proof(client: TestClient) -> None:
    wire("TOP_RIGHT")
    body = run_to_verdict(client, TOP_RIGHT_IMG)
    verdict = body["verdict"]
    assert verdict["result"] == "PASS"
    assert verdict["proof_generated"] is False
    assert verdict["workflow_state"] == "VERIFICATION_PASSED"
    assert not any(e.startswith("proof") for e in body["evidence_ids"])


def test_change_deployed_follows_the_frozen_terminal_success_state() -> None:
    """Deployed means PROOF_COMPLETE — the only TERMINAL_SUCCESS state in the model."""
    for state in WorkflowState:
        expected = state is WorkflowState.PROOF_COMPLETE
        assert change_is_deployed(make_workflow(state)) is expected
    assert change_is_deployed(None) is False


def test_a_pass_reports_the_exact_remaining_condition() -> None:
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "TOP_RIGHT")
    outcome = adjudicate(boundary, store=store)
    remaining = remaining_condition_for(outcome.workflow)
    assert "Change Proof generation" in remaining
    assert "VERIFICATION_PASSED -> PROOF_COMPLETE" in remaining
    assert outcome.change_deployed is False


def test_the_verdict_outcome_has_no_settable_deployment_or_proof_field() -> None:
    """``change_deployed`` and ``proof_generated`` are derived, not assignable."""
    fields = set(VerdictOutcome.__dataclass_fields__)
    assert "change_deployed" not in fields
    assert "proof_generated" not in fields
    assert isinstance(VerdictOutcome.change_deployed, property)
    assert isinstance(VerdictOutcome.proof_generated, property)


def test_state_transitions_use_the_frozen_state_machine() -> None:
    orchestrator = (
        REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "from driftzero.truth_engine.state_machine import" in orchestrator
    # No hand-rolled state assignment anywhere.
    assert 'state": WorkflowState' not in orchestrator
    assert "state=WorkflowState" not in orchestrator


def test_the_console_never_forces_an_illegal_transition(client: TestClient) -> None:
    """A verdict before delivery is impossible; the state machine, not the UI, says so."""
    wire("TOP_RIGHT")
    client.post("/api/hero/deploy")
    state = client.get("/api/hero/state").json()
    assert state["verdict"]["workflow_state"] == "REMEDIATION_COMPLETED"
    assert state["verdict"]["result"] is None


# ============================ 20. capability matrix ===================================


def test_the_capability_matrix_is_derived_from_the_policy(client: TestClient) -> None:
    state = client.get("/api/hero/state").json()
    assert state["capability_columns"] == [str(t) for t in ToolCapability]

    matrix = {
        agent["identity"]: {
            c["capability"]: c["permission"] for c in agent["capabilities"]
        }
        for agent in state["fleet"]
    }
    assert matrix == {
        "driftzero-change-intel": {
            "ARTIFACT_MUTATION": "DENIED",
            "FRONTLINE_DELIVERY": "DENIED",
            "FIELD_OBSERVATION": "DENIED",
        },
        "driftzero-remediation": {
            "ARTIFACT_MUTATION": "ALLOWED",
            "FRONTLINE_DELIVERY": "DENIED",
            "FIELD_OBSERVATION": "DENIED",
        },
        "driftzero-enablement": {
            "ARTIFACT_MUTATION": "DENIED",
            "FRONTLINE_DELIVERY": "ALLOWED",
            "FIELD_OBSERVATION": "DENIED",
        },
        "driftzero-field-verify": {
            "ARTIFACT_MUTATION": "DENIED",
            "FRONTLINE_DELIVERY": "DENIED",
            "FIELD_OBSERVATION": "ALLOWED",
        },
    }


def test_every_matrix_cell_agrees_with_is_authorized(client: TestClient) -> None:
    for agent in client.get("/api/hero/state").json()["fleet"]:
        for cell in agent["capabilities"]:
            expected = (
                "ALLOWED"
                if is_authorized(agent["identity"], cell["capability"])
                else "DENIED"
            )
            assert cell["permission"] == expected


def test_no_frontend_asset_hardcodes_the_capability_matrix() -> None:
    static = REPO_ROOT / "src" / "driftzero_console" / "static"
    for path in sorted(static.iterdir()):
        if path.suffix not in {".js", ".html"}:
            continue
        source = path.read_text(encoding="utf-8")
        for capability in ("ARTIFACT_MUTATION", "FRONTLINE_DELIVERY", "FIELD_OBSERVATION"):
            assert capability not in source, f"{path.name} hardcodes {capability}"
        for identity in ("driftzero-remediation", "driftzero-enablement"):
            assert identity not in source, f"{path.name} hardcodes {identity}"


# ============================ product UX ==============================================


def test_the_change_status_header_is_not_a_completion_fraction() -> None:
    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "COMPLETE`" not in app_js, "a fraction reads as near-deployment"
    assert "changeStatus()" in app_js
    for label in (
        "AWAITING FIELD EVIDENCE",
        "VERIFICATION PASSED",
        "VERIFICATION FAILED",
        "MORE EVIDENCE REQUIRED",
    ):
        assert label in app_js


def test_the_ui_labels_observation_and_verdict_with_different_authorities(
    client: TestClient,
) -> None:
    wire("TOP_RIGHT")
    body = run_to_verdict(client, TOP_RIGHT_IMG)
    assert body["verdict"]["authority"] == "DRIFTZERO TRUTH ENGINE"
    assert body["verdict"]["observation_source"] == "Gemma 4 MaaS"
    assert body["field_verification"]["observation"] == "TOP_RIGHT"
    assert body["field_verification"]["deterministic_verdict"] == "PASS"

    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "Deterministic verdict · ${esc(v.authority)}" in app_js
    assert "Model observation · ${esc(f.observation_source" in app_js


def test_the_fail_surface_states_the_required_action(client: TestClient) -> None:
    wire("LEFT")
    body = run_to_verdict(client, LEFT_IMG)
    assert body["verdict"]["result"] == "FAIL"
    assert body["verdict"]["expected_value"] == "TOP_RIGHT"
    assert body["verdict"]["observed_value"] == "LEFT"

    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "CORRECT THE WORK AND PROVIDE NEW EVIDENCE" in app_js


def test_the_inconclusive_surface_never_says_failed(client: TestClient) -> None:
    wire("INCONCLUSIVE")
    body = run_to_verdict(client, AMBIGUOUS_IMG)
    assert body["verdict"]["result"] == "INCONCLUSIVE"
    app_js = (REPO_ROOT / "src" / "driftzero_console" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert 'INCONCLUSIVE: "MORE EVIDENCE REQUIRED"' in app_js


def test_the_worker_surface_renders_the_deterministic_outcome(client: TestClient) -> None:
    wire("LEFT")
    client.post("/api/hero/deploy")
    client.post("/api/hero/deliver")
    view = client.post(
        f"/api/hero/frontline/{CHANGE_ID}/field-evidence", content=LEFT_IMG.read_bytes()
    ).json()
    field = view["field_verification"]
    assert field["deterministic_verdict"] == "FAIL"
    assert field["expected_value"] == "TOP_RIGHT"
    assert field["observation"] == "LEFT"

    frontline_js = (
        REPO_ROOT / "src" / "driftzero_console" / "static" / "frontline.js"
    ).read_text(encoding="utf-8")
    assert "Verification failed" in frontline_js
    assert "Correct the work" in frontline_js


# ============================ 23. the real pilot path =================================


def test_the_current_real_pilot_expected_observed_path_is_supported(
    client: TestClient,
) -> None:
    """Expected TOP_RIGHT, observed TOP_RIGHT — exactly today's live pilot."""
    wire("TOP_RIGHT")
    body = run_to_verdict(client, TOP_RIGHT_IMG)
    verdict = body["verdict"]
    assert (verdict["expected_value"], verdict["observed_value"]) == (
        "TOP_RIGHT",
        "TOP_RIGHT",
    )
    assert verdict["result"] == "PASS"
    assert verdict["change_verified"] is True
    assert verdict["change_deployed"] is False


def test_the_comparator_is_generic_over_the_expected_value() -> None:
    """The expected side is any approved value; ``TOP_RIGHT`` is not privileged.

    The *observed* side is a different matter: ``ObservedPosition`` is the frozen M0
    domain, so a non-position requirement cannot yet be observed at all. That limit is
    T013's and is deliberately not widened here — see the honest note in the report.
    """
    assert (
        frozen_verification.compare_observation("LEFT", ObservedPosition.LEFT)
        is VerificationResult.PASS
    )
    # An unrelated structured requirement compares correctly on the expected side: no
    # position can satisfy "18 Nm", so every observation of it is a FAIL, never a PASS.
    for observed in (ObservedPosition.LEFT, ObservedPosition.TOP_RIGHT):
        assert (
            frozen_verification.compare_observation("18 Nm", observed)
            is VerificationResult.FAIL
        )
    assert (
        frozen_verification.compare_observation("18 Nm", ObservedPosition.INCONCLUSIVE)
        is VerificationResult.INCONCLUSIVE
    )


def test_an_arbitrary_second_case_adjudicates_without_special_casing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "driftzero-runtime-2026")
    fv.register_field_observation_provider(lambda _c: StubProvider("TOP_RIGHT"))
    service = HeroConsoleService(case=TORQUE_CASE)
    monkeypatch.setattr(app_module, "_service", service)
    with TestClient(app_module.app) as client:
        client.post("/api/hero/deploy")
        client.post("/api/hero/deliver")
        verdict = client.post(
            "/api/hero/field-evidence", content=TOP_RIGHT_IMG.read_bytes()
        ).json()["verdict"]
    # Expected "18 Nm" cannot be satisfied by a position observation, so the frozen
    # comparator returns FAIL. The wiring stayed generic; the observation domain did not.
    assert verdict["expected_value"] == "18 Nm"
    assert verdict["observed_value"] == "TOP_RIGHT"
    assert verdict["result"] == "FAIL"


def test_no_pilot_value_is_hard_coded_in_the_verdict_layer() -> None:
    path = REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py"
    literals = {
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not literals & {"TOP_RIGHT", "LEFT", "label_position", "DZ-001", "WI-114"}


# ============================ 21. no live model =======================================


def test_no_live_provider_is_reachable_from_this_suite() -> None:
    assert fv.has_field_observation_provider() is False


def test_the_verdict_layer_imports_no_model_or_cloud_dependency() -> None:
    forbidden = {"google", "httpx", "requests", "vertexai", "openai", "urllib3", "grpc"}
    path = REPO_ROOT / "src" / "driftzero" / "agents" / "orchestrator.py"
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & forbidden


def test_the_verdict_is_reproducible_from_stored_evidence_alone() -> None:
    """Re-deriving the verdict needs no model: the stored evidence is sufficient."""
    store = FieldEvidenceStore()
    boundary, _ = accepted_boundary(store, "LEFT")
    outcome = adjudicate(boundary, store=store)
    record = store.resolve(outcome.event.raw_evidence_ref)
    replayed = frozen_verification.compare_observation(
        outcome.event.expected_value, record["normalized_observation"]
    )
    assert replayed is outcome.result


def test_runtime_readiness_is_unchanged_by_a_passing_verdict(client: TestClient) -> None:
    wire("TOP_RIGHT")
    body = run_to_verdict(client, TOP_RIGHT_IMG)
    assert body["verdict"]["result"] == "PASS"
    environment = body["environment"]
    assert environment["runtime_readiness"] == "LOCAL_PILOT"
    assert environment["production_ready"] is False


def test_the_state_payload_never_carries_a_proof(client: TestClient) -> None:
    wire("TOP_RIGHT")
    body = run_to_verdict(client, TOP_RIGHT_IMG)
    # ``content_hash`` on the artifact is T073's, and unrelated to proof — only
    # proof-specific material is forbidden here.
    blob = json.dumps(body)
    for banned in ("proof_id", "ChangeProof", "proof_content_hash", "completion_timestamp"):
        assert banned not in blob
    assert body["verdict"]["proof_generated"] is False
