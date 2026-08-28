"""The durable-persistence seam for the composition root.

``HeroConsoleService`` writes through this interface and never learns which backend is
behind it. That is deliberate: the module is pure — it imports no Google SDK — so an
offline M1 run needs no cloud dependency installed, and the LOCAL_PILOT path keeps
exactly the behaviour it had before T092 existed.

The cloud implementation lives in ``driftzero_cloud.composition``. The arrow points from
that package to this one, never the reverse.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from driftzero.models.action import ActionExecution
from driftzero.models.proof import ChangeProof
from driftzero.models.workflow import Workflow


@runtime_checkable
class DurableSink(Protocol):
    """Where authoritative records go when the runtime is configured for durability."""

    @property
    def durable(self) -> bool:
        """True when writes actually survive this process."""

    def record_workflow(self, workflow: Workflow, state_history: Sequence[str]) -> None:
        """Persist the aggregate and the chronology beside it."""

    def record_action(self, action: ActionExecution) -> None:
        """Persist one ledger record under its stable ``action_id``."""

    def record_proof(self, proof: ChangeProof) -> None:
        """Persist a Change Proof write-once, unchanged."""

    def record_session(self, workflow_id: str, session: object) -> None:
        """Persist the session-level state a resumed workflow needs to finish.

        The aggregate alone is not enough: the proof context is assembled from the
        impact resolution, remediation evidence and delivery ref that live beside it.
        """


class NullSink:
    """The default: record nothing, durably guarantee nothing, and say so.

    Used whenever persistence is not configured. It is not a "local database" and does
    not pretend to be one — :attr:`durable` is False, so any caller that asks whether
    state will survive a restart gets an honest answer.
    """

    durable = False

    def record_workflow(self, workflow: Workflow, state_history: Sequence[str]) -> None:
        return None

    def record_action(self, action: ActionExecution) -> None:
        return None

    def record_proof(self, proof: ChangeProof) -> None:
        return None

    def record_session(self, workflow_id: str, session: object) -> None:
        return None
