"""T092 — the restart property.

This is the limitation T081 recorded: the CLI could only report state held in the
process that created it, so "no record of this workflow" and "this workflow never
existed" were indistinguishable after a restart. Durable persistence closes it.

The proof is structural rather than narrated. Runtime A drives the real hero flow and
persists. Runtime A is then destroyed — the service object, its session, its in-memory
ledger, its in-memory proof store, all of it. Runtime B is constructed fresh, sharing
nothing with A except the database, and must recover the authoritative state.

Deterministic substitutes stand in for both models. No Gemini or Gemma call is made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from driftzero.agents import field_verify as fv
from driftzero.agents import model_client as mc
from driftzero.agents.field_verify import ProviderObservation
from driftzero.models.workflow import WorkflowState
from driftzero_cloud.composition import FirestoreSink
from driftzero_cloud.firestore import FirestorePersistence
from driftzero_console.service import HeroConsoleService
from driftzero_console.workflows import dataset_from_fixture
from tests.integration._fake_gcp import FakeFirestoreClient
from tests.integration._pilot import arm_for_service, clear_change_intelligence

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"

CATALOG = (
    "wi-packing-standard-001",
    "wi-forklift-turn-014",
    "wi-receiving-002",
    "wi-returns-009",
    "wi-shipping-003",
)


class OfflineGemma:
    """Deterministic field observations, in order. Counts every would-be billable call."""

    name = "offline_gemma"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def observe(self, **_kwargs: Any) -> ProviderObservation:
        self.calls += 1
        return ProviderObservation(
            raw_output=self.outputs[min(self.calls - 1, len(self.outputs) - 1)],
            provider=self.name,
            model="offline/deterministic",
        )


@pytest.fixture
def database() -> FakeFirestoreClient:
    """One database, shared by both runtimes and by nothing else."""
    return FakeFirestoreClient()


@pytest.fixture
def providers() -> Any:
    """Offline model substitutes, torn down so no registration leaks into the suite."""
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


def build_runtime(database: FakeFirestoreClient, namespace: str) -> HeroConsoleService:
    """Construct a service whose writes land in the shared database.

    ``arm_for_service`` registers the real ADK client over a stub model *and* sets the
    configuration the service checks before it will analyse anything — registering the
    provider alone leaves the runtime correctly refusing to run.
    """
    dataset = dataset_from_fixture(
        json.loads(HERO_FIXTURE.read_text(encoding="utf-8")), directory=FIXTURES
    )
    sink = FirestoreSink(FirestorePersistence.over(database))
    service = HeroConsoleService(
        dataset=dataset, workflow_namespace=namespace, persistence=sink
    )
    arm_for_service(service)
    return service


def drive_to_proof(service: HeroConsoleService) -> dict[str, Any]:
    """The durable hero flow: impact, remediation, delivery, FAIL, corrected PASS, proof."""
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()

    service.submit_field_evidence(LEFT_IMG.read_bytes())
    failed = service.generate_proof()

    service.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    passed = service.generate_proof()
    return {"failed": failed, "passed": passed}


# ============================ the restart property ====================================


def test_a_workflow_survives_the_destruction_of_the_runtime_that_created_it(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    workflow_id = runtime_a._session.workflow.workflow_id
    expected_state = runtime_a._session.workflow.state

    # Runtime A ceases to exist. Nothing in-process survives.
    del runtime_a
    clear_change_intelligence()
    mc.clear_model_client_provider()

    # Runtime B shares only the database.
    runtime_b = FirestorePersistence.over(database)
    recovered = runtime_b.workflows.load(workflow_id)

    assert recovered is not None, "the workflow did not survive the restart"
    assert recovered.workflow_id == workflow_id
    assert recovered.state is expected_state is WorkflowState.PROOF_COMPLETE
    assert recovered.affected_artifact_id == "wi-packing-standard-001"
    assert recovered.delivery_status == "DELIVERED"
    assert recovered.proof_id is not None


def test_the_recovered_state_is_read_not_fabricated(
    database: FakeFirestoreClient, providers: Any
) -> None:
    """A fresh runtime must not mint a plausible workflow for an id it never saw."""
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    known = runtime_a._session.workflow.workflow_id
    del runtime_a

    runtime_b = FirestorePersistence.over(database)
    assert runtime_b.workflows.load(known) is not None
    assert runtime_b.workflows.load("wf-restart-999") is None
    assert runtime_b.workflows.load_record("wf-never-existed") is None
    assert runtime_b.proofs.find_workflow("wf-restart-999") is None


def test_the_state_chronology_survives_the_restart(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    workflow_id = runtime_a._session.workflow.workflow_id
    expected = [str(state) for state in runtime_a._session.state_history]
    del runtime_a

    record = FirestorePersistence.over(database).workflows.load_record(workflow_id)
    assert record is not None
    assert list(record.state_history) == expected
    assert str(WorkflowState.CHANGE_RECEIVED) in record.state_history
    assert record.revision > 1, "each transition advanced the stored revision"


def test_the_verification_chronology_survives_including_the_failure(
    database: FakeFirestoreClient, providers: Any
) -> None:
    """The FAIL is history, not something a restart is allowed to tidy away."""
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    workflow_id = runtime_a._session.workflow.workflow_id
    del runtime_a

    recovered = FirestorePersistence.over(database).workflows.load(workflow_id)
    results = [str(event.verification_result) for event in recovered.verification_events]
    assert results == ["FAIL", "PASS"]
    assert str(recovered.latest_verification_status) == "PASS"


def test_the_action_ledger_survives_the_restart(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    workflow_id = runtime_a._session.workflow.workflow_id
    expected = {a.action_id: str(a.status) for a in runtime_a._session.ledger.all_records()}
    assert expected, "the flow recorded no actions — the test would prove nothing"
    del runtime_a

    ledger = FirestorePersistence.over(database).ledger_for(workflow_id)
    recovered = {a.action_id: str(a.status) for a in ledger.all_records()}
    assert recovered == expected


def test_no_logical_action_is_duplicated_across_the_restart(
    database: FakeFirestoreClient, providers: Any
) -> None:
    """Documents are keyed on action_id, so repeated flushes cannot fan out."""
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    workflow_id = runtime_a._session.workflow.workflow_id
    del runtime_a

    records = FirestorePersistence.over(database).ledger_for(workflow_id).all_records()
    action_ids = [a.action_id for a in records]
    assert len(action_ids) == len(set(action_ids)), f"duplicate ledger entries: {action_ids}"


def test_the_proof_survives_the_restart_with_an_identical_hash(
    database: FakeFirestoreClient, providers: Any
) -> None:
    from driftzero.truth_engine.proof_generator import compute_proof_hash

    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    workflow_id = runtime_a._session.workflow.workflow_id
    original = runtime_a._session.proof_store.find_workflow(workflow_id)
    assert original is not None
    original_hash = original.content_hash
    del runtime_a

    recovered = FirestorePersistence.over(database).proofs.find_workflow(workflow_id)
    assert recovered is not None
    assert recovered.content_hash == original_hash
    assert compute_proof_hash(recovered) == original_hash, (
        "a proof recovered from storage must still validate against its own hash"
    )
    assert recovered == original.proof


def test_exactly_one_proof_exists_after_the_restart(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    del runtime_a

    proofs = [path for path in database.documents if "/proofs/" in path]
    assert len(proofs) == 1, f"expected one durable proof, found {proofs}"


def test_the_failed_attempt_never_produced_a_proof(
    database: FakeFirestoreClient, providers: Any
) -> None:
    """Proof generation is gated on the verdict; a FAIL must persist nothing."""
    runtime_a = build_runtime(database, "wf-restart")
    runtime_a.analyze_change()
    runtime_a.deploy_change()
    runtime_a.deliver_to_frontline()
    runtime_a.submit_field_evidence(LEFT_IMG.read_bytes())
    outcome = runtime_a.generate_proof()
    assert outcome["proof"]["generated"] is False
    del runtime_a

    assert [path for path in database.documents if "/proofs/" in path] == []


def test_the_offline_flow_made_no_model_call(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime_a = build_runtime(database, "wf-restart")
    drive_to_proof(runtime_a)
    assert providers.calls == 2, "exactly two field observations, both deterministic"


def test_a_runtime_without_persistence_keeps_the_local_pilot_behaviour(
    providers: Any,
) -> None:
    """The default path is untouched: no sink, no durability claim, no cloud."""
    dataset = dataset_from_fixture(
        json.loads(HERO_FIXTURE.read_text(encoding="utf-8")), directory=FIXTURES
    )
    service = HeroConsoleService(dataset=dataset, workflow_namespace="wf-local")
    assert service.persistence.durable is False
    arm_for_service(service)
    service.analyze_change()
    assert service._session.workflow.state is not WorkflowState.CHANGE_RECEIVED
