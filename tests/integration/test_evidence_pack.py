"""T131–T135 — the judge-facing pack, validated automatically.

An evidence pack whose links rot is worse than no pack: it points a judge at a claim and
then fails to produce it. So every repository-relative reference in MANIFEST.json,
README.md, JUDGES_START_HERE.md and LIMITATIONS.md is resolved here, and every recorded
SHA-256 is re-checked against the bytes on disk.

The other half is honesty. These documents are the ones a reader will trust most, so they
are also the ones most worth checking for an overclaim.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = REPO_ROOT / "evidence"
MANIFEST = EVIDENCE / "MANIFEST.json"
README = EVIDENCE / "README.md"
JUDGES = EVIDENCE / "JUDGES_START_HERE.md"
LIMITATIONS = EVIDENCE / "LIMITATIONS.md"
FRONTLINE = EVIDENCE / "reports" / "frontline_minimums.json"

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)#]+)\)")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def frontline() -> dict[str, Any]:
    return json.loads(FRONTLINE.read_text(encoding="utf-8"))


def links(path: Path) -> list[str]:
    """Repository-relative link targets in a markdown file, excluding URLs."""
    return [
        target.strip()
        for target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8"))
        if not target.startswith(("http://", "https://", "mailto:"))
    ]


# ============================ T131 — frontline minimums ===============================


def test_the_frontline_report_exists_at_its_declared_path() -> None:
    assert FRONTLINE.is_file(), "T131 names evidence/reports/frontline_minimums.json"


def test_all_six_minimums_are_evaluated_independently(frontline: dict[str, Any]) -> None:
    ids = [c["id"] for c in frontline["criteria"]]
    assert ids == ["FSM-1", "FSM-2", "FSM-3", "FSM-4", "FSM-5", "FSM-6"]
    assert frontline["criteria_total"] == 6


def test_every_criterion_records_what_the_contract_requires(
    frontline: dict[str, Any],
) -> None:
    for criterion in frontline["criteria"]:
        assert criterion["requirement"], criterion["id"]
        assert criterion["status"] in {"PASS", "FAIL"}, criterion["id"]
        assert criterion["evidence"], criterion["id"]
        assert criterion["notes"], criterion["id"]
        assert "measurement" in criterion, criterion["id"]


def test_the_checklist_ran_against_the_deployed_surface(frontline: dict[str, Any]) -> None:
    """T131 says 'against the deployed surface', which is not the local instance."""
    surface = frontline["surface_under_test"]
    assert surface["kind"] == "DEPLOYED"
    assert surface["service"] == "driftzero-api"
    assert surface["revision"], "no serving revision recorded"
    assert all(status == 200 for status in surface["pages_served"].values())
    assert surface["unauthenticated_access"] == 403, "the surface must stay private"


def test_the_minimums_pass_with_no_unrecorded_exception(frontline: dict[str, Any]) -> None:
    assert frontline["criteria_passed"] == 6
    assert frontline["exceptions"] == []
    assert frontline["result"] == "PASS"


def test_the_report_claims_no_wcag_conformance(frontline: dict[str, Any]) -> None:
    blob = json.dumps(frontline).lower()
    assert "not a wcag conformance audit" in blob
    for overclaim in ("wcag compliant", "wcag conformant", "fully accessible",
                      "accessibility certified"):
        assert overclaim not in blob, overclaim


def test_the_report_records_real_measurements(frontline: dict[str, Any]) -> None:
    by_id = {c["id"]: c for c in frontline["criteria"]}
    assert by_id["FSM-1"]["measurement"]["max_horizontal_overflow_px"] == 0
    assert by_id["FSM-3"]["measurement"]["controls_without_an_accessible_name"] == 0
    assert by_id["FSM-6"]["measurement"]["smallest_touch_target_px"] >= 48


# ============================ T132/T133 — pack and manifest ===========================


def test_the_pack_entry_points_exist() -> None:
    for path in (MANIFEST, README, JUDGES, LIMITATIONS):
        assert path.is_file(), path.name


def test_every_indexed_artifact_resolves(manifest: dict[str, Any]) -> None:
    missing = [
        artifact["path"]
        for artifact in manifest["artifacts"]
        if not (REPO_ROOT / artifact["path"]).is_file()
    ]
    assert missing == [], f"the manifest points at files that do not exist: {missing}"


def test_every_recorded_hash_matches_the_bytes_on_disk(manifest: dict[str, Any]) -> None:
    """A manifest whose hashes have drifted is worse than one with no hashes."""
    for artifact in manifest["artifacts"]:
        path = REPO_ROOT / artifact["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == artifact["sha256"], artifact["path"]


def test_every_artifact_is_classified_and_carries_a_claim(manifest: dict[str, Any]) -> None:
    classes = set(manifest["evidence_classes"])
    for artifact in manifest["artifacts"]:
        assert artifact["evidence_class"] in classes, artifact["path"]
        assert artifact["claim"], artifact["path"]
        assert artifact["judge_relevance"], artifact["path"]
        assert artifact["task"], artifact["path"]


def test_the_evidence_classes_are_not_collapsed(manifest: dict[str, Any]) -> None:
    """Six distinct classes, each actually used — 'real' alone would say nothing."""
    expected = {
        "REAL_GOOGLE_CLOUD", "REAL_MAAS_EXECUTION", "HISTORICAL_LIVE_MODEL",
        "OFFLINE_DETERMINISTIC", "REAL_PHYSICAL_EVIDENCE", "DERIVED",
    }
    assert set(manifest["evidence_classes"]) == expected
    used = {a["evidence_class"] for a in manifest["artifacts"]}
    assert used == expected, f"declared but unused: {expected - used}"


def test_the_judge_priorities_are_all_indexed(manifest: dict[str, Any]) -> None:
    """The ten things a judge should be able to find quickly."""
    ids = {a["id"] for a in manifest["artifacts"]}
    for required in (
        "architecture",
        "cloud_run_deployment",
        "pubsub_authenticated_push",
        "durable_restart_recovery",
        "gemini_change_intelligence",
        "multimodal_evaluation",
        "real_camera_hero_run",
        "change_proof",
        "worker_surface_mobile",
        "m3_exit_gate",
    ):
        assert required in ids, required


def test_every_recorded_bundle_still_verifies(manifest: dict[str, Any]) -> None:
    assert manifest["bundles_total"] >= 14
    failed = [b["bundle"] for b in manifest["bundle_verification"] if not b["verified"]]
    assert failed == [], f"bundles no longer verify: {failed}"


def test_absent_slots_are_declared_rather_than_faked(manifest: dict[str, Any]) -> None:
    """quickstart names a fuller tree than exists. The gaps are named, not filled."""
    declared = {slot["path"] for slot in manifest["absent_slots"]}
    # cost_model.json is owned by a task that has not been executed, so it stays absent.
    assert "evidence/cost_model.json" in declared
    # geap_access_gate.json used to be declared absent. The access checks were since
    # run against the real account, so it is now an indexed artifact instead.
    assert "evidence/geap_access_gate.json" not in declared
    assert (REPO_ROOT / "evidence" / "geap_access_gate.json").is_file()
    for slot in manifest["absent_slots"]:
        assert len(slot["reason"]) > 40, slot["path"]
        assert not (REPO_ROOT / slot["path"]).exists(), (
            f"{slot['path']} is declared absent but exists"
        )


def test_the_geap_gate_defers_rather_than_simulating(manifest: dict[str, Any]) -> None:
    """plan.md: a component failing its access check is DEFERRED, never faked."""
    gate = json.loads(
        (REPO_ROOT / "evidence" / "geap_access_gate.json").read_text(encoding="utf-8")
    )
    assert gate["components_total"] == 6
    assert gate["nothing_simulated"] is True
    assert gate["core_workflow_geap_dependencies"] == 0
    for component in gate["components"]:
        assert component["result"] in {"DEFERRED", "DELIVERED"}
        assert component["reason"], component["component"]
        assert component["fallback_taken"], component["component"]
        assert component["core_workflow_depends_on_it"] is False


def test_model_armor_is_not_claimed_to_be_in_force(manifest: dict[str, Any]) -> None:
    """It is built and wired, and it cannot take effect. Both must be recorded."""
    gate = json.loads(
        (REPO_ROOT / "evidence" / "geap_access_gate.json").read_text(encoding="utf-8")
    )
    armor = next(c for c in gate["components"] if c["component"] == "Model Armor")
    assert armor["result"] == "DEFERRED"
    assert armor["template_created"] is True
    assert armor["wiring_implemented"] is True
    assert armor["screening_in_force"] is False
    assert "UNSUPPORTED_REQUEST_LOCATION" in json.dumps(armor)

    from driftzero.config import DriftZeroConfig

    assert DriftZeroConfig.from_env({}).screening.as_disclosure()["status"] == (
        "SCREENING_SKIPPED"
    )


def test_the_manifest_states_the_hash_boundary(manifest: dict[str, Any]) -> None:
    guarantee = manifest["hash_guarantee"]
    assert guarantee["algorithm"] == "SHA-256"
    assert "complete file bytes" in guarantee["covers"]
    assert "content identity" in guarantee["establishes"]
    for denied in ("a digital signature", "a trusted timestamp", "an attestation",
                   "non-repudiation"):
        assert denied in guarantee["does_not_establish"], denied
    assert "EXCLUDING its own content_hash" in guarantee[
        "not_the_same_as_change_proof_content_hash"
    ]


def test_the_pack_builder_validates_itself() -> None:
    """`--check` must fail if anything it indexes has gone missing."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "scripts.build_evidence_pack", "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stdout
    summary = json.loads(done.stdout)
    assert summary["missing_artifacts"] == []
    assert summary["failed_bundles"] == []


# ============================ link validation =========================================


@pytest.mark.parametrize("document", ["README.md", "JUDGES_START_HERE.md", "LIMITATIONS.md"])
def test_every_markdown_link_resolves(document: str) -> None:
    path = EVIDENCE / document
    broken = [
        target for target in links(path) if not (path.parent / target).resolve().exists()
    ]
    assert broken == [], f"{document} points at files that do not exist: {broken}"


def test_the_documents_cross_reference_each_other() -> None:
    judges = JUDGES.read_text(encoding="utf-8")
    assert "LIMITATIONS.md" in judges
    assert "MANIFEST.json" in judges
    assert "LIMITATIONS.md" in README.read_text(encoding="utf-8")


# ============================ honesty =================================================


def _denials_removed(text: str) -> str:
    """Strip sentences that DENY a claim, so a disclaimer is not read as the claim.

    Written after a page's own "is not a digital signature" tripped a search for
    "digital signature".
    """
    body = re.sub(r"\s+", " ", text.lower())
    for denial in re.findall(r"(?:is not|are not|not a|nor a|never a|no)[^.]*?\.", body):
        body = body.replace(denial, " ")
    return body


@pytest.mark.parametrize("document", ["JUDGES_START_HERE.md", "LIMITATIONS.md", "README.md"])
def test_no_judge_facing_document_overclaims(document: str) -> None:
    body = _denials_removed((EVIDENCE / document).read_text(encoding="utf-8"))
    for overclaim in (
        "production ready", "production-ready", "enterprise customers",
        "cryptographically signed", "digitally signed", "digital signature",
        "attestation", "non-repudiation", "blockchain", "trusted timestamp",
        "fully autonomous",
    ):
        assert overclaim not in body, f"{document} claims {overclaim!r}"


def test_the_judge_document_leads_with_product_language() -> None:
    """Internal milestone vocabulary belongs in an appendix, not the narrative."""
    body = JUDGES.read_text(encoding="utf-8")
    for internal in ("T080", "T094", "T097", "T101", "M0 ", "M1 ", "M2 ", "slice"):
        assert internal not in body, f"JUDGES_START_HERE.md exposes {internal!r}"
    for product in ("Change Intelligence", "Remediation", "Frontline Enablement",
                    "Field Verification", "Truth Engine", "Change Proof"):
        assert product in body, product


def test_the_judge_document_states_the_thesis_and_the_runtime_mode() -> None:
    body = JUDGES.read_text(encoding="utf-8")
    assert "isn't deployed when the document changes" in body
    assert "It's deployed when the work changes" in body
    assert "CLOUD_PILOT" in body
    assert "production_ready: false" in body


def test_limitations_covers_every_item_its_task_names() -> None:
    """T135 lists a minimum set; each must actually be addressed."""
    # Whitespace is flattened first: markdown wraps, and "not per-agent IAM" is split
    # across a line break in the source.
    body = re.sub(r"\s+", " ", LIMITATIONS.read_text(encoding="utf-8").lower())
    required = {
        "application-level identity": "application-level",
        "not agent identity": "not agent identity",
        "not per-agent iam": "per-agent iam",
        "deferred geap": "geap",
        "model armor / screening skipped": "screening_skipped",
        "images not screened": "images are not screened",
        "operational not ledger immutability": "not an append-only ledger",
        "g1 route outcome": "g1",
        "non-binding engineering targets": "non-binding engineering targets",
    }
    for label, needle in required.items():
        assert needle in body, f"LIMITATIONS.md does not cover: {label}"


def test_limitations_separates_its_three_kinds() -> None:
    body = LIMITATIONS.read_text(encoding="utf-8")
    assert "## Current pilot limitations" in body
    assert "## Known non-blocking technical debt" in body
    assert "## Out of scope" in body


def test_limitations_records_the_measured_timeout_observation() -> None:
    """A specific, checkable number rather than a vague caveat."""
    body = LIMITATIONS.read_text(encoding="utf-8")
    assert "per-phase" in body
    assert "90" in body
    evaluation = json.loads((EVIDENCE / "reports" / "multimodal_eval.json").read_text("utf-8"))
    slowest = max(r["latency_seconds"] for r in evaluation["records"])
    assert slowest > 60, "the limitation claims a latency the evidence does not show"


# ============================ security ================================================


# Anchored on both sides. The G1 evidence embeds multi-megabyte base64 image payloads,
# and an unanchored search for a key prefix finds one by chance inside them — "AIza"
# turned up at offset 379,266 of a data:image/heic blob. Requiring a boundary and the
# real key length keeps the check meaningful instead of teaching everyone to ignore it.
_B64 = r"[A-Za-z0-9_\-]"
CREDENTIAL_PATTERNS = {
    # Google API key: AIza + exactly 35 characters.
    "api_key": rf"(?<!{_B64})AIza{_B64}{{35}}(?!{_B64})",
    # OAuth access token.
    "oauth_token": rf"(?<!{_B64})ya29\.{_B64}{{20,}}",
    # A JWT: three dot-separated base64url segments.
    "identity_token": rf"(?<!{_B64})eyJhbGciOi{_B64}+\.{_B64}+\.{_B64}+",
    "bearer_header": r"Authorization:\s*Bearer\s+\S{20,}",
    "private_key": r"BEGIN [A-Z ]*PRIVATE KEY",
    "client_secret": r'"client_secret"\s*:\s*"[^"]{10,}',
    "refresh_token": r'"refresh_token"\s*:\s*"[^"]{10,}',
}


def test_the_credential_detector_actually_detects() -> None:
    """A scanner that cannot fail is not a scanner.

    Anchoring the patterns to avoid base64 false positives could just as easily have
    disabled them, so each one is shown catching a realistic sample.
    """
    samples = {
        "api_key": "key=AIza" + "B" * 35 + " end",
        "oauth_token": "token ya29." + "c" * 40,
        "identity_token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "bearer_header": "Authorization: Bearer " + "d" * 40,
        "private_key": "-----BEGIN RSA PRIVATE KEY-----",
        "client_secret": '{"client_secret": "abcdefghijkl"}',
        "refresh_token": '{"refresh_token": "abcdefghijkl"}',
    }
    for label, pattern in CREDENTIAL_PATTERNS.items():
        assert re.search(pattern, samples[label]), f"{label} no longer detects anything"

    # And it must not fire inside a base64 blob, which is what tripped it originally.
    blob = "HHy" + "AIza" + "PwxG5lSryUbTURKMBcFqkj7XwRDOQJcArgqhPZlkklx62pjemaVUd9"
    assert not re.search(CREDENTIAL_PATTERNS["api_key"], blob)


def test_no_credential_appears_anywhere_in_the_evidence_tree() -> None:
    findings = []
    for path in sorted(EVIDENCE.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        for label, pattern in CREDENTIAL_PATTERNS.items():
            if re.search(pattern.encode(), raw):
                findings.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{label}")
    assert findings == [], f"credential material in the evidence tree: {findings}"


def test_judge_facing_documents_expose_no_billing_identifiers() -> None:
    """Project ids and SA emails are architectural; billing and credit ids are not."""
    for document in ("JUDGES_START_HERE.md", "LIMITATIONS.md", "README.md",
                     "MANIFEST.json"):
        body = (EVIDENCE / document).read_text(encoding="utf-8")
        assert "017DAD" not in body, f"{document} exposes a billing account id"
        assert not re.search(r"\b[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b", body), document
        assert "Marketing - All things agentic" not in body, (
            f"{document} exposes a promotional credit identifier"
        )
