"""T073 — Artifact Mutation Tool.

Every idempotency claim is proven by counting real repository dispatches, never by
trusting a returned status. Fully offline: in-memory repository, injected clock, no
model, no cloud, no filesystem.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from driftzero.capabilities import AgentIdentity, MutationCapabilityBroker  # noqa: E402
from driftzero.models.action import ActionStatus, ActionType  # noqa: E402
from driftzero.models.artifact import DownstreamArtifact  # noqa: E402
from driftzero.models.remediation import MutationEvidence, NoOpEvidence  # noqa: E402
from driftzero.tools.artifact_mutation import (  # noqa: E402
    InMemoryArtifactRepository,
    MutationCapability,
    MutationOutcome,
    MutationRejection,
    MutationToolContext,
    RepositoryReadError,
    UncertainWriteError,
    apply_authorized_artifact_patch,
    artifact_content_hash,
)
from driftzero.truth_engine.actions import (  # noqa: E402
    ActionLedger,
    build_remediation_intent,
)

from ._fakes import ARTIFACT_ID, CHANGE_ID, make_change, make_classification  # noqa: E402

ACTION_ID = "act-remediate-001"
WORKFLOW_ID = "wf-001"
CORRELATION_ID = "corr-001"
UNRELATED_PROSE = "Keep the LEFT support arm attached"


class _Clock:
    """Monotonic injected clock — deterministic, never sleeps."""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_hero_artifact(**overrides: object) -> DownstreamArtifact:
    """The hero artifact: one structured requirement plus unrelated fields."""
    defaults: dict[str, object] = {
        "artifact_id": ARTIFACT_ID,
        "artifact_type": "work_instruction",
        "operation_id": "OP-9",
        "requirement_id": "label_position",
        "current_value": "LEFT",
        "content_ref": f"local://artifacts/{ARTIFACT_ID}",
        "authorized_for_remediation": True,
        "requirements": {
            "label_position": "LEFT",
            "instructions": UNRELATED_PROSE,
            "packing_mode": "STANDARD",
        },
        "data_classification": make_classification(),
    }
    defaults.update(overrides)
    return DownstreamArtifact(**defaults)


def make_capability(
    broker: MutationCapabilityBroker,
    *,
    artifact_id: str = ARTIFACT_ID,
    change_id: str = CHANGE_ID,
    source_version: str = "v3",
) -> MutationCapability:
    """Mint through the real broker — a hand-built capability would not verify."""
    return broker.issue(
        holder=AgentIdentity.REMEDIATION,
        artifact_id=artifact_id,
        change_id=change_id,
        source_version=source_version,
    )


_DEFAULT = object()


def make_context(
    artifact: DownstreamArtifact | None = None,
    *,
    capability: object = _DEFAULT,
    capability_kwargs: dict[str, str] | None = None,
    repository: object | None = None,
    ledger: ActionLedger | None = None,
    source_version_applicable: bool = True,
    broker: MutationCapabilityBroker | None = None,
) -> MutationToolContext:
    art = make_hero_artifact() if artifact is None else artifact
    repo = InMemoryArtifactRepository({art.artifact_id: art}) if repository is None else repository
    issuer = broker or MutationCapabilityBroker()
    if capability is _DEFAULT:
        cap = make_capability(issuer, **(capability_kwargs or {}))
    else:
        cap = capability  # type: ignore[assignment]
    return MutationToolContext(
        ledger=ledger or ActionLedger(),
        repository=repo,  # type: ignore[arg-type]
        capability=cap,  # type: ignore[arg-type]
        capability_verifier=issuer.verify,
        workflow_id=WORKFLOW_ID,
        change=make_change(),
        source_version_applicable=source_version_applicable,
        data_classification=make_classification(),
        clock=_Clock(),
    )


def call(context: MutationToolContext, **overrides: object):  # type: ignore[no-untyped-def]
    artifact = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    before_hash = artifact_content_hash(artifact) if artifact else "unknown-hash"
    kwargs: dict[str, object] = {
        "action_id": ACTION_ID,
        "artifact_id": ARTIFACT_ID,
        "requirement_id": "label_position",
        "expected_before_value": "LEFT",
        "expected_before_hash": before_hash,
        "new_value": "TOP_RIGHT",
        "source_procedure_id": "PROC-77",
        "source_version": "v3",
        "change_id": CHANGE_ID,
        "correlation_id": CORRELATION_ID,
    }
    kwargs.update(overrides)
    return apply_authorized_artifact_patch(**kwargs, context=context)  # type: ignore[arg-type]


# ============================ 1-3: the hero mutation ==================================


def test_left_to_top_right_happy_path() -> None:
    context = make_context()
    result = call(context)

    assert result.outcome is MutationOutcome.MUTATED
    assert result.dispatched is True
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]
    assert isinstance(result.evidence, MutationEvidence)
    assert result.evidence.before_value == "LEFT"
    assert result.evidence.after_value == "TOP_RIGHT"
    assert result.evidence.reconciled is False
    assert result.action_status is ActionStatus.COMPLETED


def test_unrelated_lexical_left_in_prose_is_untouched() -> None:
    """The whole point of a structured mutation: prose is never scanned."""
    context = make_context()
    call(context)
    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    assert after.requirements["instructions"] == UNRELATED_PROSE
    assert "LEFT" in after.requirements["instructions"]


def test_every_unrelated_structured_field_is_preserved() -> None:
    context = make_context()
    before = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    call(context)
    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]

    assert after.requirements == {
        "label_position": "TOP_RIGHT",
        "instructions": UNRELATED_PROSE,
        "packing_mode": "STANDARD",
    }
    unchanged = before.model_dump(exclude={"requirements", "current_value"})
    assert after.model_dump(exclude={"requirements", "current_value"}) == unchanged


def test_the_primary_current_value_tracks_the_mutated_requirement() -> None:
    context = make_context()
    call(context)
    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    assert after.current_value == "TOP_RIGHT"


def test_before_and_after_hashes_are_genuine_and_differ() -> None:
    context = make_context()
    before = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]
    before_hash = artifact_content_hash(before)
    result = call(context)
    after = context.repository.read(ARTIFACT_ID)  # type: ignore[union-attr]

    assert result.evidence.before_hash == before_hash
    assert result.evidence.after_hash == artifact_content_hash(after)
    assert result.evidence.before_hash != result.evidence.after_hash


# ============================ 4-8: fail-closed paths ==================================


def test_expected_before_mismatch_writes_nothing() -> None:
    """Drift is never silently overwritten."""
    artifact = make_hero_artifact(
        current_value="DIAGONAL", requirements={"label_position": "DIAGONAL"}
    )
    context = make_context(artifact)
    result = call(context, expected_before_value="LEFT", requirement_id="label_position")

    assert result.rejection is MutationRejection.BEFORE_STATE_MISMATCH
    assert result.dispatched is False
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_before_hash_mismatch_writes_nothing() -> None:
    context = make_context()
    result = call(context, expected_before_hash="0" * 64)
    assert result.rejection is MutationRejection.BEFORE_HASH_MISMATCH
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_missing_artifact_writes_nothing() -> None:
    context = make_context(repository=InMemoryArtifactRepository({}))
    result = call(context)
    assert result.rejection is MutationRejection.ARTIFACT_NOT_FOUND
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_missing_requirement_writes_nothing() -> None:
    context = make_context()
    result = call(context, requirement_id="nonexistent_requirement")
    assert result.rejection is MutationRejection.REQUIREMENT_NOT_FOUND
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_ambiguous_requirement_writes_nothing() -> None:
    """An artifact contradicting itself is refused rather than guessed at."""
    artifact = make_hero_artifact(
        current_value="LEFT", requirements={"label_position": "SOMETHING-ELSE"}
    )
    context = make_context(artifact)
    result = call(context, expected_before_value="SOMETHING-ELSE")
    assert result.rejection is MutationRejection.REQUIREMENT_AMBIGUOUS
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_unauthenticated_invocation_writes_nothing() -> None:
    context = make_context(capability=None)
    result = call(context)
    assert result.rejection is MutationRejection.CAPABILITY_MISSING
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]
    assert context.repository.read_count == 1, "rejected before the tool read anything"


def test_capability_not_granting_the_artifact_writes_nothing() -> None:
    context = make_context(capability_kwargs={"artifact_id": "WI-999"})
    result = call(context)
    assert result.rejection is MutationRejection.CAPABILITY_SCOPE_VIOLATION
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_capability_for_a_different_change_writes_nothing() -> None:
    context = make_context(capability_kwargs={"change_id": "CHG-OTHER"})
    result = call(context)
    assert result.rejection is MutationRejection.CAPABILITY_CONTEXT_MISMATCH
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_unauthorized_artifact_writes_nothing() -> None:
    context = make_context(make_hero_artifact(authorized_for_remediation=False))
    result = call(context)
    assert result.rejection is MutationRejection.ARTIFACT_NOT_AUTHORIZED
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


@pytest.mark.parametrize("blank_field", ["action_id", "artifact_id", "requirement_id", "new_value"])
def test_malformed_request_writes_nothing(blank_field: str) -> None:
    context = make_context()
    result = call(context, **{blank_field: "   "})
    assert result.rejected
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_a_no_change_request_is_malformed() -> None:
    context = make_context()
    result = call(context, new_value="LEFT")
    assert result.rejection is MutationRejection.MALFORMED_REQUEST
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_repository_read_failure_writes_nothing() -> None:
    class FailingRepo(InMemoryArtifactRepository):
        def read(self, artifact_id: str) -> DownstreamArtifact | None:
            raise RepositoryReadError("store unavailable")

    context = make_context(repository=FailingRepo({ARTIFACT_ID: make_hero_artifact()}))
    result = apply_authorized_artifact_patch(
        action_id=ACTION_ID,
        artifact_id=ARTIFACT_ID,
        requirement_id="label_position",
        expected_before_value="LEFT",
        expected_before_hash="x" * 64,
        new_value="TOP_RIGHT",
        source_procedure_id="PROC-77",
        source_version="v3",
        change_id=CHANGE_ID,
        correlation_id=CORRELATION_ID,
        context=context,
    )
    assert result.rejection is MutationRejection.REPOSITORY_READ_FAILURE
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_pre_dispatch_write_failure_is_not_uncertain() -> None:
    """A refused compare-and-set never took effect, so the outcome is known."""
    artifact = make_hero_artifact()
    repo = InMemoryArtifactRepository({ARTIFACT_ID: artifact})
    context = make_context(repository=repo)
    # Drift the store after the tool computed its expectations.
    repo._artifacts[ARTIFACT_ID] = artifact.model_copy(  # noqa: SLF001
        update={"requirements": {**artifact.requirements, "label_position": "DRIFTED"}}
    )
    result = call(context, expected_before_hash=artifact_content_hash(artifact))
    assert result.rejected
    assert result.dispatched is False
    assert repo.dispatch_count == 0


# ============================ 9-13: idempotency and reconciliation ====================


def test_replay_after_completion_makes_no_second_dispatch() -> None:
    context = make_context()
    first = call(context)
    assert first.outcome is MutationOutcome.MUTATED
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]

    second = call(context, expected_before_value="LEFT")
    assert second.outcome is MutationOutcome.ALREADY_COMPLETED
    assert second.dispatched is False
    assert context.repository.dispatch_count == 1, "replay must not write again"  # type: ignore[union-attr]


def test_same_action_id_with_a_different_payload_fails_closed() -> None:
    context = make_context()
    call(context)
    conflicting = call(context, new_value="DIAGONAL", expected_before_value="LEFT")
    assert conflicting.rejection is MutationRejection.ACTION_PAYLOAD_CONFLICT
    assert context.repository.dispatch_count == 1  # type: ignore[union-attr]


def test_same_action_id_targeting_a_different_artifact_fails_closed() -> None:
    other = make_hero_artifact(artifact_id="WI-220")
    repo = InMemoryArtifactRepository({ARTIFACT_ID: make_hero_artifact(), "WI-220": other})
    # One capability per artifact, both broker-issued, sharing a ledger: the conflict
    # must come from the reused action_id, not from an authorization gap.
    issuer = MutationCapabilityBroker()
    ledger = ActionLedger()
    first = make_context(repository=repo, ledger=ledger, broker=issuer)
    call(first)

    second = make_context(
        repository=repo,
        ledger=ledger,
        broker=issuer,
        capability_kwargs={"artifact_id": "WI-220"},
    )
    conflicting = call(second, artifact_id="WI-220")
    assert conflicting.rejection is MutationRejection.ACTION_PAYLOAD_CONFLICT
    assert repo.dispatch_count == 1


def test_post_dispatch_uncertainty_never_retries() -> None:
    class UncertainRepo(InMemoryArtifactRepository):
        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            super().apply_requirement(artifact_id, requirement_id, expected_before, new_value)
            raise UncertainWriteError("response lost after dispatch")

    repo = UncertainRepo({ARTIFACT_ID: make_hero_artifact()})
    context = make_context(repository=repo)
    result = call(context)

    assert result.rejection is MutationRejection.POST_DISPATCH_UNCERTAIN
    assert result.dispatched is True
    assert result.action_status is ActionStatus.FAILED_OR_UNCERTAIN
    assert repo.dispatch_count == 1, "exactly one dispatch; no automatic retry"


def test_crash_reconciliation_yields_a_reconciled_mutation() -> None:
    """After-value observed for an attempted action → MUTATION, reconciled=True."""
    class UncertainRepo(InMemoryArtifactRepository):
        fail_next = True

        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            result = super().apply_requirement(
                artifact_id, requirement_id, expected_before, new_value
            )
            if self.fail_next:
                self.fail_next = False
                raise UncertainWriteError("crash after commit")
            return result

    repo = UncertainRepo({ARTIFACT_ID: make_hero_artifact()})
    context = make_context(repository=repo)

    first = call(context)
    assert first.rejection is MutationRejection.POST_DISPATCH_UNCERTAIN
    assert repo.dispatch_count == 1

    recovery = call(context, expected_before_value="LEFT")
    assert recovery.outcome is MutationOutcome.RECONCILED_MUTATION
    assert isinstance(recovery.evidence, MutationEvidence)
    assert recovery.evidence.reconciled is True
    assert recovery.evidence.remediation_type == "MUTATION"
    assert recovery.dispatched is False
    assert repo.dispatch_count == 1, "reconciliation must not dispatch a second write"


def test_uncertain_action_still_at_before_value_fails_closed() -> None:
    """No blind redispatch: the frozen reconciliation refuses, and so does the tool."""
    class DispatchlessUncertainRepo(InMemoryArtifactRepository):
        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            raise UncertainWriteError("timeout before any commit was observable")

    repo = DispatchlessUncertainRepo({ARTIFACT_ID: make_hero_artifact()})
    context = make_context(repository=repo)

    first = call(context)
    assert first.rejection is MutationRejection.POST_DISPATCH_UNCERTAIN

    recovery = call(context)
    assert recovery.rejection is MutationRejection.RECONCILIATION_FAILED
    assert recovery.dispatched is False
    assert repo.dispatch_count == 0
    assert "TARGET_NOT_IN_INTENDED_AFTER_STATE" in recovery.blockers


def test_uncertain_action_with_an_unexpected_third_value_fails_closed() -> None:
    class UncertainRepo(InMemoryArtifactRepository):
        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            raise UncertainWriteError("unknown outcome")

    artifact = make_hero_artifact()
    repo = UncertainRepo({ARTIFACT_ID: artifact})
    context = make_context(repository=repo)
    call(context)

    repo._artifacts[ARTIFACT_ID] = artifact.model_copy(  # noqa: SLF001
        update={
            "current_value": "SOMETHING-ELSE",
            "requirements": {**artifact.requirements, "label_position": "SOMETHING-ELSE"},
        }
    )
    recovery = call(context)
    assert recovery.rejection is MutationRejection.RECONCILIATION_FAILED
    assert recovery.action_status is ActionStatus.FAILED_OR_UNCERTAIN
    assert repo.dispatch_count == 0


# ============================ 14: NO_OP is not reconciliation =========================


def test_legitimate_no_op_when_already_compliant() -> None:
    """Already in the after-state with nothing ever dispatched → genuine NO_OP."""
    artifact = make_hero_artifact(
        current_value="TOP_RIGHT",
        requirements={
            "label_position": "TOP_RIGHT",
            "instructions": UNRELATED_PROSE,
            "packing_mode": "STANDARD",
        },
    )
    context = make_context(artifact)
    result = call(context)

    assert result.outcome is MutationOutcome.NO_OP
    assert isinstance(result.evidence, NoOpEvidence)
    assert result.evidence.remediation_type == "NO_OP"
    assert result.evidence.observed_value == "TOP_RIGHT"
    assert result.dispatched is False
    assert context.repository.dispatch_count == 0  # type: ignore[union-attr]


def test_no_op_evidence_carries_no_before_after_pair() -> None:
    """Structural: NoOpEvidence cannot fabricate a change that did not occur."""
    for forbidden in ("before_ref", "after_ref", "before_value", "after_value", "reconciled"):
        assert forbidden not in NoOpEvidence.model_fields


def test_a_dispatched_action_can_never_be_relabelled_no_op() -> None:
    """The critical distinction: same physical value, different histories."""
    class UncertainRepo(InMemoryArtifactRepository):
        fail_next = True

        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            result = super().apply_requirement(
                artifact_id, requirement_id, expected_before, new_value
            )
            if self.fail_next:
                self.fail_next = False
                raise UncertainWriteError("crash after commit")
            return result

    repo = UncertainRepo({ARTIFACT_ID: make_hero_artifact()})
    context = make_context(repository=repo)
    call(context)
    recovery = call(context)

    assert recovery.outcome is not MutationOutcome.NO_OP
    assert not isinstance(recovery.evidence, NoOpEvidence)
    assert recovery.evidence.reconciled is True


def test_no_op_and_reconciled_mutation_reach_the_same_value_differently() -> None:
    """Both artifacts end at TOP_RIGHT; only the history distinguishes the evidence."""
    compliant = make_context(
        make_hero_artifact(
            current_value="TOP_RIGHT",
            requirements={"label_position": "TOP_RIGHT", "instructions": UNRELATED_PROSE},
        )
    )
    no_op = call(compliant)
    assert isinstance(no_op.evidence, NoOpEvidence)

    class UncertainRepo(InMemoryArtifactRepository):
        fail_next = True

        def apply_requirement(self, artifact_id, requirement_id, expected_before, new_value):  # type: ignore[no-untyped-def]
            result = super().apply_requirement(
                artifact_id, requirement_id, expected_before, new_value
            )
            if self.fail_next:
                self.fail_next = False
                raise UncertainWriteError("crash after commit")
            return result

    crashed = make_context(repository=UncertainRepo({ARTIFACT_ID: make_hero_artifact()}))
    call(crashed)
    reconciled = call(crashed)
    assert isinstance(reconciled.evidence, MutationEvidence)
    assert reconciled.evidence.reconciled is True


# ============================ 15-20: authority and scope ==============================


def test_the_tool_owns_no_workflow_transition_or_proof() -> None:
    import ast

    source = (REPO_ROOT / "src" / "driftzero" / "tools" / "artifact_mutation.py").read_text(
        encoding="utf-8"
    )
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for forbidden in (
        "driftzero.truth_engine.state_machine",
        "driftzero.truth_engine.proof_generator",
        "driftzero.truth_engine.autonomy_gate",
        "driftzero.truth_engine.impact",
        "driftzero.truth_engine.verification",
    ):
        assert forbidden not in imported


def test_the_result_carries_no_verdict_or_state_field() -> None:
    from driftzero.tools.artifact_mutation import MutationResult

    fields = set(MutationResult.__dataclass_fields__)
    for forbidden in ("workflow_state", "verdict", "passed", "proof", "authorized", "next_state"):
        assert forbidden not in fields


def test_the_tool_imports_no_model_adk_or_cloud_sdk() -> None:
    import ast

    source = (REPO_ROOT / "src" / "driftzero" / "tools" / "artifact_mutation.py").read_text(
        encoding="utf-8"
    )
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    allowed = {
        "__future__", "collections", "dataclasses", "datetime", "enum", "typing", "driftzero",
    }
    assert roots <= allowed


def test_the_tool_exposes_no_caller_provided_filesystem_path() -> None:
    """Addressing is by artifact_id only — no path, URI, or glob parameter exists."""
    import inspect

    signature = inspect.signature(apply_authorized_artifact_patch)
    for name in signature.parameters:
        assert not any(token in name.lower() for token in ("path", "file", "dir", "uri", "url"))


def test_the_tool_exposes_no_generic_patch_engine() -> None:
    """No JSON Patch, JSONPath, regex, or free-text replacement surface."""
    source = (REPO_ROOT / "src" / "driftzero" / "tools" / "artifact_mutation.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
    )
    forbidden_surfaces = (
        "jsonpatch", "jsonpath", "re.sub", "re.compile", ".replace(", "eval(", "exec(",
    )
    for forbidden in forbidden_surfaces:
        assert forbidden not in code, f"generic patching surface found: {forbidden}"


def test_the_ledger_records_the_expected_action_type_and_lifecycle() -> None:
    ledger = ActionLedger()
    context = make_context(ledger=ledger)
    call(context)

    record = ledger.require(ACTION_ID)
    assert record.action_type is ActionType.REMEDIATE_ARTIFACT
    assert record.status is ActionStatus.COMPLETED
    assert record.attempt_count == 1
    assert record.reconciled is False
    assert record.receipt_ref is not None


def test_pre_dispatch_intent_is_persisted_before_the_write() -> None:
    """Reconciliation is only possible because intent exists before dispatch."""
    ledger = ActionLedger()
    artifact = make_hero_artifact()
    ledger.plan(
        action_id=ACTION_ID,
        workflow_id=WORKFLOW_ID,
        action_type=ActionType.REMEDIATE_ARTIFACT,
        target_ref=ARTIFACT_ID,
        intent=build_remediation_intent(
            change=make_change(),
            artifact=artifact,
            expected_before_hash=artifact_content_hash(artifact),
        ),
        occurred_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    context = make_context(artifact, ledger=ledger)
    result = call(context)
    assert result.outcome is MutationOutcome.MUTATED
    assert ledger.require(ACTION_ID).intent["expected_after_value"] == "TOP_RIGHT"


def test_uses_the_frozen_ledger_rather_than_a_competing_one() -> None:
    """Structural: the tool records through ActionLedger, not a private store."""
    ledger = ActionLedger()
    context = make_context(ledger=ledger)
    call(context)
    assert len(ledger.all_records()) == 1
    assert ledger.get(ACTION_ID) is not None
