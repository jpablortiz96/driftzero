"""T057 — Change Proof acceptance: seven invariants, canonicality, replay, singularity.

Covers SC-008, SC-009 and the adversarial proof-replay-after-crash scenario.
"""

from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.models.workflow import WorkflowState
from driftzero.truth_engine.evidence import content_hash
from driftzero.truth_engine.impact import ImpactOutcome
from driftzero.truth_engine.proof_generator import (
    ProofCondition,
    ProofGenerationError,
    ProofValidationFailure,
    ProofValidator,
    compute_proof_hash,
    derive_completion_timestamp,
    derive_proof_id,
    evaluate_proof_invariants,
    generate_change_proof,
)

from ._acceptance import (
    AFTER_CONTENT,
    FAIL_EVENT,
    INCONCLUSIVE_EVENT,
    PASS_EVENT,
    T_PASS,
    WF,
    make_impact,
    make_manifest,
    make_no_op,
    make_proof_context,
)

# ============================ seven invariants ========================================


def test_seven_of_seven_is_eligible() -> None:
    result = evaluate_proof_invariants(make_proof_context())
    assert result.eligible is True
    assert result.satisfied_count == 7
    assert result.failed_conditions == ()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"applicable": False}, ProofCondition.C1_SOURCE_CHANGE_APPLICABLE),
        ({"evidence": None}, ProofCondition.C3_REMEDIATED_OR_NO_OP),
        ({"receipt": None}, ProofCondition.C4_DELTA_DELIVERED),
        ({"events": [FAIL_EVENT]}, ProofCondition.C5_LATEST_VERIFICATION_PASS),
        ({"state": WorkflowState.SUPERSEDED}, ProofCondition.C7_STATE_COMPATIBLE),
    ],
    ids=["c1", "c3", "c4", "c5", "c7"],
)
def test_each_condition_independently_blocks_eligibility(
    kwargs: dict[str, object], expected: ProofCondition
) -> None:
    """Six conditions valid, exactly one broken → not eligible."""
    result = evaluate_proof_invariants(make_proof_context(**kwargs))
    assert result.eligible is False
    assert expected in result.failed_conditions


def test_condition_2_blocks_when_impact_is_not_a_single_target() -> None:
    multi = make_impact(
        outcome=ImpactOutcome.MULTIPLE_QUALIFIED_TARGETS,
        affected_artifact_id=None,
        qualified_artifact_ids=("wi-a", "wi-b"),
        requires_review=True,
    )
    result = evaluate_proof_invariants(make_proof_context(impact=multi))
    assert ProofCondition.C2_IMPACT_DETERMINED in result.failed_conditions


def test_condition_6_blocks_when_history_is_trimmed_from_the_manifest() -> None:
    context = make_proof_context()
    trimmed = context.manifest.model_copy(update={"verification_refs": ["ev-2"]})
    result = evaluate_proof_invariants(make_proof_context(manifest=trimmed))
    assert ProofCondition.C6_EVIDENCE_TRACEABLE in result.failed_conditions


def test_six_of_seven_is_never_eligible() -> None:
    result = evaluate_proof_invariants(make_proof_context(receipt=None))
    assert result.satisfied_count == 6
    assert result.eligible is False


# ============================ condition 7 semantics ===================================


def test_current_pass_after_historical_fail_may_complete() -> None:
    result = evaluate_proof_invariants(
        make_proof_context(
            events=[FAIL_EVENT, PASS_EVENT], history=[WorkflowState.VERIFICATION_FAILED]
        )
    )
    assert result.conditions[ProofCondition.C7_STATE_COMPATIBLE] is True
    assert result.eligible is True


def test_current_pass_after_historical_inconclusive_may_complete() -> None:
    result = evaluate_proof_invariants(
        make_proof_context(
            events=[INCONCLUSIVE_EVENT, PASS_EVENT],
            history=[WorkflowState.VERIFICATION_INCONCLUSIVE],
        )
    )
    assert result.conditions[ProofCondition.C7_STATE_COMPATIBLE] is True
    assert result.eligible is True


@pytest.mark.parametrize(
    "state",
    [
        WorkflowState.VERIFICATION_FAILED,
        WorkflowState.VERIFICATION_INCONCLUSIVE,
        WorkflowState.REVIEW_REQUIRED,
        WorkflowState.SUPERSEDED,
        WorkflowState.FAILED,
    ],
)
def test_blocking_current_states_deny_completion(state: WorkflowState) -> None:
    context = make_proof_context(state=state)
    assert evaluate_proof_invariants(context).conditions[
        ProofCondition.C7_STATE_COMPATIBLE
    ] is False
    with pytest.raises(ProofGenerationError):
        generate_change_proof(context)


@pytest.mark.parametrize(
    "state", [WorkflowState.SUPERSEDED, WorkflowState.FAILED, WorkflowState.REVIEW_REQUIRED]
)
def test_blocking_states_in_history_deny_permanently(state: WorkflowState) -> None:
    """Everything else valid, current state healthy — history alone still blocks."""
    result = evaluate_proof_invariants(make_proof_context(history=[state]))
    assert result.eligible is False


# ============================ MUTATION vs NO_OP =======================================


def test_both_remediation_paths_produce_valid_distinct_proofs() -> None:
    mutation_proof = generate_change_proof(make_proof_context())
    no_op_proof = generate_change_proof(make_proof_context(evidence=make_no_op()))

    assert isinstance(mutation_proof.remediation_evidence, MutationEvidence)
    assert isinstance(no_op_proof.remediation_evidence, NoOpEvidence)
    assert len(mutation_proof.evidence_manifest.remediation_evidence_refs) == 2
    assert len(no_op_proof.evidence_manifest.remediation_evidence_refs) == 1


def test_no_op_with_a_fabricated_before_after_pair_is_rejected() -> None:
    no_op = make_no_op()
    faked = make_manifest(no_op).model_copy(
        update={"remediation_evidence_refs": ["gs://a.json", "gs://b.json"]}
    )
    result = evaluate_proof_invariants(make_proof_context(evidence=no_op, manifest=faked))
    assert ProofCondition.C3_REMEDIATED_OR_NO_OP in result.failed_conditions


def test_mutation_with_a_duplicated_reference_is_rejected() -> None:
    duplicated = make_manifest().model_copy(
        update={"remediation_evidence_refs": ["gs://same.json", "gs://same.json"]}
    )
    result = evaluate_proof_invariants(make_proof_context(manifest=duplicated))
    assert ProofCondition.C3_REMEDIATED_OR_NO_OP in result.failed_conditions


def test_a_proof_carries_exactly_one_remediation_variant() -> None:
    proof = generate_change_proof(make_proof_context())
    assert isinstance(proof.remediation_evidence, MutationEvidence | NoOpEvidence)
    assert proof.remediation_evidence.remediation_type in {"MUTATION", "NO_OP"}


# ============================ Section S — replay after crash ==========================


def test_replay_case_1_identical_inputs_reproduce_the_proof_exactly() -> None:
    """Two independent generations, no existing_proof passed."""
    a = generate_change_proof(make_proof_context())
    b = generate_change_proof(make_proof_context())
    assert a.proof_id == b.proof_id
    assert a.content_hash == b.content_hash
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_replay_case_2_retry_with_existing_proof_returns_the_same_proof() -> None:
    first = generate_change_proof(make_proof_context())
    retry = generate_change_proof(make_proof_context(), existing_proof=first)
    assert retry is first


def test_replay_case_3_crash_before_persistence_reproduces_the_proof() -> None:
    """Proof generated, process died, caller reconstructs the same authoritative state.

    Reproduction must be exact, because ``completion_timestamp`` is derived from the
    authoritative passing verification event rather than supplied by the caller.
    """
    before_crash = generate_change_proof(make_proof_context())

    # Fresh context objects, as if rebuilt from persisted authoritative state.
    after_recovery = generate_change_proof(make_proof_context())

    assert after_recovery.proof_id == before_crash.proof_id
    assert after_recovery.content_hash == before_crash.content_hash
    assert after_recovery.completion_timestamp == before_crash.completion_timestamp


def test_generator_accepts_no_caller_timestamp() -> None:
    """The defect guard: an orchestration retry cannot inject a fresh wall-clock value."""
    params = inspect.signature(generate_change_proof).parameters
    assert "completion_timestamp" not in params
    assert set(params) == {"context", "existing_proof"}


def test_completion_timestamp_is_the_authoritative_passing_event_timestamp() -> None:
    context = make_proof_context()
    assert derive_completion_timestamp(context) == PASS_EVENT.timestamp
    assert generate_change_proof(context).completion_timestamp == T_PASS


def test_proof_generation_reads_no_clock() -> None:
    """Static guard: no wall-clock call anywhere in the proof generator."""
    from pathlib import Path

    source = Path(inspect.getfile(generate_change_proof)).read_text(encoding="utf-8")
    for clock in ("datetime.now(", "datetime.utcnow(", "time.time("):
        assert clock not in source


def test_proof_hash_is_stable_across_a_fresh_interpreter() -> None:
    code = (
        "import sys; sys.path.insert(0, 'src'); sys.path.insert(0, 'tests');"
        "from unit.truth_engine._acceptance import make_proof_context;"
        "from driftzero.truth_engine.proof_generator import generate_change_proof;"
        "print(generate_change_proof(make_proof_context()).content_hash)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == generate_change_proof(make_proof_context()).content_hash


# ============================ singularity =============================================


def test_repeated_generation_never_creates_a_second_logical_proof() -> None:
    proofs = {generate_change_proof(make_proof_context()).proof_id for _ in range(5)}
    assert proofs == {derive_proof_id(WF)}


def test_different_workflows_get_different_proof_ids() -> None:
    assert derive_proof_id(WF) != derive_proof_id("wf-002")


# ============================ integrity ===============================================


def test_valid_proof_revalidates() -> None:
    context = make_proof_context()
    result = ProofValidator().validate(generate_change_proof(context), context)
    assert result.valid is True


def test_tampered_artifact_content_is_detected() -> None:
    context = make_proof_context()
    proof = generate_change_proof(context)
    result = ProofValidator().validate(
        proof, context, resolved_contents={"gs://evidence/after.json": '{"tampered":true}'}
    )
    assert result.valid is False
    assert ProofValidationFailure.CONTENT_HASH_MISMATCH in result.failures
    assert result.mismatched_refs == ("gs://evidence/after.json",)


def test_unmodified_artifact_content_revalidates() -> None:
    context = make_proof_context()
    proof = generate_change_proof(context)
    result = ProofValidator().validate(
        proof, context, resolved_contents={"gs://evidence/after.json": AFTER_CONTENT}
    )
    assert result.valid is True
    assert content_hash(AFTER_CONTENT) == proof.evidence_manifest.content_hashes[
        "gs://evidence/after.json"
    ]


def test_tampered_proof_document_is_detected() -> None:
    context = make_proof_context()
    proof = generate_change_proof(context)
    tampered = proof.model_copy(update={"worker_id": "worker-impostor"})
    result = ProofValidator().validate(tampered, context)
    assert result.valid is False
    assert ProofValidationFailure.PROOF_HASH_MISMATCH in result.failures
    assert compute_proof_hash(tampered) != proof.content_hash


def test_forged_proof_identity_is_detected() -> None:
    context = make_proof_context()
    proof = generate_change_proof(context)
    forged = proof.model_copy(update={"proof_id": "proof-2"})
    result = ProofValidator().validate(forged, context)
    assert ProofValidationFailure.PROOF_IDENTITY_MISMATCH in result.failures


def test_hash_guarantee_boundary_is_documented() -> None:
    from driftzero.truth_engine import proof_generator

    doc = " ".join((proof_generator.__doc__ or "").lower().split())
    for overclaim in (
        "digital signature",
        "trusted timestamp",
        "identity attestation",
        "non-repudiation",
        "blockchain",
        "tamper-proof",
    ):
        assert overclaim in doc, f"module must name and deny {overclaim}"
    validator_doc = " ".join((ProofValidator.__doc__ or "").lower().split())
    assert "signature verification" in validator_doc
