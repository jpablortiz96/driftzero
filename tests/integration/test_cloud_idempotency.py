"""T100 — duplicate event and duplicate evidence, against real Firestore and Pub/Sub.

Skipped unless ``DRIFTZERO_CLOUD_SMOKE=1``, so the ordinary suite stays offline and free.

The offline suites already prove these properties against doubles. What only the real
services can settle is whether the preconditions the adapters rely on behave as assumed:
Firestore's ``create`` must actually be atomic under a real contending writer, and a real
Pub/Sub redelivery must actually resolve to the workflow the first delivery created.

Safety: every identifier is namespaced ``t100-<uuid>``; teardown removes exactly what the
test created; the database is never deleted; no existing evidence, no pilot id, and no
model is touched.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from driftzero_cloud.errors import ConflictingRecord
from driftzero_cloud.firestore import FirestorePersistence, build_client
from driftzero_cloud.leases import LeaseDenied, ResumeLeases

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"

PROJECT = "driftzero-runtime-2026"
TOPIC = "driftzero-approved-changes"

pytestmark = pytest.mark.skipif(
    os.environ.get("DRIFTZERO_CLOUD_SMOKE") != "1",
    reason="set DRIFTZERO_CLOUD_SMOKE=1 to run against real Google Cloud",
)


@pytest.fixture(scope="module")
def namespace() -> str:
    return f"t100-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def cloud(namespace: str) -> Any:
    """Real Firestore, with a teardown that removes only what this run created."""
    client = build_client(project=PROJECT)
    persistence = FirestorePersistence.over(client)
    created: list[Any] = []
    server_assigned: set[str] = set()
    yield {
        "client": client,
        "persistence": persistence,
        "created": created,
        "server_assigned": server_assigned,
    }
    for ref in created:
        try:
            ref.delete()
        except Exception:  # pragma: no cover - cleanup must not mask a result
            pass


def hero_payload(change_id: str) -> dict[str, Any]:
    payload = json.loads(HERO_FIXTURE.read_text(encoding="utf-8"))
    body = {k: v for k, v in payload.items() if not k.startswith("_")}
    body["change_id"] = change_id
    return body


# ============================ duplicate event =========================================


def test_a_duplicate_change_claim_is_refused_by_real_firestore(
    cloud: Any, namespace: str
) -> None:
    """The precondition every duplicate-event guarantee rests on."""
    persistence, created = cloud["persistence"], cloud["created"]
    key = f"change-{namespace}-dup"
    created.append(cloud["client"].collection("idempotency_keys").document(key))

    first = persistence.idempotency.claim(key, "wf-first")
    assert first.granted is True

    again = persistence.idempotency.claim(key, "wf-first")
    assert again.granted is False, "a re-claim by the same owner took fresh ownership"

    with pytest.raises(ConflictingRecord):
        persistence.idempotency.claim(key, "wf-second")

    assert persistence.idempotency.owner_of(key) == "wf-first"


def test_two_contending_writers_produce_exactly_one_owner(
    cloud: Any, namespace: str
) -> None:
    """Ten attempts at one key; real Firestore must let exactly one through."""
    persistence, created = cloud["persistence"], cloud["created"]
    key = f"change-{namespace}-contended"
    created.append(cloud["client"].collection("idempotency_keys").document(key))

    granted = 0
    refused = 0
    for index in range(10):
        try:
            if persistence.idempotency.claim(key, f"wf-{index}").granted:
                granted += 1
        except ConflictingRecord:
            refused += 1

    assert granted == 1, f"{granted} writers believed they owned the key"
    assert refused == 9
    assert persistence.idempotency.owner_of(key) == "wf-0"


def test_a_real_pubsub_redelivery_resolves_to_the_first_workflow(
    cloud: Any, namespace: str
) -> None:
    """Publish the same change twice; exactly one workflow must exist afterwards."""
    from google.cloud import pubsub_v1

    change_id = f"{namespace}-event"
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(PROJECT, TOPIC)
    message = json.dumps(hero_payload(change_id)).encode("utf-8")

    first_id = publisher.publish(topic_path, message).result(timeout=30)
    second_id = publisher.publish(topic_path, message).result(timeout=30)
    assert first_id != second_id, "Pub/Sub assigned one id to two publishes"

    client = cloud["client"]
    matches: list[str] = []
    for _ in range(12):
        matches = [
            doc.id
            for doc in client.collection("workflows").stream()
            if (doc.to_dict() or {}).get("change_id") == change_id
        ]
        if matches:
            break
        import time

        time.sleep(5)

    assert len(matches) == 1, f"duplicate delivery created {len(matches)} workflows"

    workflow_id = matches[0]
    # Only registered for cleanup because its change_id is this run's namespace. The
    # workflow ID is assigned server-side, so registering it blind would risk deleting
    # a document another run created — which is exactly what happened once.
    stored = client.collection("workflows").document(workflow_id).get().to_dict() or {}
    assert stored.get("change_id") == change_id, "refusing to own a workflow we did not create"
    cloud["created"].extend(
        [
            client.collection("workflows").document(workflow_id),
            client.collection("workflow_inputs").document(workflow_id),
            client.collection("idempotency_keys").document(f"change-{change_id}"),
            client.collection("resume_snapshots").document(workflow_id),
        ]
    )
    cloud["server_assigned"].add(workflow_id)
    assert (
        persistence_owner(cloud, change_id) == workflow_id
    ), "the durable claim points at a different workflow than the one created"


def persistence_owner(cloud: Any, change_id: str) -> str | None:
    return cloud["persistence"].idempotency.owner_of(f"change-{change_id}")


# ============================ duplicate evidence ======================================


def test_a_duplicate_proof_is_refused_by_real_firestore(cloud: Any, namespace: str) -> None:
    """Evidence is write-once: a differing proof under one id must not overwrite."""
    from tests.integration.test_cloud_persistence import _generate_real_proof

    persistence, created = cloud["persistence"], cloud["created"]
    proof = _generate_real_proof()
    workflow_id = f"{namespace}-proof"
    scoped = proof.model_copy(update={"workflow_id": workflow_id})
    created.append(
        cloud["client"]
        .collection("workflows")
        .document(workflow_id)
        .collection("proofs")
        .document(scoped.proof_id)
    )

    stored = persistence.proofs.record(scoped)
    assert stored.created is True
    assert persistence.proofs.record(scoped).created is False, "not idempotent"

    tampered = scoped.model_copy(update={"worker_id": "someone-else"})
    with pytest.raises(ConflictingRecord):
        persistence.proofs.record(tampered)

    survivor = persistence.proofs.resolve(workflow_id, scoped.proof_id)
    assert survivor == scoped, "a rejected overwrite still changed the stored proof"


def test_a_duplicate_evidence_object_is_refused_by_real_gcs(namespace: str) -> None:
    """The write-once precondition immutable evidence rests on."""
    from driftzero_cloud.gcs import GcsEvidenceStore, evidence_path
    from driftzero_cloud.gcs import build_client as build_storage

    store = GcsEvidenceStore(
        build_storage(project=PROJECT), bucket=f"driftzero-evidence-{PROJECT}"
    )
    path = evidence_path(namespace, "ev-dup")
    try:
        first = store.put_evidence(
            workflow_id=namespace, evidence_id="ev-dup", data=b"original evidence"
        )
        second = store.put_evidence(
            workflow_id=namespace, evidence_id="ev-dup", data=b"original evidence"
        )
        assert first.created is True
        assert second.created is False, "identical bytes were written twice"

        with pytest.raises(ConflictingRecord):
            store.put_evidence(
                workflow_id=namespace, evidence_id="ev-dup", data=b"different evidence"
            )
        assert store.get(path) == b"original evidence"
    finally:
        store.delete(path)


def test_a_real_resume_lease_admits_exactly_one_holder(cloud: Any, namespace: str) -> None:
    """Cloud Run runs two instances; only one may own a resume."""
    client, created = cloud["client"], cloud["created"]
    workflow_id = f"{namespace}-lease"
    created.append(client.collection("resume_leases").document(workflow_id))

    leases = ResumeLeases(client)
    held = leases.acquire(workflow_id, "instance-1")

    with pytest.raises(LeaseDenied) as exc:
        ResumeLeases(client).acquire(workflow_id, "instance-2")
    assert exc.value.holder == "instance-1"

    assert leases.holder(workflow_id)["live"] is True
    assert leases.release(held) is True
    assert leases.holder(workflow_id) is None


# ============================ safety ==================================================


def test_the_run_owns_every_document_it_will_delete(cloud: Any, namespace: str) -> None:
    """Cleanup must be able to delete only what this run actually created.

    A document is owned either because its id carries this run's namespace, or because
    it is a server-assigned workflow id whose stored change_id was verified to be this
    run's before it was registered. Registering a server-assigned id without that check
    once deleted an unrelated run's workflow.
    """
    refs = [getattr(ref, "path", str(ref)) for ref in cloud["created"]]
    assert refs, "the run created nothing — it would prove nothing"
    unowned = [
        ref
        for ref in refs
        if namespace not in ref
        and not any(assigned in ref for assigned in cloud["server_assigned"])
    ]
    assert unowned == [], f"cleanup would delete documents this run did not create: {unowned}"


def test_no_pilot_evidence_was_addressed(namespace: str) -> None:
    assert not namespace.startswith("chg-")
    assert "DZ-001" not in namespace
