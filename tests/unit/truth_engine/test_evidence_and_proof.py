"""M0-F focused tests for T039-T046 — hashing, lineage, manifest, invariants, proof."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from driftzero.models.change import ApprovedChange
from driftzero.models.classification import ClassificationLabel, DataClassification
from driftzero.models.delivery import DeliveryResult
from driftzero.models.proof import ChangeProof
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.models.verification import (
    ObservedPosition,
    VerificationEvent,
    VerificationResult,
)
from driftzero.models.workflow import Workflow, WorkflowState
from driftzero.truth_engine.evidence import (
    assemble_evidence_manifest,
    canonical_hash,
    canonical_json,
    classify,
    content_hash,
    derive_classification,
    has_fabricated_before_after_pair,
    hashes_match,
    manifest_covers_all_events,
    remediation_evidence_refs,
)
from driftzero.truth_engine.impact import ImpactOutcome, ImpactResolution
from driftzero.truth_engine.proof_generator import (
    ProofCondition,
    ProofContext,
    ProofGenerationError,
    ProofValidationFailure,
    ProofValidator,
    compute_proof_hash,
    derive_proof_id,
    evaluate_proof_invariants,
    generate_change_proof,
)
from driftzero.truth_engine.validation import (
    Crossing,
    ValidationLayer,
    validate_delivery_result,
    validate_media_output,
)

SYNTHETIC = DataClassification(labels=[ClassificationLabel.SYNTHETIC])
WF = "wf-001"
ARTIFACT = "wi-packing-standard-001"
T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
BEFORE_HASH = content_hash('{"label_position":"LEFT"}')
AFTER_HASH = content_hash('{"label_position":"TOP_RIGHT"}')
RECEIPT = "gs://evidence/delivery/receipt-001.json"


def make_change() -> ApprovedChange:
    return ApprovedChange(
        change_id="chg-1",
        source_procedure_id="proc-warehouse-packing",
        source_version="v2",
        previous_version="v1",
        operation_id="packing_label_placement",
        requirement_id="label_position",
        previous_value="LEFT",
        current_value="TOP_RIGHT",
        authorized_scope=[ARTIFACT],
        approved_status="APPROVED",
        source_evidence_ref="fixtures/source_procedure_v2.json",
        received_at=T0,
        data_classification=SYNTHETIC,
    )


def make_mutation() -> MutationEvidence:
    return MutationEvidence(
        artifact_id=ARTIFACT,
        before_ref="gs://evidence/before.json",
        after_ref="gs://evidence/after.json",
        before_hash=BEFORE_HASH,
        after_hash=AFTER_HASH,
        before_value="LEFT",
        after_value="TOP_RIGHT",
        patch_description="label_position LEFT -> TOP_RIGHT",
        action_id="act-remediate-001",
        data_classification=SYNTHETIC,
    )


def make_no_op() -> NoOpEvidence:
    return NoOpEvidence(
        artifact_id=ARTIFACT,
        evaluated_artifact_ref="gs://evidence/evaluated.json",
        evaluated_artifact_hash=AFTER_HASH,
        observed_value="TOP_RIGHT",
        expected_value="TOP_RIGHT",
        no_op_reason="artifact already represented the approved value",
        compliance_basis="requirements.label_position",
        data_classification=SYNTHETIC,
    )


def make_event(
    *, event_id: str, sequence: int, observation: ObservedPosition, result: VerificationResult
) -> VerificationEvent:
    return VerificationEvent(
        event_id=event_id,
        submission_id=f"sub-{sequence}",
        workflow_id=WF,
        event_sequence=sequence,
        raw_evidence_ref=f"gs://evidence/{event_id}.jpg",
        derived_observation=observation,
        expected_value="TOP_RIGHT",
        verification_result=result,
        timestamp=T0,
        data_classification=SYNTHETIC,
    )


FAIL_EVENT = make_event(
    event_id="ev-1", sequence=1, observation=ObservedPosition.LEFT, result=VerificationResult.FAIL
)
INCONCLUSIVE_EVENT = make_event(
    event_id="ev-1i",
    sequence=1,
    observation=ObservedPosition.INCONCLUSIVE,
    result=VerificationResult.INCONCLUSIVE,
)
PASS_EVENT = make_event(
    event_id="ev-2",
    sequence=2,
    observation=ObservedPosition.TOP_RIGHT,
    result=VerificationResult.PASS,
)


def make_workflow(state: WorkflowState = WorkflowState.VERIFICATION_PASSED) -> Workflow:
    return Workflow(
        workflow_id=WF,
        change_id="chg-1",
        source_version="v2",
        state=state,
        affected_artifact_id=ARTIFACT,
        candidate_artifact_refs=[ARTIFACT],
        delivery_status="DELIVERED",
        delivery_ref=RECEIPT,
        worker_id="worker-opaque-01",
        created_at=T0,
        updated_at=T1,
        event_sequence=2,
        data_classification=SYNTHETIC,
    )


def make_impact() -> ImpactResolution:
    return ImpactResolution(
        outcome=ImpactOutcome.SINGLE_QUALIFIED_TARGET,
        affected_artifact_id=ARTIFACT,
        qualified_artifact_ids=(ARTIFACT,),
        candidate_artifact_refs=(ARTIFACT,),
        qualifications=(),
        impact_reason="exactly one candidate qualified",
        requires_review=False,
    )


_DEFAULT = object()
"""Sentinel: distinguishes "use the default" from "explicitly absent"."""


def make_context(
    *,
    evidence=_DEFAULT,
    events=None,
    state=WorkflowState.VERIFICATION_PASSED,
    history=(),
    applicable=True,
    receipt=RECEIPT,
    rejected=(),
    workflow=None,
) -> ProofContext:
    # The manifest always records what the workflow captured. Absence of an
    # authoritative receipt / remediation record is modelled on the CONTEXT, which is
    # what the invariants read — the manifest keeps whatever reference was written.
    resolved = make_mutation() if evidence is _DEFAULT else evidence
    manifest_evidence = resolved if resolved is not None else make_mutation()
    events = list(events if events is not None else [FAIL_EVENT, PASS_EVENT])
    manifest = assemble_evidence_manifest(
        source_change_ref="fixtures/hero_change.json",
        affected_artifact_ref=ARTIFACT,
        remediation_evidence=manifest_evidence,
        delivery_ref=receipt or "gs://evidence/delivery/unresolved.json",
        verification_events=events,
        state_transition_refs=["log/transitions.json"],
        rejected_result_refs=list(rejected),
    )
    return ProofContext(
        workflow=workflow or make_workflow(state),
        change=make_change(),
        impact=make_impact(),
        remediation_evidence=resolved,
        manifest=manifest,
        verification_events=events,
        state_history=list(history) or [WorkflowState.CHANGE_RECEIVED],
        source_version_applicable=applicable,
        delivery_receipt_ref=receipt,
    )


# ============================ T039 — hashing ==========================================


def test_canonical_hash_is_stable_across_repeated_calls() -> None:
    payload = {"b": 2, "a": 1}
    assert len({canonical_hash(payload) for _ in range(50)}) == 1


def test_key_ordering_does_not_alter_canonical_hash() -> None:
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_altered_content_changes_the_hash() -> None:
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})
    assert content_hash("LEFT") != content_hash("TOP_RIGHT")


def test_hash_is_stable_across_a_fresh_interpreter() -> None:
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from driftzero.truth_engine.evidence import canonical_hash;"
        "print(canonical_hash({'b': 2, 'a': 1, 'nested': {'z': 1, 'y': 2}}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == canonical_hash({"b": 2, "a": 1, "nested": {"z": 1, "y": 2}})


def test_hashes_match_detects_alteration() -> None:
    original = '{"label_position":"LEFT"}'
    assert hashes_match(content_hash(original), original)
    assert not hashes_match(content_hash(original), '{"label_position":"TOP_RIGHT"}')


def test_hash_docs_do_not_claim_signature_semantics() -> None:
    """The claim boundary is stated, and no attestation language is used."""
    from driftzero.truth_engine import evidence, proof_generator

    for module in (evidence, proof_generator):
        doc = (module.__doc__ or "").lower()
        assert "not" in doc
        for overclaim in ("digital signature", "non-repudiation", "attestation"):
            assert overclaim in doc, f"{module.__name__} must name and deny {overclaim}"
    assert "signature verification" in (ProofValidator.__doc__ or "").lower()


# ============================ T040 — classification and lineage =======================


def test_real_plus_synthetic_is_representable() -> None:
    """A real model execution over a synthetic business fixture."""
    c = derive_classification(
        labels=[ClassificationLabel.REAL, ClassificationLabel.SYNTHETIC],
        source_ref="fixtures/hero_change.json",
        source_classification=[ClassificationLabel.SYNTHETIC],
        relationship="input_to",
    )
    assert c.has(ClassificationLabel.REAL) and c.has(ClassificationLabel.SYNTHETIC)
    assert c.lineage[0].source_ref == "fixtures/hero_change.json"


def test_derived_lineage_references_its_source() -> None:
    c = derive_classification(
        labels=[ClassificationLabel.DERIVED, ClassificationLabel.REAL],
        source_ref="gs://evidence/photo.jpg",
        source_classification=[ClassificationLabel.REAL],
        relationship="observed_from",
    )
    assert c.has(ClassificationLabel.DERIVED)
    assert c.lineage[0].relationship == "observed_from"
    assert c.lineage[0].source_classification == [ClassificationLabel.REAL]


def test_simulated_is_explicitly_representable() -> None:
    c = classify([ClassificationLabel.SIMULATED])
    assert c.has(ClassificationLabel.SIMULATED)
    assert not c.has(ClassificationLabel.REAL)


def test_classification_is_not_a_single_mutually_exclusive_enum() -> None:
    c = classify([ClassificationLabel.DERIVED, ClassificationLabel.REAL])
    assert len(c.labels) == 2


# ============================ T041 — evidence manifest ================================


def test_manifest_preserves_historical_fail_and_later_pass() -> None:
    context = make_context(events=[FAIL_EVENT, PASS_EVENT])
    assert context.manifest.verification_refs == ["ev-1", "ev-2"]
    assert manifest_covers_all_events(context.manifest, [FAIL_EVENT, PASS_EVENT])


def test_manifest_preserves_historical_inconclusive_and_later_pass() -> None:
    context = make_context(events=[INCONCLUSIVE_EVENT, PASS_EVENT])
    assert context.manifest.verification_refs == ["ev-1i", "ev-2"]


def test_manifest_is_not_built_from_latest_success_only() -> None:
    context = make_context(events=[FAIL_EVENT, INCONCLUSIVE_EVENT, PASS_EVENT])
    assert len(context.manifest.verification_refs) == 3


def test_mutation_and_no_op_produce_different_manifest_shapes() -> None:
    mutation, no_op = make_mutation(), make_no_op()
    assert remediation_evidence_refs(mutation) == (mutation.before_ref, mutation.after_ref)
    assert remediation_evidence_refs(no_op) == (no_op.evaluated_artifact_ref,)


def test_no_op_manifest_contains_no_fabricated_before_after_pair() -> None:
    context = make_context(evidence=make_no_op())
    assert len(context.manifest.remediation_evidence_refs) == 1
    assert not has_fabricated_before_after_pair(context.manifest, make_no_op())


def test_duplicated_ref_pair_is_detected_as_fabricated() -> None:
    manifest = make_context().manifest.model_copy(
        update={"remediation_evidence_refs": ["gs://same.json", "gs://same.json"]}
    )
    assert has_fabricated_before_after_pair(manifest, make_mutation())


def test_rejected_result_refs_are_retained_for_audit() -> None:
    context = make_context(rejected=["security/tool_poisoning_rejected.json"])
    assert context.manifest.rejected_result_refs == ["security/tool_poisoning_rejected.json"]


# ============================ T042 — trust boundary ===================================


def test_agent_delivery_assertion_without_receipt_is_rejected() -> None:
    result = DeliveryResult(
        worker_id="worker-opaque-01",
        delivery_mechanism="web_notification",
        delta_content="Place the label on the TOP-RIGHT.",
        delivered=True,
        delivery_evidence_ref="agent-said-so",
    )
    outcome = validate_delivery_result(
        result,
        expected_worker_id="worker-opaque-01",
        expected_mechanism="web_notification",
        resolvable_receipt_refs=frozenset(),
        rejection_ref="security/delivery_assertion_rejected.json",
    )
    assert outcome.rejected
    assert ValidationLayer.POSITIVE_RECEIPT in outcome.failed_layers
    assert outcome.rejection_ref == "security/delivery_assertion_rejected.json"


def test_resolvable_receipt_is_accepted() -> None:
    result = DeliveryResult(
        worker_id="worker-opaque-01",
        delivery_mechanism="web_notification",
        delta_content="delta",
        delivered=True,
        delivery_evidence_ref=RECEIPT,
    )
    outcome = validate_delivery_result(
        result,
        expected_worker_id="worker-opaque-01",
        expected_mechanism="web_notification",
        resolvable_receipt_refs=frozenset({RECEIPT}),
        rejection_ref="ignored",
    )
    assert outcome.accepted
    assert outcome.crossing is Crossing.DELIVERY_RESULT


def test_media_generation_success_requires_classification_and_resolvable_ref() -> None:
    ok = validate_media_output(
        asset_ref="gs://evidence/veo.mp4",
        resolvable_asset_refs=frozenset({"gs://evidence/veo.mp4"}),
        classification_recorded=True,
        rejection_ref="r",
    )
    assert ok.accepted
    bad = validate_media_output(
        asset_ref="gs://evidence/veo.mp4",
        resolvable_asset_refs=frozenset({"gs://evidence/veo.mp4"}),
        classification_recorded=False,
        rejection_ref="r",
    )
    assert bad.rejected
    assert ValidationLayer.CLASSIFICATION_REQUIRED in bad.failed_layers


# ============================ T043 — seven invariants =================================


def test_all_seven_conditions_pass_makes_proof_eligible() -> None:
    result = evaluate_proof_invariants(make_context())
    assert result.eligible is True
    assert result.failed_conditions == ()
    assert len(result.conditions) == 7
    assert result.satisfied_count == 7


def test_no_op_path_is_independently_eligible() -> None:
    result = evaluate_proof_invariants(make_context(evidence=make_no_op()))
    assert result.eligible is True


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"applicable": False}, ProofCondition.C1_SOURCE_CHANGE_APPLICABLE),
        ({"receipt": None}, ProofCondition.C4_DELTA_DELIVERED),
        ({"events": [FAIL_EVENT]}, ProofCondition.C5_LATEST_VERIFICATION_PASS),
        ({"events": [INCONCLUSIVE_EVENT]}, ProofCondition.C5_LATEST_VERIFICATION_PASS),
        ({"state": WorkflowState.SUPERSEDED}, ProofCondition.C7_STATE_COMPATIBLE),
        ({"state": WorkflowState.FAILED}, ProofCondition.C7_STATE_COMPATIBLE),
        ({"state": WorkflowState.REVIEW_REQUIRED}, ProofCondition.C7_STATE_COMPATIBLE),
        ({"state": WorkflowState.VERIFICATION_FAILED}, ProofCondition.C7_STATE_COMPATIBLE),
        ({"state": WorkflowState.VERIFICATION_INCONCLUSIVE}, ProofCondition.C7_STATE_COMPATIBLE),
        ({"evidence": None}, ProofCondition.C3_REMEDIATED_OR_NO_OP),
    ],
)
def test_each_failing_condition_blocks_eligibility(
    kwargs: dict[str, object], expected: ProofCondition
) -> None:
    result = evaluate_proof_invariants(make_context(**kwargs))
    assert result.eligible is False
    assert expected in result.failed_conditions


def test_impact_not_single_target_blocks_condition_2() -> None:
    context = make_context()
    multi = ImpactResolution(
        outcome=ImpactOutcome.MULTIPLE_QUALIFIED_TARGETS,
        affected_artifact_id=None,
        qualified_artifact_ids=(ARTIFACT, "wi-b"),
        candidate_artifact_refs=(ARTIFACT, "wi-b"),
        qualifications=(),
        impact_reason="two qualified",
        requires_review=True,
    )
    result = evaluate_proof_invariants(
        ProofContext(**{**context.__dict__, "impact": multi})
    )
    assert ProofCondition.C2_IMPACT_DETERMINED in result.failed_conditions


def test_incomplete_manifest_blocks_condition_6() -> None:
    context = make_context(events=[FAIL_EVENT, PASS_EVENT])
    trimmed = context.manifest.model_copy(update={"verification_refs": ["ev-2"]})
    result = evaluate_proof_invariants(ProofContext(**{**context.__dict__, "manifest": trimmed}))
    assert ProofCondition.C6_EVIDENCE_TRACEABLE in result.failed_conditions


def test_six_of_seven_never_passes() -> None:
    result = evaluate_proof_invariants(make_context(receipt=None))
    assert result.satisfied_count == 6
    assert result.eligible is False


def test_rejected_results_do_not_satisfy_any_condition() -> None:
    """Retaining a rejected result for audit never advances completion."""
    with_rejects = evaluate_proof_invariants(
        make_context(rejected=["security/tool_poisoning_rejected.json"], receipt=None)
    )
    assert with_rejects.eligible is False
    assert ProofCondition.C4_DELTA_DELIVERED in with_rejects.failed_conditions


# ============================ condition 7 semantics ===================================


def test_historical_fail_then_current_pass_satisfies_condition_7() -> None:
    result = evaluate_proof_invariants(
        make_context(
            events=[FAIL_EVENT, PASS_EVENT],
            history=[
                WorkflowState.AWAITING_FIELD_VERIFICATION,
                WorkflowState.VERIFICATION_FAILED,
                WorkflowState.AWAITING_FIELD_VERIFICATION,
            ],
        )
    )
    assert result.conditions[ProofCondition.C7_STATE_COMPATIBLE] is True
    assert result.eligible is True


def test_historical_inconclusive_then_current_pass_satisfies_condition_7() -> None:
    result = evaluate_proof_invariants(
        make_context(
            events=[INCONCLUSIVE_EVENT, PASS_EVENT],
            history=[
                WorkflowState.AWAITING_FIELD_VERIFICATION,
                WorkflowState.VERIFICATION_INCONCLUSIVE,
            ],
        )
    )
    assert result.conditions[ProofCondition.C7_STATE_COMPATIBLE] is True
    assert result.eligible is True


@pytest.mark.parametrize("terminal", [WorkflowState.SUPERSEDED, WorkflowState.FAILED])
def test_terminal_non_success_permanently_blocks_even_in_history(
    terminal: WorkflowState,
) -> None:
    """Everything else looks good; a terminal state anywhere still blocks."""
    result = evaluate_proof_invariants(make_context(history=[terminal]))
    assert result.conditions[ProofCondition.C7_STATE_COMPATIBLE] is False
    assert result.eligible is False


def test_review_required_in_history_blocks() -> None:
    result = evaluate_proof_invariants(make_context(history=[WorkflowState.REVIEW_REQUIRED]))
    assert result.conditions[ProofCondition.C7_STATE_COMPATIBLE] is False


def test_condition_7_is_not_was_fail_ever_seen() -> None:
    """The corrected semantics: a historical FAIL alone must not block."""
    with_fail = make_context(history=[WorkflowState.VERIFICATION_FAILED])
    assert evaluate_proof_invariants(with_fail).conditions[
        ProofCondition.C7_STATE_COMPATIBLE
    ] is True


def test_outdated_event_cannot_override_current_pass() -> None:
    """A late-delivered older FAIL does not change the authoritative verification."""
    result = evaluate_proof_invariants(make_context(events=[PASS_EVENT, FAIL_EVENT]))
    assert result.conditions[ProofCondition.C5_LATEST_VERIFICATION_PASS] is True
    assert result.eligible is True


# ============================ T044/T046 — generation and singularity ==================


def test_generation_refuses_when_not_eligible() -> None:
    with pytest.raises(ProofGenerationError) as exc:
        generate_change_proof(make_context(receipt=None), completion_timestamp=T1)
    assert ProofCondition.C4_DELTA_DELIVERED in exc.value.failed_conditions


@pytest.mark.parametrize(
    "state",
    [
        WorkflowState.SUPERSEDED,
        WorkflowState.FAILED,
        WorkflowState.REVIEW_REQUIRED,
        WorkflowState.VERIFICATION_FAILED,
        WorkflowState.VERIFICATION_INCONCLUSIVE,
    ],
)
def test_blocked_states_cannot_produce_a_proof(state: WorkflowState) -> None:
    with pytest.raises(ProofGenerationError):
        generate_change_proof(make_context(state=state), completion_timestamp=T1)


def test_canonical_proof_is_deterministic_for_identical_inputs() -> None:
    a = generate_change_proof(make_context(), completion_timestamp=T1)
    b = generate_change_proof(make_context(), completion_timestamp=T1)
    assert a.content_hash == b.content_hash
    assert a.model_dump(mode="json") == b.model_dump(mode="json")


def test_proof_hash_covers_content_changes() -> None:
    proof = generate_change_proof(make_context(), completion_timestamp=T1)
    tampered = proof.model_copy(update={"worker_id": "worker-other"})
    assert compute_proof_hash(tampered) != proof.content_hash


def test_both_remediation_paths_generate_valid_proofs() -> None:
    mutation_proof = generate_change_proof(make_context(), completion_timestamp=T1)
    no_op_proof = generate_change_proof(
        make_context(evidence=make_no_op()), completion_timestamp=T1
    )
    assert isinstance(mutation_proof.remediation_evidence, MutationEvidence)
    assert isinstance(no_op_proof.remediation_evidence, NoOpEvidence)
    assert len(no_op_proof.evidence_manifest.remediation_evidence_refs) == 1


def test_repeated_generation_resolves_to_one_logical_proof() -> None:
    first = generate_change_proof(make_context(), completion_timestamp=T1)
    retry = generate_change_proof(
        make_context(), completion_timestamp=T1, existing_proof=first
    )
    assert retry is first
    assert retry.proof_id == first.proof_id == derive_proof_id(WF)


def test_proof_identity_is_independent_of_timestamp() -> None:
    """A retry at a different moment cannot create proof-2."""
    a = generate_change_proof(make_context(), completion_timestamp=T1)
    b = generate_change_proof(
        make_context(), completion_timestamp=datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    )
    assert a.proof_id == b.proof_id
    assert a.content_hash != b.content_hash, "timestamp is inside the canonical document"


def test_proof_id_differs_per_workflow() -> None:
    assert derive_proof_id(WF) != derive_proof_id("wf-002")


# ============================ T045 — validation =======================================


def test_valid_proof_revalidates() -> None:
    context = make_context()
    proof = generate_change_proof(context, completion_timestamp=T1)
    result = ProofValidator().validate(proof, context)
    assert result.valid is True
    assert result.failures == ()


def test_content_hash_mismatch_invalidates_proof() -> None:
    context = make_context()
    proof = generate_change_proof(context, completion_timestamp=T1)
    result = ProofValidator().validate(
        proof, context, resolved_contents={"gs://evidence/after.json": '{"tampered":true}'}
    )
    assert result.valid is False
    assert ProofValidationFailure.CONTENT_HASH_MISMATCH in result.failures
    assert result.mismatched_refs == ("gs://evidence/after.json",)


def test_unchanged_content_passes_revalidation() -> None:
    context = make_context()
    proof = generate_change_proof(context, completion_timestamp=T1)
    result = ProofValidator().validate(
        proof,
        context,
        resolved_contents={"gs://evidence/after.json": '{"label_position":"TOP_RIGHT"}'},
    )
    assert result.valid is True


def test_tampered_proof_document_fails_revalidation() -> None:
    context = make_context()
    proof = generate_change_proof(context, completion_timestamp=T1)
    tampered = proof.model_copy(update={"worker_id": "worker-other"})
    result = ProofValidator().validate(tampered, context)
    assert result.valid is False
    assert ProofValidationFailure.PROOF_HASH_MISMATCH in result.failures


def test_forged_proof_identity_fails_revalidation() -> None:
    context = make_context()
    proof = generate_change_proof(context, completion_timestamp=T1)
    forged = proof.model_copy(update={"proof_id": "proof-2"})
    result = ProofValidator().validate(forged, context)
    assert result.valid is False
    assert ProofValidationFailure.PROOF_IDENTITY_MISMATCH in result.failures


def test_validation_fails_when_conditions_no_longer_hold() -> None:
    """A proof issued earlier does not stay valid if the workflow was superseded."""
    context = make_context()
    proof = generate_change_proof(context, completion_timestamp=T1)
    result = ProofValidator().validate(proof, make_context(state=WorkflowState.SUPERSEDED))
    assert result.valid is False
    assert ProofValidationFailure.UNMET_COMPLETION_CONDITION in result.failures
    assert ProofCondition.C7_STATE_COMPATIBLE in result.failed_conditions


# ============================ authority boundary ======================================


def test_no_agent_path_can_set_proof_complete() -> None:
    """Completion is derived from validated structures; there is no trusted flag."""
    from driftzero.truth_engine import proof_generator

    exported = set(dir(proof_generator))
    for forbidden in ("trusted", "force_complete", "mark_proof_complete", "set_completion"):
        assert forbidden not in exported

    fields = set(ChangeProof.model_fields)
    for forbidden in ("proof_valid", "completion", "confidence", "trusted"):
        assert forbidden not in fields


def test_no_new_workflow_states_were_introduced() -> None:
    assert len(WorkflowState) == 13
    from driftzero.truth_engine.proof_generator import ProofCondition as PC
    from driftzero.truth_engine.proof_generator import ProofValidationFailure as PVF

    assert {c.value for c in PC}.isdisjoint({s.value for s in WorkflowState})
    assert {f.value for f in PVF}.isdisjoint({s.value for s in WorkflowState})


def test_generation_performs_no_workflow_transition() -> None:
    from driftzero.truth_engine import proof_generator

    exported = set(dir(proof_generator))
    for forbidden in ("transition", "assert_transition_allowed"):
        assert forbidden not in exported
