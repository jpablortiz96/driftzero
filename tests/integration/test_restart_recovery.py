"""T099 — restart/recovery, and the T097 resumability it depends on.

The scenario is deliberately three processes, not one with a reset flag. Each runtime is
constructed, used, and destroyed; the next one shares nothing with it except Firestore.
That is the only way to tell real durable resumption from a convincing in-process
simulation.

    runtime A   source change -> impact -> remediation -> delivery -> pause
       destroyed
    runtime B   recovers, submits LEFT  -> Crossing 4 -> FAIL
       destroyed
    runtime C   recovers, submits TOP_RIGHT -> PASS -> 7/7 -> PROOF_COMPLETE

Then: history is [FAIL, PASS], remediation dispatched once, delivery dispatched once,
exactly one proof, one logical orchestration lineage.

Offline throughout — deterministic model substitutes, the in-memory Firestore double.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from driftzero.agents import field_verify as fv
from driftzero.agents import model_client as mc
from driftzero.models.workflow import WorkflowState
from driftzero_api.runtime import ApiRuntime, NotResumable, ResumeHeldElsewhere
from driftzero_cloud.composition import FirestoreSink
from driftzero_cloud.firestore import FirestorePersistence
from driftzero_cloud.leases import LeaseDenied, ResumeLeases
from tests.integration._fake_gcp import FakeFirestoreClient
from tests.integration._pilot import arm_for_service, clear_change_intelligence
from tests.integration.test_restart_persistence import OfflineGemma

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"


def hero_body() -> dict[str, Any]:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    return {k: v for k, v in payload.items() if not k.startswith("_")}


@pytest.fixture
def database() -> FakeFirestoreClient:
    """The only thing the three runtimes share."""
    return FakeFirestoreClient()


@pytest.fixture
def providers() -> Any:
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


def new_runtime(database: FakeFirestoreClient, *, instance: str) -> ApiRuntime:
    """A fresh process, sharing only the database."""
    persistence = FirestorePersistence.over(database)
    return ApiRuntime(
        fixtures_dir=FIXTURES,
        sink=FirestoreSink(persistence),
        persistence=persistence,
        instance_id=instance,
    )


# ============================ the three-runtime scenario ==============================


@pytest.fixture
def scenario(database: FakeFirestoreClient, providers: Any) -> dict[str, Any]:
    """Runtime A creates and pauses; B fails; C passes. Each is destroyed in turn."""
    runtime_a = new_runtime(database, instance="instance-a")
    accepted = runtime_a.accept_change(hero_body())
    workflow_id = accepted["workflow_id"]
    service = runtime_a.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()
    paused_state = service._session.workflow.state
    del runtime_a, service
    mc.clear_model_client_provider()

    runtime_b = new_runtime(database, instance="instance-b")
    service_b = runtime_b.resume(workflow_id)
    arm_for_service(service_b)
    service_b.submit_field_evidence(LEFT_IMG.read_bytes())
    failed = service_b.generate_proof()
    runtime_b.release(workflow_id)
    del runtime_b, service_b
    mc.clear_model_client_provider()

    runtime_c = new_runtime(database, instance="instance-c")
    service_c = runtime_c.resume(workflow_id)
    arm_for_service(service_c)
    service_c.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    passed = service_c.generate_proof()
    runtime_c.release(workflow_id)

    return {
        "database": database,
        "workflow_id": workflow_id,
        "paused_state": paused_state,
        "failed": failed,
        "passed": passed,
        "runtime_c": runtime_c,
        "service_c": service_c,
        "gemma": providers,
    }


def test_runtime_a_pauses_awaiting_field_verification(scenario: dict[str, Any]) -> None:
    assert scenario["paused_state"] is WorkflowState.AWAITING_FIELD_VERIFICATION


def test_the_workflow_reaches_proof_complete_across_three_processes(
    scenario: dict[str, Any],
) -> None:
    final = FirestorePersistence.over(scenario["database"]).workflows.load(
        scenario["workflow_id"]
    )
    assert final is not None
    assert final.state is WorkflowState.PROOF_COMPLETE
    assert final.proof_id is not None


def test_the_verification_chronology_is_fail_then_pass(scenario: dict[str, Any]) -> None:
    """Both attempts survive. The FAIL is history, not something a restart tidies away."""
    final = FirestorePersistence.over(scenario["database"]).workflows.load(
        scenario["workflow_id"]
    )
    assert [str(e.verification_result) for e in final.verification_events] == [
        "FAIL",
        "PASS",
    ]
    assert str(final.latest_verification_status) == "PASS"


def test_the_fail_blocked_the_proof_and_the_pass_produced_it(
    scenario: dict[str, Any],
) -> None:
    assert scenario["failed"]["proof"]["generated"] is False
    assert scenario["passed"]["proof"]["generated"] is True


def test_exactly_one_proof_exists(scenario: dict[str, Any]) -> None:
    proofs = [p for p in scenario["database"].documents if "/proofs/" in p]
    assert len(proofs) == 1, f"expected one durable proof, found {proofs}"


def test_the_proof_still_validates_against_its_own_hash(scenario: dict[str, Any]) -> None:
    from driftzero.truth_engine.proof_generator import compute_proof_hash

    proof = FirestorePersistence.over(scenario["database"]).proofs.find_workflow(
        scenario["workflow_id"]
    )
    assert proof is not None
    assert compute_proof_hash(proof) == proof.content_hash


# ============================ zero duplicate logical actions ==========================


def test_no_logical_action_was_duplicated_across_the_restarts(
    scenario: dict[str, Any],
) -> None:
    """The core T099 assertion: a restart must not redispatch what already happened."""
    ledger = FirestorePersistence.over(scenario["database"]).ledger_for(
        scenario["workflow_id"]
    )
    records = ledger.all_records()
    action_ids = [a.action_id for a in records]
    assert len(action_ids) == len(set(action_ids)), f"duplicates: {action_ids}"

    by_type: dict[str, int] = {}
    for action in records:
        by_type[str(action.action_type)] = by_type.get(str(action.action_type), 0) + 1
    assert by_type.get("REMEDIATE_ARTIFACT") == 1, by_type
    assert by_type.get("DELIVER_DELTA") == 1, by_type


def test_the_resumed_process_redispatched_nothing(scenario: dict[str, Any]) -> None:
    """The sharpest statement of the property.

    Runtime C rebuilt the workflow, submitted evidence and generated the proof — and
    dispatched no remediation and no delivery, because the ledger it recovered already
    said both had happened. A resume that redispatched would show a count of 1 here.
    """
    session = scenario["service_c"]._session
    assert session.repository.dispatch_count == 0, "remediation was redispatched on resume"
    assert session.channel.dispatch_count == 0, "delivery was redispatched on resume"


def test_the_recovered_artifact_carries_the_remediation_that_already_happened(
    scenario: dict[str, Any],
) -> None:
    """Not redispatching is only safe if the mutation is still visible after recovery."""
    artifact = scenario["service_c"]._session.repository.read("wi-packing-standard-001")
    assert artifact is not None
    assert artifact.requirements["label_position"] == "TOP_RIGHT"


def test_every_recovered_action_kept_its_original_status(
    scenario: dict[str, Any],
) -> None:
    """Restoring the ledger must reinstate the record, not re-derive a new one."""
    records = (
        FirestorePersistence.over(scenario["database"])
        .ledger_for(scenario["workflow_id"])
        .all_records()
    )
    assert records
    assert all(str(a.status) == "COMPLETED" for a in records), [
        (a.action_id, str(a.status)) for a in records
    ]
    # A re-derived record would carry a fresh timestamp; a reinstated one does not.
    assert all(a.created_at <= a.updated_at for a in records)


def test_the_model_was_called_exactly_twice(scenario: dict[str, Any]) -> None:
    """Two observations across three processes — no attempt was replayed."""
    assert scenario["gemma"].calls == 2


# ============================ one logical orchestration lineage =======================


def test_one_durable_workflow_document_exists(scenario: dict[str, Any]) -> None:
    workflows = [
        p for p in scenario["database"].documents if p.count("/") == 1 and "workflows/" in p
    ]
    assert workflows == [f"workflows/{scenario['workflow_id']}"]


def test_the_stored_input_is_what_lets_a_fresh_process_rebuild(
    scenario: dict[str, Any],
) -> None:
    """Without the original source change, a new instance could only guess at it."""
    inputs = [p for p in scenario["database"].documents if "workflow_inputs/" in p]
    assert inputs == [f"workflow_inputs/{scenario['workflow_id']}"]
    document = scenario["database"].documents[inputs[0]]
    assert document["schema_version"] == 1
    assert document["payload"]["change_id"] == "chg-2026-0817-0001"


def test_an_unreadable_stored_input_fails_closed(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime = new_runtime(database, instance="instance-a")
    workflow_id = runtime.accept_change(hero_body())["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    del runtime

    # A future schema this build cannot read.
    database.documents[f"workflow_inputs/{workflow_id}"]["schema_version"] = 99
    fresh = new_runtime(database, instance="instance-z")
    fresh.registry._services.clear()
    with pytest.raises(NotResumable, match="schema_version"):
        fresh.resume(workflow_id)


# ============================ terminal-state rules ====================================


def test_a_completed_workflow_is_not_resumable(scenario: dict[str, Any]) -> None:
    fresh = new_runtime(scenario["database"], instance="instance-d")
    eligibility = fresh.resume_eligibility(scenario["workflow_id"])
    assert eligibility["eligible"] is False
    assert eligibility["reason"] == "TERMINAL_SUCCESS"
    with pytest.raises(NotResumable, match="TERMINAL_SUCCESS"):
        fresh.resume(scenario["workflow_id"])


def test_a_paused_workflow_is_resumable(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime = new_runtime(database, instance="instance-a")
    workflow_id = runtime.accept_change(hero_body())["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()
    del runtime

    fresh = new_runtime(database, instance="instance-b")
    eligibility = fresh.resume_eligibility(workflow_id)
    assert eligibility["eligible"] is True
    assert eligibility["state"] == "AWAITING_FIELD_VERIFICATION"


def test_an_unknown_workflow_is_not_found_rather_than_not_resumable(
    database: FakeFirestoreClient,
) -> None:
    """Two different answers. Collapsing them would hide a real workflow behind a 404."""
    from driftzero_api.runtime import WorkflowNotFound

    runtime = new_runtime(database, instance="instance-a")
    with pytest.raises(WorkflowNotFound):
        runtime.resume_eligibility("wf-never-existed")


# ============================ concurrent resume =======================================


def test_two_instances_cannot_both_resume_the_same_workflow(
    database: FakeFirestoreClient, providers: Any
) -> None:
    """Cloud Run runs up to two instances; both may receive work for one workflow."""
    runtime = new_runtime(database, instance="instance-a")
    workflow_id = runtime.accept_change(hero_body())["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()
    del runtime

    first = new_runtime(database, instance="instance-1")
    second = new_runtime(database, instance="instance-2")

    first.resume(workflow_id)
    with pytest.raises(ResumeHeldElsewhere) as exc:
        second.resume(workflow_id)
    assert exc.value.holder == "instance-1"
    assert workflow_id not in second.registry._services, "the loser rebuilt anyway"


def test_the_lease_is_released_and_can_then_be_taken(
    database: FakeFirestoreClient, providers: Any
) -> None:
    runtime = new_runtime(database, instance="instance-a")
    workflow_id = runtime.accept_change(hero_body())["workflow_id"]
    service = runtime.live_service(workflow_id)
    arm_for_service(service)
    service.analyze_change()
    service.deploy_change()
    service.deliver_to_frontline()
    del runtime

    first = new_runtime(database, instance="instance-1")
    first.resume(workflow_id)
    assert first.release(workflow_id) is True

    second = new_runtime(database, instance="instance-2")
    assert second.resume(workflow_id) is not None


def test_the_claim_is_atomic_not_read_then_write(database: FakeFirestoreClient) -> None:
    leases = ResumeLeases(database)
    first = leases.acquire("wf-1", "instance-1")
    with pytest.raises(LeaseDenied) as exc:
        leases.acquire("wf-1", "instance-2")
    assert exc.value.holder == "instance-1"
    assert leases.holder("wf-1")["live"] is True
    assert leases.release(first) is True
    assert leases.holder("wf-1") is None


def test_an_expired_lease_can_be_taken_over(database: FakeFirestoreClient) -> None:
    """A holder that crashes must not lock the workflow out forever."""
    leases = ResumeLeases(database, ttl_seconds=0)
    first = leases.acquire("wf-1", "instance-1")
    taken = leases.acquire("wf-1", "instance-2")

    assert taken.owner == "instance-2"
    record = leases.holder("wf-1")
    assert record["owner"] == "instance-2"
    assert record["superseded_owner"] == "instance-1", "the displacement is recorded"
    # The displaced holder can no longer release a lease it no longer owns.
    assert leases.release(first) is False


def test_releasing_a_lease_you_do_not_hold_is_refused(
    database: FakeFirestoreClient,
) -> None:
    leases = ResumeLeases(database, ttl_seconds=0)
    stale = leases.acquire("wf-1", "instance-1")
    leases.acquire("wf-1", "instance-2")
    assert leases.release(stale) is False, "a stale holder released the new owner's lease"


def test_the_lease_is_durable_not_an_in_process_lock(
    database: FakeFirestoreClient,
) -> None:
    """Two separate lease clients over one database still contend correctly."""
    ResumeLeases(database).acquire("wf-1", "instance-1")
    with pytest.raises(LeaseDenied):
        ResumeLeases(database).acquire("wf-1", "instance-2")
    assert any("resume_leases/" in p for p in database.documents)


# ============================ resume authority ========================================


def test_resuming_never_lets_an_adapter_set_a_verdict() -> None:
    """The recovered path still goes through Crossing 4 and the frozen comparator."""
    import ast
    import re

    source = (REPO_ROOT / "src" / "driftzero_api" / "runtime.py").read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            node.body = [
                n
                for n in node.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ] or [ast.Pass()]
    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ast.unparse(tree)))
    for forbidden in (
        "VerificationResult",
        "adjudicate_field_verification",
        "generate_change_proof",
        "compute_proof_hash",
        "normalize_observation",
    ):
        assert forbidden not in names, f"the resume path touches {forbidden!r}"


def test_the_session_adapter_is_outside_the_purity_boundary() -> None:
    import ast

    src = REPO_ROOT / "src"
    offenders = []
    for path in sorted((src / "driftzero").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            if "google" in names:
                offenders.append(path.name)
    assert offenders == []
    assert (src / "driftzero_adk" / "firestore_session.py").is_file()
