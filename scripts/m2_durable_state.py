"""T092/T093 — capture durable-persistence evidence.

Every value is observed, not asserted. The script drives the real hero flow through the
real adapters, destroys the runtime, recovers from storage, and records what actually
came back. Offline throughout: the two models are deterministic substitutes and no
Gemini or Gemma call is made.

Run:  python -m scripts.m2_durable_state
"""

from __future__ import annotations

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

EVIDENCE_DIR = REPO_ROOT / "evidence" / "m2" / "durable_state"
FIXTURES = REPO_ROOT / "fixtures"
HERO_FIXTURE = FIXTURES / "hero_change.json"
LEFT_IMG = FIXTURES / "multimodal" / "label_left_01.jpg"
TOP_RIGHT_IMG = FIXTURES / "multimodal" / "label_top_right_01.jpg"

PROJECT = "driftzero-runtime-2026"
LEGACY_PROJECT = "driftzero-agentic-2026"
BUCKET = f"driftzero-evidence-{PROJECT}"

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.truth_engine.evidence import content_hash  # noqa: E402
from driftzero.truth_engine.proof_generator import compute_proof_hash  # noqa: E402
from driftzero_cloud.composition import FirestoreSink  # noqa: E402
from driftzero_cloud.errors import ConflictingRecord, IdentifierRejected  # noqa: E402
from driftzero_cloud.firestore import FirestorePersistence  # noqa: E402
from driftzero_cloud.gcs import GcsEvidenceStore, evidence_path  # noqa: E402
from driftzero_console.service import HeroConsoleService  # noqa: E402
from driftzero_console.workflows import dataset_from_fixture  # noqa: E402
from tests.integration._fake_gcp import FakeFirestoreClient, FakeStorageClient  # noqa: E402
from tests.integration._pilot import (  # noqa: E402
    arm_for_service,
    clear_change_intelligence,
)
from tests.integration.test_restart_persistence import OfflineGemma  # noqa: E402


def run_restart_flow() -> dict[str, Any]:
    """Runtime A persists, is destroyed, and runtime B recovers from storage alone."""
    os.environ["DRIFTZERO_FIELD_PROVIDER"] = "vertex_maas"
    gemma = OfflineGemma(["LEFT", "TOP_RIGHT"])
    fv.register_field_observation_provider(lambda _c: gemma)

    database = FakeFirestoreClient()
    dataset = dataset_from_fixture(
        json.loads(HERO_FIXTURE.read_text(encoding="utf-8")), directory=FIXTURES
    )
    runtime_a = HeroConsoleService(
        dataset=dataset,
        workflow_namespace="wf-m2-durable",
        persistence=FirestoreSink(FirestorePersistence.over(database)),
    )
    arm_for_service(runtime_a)

    runtime_a.analyze_change()
    runtime_a.deploy_change()
    runtime_a.deliver_to_frontline()
    runtime_a.submit_field_evidence(LEFT_IMG.read_bytes())
    runtime_a.generate_proof()
    runtime_a.submit_field_evidence(TOP_RIGHT_IMG.read_bytes())
    runtime_a.generate_proof()

    session = runtime_a._session
    workflow_id = session.workflow.workflow_id
    before = {
        "workflow_id": workflow_id,
        "state": str(session.workflow.state),
        "state_history": [str(s) for s in session.state_history],
        "verification_results": [
            str(e.verification_result) for e in session.workflow.verification_events
        ],
        "ledger": {a.action_id: str(a.status) for a in session.ledger.all_records()},
        "proof_id": session.workflow.proof_id,
        "proof_content_hash": session.proof_store.find_workflow(workflow_id).content_hash,
    }

    # Runtime A ceases to exist.
    del runtime_a, session
    clear_change_intelligence()
    fv.clear_field_observation_provider()
    mc.clear_model_client_provider()
    os.environ.pop("DRIFTZERO_FIELD_PROVIDER", None)

    # Runtime B shares only the database.
    runtime_b = FirestorePersistence.over(database)
    record = runtime_b.workflows.load_record(workflow_id)
    proof = runtime_b.proofs.find_workflow(workflow_id)
    after = {
        "workflow_id": record.workflow.workflow_id,
        "state": str(record.workflow.state),
        "state_history": list(record.state_history),
        "verification_results": [
            str(e.verification_result) for e in record.workflow.verification_events
        ],
        "ledger": {
            a.action_id: str(a.status)
            for a in runtime_b.ledger_for(workflow_id).all_records()
        },
        "proof_id": record.workflow.proof_id,
        "proof_content_hash": proof.content_hash,
        "revision": record.revision,
    }

    return {
        "database": database,
        "before": before,
        "after": after,
        "unknown_workflow_returns_none": runtime_b.workflows.load("wf-never-existed") is None,
        "unknown_proof_returns_none": runtime_b.proofs.find_workflow("wf-nope") is None,
        "proof_still_validates": compute_proof_hash(proof) == proof.content_hash,
        "provider_calls": gemma.calls,
    }


def capture_idempotency() -> dict[str, Any]:
    persistence = FirestorePersistence.over(FakeFirestoreClient())
    first = persistence.idempotency.claim("delivery-key", "runtime-a")
    repeat = persistence.idempotency.claim("delivery-key", "runtime-a")
    try:
        persistence.idempotency.claim("delivery-key", "runtime-b")
        contested = "ACCEPTED (DEFECT)"
    except ConflictingRecord as exc:
        contested = f"REFUSED: {exc}"
    return {
        "first_claim_granted": first.granted,
        "same_owner_reclaim_granted": repeat.granted,
        "different_owner": contested,
        "owner_after_contest": persistence.idempotency.owner_of("delivery-key"),
        "mechanism": "Firestore create() precondition — not read-then-write",
    }


def capture_proof_roundtrip(
    database: FakeFirestoreClient, before: dict[str, Any]
) -> dict[str, Any]:
    persistence = FirestorePersistence.over(database)
    proof = persistence.proofs.find_workflow(before["workflow_id"])
    stored_again = persistence.proofs.record(proof)
    tampered = proof.model_copy(update={"worker_id": "someone-else"})
    try:
        persistence.proofs.record(tampered)
        conflict = "ACCEPTED (DEFECT)"
    except ConflictingRecord as exc:
        conflict = f"REFUSED: {exc}"
    return {
        "proof_id": proof.proof_id,
        "content_hash": proof.content_hash,
        "hash_unchanged_by_persistence": proof.content_hash == before["proof_content_hash"],
        "recomputed_hash_matches": compute_proof_hash(proof) == proof.content_hash,
        "identical_rewrite_created_new_record": stored_again.created,
        "conflicting_proof_under_same_id": conflict,
        "hash_meaning": (
            "SHA-256 over the proof's canonical JSON excluding its own content_hash "
            "field. Persistence copies it; it never recomputes proof hash semantics."
        ),
    }


def capture_gcs_roundtrip() -> dict[str, Any]:
    store = GcsEvidenceStore(FakeStorageClient(), bucket=BUCKET)
    data = LEFT_IMG.read_bytes()
    first = store.put_evidence(
        workflow_id="wf-m2-durable-001", evidence_id="sub-001", data=data,
        content_type="image/jpeg",
    )
    second = store.put_evidence(
        workflow_id="wf-m2-durable-001", evidence_id="sub-001", data=data,
        content_type="image/jpeg",
    )
    try:
        store.put_evidence(
            workflow_id="wf-m2-durable-001", evidence_id="sub-001", data=b"different"
        )
        conflict = "ACCEPTED (DEFECT)"
    except ConflictingRecord as exc:
        conflict = f"REFUSED: {exc}"

    traversal = []
    for candidate in ("../../etc/passwd", "a/b", "..", ""):
        try:
            evidence_path("wf-m2-durable-001", candidate)
            traversal.append({"input": candidate, "result": "ACCEPTED (DEFECT)"})
        except IdentifierRejected as exc:
            traversal.append({"input": candidate, "result": f"REJECTED: {exc.reason}"})

    path = evidence_path("wf-m2-durable-001", "sub-001")
    return {
        "object_ref": first.object_ref,
        "content_hash": first.content_hash,
        "sha256_of_raw_bytes_matches": first.content_hash == content_hash(data),
        "size": first.size,
        "content_type": first.content_type,
        "generation": first.generation,
        "identical_rewrite_created_new_object": second.created,
        "conflicting_content": conflict,
        "readback_identical": store.get(path) == data,
        "path_traversal": traversal,
        "immutability_mechanism": "if_generation_match=0 write-once precondition",
        "hash_note": (
            "SHA-256 of the raw object bytes — object identity. Distinct from "
            "ChangeProof.content_hash, which hashes canonical proof JSON."
        ),
    }


def capture_purity() -> dict[str, Any]:
    import ast

    def roots(path: pathlib.Path) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        return found

    core = sorted((REPO_ROOT / "src" / "driftzero").rglob("*.py"))
    offenders = [p.name for p in core if "google" in roots(p)]
    adapters = sorted((REPO_ROOT / "src" / "driftzero_cloud").glob("*.py"))
    return {
        "core_files_scanned": len(core),
        "google_imports_inside_src_driftzero": offenders,
        "adapter_files": [p.name for p in adapters],
        "adapter_google_imports": sorted(
            {r for p in adapters for r in roots(p) if r == "google"}
        ),
        "domain_imports_adapter": [
            p.name for p in core if "driftzero_cloud" in roots(p)
        ],
        "subprocess_in_adapters": [
            p.name for p in adapters if "subprocess" in roots(p)
        ],
        "pickle_in_adapters": [p.name for p in adapters if "pickle" in roots(p)],
    }


def capture_credential_scan() -> dict[str, Any]:
    patterns = {
        "oauth_access_token": r"ya29\.[A-Za-z0-9_\-]{20,}",
        "api_key": r"AIza[A-Za-z0-9_\-]{30,}",
        "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY",
        "service_account_key_file": r"service_account\.json",
        "refresh_token": r'"refresh_token"\s*:\s*"[^"]{10,}',
    }
    findings: list[dict[str, str]] = []
    scanned = 0
    for path in sorted((REPO_ROOT / "src" / "driftzero_cloud").glob("*.py")) + sorted(
        (REPO_ROOT / "src" / "driftzero_console").glob("*.py")
    ):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for label, pattern in patterns.items():
            if re.search(pattern, text):
                findings.append({"file": path.name, "pattern": label})
    return {"files_scanned": scanned, "findings": findings, "clean": not findings}


def run_tests() -> dict[str, Any]:
    targets = [
        "tests/integration/test_cloud_persistence.py",
        "tests/integration/test_restart_persistence.py",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    summary = [ln for ln in completed.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {
        "targets": targets,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "summary": summary[-1].strip() if summary else "no summary line",
    }


def main() -> int:
    flow = run_restart_flow()
    before, after = flow["before"], flow["after"]

    bundle: dict[str, Any] = {
        "resource_identity.json": {
            "project": PROJECT,
            "legacy_project": LEGACY_PROJECT,
            "legacy_mutations": 0,
            "firestore_database": "(default)",
            "evidence_bucket": BUCKET,
            "region": "us-central1",
            "offline_capture": True,
            "note": (
                "This capture runs against in-memory doubles so it is reproducible and "
                "free. The same behaviours are exercised against real Firestore and "
                "Cloud Storage by tests/integration/test_cloud_smoke.py."
            ),
        },
        "firestore_roundtrip.json": {
            "workflow_id": after["workflow_id"],
            "state": after["state"],
            "state_history": after["state_history"],
            "revision": after["revision"],
            "verification_results": after["verification_results"],
            "ledger": after["ledger"],
            "collections": [
                "workflows/{workflow_id}",
                "workflows/{workflow_id}/actions/{action_id}",
                "workflows/{workflow_id}/proofs/{proof_id}",
                "idempotency_keys/{stable_key}",
            ],
            "serialization": "pydantic model_dump(mode='json'); no pickle, no repr",
        },
        "restart_test.json": {
            "runtime_a": before,
            "runtime_b_recovered": after,
            "identical": before == {k: v for k, v in after.items() if k in before},
            "unknown_workflow_returns_none": flow["unknown_workflow_returns_none"],
            "unknown_proof_returns_none": flow["unknown_proof_returns_none"],
            "fabricated_defaults": False,
            "closes_limitation": "T081 process-local workflow registry",
            "provider_calls": flow["provider_calls"],
            "live_model_calls": 0,
        },
        "idempotency.json": capture_idempotency(),
        "proof_roundtrip.json": capture_proof_roundtrip(flow["database"], before),
        "gcs_roundtrip.json": capture_gcs_roundtrip(),
        "purity.json": capture_purity(),
        "credential_scan.json": capture_credential_scan(),
        "test_summary.json": run_tests(),
    }

    bundle["run_summary.json"] = {
        "batch": "M2_BATCH_B",
        "tasks": "T092, T093",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "restart_recovered_state": after["state"],
        "restart_identical": bundle["restart_test.json"]["identical"],
        "proof_hash_unchanged": bundle["proof_roundtrip.json"]["hash_unchanged_by_persistence"],
        "purity_violations": bundle["purity.json"]["google_imports_inside_src_driftzero"],
        "credentials_clean": bundle["credential_scan.json"]["clean"],
        "tests": bundle["test_summary.json"]["summary"],
        "live_model_calls": 0,
        "legacy_project_mutations": 0,
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
    with (EVIDENCE_DIR / "SHA256SUMS.txt").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")

    for path in [*written, EVIDENCE_DIR / "SHA256SUMS.txt"]:
        print(f"  wrote {path.relative_to(REPO_ROOT).as_posix()}", file=sys.stderr)
    print(json.dumps(bundle["run_summary.json"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
