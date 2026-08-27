"""T092 + T093 — durable Firestore persistence and immutable GCS evidence.

Offline throughout: the adapters are exercised against in-memory doubles that model the
two preconditions correctness rests on (``create`` must not exist, ``if_generation_match=0``).
The same behaviours are confirmed against real Google Cloud by
``test_cloud_smoke.py``, which is skipped unless credentials are present.

No model is called anywhere in this file.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from driftzero.config import DriftZeroConfig, PersistenceConfig
from driftzero.models.action import ActionExecution, ActionStatus, ActionType
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.truth_engine.proof_generator import compute_proof_hash
from driftzero_cloud.errors import CloudAdapterError, ConflictingRecord, IdentifierRejected
from driftzero_cloud.firestore import (
    LEGACY_PROJECT,
    FirestorePersistence,
    FirestoreProofStore,
    build_client,
)
from driftzero_cloud.gcs import GcsEvidenceStore, evidence_path, proof_path
from driftzero_cloud.serialization import (
    DOCUMENT_SCHEMA_VERSION,
    decode_proof,
    decode_workflow_record,
    encode_action,
    encode_proof,
    encode_workflow,
    safe_identifier,
)
from tests.integration._fake_gcp import FakeFirestoreClient, FakeStorageClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


# ============================ fixtures ================================================


@pytest.fixture
def firestore() -> FirestorePersistence:
    return FirestorePersistence.over(FakeFirestoreClient())


@pytest.fixture
def evidence() -> GcsEvidenceStore:
    return GcsEvidenceStore(FakeStorageClient(), bucket="driftzero-evidence-test")


def make_workflow(**over: Any) -> Workflow:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    base: dict[str, Any] = {
        "workflow_id": "wf-durable-001",
        "change_id": "chg-2026-0817-0001",
        "source_version": "source:proc-warehouse-packing:v2",
        "state": WorkflowState.CHANGE_RECEIVED,
        "worker_id": "worker-77",
        "created_at": now,
        "updated_at": now,
        "data_classification": _classification(),
    }
    base.update(over)
    return Workflow(**base)


def _classification() -> Any:
    from driftzero.models.classification import ClassificationLabel, DataClassification

    return DataClassification(labels=[ClassificationLabel.SYNTHETIC], lineage=[])


def make_action(**over: Any) -> ActionExecution:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    base: dict[str, Any] = {
        "action_id": "act-remediate-abc123",
        "workflow_id": "wf-durable-001",
        "action_type": ActionType.REMEDIATE_ARTIFACT,
        "status": ActionStatus.PLANNED,
        "target_ref": "wi-packing-standard-001",
        "created_at": now,
        "updated_at": now,
    }
    base.update(over)
    return ActionExecution(**base)


# ============================ T092 · workflow persistence =============================


def test_a_workflow_survives_a_round_trip_unchanged(firestore: FirestorePersistence) -> None:
    workflow = make_workflow(
        state=WorkflowState.IMPACT_DETERMINED,
        affected_artifact_id="wi-packing-standard-001",
        impact_reason="requirement label_position changed",
        candidate_artifact_refs=["a", "b"],
    )
    firestore.workflows.save(workflow)

    loaded = firestore.workflows.load("wf-durable-001")
    assert loaded == workflow
    assert loaded is not workflow, "a round trip must reconstruct, not hand back the input"


def test_an_unknown_workflow_is_not_found_and_is_never_invented(
    firestore: FirestorePersistence,
) -> None:
    """The whole point of durable state: 'no record' and 'a fresh default' differ."""
    assert firestore.workflows.load("wf-never-created") is None
    assert firestore.workflows.load_record("wf-never-created") is None


def test_the_state_chronology_is_persisted_beside_the_aggregate(
    firestore: FirestorePersistence,
) -> None:
    """Workflow carries no state history; encoding the model alone would drop it."""
    history = ["CHANGE_RECEIVED", "IMPACT_DETERMINED", "REMEDIATION_APPLIED"]
    firestore.workflows.save(make_workflow(), state_history=history)

    record = firestore.workflows.load_record("wf-durable-001")
    assert record is not None
    assert list(record.state_history) == history


def test_a_stale_write_is_refused_rather_than_clobbering(
    firestore: FirestorePersistence,
) -> None:
    workflow = make_workflow()
    first = firestore.workflows.save(workflow)
    second = firestore.workflows.save(workflow)
    assert (first, second) == (1, 2), "each save advances the revision"

    # Simulate a writer that still believes revision 1 is current.
    from driftzero_cloud.firestore import WORKFLOWS, _save_workflow_checked

    ref = firestore.client.collection(WORKFLOWS).document("wf-durable-001")
    with pytest.raises(ConflictingRecord) as exc:
        _save_workflow_checked.to_wrap(
            firestore.client.transaction(),
            ref=ref,
            workflow=workflow,
            state_history=(),
            expected_revision=1,
        )
    assert "expected revision 1" in str(exc.value)
    assert "stored revision is 2" in str(exc.value)


def test_a_matching_revision_is_accepted(firestore: FirestorePersistence) -> None:
    from driftzero_cloud.firestore import WORKFLOWS, _save_workflow_checked

    workflow = make_workflow()
    firestore.workflows.save(workflow)
    ref = firestore.client.collection(WORKFLOWS).document("wf-durable-001")
    revision = _save_workflow_checked.to_wrap(
        firestore.client.transaction(),
        ref=ref,
        workflow=workflow,
        state_history=(),
        expected_revision=1,
    )
    assert revision == 2


# ============================ T092 · action ledger ====================================


def test_the_action_ledger_round_trips(firestore: FirestorePersistence) -> None:
    ledger = firestore.ledger_for("wf-durable-001")
    action = make_action()
    ledger.save(action)
    assert ledger.get("act-remediate-abc123") == action


def test_persisting_the_same_action_twice_yields_one_record(
    firestore: FirestorePersistence,
) -> None:
    """The document id is the action identity, so a re-save can never duplicate."""
    ledger = firestore.ledger_for("wf-durable-001")
    ledger.save(make_action())
    ledger.save(make_action(status=ActionStatus.ATTEMPTED, attempt_count=1))

    records = ledger.all_records()
    assert len(records) == 1
    assert records[0].status is ActionStatus.ATTEMPTED
    assert records[0].attempt_count == 1


def test_every_ledger_status_survives_persistence(firestore: FirestorePersistence) -> None:
    ledger = firestore.ledger_for("wf-durable-001")
    for index, status in enumerate(ActionStatus):
        ledger.save(make_action(action_id=f"act-{index}", status=status))
    stored = {a.action_id: a.status for a in ledger.all_records()}
    assert set(stored.values()) == set(ActionStatus)


def test_an_action_from_another_workflow_is_refused(
    firestore: FirestorePersistence,
) -> None:
    ledger = firestore.ledger_for("wf-durable-001")
    with pytest.raises(CloudAdapterError, match="belongs to workflow"):
        ledger.save(make_action(workflow_id="wf-somewhere-else"))


def test_the_ledger_does_not_decide_whether_a_retry_is_safe() -> None:
    """Firestore stores the reconciliation decision; it must never make one."""
    body = code_without_docstrings(SRC / "driftzero_cloud" / "firestore.py")
    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))
    for forbidden in (
        "reconcile_mutation",
        "can_transition",
        "adjudicate_field_verification",
        "ReconciliationOutcome",
    ):
        assert forbidden not in names, f"adapter references domain decision {forbidden!r}"


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def code_without_docstrings(path: Path) -> str:
    """Source with every docstring removed, so prose never satisfies a code assertion.

    Written the hard way after a docstring saying the adapter "never adjudicates a
    verdict" matched a search for the word "adjudicate".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            node.body = [n for n in node.body if not _is_docstring(n)] or [ast.Pass()]
    return ast.unparse(tree)


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


# ============================ T092 · proofs ===========================================


@pytest.fixture
def proof() -> Any:
    """A real ChangeProof from the frozen generator, not a hand-built stand-in."""
    from tests.integration._pilot import clear_change_intelligence  # noqa: F401

    return _generate_real_proof()


def _generate_real_proof() -> Any:
    from driftzero.truth_engine.proof_generator import generate_change_proof
    from tests.unit.truth_engine.test_data_lineage import make_proof_context

    return generate_change_proof(make_proof_context())


def test_a_proof_round_trips_with_an_identical_hash(
    firestore: FirestorePersistence, proof: Any
) -> None:
    stored = firestore.proofs.record(proof)
    assert stored.created is True

    loaded = firestore.proofs.resolve(proof.workflow_id, proof.proof_id)
    assert loaded == proof
    assert loaded.content_hash == proof.content_hash
    assert compute_proof_hash(loaded) == compute_proof_hash(proof)


def test_persistence_never_recomputes_a_different_proof_hash(
    firestore: FirestorePersistence, proof: Any
) -> None:
    original = proof.content_hash
    firestore.proofs.record(proof)
    loaded = firestore.proofs.resolve(proof.workflow_id, proof.proof_id)
    assert loaded.content_hash == original
    assert compute_proof_hash(loaded) == original, "the stored hash must still validate"


def test_recording_an_identical_proof_twice_is_idempotent(
    firestore: FirestorePersistence, proof: Any
) -> None:
    first = firestore.proofs.record(proof)
    second = firestore.proofs.record(proof)
    assert first.created is True
    assert second.created is False
    assert first.proof_ref == second.proof_ref


def test_a_differing_proof_under_the_same_id_is_refused(
    firestore: FirestorePersistence, proof: Any
) -> None:
    firestore.proofs.record(proof)
    tampered = proof.model_copy(update={"worker_id": "someone-else"})
    assert tampered.proof_id == proof.proof_id

    with pytest.raises(ConflictingRecord) as exc:
        firestore.proofs.record(tampered)
    assert "a different proof is already stored" in str(exc.value)

    survivor = firestore.proofs.resolve(proof.workflow_id, proof.proof_id)
    assert survivor == proof, "the original must survive a rejected overwrite"


def test_a_proof_is_findable_by_workflow(
    firestore: FirestorePersistence, proof: Any
) -> None:
    firestore.proofs.record(proof)
    assert firestore.proofs.find_workflow(proof.workflow_id) == proof
    assert firestore.proofs.find_workflow("wf-with-no-proof") is None


def test_a_corrupted_proof_document_fails_closed_on_read(proof: Any) -> None:
    document = encode_proof(proof)
    document["content_hash"] = "0" * 64
    with pytest.raises(CloudAdapterError, match="content_hash mismatch"):
        decode_proof(document)


def test_a_proof_document_with_edited_payload_fails_the_byte_comparison(
    proof: Any,
) -> None:
    document = encode_proof(proof)
    document["payload"]["worker_id"] = "edited-worker"
    with pytest.raises(CloudAdapterError, match="does not round-trip"):
        decode_proof(document)


# ============================ T092 · idempotency keys =================================


def test_a_key_is_claimed_exactly_once(firestore: FirestorePersistence) -> None:
    first = firestore.idempotency.claim("delivery-wf-1", "runtime-a")
    assert first.granted is True
    assert firestore.idempotency.owner_of("delivery-wf-1") == "runtime-a"


def test_the_same_owner_reclaiming_is_an_idempotent_success(
    firestore: FirestorePersistence,
) -> None:
    firestore.idempotency.claim("delivery-wf-1", "runtime-a")
    again = firestore.idempotency.claim("delivery-wf-1", "runtime-a")
    assert again.granted is False, "a re-claim did not take fresh ownership"


def test_two_writers_cannot_both_claim_one_key(firestore: FirestorePersistence) -> None:
    assert firestore.idempotency.claim("delivery-wf-1", "runtime-a").granted is True
    with pytest.raises(ConflictingRecord, match="already claimed by 'runtime-a'"):
        firestore.idempotency.claim("delivery-wf-1", "runtime-b")
    assert firestore.idempotency.owner_of("delivery-wf-1") == "runtime-a"


def test_an_unclaimed_key_has_no_owner(firestore: FirestorePersistence) -> None:
    assert firestore.idempotency.owner_of("never-claimed") is None


def test_the_claim_is_atomic_not_read_then_write() -> None:
    """A read-then-write lets two concurrent writers both observe 'absent'."""
    source = (SRC / "driftzero_cloud" / "firestore.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    claim = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "claim"
    )
    body = ast.unparse(claim)
    assert "ref.create(" in body, "the claim must use create's must-not-exist precondition"
    create_at = body.index("ref.create(")
    # Any read of the existing owner happens only in the AlreadyExists recovery path,
    # which is after the atomic attempt — never before it as a guard.
    assert "AlreadyExists" in body
    assert body.index("AlreadyExists") > create_at


# ============================ serialization ===========================================


def test_documents_are_json_compatible_and_carry_no_pickle(proof: Any) -> None:
    for document in (
        encode_workflow(make_workflow()),
        encode_action(make_action()),
        encode_proof(proof),
    ):
        # Round-trips through plain JSON: nothing Python-specific survived encoding.
        assert json.loads(json.dumps(document)) == document
        assert document["schema_version"] == DOCUMENT_SCHEMA_VERSION


def test_no_adapter_module_imports_pickle_or_uses_repr_encoding() -> None:
    for name in ("serialization.py", "firestore.py", "gcs.py", "composition.py"):
        source = (SRC / "driftzero_cloud" / name).read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert "pickle" not in roots, f"{name} imports pickle"
        assert "marshal" not in roots, f"{name} imports marshal"
        assert "shelve" not in roots, f"{name} imports shelve"


def test_an_unknown_schema_version_is_refused() -> None:
    document = encode_workflow(make_workflow())
    document["schema_version"] = DOCUMENT_SCHEMA_VERSION + 1
    with pytest.raises(CloudAdapterError, match="unsupported workflow document"):
        decode_workflow_record(document)


def test_a_document_of_the_wrong_kind_is_refused(proof: Any) -> None:
    with pytest.raises(CloudAdapterError, match="expected a 'workflow' document"):
        decode_workflow_record(encode_proof(proof))


# ============================ identifiers · path traversal ============================


@pytest.mark.parametrize(
    "value",
    ["../etc/passwd", "a/b", "..", "", ".", "wf/../..", "x" * 300, "-leading", "\x00null"],
)
def test_unsafe_identifiers_are_rejected(value: str) -> None:
    with pytest.raises(IdentifierRejected):
        safe_identifier(value, kind="workflow_id")


@pytest.mark.parametrize("value", ["wf-001", "act-generate_proof-74f20eb3", "a.b-c_d", "0"])
def test_identifiers_the_product_actually_mints_are_accepted(value: str) -> None:
    assert safe_identifier(value) == value


def test_traversal_cannot_escape_an_evidence_prefix() -> None:
    with pytest.raises(IdentifierRejected):
        evidence_path("wf-1", "../../../../etc/passwd")
    with pytest.raises(IdentifierRejected):
        evidence_path("../other-workflow", "ev-1")


def test_a_traversal_attempt_never_reaches_the_bucket(evidence: GcsEvidenceStore) -> None:
    with pytest.raises(IdentifierRejected):
        evidence.put_evidence(workflow_id="wf-1", evidence_id="../escape", data=b"x")
    client_objects = evidence._client.bucket("driftzero-evidence-test").objects
    assert client_objects == {}, "a rejected path must not have written anything"


# ============================ T093 · evidence objects =================================


def test_an_evidence_object_round_trips_with_its_metadata(
    evidence: GcsEvidenceStore,
) -> None:
    from driftzero.truth_engine.evidence import content_hash

    data = b"\x89PNG\r\n\x1a\n raw field evidence"
    stored = evidence.put_evidence(
        workflow_id="wf-durable-001", evidence_id="sub-001", data=data, content_type="image/png"
    )
    assert stored.object_ref == (
        "gs://driftzero-evidence-test/workflows/wf-durable-001/evidence/sub-001"
    )
    assert stored.content_hash == content_hash(data)
    assert stored.size == len(data)
    assert stored.content_type == "image/png"
    assert stored.generation is not None
    assert stored.created is True
    assert evidence.get(evidence_path("wf-durable-001", "sub-001")) == data


def test_storing_identical_evidence_twice_is_idempotent(
    evidence: GcsEvidenceStore,
) -> None:
    data = b"identical bytes"
    first = evidence.put_evidence(workflow_id="wf-1", evidence_id="ev-1", data=data)
    second = evidence.put_evidence(workflow_id="wf-1", evidence_id="ev-1", data=data)
    assert first.created is True
    assert second.created is False
    assert first.content_hash == second.content_hash


def test_differing_evidence_at_an_immutable_ref_is_refused(
    evidence: GcsEvidenceStore,
) -> None:
    evidence.put_evidence(workflow_id="wf-1", evidence_id="ev-1", data=b"original")
    with pytest.raises(ConflictingRecord, match="refusing to overwrite immutable evidence"):
        evidence.put_evidence(workflow_id="wf-1", evidence_id="ev-1", data=b"replacement")
    assert evidence.get(evidence_path("wf-1", "ev-1")) == b"original"


def test_a_missing_object_reads_as_none(evidence: GcsEvidenceStore) -> None:
    assert evidence.get(evidence_path("wf-1", "never-written")) is None
    assert evidence.describe(evidence_path("wf-1", "never-written")) is None


def test_evidence_must_be_bytes(evidence: GcsEvidenceStore) -> None:
    with pytest.raises(CloudAdapterError, match="evidence must be bytes"):
        evidence.put_evidence(workflow_id="wf-1", evidence_id="ev-1", data="a string")


def test_the_object_hash_is_not_the_change_proof_hash(
    evidence: GcsEvidenceStore, proof: Any
) -> None:
    """Two different hashes over two different preimages. Conflating them is the bug."""
    rendered = json.dumps(proof.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    stored = evidence.put_rendered_proof(proof_id=proof.proof_id, data=rendered)

    assert stored.object_ref.endswith(f"{proof.proof_id}/proof.json")
    assert stored.content_hash != proof.content_hash
    # The file hash is over bytes that CONTAIN content_hash; the proof hash excludes it.
    assert proof.content_hash.encode() in rendered
    assert stored.content_hash != compute_proof_hash(proof)


def test_the_adapter_never_touches_iam_or_mints_a_signed_url() -> None:
    body = code_without_docstrings(SRC / "driftzero_cloud" / "gcs.py")
    # Whole identifiers only: a substring search for "acl" also matches "dataclasses".
    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))
    for forbidden in (
        "generate_signed_url",
        "set_iam_policy",
        "make_public",
        "acl",
        "add_iam_policy_binding",
    ):
        assert forbidden not in names, f"gcs adapter uses {forbidden!r}"


# ============================ configuration & safety ==================================


def test_persistence_defaults_to_memory_so_offline_runs_never_reach_the_cloud() -> None:
    config = DriftZeroConfig.from_env({})
    assert config.persistence.backend == "memory"
    assert config.persistence.is_durable is False


def test_a_half_configured_durable_backend_fails_loudly() -> None:
    config = PersistenceConfig(backend="firestore")
    assert config.missing_settings()
    with pytest.raises(Exception, match="durable persistence requires"):
        config.validated()


def test_the_composition_root_returns_a_null_sink_when_unconfigured() -> None:
    from driftzero_cloud.composition import build_sink
    from driftzero_console.persistence import NullSink

    sink = build_sink(DriftZeroConfig.from_env({}))
    assert isinstance(sink, NullSink)
    assert sink.durable is False


def test_a_null_sink_never_claims_durability() -> None:
    from driftzero_console.persistence import NullSink

    sink = NullSink()
    assert sink.durable is False
    # It also must not silently swallow a call by pretending to store something.
    assert sink.record_workflow(make_workflow(), []) is None


@pytest.mark.parametrize("builder", ["firestore", "gcs"])
def test_a_client_cannot_be_opened_against_the_quarantined_legacy_project(
    builder: str,
) -> None:
    from driftzero_cloud import gcs as gcs_module

    build = build_client if builder == "firestore" else gcs_module.build_client
    with pytest.raises(CloudAdapterError, match="quarantined"):
        build(project=LEGACY_PROJECT)
    with pytest.raises(CloudAdapterError, match="requires an explicit project"):
        build(project="")


def test_no_adapter_shells_out_or_embeds_a_credential() -> None:
    for path in sorted((SRC / "driftzero_cloud").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        roots = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert "subprocess" not in roots, f"{path.name} imports subprocess"
        assert "os" not in roots or "environ" not in source, (
            f"{path.name} reads the environment directly instead of taking config"
        )
        for marker in ("ya29.", "AIza", "BEGIN PRIVATE KEY", "service_account.json"):
            assert marker not in source, f"{path.name} embeds {marker!r}"
        assert "gcloud " not in source.replace("gcloud_exceptions", ""), (
            f"{path.name} appears to shell to gcloud"
        )


def test_the_purity_boundary_still_holds() -> None:
    """T092/T093 must not have put a Google import inside src/driftzero/**."""
    offenders = [
        path.name
        for path in sorted((SRC / "driftzero").rglob("*.py"))
        if "google" in imported_roots(path)
    ]
    assert offenders == [], f"google imported inside the purity boundary: {offenders}"


def test_the_domain_never_imports_the_cloud_adapter() -> None:
    """The dependency arrow points one way: adapter -> domain."""
    offenders = [
        path.name
        for path in sorted((SRC / "driftzero").rglob("*.py"))
        if "driftzero_cloud" in imported_roots(path)
    ]
    assert offenders == [], f"domain code imports driftzero_cloud: {offenders}"


def test_the_pure_persistence_seam_imports_no_google_sdk() -> None:
    """service.py must stay importable with no cloud dependency installed."""
    source = (SRC / "driftzero_console" / "persistence.py").read_text(encoding="utf-8")
    assert "google" not in source
    service = (SRC / "driftzero_console" / "service.py").read_text(encoding="utf-8")
    assert "driftzero_cloud" not in service
    assert "google" not in service


def test_firestore_cannot_set_a_verdict_or_business_truth() -> None:
    """No adapter may name a verdict, a state, or an authorization decision."""
    for name in ("firestore.py", "gcs.py"):
        names = set(
            re.findall(
                r"[A-Za-z_][A-Za-z0-9_]*",
                code_without_docstrings(SRC / "driftzero_cloud" / name),
            )
        )
        for forbidden in (
            "VerificationResult",
            "WorkflowState",
            "ProofCondition",
            "CapabilityBroker",
            "ToolCapability",
            "compute_proof_hash",
            "generate_change_proof",
        ):
            assert forbidden not in names, f"{name} touches business truth via {forbidden!r}"


def test_the_proof_ref_shape_is_stable() -> None:
    assert (
        FirestoreProofStore.proof_ref("wf-1", "act-generate_proof-abc")
        == "workflows/wf-1/proofs/act-generate_proof-abc"
    )
    assert proof_path("act-generate_proof-abc") == "proofs/act-generate_proof-abc/proof.json"
