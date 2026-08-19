"""T056 — Data classification and lineage acceptance (FR-010, SC-013; quickstart VS-12).

Emits ``evidence/reports/data_lineage.json`` — a local repository file. No cloud storage
is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.truth_engine.evidence import (
    build_lineage_entry,
    classify,
    derive_classification,
)
from driftzero.truth_engine.proof_generator import generate_change_proof

from ._acceptance import make_proof_context

REPORT_PATH = Path(__file__).resolve().parents[3] / "evidence" / "reports" / "data_lineage.json"


def test_synthetic_business_fixture_is_labelled() -> None:
    c = classify([ClassificationLabel.SYNTHETIC])
    assert c.has(ClassificationLabel.SYNTHETIC)
    assert not c.has(ClassificationLabel.REAL)


def test_real_execution_over_synthetic_content_is_real_plus_synthetic() -> None:
    """A real model call processing a synthetic fixture is honestly both."""
    c = derive_classification(
        labels=[ClassificationLabel.REAL, ClassificationLabel.SYNTHETIC],
        source_ref="fixtures/hero_change.json",
        source_classification=[ClassificationLabel.SYNTHETIC],
        relationship="input_to",
    )
    assert c.has(ClassificationLabel.REAL) and c.has(ClassificationLabel.SYNTHETIC)
    assert c.lineage[0].source_ref == "fixtures/hero_change.json"
    assert c.lineage[0].relationship == "input_to"


def test_derived_observation_over_real_raw_evidence_is_derived_plus_real() -> None:
    c = derive_classification(
        labels=[ClassificationLabel.DERIVED, ClassificationLabel.REAL],
        source_ref="gs://evidence/photo.jpg",
        source_classification=[ClassificationLabel.REAL],
        relationship="observed_from",
    )
    assert c.has(ClassificationLabel.DERIVED) and c.has(ClassificationLabel.REAL)
    assert c.lineage[0].source_classification == [ClassificationLabel.REAL]


def test_simulated_dependency_is_explicitly_representable() -> None:
    """The SIMULATED agent-registry fallback must never read as REAL."""
    c = derive_classification(
        labels=[ClassificationLabel.SIMULATED],
        source_ref="fixtures/agent_registry.json",
        source_classification=[ClassificationLabel.SYNTHETIC],
        relationship="emulates",
    )
    assert c.has(ClassificationLabel.SIMULATED)
    assert not c.has(ClassificationLabel.REAL)


def test_lineage_preserves_source_ref_and_relationship() -> None:
    entry = build_lineage_entry(
        source_ref="gs://evidence/before.json",
        source_classification=[ClassificationLabel.SYNTHETIC],
        relationship="derived_from",
    )
    assert entry.source_ref == "gs://evidence/before.json"
    assert entry.relationship == "derived_from"


def test_multi_hop_lineage_chain_is_ordered() -> None:
    c = DataClassification(
        labels=[ClassificationLabel.DERIVED, ClassificationLabel.REAL],
        lineage=[
            build_lineage_entry(
                source_ref="gs://evidence/photo.jpg",
                source_classification=[ClassificationLabel.REAL],
                relationship="observed_from",
            ),
            build_lineage_entry(
                source_ref="fixtures/hero_change.json",
                source_classification=[ClassificationLabel.SYNTHETIC],
                relationship="input_to",
            ),
        ],
    )
    assert [e.relationship for e in c.lineage] == ["observed_from", "input_to"]


def test_change_proof_is_classified_derived() -> None:
    proof = generate_change_proof(make_proof_context())
    assert proof.data_classification.has(ClassificationLabel.DERIVED)


def test_no_judged_evidence_item_lacks_a_classification() -> None:
    """Every persisted evidence-bearing model requires a classification (FR-010)."""
    context = make_proof_context()
    proof = generate_change_proof(context)
    for item in (
        context.change,
        context.remediation_evidence,
        *context.verification_events,
        proof,
    ):
        assert item.data_classification.labels, f"{type(item).__name__} has no classification"


def test_emit_data_lineage_report() -> None:
    """Write the VS-12 evidence artifact locally. No cloud call is made."""
    context = make_proof_context()
    proof = generate_change_proof(context)
    report = {
        "scenario": "hero_change_deployment",
        "note": (
            "Classification is non-exclusive; hashes give content identity only, not "
            "signature, attestation, or non-repudiation."
        ),
        "items": [
            {
                "item": "approved_change_fixture",
                "labels": [str(x) for x in context.change.data_classification.labels],
                "lineage": [
                    {"source_ref": e.source_ref, "relationship": e.relationship}
                    for e in context.change.data_classification.lineage
                ],
            },
            {
                "item": "remediation_evidence",
                "labels": [
                    str(x) for x in context.remediation_evidence.data_classification.labels
                ],
                "remediation_type": context.remediation_evidence.remediation_type,
            },
            {
                "item": "verification_events",
                "count": len(context.verification_events),
                "results": [str(e.verification_result) for e in context.verification_events],
            },
            {
                "item": "change_proof",
                "labels": [str(x) for x in proof.data_classification.labels],
                "content_hash": proof.content_hash,
            },
        ],
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    written = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert written["items"][3]["labels"] == ["DERIVED"]
    assert len(written["items"]) == 4
