"""T011/T012 — Discriminated remediation evidence (FR-003, US3 scenario 2).

``RemediationEvidence = MutationEvidence | NoOpEvidence`` discriminated on
``remediation_type``. The two variants are structurally different **on purpose**:
an already-compliant artifact must never be represented with a fabricated
mutation, a synthetic after-state, or a before/after pair implying a change that
did not occur.

``extra="forbid"`` is what makes the prohibition structural: a ``NoOpEvidence``
payload carrying ``before_ref``/``after_ref`` is rejected at parse time rather
than by a later convention.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from driftzero.models.classification import DataClassification


class MutationEvidence(BaseModel):
    """T011 — evidence that a real write occurred.

    Answers: what was changed, from what, to what, and are both states retrievable
    and hash-verifiable?

    ``reconciled=True`` marks completion established by post-crash reconciliation
    (T034) rather than an observed tool response. A reconciled mutation remains a
    MUTATION and is never converted to a NO_OP. The reconciliation *algorithm* is
    T034; this model only carries the flag it will set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    remediation_type: Literal["MUTATION"] = "MUTATION"
    artifact_id: str = Field(min_length=1)
    before_ref: str = Field(min_length=1, description="Content reference before mutation")
    after_ref: str = Field(min_length=1, description="Content reference after mutation")
    before_hash: str = Field(min_length=1, description="SHA-256 of the before content")
    after_hash: str = Field(min_length=1, description="SHA-256 of the after content")
    before_value: str = Field(min_length=1)
    after_value: str = Field(min_length=1)
    patch_description: str = Field(min_length=1, description="Auditable atomic change applied")
    reconciled: bool = Field(
        default=False,
        description="True when established by post-crash reconciliation (T034), still MUTATION",
    )
    action_id: str = Field(
        min_length=1, description="Stable identity of the REMEDIATE_ARTIFACT action"
    )
    data_classification: DataClassification


class NoOpEvidence(BaseModel):
    """T012 — evidence that the artifact was already compliant.

    Answers: which artifact was evaluated, in what exact state, against which
    approved value, and on what basis was it already compliant — **without**
    asserting that anything was modified.

    Carries a single evaluated state. There is no before/after pair, and
    ``extra="forbid"`` rejects any attempt to add one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    remediation_type: Literal["NO_OP"] = "NO_OP"
    artifact_id: str = Field(min_length=1)
    evaluated_artifact_ref: str = Field(
        min_length=1, description="Single evaluated state — no before/after pair"
    )
    evaluated_artifact_hash: str = Field(min_length=1, description="SHA-256 of evaluated content")
    observed_value: str = Field(min_length=1, description="Value the artifact already represented")
    expected_value: str = Field(min_length=1, description="Approved value compared against")
    no_op_reason: str = Field(min_length=1)
    compliance_basis: str = Field(
        min_length=1, description="Locator of the instruction establishing compliance"
    )
    data_classification: DataClassification


RemediationEvidence: TypeAlias = Annotated[
    MutationEvidence | NoOpEvidence, Field(discriminator="remediation_type")
]
"""Discriminated union satisfying completion condition 3 by exactly one path."""
