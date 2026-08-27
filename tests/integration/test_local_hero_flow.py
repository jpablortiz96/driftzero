"""T082 — the local end-to-end hero flow.

One controlled change, driven in-process through every real seam:

    synthetic source change → ChangeSet → Crossing 1 → impact → remediation →
    Crossing 2 → delivery → Crossing 3 → FAIL evidence → Crossing 4 → corrected
    PASS → seven invariants → PROOF_COMPLETE

Nothing is stubbed except the two models. The ADK agents, runner, session service and
resumability are real; the Truth Engine, the crossings, the capability broker, the stores
and the proof generator are the production ones. The test never constructs a
``VerificationEvent`` or a ``ChangeProof`` — it asserts what the system produced.

Outcomes are read, never written. A driver that told the flow what to conclude would be
testing itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.agents.field_verify import ProviderObservation  # noqa: E402
from driftzero.models.verification import VerificationResult  # noqa: E402
from driftzero.models.workflow import WorkflowState  # noqa: E402
from driftzero.truth_engine.proof_generator import (  # noqa: E402
    ProofCondition,
    ProofValidator,
    compute_proof_hash,
)
from driftzero_adk.hero_workflow import HeroWorkflowRun  # noqa: E402
from driftzero_console.service import HeroConsoleService  # noqa: E402
from driftzero_console.workflows import dataset_from_fixture  # noqa: E402

from ._pilot import clear_change_intelligence, make_stub_llm, proposal_payload  # noqa: E402

FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"

CATALOG = (
    "wi-forklift-turn-014",
    "wi-packing-nightshift-007",
    "wi-packing-standard-001",
    "wi-packing-standard-002",
    "wi-packing-standard-003",
)
QUALIFIED = "wi-packing-standard-001"
LEXICAL_DECOY = "wi-forklift-turn-014"
"""Carries ``LEFT`` in a different operation. SC-002: it must never be touched."""


class CountingGemma:
    """Deterministic observations in order, counting every billable call."""

    name = "stub_gemma"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def observe(self, **_kwargs: Any) -> ProviderObservation:
        self.calls += 1
        return ProviderObservation(
            raw_output=self.outputs[min(self.calls - 1, len(self.outputs) - 1)],
            provider=self.name,
            model="stub/gemma",
        )


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    yield
    clear_change_intelligence()
    fv.clear_field_observation_provider()


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A complete offline runtime for one synthetic change."""
    monkeypatch.setenv("DRIFTZERO_FIELD_PROVIDER", "vertex_maas")
    monkeypatch.setenv("DRIFTZERO_GCP_PROJECT", "offline-stub")
    monkeypatch.setenv("DRIFTZERO_SEMANTIC_PROVIDER", "google_adk")
    monkeypatch.setenv("DRIFTZERO_GEMINI_MODEL", "stub-gemini")

    dataset = dataset_from_fixture(
        json.loads(HERO_FIXTURE.read_text(encoding="utf-8")), directory=FIXTURES
    )
    service = HeroConsoleService(dataset=dataset, workflow_namespace="wf-e2e")

    from driftzero.models.change import ChangeSet
    from driftzero_adk.change_intel_runtime import GoogleAdkSemanticClient

    # The model proposes every catalog artifact. Any single qualified target that comes
    # out the far end was chosen by the Truth Engine, not handed to it.
    handle = make_stub_llm(
        lambda _req: proposal_payload(service.current_change, artifact_ids=CATALOG)
    )
    mc.register_model_client_provider(
        lambda cfg: GoogleAdkSemanticClient(
            config=cfg, output_schema=ChangeSet, model_override=handle.llm, use_vertex=False
        )
    )
    gemma = CountingGemma(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: gemma)

    return {"service": service, "gemma": gemma, "llm": handle}


def run_to_pause(flow: dict[str, Any]) -> HeroWorkflowRun:
    run = HeroWorkflowRun(service=flow["service"])
    asyncio.run(run.start())
    return run


def submit(flow: dict[str, Any], image: Path) -> dict[str, Any]:
    """Submit field evidence through the real use case, then attempt the proof gate."""
    service = flow["service"]
    service.submit_field_evidence(
        image.read_bytes(), declared_filename=image.name, declared_content_type="image/jpeg"
    )
    return service.generate_proof()


# ============================ the whole flow, once ====================================


@pytest.fixture
def completed(flow: dict[str, Any]) -> dict[str, Any]:
    """Drive the entire scenario and hand back every observed state along the way."""
    run = run_to_pause(flow)
    paused = flow["service"].get_state()

    failed = submit(flow, LEFT_IMG)
    passed = submit(flow, TOP_RIGHT_IMG)

    asyncio.run(run.close())
    return {**flow, "paused": paused, "failed": failed, "passed": passed, "run": run}


# ============================ source and impact =======================================


def test_the_source_versions_resolve_and_derive_the_change(flow: dict[str, Any]) -> None:
    service = flow["service"]
    session = service._session
    ingestion = session.ingestion

    for ref in (ingestion.previous.content_ref, ingestion.current.content_ref):
        assert session.source_store.resolve(ref) is not None, f"{ref} must resolve"

    # Derived from the diff of two retrieved versions, not declared by the fixture.
    assert ingestion.as_evidence()["derivation"] == "DIFF_OF_TWO_RETRIEVED_SOURCE_VERSIONS"
    assert ingestion.delta.requirement_id == "label_position"
    assert (ingestion.delta.previous_value, ingestion.delta.current_value) == (
        "LEFT",
        "TOP_RIGHT",
    )
    assert service.current_change.requirement_id == ingestion.delta.requirement_id


def test_crossing_1_accepts_and_impact_qualifies_exactly_one(flow: dict[str, Any]) -> None:
    run_to_pause(flow)
    state = flow["service"].get_state()

    assert state["crossing_1"]["verdict"] == "ACCEPTED"
    impact = state["impact"]
    assert impact["candidate_count"] == len(CATALOG)
    assert impact["qualified_count"] == 1
    assert impact["affected_artifact_id"] == QUALIFIED
    assert impact["outcome"] == "SINGLE_QUALIFIED_TARGET"
    assert impact["authority"] == "DRIFTZERO TRUTH ENGINE"
    # The model claimed all five. Four were overruled.
    assert sum(1 for e in impact["evaluated"] if e["agent_proposal_disagreed"]) == 4


def test_the_unrelated_lexical_left_artifact_is_never_touched(flow: dict[str, Any]) -> None:
    """SC-002: a different operation that merely contains ``LEFT`` is not affected."""
    service = flow["service"]
    before = service._session.repository.read(LEXICAL_DECOY)
    before_value = before.requirements["turn_direction"]

    run_to_pause(flow)
    submit(flow, TOP_RIGHT_IMG)

    after = service._session.repository.read(LEXICAL_DECOY)
    assert after.requirements["turn_direction"] == before_value == "LEFT"
    assert service._session.repository.dispatch_count == 1, "only the qualified target"


# ============================ remediation and delivery ================================


def test_remediation_and_crossing_2(flow: dict[str, Any]) -> None:
    run_to_pause(flow)
    state = flow["service"].get_state()

    assert state["remediation"]["status"] == "MUTATED"
    assert state["remediation"]["dispatch_count"] == 1
    assert state["crossing_2"]["verdict"] == "ACCEPTED"
    assert state["validated_execution"]["accepted"] is True
    assert state["artifact"]["artifact_id"] == QUALIFIED
    assert state["artifact"]["requirements"]["label_position"] == "TOP_RIGHT"
    # Independently retrievable before and after states.
    assert (
        state["crossing_2"]["authoritative_before_hash"]
        != state["crossing_2"]["authoritative_after_hash"]
    )


def test_delivery_receipt_resolves_and_crossing_3_accepts(flow: dict[str, Any]) -> None:
    run_to_pause(flow)
    service = flow["service"]
    state = service.get_state()

    delivery = state["delivery"]
    assert delivery["crossing_3"] == "ACCEPTED"
    assert delivery["delivery_established"] is True
    assert delivery["dispatch_count"] == 1

    receipt = service._session.channel.resolve(delivery["receipt_ref"])
    assert receipt is not None, "the receipt reference must independently resolve"
    assert receipt.payload_hash == delivery["authoritative_payload_hash"]


def test_the_flow_pauses_awaiting_field_evidence(flow: dict[str, Any]) -> None:
    run = run_to_pause(flow)
    assert run.log.paused_at == "s08_await_field_evidence"
    assert "s11_change_proof" not in run.log.executed
    state = flow["service"].get_state()
    assert state["verdict"]["workflow_state"] == str(
        WorkflowState.AWAITING_FIELD_VERIFICATION
    )
    assert flow["gemma"].calls == 0


# ============================ FAIL evidence ===========================================


def test_the_first_observation_fails_and_blocks_the_proof(completed: dict[str, Any]) -> None:
    failed = completed["failed"]

    field = failed["field_verification"]
    assert field["observation"] == "LEFT"
    assert field["crossing_4"]["verdict"] == "ACCEPTED", "a FAIL is still validated"

    verdict = failed["verdict"]
    assert verdict["result"] == str(VerificationResult.FAIL)
    assert verdict["expected_value"] == "TOP_RIGHT"
    assert verdict["observed_value"] == "LEFT"
    assert verdict["workflow_state"] == str(WorkflowState.VERIFICATION_FAILED)
    assert verdict["change_deployed"] is False

    proof = failed["proof"]
    assert proof["generated"] is False
    assert proof["proof_id"] is None
    assert proof["satisfied_count"] < 7
    assert "Latest authoritative verification is PASS" in proof["blockers"]


# ============================ corrected PASS ==========================================


def test_the_corrected_observation_passes_and_preserves_history(
    completed: dict[str, Any],
) -> None:
    passed = completed["passed"]
    verdict = passed["verdict"]

    assert passed["field_verification"]["observation"] == "TOP_RIGHT"
    assert passed["field_verification"]["crossing_4"]["verdict"] == "ACCEPTED"
    assert verdict["result"] == str(VerificationResult.PASS)

    history = verdict["history"]
    assert [h["result"] for h in history] == ["FAIL", "PASS"]
    assert [h["event_sequence"] for h in history] == [1, 2]
    # The historical failure is retained verbatim, not rewritten.
    assert history[0]["observed"] == "LEFT"
    assert history[0]["expected"] == "TOP_RIGHT"


def test_the_current_authoritative_state_is_the_later_pass(completed: dict[str, Any]) -> None:
    from driftzero.truth_engine.verification import latest_authoritative_event

    session = completed["service"]._session
    latest = latest_authoritative_event(
        session.verification_events, session.workflow.workflow_id
    )
    assert latest is not None
    assert latest.verification_result is VerificationResult.PASS
    assert latest.event_sequence == 2


# ============================ proof ===================================================


def test_all_seven_invariants_hold_and_the_proof_completes(completed: dict[str, Any]) -> None:
    proof = completed["passed"]["proof"]

    assert proof["status"] == "PROOF_COMPLETE"
    assert proof["satisfied_count"] == 7
    assert proof["total"] == 7
    assert [c["condition"] for c in proof["conditions"]] == [str(c) for c in ProofCondition]
    assert all(c["satisfied"] for c in proof["conditions"])

    assert completed["passed"]["verdict"]["workflow_state"] == str(
        WorkflowState.PROOF_COMPLETE
    )
    assert proof["change_deployed"] is True


def test_exactly_one_proof_exists_and_it_resolves(completed: dict[str, Any]) -> None:
    service = completed["service"]
    session = service._session
    assert len(session.proof_store) == 1

    stored = session.proof_store.find_workflow(session.workflow.workflow_id)
    assert stored is not None
    assert session.proof_store.resolve(stored.proof_ref) is stored

    document = service.get_proof_document()
    assert json.loads(document["canonical_json"]) == document["document"]
    assert document["content_hash"] == stored.content_hash


def test_the_canonical_hash_validates_from_the_stored_bytes(
    completed: dict[str, Any],
) -> None:
    """Recomputed the way a third party would: the preimage excludes its own digest."""
    document = completed["service"].get_proof_document()
    raw = document["canonical_json"].encode("utf-8")
    doc = json.loads(raw.decode("utf-8"))

    material = {k: v for k, v in doc.items() if k != "content_hash"}
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == doc["content_hash"]
    # And the whole-file digest differs, by construction.
    assert hashlib.sha256(raw).hexdigest() != doc["content_hash"]


def test_the_frozen_validator_accepts_the_generated_proof(completed: dict[str, Any]) -> None:
    service = completed["service"]
    session = service._session
    stored = session.proof_store.find_workflow(session.workflow.workflow_id)
    context = service._proof_context(session)

    result = ProofValidator().validate(stored.proof, context)
    assert result.valid is True
    assert result.failures == ()
    assert compute_proof_hash(stored.proof) == stored.content_hash


# ============================ evidence lineage ========================================


def test_the_proof_references_the_complete_evidence_chain(completed: dict[str, Any]) -> None:
    service = completed["service"]
    session = service._session
    stored = session.proof_store.find_workflow(session.workflow.workflow_id)
    proof = stored.proof
    manifest = proof.evidence_manifest

    assert manifest.source_change_ref == session.ingestion.current.content_ref
    assert session.source_store.resolve(manifest.source_change_ref) is not None

    assert manifest.affected_artifact_ref
    assert len(manifest.remediation_evidence_refs) == 2, "before and after, distinct"
    assert manifest.remediation_evidence_refs[0] != manifest.remediation_evidence_refs[1]
    for ref in manifest.remediation_evidence_refs:
        assert session.repository.resolve(ref) is not None, f"{ref} must resolve"
        assert ref in manifest.content_hashes

    assert manifest.delivery_ref == session.delivery_receipt_ref
    assert session.channel.resolve(manifest.delivery_ref) is not None

    # Both attempts are referenced — condition 6 requires the full history.
    assert len(manifest.verification_refs) == 2
    assert proof.verification_event_id in manifest.verification_refs
    assert manifest.state_transition_refs

    assert proof.affected_artifact_id == QUALIFIED
    assert (proof.previous_value, proof.current_value) == ("LEFT", "TOP_RIGHT")
    assert proof.verification_result is VerificationResult.PASS


# ============================ replay and idempotency ==================================


def test_replaying_the_whole_scenario_duplicates_nothing(completed: dict[str, Any]) -> None:
    service = completed["service"]
    session = service._session
    before = {
        "remediation": session.repository.dispatch_count,
        "delivery": session.channel.dispatch_count,
        "gemma": completed["gemma"].calls,
        "events": len(session.verification_events),
        "proofs": len(session.proof_store),
        "hash": session.proof_store.find_workflow(
            session.workflow.workflow_id
        ).content_hash,
    }

    # Re-drive every side-effecting use case with identical inputs.
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()
    submit(completed, TOP_RIGHT_IMG)
    asyncio.run(completed["run"].resume())

    after_stored = session.proof_store.find_workflow(session.workflow.workflow_id)
    assert session.repository.dispatch_count == before["remediation"] == 1
    assert session.channel.dispatch_count == before["delivery"] == 1
    assert completed["gemma"].calls == before["gemma"] == 2, "one call per distinct image"
    assert len(session.verification_events) == before["events"] == 2
    assert len(session.proof_store) == before["proofs"] == 1
    assert after_stored.content_hash == before["hash"]


def test_an_identical_image_never_costs_a_second_provider_call(
    completed: dict[str, Any],
) -> None:
    before = completed["gemma"].calls
    for _ in range(3):
        submit(completed, TOP_RIGHT_IMG)
    assert completed["gemma"].calls == before
    assert len(completed["service"]._session.verification_events) == 2


def test_the_proof_is_returned_unchanged_on_repeat(completed: dict[str, Any]) -> None:
    service = completed["service"]
    first = service.get_proof_document()
    for _ in range(3):
        state = service.generate_proof()
        assert state["proof"]["replayed"] is True
    again = service.get_proof_document()
    assert again["canonical_json"] == first["canonical_json"]
    assert again["content_hash"] == first["content_hash"]


# ============================ readiness and hygiene ===================================


def test_runtime_readiness_is_unchanged_by_a_completed_proof(
    completed: dict[str, Any],
) -> None:
    environment = completed["passed"]["environment"]
    assert environment["runtime_readiness"] == "LOCAL_PILOT"
    assert environment["production_ready"] is False
    assert "PRODUCTION READY" not in json.dumps(completed["passed"]).upper()


def test_the_flow_made_no_live_provider_call() -> None:
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    assert mc.has_model_client_provider() is False
    assert fv.has_field_observation_provider() is False


def test_the_change_is_synthetic_and_declared_as_such() -> None:
    fixture = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["_data_classification"]["labels"] == ["SYNTHETIC"]
