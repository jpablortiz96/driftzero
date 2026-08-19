"""T059 — FR/SC traceability completeness gate.

Every FR-001…FR-011 and SC-001…SC-015 must map to either an **executed deterministic
M0 validation** or an **explicitly named later-milestone scenario**. A generic
"covered later" is not accepted: each later-milestone entry names its exact task and
quickstart scenario.

This is the safety net referenced by plan.md § Requirement Traceability Matrix. It fails
if a requirement is orphaned, if an M0 claim points at a test module that does not exist,
or if a later-milestone claim is vague.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest


class Coverage(StrEnum):
    EXECUTED_M0 = "EXECUTED_M0"
    DOCUMENTED_LATER_MILESTONE = "DOCUMENTED_LATER_MILESTONE"


@dataclass(frozen=True)
class Trace:
    coverage: Coverage
    modules: tuple[str, ...]
    """Executed M0 test modules, for EXECUTED_M0 entries."""
    note: str
    """For later-milestone entries: the exact task + scenario that will demonstrate it."""


M0 = Coverage.EXECUTED_M0
LATER = Coverage.DOCUMENTED_LATER_MILESTONE

FR_TRACEABILITY: dict[str, Trace] = {
    "FR-001": Trace(M0, ("test_domain_models", "test_state_machine"), ""),
    "FR-002": Trace(M0, ("test_impact", "test_validation"), ""),
    "FR-003": Trace(M0, ("test_autonomy_gate", "test_divergence", "test_no_op"), ""),
    "FR-004": Trace(M0, ("test_action_idempotency", "test_validation"), ""),
    "FR-005": Trace(M0, ("test_verification",), ""),
    "FR-006": Trace(M0, ("test_proof", "test_evidence_and_proof"), ""),
    "FR-007": Trace(M0, ("test_idempotency", "test_action_idempotency"), ""),
    "FR-008": Trace(M0, ("test_action_idempotency", "test_no_op"), ""),
    "FR-009": Trace(M0, ("test_supersession",), ""),
    "FR-010": Trace(M0, ("test_data_lineage",), ""),
    "FR-011": Trace(M0, ("test_validation", "test_action_idempotency"), ""),
}

SC_TRACEABILITY: dict[str, Trace] = {
    "SC-001": Trace(M0, ("test_domain_models", "test_impact"), ""),
    "SC-002": Trace(M0, ("test_impact",), ""),
    "SC-003": Trace(M0, ("test_no_op", "test_proof"), ""),
    # SC-004 asserts the authoritative master procedure is never modified. M0 proves the
    # engine holds no write path to it; demonstrating it against a real stored procedure
    # requires the cloud artifact store.
    "SC-004": Trace(
        LATER,
        (),
        "T073 Artifact Mutation Tool has no source-procedure access; demonstrated by "
        "T082 local end-to-end and quickstart VS-2 step 7",
    ),
    # SC-005 requires a real delivery mechanism receipt; M0 proves the receipt rule.
    "SC-005": Trace(
        LATER,
        (),
        "T078 local delivery channel with receipt + Crossing 3; demonstrated by "
        "quickstart VS-2 and VS-10 (delivery assertion rejected)",
    ),
    "SC-006": Trace(M0, ("test_verification",), ""),
    "SC-007": Trace(M0, ("test_verification",), ""),
    "SC-008": Trace(M0, ("test_proof",), ""),
    "SC-009": Trace(M0, ("test_proof",), ""),
    "SC-010": Trace(M0, ("test_idempotency",), ""),
    "SC-011": Trace(M0, ("test_action_idempotency", "test_no_op"), ""),
    "SC-012": Trace(M0, ("test_verification", "test_validation"), ""),
    "SC-013": Trace(M0, ("test_data_lineage",), ""),
    # SC-014 is end-to-end reproducibility from documented fixtures, which needs the
    # CLI and cloud paths that M0 deliberately excludes.
    "SC-014": Trace(
        LATER,
        (),
        "T082 local end-to-end (quickstart VS-2) plus T101 cloud smoke (VS-7); "
        "fixtures already created by T004/T005",
    ),
    "SC-015": Trace(M0, ("test_supersession",), ""),
}

TEST_DIR = Path(__file__).parent


def test_every_fr_is_traced() -> None:
    expected = {f"FR-{i:03d}" for i in range(1, 12)}
    assert set(FR_TRACEABILITY) == expected
    assert len(FR_TRACEABILITY) == 11


def test_every_sc_is_traced() -> None:
    expected = {f"SC-{i:03d}" for i in range(1, 16)}
    assert set(SC_TRACEABILITY) == expected
    assert len(SC_TRACEABILITY) == 15


def test_no_orphan_requirements() -> None:
    orphans = [
        key
        for key, trace in {**FR_TRACEABILITY, **SC_TRACEABILITY}.items()
        if not trace.modules and not trace.note
    ]
    assert orphans == [], f"orphaned requirements: {orphans}"


@pytest.mark.parametrize(
    ("key", "trace"), sorted({**FR_TRACEABILITY, **SC_TRACEABILITY}.items())
)
def test_executed_m0_claims_point_at_real_test_modules(key: str, trace: Trace) -> None:
    if trace.coverage is not Coverage.EXECUTED_M0:
        pytest.skip(f"{key} is {trace.coverage}")
    assert trace.modules, f"{key} claims EXECUTED_M0 but names no module"
    for module in trace.modules:
        assert (TEST_DIR / f"{module}.py").exists(), f"{key} names missing module {module}"
        importlib.import_module(f"tests.unit.truth_engine.{module}")


@pytest.mark.parametrize(
    ("key", "trace"), sorted({**FR_TRACEABILITY, **SC_TRACEABILITY}.items())
)
def test_later_milestone_claims_name_an_exact_task_and_scenario(key: str, trace: Trace) -> None:
    if trace.coverage is not Coverage.DOCUMENTED_LATER_MILESTONE:
        pytest.skip(f"{key} is {trace.coverage}")
    assert trace.modules == (), f"{key} is later-milestone but claims M0 modules"
    assert "T0" in trace.note, f"{key} must name the exact implementing task"
    assert "VS-" in trace.note, f"{key} must name the exact quickstart scenario"
    assert len(trace.note) > 40, f"{key} note is too vague to be auditable"


def test_all_eleven_frs_are_executed_in_m0() -> None:
    """Every functional requirement has deterministic M0 coverage."""
    not_executed = [k for k, t in FR_TRACEABILITY.items() if t.coverage is not Coverage.EXECUTED_M0]
    assert not_executed == []


def test_later_milestone_success_criteria_are_exactly_the_expected_three() -> None:
    """SC-004, SC-005, SC-014 need cloud/CLI paths M0 deliberately excludes.

    Guards against silently reclassifying an M0-provable criterion as "later".
    """
    later = {
        k for k, t in SC_TRACEABILITY.items() if t.coverage is Coverage.DOCUMENTED_LATER_MILESTONE
    }
    assert later == {"SC-004", "SC-005", "SC-014"}


def test_no_cloud_behavior_is_claimed_as_demonstrated_by_m0() -> None:
    """An EXECUTED_M0 entry must not cite a cloud or UI scenario."""
    for key, trace in {**FR_TRACEABILITY, **SC_TRACEABILITY}.items():
        if trace.coverage is Coverage.EXECUTED_M0:
            lowered = trace.note.lower()
            for cloud_word in ("firestore", "pub/sub", "cloud run", "gcs", "gemma", "veo"):
                assert cloud_word not in lowered, f"{key} claims cloud behavior in M0"


def test_traceability_summary_counts() -> None:
    executed = sum(
        1
        for t in {**FR_TRACEABILITY, **SC_TRACEABILITY}.values()
        if t.coverage is Coverage.EXECUTED_M0
    )
    later = sum(
        1
        for t in {**FR_TRACEABILITY, **SC_TRACEABILITY}.values()
        if t.coverage is Coverage.DOCUMENTED_LATER_MILESTONE
    )
    assert executed + later == 26
    assert executed == 23
    assert later == 3
