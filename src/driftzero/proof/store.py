"""T080 step 11 — assembling the proof context, and storing what was generated.

This module **wires**. It contains no invariant, no eligibility rule, and no second
generator: the seven conditions are evaluated by the frozen
:func:`~driftzero.truth_engine.proof_generator.evaluate_proof_invariants`, the proof is
produced by the frozen ``generate_change_proof``, and the manifest is built by the frozen
``assemble_evidence_manifest``. What lives here is the plumbing that turns application
state into the :class:`ProofContext` those functions expect, plus an append-only store.

Canonical bytes
---------------
The proof's identity is its canonical JSON and the SHA-256 over it, both produced by M0.
The store keeps **those exact bytes**, so what a caller downloads is byte-for-byte what
was hashed. Re-serialising a proof through a presentation schema and hashing the result
would produce a different document with the same name — which is how an audit trail
quietly stops being one.

What the hash is, and is not
----------------------------
SHA-256 over canonical JSON gives **content identity and alteration detection**. It is
not a signature, not an attestation, not a trusted timestamp, and not non-repudiation:
anyone able to alter the proof could recompute the hash. Nothing here or downstream may
describe it otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from driftzero.models.proof import ChangeProof
from driftzero.truth_engine.evidence import canonical_json
from driftzero.truth_engine.proof_generator import (
    ProofCondition,
    ProofContext,
    ProofInvariantResult,
    compute_proof_hash,
    evaluate_proof_invariants,
    generate_change_proof,
)

PROOF_REF_SCHEME = "proof"
"""``proof:pf-wf-dz-001-001`` — stable, and it resolves to the canonical bytes."""

HASH_MEANING = "SHA-256 content identity and alteration detection over canonical JSON"
"""The only claim this hash supports. Not a signature, attestation, or timestamp."""

CONDITION_LABELS: Mapping[ProofCondition, str] = {
    ProofCondition.C1_SOURCE_CHANGE_APPLICABLE: (
        "Source change represented and still applicable"
    ),
    ProofCondition.C2_IMPACT_DETERMINED: "Impact validly determined to one target",
    ProofCondition.C3_REMEDIATED_OR_NO_OP: "Remediated, or a valid no-op",
    ProofCondition.C4_DELTA_DELIVERED: "Delta delivered with a resolvable receipt",
    ProofCondition.C5_LATEST_VERIFICATION_PASS: (
        "Latest authoritative verification is PASS"
    ),
    ProofCondition.C6_EVIDENCE_TRACEABLE: "Evidence complete and traceable",
    ProofCondition.C7_STATE_COMPATIBLE: "Current state compatible with completion",
}
"""Human-readable names for the frozen seven. The set is never extended or trimmed."""


class ProofStorageError(Exception):
    """A stored proof would be overwritten. History is append-only."""


def invariant_report(result: ProofInvariantResult) -> list[dict[str, Any]]:
    """Every condition, individually, in specification order.

    Reports all seven whatever the outcome — a UI showing ``6 / 7`` has to be able to
    name the one that failed, and a UI showing ``7 / 7`` should be showing seven real
    results rather than a constant.
    """
    return [
        {
            "condition": str(condition),
            "label": CONDITION_LABELS[condition],
            "satisfied": bool(result.conditions[condition]),
        }
        for condition in ProofCondition
    ]


@dataclass(frozen=True)
class ProofEligibility:
    """The frozen gate's verdict, projected for the application layer."""

    eligible: bool
    conditions: list[dict[str, Any]]
    failed_conditions: tuple[str, ...]

    @property
    def satisfied_count(self) -> int:
        return sum(1 for entry in self.conditions if entry["satisfied"])

    @property
    def total(self) -> int:
        return len(self.conditions)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "satisfied_count": self.satisfied_count,
            "total": self.total,
            "conditions": list(self.conditions),
            "failed_conditions": list(self.failed_conditions),
        }


def evaluate_eligibility(context: ProofContext) -> ProofEligibility:
    """Run the frozen seven-invariant gate. No condition is added, skipped, or reordered."""
    result = evaluate_proof_invariants(context)
    return ProofEligibility(
        eligible=result.eligible,
        conditions=invariant_report(result),
        failed_conditions=tuple(str(c) for c in result.failed_conditions),
    )


@dataclass(frozen=True)
class StoredProof:
    """One generated proof, with the exact bytes its hash was computed over."""

    proof_ref: str
    proof: ChangeProof
    canonical_bytes: str
    content_hash: str

    def as_summary(self) -> dict[str, Any]:
        """Presentation projection. Never the thing that gets hashed."""
        return {
            "proof_ref": self.proof_ref,
            "proof_id": self.proof.proof_id,
            "workflow_id": self.proof.workflow_id,
            "change_id": self.proof.change_id,
            "source_procedure_id": self.proof.source_procedure_id,
            "source_version": self.proof.source_version,
            "affected_artifact_id": self.proof.affected_artifact_id,
            "previous_value": self.proof.previous_value,
            "current_value": self.proof.current_value,
            "verification_result": str(self.proof.verification_result),
            "verification_event_id": self.proof.verification_event_id,
            "delivery_ref": self.proof.delivery_ref,
            "completion_timestamp": self.proof.completion_timestamp.isoformat(),
            "content_hash": self.content_hash,
            "hash_meaning": HASH_MEANING,
            "byte_count": len(self.canonical_bytes.encode("utf-8")),
        }


@dataclass
class ProofStore:
    """Append-only store of generated proofs.

    One canonical proof per workflow. A repeat request returns the stored proof
    unchanged rather than producing a second one, and an attempt to store a *different*
    proof under an existing reference raises.
    """

    _proofs: dict[str, StoredProof] = field(default_factory=dict)
    _by_workflow: dict[str, str] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    @staticmethod
    def proof_ref(proof_id: str) -> str:
        return f"{PROOF_REF_SCHEME}:{proof_id}"

    def record(self, proof: ChangeProof) -> StoredProof:
        """Store a proof with its canonical bytes, or return the identical existing one."""
        ref = self.proof_ref(proof.proof_id)
        canonical = canonical_json(proof.model_dump(mode="json"))
        existing = self._proofs.get(ref)
        if existing is not None:
            if existing.canonical_bytes != canonical:
                raise ProofStorageError(
                    f"refusing to overwrite {ref}: the stored proof differs byte-for-byte"
                )
            return existing

        stored = StoredProof(
            proof_ref=ref,
            proof=proof,
            canonical_bytes=canonical,
            content_hash=proof.content_hash,
        )
        self._proofs[ref] = stored
        self._by_workflow[proof.workflow_id] = ref
        self._order.append(ref)
        return stored

    def resolve(self, proof_ref: str) -> StoredProof | None:
        """Retrieve a proof. Resolving never mutates the store."""
        return self._proofs.get(proof_ref)

    def find_workflow(self, workflow_id: str) -> StoredProof | None:
        ref = self._by_workflow.get(workflow_id)
        return self.resolve(ref) if ref else None

    def resolvable_refs(self) -> frozenset[str]:
        return frozenset(self._proofs)

    def __len__(self) -> int:
        return len(self._proofs)


@dataclass(frozen=True)
class ProofOutcome:
    """The result of one proof attempt. Blocked is a first-class, explained outcome."""

    generated: bool
    eligibility: ProofEligibility
    stored: StoredProof | None = None
    replayed: bool = False
    blocker_detail: str | None = None

    @property
    def proof_ref(self) -> str | None:
        return self.stored.proof_ref if self.stored else None


def attempt_proof(
    context: ProofContext,
    *,
    store: ProofStore,
) -> ProofOutcome:
    """Generate the Change Proof if — and only if — the frozen seven all hold.

    An already-generated proof is returned unchanged, so a replay produces the same
    ``proof_id``, the same bytes, and the same hash rather than a second authoritative
    record.
    """
    existing = store.find_workflow(context.workflow.workflow_id)
    eligibility = evaluate_eligibility(context)

    if existing is not None:
        return ProofOutcome(
            generated=True, eligibility=eligibility, stored=existing, replayed=True
        )

    if not eligibility.eligible:
        return ProofOutcome(
            generated=False,
            eligibility=eligibility,
            blocker_detail=(
                f"{eligibility.satisfied_count} of {eligibility.total} completion "
                "conditions hold; unmet: "
                + ", ".join(
                    CONDITION_LABELS[ProofCondition(c)]
                    for c in eligibility.failed_conditions
                )
            ),
        )

    proof = generate_change_proof(context)
    return ProofOutcome(generated=True, eligibility=eligibility, stored=store.record(proof))


def verify_stored_hash(stored: StoredProof) -> bool:
    """Re-derive the hash from the stored bytes. Integrity, not authorship."""
    return compute_proof_hash(stored.proof) == stored.content_hash


def replay_audit(
    *,
    stored: StoredProof,
    verification_events: Sequence[Any],
    timeline: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Render the recorded chronology. Executes nothing.

    A replay is a *reading* of what was already recorded — it dispatches no mutation,
    sends no delivery, and calls no model. That property is what makes it safe to offer
    as a button.
    """
    return {
        "proof_ref": stored.proof_ref,
        "proof_id": stored.proof.proof_id,
        "content_hash": stored.content_hash,
        "hash_verified": verify_stored_hash(stored),
        "hash_meaning": HASH_MEANING,
        "side_effects_executed": 0,
        "manifest": stored.proof.evidence_manifest.model_dump(mode="json"),
        "verification_chronology": [
            {
                "event_sequence": event.event_sequence,
                "event_id": event.event_id,
                "observed": str(event.derived_observation),
                "expected": event.expected_value,
                "result": str(event.verification_result),
                "timestamp": event.timestamp.isoformat(),
            }
            for event in sorted(verification_events, key=lambda e: e.event_sequence)
        ],
        "timeline": [dict(entry) for entry in timeline],
    }
