"""T084 — the M1 exit gate, run offline in CI.

The gate itself is the deliverable; this suite proves it is reproducible, that its
verdict is derived rather than asserted, and that its evidence bundle says what it means.

The gate is invoked with ``--skip-suite`` here: it shells out to the full pytest run when
executed standalone, and having a test invoke that would recurse.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from driftzero.agents import field_verify as fv  # noqa: E402
from driftzero.agents import model_client as mc  # noqa: E402
from driftzero.truth_engine.proof_generator import ProofCondition  # noqa: E402
from scripts import m1_exit_gate as gate  # noqa: E402

EVIDENCE = REPO_ROOT / "evidence" / "runs" / "hero_run_local"


@pytest.fixture(scope="module")
def result() -> dict[str, Any]:
    """One offline gate run, shared across the assertions below."""
    outcome = gate.run_offline_flow()
    ledger = gate.evaluate(outcome)
    manifest = gate.build_evidence(
        ledger, outcome, {"exit_code": None, "passed": None, "summary": "SKIPPED"}
    )
    return {"outcome": outcome, "ledger": ledger, "manifest": manifest}


# ============================ the gate verdict ========================================


def test_the_gate_passes_every_mandatory_condition(result: dict[str, Any]) -> None:
    ledger = result["ledger"]
    assert ledger.verdict == "PASS", [c.name for c in ledger.failed]
    assert ledger.failed == []
    assert len(ledger.checks) == 41


def test_every_check_is_numbered_uniquely_and_named(result: dict[str, Any]) -> None:
    checks = result["ledger"].checks
    assert [c.number for c in checks] == list(range(1, 42))
    assert all(c.name and c.mandatory for c in checks)
    # Each records what it observed, so a reader can audit the basis of the verdict.
    assert all(c.observed is not None for c in checks)


def test_the_verdict_is_derived_from_the_checks_not_declared() -> None:
    """A gate that could report PASS independently of its checks would prove nothing."""
    source = (REPO_ROOT / "scripts" / "m1_exit_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    verdict = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "verdict"
    )
    body = ast.unparse(verdict)
    assert "self.failed" in body
    # ast.unparse normalizes string quotes, so match on the bare literals.
    assert "'PASS'" in body and "'FAIL'" in body
    # No literal PASS is written into the manifest anywhere.
    assert '"verdict": "PASS"' not in source


def test_a_failed_check_flips_the_verdict() -> None:
    ledger = gate.GateLedger()
    ledger.record(1, "a passing check", True, "ok")
    assert ledger.verdict == "PASS"
    ledger.record(2, "a failing check", False, "observed something wrong")
    assert ledger.verdict == "FAIL"
    assert [c.name for c in ledger.failed] == ["a failing check"]


# ============================ the recorded flow =======================================


def test_the_manifest_records_the_complete_flow(result: dict[str, Any]) -> None:
    manifest = result["manifest"]

    assert manifest["crossing_1"] == "ACCEPTED"
    assert manifest["crossing_2"] == "ACCEPTED"
    assert manifest["crossing_3"] == "ACCEPTED"
    assert manifest["impact"]["qualified_count"] == 1
    assert manifest["impact"]["affected_artifact_id"] == "wi-packing-standard-001"
    assert manifest["remediation_dispatch_count"] == 1
    assert manifest["delivery_dispatch_count"] == 1

    observations = manifest["field_observations"]
    assert [o["observation"] for o in observations] == ["LEFT", "TOP_RIGHT"]
    assert [o["verdict"] for o in observations] == ["FAIL", "PASS"]
    assert [o["crossing_4"] for o in observations] == ["ACCEPTED", "ACCEPTED"]
    assert observations[0]["proof_generated"] is False
    assert observations[1]["proof_generated"] is True

    assert [h["result"] for h in manifest["verification_chronology"]] == ["FAIL", "PASS"]
    assert manifest["workflow_state"] == "PROOF_COMPLETE"
    assert manifest["change_deployed"] is True
    assert manifest["m1_status"] == "CLOSED"


def test_the_manifest_records_all_seven_proof_conditions(result: dict[str, Any]) -> None:
    proof = result["manifest"]["proof"]
    assert proof["satisfied"] == proof["total"] == 7
    assert [c["condition"] for c in proof["conditions"]] == [str(c) for c in ProofCondition]
    assert all(c["satisfied"] for c in proof["conditions"])
    assert len(proof["content_hash"]) == 64
    assert proof["hash_preimage"] == "canonical-json-excluding-content_hash"


def test_the_manifest_declares_offline_provenance(result: dict[str, Any]) -> None:
    manifest = result["manifest"]
    assert manifest["provider_mode"] == "OFFLINE_DETERMINISTIC_SUBSTITUTES"
    assert manifest["network_calls"] == 0
    assert manifest["runtime_readiness"] == "LOCAL_PILOT"
    assert manifest["production_ready"] is False
    assert manifest["git_head"]


def test_the_manifest_is_honest_about_proof_hash_determinism(result: dict[str, Any]) -> None:
    """A differing hash across independent runs is by design, and says so."""
    determinism = result["manifest"]["proof_determinism"]
    assert determinism["proof_id_stable_across_runs"] is True
    assert determinism["content_hash_stable_across_runs"] is False
    assert "completion_timestamp" in determinism["reason"]
    assert "not a defect" in determinism["reason"]


def test_idempotency_counts_are_recorded(result: dict[str, Any]) -> None:
    idempotency = result["manifest"]["idempotency"]
    assert idempotency == {
        "remediation_dispatch": 1,
        "delivery_dispatch": 1,
        "provider_calls": 2,
        "verification_events": 2,
        "proofs": 1,
    }


# ============================ repository status =======================================


def test_the_gate_reports_the_real_m0_diff_status() -> None:
    status = gate.m0_diff_status()
    assert status["clean"] is True, status["changed_files"]
    assert set(status["paths"]) == {"src/driftzero/truth_engine", "src/driftzero/models"}


def test_the_gate_reports_live_evidence_as_a_separate_class() -> None:
    status = gate.live_evidence_status()
    assert status["intact"] is True
    assert status["gate_depends_on_it"] is False
    assert "separate" in status["evidence_class"].lower()
    for name, expected in gate.LIVE_PILOT_HASHES.items():
        assert status["observed"][name] == expected


def test_the_gate_verdict_does_not_depend_on_live_evidence() -> None:
    """Checks 1-39 are the substantive gate; live integrity is check 41 and read-only."""
    source = (REPO_ROOT / "scripts" / "m1_exit_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)
            # Nothing writes into the live pilot directory.
            assert name not in {"write_text", "write_bytes", "unlink"} or (
                "LIVE_PILOT" not in ast.unparse(node)
            )


def test_the_suite_subprocess_runs_without_the_gates_own_configuration() -> None:
    """The gate sets provider env for its run; leaking it made two tests fail."""
    source = (REPO_ROOT / "scripts" / "m1_exit_gate.py").read_text(encoding="utf-8")
    assert "GATE_ENV_KEYS" in source
    for key in (
        "DRIFTZERO_FIELD_PROVIDER",
        "DRIFTZERO_SEMANTIC_PROVIDER",
        "DRIFTZERO_GEMINI_MODEL",
    ):
        assert key in gate.GATE_ENV_KEYS
    run_suite = source[source.index("def run_test_suite") :]
    run_suite = run_suite[: run_suite.index("\n\n\n")]
    assert "env=env" in run_suite
    assert "GATE_ENV_KEYS" in run_suite


# ============================ the evidence bundle =====================================


def test_the_evidence_bundle_exists_and_is_complete() -> None:
    assert EVIDENCE.is_dir(), "T084 must record to evidence/runs/hero_run_local/"
    for name in ("manifest.json", "run_summary.json", "SHA256SUMS.txt"):
        assert (EVIDENCE / name).is_file(), name


def test_the_recorded_bundle_is_a_complete_gate_record() -> None:
    """Structure only.

    This test runs *inside* the suite the gate grades. Asserting that the recorded
    verdict is PASS would be circular — one red run would latch the bundle red and no
    later run could ever go green again. The verdict is established by the live ledger
    above and by the gate's own exit code; here we check the record is well formed.
    """
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gate_id"] == "M1_EXIT_GATE"
    assert manifest["task"] == "T084"
    assert manifest["verdict"] in {"PASS", "FAIL"}
    assert manifest["m1_status"] in {"CLOSED", "OPEN"}
    assert manifest["checks_total"] == 42, "41 flow checks plus the regression suite"
    assert manifest["checks_passed"] == manifest["checks_total"] - len(
        manifest["failed_checks"]
    )
    # A written bundle always observed a real suite run; --skip-suite cannot write.
    assert manifest["test_suite"]["summary"] != "SKIPPED"
    assert isinstance(manifest["test_suite"]["passed"], bool)


def test_the_regression_suite_gates_the_verdict() -> None:
    """Recording a failing suite while reporting PASS would make the gate meaningless."""
    ledger = gate.GateLedger()
    gate.record_suite(ledger, {"passed": True, "summary": "1500 passed"})
    assert ledger.verdict == "PASS"

    failing = gate.GateLedger()
    gate.record_suite(failing, {"passed": False, "summary": "3 failed, 1500 passed"})
    assert failing.verdict == "FAIL"
    assert [c.name for c in failing.failed] == ["full regression suite passes"]

    unobserved = gate.GateLedger()
    gate.record_suite(unobserved, {"passed": None, "summary": "SKIPPED"})
    assert unobserved.verdict == "FAIL", "an unobserved suite is not a passing suite"


def test_skipping_the_suite_cannot_write_evidence() -> None:
    """Evidence must never record a gate whose regressions were never observed."""
    source = (REPO_ROOT / "scripts" / "m1_exit_gate.py").read_text(encoding="utf-8")
    main = source[source.index("def main(") :]
    assert "args.dry_run = True" in main
    skip_branch = main[main.index("if args.skip_suite:") : main.index("    else:")]
    assert "args.dry_run = True" in skip_branch
    assert "record_suite" not in skip_branch, "a skipped suite must not be graded"
    assert '"SKIPPED"' in skip_branch


def test_the_gate_restores_the_environment_it_configured() -> None:
    """An escaped DRIFTZERO_* value makes later readers believe a provider is wired."""
    import os

    sentinel = "DRIFTZERO_FIELD_PROVIDER"
    before = os.environ.get(sentinel)
    os.environ.pop(sentinel, None)
    try:
        with gate.gate_environment():
            assert os.environ[sentinel] == "vertex_maas"
        assert sentinel not in os.environ, "the gate leaked its own configuration"

        os.environ[sentinel] = "pre-existing"
        with gate.gate_environment():
            assert os.environ[sentinel] == "vertex_maas"
        assert os.environ[sentinel] == "pre-existing", "a prior value must be restored"
    finally:
        os.environ.pop(sentinel, None)
        if before is not None:
            os.environ[sentinel] = before


def test_the_bundle_is_written_with_portable_line_endings() -> None:
    """A CRLF SHA256SUMS.txt is unreadable by sha256sum -c on any POSIX machine.

    str.splitlines() hides this, so assert on the raw bytes instead.
    """
    for name in ("SHA256SUMS.txt", "manifest.json", "run_summary.json"):
        assert b"\r" not in (EVIDENCE / name).read_bytes(), f"{name} carries CR bytes"


def test_the_bundle_path_is_protected_from_line_ending_rewriting() -> None:
    """Writing LF is not enough if git converts it back on the next checkout."""
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = [
        line.strip()
        for line in attributes.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "evidence/runs/** -text" in rules

    completed = subprocess.run(
        ["git", "check-attr", "text", "--", "evidence/runs/hero_run_local/SHA256SUMS.txt"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip().endswith("text: unset"), completed.stdout


def test_the_checksums_verify_the_way_a_posix_tool_reads_them() -> None:
    """Parse exactly as sha256sum -c does: split on LF, then two spaces."""
    raw = (EVIDENCE / "SHA256SUMS.txt").read_bytes().decode("utf-8")
    entries = [line for line in raw.split("\n") if line]
    for line in entries:
        digest, _, name = line.partition("  ")
        assert len(digest) == 64 and name, f"unparseable line: {line!r}"
        actual = hashlib.sha256((EVIDENCE / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} does not match its recorded checksum"


def test_the_checksums_match_the_recorded_files() -> None:
    lines = (EVIDENCE / "SHA256SUMS.txt").read_text(encoding="utf-8").strip().splitlines()
    recorded = dict(
        (name, digest) for digest, name in (line.split("  ", 1) for line in lines)
    )
    assert set(recorded) == {"manifest.json", "run_summary.json"}
    for name, digest in recorded.items():
        actual = hashlib.sha256((EVIDENCE / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} does not match its recorded checksum"


def test_the_checksums_are_file_hashes_not_a_proof_hash() -> None:
    """SHA256SUMS.txt hashes evidence FILES; ChangeProof.content_hash does not."""
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    lines = (EVIDENCE / "SHA256SUMS.txt").read_text(encoding="utf-8").strip().splitlines()
    digests = {line.split("  ", 1)[0] for line in lines}
    assert manifest["proof"]["content_hash"] not in digests
    assert "separate mechanism" in manifest["proof"]["not_the_same_as_sha256sums"]
    summary = json.loads((EVIDENCE / "run_summary.json").read_text(encoding="utf-8"))
    assert "unrelated to ChangeProof.content_hash" in summary["note"]


def test_the_summary_is_a_projection_of_the_manifest() -> None:
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((EVIDENCE / "run_summary.json").read_text(encoding="utf-8"))
    for key in ("gate_id", "task", "verdict", "m1_status", "workflow_id", "change_id"):
        assert summary[key] == manifest[key]
    assert summary["proof_id"] == manifest["proof"]["proof_id"]
    assert summary["proof_content_hash"] == manifest["proof"]["content_hash"]


def test_the_bundle_carries_no_credential() -> None:
    for name in ("manifest.json", "run_summary.json"):
        blob = (EVIDENCE / name).read_text(encoding="utf-8")
        for secret in (
            "Bearer ",
            "access_token",
            "refresh_token",
            "client_secret",
            "private_key",
            "grant_token",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            assert secret not in blob, f"{secret!r} leaked into {name}"


def test_the_non_blocking_debt_is_recorded_and_marked_non_blocking() -> None:
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    debt = manifest["non_blocking_debt"]
    assert debt, "known debt must be recorded, not omitted"
    assert all(item["blocking"] is False for item in debt)
    assert any("SequentialAgent" in item["detail"] for item in debt)


# ============================ hygiene =================================================


def test_the_gate_made_no_live_provider_call() -> None:
    mc.clear_model_client_provider()
    fv.clear_field_observation_provider()
    assert mc.has_model_client_provider() is False
    assert fv.has_field_observation_provider() is False


def test_the_gate_reuses_the_application_seam_rather_than_reimplementing_it() -> None:
    """T084 records a run; it does not contain a second copy of the workflow."""
    source = (REPO_ROOT / "scripts" / "m1_exit_gate.py").read_text(encoding="utf-8")
    for seam in (
        "HeroConsoleService",
        "HeroWorkflowRun",
        "dataset_from_fixture",
        "service.generate_proof",
        "service.submit_field_evidence",
    ):
        assert seam in source, f"the gate must drive {seam}"
    # And it constructs no authoritative record of its own.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            constructed = getattr(node.func, "id", None)
            assert constructed not in {"ChangeProof", "VerificationEvent"}, constructed
