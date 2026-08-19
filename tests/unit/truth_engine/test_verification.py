"""M0-E focused tests for T037/T038 — chronology and the deterministic comparator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.verification import (
    FieldObservation,
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.truth_engine.verification import (
    AmbiguousChronologyError,
    IngestOutcome,
    UnnormalizedObservationError,
    compare_observation,
    events_for_workflow,
    ingest_observation,
    latest_authoritative_event,
    latest_verification_status,
    next_event_sequence,
    normalize_observation,
    would_override_authoritative,
)

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
WF = "wf-001"
EXPECTED = "TOP_RIGHT"
T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
T_LATE = datetime(2026, 8, 17, 23, 0, tzinfo=UTC)


def make_event(
    *,
    event_id: str,
    submission_id: str,
    sequence: int,
    observation: ObservedPosition,
    result: VerificationResult,
    timestamp: datetime = T0,
    workflow_id: str = WF,
) -> VerificationEvent:
    return VerificationEvent(
        event_id=event_id,
        submission_id=submission_id,
        workflow_id=workflow_id,
        event_sequence=sequence,
        raw_evidence_ref=f"gs://evidence/{submission_id}.jpg",
        derived_observation=observation,
        expected_value=EXPECTED,
        verification_result=result,
        timestamp=timestamp,
        data_classification=SYNTHETIC,
    )


def make_observation(
    submission_id: str,
    position: ObservedPosition,
    *,
    confidence_note: str = "",
) -> FieldObservation:
    return FieldObservation(
        submission_id=submission_id,
        raw_evidence_ref=f"gs://evidence/{submission_id}.jpg",
        observed_label_position=position,
        confidence_note=confidence_note,
    )


FAIL_EVENT = make_event(
    event_id="ev-1",
    submission_id="sub-1",
    sequence=1,
    observation=ObservedPosition.LEFT,
    result=VerificationResult.FAIL,
)
PASS_EVENT = make_event(
    event_id="ev-2",
    submission_id="sub-2",
    sequence=2,
    observation=ObservedPosition.TOP_RIGHT,
    result=VerificationResult.PASS,
)


# ============================ T038 — comparator =======================================


def test_expected_matches_observed_is_pass() -> None:
    assert compare_observation(EXPECTED, ObservedPosition.TOP_RIGHT) is VerificationResult.PASS


def test_expected_differs_from_observed_is_fail() -> None:
    assert compare_observation(EXPECTED, ObservedPosition.LEFT) is VerificationResult.FAIL


def test_inconclusive_observation_is_inconclusive() -> None:
    result = compare_observation(EXPECTED, ObservedPosition.INCONCLUSIVE)
    assert result is VerificationResult.INCONCLUSIVE
    assert result is not VerificationResult.FAIL
    assert result is not VerificationResult.PASS


def test_inconclusive_is_never_coerced_even_when_expected_is_inconclusive_like() -> None:
    """INCONCLUSIVE is checked first, so it can never be equality-matched into PASS."""
    assert (
        compare_observation("INCONCLUSIVE", ObservedPosition.INCONCLUSIVE)
        is VerificationResult.INCONCLUSIVE
    )


def test_comparator_is_generic_over_structured_expected_values() -> None:
    """LEFT/TOP_RIGHT are the fixture's values, not a hard-coded vocabulary."""
    assert compare_observation("LEFT", ObservedPosition.LEFT) is VerificationResult.PASS
    assert compare_observation("LEFT", ObservedPosition.TOP_RIGHT) is VerificationResult.FAIL
    assert compare_observation("BOTTOM_LEFT", ObservedPosition.LEFT) is VerificationResult.FAIL


def test_comparator_is_deterministic_for_repeated_identical_inputs() -> None:
    results = {compare_observation(EXPECTED, ObservedPosition.LEFT) for _ in range(50)}
    assert results == {VerificationResult.FAIL}


@pytest.mark.parametrize(
    "note",
    ["confidence 0.99", "certain", "very low confidence", "model is sure this is correct"],
)
def test_confidence_note_never_affects_the_result(note: str) -> None:
    """A confidently-wrong observation still fails; a hedged correct one still passes."""
    wrong = make_observation("sub-x", ObservedPosition.LEFT, confidence_note=note)
    right = make_observation("sub-y", ObservedPosition.TOP_RIGHT, confidence_note=note)
    assert compare_observation(EXPECTED, wrong.observed_label_position) is VerificationResult.FAIL
    assert compare_observation(EXPECTED, right.observed_label_position) is VerificationResult.PASS


@pytest.mark.parametrize("raw", ["MAYBE_LEFT", "top_right", "", "PASS", 42, None, object()])
def test_out_of_enum_observations_are_rejected_not_coerced(raw: object) -> None:
    with pytest.raises(UnnormalizedObservationError):
        compare_observation(EXPECTED, raw)


def test_normalize_accepts_only_the_closed_domain() -> None:
    assert normalize_observation("TOP_RIGHT") is ObservedPosition.TOP_RIGHT
    assert normalize_observation(ObservedPosition.LEFT) is ObservedPosition.LEFT
    with pytest.raises(UnnormalizedObservationError):
        normalize_observation("SIDEWAYS")


# ============================ T037 — chronology =======================================


def test_event_sequence_determines_chronology() -> None:
    latest = latest_authoritative_event([FAIL_EVENT, PASS_EVENT], WF)
    assert latest is not None
    assert latest.event_sequence == 2
    assert latest.verification_result is VerificationResult.PASS


def test_input_order_does_not_affect_selection() -> None:
    forward = latest_authoritative_event([FAIL_EVENT, PASS_EVENT], WF)
    reversed_ = latest_authoritative_event([PASS_EVENT, FAIL_EVENT], WF)
    assert forward is not None and reversed_ is not None
    assert forward.event_id == reversed_.event_id == "ev-2"


def test_timestamp_cannot_override_event_sequence() -> None:
    """Sequence 1 carries a much later timestamp; sequence 2 still wins."""
    stale_but_late = make_event(
        event_id="ev-1",
        submission_id="sub-1",
        sequence=1,
        observation=ObservedPosition.LEFT,
        result=VerificationResult.FAIL,
        timestamp=T_LATE,
    )
    latest = latest_authoritative_event([stale_but_late, PASS_EVENT], WF)
    assert latest is not None
    assert latest.event_sequence == 2
    assert latest.timestamp < stale_but_late.timestamp
    assert latest.verification_result is VerificationResult.PASS


def test_late_arriving_older_event_cannot_override_current_result() -> None:
    """Sequence 4 FAIL, then 5 PASS; a delayed re-delivery of 4 cannot restore FAIL."""
    fail_4 = make_event(
        event_id="ev-4",
        submission_id="sub-4",
        sequence=4,
        observation=ObservedPosition.LEFT,
        result=VerificationResult.FAIL,
    )
    pass_5 = make_event(
        event_id="ev-5",
        submission_id="sub-5",
        sequence=5,
        observation=ObservedPosition.TOP_RIGHT,
        result=VerificationResult.PASS,
    )
    history = [pass_5, fail_4]  # older event arrives last
    assert latest_verification_status(history, WF) is VerificationResult.PASS
    assert would_override_authoritative(history, WF, 4) is False
    assert would_override_authoritative(history, WF, 6) is True


def test_no_events_yields_no_status() -> None:
    assert latest_authoritative_event([], WF) is None
    assert latest_verification_status([], WF) is None


def test_next_event_sequence_is_monotonic() -> None:
    assert next_event_sequence([], WF) == 1
    assert next_event_sequence([FAIL_EVENT], WF) == 2
    assert next_event_sequence([FAIL_EVENT, PASS_EVENT], WF) == 3


def test_duplicate_event_sequence_fails_closed() -> None:
    clash = make_event(
        event_id="ev-dup",
        submission_id="sub-dup",
        sequence=2,
        observation=ObservedPosition.LEFT,
        result=VerificationResult.FAIL,
    )
    with pytest.raises(AmbiguousChronologyError):
        latest_authoritative_event([PASS_EVENT, clash], WF)


# --- source-version / workflow boundary ------------------------------------------------


def test_events_are_scoped_to_their_workflow() -> None:
    """A superseded workflow's evidence cannot become the newer workflow's verification."""
    superseded_pass = make_event(
        event_id="ev-old",
        submission_id="sub-old",
        sequence=99,
        observation=ObservedPosition.TOP_RIGHT,
        result=VerificationResult.PASS,
        workflow_id="wf-superseded",
    )
    history = [superseded_pass, FAIL_EVENT]
    assert events_for_workflow(history, WF) == (FAIL_EVENT,)
    assert latest_verification_status(history, WF) is VerificationResult.FAIL
    assert latest_verification_status(history, "wf-superseded") is VerificationResult.PASS


# ============================ ingestion + duplicate interaction =======================


def ingest(observation: FieldObservation, existing: list[VerificationEvent], event_id: str):
    return ingest_observation(
        observation,
        workflow_id=WF,
        expected_value=EXPECTED,
        existing_events=existing,
        event_id=event_id,
        timestamp=T1,
        data_classification=SYNTHETIC,
    )


def test_new_submission_records_a_new_attempt_with_derived_result() -> None:
    result = ingest(make_observation("sub-1", ObservedPosition.LEFT), [], "ev-1")
    assert result.outcome is IngestOutcome.RECORDED_NEW_ATTEMPT
    assert result.allocated_sequence == 1
    assert result.event.verification_result is VerificationResult.FAIL
    assert result.event.derived_observation is ObservedPosition.LEFT


def test_transport_duplicate_does_not_allocate_a_newer_verification() -> None:
    history = [FAIL_EVENT]
    result = ingest(make_observation("sub-1", ObservedPosition.LEFT), history, "ev-would-be-new")
    assert result.outcome is IngestOutcome.TRANSPORT_DUPLICATE
    assert result.allocated_sequence is None
    assert result.event.event_id == "ev-1"
    assert result.event.event_sequence == 1
    assert result.is_new_attempt is False


def test_transport_duplicate_never_supersedes_or_becomes_corrected_evidence() -> None:
    history = [FAIL_EVENT, PASS_EVENT]
    before = latest_authoritative_event(history, WF)
    result = ingest(make_observation("sub-1", ObservedPosition.LEFT), history, "ev-dup")
    after = latest_authoritative_event(history, WF)
    assert result.outcome is IngestOutcome.TRANSPORT_DUPLICATE
    assert before == after, "history unchanged by a duplicate"
    assert latest_verification_status(history, WF) is VerificationResult.PASS


def test_different_submission_id_creates_a_genuinely_new_attempt() -> None:
    history = [FAIL_EVENT]
    result = ingest(make_observation("sub-2", ObservedPosition.TOP_RIGHT), history, "ev-2")
    assert result.outcome is IngestOutcome.RECORDED_NEW_ATTEMPT
    assert result.allocated_sequence == 2
    assert result.event.verification_result is VerificationResult.PASS


# ============================ corrected evidence history ==============================


def test_historical_fail_is_preserved_after_later_pass() -> None:
    history = [FAIL_EVENT]
    corrected = ingest(make_observation("sub-2", ObservedPosition.TOP_RIGHT), history, "ev-2")
    history.append(corrected.event)

    assert len(history) == 2
    assert history[0] is FAIL_EVENT, "original attempt neither deleted nor mutated"
    assert history[0].verification_result is VerificationResult.FAIL
    assert latest_verification_status(history, WF) is VerificationResult.PASS


def test_historical_inconclusive_is_preserved_after_later_pass() -> None:
    history: list[VerificationEvent] = []
    first = ingest(make_observation("sub-1", ObservedPosition.INCONCLUSIVE), history, "ev-1")
    history.append(first.event)
    assert first.event.verification_result is VerificationResult.INCONCLUSIVE

    second = ingest(make_observation("sub-2", ObservedPosition.TOP_RIGHT), history, "ev-2")
    history.append(second.event)

    assert [e.verification_result for e in history] == [
        VerificationResult.INCONCLUSIVE,
        VerificationResult.PASS,
    ]
    assert latest_verification_status(history, WF) is VerificationResult.PASS


def test_full_fail_then_corrected_pass_sequence() -> None:
    """US6: FAIL then corrected PASS, with both attempts retained."""
    history: list[VerificationEvent] = []
    for submission, position, expected_result in (
        ("sub-1", ObservedPosition.LEFT, VerificationResult.FAIL),
        ("sub-2", ObservedPosition.TOP_RIGHT, VerificationResult.PASS),
    ):
        outcome = ingest(
            make_observation(submission, position), history, f"ev-{len(history) + 1}"
        )
        assert outcome.event.verification_result is expected_result
        history.append(outcome.event)

    assert len(history) == 2
    assert latest_verification_status(history, WF) is VerificationResult.PASS


# ============================ authority boundary ======================================


def test_field_observation_cannot_carry_a_verdict() -> None:
    observation = make_observation("sub-1", ObservedPosition.TOP_RIGHT)
    assert not hasattr(observation, "verification_result")
    assert not hasattr(observation, "passed")
    assert observation.model_fields_set <= {
        "submission_id",
        "raw_evidence_ref",
        "observed_label_position",
        "confidence_note",
    }


def test_observation_cannot_directly_set_pass_or_fail() -> None:
    """The result on the recorded event comes from the comparator, not the observation."""
    result = ingest(make_observation("sub-1", ObservedPosition.LEFT), [], "ev-1")
    assert result.event.verification_result is compare_observation(
        EXPECTED, ObservedPosition.LEFT
    )
    assert result.event.verification_result is VerificationResult.FAIL


def test_module_performs_no_proof_or_transition_work() -> None:
    from driftzero.truth_engine import verification

    exported = set(dir(verification))
    for forbidden in (
        "transition",
        "WorkflowState",
        "generate_proof",
        "ChangeProof",
        "ProofValidator",
        "evaluate_proof_invariants",
    ):
        assert forbidden not in exported


def test_no_proof_complete_is_produced_by_a_passing_verification() -> None:
    """A PASS is a verification result only; completion is T043+."""
    result = ingest(make_observation("sub-1", ObservedPosition.TOP_RIGHT), [], "ev-1")
    assert result.event.verification_result is VerificationResult.PASS
    assert not hasattr(result, "proof_id")
    assert not hasattr(result.event, "proof_id")
