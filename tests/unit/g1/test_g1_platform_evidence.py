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

import json
import sys
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


def test_canonical_real_physical_paths_are_absent() -> None:
    """The generated images must not occupy paths reserved for physical evidence."""
    for name in CANONICAL_NAMES:
        assert not (FIXTURES_DIR / name).exists(), f"{name} occupies a REAL_PHYSICAL path"


def test_missing_real_fixture_paths_fail_closed() -> None:
    """Absence is not neutral: it leaves T064 unsatisfied."""
    status = collect_fixture_status(FIXTURES_DIR)
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
    assert doc["fixtures"]["all_present"] is False
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
