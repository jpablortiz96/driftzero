"""T029-T031 — Deterministic identity and duplicate absorption (FR-007, SC-010).

Three identities, all deterministic and all stable across processes:

* ``change_id`` — the logical approved change. Re-delivery is a transport duplicate.
* ``action_id`` — one consequential logical side effect, derived from its inputs.
* ``submission_id`` — one logical field-evidence submission.

A transport retry is never a new business event. This module decides that, and
nothing else: it performs no I/O, selects no datastore, and owns no lifecycle state.

Cross-process stability: ``action_id`` is derived with SHA-256 over a canonical
JSON encoding, never with Python's ``hash()``, which is randomized per process.
The digest provides collision resistance and stable identity — it is not a
signature, an attestation, or proof of authorship.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from driftzero.models.action import ActionType
from driftzero.models.change import ApprovedChange
from driftzero.models.verification import VerificationEvent

ACTION_ID_PREFIX = "act"
_DIGEST_LENGTH = 32
"""Hex characters retained from the SHA-256 digest. Identity, not cryptographic proof."""


# ============================ T029 — duplicate change events ==========================


class ChangeEventOutcome(StrEnum):
    """Whether an inbound approved change is new or a transport duplicate."""

    NEW_LOGICAL_CHANGE = "NEW_LOGICAL_CHANGE"
    TRANSPORT_DUPLICATE = "TRANSPORT_DUPLICATE"


@dataclass(frozen=True)
class ChangeEventDecision:
    """Deterministic verdict for one inbound approved-change delivery."""

    outcome: ChangeEventOutcome
    change_id: str
    existing_workflow_id: str | None
    """Set when the change was already accepted, so the caller reuses that workflow."""

    @property
    def is_duplicate(self) -> bool:
        return self.outcome is ChangeEventOutcome.TRANSPORT_DUPLICATE


def classify_change_event(
    change: ApprovedChange, known_changes: Mapping[str, str]
) -> ChangeEventDecision:
    """T029 — decide whether this approved change has already been accepted.

    ``known_changes`` maps an already-accepted ``change_id`` to its ``workflow_id``.
    Re-delivery of the same logical ``change_id`` resolves to the existing workflow
    instead of starting a second logical execution (FR-007): no duplicate remediation,
    no duplicate delivery, no second Change Proof.
    """
    existing = known_changes.get(change.change_id)
    if existing is not None:
        return ChangeEventDecision(
            outcome=ChangeEventOutcome.TRANSPORT_DUPLICATE,
            change_id=change.change_id,
            existing_workflow_id=existing,
        )
    return ChangeEventDecision(
        outcome=ChangeEventOutcome.NEW_LOGICAL_CHANGE,
        change_id=change.change_id,
        existing_workflow_id=None,
    )


# ============================ T030 — stable action identity ===========================


def _canonical(payload: Mapping[str, str]) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, stable separators."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def derive_action_id(
    *,
    workflow_id: str,
    action_type: ActionType,
    target_ref: str,
    change_id: str = "",
    source_version: str = "",
) -> str:
    """T030 — derive the stable identity of one logical side effect.

    Identity inputs: ``workflow_id``, ``action_type``, the applicable
    ``change_id`` / ``source_version``, and the target identity. Recomputing over the
    same logical inputs — in another process, from reconstructed objects — yields the
    same value, because the inputs are canonicalized before hashing and ``hash()`` is
    never used.

    Materially different logical actions produce different ids: changing any input
    changes the digest.
    """
    payload = {
        "workflow_id": workflow_id,
        "action_type": str(action_type),
        "target_ref": target_ref,
        "change_id": change_id,
        "source_version": source_version,
    }
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return f"{ACTION_ID_PREFIX}-{str(action_type).lower()}-{digest[:_DIGEST_LENGTH]}"


def derive_remediation_action_id(
    *, workflow_id: str, change: ApprovedChange, artifact_id: str
) -> str:
    """``REMEDIATE_ARTIFACT`` identity — target is the artifact."""
    return derive_action_id(
        workflow_id=workflow_id,
        action_type=ActionType.REMEDIATE_ARTIFACT,
        target_ref=artifact_id,
        change_id=change.change_id,
        source_version=change.source_version,
    )


def derive_delivery_action_id(
    *, workflow_id: str, change: ApprovedChange, worker_id: str
) -> str:
    """``DELIVER_DELTA`` identity — target is the worker identity."""
    return derive_action_id(
        workflow_id=workflow_id,
        action_type=ActionType.DELIVER_DELTA,
        target_ref=worker_id,
        change_id=change.change_id,
        source_version=change.source_version,
    )


def derive_field_evidence_action_id(*, workflow_id: str, submission_id: str) -> str:
    """``PROCESS_FIELD_EVIDENCE`` identity — target is the submission."""
    return derive_action_id(
        workflow_id=workflow_id,
        action_type=ActionType.PROCESS_FIELD_EVIDENCE,
        target_ref=submission_id,
    )


def derive_proof_action_id(*, workflow_id: str) -> str:
    """``GENERATE_PROOF`` identity — keyed by workflow, giving one canonical proof.

    Identity only. Proof generation and validation remain T043+.
    """
    return derive_action_id(
        workflow_id=workflow_id,
        action_type=ActionType.GENERATE_PROOF,
        target_ref=workflow_id,
    )


# ============================ T031 — field evidence submissions =======================


class SubmissionOutcome(StrEnum):
    """Whether a field-evidence submission is new or a transport duplicate."""

    NEW_EVIDENCE_ATTEMPT = "NEW_EVIDENCE_ATTEMPT"
    TRANSPORT_DUPLICATE = "TRANSPORT_DUPLICATE"


@dataclass(frozen=True)
class SubmissionDecision:
    """Deterministic verdict for one inbound field-evidence submission."""

    outcome: SubmissionOutcome
    submission_id: str
    existing_event_id: str | None
    """Set for a transport duplicate: the authoritative event already recorded."""
    allocates_event_sequence: bool
    """True only for a genuinely new attempt. A retry never consumes a position."""

    @property
    def is_duplicate(self) -> bool:
        return self.outcome is SubmissionOutcome.TRANSPORT_DUPLICATE


def classify_submission(
    submission_id: str, existing_events: Iterable[VerificationEvent]
) -> SubmissionDecision:
    """T031 — absorb transport duplicates, admit genuinely new attempts.

    Same ``submission_id`` re-delivered resolves to the existing
    ``VerificationEvent``: no second authoritative event, no new ``event_sequence``,
    no duplicated evidence, and it is never mistaken for corrected evidence.

    A different ``submission_id`` is a new attempt, which may later carry the
    corrected evidence after a FAIL or INCONCLUSIVE. Whether it does — and what it
    means chronologically — is T037+; this module only decides identity.
    """
    for event in existing_events:
        if event.submission_id == submission_id:
            return SubmissionDecision(
                outcome=SubmissionOutcome.TRANSPORT_DUPLICATE,
                submission_id=submission_id,
                existing_event_id=event.event_id,
                allocates_event_sequence=False,
            )
    return SubmissionDecision(
        outcome=SubmissionOutcome.NEW_EVIDENCE_ATTEMPT,
        submission_id=submission_id,
        existing_event_id=None,
        allocates_event_sequence=True,
    )
