"""T069 — the semantic agent layer.

Agents sit **outside** authoritative domain logic. They read, they propose, and that is
all. Every consequential decision stays in the deterministic Truth Engine:

* qualification and impact-target selection
* target cardinality rules
* divergence checks
* autonomy conditions
* workflow state transitions
* PASS / FAIL / ``PROOF_COMPLETE``

Nothing in this package may transition a workflow, write business truth, authorize a
remediation, or generate a Change Proof. Agent output is a *proposal* that must survive
deterministic trust-boundary validation before anything acts on it.

Import discipline: this package imports stdlib, pydantic, and ``driftzero`` only. Model
and cloud SDKs are reached through a registered provider (see :mod:`model_client`) so the
whole distribution stays importable with no SDK installed and no credentials present —
enforced by the M0 purity guard, which scans every file under ``src/driftzero``.
"""

from __future__ import annotations

from driftzero.agents.model_client import (
    SemanticModelClient,
    SemanticRequest,
    clear_model_client_provider,
    get_model_client,
    register_model_client_provider,
)

__all__ = [
    "SemanticModelClient",
    "SemanticRequest",
    "clear_model_client_provider",
    "get_model_client",
    "register_model_client_provider",
]
