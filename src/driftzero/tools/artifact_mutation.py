"""T073 — the Artifact Mutation Tool: DRIFTZERO's first controlled write.

Mutates **one** authorized requirement in **one** structured downstream artifact. It is
a deterministic side-effect tool, not an agent: it calls no model, and every decision it
makes is a mechanical check against values it was given.

What it never does
------------------
It does not decide whether remediation is authorized, select an impact target, infer an
``artifact_id`` or ``requirement_id``, evaluate the autonomy conditions, transition
workflow state, determine PASS/FAIL, or generate a Change Proof. Those belong to the
deterministic Truth Engine and to callers upstream. This tool receives an
already-qualified request plus a capability and either performs exactly that change or
refuses.

Structured, not textual
-----------------------
The mutation targets a key in the artifact's structured ``requirements`` mapping. There
is no text search, no regex, no fuzzy match, and no patch language. An unrelated
occurrence of the old value in prose — "Keep the LEFT support arm attached" — is not
reachable by this code path, because prose is never scanned.

Reuse, not reinvention
----------------------
The ledger (:class:`ActionLedger`), retry deduplication (:func:`decide_retry`), crash
reconciliation (:func:`reconcile_mutation`), the evidence union
(``MutationEvidence | NoOpEvidence``) and the hashing helpers are all frozen M0
primitives, used as-is. This module adds no competing ledger, no second evidence type,
and no alternative reconciliation rule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from driftzero.models.action import ActionStatus, ActionType
from driftzero.models.artifact import DownstreamArtifact
from driftzero.models.change import ApprovedChange
from driftzero.models.classification import DataClassification
from driftzero.models.remediation import MutationEvidence, NoOpEvidence
from driftzero.truth_engine.actions import (
    ActionLedger,
    ReconciliationOutcome,
    RetryDecision,
    build_remediation_intent,
    decide_retry,
    no_op_admissible,
    reconcile_mutation,
)
from driftzero.truth_engine.evidence import canonical_hash

TOOL_IDENTITY = "driftzero.tools.artifact_mutation"
"""Stable tool identity recorded on receipts and checked at Crossing 2."""

TOOL_CAPABILITY = "ARTIFACT_MUTATION"
"""The capability a caller must hold to invoke this tool.

Mirrors ``driftzero.capabilities.ToolCapability.ARTIFACT_MUTATION`` as a plain string so
this module keeps no import of the authorization layer — the dependency runs
capabilities -> tools, and inverting it here would create a cycle. A structural test
asserts the two stay identical.
"""


class MutationOutcome(StrEnum):
    """What the tool did. Exactly one applies per invocation."""

    MUTATED = "MUTATED"
    NO_OP = "NO_OP"
    ALREADY_COMPLETED = "ALREADY_COMPLETED"
    RECONCILED_MUTATION = "RECONCILED_MUTATION"
    REJECTED = "REJECTED"


class MutationRejection(StrEnum):
    """Deterministic reason codes. Every one implies zero further dispatch."""

    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    CAPABILITY_NOT_ISSUED = "CAPABILITY_NOT_ISSUED"
    CAPABILITY_WRONG_TOOL = "CAPABILITY_WRONG_TOOL"
    CAPABILITY_SCOPE_VIOLATION = "CAPABILITY_SCOPE_VIOLATION"
    CAPABILITY_CONTEXT_MISMATCH = "CAPABILITY_CONTEXT_MISMATCH"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_NOT_AUTHORIZED = "ARTIFACT_NOT_AUTHORIZED"
    REQUIREMENT_NOT_FOUND = "REQUIREMENT_NOT_FOUND"
    REQUIREMENT_AMBIGUOUS = "REQUIREMENT_AMBIGUOUS"
    BEFORE_STATE_MISMATCH = "BEFORE_STATE_MISMATCH"
    BEFORE_HASH_MISMATCH = "BEFORE_HASH_MISMATCH"
    REPOSITORY_READ_FAILURE = "REPOSITORY_READ_FAILURE"
    DISPATCH_FAILURE = "DISPATCH_FAILURE"
    POST_DISPATCH_UNCERTAIN = "POST_DISPATCH_UNCERTAIN"
    ACTION_PAYLOAD_CONFLICT = "ACTION_PAYLOAD_CONFLICT"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


class RepositoryReadError(Exception):
    """The artifact store could not be read. Never confused with 'not found'."""


class RepositoryWriteError(Exception):
    """The write failed *before* taking effect. Safe to treat as no side effect."""


class UncertainWriteError(Exception):
    """The write was dispatched and its outcome is unknown.

    The defining case of the side-effect safety rule: the change may or may not have
    landed, so the action becomes ``FAILED_OR_UNCERTAIN`` and is never blindly retried.
    """


@dataclass(frozen=True)
class MutationCapability:
    """An unforgeable-by-construction grant to mutate specific artifacts.

    ``holder`` is an opaque logical identity. This tool deliberately does **not** know
    which identity is allowed to hold a capability — minting policy belongs to the
    broker. What the tool enforces is narrower and entirely mechanical: a capability
    must be present, must **verify as broker-issued**, must name this artifact, and must
    match the change it was issued for.

    Verification is what stops a caller from forging authority by constructing this
    dataclass directly with a privileged-looking ``holder`` string.
    """

    capability_id: str
    holder: str
    tool: str
    """The capability this grant is for. Bound into the broker's HMAC payload."""
    authorized_artifact_ids: frozenset[str]
    change_id: str
    source_version: str
    grant_token: str
    """Broker-issued integrity material. A hand-built value cannot verify."""


@runtime_checkable
class ArtifactRepository(Protocol):
    """The narrowest store this tool needs.

    Addressing is by ``artifact_id`` only. There is no path, URI, or query parameter a
    caller could steer, so the tool exposes no filesystem surface at all.
    """

    def read(self, artifact_id: str) -> DownstreamArtifact | None:
        """Return the artifact, or ``None`` when it does not exist."""
        ...

    def apply_requirement(
        self, artifact_id: str, requirement_id: str, expected_before: str, new_value: str
    ) -> DownstreamArtifact:
        """Atomically set one requirement, conditional on its current value.

        Implementations MUST apply the change wholly or not at all, and MUST leave every
        other field untouched.
        """
        ...


class InMemoryArtifactRepository:
    """Deterministic in-process store for M1. No cloud, no filesystem, no framework.

    ``dispatch_count`` exists so idempotency can be proven by counting real writes
    rather than by trusting a returned status.
    """

    def __init__(self, artifacts: dict[str, DownstreamArtifact] | None = None) -> None:
        self._artifacts: dict[str, DownstreamArtifact] = dict(artifacts or {})
        self._revision: dict[str, int] = {}
        self.dispatch_count = 0
        self.read_count = 0

    def read(self, artifact_id: str) -> DownstreamArtifact | None:
        self.read_count += 1
        return self._artifacts.get(artifact_id)

    def apply_requirement(
        self, artifact_id: str, requirement_id: str, expected_before: str, new_value: str
    ) -> DownstreamArtifact:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None:
            raise RepositoryWriteError(f"artifact {artifact_id} disappeared before write")

        current = artifact.requirements.get(requirement_id)
        if current != expected_before:
            # Compare-and-set: drift discovered at write time is never overwritten.
            raise RepositoryWriteError(
                f"before-state changed under the write: expected {expected_before!r}, "
                f"found {current!r}"
            )

        requirements = dict(artifact.requirements)
        requirements[requirement_id] = new_value
        # A mutation must leave the before-state independently retrievable: Crossing 2
        # requires before_ref != after_ref so both states can be fetched and hashed. A
        # store that overwrites in place under one ref cannot satisfy that, so the
        # committed artifact gets its own versioned reference.
        self._revision[artifact_id] = self._revision.get(artifact_id, 1) + 1
        base = artifact.content_ref.split("#v")[0]
        update: dict[str, object] = {
            "requirements": requirements,
            "content_ref": f"{base}#v{self._revision[artifact_id]}",
        }
        if requirement_id == artifact.requirement_id:
            # Keep the model coherent: current_value tracks the artifact's own
            # requirement, and reconciliation compares against exactly that field.
            update["current_value"] = new_value

        mutated = artifact.model_copy(update=update)
        self._artifacts[artifact_id] = mutated
        self.dispatch_count += 1
        return mutated


@dataclass(frozen=True)
class MutationToolContext:
    """Everything the tool needs that is not part of the request itself.

    ``change`` and ``source_version_applicable`` exist solely so reconciliation can be
    delegated to the frozen T034 implementation; the tool never re-derives either.

    ``capability_verifier`` has no default on purpose: an optional authorization check
    is one forgotten argument away from no authorization check.
    """

    ledger: ActionLedger
    repository: ArtifactRepository
    capability: MutationCapability | None
    capability_verifier: Callable[[MutationCapability], bool]
    """Broker verification hook. Required — no call site may skip authorization."""
    workflow_id: str
    change: ApprovedChange
    source_version_applicable: bool
    data_classification: DataClassification
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class MutationResult:
    """The tool's verdict, the evidence it supports, and what it actually did."""

    outcome: MutationOutcome
    evidence: MutationEvidence | NoOpEvidence | None = None
    rejection: MutationRejection | None = None
    action_status: ActionStatus | None = None
    dispatched: bool = False
    """True only when a write was actually sent to the repository."""
    detail: str | None = None
    blockers: tuple[str, ...] = field(default_factory=tuple)
    receipt_ref: str | None = None

    @property
    def rejected(self) -> bool:
        return self.outcome is MutationOutcome.REJECTED

    @property
    def requires_review(self) -> bool:
        """Fail-closed outcomes route the workflow to REVIEW_REQUIRED upstream."""
        return self.rejected


def artifact_content_hash(artifact: DownstreamArtifact) -> str:
    """Canonical hash of the artifact's structured requirement set.

    Uses the frozen M0 canonical hashing helper so a hash computed here is the same
    value the Truth Engine computes for the same content.
    """
    return canonical_hash(artifact.requirements)


def _reject(
    reason: MutationRejection,
    *,
    detail: str | None = None,
    status: ActionStatus | None = None,
    dispatched: bool = False,
    blockers: tuple[str, ...] = (),
) -> MutationResult:
    return MutationResult(
        outcome=MutationOutcome.REJECTED,
        rejection=reason,
        detail=detail,
        action_status=status,
        dispatched=dispatched,
        blockers=blockers,
    )


def apply_authorized_artifact_patch(
    action_id: str,
    artifact_id: str,
    requirement_id: str,
    expected_before_value: str,
    expected_before_hash: str,
    new_value: str,
    source_procedure_id: str,
    source_version: str,
    change_id: str,
    correlation_id: str,
    *,
    context: MutationToolContext,
) -> MutationResult:
    """Apply exactly one authorized requirement change, or fail closed with zero effect.

    The branch order is the safety property. Authentication and request validity are
    checked before anything is read; the ledger is consulted before anything is written;
    a dispatched-but-unconfirmed action is reconciled rather than repeated. No path
    reaches the repository write more than once per ``action_id``.
    """
    # ---- 1. capability: unauthenticated invocation never reaches a read ------------
    capability = context.capability
    if capability is None:
        return _reject(MutationRejection.CAPABILITY_MISSING, detail="no capability presented")
    if capability.tool != TOOL_CAPABILITY:
        # Checked before verification so a capability minted for another tool is refused
        # on its own terms, not merely because its signature happens not to match here.
        return _reject(
            MutationRejection.CAPABILITY_WRONG_TOOL,
            detail=(
                f"capability was minted for {capability.tool!r}, not {TOOL_CAPABILITY!r}; "
                "a capability never crosses a tool boundary"
            ),
        )
    if not context.capability_verifier(capability):
        return _reject(
            MutationRejection.CAPABILITY_NOT_ISSUED,
            detail=(
                "capability did not verify as broker-issued; a constructed or revoked "
                "capability confers no authority"
            ),
        )
    if artifact_id not in capability.authorized_artifact_ids:
        return _reject(
            MutationRejection.CAPABILITY_SCOPE_VIOLATION,
            detail=f"capability does not grant {artifact_id}",
        )
    if capability.change_id != change_id or capability.source_version != source_version:
        return _reject(
            MutationRejection.CAPABILITY_CONTEXT_MISMATCH,
            detail="capability was issued for a different change or source version",
        )

    # ---- 2. request structure ------------------------------------------------------
    required = {
        "action_id": action_id,
        "artifact_id": artifact_id,
        "requirement_id": requirement_id,
        "expected_before_value": expected_before_value,
        "expected_before_hash": expected_before_hash,
        "new_value": new_value,
        "source_procedure_id": source_procedure_id,
        "source_version": source_version,
        "change_id": change_id,
        "correlation_id": correlation_id,
    }
    blank = sorted(name for name, value in required.items() if not str(value).strip())
    if blank:
        return _reject(
            MutationRejection.MALFORMED_REQUEST, detail=f"blank required field(s): {blank}"
        )
    if expected_before_value == new_value:
        return _reject(
            MutationRejection.MALFORMED_REQUEST,
            detail="expected_before_value equals new_value; no change was requested",
        )
    if change_id != context.change.change_id or source_version != context.change.source_version:
        return _reject(
            MutationRejection.CAPABILITY_CONTEXT_MISMATCH,
            detail="request does not match the approved change in context",
        )

    ledger = context.ledger
    record = ledger.get(action_id)

    # ---- 3. payload conflict on a reused identity ----------------------------------
    if record is not None:
        conflict = _intent_conflict(record.intent, artifact_id, expected_before_value, new_value)
        if conflict:
            return _reject(
                MutationRejection.ACTION_PAYLOAD_CONFLICT,
                detail=conflict,
                status=record.status,
            )

    decision = decide_retry(ledger, action_id)

    # ---- 4. already completed: reconstruct, never re-dispatch -----------------------
    if decision is RetryDecision.ALREADY_COMPLETED:
        return _replay_completed(action_id, context)

    # ---- 5. dispatched but unconfirmed: reconcile, never re-dispatch ----------------
    if decision is RetryDecision.RECONCILIATION_REQUIRED:
        return _reconcile(action_id, artifact_id, context)

    # ---- 6. read the artifact ------------------------------------------------------
    try:
        artifact = context.repository.read(artifact_id)
    except RepositoryReadError as exc:
        return _reject(MutationRejection.REPOSITORY_READ_FAILURE, detail=str(exc))
    if artifact is None:
        return _reject(MutationRejection.ARTIFACT_NOT_FOUND, detail=artifact_id)
    if not artifact.authorized_for_remediation:
        return _reject(
            MutationRejection.ARTIFACT_NOT_AUTHORIZED,
            detail=f"{artifact_id} is not authorized for remediation",
        )

    # ---- 7. resolve exactly one structured requirement -----------------------------
    if requirement_id not in artifact.requirements:
        return _reject(
            MutationRejection.REQUIREMENT_NOT_FOUND,
            detail=f"{requirement_id} is not a structured requirement of {artifact_id}",
        )
    observed = artifact.requirements[requirement_id]
    if requirement_id == artifact.requirement_id and artifact.current_value != observed:
        # The artifact contradicts itself about its own primary requirement. Resolving
        # that guess-free is impossible, so refuse rather than pick a side.
        return _reject(
            MutationRejection.REQUIREMENT_AMBIGUOUS,
            detail=(
                f"{artifact_id} reports current_value {artifact.current_value!r} but "
                f"requirements[{requirement_id}] is {observed!r}"
            ),
        )

    before_hash = artifact_content_hash(artifact)

    # ---- 8. already in the after-state: NO_OP only if nothing was ever dispatched ---
    if observed == new_value:
        if no_op_admissible(ledger, action_id):
            return _no_op(artifact, requirement_id, observed, new_value, before_hash, context)
        return _reject(
            MutationRejection.ACTION_PAYLOAD_CONFLICT,
            detail=(
                "artifact already holds the after-value but this action was dispatched; "
                "reconciliation decides the outcome, not a NO_OP"
            ),
            status=record.status if record else None,
        )

    # ---- 9. before-state consistency ------------------------------------------------
    if observed != expected_before_value:
        return _reject(
            MutationRejection.BEFORE_STATE_MISMATCH,
            detail=f"expected {expected_before_value!r}, found {observed!r}; drift not overwritten",
        )
    if before_hash != expected_before_hash:
        return _reject(
            MutationRejection.BEFORE_HASH_MISMATCH,
            detail=f"expected before-hash {expected_before_hash}, computed {before_hash}",
        )

    # ---- 10. persist intent, then dispatch exactly once -----------------------------
    now = context.clock()
    if record is None:
        ledger.plan(
            action_id=action_id,
            workflow_id=context.workflow_id,
            action_type=ActionType.REMEDIATE_ARTIFACT,
            target_ref=artifact_id,
            intent=build_remediation_intent(
                change=context.change, artifact=artifact, expected_before_hash=before_hash
            ),
            occurred_at=now,
        )

    ledger.mark_attempted(action_id, occurred_at=now)
    try:
        mutated = context.repository.apply_requirement(
            artifact_id, requirement_id, expected_before_value, new_value
        )
    except UncertainWriteError as exc:
        # Dispatched, outcome unknown. Never retried here; reconciliation owns recovery.
        ledger.mark_failed_or_uncertain(action_id, occurred_at=context.clock())
        return _reject(
            MutationRejection.POST_DISPATCH_UNCERTAIN,
            detail=str(exc),
            status=ActionStatus.FAILED_OR_UNCERTAIN,
            dispatched=True,
        )
    except RepositoryWriteError as exc:
        ledger.mark_failed_or_uncertain(action_id, occurred_at=context.clock())
        return _reject(
            MutationRejection.DISPATCH_FAILURE,
            detail=str(exc),
            status=ActionStatus.FAILED_OR_UNCERTAIN,
            dispatched=False,
        )

    after_hash = artifact_content_hash(mutated)
    receipt = f"{TOOL_IDENTITY}:{action_id}:{correlation_id}"
    evidence = MutationEvidence(
        artifact_id=artifact_id,
        before_ref=artifact.content_ref,
        after_ref=mutated.content_ref,
        before_hash=before_hash,
        after_hash=after_hash,
        before_value=expected_before_value,
        after_value=new_value,
        patch_description=(
            f"{requirement_id}: {expected_before_value} -> {new_value} on {artifact_id}"
        ),
        reconciled=False,
        action_id=action_id,
        data_classification=context.data_classification,
    )
    completed = ledger.mark_completed(
        action_id,
        occurred_at=context.clock(),
        receipt_ref=receipt,
        outcome_evidence_ref=mutated.content_ref,
        reconciled=False,
    )
    return MutationResult(
        outcome=MutationOutcome.MUTATED,
        evidence=evidence,
        action_status=completed.status,
        dispatched=True,
        receipt_ref=receipt,
    )


# ============================ helpers =================================================


def _intent_conflict(
    intent: dict[str, object], artifact_id: str, expected_before: str, new_value: str
) -> str | None:
    """Detect a reused ``action_id`` carrying a materially different payload."""
    if not intent:
        return None
    mismatches = [
        f"{key}: recorded {intent[key]!r}, requested {requested!r}"
        for key, requested in (
            ("artifact_id", artifact_id),
            ("expected_before_value", expected_before),
            ("expected_after_value", new_value),
        )
        if key in intent and intent[key] != requested
    ]
    if not mismatches:
        return None
    return "action_id reused with a different payload — " + "; ".join(mismatches)


def _replay_completed(action_id: str, context: MutationToolContext) -> MutationResult:
    """Return the existing completion without touching the repository."""
    record = context.ledger.require(action_id)
    return MutationResult(
        outcome=MutationOutcome.ALREADY_COMPLETED,
        evidence=None,
        action_status=record.status,
        dispatched=False,
        detail="action already completed; no second dispatch",
        receipt_ref=record.receipt_ref,
    )


def _reconcile(
    action_id: str, artifact_id: str, context: MutationToolContext
) -> MutationResult:
    """Delegate to the frozen T034 reconciliation. No dispatch on any branch."""
    try:
        artifact = context.repository.read(artifact_id)
    except RepositoryReadError as exc:
        return _reject(
            MutationRejection.REPOSITORY_READ_FAILURE,
            detail=str(exc),
            status=ActionStatus.FAILED_OR_UNCERTAIN,
        )
    if artifact is None:
        return _reject(
            MutationRejection.ARTIFACT_NOT_FOUND,
            detail=artifact_id,
            status=ActionStatus.FAILED_OR_UNCERTAIN,
        )

    result = reconcile_mutation(
        context.ledger,
        action_id,
        observed_artifact=artifact,
        observed_after_hash=artifact_content_hash(artifact),
        after_ref=artifact.content_ref,
        change=context.change,
        source_version_applicable=context.source_version_applicable,
        occurred_at=context.clock(),
        data_classification=context.data_classification,
    )

    if result.outcome is ReconciliationOutcome.RECONCILED_MUTATION:
        return MutationResult(
            outcome=MutationOutcome.RECONCILED_MUTATION,
            evidence=result.evidence,
            action_status=ActionStatus.COMPLETED,
            dispatched=False,
            detail="completed by reconciliation; no second dispatch",
        )
    if result.outcome is ReconciliationOutcome.ALREADY_COMPLETED:
        return _replay_completed(action_id, context)

    context.ledger.mark_failed_or_uncertain(action_id, occurred_at=context.clock())
    return _reject(
        MutationRejection.RECONCILIATION_FAILED,
        detail="reconciliation could not safely establish the outcome",
        status=ActionStatus.FAILED_OR_UNCERTAIN,
        blockers=tuple(str(blocker) for blocker in result.blockers),
    )


def _no_op(
    artifact: DownstreamArtifact,
    requirement_id: str,
    observed: str,
    expected: str,
    evaluated_hash: str,
    context: MutationToolContext,
) -> MutationResult:
    """Record a genuinely already-compliant artifact.

    Reached only when this action never left ``PLANNED``, so no mutation of this
    artifact can be attributed to this workflow. A single evaluated state is recorded —
    ``NoOpEvidence`` has no before/after pair to fabricate.
    """
    evidence = NoOpEvidence(
        artifact_id=artifact.artifact_id,
        evaluated_artifact_ref=artifact.content_ref,
        evaluated_artifact_hash=evaluated_hash,
        observed_value=observed,
        expected_value=expected,
        no_op_reason="artifact already represented the approved value before any mutation",
        compliance_basis=f"requirements[{requirement_id}]",
        data_classification=context.data_classification,
    )
    return MutationResult(
        outcome=MutationOutcome.NO_OP,
        evidence=evidence,
        action_status=None,
        dispatched=False,
        detail="already compliant; no write dispatched",
    )
