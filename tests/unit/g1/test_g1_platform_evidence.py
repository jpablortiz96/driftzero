"""G1 platform-evidence tests — the honesty boundary around a failed spike.

A feasibility spike that ends without a working model is a legitimate outcome. What is
*not* legitimate is letting that outcome read as success. Two specific ways that could
happen here, both guarded below:

1. **Presence mistaken for provenance.** Synthetic images sitting at the three required
   fixture paths must never discharge T064 or contribute to a GO verdict.
2. **Platform support mistaken for a working deployment.** ``PLATFORM_SUPPORTED`` and
   ``PLATFORM_ATTEMPTED`` are true; ``DEPLOYMENT_SUCCEEDED`` and ``INFERENCE_SUCCEEDED``
   are false. Collapsing the four into one another is how "we tried" becomes "it works".

Entirely offline: no subprocess, no network, no cloud SDK.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from g1_gemma_probe import (  # noqa: E402
    PHYSICAL_CAPTURE_METHOD,
    SYNTHETIC_COUNTERPARTS,
    ProbeRecord,
    ServingRoute,
    build_report,
    classify_fixture,
    collect_fixture_status,
    derive_outcome_flags,
    detect_generated_media,
    load_fixture_provenance,
    load_platform_session,
    qualifying_records,
)

FIXTURES_DIR = REPO_ROOT / "fixtures" / "multimodal"
SYNTHETIC_DIR = FIXTURES_DIR / "synthetic"
CANONICAL_NAMES = ("label_left_01.jpg", "label_top_right_01.jpg", "label_ambiguous_01.jpg")
EVIDENCE = REPO_ROOT / "evidence" / "g1_gemma_feasibility.json"
SESSION = REPO_ROOT / "evidence" / "g1_platform_session.json"

OUTCOME_FLAGS = (
    "PLATFORM_SUPPORTED",
    "PLATFORM_ATTEMPTED",
    "DEPLOYMENT_SUCCEEDED",
    "INFERENCE_SUCCEEDED",
)


@pytest.fixture
def session() -> dict:
    return load_platform_session(SESSION)


# ==================== the four outcomes are distinct ==================================


def test_all_four_outcome_flags_are_present(session: dict) -> None:
    flags = derive_outcome_flags(session)
    assert set(flags) == set(OUTCOME_FLAGS)


def test_platform_supported_and_attempted_are_true(session: dict) -> None:
    """Both are backed by real evidence: an admitted config and two real operations."""
    flags = derive_outcome_flags(session)
    assert flags["PLATFORM_SUPPORTED"]["value"] is True
    assert flags["PLATFORM_ATTEMPTED"]["value"] is True


def test_deployment_and_inference_are_false(session: dict) -> None:
    """No deployed model ever existed, so neither may be claimed."""
    flags = derive_outcome_flags(session)
    assert flags["DEPLOYMENT_SUCCEEDED"]["value"] is False
    assert flags["INFERENCE_SUCCEEDED"]["value"] is False


def test_every_flag_carries_a_basis(session: dict) -> None:
    for name, flag in derive_outcome_flags(session).items():
        assert flag["basis"].strip(), f"{name} asserts a value with no stated basis"


def test_platform_support_does_not_imply_deployment() -> None:
    """The flags are independent by construction, not derived from one another."""
    flags = derive_outcome_flags(
        {
            "outcome_flags": {
                "PLATFORM_SUPPORTED": {"value": True, "basis": "config admitted"},
                "PLATFORM_ATTEMPTED": {"value": True, "basis": "two operations"},
            }
        }
    )
    assert flags["PLATFORM_SUPPORTED"]["value"] is True
    assert flags["DEPLOYMENT_SUCCEEDED"]["value"] is False
    assert flags["INFERENCE_SUCCEEDED"]["value"] is False


def test_missing_platform_session_fails_closed() -> None:
    """No evidence means no claim — never an optimistic default."""
    flags = derive_outcome_flags({})
    assert all(flags[name]["value"] is False for name in OUTCOME_FLAGS)


# ==================== presence is not provenance (T064) ===============================


def test_declared_synthetic_fixture_never_satisfies_t064() -> None:
    classification, satisfies = classify_fixture(
        "label_left_01.jpg",
        {
            "label_left_01.jpg": {
                "classification": ["SYNTHETIC"],
                "capture_method": "GENERATED_IMAGE",
                "satisfies_t064_physical_capture": False,
            }
        },
    )
    assert classification == "SYNTHETIC"
    assert satisfies is False


def test_undeclared_fixture_fails_closed() -> None:
    classification, satisfies = classify_fixture("label_left_01.jpg", {})
    assert classification == "UNDECLARED"
    assert satisfies is False


def test_a_real_label_alone_does_not_satisfy_t064() -> None:
    """Claiming REAL while admitting a generated capture method must not pass."""
    _, satisfies = classify_fixture(
        "x.jpg",
        {
            "x.jpg": {
                "classification": ["REAL"],
                "capture_method": "GENERATED_IMAGE",
                "satisfies_t064_physical_capture": True,
            }
        },
    )
    assert satisfies is False


def test_only_a_real_physical_camera_capture_satisfies_t064() -> None:
    _, satisfies = classify_fixture(
        "x.jpg",
        {
            "x.jpg": {
                "classification": ["REAL"],
                "capture_method": PHYSICAL_CAPTURE_METHOD,
                "satisfies_t064_physical_capture": True,
            }
        },
    )
    assert satisfies is True


def test_no_generated_media_may_hold_a_canonical_real_physical_path() -> None:
    """Files may sit at these paths, but generated media can never qualify there.

    Files ARE currently present at all three canonical paths (a rejected submission);
    what matters is that none of them satisfies T064.
    """
    status = collect_fixture_status(FIXTURES_DIR)
    for name in CANONICAL_NAMES:
        assert status["required"][name]["satisfies_t064_physical_capture"] is False, name


def test_missing_real_fixture_paths_fail_closed(tmp_path: Path) -> None:
    """Absence is not neutral: an empty canonical directory leaves T064 unsatisfied."""
    status = collect_fixture_status(tmp_path)
    assert status["all_present"] is False
    assert status["physical_capture_satisfied"] is False
    for name, entry in status["required"].items():
        assert entry["present"] is False, name
        assert entry["satisfies_t064_physical_capture"] is False, name


def test_synthetic_counterparts_exist_under_a_distinct_directory_and_name() -> None:
    """The generated images still exist — parked where they cannot be mistaken."""
    for canonical, relative in SYNTHETIC_COUNTERPARTS.items():
        counterpart = FIXTURES_DIR / relative
        assert counterpart.exists(), relative
        assert counterpart.name != canonical, "a synthetic file must not reuse a canonical name"
        assert counterpart.parent == SYNTHETIC_DIR


def test_synthetic_directory_declares_every_image_synthetic() -> None:
    provenance = load_fixture_provenance(SYNTHETIC_DIR)
    assert provenance, "the synthetic directory must carry its own provenance manifest"
    for name, entry in provenance.items():
        classification, satisfies = classify_fixture(name, provenance)
        assert classification == "SYNTHETIC", name
        assert satisfies is False, name
        assert entry["capture_method"] == "GENERATED_IMAGE"


def test_directory_placement_alone_cannot_convert_generated_media_into_real(
    tmp_path: Path,
) -> None:
    """Copying a generated image onto a canonical path must change nothing.

    This is the exact mistake that previously cleared the T064 blocker in silence.
    """
    canonical = tmp_path / "label_left_01.jpg"
    canonical.write_bytes((SYNTHETIC_DIR / "label_left_synthetic_01.jpg").read_bytes())
    for sibling in ("label_top_right_01.jpg", "label_ambiguous_01.jpg"):
        (tmp_path / sibling).write_bytes(b"generated")

    status = collect_fixture_status(tmp_path)
    assert status["all_present"] is True, "the files really are sitting at canonical paths"
    assert status["physical_capture_satisfied"] is False
    assert status["provenance_manifest_present"] is False


def test_provenance_alone_cannot_convert_generated_media_into_real(tmp_path: Path) -> None:
    """A declaration cannot promote generated media, even a maximally assertive one."""
    for name in CANONICAL_NAMES:
        (tmp_path / name).write_bytes(b"generated")
    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "fixtures": {
                    name: {
                        # Claims REAL and the physical capture method, but still admits
                        # SYNTHETIC. The SYNTHETIC label alone is disqualifying.
                        "classification": ["REAL", "SYNTHETIC"],
                        "capture_method": PHYSICAL_CAPTURE_METHOD,
                        "satisfies_t064_physical_capture": True,
                    }
                    for name in CANONICAL_NAMES
                }
            }
        ),
        encoding="utf-8",
    )
    status = collect_fixture_status(tmp_path)
    assert status["all_present"] is True
    assert status["physical_capture_satisfied"] is False


def test_a_synthetic_label_is_disqualifying_on_its_own() -> None:
    _, satisfies = classify_fixture(
        "x.jpg",
        {
            "x.jpg": {
                "classification": ["REAL", "SYNTHETIC"],
                "capture_method": PHYSICAL_CAPTURE_METHOD,
                "satisfies_t064_physical_capture": True,
            }
        },
    )
    assert satisfies is False


# ==================== the verdict cannot be reached by accident =======================


def _perfect_records(classification: str = "SYNTHETIC") -> list[ProbeRecord]:
    """Three fixtures, every answer correct — the best case a probe run could produce."""
    return [
        ProbeRecord(
            fixture_id=name,
            fixture_classification=classification,
            expected_observation=expected,
            raw_output=expected,
            normalized_output=expected,
            normalization_succeeded=True,
            latency_seconds=0.5,
            latency_label="ACTUAL_OBSERVED",
            matched_expected=True,
            attempt=1,
        )
        for name, expected in (
            ("label_left_01.jpg", "LEFT"),
            ("label_top_right_01.jpg", "TOP_RIGHT"),
            ("label_ambiguous_01.jpg", "INCONCLUSIVE"),
        )
    ]


def test_perfect_results_on_synthetic_fixtures_still_cannot_reach_go(session: dict) -> None:
    """The regression this file exists for.

    Before the provenance gate, three synthetic images at the required paths silently
    cleared the T064 blocker; a lucky 3-for-3 run would then have emitted GO off
    generated pictures. It must not, no matter how good the results look.
    """
    report = build_report(
        ServingRoute.MODEL_GARDEN,
        "https://example.invalid",
        FIXTURES_DIR,
        _perfect_records(),
        session=session,
        offline=True,
    )
    assert report.verdict == "NOT_YET_DECIDABLE"
    assert any("T064" in reason for reason in report.verdict_reasoning)


def test_verdict_is_not_decidable_and_names_every_open_blocker(session: dict) -> None:
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    assert report.verdict == "NOT_YET_DECIDABLE"
    joined = " | ".join(report.verdict_reasoning)
    for expected in ("T063", "T064", "T066"):
        assert expected in joined, f"{expected} is unresolved but not named as a blocker"


def test_no_inference_record_exists(session: dict) -> None:
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    assert report.records == []
    assert report.stability == {}


# ==================== quota is unknown, not invented (T063) ===========================


def test_effective_gpu_quota_is_recorded_as_unknown(session: dict) -> None:
    quota = session["quota_findings"]
    assert quota["resolved"] is False
    family = quota["cloud_quotas_family"]
    assert family["gpu_family"] == "NVIDIA_RTX_PRO_6000"
    assert family["family_entry_present"] is True
    assert family["applicable_locations_include_us_central1"] is True
    assert family["details"] == {}
    assert family["details_value_present"] is False
    assert family["effective_numeric_quota"] == "UNKNOWN_NOT_RETURNED"


def test_no_numeric_rtx_quota_is_asserted_anywhere(session: dict) -> None:
    """Neither >= 1 nor 0 may be claimed for the accelerator the route requires."""
    quota = json.dumps(session["quota_findings"])
    assert "UNKNOWN_NOT_RETURNED" in quota
    family = session["quota_findings"]["cloud_quotas_family"]
    assert "value" not in family["details"]
    assert not isinstance(family["effective_numeric_quota"], (int, float))


def test_the_l4_limit_is_not_treated_as_satisfying_the_selected_route(session: dict) -> None:
    """An L4 quota of 1 says nothing about the RTX PRO 6000 the chosen route needs."""
    legacy = session["quota_findings"]["legacy_compute_regional_listing"]
    assert legacy["rtx_pro_6000_metric_present"] is False
    assert legacy["adjacent_observation"]["relevance"] == "NOT_APPLICABLE_TO_SELECTED_ROUTE"


def test_unresolved_quota_blocks_the_gate(session: dict) -> None:
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    assert any("T063" in reason for reason in report.verdict_reasoning)


# ==================== deployment attempts and billing =================================


def test_both_deployment_attempts_are_recorded_as_failures(session: dict) -> None:
    attempts = session["deployment_attempts"]
    assert len(attempts) == 2
    for attempt in attempts:
        assert attempt["deployment_succeeded"] is False
        assert attempt["inference_performed"] is False
    assert attempts[0]["error_code"] == 13
    assert attempts[0]["error_message"] == "INTERNAL"
    assert attempts[1]["observed_endpoint_state"]["deployedModels"] is None


def test_an_endpoint_without_a_deployed_model_is_not_a_deployment(session: dict) -> None:
    """Attempt 2 created an endpoint resource. Nothing was ever servable on it."""
    attempt = session["deployment_attempts"][1]
    assert attempt["endpoint_created"]
    assert attempt["observed_endpoint_state"]["deployedModels"] is None
    assert attempt["deployment_succeeded"] is False


def test_billing_is_recorded_as_an_intentional_safety_state(session: dict) -> None:
    billing = session["billing_state"]
    assert billing["billing_enabled"] is False
    assert billing["intentional"] is True
    assert billing["not_an_infrastructure_defect"] is True
    assert billing["remediation_required_now"] is False


def test_billing_is_an_operational_hold_not_a_decision_blocker(session: dict) -> None:
    """A deliberate financial control must never be filed as technical infeasibility."""
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)

    holds = {hold["code"]: hold for hold in report.operational_holds}
    assert "BILLING_DISABLED" in holds
    hold = holds["BILLING_DISABLED"]
    assert hold["intentional"] is True
    assert hold["financial_safety_control"] is True
    assert hold["infrastructure_defect"] is False
    assert hold["blocks_g1_decision"] is False

    codes = [blocker["code"] for blocker in report.decision_blockers]
    assert not any("BILLING" in code for code in codes)
    assert not any("billing" in reason.lower() for reason in report.verdict_reasoning)


def test_billing_hold_is_not_evidence_of_model_infeasibility(session: dict) -> None:
    """Billing being off says nothing about whether Gemma can do the task."""
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    hold = next(h for h in report.operational_holds if h["code"] == "BILLING_DISABLED")
    assert "not evidence of model infeasibility" in hold["detail"]
    assert report.outcome_flags["PLATFORM_SUPPORTED"]["value"] is True


# ==================== blocker taxonomy ================================================


def test_decision_blockers_use_the_four_canonical_codes(session: dict) -> None:
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    codes = [blocker["code"] for blocker in report.decision_blockers]
    assert codes == [
        "T063_QUOTA",
        "T064_PHYSICAL_FIXTURES",
        "T066_DEPLOYMENT",
        "T066_INFERENCE",
    ]


def test_blocker_codes_are_unique(session: dict) -> None:
    """The two T066 concerns are distinct codes, never two identical entries."""
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    codes = [blocker["code"] for blocker in report.decision_blockers]
    assert len(codes) == len(set(codes))


def test_t062_is_complete_and_never_reported_as_a_blocker(session: dict) -> None:
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    for blocker in report.decision_blockers:
        assert "T062" not in blocker["code"]
        assert blocker["task"] != "T062"
    assert report.route_decision["decided"] is True


def test_every_blocker_names_a_task_and_a_detail(session: dict) -> None:
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    for blocker in report.decision_blockers:
        assert blocker["task"].startswith("T0")
        assert blocker["summary"].strip()
        assert len(blocker["detail"]) > 40, blocker["code"]


# ==================== qualifying records ==============================================


def test_synthetic_records_never_qualify() -> None:
    assert qualifying_records(_perfect_records("SYNTHETIC")) == []


def test_undeclared_records_never_qualify() -> None:
    assert qualifying_records(_perfect_records("UNDECLARED")) == []


def test_only_real_records_qualify() -> None:
    assert len(qualifying_records(_perfect_records("REAL"))) == 3


def test_synthetic_results_leave_the_inference_blocker_standing(session: dict) -> None:
    """Even a full sweep of correct answers off generated images decides nothing."""
    report = build_report(
        ServingRoute.MODEL_GARDEN,
        "https://example.invalid",
        FIXTURES_DIR,
        _perfect_records("SYNTHETIC"),
        session=session,
        offline=True,
    )
    codes = [blocker["code"] for blocker in report.decision_blockers]
    assert "T066_INFERENCE" in codes
    assert report.verdict == "NOT_YET_DECIDABLE"


# ==================== provenance of every recorded fact ===============================


ALLOWED_PROVENANCE = {"MACHINE_CAPTURED_ARTIFACT", "OPERATOR_REPORTED", "EMPIRICALLY_OBSERVED"}


def test_every_recorded_fact_declares_its_provenance(session: dict) -> None:
    """Operator-reported facts must never be indistinguishable from captured artifacts."""
    for key in ("project", "verified_deployment_configuration"):
        assert session[key]["provenance"] in ALLOWED_PROVENANCE, key
    for attempt in session["deployment_attempts"]:
        assert attempt["provenance"] in ALLOWED_PROVENANCE
    access = session["model_access"]
    for key in ("eula_acceptance", "model_backend_access"):
        assert access[key]["provenance"] in ALLOWED_PROVENANCE, key


def test_machine_captured_facts_cite_an_artifact_that_exists(session: dict) -> None:
    entries = [session["project"], session["model_access"]["model_backend_access"]]
    for entry in entries:
        if entry["provenance"] in {"MACHINE_CAPTURED_ARTIFACT", "EMPIRICALLY_OBSERVED"}:
            assert (REPO_ROOT / entry["source_artifact"]).exists()


# ==================== T061: EULA and backend access are separate claims ===============


def test_eula_acceptance_is_operator_reported(session: dict) -> None:
    """The EULA evidence is the --accept-eula flag, which only the operator observed."""
    eula = session["model_access"]["eula_acceptance"]
    assert eula["provenance"] == "OPERATOR_REPORTED"
    assert "--accept-eula" in eula["evidence"]
    assert eula["source_artifact"] is None


def test_model_backend_access_is_empirically_observed(session: dict) -> None:
    """Distinct claim, distinct and stronger provenance tier."""
    access = session["model_access"]["model_backend_access"]
    assert access["provenance"] == "EMPIRICALLY_OBSERVED"
    assert "DeployOperationMetadata" in access["evidence"]
    assert "publishers/google/models/gemma4@gemma-4-12b-it" in access["evidence"]
    assert access["source_artifact"] == "evidence/g1_vertex_deploy_operation.json"


def test_the_untested_negative_case_is_not_claimed(session: dict) -> None:
    """We never ran a deploy without the EULA, so nothing may be asserted about it.

    The earlier wording claimed a missing licence "is rejected at submission and never
    produces such an operation". That negative case was never exercised.
    """
    blob = json.dumps(session)
    for unsupported in (
        "never produces",
        "is rejected at submission",
        "IMPLIED_BY_ACCEPTED_DEPLOY_REQUEST",
    ):
        assert unsupported not in blob, f"unsupported inference present: {unsupported}"
    assert "negative case was never exercised" in (
        session["model_access"]["eula_acceptance"]["not_established"]
    )


def test_model_access_states_what_it_does_not_prove(session: dict) -> None:
    access = session["model_access"]
    assert "not prove" in access["what_this_does_NOT_prove"].lower()
    for key in ("eula_acceptance", "model_backend_access"):
        assert access[key]["established"].strip()
        assert access[key]["not_established"].strip()


# ==================== T063: route supersession =======================================


def test_quota_requirement_targets_the_selected_route_accelerator(session: dict) -> None:
    requirement = session["quota_findings"]["requirement"]
    assert "NVIDIA_RTX_PRO_6000" in requirement
    assert "g4-standard-48" in requirement
    assert "us-central1" in requirement
    assert "another accelerator family does not satisfy" in requirement


def test_route_supersession_is_traceable(session: dict) -> None:
    supersession = session["quota_findings"]["route_supersession"]
    assert supersession["previous_candidate"] == "Cloud Run + NVIDIA L4"
    assert supersession["superseded_by"] == "Vertex AI Model Garden + NVIDIA RTX PRO 6000"
    assert supersession["product_requirements_changed"] is False
    assert supersession["artifacts_updated"]


def test_t063_task_text_names_the_selected_accelerator() -> None:
    """The authoritative task must not still point at the superseded L4 route."""
    tasks = (REPO_ROOT / "specs" / "001-hero-change-deployment" / "tasks.md").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in tasks.splitlines() if ln.startswith("- [ ] T063"))
    assert "NVIDIA_RTX_PRO_6000" in line
    assert "g4-standard-48" in line
    assert "MUST NOT satisfy" in line
    assert "Supersession" in line


def test_t063_remains_incomplete() -> None:
    tasks = (REPO_ROOT / "specs" / "001-hero-change-deployment" / "tasks.md").read_text(
        encoding="utf-8"
    )
    assert any(ln.startswith("- [ ] T063") for ln in tasks.splitlines())


# ==================== offline generation touches nothing ==============================


def test_offline_mode_runs_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard guarantee: regenerating evidence contacts nothing, billable or not."""
    import subprocess

    import g1_gemma_probe

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline evidence generation must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(g1_gemma_probe, "_run", explode)

    report = build_report(None, None, FIXTURES_DIR, [], offline=True)
    assert report.access_checks["mode"]["offline"] is True


# ==================== the written evidence file agrees ================================


def test_written_evidence_file_reflects_the_true_state() -> None:
    """The committed evidence document must not drift from these guarantees."""
    assert EVIDENCE.exists(), "run: python scripts/g1_gemma_probe.py --access-check-only --offline"
    doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert doc["verdict"] == "NOT_YET_DECIDABLE"
    assert doc["records"] == []
    assert doc["outcome_flags"]["PLATFORM_SUPPORTED"]["value"] is True
    assert doc["outcome_flags"]["PLATFORM_ATTEMPTED"]["value"] is True
    assert doc["outcome_flags"]["DEPLOYMENT_SUCCEEDED"]["value"] is False
    assert doc["outcome_flags"]["INFERENCE_SUCCEEDED"]["value"] is False
    assert doc["fixtures"]["physical_capture_satisfied"] is False
    assert doc["quota_findings"]["resolved"] is False
    assert doc["billing_state"]["billing_enabled"] is False
    assert len(doc["deployment_attempts"]) == 2

    codes = [blocker["code"] for blocker in doc["decision_blockers"]]
    assert codes == [
        "T063_QUOTA",
        "T064_PHYSICAL_FIXTURES",
        "T066_DEPLOYMENT",
        "T066_INFERENCE",
    ]
    holds = [hold["code"] for hold in doc["operational_holds"]]
    assert holds == ["BILLING_DISABLED"]
    # The canonical slots are empty again; both rejections live in history.
    assert doc["fixtures"]["physical_capture_satisfied"] is False
    assert doc["fixtures"]["all_present"] is False
    assert doc["fixtures"]["rejected_as_generated"] == []
    assert (
        doc["quota_findings"]["cloud_quotas_family"]["effective_numeric_quota"]
        == "UNKNOWN_NOT_RETURNED"
    )


def test_written_evidence_records_the_selected_route_and_variant() -> None:
    doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert doc["serving_route"] == "vertex_ai_model_garden"
    assert doc["model_variant"] == "gemma-4-12b"
    assert doc["route_decision"]["decided"] is True
    config = doc["verified_deployment_configuration"]
    assert config["machine_type"] == "g4-standard-48"
    assert config["accelerator_type"] == "NVIDIA_RTX_PRO_6000"
    assert config["accelerator_count"] == 1
    assert "pytorch-vllm-serve" in config["container_image"]


# ==================== planning reconciliation (G1 route discovery) ====================

TASKS_MD = REPO_ROOT / "specs" / "001-hero-change-deployment" / "tasks.md"
QUICKSTART_MD = REPO_ROOT / "specs" / "001-hero-change-deployment" / "quickstart.md"
RESEARCH_MD = REPO_ROOT / "specs" / "001-hero-change-deployment" / "research.md"


def _task_line(prefix: str) -> str:
    return next(
        line for line in TASKS_MD.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"- [ ] {prefix}") or line.startswith(f"- [x] {prefix}")
    )


def test_t102_consumes_the_g1_selected_route_not_a_hardcoded_one() -> None:
    line = _task_line("T102")
    assert "selected by G1" in line
    assert "NVIDIA_RTX_PRO_6000" in line
    assert "g4-standard-48" in line
    assert "nvidia-l4" not in line, "T102 must not hardcode the superseded accelerator"


def test_t102_is_gated_on_quota_and_restored_billing() -> None:
    line = _task_line("T102")
    assert "Do not provision until T063 is satisfied" in line
    assert "billing/cost authorization is explicitly restored" in line
    assert "depends T063, T067, T091" in line


def test_t066_depends_on_quota_verification() -> None:
    """No deployment without quota, therefore no qualifying inference without it."""
    assert "depends T063, T064, T065" in _task_line("T066")


def test_g1_dependency_chain_is_explicit() -> None:
    tasks = TASKS_MD.read_text(encoding="utf-8")
    assert "G1 serving-feasibility chain" in tasks
    chain = next(ln for ln in tasks.splitlines() if "G1 serving-feasibility chain" in ln)
    for token in ("T063", "T064", "T066", "T067", "T102"):
        assert token in chain
    assert "parallel" in chain, "T063/T064 independence must stay documented"


def test_g1_gate_tasks_remain_incomplete() -> None:
    """This reconciliation must not have advanced any gate."""
    for task in ("T063", "T064", "T066", "T067", "T068"):
        assert _task_line(task).startswith("- [ ] "), f"{task} must remain incomplete"


def test_quickstart_no_longer_prescribes_the_superseded_accelerator() -> None:
    quickstart = QUICKSTART_MD.read_text(encoding="utf-8")
    for line in quickstart.splitlines():
        if "nvidia-l4" in line:
            assert "superseded" in line.lower(), (
                f"L4 may only appear as an explicitly superseded candidate: {line[:90]}"
            )


def test_research_r008_marks_the_old_recommendation_superseded() -> None:
    research = RESEARCH_MD.read_text(encoding="utf-8")
    assert "**Recommended (SUPERSEDED — pre-G1 desk research)**" in research
    assert "**SELECTED (empirical, G1 T062)**" in research
    assert "NVIDIA_RTX_PRO_6000" in research


def test_cloud_run_vllm_is_not_promoted_to_a_fallback_route(session: dict) -> None:
    """It was never exercised and no artifact designates it a fallback."""
    status = session["planning_reconciliation"]["cloud_run_vllm_status"]
    assert status["designation"] == "SUPERSEDED_CANDIDATE"
    assert status["is_currently_selected_route"] is False
    assert status["is_formal_g1_fallback"] is False
    assert "T068" in status["note"], "the real fallback mechanism must be named"


def test_architectural_conflicts_are_reported_not_silently_edited(session: dict) -> None:
    reported = session["planning_reconciliation"]["reported_not_edited"]
    refs = " ".join(item["ref"] for item in reported["items"])
    for expected in ("architecture diagram", "topology", "service-account", "Cost Model"):
        assert expected in refs, f"unreported architectural conflict: {expected}"
    for item in reported["items"]:
        assert item["conflict"].strip()
    assert reported["recommended_owner_action"].strip()


def test_plan_and_contracts_were_not_edited_by_this_reconciliation(session: dict) -> None:
    """Architecture artifacts stay untouched; the conflicts are reported instead."""
    updated = " ".join(
        entry["ref"] for entry in session["planning_reconciliation"]["updated_stale_prescriptive"]
    )
    assert "plan.md" not in updated
    assert "contracts/" not in updated


def test_reconciliation_changed_no_product_requirement(session: dict) -> None:
    reconciliation = session["planning_reconciliation"]
    assert "No product requirement" in reconciliation["scope"]
    assert session["quota_findings"]["route_supersession"]["product_requirements_changed"] is False


# ==================== content evidence outranks declarations (T064) ==================

PNG_MAGIC = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])
REJECTED_SUBMISSION = REPO_ROOT / "evidence" / "g1_t064_rejected_submission.json"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def make_png(*chunks: bytes) -> bytes:
    """A structurally valid PNG so the chunk parser can actually walk it."""
    return (
        PNG_MAGIC
        + _png_chunk(b"IHDR", bytes(13))
        + b"".join(chunks)
        + _png_chunk(b"IEND", b"")
    )


def make_jpeg(*segments: bytes, scan_data: bytes = b"") -> bytes:
    """A structurally valid JPEG; ``scan_data`` lands after SOS, i.e. in pixel data."""
    body = b"".join(segments)
    if scan_data:
        body += bytes([0xFF, 0xDA]) + struct.pack(">H", 2) + scan_data
    return bytes([0xFF, 0xD8]) + body + bytes([0xFF, 0xD9])


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


# Generated media declared through a *recognized provenance structure*.
C2PA_GENERATED = make_png(
    _png_chunk(b"caBX", b"jumb" + b"trainedAlgorithmicMedia" + b"gpt-image")
)
GENERATED_JPEG = make_jpeg(
    jpeg_segment(0xEB, b"jumb" + b"c2pa" + b"trainedAlgorithmicMedia")
)
# A clean, EXIF-free photograph-shaped JPEG: no provenance structure at all.
CLEAN_JPEG_NO_EXIF = make_jpeg(jpeg_segment(0xE0, b"JFIF" + bytes(12)))


def _write_declared_real(directory: Path, names: tuple[str, ...], payload: bytes) -> None:
    """Place files and the strongest possible REAL declaration over them."""
    for name in names:
        (directory / name).write_bytes(payload)
    (directory / "provenance.json").write_text(
        json.dumps(
            {
                "fixtures": {
                    name: {
                        "classification": ["REAL"],
                        "capture_method": PHYSICAL_CAPTURE_METHOD,
                        "generated": False,
                        "satisfies_t064_physical_capture": True,
                    }
                    for name in names
                }
            }
        ),
        encoding="utf-8",
    )


def test_detector_flags_c2pa_trained_algorithmic_media(tmp_path: Path) -> None:
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(C2PA_GENERATED)
    finding = detect_generated_media(target, known_generated={})
    assert finding["is_generated"] is True
    assert "trainedAlgorithmicMedia" in finding["markers"]
    assert "gpt-image" in finding["markers"]


def test_container_extension_mismatch_alone_never_classifies_as_generated(
    tmp_path: Path,
) -> None:
    """Requirement 3. A PNG named .jpg is a content-type anomaly, not a generator claim."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    finding = detect_generated_media(target, known_generated={})
    assert finding["container_format"] == "PNG"
    assert finding["extension_matches_container"] is False
    assert finding["is_generated"] is False, "an anomaly must not become a verdict"
    assert finding["decisive_signals"] == []
    anomalies = {a["anomaly"] for a in finding["anomalies"]}
    assert "CONTAINER_EXTENSION_MISMATCH" in anomalies
    assert all(a["decisive"] is False for a in finding["anomalies"])


def test_missing_exif_alone_never_classifies_as_generated(tmp_path: Path) -> None:
    """Requirement 2. Real photos lose EXIF to messaging apps, editors, and exports."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 256)
    finding = detect_generated_media(target, known_generated={})
    assert finding["exif_present"] is False
    assert finding["is_generated"] is False
    assert finding["decisive_signals"] == []
    assert "NO_EXIF" in {a["anomaly"] for a in finding["anomalies"]}


def test_no_camera_metadata_signal_is_ever_decisive(tmp_path: Path) -> None:
    """Absence of Make/Model/DateTimeOriginal must never be a decisive signal."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 256)
    finding = detect_generated_media(target, known_generated={})
    decisive = {signal["signal"] for signal in finding["decisive_signals"]}
    assert decisive <= {"KNOWN_GENERATED_HASH_MATCH", "EMBEDDED_GENERATOR_PROVENANCE"}
    for forbidden in ("EXIF", "MAKE", "MODEL", "DATETIME", "MTIME", "EXTENSION"):
        assert not any(forbidden in signal for signal in decisive)


def test_detector_does_not_flag_a_plain_jpeg(tmp_path: Path) -> None:
    """The detector must not reject an ordinary photograph."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(b"\xff\xd8\xff\xe1" + b"Exif\x00\x00" + b"\x00" * 128)
    finding = detect_generated_media(target, known_generated={})
    assert finding["container_format"] == "JPEG"
    assert finding["extension_matches_container"] is True
    assert finding["exif_present"] is True
    assert finding["is_generated"] is False
    assert finding["markers"] == []


def test_detector_is_inert_on_a_missing_file(tmp_path: Path) -> None:
    finding = detect_generated_media(tmp_path / "absent.jpg", known_generated={})
    assert finding["inspected"] is False
    assert finding["is_generated"] is False


def test_a_real_declaration_cannot_override_generator_provenance(tmp_path: Path) -> None:
    """The decisive guarantee: bytes beat attestations.

    Even a declaration asserting REAL + PHYSICAL_CAMERA_CAPTURE + generated:false over
    files carrying C2PA trainedAlgorithmicMedia must leave T064 unsatisfied.
    """
    _write_declared_real(tmp_path, CANONICAL_NAMES, C2PA_GENERATED)
    status = collect_fixture_status(tmp_path)
    assert status["all_present"] is True
    assert status["provenance_manifest_present"] is True
    assert status["physical_capture_satisfied"] is False
    assert sorted(status["rejected_as_generated"]) == sorted(CANONICAL_NAMES)


def test_a_declaration_alone_still_satisfies_t064_for_clean_content(tmp_path: Path) -> None:
    """The gate must remain passable by a genuine capture — not closed forever."""
    _write_declared_real(
        tmp_path, CANONICAL_NAMES, b"\xff\xd8\xff\xe1" + b"Exif\x00\x00" + b"\x00" * 128
    )
    status = collect_fixture_status(tmp_path)
    assert status["physical_capture_satisfied"] is True
    assert status["rejected_as_generated"] == []


# ==================== the submitted files are the synthetic ones =====================


def test_rejected_submission_remains_auditable_after_cleanup() -> None:
    """Requirement 1. Deleting the files must not delete the record of why."""
    assert REJECTED_SUBMISSION.exists(), "the rejection history artifact is missing"
    doc = json.loads(REJECTED_SUBMISSION.read_text(encoding="utf-8"))
    assert doc["status"] == "REJECTED_NOT_A_PHYSICAL_CAPTURE"
    assert doc["record_type"] == "HISTORICAL_EVIDENCE"
    assert len(doc["submissions"]) == 3
    for entry in doc["submissions"]:
        assert entry["original_canonical_path"].startswith("fixtures/multimodal/label_")
        assert len(entry["sha256"]) == 64
        signals = {s["signal"] for s in entry["decisive_generated_media_signals"]}
        assert signals == {"KNOWN_GENERATED_HASH_MATCH", "EMBEDDED_GENERATOR_PROVENANCE"}
        assert entry["non_decisive_diagnostics"]
        assert all(a["decisive"] is False for a in entry["non_decisive_diagnostics"])


def test_rejected_submission_records_the_synthetic_counterpart_hashes() -> None:
    doc = json.loads(REJECTED_SUBMISSION.read_text(encoding="utf-8"))
    for entry in doc["submissions"]:
        counterpart = entry["known_synthetic_counterpart"]
        assert counterpart["hash_identical"] is True
        assert counterpart["sha256"] == entry["sha256"]
        assert (REPO_ROOT / counterpart["path"]).exists(), "the generated original is retained"


def test_rejected_submission_states_what_is_not_proof_of_generation() -> None:
    """The artifact must not re-introduce the invalid EXIF invariant."""
    doc = json.loads(REJECTED_SUBMISSION.read_text(encoding="utf-8"))
    disclaimers = " ".join(doc["what_is_NOT_proof_of_generation"]).lower()
    for topic in ("exif", "extension", "mtime", "raw bytes"):
        assert topic in disclaimers, f"missing disclaimer about {topic}"
    assert "must not establish real_physical" in doc["asymmetry_rule"].lower()


def test_rejected_submission_records_the_unchanged_outcome() -> None:
    """Requirement 12/13 as recorded history: nothing advanced."""
    outcome = json.loads(REJECTED_SUBMISSION.read_text(encoding="utf-8"))["outcome"]
    assert outcome["real_physical_fixtures_accepted"] == 0
    assert outcome["t064_remained_open"] is True
    assert outcome["g1_verdict_after"] == "NOT_YET_DECIDABLE"
    assert outcome["task_checkboxes_changed"] is False
    assert outcome["synthetic_fixtures_modified"] is False
    assert set(outcome["tasks_still_open"]) == {"T063", "T064", "T066", "T067", "T068"}


def test_rejection_history_is_not_read_as_current_state() -> None:
    """Historical evidence must be labelled so it cannot be mistaken for the present."""
    doc = json.loads(REJECTED_SUBMISSION.read_text(encoding="utf-8"))
    assert "MUST NOT be read as the current fixture state" in doc["record_type_warning"]
    manifest = json.loads((FIXTURES_DIR / "provenance.json").read_text(encoding="utf-8"))
    history = manifest["historical_rejections"][0]
    assert history["is_current_state"] is False
    assert history["evidence"] == "evidence/g1_t064_rejected_submission.json"


def test_no_canonical_path_holds_a_qualifying_fixture() -> None:
    """No generated media may ever *qualify* in a REAL_PHYSICAL slot.

    Files occupy these paths again (submission 02, rejected). The invariant is not that
    the slots are empty but that nothing in them discharges T064.
    """
    status = collect_fixture_status(FIXTURES_DIR)
    for name in CANONICAL_NAMES:
        assert status["required"][name]["satisfies_t064_physical_capture"] is False, name


def test_current_provenance_reports_awaiting_real_physical_capture() -> None:
    """Requirement 3. The manifest describes now, not the rejected submission."""
    manifest = json.loads((FIXTURES_DIR / "provenance.json").read_text(encoding="utf-8"))
    assert manifest["t064_status"] == "AWAITING_REAL_PHYSICAL_CAPTURE"
    assert manifest["physical_capture_satisfied"] is False
    assert manifest["all_present"] is False
    for name in CANONICAL_NAMES:
        entry = manifest["fixtures"][name]
        assert entry["status"] == "AWAITING_REAL_PHYSICAL_CAPTURE"
        assert entry["present"] is False
        assert entry["classification"] == []
        assert entry["capture_method"] == "NOT_YET_CAPTURED"
        assert entry["satisfies_t064_physical_capture"] is False


def test_no_current_canonical_fixture_is_classified_real() -> None:
    manifest = json.loads((FIXTURES_DIR / "provenance.json").read_text(encoding="utf-8"))
    for name, entry in manifest["fixtures"].items():
        assert "REAL" not in entry["classification"], name


def test_t064_is_unsatisfied_in_the_live_gate() -> None:
    """Not just the manifest — the computed gate agrees."""
    status = collect_fixture_status(FIXTURES_DIR)
    assert status["all_present"] is False
    assert status["physical_capture_satisfied"] is False
    assert status["rejected_as_generated"] == []


def test_synthetic_fixtures_survived_the_cleanup() -> None:
    """Only the canonical slots were cleared; the generated originals are retained."""
    for relative in SYNTHETIC_COUNTERPARTS.values():
        assert (FIXTURES_DIR / relative).exists(), relative



def test_t064_remains_incomplete() -> None:
    assert _task_line("T064").startswith("- [ ] ")


# ============ asymmetry: positive evidence disqualifies, clean content never qualifies ==


def test_clean_jpeg_without_exif_can_still_satisfy_t064(tmp_path: Path) -> None:
    """Requirement 1. EXIF-stripped photographs are ordinary and must remain usable."""
    _write_declared_real(tmp_path, CANONICAL_NAMES, b"\xff\xd8\xff\xe0" + b"JFIF" + b"\x00" * 256)
    status = collect_fixture_status(tmp_path)
    for name, entry in status["required"].items():
        assert entry["content_inspection"]["exif_present"] is False, name
        assert entry["content_inspection"]["is_generated"] is False, name
    assert status["physical_capture_satisfied"] is True
    assert status["rejected_as_generated"] == []


def test_known_synthetic_hash_rejects_despite_a_real_declaration(tmp_path: Path) -> None:
    """Requirement 4. Hash identity with recorded synthetic media is decisive."""
    payload = (SYNTHETIC_DIR / "label_left_synthetic_01.jpg").read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    _write_declared_real(tmp_path, CANONICAL_NAMES, payload)
    status = collect_fixture_status(tmp_path)
    assert status["physical_capture_satisfied"] is False
    finding = status["required"]["label_left_01.jpg"]["content_inspection"]
    assert finding["sha256"] == digest
    assert finding["known_generated_hash_match"]
    assert "KNOWN_GENERATED_HASH_MATCH" in {s["signal"] for s in finding["decisive_signals"]}


def test_trained_algorithmic_media_rejects_despite_a_real_declaration(
    tmp_path: Path,
) -> None:
    """Requirement 5. Embedded generator provenance is decisive on a valid JPEG too."""
    _write_declared_real(tmp_path, CANONICAL_NAMES, GENERATED_JPEG)
    status = collect_fixture_status(tmp_path)
    assert status["physical_capture_satisfied"] is False
    finding = status["required"]["label_left_01.jpg"]["content_inspection"]
    # No confounders: valid JPEG container, matching extension, no hash match.
    assert finding["container_format"] == "JPEG"
    assert finding["extension_matches_container"] is True
    assert finding["known_generated_hash_match"] is None
    assert {s["signal"] for s in finding["decisive_signals"]} == {
        "EMBEDDED_GENERATOR_PROVENANCE"
    }


def test_positive_generated_evidence_overrides_the_operator_declaration(
    tmp_path: Path,
) -> None:
    """Requirement 6. The strongest possible declaration still loses to the bytes."""
    _write_declared_real(tmp_path, CANONICAL_NAMES, GENERATED_JPEG)
    manifest = json.loads((tmp_path / "provenance.json").read_text(encoding="utf-8"))
    declaration = manifest["fixtures"]["label_left_01.jpg"]
    assert declaration["classification"] == ["REAL"]
    assert declaration["capture_method"] == PHYSICAL_CAPTURE_METHOD
    assert declaration["generated"] is False
    assert declaration["satisfies_t064_physical_capture"] is True

    status = collect_fixture_status(tmp_path)
    assert status["physical_capture_satisfied"] is False
    assert sorted(status["rejected_as_generated"]) == sorted(CANONICAL_NAMES)


def test_clean_content_alone_never_establishes_real_physical(tmp_path: Path) -> None:
    """Requirement 7. Absence of generated evidence is not presence of a photograph."""
    for name in CANONICAL_NAMES:
        (tmp_path / name).write_bytes(b"\xff\xd8\xff\xe0" + b"JFIF" + b"\x00" * 256)
    status = collect_fixture_status(tmp_path)
    assert status["all_present"] is True
    for entry in status["required"].values():
        assert entry["content_inspection"]["is_generated"] is False
    assert status["provenance_manifest_present"] is False
    assert status["physical_capture_satisfied"] is False, (
        "clean bytes must never qualify a fixture on their own"
    )


def test_a_synthetic_declaration_still_fails_on_clean_content(tmp_path: Path) -> None:
    """The declaration rules survive intact: SYNTHETIC stays disqualifying."""
    for name in CANONICAL_NAMES:
        (tmp_path / name).write_bytes(b"\xff\xd8\xff\xe0" + b"JFIF" + b"\x00" * 256)
    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "fixtures": {
                    name: {
                        "classification": ["SYNTHETIC"],
                        "capture_method": "GENERATED_IMAGE",
                        "satisfies_t064_physical_capture": False,
                    }
                    for name in CANONICAL_NAMES
                }
            }
        ),
        encoding="utf-8",
    )
    assert collect_fixture_status(tmp_path)["physical_capture_satisfied"] is False


# ============ producer names are decisive only inside a provenance structure ==========


def test_producer_name_in_raw_bytes_alone_is_not_generated_evidence(tmp_path: Path) -> None:
    """Requirement 7. Arbitrary raw-byte occurrence must not classify media."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(make_png() + b"gpt-image OpenAI Media Service midjourney")
    finding = detect_generated_media(target, known_generated={})
    assert finding["is_generated"] is False, "a raw-byte hit must not become a verdict"
    assert finding["markers"] == []
    assert finding["decisive_signals"] == []


def test_producer_name_in_pixel_data_is_not_generated_evidence(tmp_path: Path) -> None:
    """Requirement 8. Image content is not a provenance claim.

    A photograph may legitimately show a poster reading "midjourney", or carry the text
    in its scan data. That is picture content, not a content credential.
    """
    png_target = tmp_path / "label_left_01.jpg"
    png_target.write_bytes(make_png(_png_chunk(b"IDAT", b"OpenAI Media Service" * 4)))
    png_finding = detect_generated_media(png_target, known_generated={})
    assert png_finding["is_generated"] is False
    assert png_finding["provenance_structures"] == []

    jpeg_target = tmp_path / "label_top_right_01.jpg"
    jpeg_target.write_bytes(make_jpeg(scan_data=b"stable-diffusion imagen dall-e"))
    jpeg_finding = detect_generated_media(jpeg_target, known_generated={})
    assert jpeg_finding["is_generated"] is False
    assert jpeg_finding["markers"] == []


def test_producer_name_in_a_provenance_structure_is_decisive(tmp_path: Path) -> None:
    """The counterpart: the same string inside C2PA/JUMBF does disqualify."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(make_png(_png_chunk(b"caBX", b"jumb" + b"gpt-image")))
    finding = detect_generated_media(target, known_generated={})
    assert finding["is_generated"] is True
    assert finding["provenance_structures"] == ["PNG_caBX"]
    assert finding["generator_provenance_matches"][0]["structure"] == "PNG_caBX"


def test_raw_bytes_and_structure_are_distinguished_on_the_same_marker(
    tmp_path: Path,
) -> None:
    """Same producer name, two locations, opposite verdicts."""
    loose = tmp_path / "label_left_01.jpg"
    loose.write_bytes(make_png() + b"trainedAlgorithmicMedia")
    structured = tmp_path / "label_top_right_01.jpg"
    structured.write_bytes(make_png(_png_chunk(b"caBX", b"trainedAlgorithmicMedia")))
    assert detect_generated_media(loose, known_generated={})["is_generated"] is False
    assert detect_generated_media(structured, known_generated={})["is_generated"] is True


def test_a_declared_real_fixture_with_a_loose_producer_name_still_qualifies(
    tmp_path: Path,
) -> None:
    """A genuine photo of a poster reading "midjourney" must remain usable."""
    _write_declared_real(
        tmp_path, CANONICAL_NAMES, CLEAN_JPEG_NO_EXIF + b"midjourney poster in frame"
    )
    status = collect_fixture_status(tmp_path)
    assert status["physical_capture_satisfied"] is True
    assert status["rejected_as_generated"] == []


# ============ the gate as a whole, after cleanup ======================================


def test_g1_verdict_remains_not_yet_decidable(session: dict) -> None:
    """Requirement 12."""
    report = build_report(None, None, FIXTURES_DIR, [], session=session, offline=True)
    assert report.verdict == "NOT_YET_DECIDABLE"
    codes = [blocker["code"] for blocker in report.decision_blockers]
    assert "T064_PHYSICAL_FIXTURES" in codes


def test_all_open_g1_tasks_remain_open() -> None:
    """Requirement 13."""
    for task in ("T063", "T064", "T066", "T067", "T068"):
        assert _task_line(task).startswith("- [ ] "), f"{task} must remain open"
    assert _task_line("T065").startswith("- [x] "), "T065 stays complete"


# ============ submission 02: new files, rejected on embedded provenance alone =========

REJECTED_SUBMISSION_02 = REPO_ROOT / "evidence" / "g1_t064_rejected_submission_02.json"


def _submission_02() -> list[dict]:
    return json.loads(REJECTED_SUBMISSION_02.read_text(encoding="utf-8"))["submissions"]


def test_submission_02_files_are_not_known_synthetic() -> None:
    """The hash signal did NOT fire on submission 02 — these were genuinely new files.

    Read from the preserved artifact: the files themselves were deleted in cleanup, and
    the record is what must survive.
    """
    synthetic_hashes = {
        hashlib.sha256((SYNTHETIC_DIR / name).read_bytes()).hexdigest()
        for name in (
            "label_left_synthetic_01.jpg",
            "label_top_right_synthetic_01.jpg",
            "label_ambiguous_synthetic_01.jpg",
        )
    }
    for entry in _submission_02():
        assert entry["sha256"] not in synthetic_hashes
        assert entry["known_synthetic_hash_match"] is None


def test_submission_02_containers_were_valid_jpegs() -> None:
    """None of submission 01's anomalies applied: real JPEGs, matching extensions."""
    for entry in _submission_02():
        assert entry["container_format"] == "JPEG"
        assert entry["extension_matches_container"] is True


def test_submission_02_rejected_solely_on_embedded_generator_provenance() -> None:
    """One decisive signal, sourced from a parsed APP11/JUMBF structure."""
    for entry in _submission_02():
        signals = {s["signal"] for s in entry["decisive_generated_media_signals"]}
        assert signals == {"EMBEDDED_GENERATOR_PROVENANCE"}
        assert entry["provenance_structures_found"] == ["APP11_JUMBF_C2PA"]
        findings = entry["c2pa_manifest_findings"]
        assert findings["action"] == "c2pa.created"
        assert findings["action_description"] == "Created by Google Generative AI."
        assert findings["digital_source_type"].endswith("trainedAlgorithmicMedia")
        assert findings["camera_capture_assertion_present"] is False


def test_submission_02_was_not_rejected_on_any_non_decisive_diagnostic() -> None:
    """EXIF absence was present but must not have done the work."""
    for entry in _submission_02():
        assert entry["non_decisive_diagnostics"]
        assert all(a["decisive"] is False for a in entry["non_decisive_diagnostics"])
        assert {a["anomaly"] for a in entry["non_decisive_diagnostics"]} == {"NO_EXIF"}


def test_submission_02_hashes_survive_the_deletion() -> None:
    """Deleting the files must not delete the ability to identify them later."""
    for entry in _submission_02():
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0
        assert not (REPO_ROOT / entry["original_canonical_path"]).exists()


def test_operator_attestation_cannot_override_a_creation_claim(tmp_path: Path) -> None:
    """The attestation is necessary provenance, never sufficient against positive evidence."""
    payload = make_jpeg(
        jpeg_segment(
            0xEB,
            b"jumb"
            + b"c2pa.created"
            + b"Created by Google Generative AI."
            + b"trainedAlgorithmicMedia",
        )
    )
    _write_declared_real(tmp_path, CANONICAL_NAMES, payload)
    status = collect_fixture_status(tmp_path)
    assert status["all_present"] is True
    assert status["physical_capture_satisfied"] is False
    assert sorted(status["rejected_as_generated"]) == sorted(CANONICAL_NAMES)


def test_a_c2pa_created_generative_claim_is_decisive(tmp_path: Path) -> None:
    """A synthetic reproduction of the exact structure submission 02 carries."""
    target = tmp_path / "label_left_01.jpg"
    target.write_bytes(
        make_jpeg(
            jpeg_segment(
                0xEB,
                b"jumb"
                + b"c2pa.created"
                + b"Created by Google Generative AI."
                + b"trainedAlgorithmicMedia",
            )
        )
    )
    finding = detect_generated_media(target, known_generated={})
    assert finding["is_generated"] is True
    assert finding["provenance_structures"] == ["APP11_JUMBF_C2PA"]


def test_submission_02_record_is_auditable_and_historical() -> None:
    assert REJECTED_SUBMISSION_02.exists()
    doc = json.loads(REJECTED_SUBMISSION_02.read_text(encoding="utf-8"))
    assert doc["status"] == "REJECTED_NOT_A_PHYSICAL_CAPTURE"
    assert doc["submission_sequence"] == 2
    assert doc["previous_submission_record"] == "evidence/g1_t064_rejected_submission.json"
    assert len(doc["submissions"]) == 3
    for entry in doc["submissions"]:
        assert entry["known_synthetic_hash_match"] is None
        signals = {s["signal"] for s in entry["decisive_generated_media_signals"]}
        assert signals == {"EMBEDDED_GENERATOR_PROVENANCE"}
    assert doc["outcome"]["real_physical_fixtures_accepted"] == 0
    assert doc["outcome"]["g1_verdict_after"] == "NOT_YET_DECIDABLE"
    assert doc["outcome"]["inference_records_created"] == 0


def test_submission_01_record_is_preserved_unchanged() -> None:
    """The first rejection record must survive the second submission untouched."""
    doc = json.loads(REJECTED_SUBMISSION.read_text(encoding="utf-8"))
    assert doc["status"] == "REJECTED_NOT_A_PHYSICAL_CAPTURE"
    assert "submission_sequence" not in doc, "submission 01 must not have been rewritten"
    for entry in doc["submissions"]:
        assert entry["known_synthetic_counterpart"]["hash_identical"] is True


def test_both_rejections_are_listed_in_the_manifest_history() -> None:
    manifest = json.loads((FIXTURES_DIR / "provenance.json").read_text(encoding="utf-8"))
    records = manifest["historical_rejections"]
    assert len(records) == 2
    assert records[0]["evidence"] == "evidence/g1_t064_rejected_submission.json"
    assert records[1]["evidence"] == "evidence/g1_t064_rejected_submission_02.json"


def test_no_inference_record_was_created_by_this_step() -> None:
    doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert doc["records"] == []
    assert doc["stability"] == {}
