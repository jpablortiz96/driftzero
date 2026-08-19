"""T042 — Deterministic trust-boundary validation for the five crossings (FR-011).

Every non-authoritative agent or tool result crossing into the Truth Engine is validated
here before it may affect authoritative state. Schema validity is a necessary first
filter, never a sufficient one: a response can be perfectly shaped and still name an
artifact outside authorized scope, cite a superseded version, or assert an outcome that
never happened.

Validation is context-appropriate per crossing. A rejected result is recorded to
``rejected_result_refs`` for audit and advances nothing — retention is not endorsement,
and no proof condition may be satisfied by a rejected result.

There is no ``trusted=True`` escape hatch, and no precomputed boolean from an agent is
believed: ``candidate.is_affected``, ``delivery.delivered``, and any confidence value
are inputs to be checked, never conclusions to be accepted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from driftzero.models.change import ApprovedChange, ChangeSet
from driftzero.models.delivery import DeliveryResult
from driftzero.models.remediation import MutationEvidence, RemediationEvidence
from driftzero.models.verification import FieldObservation, ObservedPosition


class Crossing(StrEnum):
    """The five non-authoritative results that may cross into the Truth Engine."""

    CHANGE_SET = "CROSSING_1_CHANGE_SET"
    REMEDIATION_EVIDENCE = "CROSSING_2_REMEDIATION_EVIDENCE"
    DELIVERY_RESULT = "CROSSING_3_DELIVERY_RESULT"
    FIELD_OBSERVATION = "CROSSING_4_FIELD_OBSERVATION"
    MEDIA_OUTPUT = "CROSSING_5_MEDIA_OUTPUT"


class ValidationLayer(StrEnum):
    """Deterministic rejection reasons, named so audits can cite the failing layer."""

    SCHEMA = "SCHEMA"
    PROVENANCE = "PROVENANCE"
    EXPECTED_SOURCE_IDENTITY = "EXPECTED_SOURCE_IDENTITY"
    EXPECTED_ARTIFACT_IDENTITY = "EXPECTED_ARTIFACT_IDENTITY"
    EXPECTED_TOOL_IDENTITY = "EXPECTED_TOOL_IDENTITY"
    AUTHORIZATION_SCOPE = "AUTHORIZATION_SCOPE"
    SOURCE_VERSION_APPLICABILITY = "SOURCE_VERSION_APPLICABILITY"
    BEFORE_STATE_CONSISTENCY = "BEFORE_STATE_CONSISTENCY"
    SEMANTIC_INVARIANT = "SEMANTIC_INVARIANT"
    POSITIVE_RECEIPT = "POSITIVE_RECEIPT"
    OBSERVATION_DOMAIN = "OBSERVATION_DOMAIN"
    EVIDENCE_REFERENCE = "EVIDENCE_REFERENCE"
    CLASSIFICATION_REQUIRED = "CLASSIFICATION_REQUIRED"


@dataclass(frozen=True)
class ValidationOutcome:
    """Accept/reject verdict for one crossing."""

    crossing: Crossing
    accepted: bool
    failed_layers: tuple[ValidationLayer, ...]
    rejection_ref: str | None
    """Reference recorded to ``rejected_result_refs`` when the result is rejected."""

    @property
    def rejected(self) -> bool:
        return not self.accepted


def _outcome(
    crossing: Crossing, failed: list[ValidationLayer], rejection_ref: str
) -> ValidationOutcome:
    if not failed:
        return ValidationOutcome(crossing, True, (), None)
    return ValidationOutcome(
        crossing, False, tuple(dict.fromkeys(failed)), rejection_ref
    )


# ============================ Crossing 1 — ChangeSet ==================================


def validate_change_set(
    change_set: ChangeSet,
    *,
    change: ApprovedChange,
    known_artifact_ids: frozenset[str],
    source_version_applicable: bool,
    rejection_ref: str,
) -> ValidationOutcome:
    """Validate an agent-extracted ``ChangeSet``.

    ``candidate.is_affected`` is deliberately not consulted: impact is decided by
    deterministic qualification (T025), never by the agent's own flag.
    """
    failed: list[ValidationLayer] = []

    if change_set.change_id != change.change_id:
        failed.append(ValidationLayer.PROVENANCE)
    if change_set.source_procedure_id != change.source_procedure_id:
        failed.append(ValidationLayer.EXPECTED_SOURCE_IDENTITY)
    if change_set.source_version != change.source_version or not source_version_applicable:
        failed.append(ValidationLayer.SOURCE_VERSION_APPLICABILITY)
    if any(
        candidate.artifact_id not in known_artifact_ids
        for candidate in change_set.candidate_affected_artifacts
    ):
        failed.append(ValidationLayer.EXPECTED_ARTIFACT_IDENTITY)
    if (
        change_set.previous_value != change.previous_value
        or change_set.current_value != change.current_value
        or change_set.operation_id != change.operation_id
        or change_set.requirement_id != change.requirement_id
    ):
        failed.append(ValidationLayer.SEMANTIC_INVARIANT)

    return _outcome(Crossing.CHANGE_SET, failed, rejection_ref)


# ============================ Crossing 2 — RemediationEvidence ========================


def validate_remediation_evidence(
    evidence: RemediationEvidence,
    *,
    change: ApprovedChange,
    expected_artifact_id: str,
    expected_before_hash: str,
    expected_action_id: str,
    source_version_applicable: bool,
    rejection_ref: str,
) -> ValidationOutcome:
    """Validate a remediation result against what the Truth Engine actually targeted."""
    failed: list[ValidationLayer] = []

    if evidence.artifact_id != expected_artifact_id:
        failed.append(ValidationLayer.EXPECTED_ARTIFACT_IDENTITY)
    if expected_artifact_id not in change.authorized_scope:
        failed.append(ValidationLayer.AUTHORIZATION_SCOPE)
    if not source_version_applicable:
        failed.append(ValidationLayer.SOURCE_VERSION_APPLICABILITY)

    if isinstance(evidence, MutationEvidence):
        if evidence.action_id != expected_action_id:
            failed.append(ValidationLayer.EXPECTED_TOOL_IDENTITY)
        if evidence.before_hash != expected_before_hash:
            failed.append(ValidationLayer.BEFORE_STATE_CONSISTENCY)
        if evidence.after_value != change.current_value:
            failed.append(ValidationLayer.SEMANTIC_INVARIANT)
        if evidence.before_value != change.previous_value:
            failed.append(ValidationLayer.SEMANTIC_INVARIANT)
        if evidence.before_ref == evidence.after_ref:
            failed.append(ValidationLayer.SEMANTIC_INVARIANT)
    else:
        # NoOpEvidence: compliance must be genuine, not a disguised mutation.
        if evidence.observed_value != evidence.expected_value:
            failed.append(ValidationLayer.SEMANTIC_INVARIANT)
        if evidence.expected_value != change.current_value:
            failed.append(ValidationLayer.SEMANTIC_INVARIANT)

    return _outcome(Crossing.REMEDIATION_EVIDENCE, failed, rejection_ref)


# ============================ Crossing 3 — DeliveryResult =============================


def validate_delivery_result(
    result: DeliveryResult,
    *,
    expected_worker_id: str,
    expected_mechanism: str,
    resolvable_receipt_refs: frozenset[str],
    rejection_ref: str,
) -> ValidationOutcome:
    """Validate a delivery result. ``delivered=True`` alone establishes nothing.

    ``DELIVERED`` requires a receipt the mechanism can actually resolve, so an agent
    asserting delivery without one is rejected at the positive-receipt layer.
    """
    failed: list[ValidationLayer] = []

    if result.worker_id != expected_worker_id:
        failed.append(ValidationLayer.PROVENANCE)
    if result.delivery_mechanism != expected_mechanism:
        failed.append(ValidationLayer.SEMANTIC_INVARIANT)
    if not result.delivery_evidence_ref or (
        result.delivery_evidence_ref not in resolvable_receipt_refs
    ):
        failed.append(ValidationLayer.POSITIVE_RECEIPT)

    return _outcome(Crossing.DELIVERY_RESULT, failed, rejection_ref)


# ============================ Crossing 4 — FieldObservation ===========================


def validate_field_observation(
    observation: FieldObservation,
    *,
    resolvable_evidence_refs: frozenset[str],
    known_submission_ids: frozenset[str],
    rejection_ref: str,
) -> ValidationOutcome:
    """Validate a field observation. It may report; it may not adjudicate.

    ``confidence_note`` is never consulted. A duplicate ``submission_id`` is not a
    validation failure — T031 absorbs it as a transport duplicate — so it is reported
    at the provenance layer only when the caller supplies it as already-known.
    """
    failed: list[ValidationLayer] = []

    if not isinstance(observation.observed_label_position, ObservedPosition):
        failed.append(ValidationLayer.OBSERVATION_DOMAIN)
    if observation.raw_evidence_ref not in resolvable_evidence_refs:
        failed.append(ValidationLayer.EVIDENCE_REFERENCE)
    if observation.submission_id in known_submission_ids:
        failed.append(ValidationLayer.PROVENANCE)

    return _outcome(Crossing.FIELD_OBSERVATION, failed, rejection_ref)


# ============================ Crossing 5 — optional media output ======================


def validate_media_output(
    *,
    asset_ref: str | None,
    resolvable_asset_refs: frozenset[str],
    classification_recorded: bool,
    rejection_ref: str,
) -> ValidationOutcome:
    """Validate an optional generated asset.

    Generation success never establishes delivery: this crossing yields an attachable
    asset only, and no delivery condition reads it.
    """
    failed: list[ValidationLayer] = []

    if not asset_ref or asset_ref not in resolvable_asset_refs:
        failed.append(ValidationLayer.EVIDENCE_REFERENCE)
    if not classification_recorded:
        failed.append(ValidationLayer.CLASSIFICATION_REQUIRED)

    return _outcome(Crossing.MEDIA_OUTPUT, failed, rejection_ref)


# ============================ rejection collection ====================================


def collect_rejections(outcomes: Mapping[str, ValidationOutcome]) -> tuple[str, ...]:
    """Rejection references for every rejected crossing, for the evidence manifest."""
    return tuple(
        outcome.rejection_ref
        for outcome in outcomes.values()
        if outcome.rejected and outcome.rejection_ref
    )
