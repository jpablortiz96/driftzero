"""T037/T038 — Verification chronology and the deterministic comparator.

FR-005, SC-006, SC-007, SC-012.

Two responsibilities, both deterministic and both owned exclusively by the Truth
Engine:

* **Chronology (T037)** — which verification attempt is authoritative *now*. The only
  ordering signal is ``event_sequence``. Timestamps are metadata: a client clock, a
  camera clock, a model clock, and arrival order are all untrusted for ordering.
* **Comparator (T038)** — expected value versus normalized observation, yielding
  PASS / FAIL / INCONCLUSIVE.

Authority boundary: a ``FieldObservation`` says only *what was observed*. It carries no
verdict field (T013), so it structurally cannot assert PASS or FAIL, cannot transition a
workflow, and cannot authorize anything. ``confidence_note`` is informational and is
never read here — a confidently-wrong observation still fails.

No model call, no threshold, no fuzzy matching, no semantic similarity. Gemma is not
involved: by the time a value reaches this module it is already a normalized
observation, and how it got normalized is upstream (M1/M3).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from driftzero.models.classification import DataClassification
from driftzero.models.verification import (
    FieldObservation,
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.truth_engine.idempotency import classify_submission

# ============================ T038 — the comparator ===================================


class UnnormalizedObservationError(Exception):
    """A value outside the normalized observation domain reached the comparator.

    Rejected rather than coerced: guessing what an unrecognized value "probably meant"
    is exactly the silent conversion of uncertainty that FR-011 forbids.
    """

    def __init__(self, raw: object) -> None:
        self.raw = raw
        allowed = sorted(p.value for p in ObservedPosition)
        super().__init__(f"observation {raw!r} is not a normalized value; allowed: {allowed}")


def normalize_observation(raw: object) -> ObservedPosition:
    """Return the normalized observation, or raise. Never coerces an unknown value."""
    if isinstance(raw, ObservedPosition):
        return raw
    if isinstance(raw, str):
        try:
            return ObservedPosition(raw)
        except ValueError as exc:
            raise UnnormalizedObservationError(raw) from exc
    raise UnnormalizedObservationError(raw)


def compare_observation(expected_value: str, observed: object) -> VerificationResult:
    """T038 — derive the authoritative verification result.

    ``observed == expected`` → PASS; ``observed != expected`` and not INCONCLUSIVE →
    FAIL; otherwise INCONCLUSIVE.

    Generic over the structured approved value: ``expected_value`` is whatever the
    approved requirement carries, compared by exact equality against the normalized
    observation. ``LEFT``/``TOP_RIGHT`` are the hero fixture's values, not a hard-coded
    vocabulary.

    INCONCLUSIVE is checked first and returned as itself, so it can never be folded into
    FAIL by an equality test.
    """
    normalized = normalize_observation(observed)
    if normalized is ObservedPosition.INCONCLUSIVE:
        return VerificationResult.INCONCLUSIVE
    if normalized.value == expected_value:
        return VerificationResult.PASS
    return VerificationResult.FAIL


# ============================ T037 — chronology =======================================


class AmbiguousChronologyError(Exception):
    """Two events in one workflow claim the same ``event_sequence``.

    Ordering would be undecidable, so selection fails closed rather than picking one.
    """

    def __init__(self, workflow_id: str, event_sequence: int) -> None:
        self.workflow_id = workflow_id
        self.event_sequence = event_sequence
        super().__init__(
            f"workflow {workflow_id} has multiple events at event_sequence {event_sequence}"
        )


def events_for_workflow(
    events: Iterable[VerificationEvent], workflow_id: str
) -> tuple[VerificationEvent, ...]:
    """Scope events to one workflow.

    This is also the source-version boundary. A workflow is bound to exactly one
    applicable ``source_version`` (``Workflow.source_version``), and every event carries
    its parent ``workflow_id``, so scoping by workflow is precisely scoping by applicable
    version. Evidence recorded against a superseded workflow stays with that workflow and
    can never become the current verification of the newer one.
    """
    return tuple(event for event in events if event.workflow_id == workflow_id)


def next_event_sequence(events: Iterable[VerificationEvent], workflow_id: str) -> int:
    """Next monotonic position for this workflow. Allocated only for a new attempt."""
    scoped = events_for_workflow(events, workflow_id)
    return max((event.event_sequence for event in scoped), default=0) + 1


def latest_authoritative_event(
    events: Iterable[VerificationEvent], workflow_id: str
) -> VerificationEvent | None:
    """The authoritative verification: the event with the greatest ``event_sequence``.

    Arrival order and timestamps are ignored. A late-delivered older event therefore
    cannot displace a newer one — it simply is not the maximum.
    """
    scoped = events_for_workflow(events, workflow_id)
    if not scoped:
        return None
    seen: set[int] = set()
    for event in scoped:
        if event.event_sequence in seen:
            raise AmbiguousChronologyError(workflow_id, event.event_sequence)
        seen.add(event.event_sequence)
    return max(scoped, key=lambda event: event.event_sequence)


def latest_verification_status(
    events: Iterable[VerificationEvent], workflow_id: str
) -> VerificationResult | None:
    """Result of the authoritative event, or None when no attempt exists yet."""
    latest = latest_authoritative_event(events, workflow_id)
    return None if latest is None else latest.verification_result


def would_override_authoritative(
    events: Iterable[VerificationEvent], workflow_id: str, candidate_sequence: int
) -> bool:
    """True when an event at ``candidate_sequence`` would become authoritative."""
    latest = latest_authoritative_event(events, workflow_id)
    return latest is None or candidate_sequence > latest.event_sequence


# ============================ ingestion ===============================================


class IngestOutcome(StrEnum):
    """Whether an inbound observation produced a new authoritative attempt."""

    RECORDED_NEW_ATTEMPT = "RECORDED_NEW_ATTEMPT"
    TRANSPORT_DUPLICATE = "TRANSPORT_DUPLICATE"


@dataclass(frozen=True)
class VerificationIngestResult:
    """Outcome of accepting one field-evidence observation."""

    outcome: IngestOutcome
    event: VerificationEvent
    """For a duplicate this is the pre-existing event, returned unchanged."""
    allocated_sequence: int | None
    """None for a transport duplicate: a retry never consumes a position."""

    @property
    def is_new_attempt(self) -> bool:
        return self.outcome is IngestOutcome.RECORDED_NEW_ATTEMPT


def ingest_observation(
    observation: FieldObservation,
    *,
    workflow_id: str,
    expected_value: str,
    existing_events: Iterable[VerificationEvent],
    event_id: str,
    timestamp: datetime,
    data_classification: DataClassification,
) -> VerificationIngestResult:
    """Accept one observation: absorb duplicates, else record a new authoritative attempt.

    Duplicate handling delegates to :func:`classify_submission` (T031) rather than
    re-deriving the rule — a re-delivery of the same ``submission_id`` returns the
    existing event untouched, allocating no sequence and creating no second attempt.

    A genuinely new ``submission_id`` receives the next sequence, and its
    ``verification_result`` is derived here by the comparator. The observation never
    supplies it: ``FieldObservation`` has no such field.

    The event is built through normal validated construction — no ``model_construct``,
    and no unvalidated ``model_copy`` on externally-sourced data.
    """
    scoped = events_for_workflow(existing_events, workflow_id)
    decision = classify_submission(observation.submission_id, scoped)

    if decision.is_duplicate:
        existing = next(
            event for event in scoped if event.event_id == decision.existing_event_id
        )
        return VerificationIngestResult(
            outcome=IngestOutcome.TRANSPORT_DUPLICATE,
            event=existing,
            allocated_sequence=None,
        )

    sequence = next_event_sequence(scoped, workflow_id)
    event = VerificationEvent(
        event_id=event_id,
        submission_id=observation.submission_id,
        workflow_id=workflow_id,
        event_sequence=sequence,
        raw_evidence_ref=observation.raw_evidence_ref,
        derived_observation=observation.observed_label_position,
        expected_value=expected_value,
        verification_result=compare_observation(
            expected_value, observation.observed_label_position
        ),
        timestamp=timestamp,
        data_classification=data_classification,
    )
    return VerificationIngestResult(
        outcome=IngestOutcome.RECORDED_NEW_ATTEMPT,
        event=event,
        allocated_sequence=sequence,
    )
