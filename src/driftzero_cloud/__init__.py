"""DRIFTZERO cloud adapters — Firestore (T092) and Cloud Storage (T093).

This package sits deliberately OUTSIDE ``src/driftzero/``, which the M0 purity guard
protects recursively and where a Google SDK import is forbidden. The repository already
uses this shape for ``driftzero_adk`` (google.adk, google.genai) and
``driftzero_providers`` (google.auth); persistence is the third instance of it.

The dependency direction is one-way and load-bearing::

    Truth Engine / domain          knows nothing about this package
            ^
    application composition        chooses an implementation
            ^
    cloud adapter (here)           imports the domain, never the reverse

Firestore and Cloud Storage are *mechanisms*. They persist records the Truth Engine has
already decided. They do not own workflow transitions, authorization, impact
qualification, the verification verdict, proof conditions, proof identity, or proof hash
semantics — and nothing in this package may be given the chance to.

Importing this module does not open a connection or read credentials. Clients are
constructed explicitly, so an offline test that never builds an adapter never touches
Google Cloud.
"""

from __future__ import annotations

from driftzero_cloud.errors import (
    CloudAdapterError,
    ConflictingRecord,
    IdentifierRejected,
)
from driftzero_cloud.ports import (
    ActionLedgerPort,
    EvidenceObjectStore,
    IdempotencyKeyStore,
    ProofStorePort,
    WorkflowStore,
)
from driftzero_cloud.serialization import (
    DOCUMENT_SCHEMA_VERSION,
    WorkflowRecord,
    decode_action,
    decode_proof,
    decode_workflow,
    decode_workflow_record,
    encode_action,
    encode_proof,
    encode_workflow,
    safe_identifier,
)

__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "ActionLedgerPort",
    "CloudAdapterError",
    "ConflictingRecord",
    "EvidenceObjectStore",
    "IdempotencyKeyStore",
    "IdentifierRejected",
    "ProofStorePort",
    "WorkflowRecord",
    "WorkflowStore",
    "decode_action",
    "decode_proof",
    "decode_workflow",
    "decode_workflow_record",
    "encode_action",
    "encode_proof",
    "encode_workflow",
    "safe_identifier",
]
