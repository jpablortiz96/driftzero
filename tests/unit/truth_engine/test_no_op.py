"""T055 — NO_OP path and the NO_OP-vs-reconciled-MUTATION race (SC-003).

The decisive scenario for evidence honesty: two histories reach the same physical value
``TOP_RIGHT`` and must produce different evidence, because one workflow mutated the
artifact and the other did not.
"""

from __future__ import annotations

from driftzero.models.action import ActionType
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.truth_engine.actions import (
    ActionLedger,
    ReconciliationOutcome,
    build_remediation_intent,
    no_op_admissible,
    reconcile_mutation,
    was_ever_dispatched,
)
from driftzero.truth_engine.evidence import has_fabricated_before_after_pair
from driftzero.truth_engine.idempotency import derive_remediation_action_id
from driftzero.truth_engine.proof_generator import (
    ProofCondition,
    evaluate_proof_invariants,
    generate_change_proof,
)

from ._acceptance import (
    AFTER_HASH,
    ARTIFACT,
    BEFORE_HASH,
    SYNTHETIC,
    T0,
    T_PASS,
    WF,
    make_artifact,
    make_change,
    make_manifest,
    make_no_op,
    make_proof_context,
)


def plan_remediation(ledger: ActionLedger) -> str:
    change, artifact = make_change(), make_artifact()
    action_id = derive_remediation_action_id(
        workflow_id=WF, change=change, artifact_id=ARTIFACT
    )
    ledger.plan(
        action_id=action_id,
        workflow_id=WF,
        action_type=ActionType.REMEDIATE_ARTIFACT,
        target_ref=ARTIFACT,
        intent=build_remediation_intent(
            change=change, artifact=artifact, expected_before_hash=BEFORE_HASH
        ),
        occurred_at=T0,
    )
    return action_id


def mutated_artifact():
    return make_artifact(
        current_value="TOP_RIGHT",
        requirements={"label_position": "TOP_RIGHT", "box_size": "STANDARD", "seal_type": "TAPE"},
    )


# ---------------------------------------------------------------- history A: NO_OP


def test_artifact_compliant_before_any_dispatch_is_a_valid_no_op() -> None:
    """Impact-time LEFT, then an external legitimate cause sets TOP_RIGHT first."""
    ledger = ActionLedger()
    action_id = plan_remediation(ledger)

    assert was_ever_dispatched(ledger, action_id) is False
    assert no_op_admissible(ledger, action_id) is True

    evidence = make_no_op()
    assert isinstance(evidence, NoOpEvidence)
    assert evidence.observed_value == evidence.expected_value == "TOP_RIGHT"


def test_no_op_evidence_carries_a_single_evaluated_state() -> None:
    evidence = make_no_op()
    manifest = make_manifest(evidence)
    assert manifest.remediation_evidence_refs == [evidence.evaluated_artifact_ref]
    assert not has_fabricated_before_after_pair(manifest, evidence)
    assert not hasattr(evidence, "before_ref")
    assert not hasattr(evidence, "after_ref")


def test_no_op_satisfies_completion_condition_3b() -> None:
    result = evaluate_proof_invariants(make_proof_context(evidence=make_no_op()))
    assert result.conditions[ProofCondition.C3_REMEDIATED_OR_NO_OP] is True
    assert result.eligible is True


def test_no_op_proof_generates_and_contains_no_mutation_pair() -> None:
    proof = generate_change_proof(make_proof_context(evidence=make_no_op()))
    assert isinstance(proof.remediation_evidence, NoOpEvidence)
    assert len(proof.evidence_manifest.remediation_evidence_refs) == 1


# ---------------------------------------------------------------- history B: MUTATION


def test_crash_after_this_workflow_mutated_yields_reconciled_mutation() -> None:
    ledger = ActionLedger()
    action_id = plan_remediation(ledger)
    ledger.mark_attempted(action_id, occurred_at=T0)

    result = reconcile_mutation(
        ledger,
        action_id,
        observed_artifact=mutated_artifact(),
        observed_after_hash=AFTER_HASH,
        after_ref="gs://evidence/after.json",
        change=make_change(),
        source_version_applicable=True,
        occurred_at=T_PASS,
        data_classification=SYNTHETIC,
    )
    assert result.outcome is ReconciliationOutcome.RECONCILED_MUTATION
    assert isinstance(result.evidence, MutationEvidence)
    assert result.evidence.reconciled is True
    assert no_op_admissible(ledger, action_id) is False


# ---------------------------------------------------------------- the contrast


def test_same_final_value_two_histories_two_evidence_types() -> None:
    """Both artifacts end at TOP_RIGHT; only the recorded history separates them."""
    # A — never dispatched, so NO_OP remains admissible.
    ledger_a = ActionLedger()
    action_a = plan_remediation(ledger_a)
    evidence_a = make_no_op()

    # B — dispatched by this workflow, then crashed; reconciliation applies.
    ledger_b = ActionLedger()
    action_b = plan_remediation(ledger_b)
    ledger_b.mark_attempted(action_b, occurred_at=T0)
    evidence_b = reconcile_mutation(
        ledger_b,
        action_b,
        observed_artifact=mutated_artifact(),
        observed_after_hash=AFTER_HASH,
        after_ref="gs://evidence/after.json",
        change=make_change(),
        source_version_applicable=True,
        occurred_at=T_PASS,
        data_classification=SYNTHETIC,
    ).evidence

    assert evidence_a.observed_value == evidence_b.after_value == "TOP_RIGHT"
    assert evidence_a.remediation_type == "NO_OP"
    assert evidence_b.remediation_type == "MUTATION"
    assert no_op_admissible(ledger_a, action_a) is True
    assert no_op_admissible(ledger_b, action_b) is False


def test_reconciliation_never_downgrades_a_mutation_into_a_no_op() -> None:
    ledger = ActionLedger()
    action_id = plan_remediation(ledger)
    ledger.mark_attempted(action_id, occurred_at=T0)
    result = reconcile_mutation(
        ledger,
        action_id,
        observed_artifact=mutated_artifact(),
        observed_after_hash=AFTER_HASH,
        after_ref="gs://evidence/after.json",
        change=make_change(),
        source_version_applicable=True,
        occurred_at=T_PASS,
        data_classification=SYNTHETIC,
    )
    assert not isinstance(result.evidence, NoOpEvidence)
