"""T039-T041 — Canonical hashing, classification/lineage, and evidence manifest assembly.

FR-006, FR-010, SC-013.

**Hash guarantee boundary (read this before citing a hash anywhere).** SHA-256 digests
here establish **content identity and replacement/alteration detection only**: a
referenced artifact can be shown byte-identical to what was recorded, and silent
substitution becomes detectable by comparison.

They do **not** establish a digital signature, a trusted timestamp, identity
attestation, proof of authorship, non-repudiation, blockchain immutability, or
tamper-proof storage. "Immutable" retains only its approved operational meaning:
write-once application semantics within one project's trust boundary. No docstring,
evidence artifact, or judged claim may describe these digests otherwise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from driftzero.models.classification import (
    ClassificationLabel,
    DataClassification,
    LineageEntry,
)
from driftzero.models.proof import EvidenceManifest
from driftzero.models.remediation import MutationEvidence, NoOpEvidence, RemediationEvidence
from driftzero.models.verification import VerificationEvent

# ============================ T039 — canonical hashing ================================


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, stable separators.

    Semantically identical objects serialize identically regardless of key insertion
    order, so the resulting digest is order-independent.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(content: str | bytes) -> str:
    """SHA-256 hex digest of raw content. Identity and alteration detection only."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def canonical_hash(payload: Any) -> str:
    """SHA-256 of the canonical JSON encoding of ``payload``.

    Stable across processes: derived from an explicit canonical encoding, never from
    Python's per-process randomized ``hash()``.
    """
    return content_hash(canonical_json(payload))


def hashes_match(expected: str, actual_content: str | bytes) -> bool:
    """True when ``actual_content`` still hashes to ``expected``. Not signature checking."""
    return content_hash(actual_content) == expected


# ============================ T040 — classification and lineage =======================


def build_lineage_entry(
    *, source_ref: str, source_classification: Sequence[ClassificationLabel], relationship: str
) -> LineageEntry:
    """One provenance link: what this came from, and how it was produced."""
    return LineageEntry(
        source_ref=source_ref,
        source_classification=list(source_classification),
        relationship=relationship,
    )


def classify(
    labels: Sequence[ClassificationLabel], lineage: Sequence[LineageEntry] = ()
) -> DataClassification:
    """Assemble a non-exclusive classification with an ordered lineage chain.

    Labels are a set, never a single mutually exclusive enum: one evidence item may
    legitimately be several things at once — a real model execution over a synthetic
    fixture is ``[REAL, SYNTHETIC]``; a derived observation of a real photo is
    ``[DERIVED, REAL]``; an emulated dependency is ``[SIMULATED]``.
    """
    return DataClassification(labels=list(labels), lineage=list(lineage))


def derive_classification(
    *,
    labels: Sequence[ClassificationLabel],
    source_ref: str,
    source_classification: Sequence[ClassificationLabel],
    relationship: str = "derived_from",
) -> DataClassification:
    """Classify a derived item, recording the source it was derived from.

    Provenance is never fabricated: the caller supplies the real source reference and
    the source's own classification, and both are preserved verbatim.
    """
    return classify(
        labels,
        [
            build_lineage_entry(
                source_ref=source_ref,
                source_classification=source_classification,
                relationship=relationship,
            )
        ],
    )


# ============================ T041 — evidence manifest ================================


def remediation_evidence_refs(evidence: RemediationEvidence) -> tuple[str, ...]:
    """Refs appropriate to the discriminated variant.

    MUTATION contributes its genuine before and after references. NO_OP contributes the
    single evaluated state — no synthesized after-state, and never one reference
    duplicated into a before/after pair.
    """
    if isinstance(evidence, MutationEvidence):
        return (evidence.before_ref, evidence.after_ref)
    return (evidence.evaluated_artifact_ref,)


def remediation_content_hashes(evidence: RemediationEvidence) -> dict[str, str]:
    """Recorded digests for the remediation refs, keyed by reference."""
    if isinstance(evidence, MutationEvidence):
        return {
            evidence.before_ref: evidence.before_hash,
            evidence.after_ref: evidence.after_hash,
        }
    return {evidence.evaluated_artifact_ref: evidence.evaluated_artifact_hash}


def assemble_evidence_manifest(
    *,
    source_change_ref: str,
    affected_artifact_ref: str,
    remediation_evidence: RemediationEvidence,
    delivery_ref: str,
    verification_events: Iterable[VerificationEvent],
    state_transition_refs: Sequence[str] = (),
    rejected_result_refs: Sequence[str] = (),
    extra_content_hashes: Mapping[str, str] | None = None,
) -> EvidenceManifest:
    """T041 — assemble the complete manifest for a workflow.

    **Every** verification attempt is referenced, in ``event_sequence`` order —
    including FAIL and INCONCLUSIVE ones. Completion condition 6 requires the full
    history to be preserved and traceably associated, so the manifest is never built
    from the latest successful events alone.

    ``rejected_result_refs`` retains agent/tool results rejected at a trust boundary so
    the rejection is auditable. Retention is not endorsement: a rejected result
    satisfies no completion condition (see ``proof_generator``).

    Telemetry traces are not business evidence and do not enter here. Only
    ``state_transition_refs`` — an explicit field of the approved model — is accepted,
    and only from the caller.
    """
    ordered_events = sorted(verification_events, key=lambda event: event.event_sequence)
    hashes = dict(remediation_content_hashes(remediation_evidence))
    if extra_content_hashes:
        hashes.update(extra_content_hashes)

    return EvidenceManifest(
        source_change_ref=source_change_ref,
        affected_artifact_ref=affected_artifact_ref,
        remediation_evidence_refs=list(remediation_evidence_refs(remediation_evidence)),
        rejected_result_refs=list(rejected_result_refs),
        delivery_ref=delivery_ref,
        verification_refs=[event.event_id for event in ordered_events],
        state_transition_refs=list(state_transition_refs),
        content_hashes=hashes,
    )


def manifest_covers_all_events(
    manifest: EvidenceManifest, verification_events: Iterable[VerificationEvent]
) -> bool:
    """True when every verification attempt is referenced by the manifest."""
    referenced = set(manifest.verification_refs)
    return all(event.event_id in referenced for event in verification_events)


def is_no_op_manifest_shape(manifest: EvidenceManifest) -> bool:
    """True when the manifest carries the single-state NO_OP shape."""
    return len(manifest.remediation_evidence_refs) == 1


def has_fabricated_before_after_pair(
    manifest: EvidenceManifest, evidence: RemediationEvidence
) -> bool:
    """True when a NO_OP is dressed up as a mutation, or a pair is duplicated.

    Catches the two prohibited representations: a NO_OP carrying two refs, and a
    MUTATION whose before and after references are the same object.
    """
    refs = manifest.remediation_evidence_refs
    if isinstance(evidence, NoOpEvidence):
        return len(refs) != 1
    return len(refs) != 2 or refs[0] == refs[1]
