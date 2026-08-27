"""T092/T093 — smoke tests against real Google Cloud.

Skipped unless ``DRIFTZERO_CLOUD_SMOKE=1`` and Application Default Credentials are
available, so the ordinary suite stays offline and free.

Safety rules these tests hold themselves to:

* only ``driftzero-runtime-2026``; the legacy project is never addressed
* every document and object carries a unique ``smoke-<uuid>`` namespace
* teardown removes exactly what the test created, and nothing else
* the database is never deleted, no bucket is created or removed, no IAM is touched
* no existing evidence object is read, written, or overwritten
* no Gemini or Gemma call is made

What these add over the offline doubles is the part a fake cannot honestly assert: that
Firestore's ``create`` precondition and Cloud Storage's ``if_generation_match=0`` behave
as the adapters assume, and that a transaction really is atomic.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from driftzero.truth_engine.evidence import content_hash
from driftzero_cloud.errors import ConflictingRecord
from driftzero_cloud.firestore import FirestorePersistence, build_client
from driftzero_cloud.gcs import GcsEvidenceStore
from driftzero_cloud.gcs import build_client as build_storage_client

PROJECT = "driftzero-runtime-2026"
BUCKET = f"driftzero-evidence-{PROJECT}"
DATABASE = "(default)"

pytestmark = pytest.mark.skipif(
    os.environ.get("DRIFTZERO_CLOUD_SMOKE") != "1",
    reason="set DRIFTZERO_CLOUD_SMOKE=1 to run smoke tests against real Google Cloud",
)


@pytest.fixture(scope="module")
def namespace() -> str:
    """A unique prefix so a smoke run can never collide with real data or another run."""
    return f"smoke-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="module")
def firestore(namespace: str) -> Any:
    client = build_client(project=PROJECT, database=DATABASE)
    persistence = FirestorePersistence.over(client)
    created: list[Any] = []
    yield persistence, created
    # Remove exactly the documents this run created.
    for ref in created:
        try:
            ref.delete()
        except Exception:  # pragma: no cover - cleanup must never mask a test result
            pass


@pytest.fixture(scope="module")
def evidence(namespace: str) -> Any:
    store = GcsEvidenceStore(build_storage_client(project=PROJECT), bucket=BUCKET)
    written: list[str] = []
    yield store, written
    for path in written:
        try:
            store.delete(path)
        except Exception:  # pragma: no cover
            pass


# ============================ project safety ==========================================


def test_the_smoke_run_targets_only_the_runtime_project() -> None:
    client = build_client(project=PROJECT, database=DATABASE)
    assert client.project == PROJECT
    assert "agentic" not in client.project


# ============================ Firestore ===============================================


def test_a_real_idempotency_key_is_claimed_exactly_once(
    firestore: Any, namespace: str
) -> None:
    """The behaviour the offline double models: create is atomic, not read-then-write."""
    persistence, created = firestore
    key = f"{namespace}-key"
    created.append(persistence.client.collection("idempotency_keys").document(key))

    first = persistence.idempotency.claim(key, "runtime-a")
    assert first.granted is True

    again = persistence.idempotency.claim(key, "runtime-a")
    assert again.granted is False

    with pytest.raises(ConflictingRecord):
        persistence.idempotency.claim(key, "runtime-b")

    assert persistence.idempotency.owner_of(key) == "runtime-a"


def test_a_real_workflow_round_trips(firestore: Any, namespace: str) -> None:
    from tests.integration.test_cloud_persistence import make_workflow

    persistence, created = firestore
    workflow_id = f"{namespace}-wf"
    created.append(persistence.client.collection("workflows").document(workflow_id))

    workflow = make_workflow(workflow_id=workflow_id)
    persistence.workflows.save(workflow, state_history=["CHANGE_RECEIVED"])

    record = persistence.workflows.load_record(workflow_id)
    assert record is not None
    assert record.workflow == workflow
    assert list(record.state_history) == ["CHANGE_RECEIVED"]
    assert persistence.workflows.load(f"{namespace}-absent") is None


def test_a_real_transaction_refuses_a_stale_revision(
    firestore: Any, namespace: str
) -> None:
    """Compare-and-set inside a genuine Firestore transaction."""
    from tests.integration.test_cloud_persistence import make_workflow

    persistence, created = firestore
    workflow_id = f"{namespace}-wf-cas"
    created.append(persistence.client.collection("workflows").document(workflow_id))

    workflow = make_workflow(workflow_id=workflow_id)
    first = persistence.workflows.save(workflow)
    second = persistence.workflows.save(workflow, expected_revision=first)
    assert second == first + 1

    with pytest.raises(ConflictingRecord):
        persistence.workflows.save(workflow, expected_revision=first)


def test_a_real_conflicting_proof_is_refused(firestore: Any, namespace: str) -> None:
    from tests.integration.test_cloud_persistence import _generate_real_proof

    persistence, created = firestore
    proof = _generate_real_proof()
    workflow_id = f"{namespace}-wf-proof"
    scoped = proof.model_copy(update={"workflow_id": workflow_id})
    created.append(
        persistence.client.collection("workflows")
        .document(workflow_id)
        .collection("proofs")
        .document(scoped.proof_id)
    )

    stored = persistence.proofs.record(scoped)
    assert stored.created is True
    assert persistence.proofs.record(scoped).created is False

    tampered = scoped.model_copy(update={"worker_id": "someone-else"})
    with pytest.raises(ConflictingRecord):
        persistence.proofs.record(tampered)

    recovered = persistence.proofs.resolve(workflow_id, scoped.proof_id)
    assert recovered == scoped
    assert recovered.content_hash == scoped.content_hash


# ============================ Cloud Storage ===========================================


def test_a_real_evidence_object_round_trips(evidence: Any, namespace: str) -> None:
    store, written = evidence
    from driftzero_cloud.gcs import evidence_path

    data = b"DRIFTZERO cloud smoke evidence payload"
    path = evidence_path(namespace, "ev-001")
    written.append(path)

    stored = store.put_evidence(
        workflow_id=namespace, evidence_id="ev-001", data=data, content_type="text/plain"
    )
    assert stored.created is True
    assert stored.content_hash == content_hash(data)
    assert stored.size == len(data)
    assert stored.generation is not None
    assert store.get(path) == data


def test_a_real_identical_rewrite_is_idempotent(evidence: Any, namespace: str) -> None:
    store, written = evidence
    from driftzero_cloud.gcs import evidence_path

    data = b"identical payload"
    path = evidence_path(namespace, "ev-idem")
    written.append(path)

    first = store.put_evidence(workflow_id=namespace, evidence_id="ev-idem", data=data)
    second = store.put_evidence(workflow_id=namespace, evidence_id="ev-idem", data=data)
    assert first.created is True
    assert second.created is False
    assert first.content_hash == second.content_hash


def test_a_real_conflicting_object_is_refused(evidence: Any, namespace: str) -> None:
    """The generation precondition really does prevent overwriting evidence."""
    store, written = evidence
    from driftzero_cloud.gcs import evidence_path

    path = evidence_path(namespace, "ev-immutable")
    written.append(path)

    store.put_evidence(workflow_id=namespace, evidence_id="ev-immutable", data=b"original")
    with pytest.raises(ConflictingRecord):
        store.put_evidence(
            workflow_id=namespace, evidence_id="ev-immutable", data=b"replacement"
        )
    assert store.get(path) == b"original"


def test_the_smoke_run_never_touched_existing_evidence(evidence: Any, namespace: str) -> None:
    """Everything this module wrote lives under its own namespace."""
    _store, written = evidence
    assert written, "the smoke run wrote nothing — it would prove nothing"
    assert all(namespace in path for path in written)
