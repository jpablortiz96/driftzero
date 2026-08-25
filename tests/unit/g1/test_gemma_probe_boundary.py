"""G1 probe boundary tests (T065).

Deliberately outside ``tests/unit/truth_engine/`` — that subtree is the M0 deterministic
suite with its own network block, and the G1 probe is experimental code that will one day
talk to a live route. Keeping it separate preserves both boundaries.

These tests never contact a network: only the pure normalization and reporting logic is
exercised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from g1_gemma_probe import (  # noqa: E402
    EXPECTED_BY_FIXTURE,
    MODEL_VARIANT,
    NormalizationError,
    ProbeRecord,
    ServingRoute,
    build_report,
    extract_raw_observation,
    normalize_observation,
    summarize_stability,
)

from driftzero.models.verification import ObservedPosition  # noqa: E402

# ============================ closed observation domain ===============================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("LEFT", ObservedPosition.LEFT),
        ("TOP_RIGHT", ObservedPosition.TOP_RIGHT),
        ("INCONCLUSIVE", ObservedPosition.INCONCLUSIVE),
        ("  left  ", ObservedPosition.LEFT),
        ("'TOP_RIGHT'", ObservedPosition.TOP_RIGHT),
        ("TOP-RIGHT", ObservedPosition.TOP_RIGHT),
        ("top right", ObservedPosition.TOP_RIGHT),
        ("LEFT.", ObservedPosition.LEFT),
    ],
)
def test_only_the_three_approved_observations_normalize(
    raw: str, expected: ObservedPosition
) -> None:
    assert normalize_observation(raw) is expected


@pytest.mark.parametrize(
    "raw",
    [
        "PASS",
        "FAIL",
        "PROOF_COMPLETE",
        "PROBABLY_LEFT",
        "RIGHT",
        "UNKNOWN_BUT_LIKELY_TOP_RIGHT",
        "0.92",
        "The package is compliant",
        "LEFT or TOP_RIGHT",
        "VERIFICATION_PASSED",
        "",
        "   ",
    ],
)
def test_out_of_domain_output_is_rejected_not_guessed(raw: str) -> None:
    with pytest.raises(NormalizationError):
        normalize_observation(raw)


@pytest.mark.parametrize("raw", [0.92, None, True, ["LEFT"], {"observation": "LEFT"}])
def test_non_string_output_is_rejected(raw: object) -> None:
    with pytest.raises(NormalizationError):
        normalize_observation(raw)


def test_no_verdict_value_can_ever_normalize() -> None:
    """PASS/FAIL are workflow verdicts and must never cross as observations."""
    for verdict in ("PASS", "FAIL", "INCONCLUSIVE_PASS", "FAILED", "VERIFICATION_FAILED"):
        if verdict == "INCONCLUSIVE":
            continue
        with pytest.raises(NormalizationError):
            normalize_observation(verdict)


def test_normalized_domain_matches_the_truth_engine_enum_exactly() -> None:
    """The probe reuses the approved enum — no second observation vocabulary exists."""
    assert {p.value for p in ObservedPosition} == {"LEFT", "TOP_RIGHT", "INCONCLUSIVE"}
    for position in ObservedPosition:
        assert normalize_observation(position.value) is position


# ============================ raw extraction ==========================================


def test_raw_output_is_extracted_without_interpretation() -> None:
    assert extract_raw_observation("LEFT") == "LEFT"
    assert extract_raw_observation({"observed_label_position": "TOP_RIGHT"}) == "TOP_RIGHT"
    assert extract_raw_observation({"text": "banana"}) == "banana"
    assert "unexpected" in extract_raw_observation({"unexpected": 1})


# ============================ record / report honesty =================================


def test_failed_and_inconclusive_records_are_preserved() -> None:
    records = [
        ProbeRecord(
            fixture_id="label_left_01.jpg",
            fixture_classification="SYNTHETIC",
            expected_observation="LEFT",
            raw_output="banana",
            normalized_output=None,
            normalization_succeeded=False,
            latency_seconds=0.4,
            latency_label="ACTUAL_OBSERVED",
            matched_expected=False,
            attempt=1,
            error="out-of-domain",
        ),
        ProbeRecord(
            fixture_id="label_ambiguous_01.jpg",
            fixture_classification="SYNTHETIC",
            expected_observation="INCONCLUSIVE",
            raw_output="INCONCLUSIVE",
            normalized_output="INCONCLUSIVE",
            normalization_succeeded=True,
            latency_seconds=0.3,
            latency_label="ACTUAL_OBSERVED",
            matched_expected=True,
            attempt=1,
        ),
    ]
    report = build_report(ServingRoute.CLOUD_RUN_VLLM, "https://x", Path("nope"), records)
    assert len(report.records) == 2, "failures are not dropped from the evidence"
    assert report.records[0]["normalization_succeeded"] is False


def test_instability_across_repeats_is_reported_not_hidden() -> None:
    records = [
        ProbeRecord(
            fixture_id="label_left_01.jpg",
            fixture_classification="SYNTHETIC",
            expected_observation="LEFT",
            raw_output=value,
            normalized_output=value,
            normalization_succeeded=True,
            latency_seconds=0.2,
            latency_label="ACTUAL_OBSERVED",
            matched_expected=value == "LEFT",
            attempt=i + 1,
        )
        for i, value in enumerate(["LEFT", "TOP_RIGHT", "LEFT"])
    ]
    stability = summarize_stability(records)
    assert stability["label_left_01.jpg"]["stable"] is False
    assert stability["label_left_01.jpg"]["distinct_normalized_outputs"] == ["LEFT", "TOP_RIGHT"]


def test_verdict_is_not_decidable_without_fixtures_or_inference(tmp_path: Path) -> None:
    """No fixtures, no deployment, no inference: the gate must not claim GO.

    Uses an empty directory so the fail-closed path is exercised on its own terms,
    independent of whichever submission occupies the real fixture directory.
    """
    report = build_report(None, None, tmp_path, [])
    assert report.verdict == "NOT_YET_DECIDABLE"
    assert report.verdict_reasoning, "the blockers must be enumerated"
    assert any("T064" in reason for reason in report.verdict_reasoning)


def test_report_pins_the_authoritative_model_variant() -> None:
    """R-008 names gemma-4-12b; the probe must not silently substitute a model."""
    assert MODEL_VARIANT == "gemma-4-12b"
    report = build_report(None, None, Path("nope"), [])
    assert report.model_variant == "gemma-4-12b"


def test_fixture_set_covers_left_top_right_and_ambiguous() -> None:
    assert set(EXPECTED_BY_FIXTURE.values()) == {
        ObservedPosition.LEFT,
        ObservedPosition.TOP_RIGHT,
        ObservedPosition.INCONCLUSIVE,
    }


def test_probe_never_emits_a_workflow_verdict_field() -> None:
    """The record schema has no place to put PASS/FAIL."""
    fields = set(ProbeRecord.__dataclass_fields__)
    for forbidden in ("verification_result", "passed", "verdict", "proof", "authorized"):
        assert forbidden not in fields
