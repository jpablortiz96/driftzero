"""T076 — Crossing 2: RemediationEvidence validation.

Every case runs the **real** chain: authorization (T075) → mutation (T073) → the
evidence the tool actually produced → this crossing. Nothing hand-builds evidence and
calls it validated, and no tampered field is accepted because it merely looks plausible.

Fully offline: in-memory repository, injected clock, no model, no cloud.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.agents.remediation import (  # noqa: E402
    RemediationAgent,
    RemediationStatus,
)
from driftzero.capabilities import MutationCapabilityBroker  # noqa: E402
from driftzero.models.action import ActionStatus  # noqa: E402
from driftzero.models.remediation import MutationEvidence, NoOpEvidence  # noqa: E402
from driftzero.orchestration import (  # noqa: E402
    RemediationBoundaryResult,
    RemediationCrossingContext,
    RemediationRejection,
    accept_remediation_evidence,
)
from driftzero.tools.artifact_mutation import (  # noqa: E402
    InMemoryArtifactRepository,
    MutationToolContext,
    UncertainWriteError,
    artifact_content_hash,
)
from driftzero.truth_engine.actions import ActionLedger  # noqa: E402
from driftzero.truth_engine.validation import Crossing  # noqa: E402

from ._fakes import ARTIFACT_ID, make_change, make_classification  # noqa: E402
from .test_artifact_mutation import (  # noqa: E402
    ACTION_ID,
    UNRELATED_PROSE,
    WORKFLOW_ID,
    _Clock,
    make_hero_artifact,
)
from .test_remediation_agent import make_intent, make_unscoped_context  # noqa: E402

REJECTION_REF = "rej-crossing2-001"


def make_crossing_context(
    tool_context: MutationToolContext, **overrides: object
) -> RemediationCrossingContext:
    kwargs: dict[str, object] = {
        "ledger": tool_context.ledger,
        "repository": tool_context.repository,
        "change": make_change(),
        "action_id": ACTION_ID,
        "expected_artifact_id": ARTIFACT_ID,
        "expected_requirement_id": "label_position",
        "source_version_applicable": True,
        "rejection_ref": REJECTION_REF,
    }
    kwargs.update(overrides)
    return RemediationCrossingContext(**kwargs)  # type: ignore[arg-type]


def run_hero_mutation(broker: MutationCapabilityBroker | None = None):  # type: ignore[no-untyped-def]
    """The full authorized chain, returning (tool_context, remediation result)."""
    issuer = broker or MutationCapabilityBroker()
    context = make_unscoped_context(issuer)
    result = RemediationAgent(broker=issuer).remediate(make_intent(), context)
    assert result.status is RemediationStatus.MUTATED
    return context, result


class _CrashOnceRepository(InMemoryArtifactRepository):
    """Commits the write, then loses the response — the post-dispatch uncertainty path."""

    fail_next = True

    def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
        committed = super().apply_requirement(
            artifact_id, requirement_id, expected_before, new_value
        )
        if self.fail_next:
            self.fail_next = False
            raise UncertainWriteError("crash after commit")
        return committed


# ============================ hero mutation case ======================================


def test_hero_mutation_evidence_is_accepted() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(result.evidence, context=make_crossing_context(context))

    assert verdict.accepted is True
    assert verdict.accepted_evidence is result.evidence
    assert verdict.rejections == ()
    assert verdict.requires_review is False
    assert verdict.outcome.crossing is Crossing.REMEDIATION_EVIDENCE


def test_the_accepted_evidence_carries_the_hero_values() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(result.evidence, context=make_crossing_context(context))
    evidence = verdict.accepted_evidence

    assert isinstance(evidence, MutationEvidence)
    assert evidence.remediation_type == "MUTATION"
    assert evidence.before_value == "LEFT"
    assert evidence.after_value == "TOP_RIGHT"
    assert evidence.reconciled is False


def test_the_crossing_measures_hashes_against_authoritative_state() -> None:
    """Before-hash from the pre-dispatch intent; after-hash from the committed artifact."""
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(result.evidence, context=make_crossing_context(context))

    ledger_intent = context.ledger.require(ACTION_ID).intent
    committed = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]

    assert verdict.authoritative_before_hash == ledger_intent["expected_before_hash"]
    assert verdict.authoritative_after_hash == artifact_content_hash(committed)
    assert result.evidence.before_hash == verdict.authoritative_before_hash
    assert result.evidence.after_hash == verdict.authoritative_after_hash
    assert verdict.authoritative_before_hash != verdict.authoritative_after_hash


def test_the_hero_mutation_dispatched_exactly_once_and_left_prose_alone() -> None:
    context, result = run_hero_mutation()
    accept_remediation_evidence(result.evidence, context=make_crossing_context(context))
    committed = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]

    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]
    assert committed.requirements["label_position"] == "TOP_RIGHT"
    assert committed.requirements["instructions"] == UNRELATED_PROSE
    assert "LEFT" in committed.requirements["instructions"]


def test_validating_twice_does_not_mutate_or_dispatch_again() -> None:
    """The crossing is a pure verdict: accepting evidence changes nothing."""
    context, result = run_hero_mutation()
    crossing = make_crossing_context(context)
    before = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]

    first = accept_remediation_evidence(result.evidence, context=crossing)
    second = accept_remediation_evidence(result.evidence, context=crossing)

    assert first.accepted and second.accepted
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]
    assert context.repository.read(ARTIFACT_ID) == before  # type: ignore[union-attr]


# ============================ tampering is rejected ===================================


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("before_hash", "0" * 64, RemediationRejection.BEFORE_HASH_MISMATCH),
        ("after_hash", "1" * 64, RemediationRejection.AFTER_HASH_MISMATCH),
        ("artifact_id", "WI-GHOST", RemediationRejection.ARTIFACT_MISMATCH),
        ("after_value", "DIAGONAL", RemediationRejection.REQUIREMENT_MISMATCH),
    ],
)
def test_tampering_with_a_bound_field_is_rejected(
    field: str, value: str, expected: RemediationRejection
) -> None:
    context, result = run_hero_mutation()
    tampered = result.evidence.model_copy(update={field: value})
    verdict = accept_remediation_evidence(tampered, context=make_crossing_context(context))

    assert verdict.accepted is False
    assert verdict.accepted_evidence is None
    assert expected in verdict.rejections
    assert verdict.requires_review is True


def test_a_wrong_requirement_identity_is_rejected() -> None:
    """Requirement identity is bound to observable committed state, not to a claim."""
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence,
        context=make_crossing_context(context, expected_requirement_id="packing_mode"),
    )
    assert verdict.accepted is False
    assert RemediationRejection.REQUIREMENT_MISMATCH in verdict.rejections


def test_a_before_value_mismatch_is_rejected() -> None:
    context, result = run_hero_mutation()
    tampered = result.evidence.model_copy(update={"before_value": "DIAGONAL"})
    verdict = accept_remediation_evidence(tampered, context=make_crossing_context(context))
    assert verdict.accepted is False
    assert "SEMANTIC_INVARIANT" in verdict.failed_layers


def test_identical_hashes_cannot_pass_as_a_mutation() -> None:
    """A 'mutation' whose before and after are identical did not change anything."""
    context, result = run_hero_mutation()
    tampered = result.evidence.model_copy(update={"after_hash": result.evidence.before_hash})
    verdict = accept_remediation_evidence(tampered, context=make_crossing_context(context))
    assert verdict.accepted is False
    assert RemediationRejection.HASHES_IDENTICAL in verdict.rejections


def test_a_wrong_action_identity_is_rejected() -> None:
    context, result = run_hero_mutation()
    tampered = result.evidence.model_copy(update={"action_id": "act-someone-elses"})
    verdict = accept_remediation_evidence(tampered, context=make_crossing_context(context))
    assert verdict.accepted is False
    assert "EXPECTED_TOOL_IDENTITY" in verdict.failed_layers


def test_an_inapplicable_source_version_is_rejected() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence,
        context=make_crossing_context(context, source_version_applicable=False),
    )
    assert verdict.accepted is False
    assert "SOURCE_VERSION_APPLICABILITY" in verdict.failed_layers


def test_an_artifact_outside_authorized_scope_is_rejected() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence,
        context=make_crossing_context(context, change=make_change(authorized_scope=[])),
    )
    assert verdict.accepted is False
    assert "AUTHORIZATION_SCOPE" in verdict.failed_layers


def test_evidence_with_no_ledger_record_is_rejected() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence, context=make_crossing_context(context, ledger=ActionLedger())
    )
    assert verdict.accepted is False
    assert verdict.rejections == (RemediationRejection.NO_LEDGER_RECORD,)


def test_a_missing_artifact_is_rejected() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence,
        context=make_crossing_context(context, repository=InMemoryArtifactRepository({})),
    )
    assert verdict.accepted is False
    assert RemediationRejection.ARTIFACT_NOT_FOUND in verdict.rejections


# ============================ crash-reconciled case ===================================


def run_reconciled_mutation():  # type: ignore[no-untyped-def]
    broker = MutationCapabilityBroker()
    repository = _CrashOnceRepository({ARTIFACT_ID: make_hero_artifact()})
    context = make_unscoped_context(broker, repository=repository)
    agent = RemediationAgent(broker=broker)

    first = agent.remediate(make_intent(), context)
    assert first.status is RemediationStatus.TOOL_REJECTED
    recovery = agent.remediate(make_intent(), context)
    assert recovery.status is RemediationStatus.RECONCILED_MUTATION
    return context, recovery


def test_a_reconciled_mutation_is_accepted_and_stays_a_mutation() -> None:
    context, recovery = run_reconciled_mutation()
    verdict = accept_remediation_evidence(
        recovery.evidence, context=make_crossing_context(context)
    )

    assert verdict.accepted is True
    evidence = verdict.accepted_evidence
    assert isinstance(evidence, MutationEvidence)
    assert evidence.remediation_type == "MUTATION"
    assert evidence.reconciled is True
    assert not isinstance(evidence, NoOpEvidence)
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]


def test_the_reconciled_flag_must_match_the_ledger() -> None:
    """``reconciled=True`` is admissible only for an action actually reconciled."""
    context, result = run_hero_mutation()
    assert context.ledger.require(ACTION_ID).reconciled is False
    lying = result.evidence.model_copy(update={"reconciled": True})
    verdict = accept_remediation_evidence(lying, context=make_crossing_context(context))

    assert verdict.accepted is False
    assert RemediationRejection.RECONCILED_FLAG_MISMATCH in verdict.rejections


def test_a_reconciled_mutation_cannot_hide_its_reconciliation() -> None:
    context, recovery = run_reconciled_mutation()
    assert context.ledger.require(ACTION_ID).reconciled is True
    lying = recovery.evidence.model_copy(update={"reconciled": False})
    verdict = accept_remediation_evidence(lying, context=make_crossing_context(context))

    assert verdict.accepted is False
    assert RemediationRejection.RECONCILED_FLAG_MISMATCH in verdict.rejections


# ============================ NO_OP case ==============================================


def make_compliant_context(broker: MutationCapabilityBroker):  # type: ignore[no-untyped-def]
    compliant = make_hero_artifact(
        current_value="TOP_RIGHT",
        requirements={
            "label_position": "TOP_RIGHT",
            "instructions": UNRELATED_PROSE,
            "packing_mode": "STANDARD",
        },
    )
    return make_unscoped_context(broker, artifact=compliant), compliant


def run_no_op():  # type: ignore[no-untyped-def]
    broker = MutationCapabilityBroker()
    context, compliant = make_compliant_context(broker)
    result = RemediationAgent(broker=broker).remediate(
        make_intent(expected_before_hash=artifact_content_hash(compliant)), context
    )
    assert result.status is RemediationStatus.NO_OP
    return context, result


def test_a_legitimate_no_op_is_accepted_with_zero_dispatch() -> None:
    context, result = run_no_op()
    # A NO_OP never reaches the ledger, so the crossing is given the intent explicitly.
    ledger = ActionLedger()
    from driftzero.models.action import ActionType
    from driftzero.truth_engine.actions import build_remediation_intent

    compliant = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    ledger.plan(
        action_id=ACTION_ID,
        workflow_id=WORKFLOW_ID,
        action_type=ActionType.REMEDIATE_ARTIFACT,
        target_ref=ARTIFACT_ID,
        intent=build_remediation_intent(
            change=make_change(),
            artifact=compliant,
            expected_before_hash=artifact_content_hash(compliant),
        ),
        occurred_at=_Clock()(),
    )
    verdict = accept_remediation_evidence(
        result.evidence, context=make_crossing_context(context, ledger=ledger)
    )

    assert verdict.accepted is True
    assert isinstance(verdict.accepted_evidence, NoOpEvidence)
    assert verdict.accepted_evidence.remediation_type == "NO_OP"
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_no_op_is_rejected_once_a_mutation_was_dispatched() -> None:
    """Matching the after-value is necessary but never sufficient for NO_OP."""
    context, result = run_no_op()
    mutated_context, _ = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence,
        context=make_crossing_context(context, ledger=mutated_context.ledger),
    )
    assert verdict.accepted is False
    assert RemediationRejection.NO_OP_NOT_ADMISSIBLE in verdict.rejections


def test_a_no_op_cannot_carry_a_before_after_pair() -> None:
    """Structural: the schema has no field for a change that did not happen."""
    for forbidden in ("before_ref", "after_ref", "before_value", "after_value", "reconciled"):
        assert forbidden not in NoOpEvidence.model_fields


def test_a_no_op_hash_must_match_the_evaluated_artifact() -> None:
    context, result = run_no_op()
    ledger = ActionLedger()
    tampered = result.evidence.model_copy(update={"evaluated_artifact_hash": "0" * 64})
    verdict = accept_remediation_evidence(
        tampered, context=make_crossing_context(context, ledger=ledger)
    )
    assert verdict.accepted is False


# ============================ security: no manufactured evidence ======================


def test_a_frontend_cannot_manufacture_plausible_mutation_evidence() -> None:
    """Plausible fields are not authority. The hashes must match reality."""
    context, result = run_hero_mutation()
    manufactured = MutationEvidence(
        artifact_id=ARTIFACT_ID,
        before_ref=f"local://artifacts/{ARTIFACT_ID}",
        after_ref=f"local://artifacts/{ARTIFACT_ID}#v2",
        before_hash="a" * 64,
        after_hash="b" * 64,
        before_value="LEFT",
        after_value="TOP_RIGHT",
        patch_description="label_position: LEFT -> TOP_RIGHT",
        reconciled=False,
        action_id=ACTION_ID,
        data_classification=make_classification(),
    )
    verdict = accept_remediation_evidence(manufactured, context=make_crossing_context(context))

    assert verdict.accepted is False
    assert RemediationRejection.BEFORE_HASH_MISMATCH in verdict.rejections
    assert RemediationRejection.AFTER_HASH_MISMATCH in verdict.rejections


def test_an_agent_supplied_after_hash_never_becomes_authoritative() -> None:
    context, result = run_hero_mutation()
    claimed = result.evidence.model_copy(update={"after_hash": "abc"})
    verdict = accept_remediation_evidence(claimed, context=make_crossing_context(context))

    assert verdict.accepted is False
    assert verdict.authoritative_after_hash != "abc"
    assert verdict.authoritative_after_hash == artifact_content_hash(
        context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    )


def test_the_crossing_context_exposes_no_caller_supplied_path_or_hash() -> None:
    fields = set(RemediationCrossingContext.__dataclass_fields__)
    for forbidden in ("path", "file", "dir", "uri", "url", "before_hash", "after_hash"):
        assert not any(forbidden in name for name in fields)


# ============================ authority containment ===================================


def test_the_crossing_result_carries_no_workflow_or_verdict_authority() -> None:
    fields = set(RemediationBoundaryResult.__dataclass_fields__)
    for forbidden in (
        "workflow_state", "next_state", "verdict", "passed", "failed",
        "proof", "change_proof", "proof_complete", "transition", "authorized",
    ):
        assert forbidden not in fields


def test_the_crossing_does_not_reauthorize_or_mint_capabilities() -> None:
    """T075 owns authorization; Crossing 2 must not become a second gate."""
    import ast

    source = (REPO_ROOT / "src" / "driftzero" / "orchestration.py").read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "driftzero.capabilities" not in imported
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
    )
    forbidden_surfaces = (
        "AUTHORIZATION_POLICY", "MutationCapabilityBroker", ".issue(", "is_authorized",
    )
    for forbidden in forbidden_surfaces:
        assert forbidden not in code


def test_the_crossing_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation must not reach the repository write path."""
    context, result = run_hero_mutation()

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("Crossing 2 must never dispatch a write")

    monkeypatch.setattr(type(context.repository), "apply_requirement", explode)
    verdict = accept_remediation_evidence(result.evidence, context=make_crossing_context(context))
    assert verdict.accepted is True


def test_rejected_evidence_is_never_forwarded() -> None:
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence.model_copy(update={"after_hash": "0" * 64}),
        context=make_crossing_context(context),
    )
    assert verdict.accepted_evidence is None
    assert verdict.rejection_reason


def test_a_rejection_produces_a_deterministic_evidence_reference() -> None:
    """A reference only — nothing here claims persistence that does not exist."""
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(
        result.evidence.model_copy(update={"after_hash": "0" * 64}),
        context=make_crossing_context(context),
    )
    ref = verdict.evidence_ref()
    assert ref is not None
    assert ref.startswith(f"crossing2-rejected:{REJECTION_REF}:")
    assert "AFTER_HASH_MISMATCH" in ref
    assert accept_remediation_evidence(
        result.evidence, context=make_crossing_context(context)
    ).evidence_ref() is None


def test_the_discriminated_union_is_preserved_end_to_end() -> None:
    """No stage flattens the evidence into a dict."""
    context, result = run_hero_mutation()
    verdict = accept_remediation_evidence(result.evidence, context=make_crossing_context(context))
    assert not isinstance(verdict.accepted_evidence, dict)
    assert verdict.accepted_evidence.remediation_type == "MUTATION"

    no_op_context, no_op_result = run_no_op()
    assert no_op_result.evidence.remediation_type == "NO_OP"
    assert not isinstance(no_op_result.evidence, dict)


def test_an_incomplete_action_cannot_produce_accepted_mutation_evidence() -> None:
    context, result = run_hero_mutation()
    ledger = context.ledger
    ledger.mark_failed_or_uncertain(ACTION_ID, occurred_at=_Clock()())
    assert ledger.require(ACTION_ID).status is ActionStatus.FAILED_OR_UNCERTAIN

    verdict = accept_remediation_evidence(result.evidence, context=make_crossing_context(context))
    assert verdict.accepted is False
    assert RemediationRejection.ACTION_NOT_COMPLETED in verdict.rejections
