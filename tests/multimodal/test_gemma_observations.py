"""T105 — the multimodal evaluation run against live Vertex AI MaaS.

Skipped unless ``DRIFTZERO_LIVE_MAAS=1``, so the ordinary suite makes no billable call.
One inference per manifest fixture — three in total. G1 already established
repeatability with nine; repeating that here would spend calls to re-answer a question
already answered and recorded.

What this run establishes that G1's did not: that the **production adapter**
(``driftzero_providers.vertex_maas``, T103) — not G1's probe harness — returns in-domain
observations for the real physical fixtures, and that the frozen normalizer accepts
them.

The model is never allowed to state a verdict. It reports a position; the deterministic
comparator decides PASS or FAIL, and that separation is asserted here too.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from typing import Any

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "multimodal"
MANIFEST = FIXTURES / "manifest.json"
REPORT = REPO_ROOT / "evidence" / "reports" / "multimodal_eval.json"

PROJECT = "driftzero-runtime-2026"

live_only = pytest.mark.skipif(
    os.environ.get("DRIFTZERO_LIVE_MAAS") != "1",
    reason="set DRIFTZERO_LIVE_MAAS=1 to run billable Vertex AI MaaS inferences",
)


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ============================ the manifest itself (offline) ===========================


def test_the_manifest_exists_and_is_versioned(manifest: dict[str, Any]) -> None:
    assert manifest["schema"] == "driftzero.m3.multimodal_manifest.v1"
    assert manifest["task"] == "T104"
    assert manifest["fixture_count"] == len(manifest["fixtures"]) == 3


def test_every_fixture_hash_still_matches_its_bytes(manifest: dict[str, Any]) -> None:
    """A manifest that drifted from the files would evaluate something else."""
    for entry in manifest["fixtures"]:
        raw = (FIXTURES / entry["filename"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], entry["filename"]
        assert len(raw) == entry["size_bytes"]


def test_the_manifest_records_actual_bytes_not_the_extension(
    manifest: dict[str, Any],
) -> None:
    """These are HEIC containers named .jpg. The bytes are the authority."""
    for entry in manifest["fixtures"]:
        assert entry["declared_extension"] == ".jpg"
        assert entry["actual_mime_type"] == "image/heic"
        assert entry["extension_matches_content"] is False


def test_expected_observations_cover_the_closed_domain(manifest: dict[str, Any]) -> None:
    from driftzero.models.verification import ObservedPosition

    expected = {e["expected_observation"] for e in manifest["fixtures"]}
    assert expected == {str(v) for v in ObservedPosition}
    assert manifest["observation_domain"] == [str(v) for v in ObservedPosition]


def test_synthetic_images_are_excluded(manifest: dict[str, Any]) -> None:
    """Generated images must never be evaluated as real physical evidence."""
    assert manifest["synthetic_directory"]["excluded_from_this_manifest"] is True
    names = {e["filename"] for e in manifest["fixtures"]}
    assert not any("synthetic" in name for name in names)
    assert all(e["provenance_class"] == "REAL_PHYSICAL" for e in manifest["fixtures"])


# ============================ the live evaluation =====================================


@pytest.fixture(scope="module")
def evaluation(manifest: dict[str, Any]) -> dict[str, Any]:
    """One live MaaS inference per fixture, through the production adapter."""
    if os.environ.get("DRIFTZERO_LIVE_MAAS") != "1":
        pytest.skip("live MaaS disabled")

    from driftzero.agents.field_verify import (
        FIELD_OBSERVATION_PROMPT,
        NormalizationError,
        ObservationContext,
        normalize_observation,
    )
    from driftzero.config import DriftZeroConfig
    from driftzero.media.container import sniff_mime_type
    from driftzero_providers.vertex_maas import VertexMaaSGemmaObservationProvider

    config = DriftZeroConfig.from_env(
        {
            "DRIFTZERO_FIELD_PROVIDER": "vertex_maas",
            "DRIFTZERO_GCP_PROJECT": PROJECT,
        }
    ).field_provider
    provider = VertexMaaSGemmaObservationProvider(config)

    records: list[dict[str, Any]] = []
    for entry in manifest["fixtures"]:
        raw = (FIXTURES / entry["filename"]).read_bytes()
        # Derived from the bytes, exactly as the ingestion path does. Passing the
        # extension's claim here would test a different thing than production runs.
        mime = sniff_mime_type(raw)
        context = ObservationContext(
            change_id="t105-multimodal-eval",
            source_version="fixtures/multimodal/manifest.json",
            submission_id=entry["sha256"][:32],
            prompt=FIELD_OBSERVATION_PROMPT,
        )
        started = time.perf_counter()
        observation = provider.observe(
            image_bytes=raw,
            mime_type=mime,
            context=context,
            deadline_seconds=config.semantic.timeout_seconds,
        )
        elapsed = round(time.perf_counter() - started, 3)

        try:
            normalized: str | None = str(normalize_observation(observation.raw_output))
            rejection: str | None = None
        except NormalizationError as exc:
            normalized, rejection = None, str(exc)

        records.append(
            {
                "filename": entry["filename"],
                "image_sha256": entry["sha256"],
                "actual_mime_type": mime,
                "expected_observation": entry["expected_observation"],
                "raw_output": observation.raw_output,
                "normalized_observation": normalized,
                "normalization_rejected": rejection,
                "matches_expected": normalized == entry["expected_observation"],
                "provider": observation.provider,
                "model": observation.model,
                "finish_reason": observation.finish_reason,
                "prompt_tokens": observation.prompt_tokens,
                "completion_tokens": getattr(observation, "completion_tokens", None),
                "latency_seconds": elapsed,
                "request_hash": getattr(observation, "request_hash", None),
            }
        )

    return {
        "task": "T105",
        "evidence_class": "REAL_MAAS_EXECUTION",
        "provider": "vertex_ai_maas",
        "model": config.model,
        "project": PROJECT,
        "traffic_type": "ON_DEMAND",
        "serving_route_source": "evidence/g1_gemma_feasibility.json (G1 GO)",
        "adapter": "src/driftzero_providers/vertex_maas.py (T103)",
        "inference_count": len(records),
        "records": records,
        "correct": sum(1 for r in records if r["matches_expected"]),
        "in_domain": sum(1 for r in records if r["normalized_observation"] is not None),
    }


@live_only
def test_every_fixture_returns_an_in_domain_observation(evaluation: dict[str, Any]) -> None:
    """The production adapter's output must survive the frozen normalizer."""
    rejected = [r for r in evaluation["records"] if r["normalized_observation"] is None]
    assert rejected == [], f"out-of-domain output: {rejected}"


@live_only
def test_the_distinguishable_fixtures_are_observed_correctly(
    evaluation: dict[str, Any],
) -> None:
    """LEFT and TOP_RIGHT are the two the hero flow depends on telling apart."""
    by_name = {r["filename"]: r for r in evaluation["records"]}
    assert by_name["label_left_01.jpg"]["normalized_observation"] == "LEFT"
    assert by_name["label_top_right_01.jpg"]["normalized_observation"] == "TOP_RIGHT"


@live_only
def test_the_model_returned_a_position_never_a_verdict(evaluation: dict[str, Any]) -> None:
    """A model that could say PASS would be deciding, not observing."""
    for record in evaluation["records"]:
        raw = str(record["raw_output"]).upper()
        for forbidden in ("PASS", "FAIL", "PROOF_COMPLETE", "VERIFIED", "APPROVED"):
            assert forbidden not in raw, f"{record['filename']} returned {forbidden!r}"


@live_only
def test_the_run_used_exactly_one_call_per_fixture(evaluation: dict[str, Any]) -> None:
    assert evaluation["inference_count"] == 3, "the run made more calls than fixtures"


@live_only
def test_no_credential_reaches_the_recorded_evidence(evaluation: dict[str, Any]) -> None:
    blob = json.dumps(evaluation)
    for marker in ("ya29.", "Bearer ", "Authorization", "refresh_token", "private_key"):
        assert marker not in blob, f"{marker!r} leaked into the evaluation record"


@live_only
def test_the_report_is_written(evaluation: dict[str, Any]) -> None:
    """T105's declared output path."""
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **evaluation,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    with REPORT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")

    written = json.loads(REPORT.read_text(encoding="utf-8"))
    assert written["inference_count"] == 3
    assert written["evidence_class"] == "REAL_MAAS_EXECUTION"
