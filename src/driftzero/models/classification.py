"""T006 — Data classification and lineage (FR-010, SC-013).

Classification is deliberately **non-exclusive**: one evidence item may carry
several dimensions at once (for example a real model call over a synthetic
fixture is ``[REAL]`` with synthetic lineage, and a derived observation of a
real photo in a demo scenario is ``[DERIVED, REAL]``).

This module therefore models labels as a *set*, never as a single mutually
exclusive ``classification: Enum`` field.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClassificationLabel(StrEnum):
    """The four non-exclusive classification dimensions (spec § Data Classification)."""

    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"
    DERIVED = "DERIVED"
    SIMULATED = "SIMULATED"


class LineageEntry(BaseModel):
    """One link in an ordered provenance chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str = Field(min_length=1, description="Reference to source evidence/artifact")
    source_classification: list[ClassificationLabel] = Field(
        default_factory=list, description="Classification labels of the source"
    )
    relationship: str = Field(
        min_length=1, description="e.g. derived_from, observed_from, input_to"
    )


class DataClassification(BaseModel):
    """Non-exclusive labels plus an ordered lineage chain.

    At least one label is required: FR-010 mandates explicit classification, so an
    unlabelled evidence item is structurally invalid.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    labels: list[ClassificationLabel] = Field(
        min_length=1, description="Non-exclusive set of applicable dimensions"
    )
    lineage: list[LineageEntry] = Field(
        default_factory=list, description="Ordered provenance chain"
    )

    @field_validator("labels")
    @classmethod
    def _labels_unique(cls, v: list[ClassificationLabel]) -> list[ClassificationLabel]:
        """Labels form a set: duplicates are a structural error, order is preserved."""
        if len(set(v)) != len(v):
            raise ValueError("labels must not contain duplicates")
        return v

    def has(self, label: ClassificationLabel) -> bool:
        """True when this item carries the given dimension."""
        return label in self.labels
