"""T078 — the local pilot delivery mechanism and its resolvable receipt.

A UI render is not delivery. An agent saying "delivered" is not delivery. A worker
acknowledgment is not delivery. Delivery is established only when the mechanism itself
produces a receipt that can be independently resolved and whose content binds to the
exact instruction that was sent.

Transport neutrality
--------------------
:class:`DeliveryChannel` is the whole coupling surface. The Frontline Enablement Agent
knows nothing about HTTP, email, Pub/Sub, Firestore, or a UI. Replacing this local pilot
channel with a durable or cloud channel changes no agent semantics.

Append-only receipts
--------------------
The receipt store keeps every receipt under its own reference and refuses to overwrite
one. This is the lesson T073 taught the hard way: a reference that no longer resolves to
the thing it described is not evidence, and a store that overwrites in place cannot hold
history.

This is a **local pilot transport**, not a mock. It really delivers into the pilot
frontline channel, and the receipt it returns is the real thing Crossing 3 validates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from driftzero.capabilities import ToolCapability, ToolGrant
from driftzero.truth_engine.evidence import canonical_hash

CHANNEL_LOCAL_PILOT = "local_pilot_frontline"
"""Mechanism identity recorded on every receipt this channel issues."""


class DeliveryStatus(StrEnum):
    """Outcome the mechanism itself reports. Never an agent's opinion."""

    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class DeliveryDispatchError(Exception):
    """The channel could not accept the payload. Nothing was delivered."""


class DeliveryNotAuthorized(Exception):
    """No valid ``FRONTLINE_DELIVERY`` grant accompanied the dispatch.

    Raised before anything is sent, so a refused delivery leaves no receipt, no payload,
    and no dispatch count behind.
    """


class UncertainDeliveryError(Exception):
    """Dispatched, outcome unknown.

    The delivery may or may not have landed, so the action becomes uncertain and must be
    reconciled against the mechanism's receipt rather than blindly re-sent.
    """


@dataclass(frozen=True)
class DeliveryReceipt:
    """Evidence produced by the mechanism that a specific payload was delivered.

    ``payload_hash`` is what makes this more than a claim: it binds the receipt to the
    exact content delivered, so Crossing 3 can re-derive it from the authoritative
    instruction rather than trusting the identifier alone.
    """

    receipt_id: str
    instruction_id: str
    change_id: str
    channel: str
    destination_ref: str
    payload_hash: str
    status: DeliveryStatus
    issued_at: datetime
    identity_basis: str
    """How the destination was identified. Honest about what is not proven."""

    @property
    def evidence_ref(self) -> str:
        """The reference Crossing 3 resolves. Stable and derived from the receipt id."""
        return f"{self.channel}:receipt:{self.receipt_id}"

    @property
    def delivered(self) -> bool:
        return self.status is DeliveryStatus.DELIVERED


@runtime_checkable
class DeliveryChannel(Protocol):
    """The narrowest transport contract.

    Implementations MUST return a receipt that resolves through :meth:`resolve`, and
    MUST bind ``payload_hash`` to the content actually sent.
    """

    @property
    def channel(self) -> str:
        """Mechanism identity recorded on receipts."""
        ...

    def deliver(
        self,
        *,
        instruction_id: str,
        change_id: str,
        payload: Mapping[str, Any],
        destination_ref: str,
        occurred_at: datetime,
        grant: ToolGrant,
        grant_verifier: Callable[[ToolGrant], bool],
    ) -> DeliveryReceipt:
        """Deliver ``payload`` and return the mechanism's receipt.

        Implementations MUST verify ``grant`` through ``grant_verifier`` before sending
        anything, and MUST raise :class:`DeliveryNotAuthorized` when it does not verify.
        """
        ...

    def resolve(self, evidence_ref: str) -> DeliveryReceipt | None:
        """Retrieve the receipt at ``evidence_ref``, or ``None``."""
        ...

    def resolvable_refs(self) -> frozenset[str]:
        """Every reference this mechanism can currently resolve."""
        ...


def payload_hash(payload: Mapping[str, Any]) -> str:
    """Canonical hash of the delivered content.

    Reuses the frozen M0 hashing helper so a hash computed here is the same value the
    Truth Engine computes for the same content. No second hash implementation exists.
    """
    return canonical_hash(dict(payload))


class LocalPilotDeliveryChannel:
    """In-process delivery for the local physical pilot.

    Real transport for the pilot's scope: it hands the instruction to the frontline
    surface this deployment actually serves, and issues a receipt bound to that exact
    payload. It is not durable and it is not a workforce messaging system — the receipt
    records that honestly rather than implying reach it does not have.
    """

    channel = CHANNEL_LOCAL_PILOT

    IDENTITY_BASIS = (
        "UNAUTHENTICATED_LOCAL_SESSION — the destination is a local pilot frontline "
        "surface, not a named employee, mailbox, or device. No enterprise workforce "
        "identity system is integrated."
    )

    def __init__(self) -> None:
        self._receipts: dict[str, DeliveryReceipt] = {}
        self._delivered: dict[str, dict[str, Any]] = {}
        self.dispatch_count = 0

    def deliver(
        self,
        *,
        instruction_id: str,
        change_id: str,
        payload: Mapping[str, Any],
        destination_ref: str,
        occurred_at: datetime,
        grant: ToolGrant,
        grant_verifier: Callable[[ToolGrant], bool],
    ) -> DeliveryReceipt:
        """Deliver the payload into the pilot frontline channel and issue a receipt.

        Authorization is checked first and fails closed. The mechanism does not decide
        *who* may deliver — that is the policy table's job — it only refuses to act
        without a grant the broker actually issued for this destination and change.
        """
        self._require_authorization(
            grant,
            grant_verifier,
            destination_ref=destination_ref,
            change_id=change_id,
        )
        if not instruction_id.strip() or not change_id.strip():
            raise DeliveryDispatchError("instruction_id and change_id are required")
        if not payload:
            raise DeliveryDispatchError("refusing to deliver an empty payload")

        self.dispatch_count += 1
        receipt = DeliveryReceipt(
            receipt_id=f"rcpt-{self.dispatch_count:04d}-{instruction_id}",
            instruction_id=instruction_id,
            change_id=change_id,
            channel=self.channel,
            destination_ref=destination_ref,
            payload_hash=payload_hash(payload),
            status=DeliveryStatus.DELIVERED,
            issued_at=occurred_at,
            identity_basis=self.IDENTITY_BASIS,
        )
        if receipt.evidence_ref in self._receipts:  # pragma: no cover - defensive
            raise DeliveryDispatchError(
                f"refusing to overwrite the receipt at {receipt.evidence_ref}"
            )
        self._receipts[receipt.evidence_ref] = receipt
        self._delivered[receipt.evidence_ref] = dict(payload)
        return receipt

    @staticmethod
    def _require_authorization(
        grant: ToolGrant,
        grant_verifier: Callable[[ToolGrant], bool],
        *,
        destination_ref: str,
        change_id: str,
    ) -> None:
        """Refuse to dispatch without a verified, correctly scoped delivery grant."""
        if grant is None or grant_verifier is None:
            raise DeliveryNotAuthorized(
                "a broker-issued FRONTLINE_DELIVERY grant is required to deliver"
            )
        if not grant_verifier(grant):
            raise DeliveryNotAuthorized(
                "the supplied grant is not a valid broker-issued "
                f"{ToolCapability.FRONTLINE_DELIVERY} capability"
            )
        if not grant.covers(destination_ref):
            raise DeliveryNotAuthorized(
                f"the grant does not cover destination {destination_ref!r}"
            )
        if grant.change_id != change_id:
            raise DeliveryNotAuthorized(
                f"the grant was issued for change {grant.change_id!r}, not {change_id!r}"
            )

    def resolve(self, evidence_ref: str) -> DeliveryReceipt | None:
        """Retrieve a receipt. Append-only: resolving never mutates the store."""
        return self._receipts.get(evidence_ref)

    def resolve_payload(self, evidence_ref: str) -> dict[str, Any] | None:
        """The exact content delivered under ``evidence_ref``, for audit."""
        payload = self._delivered.get(evidence_ref)
        return dict(payload) if payload is not None else None

    def resolvable_refs(self) -> frozenset[str]:
        return frozenset(self._receipts)

    def recoverable_receipt_ref(self, instruction_id: str) -> str | None:
        """The receipt this mechanism can still produce for a logical delivery.

        Used by reconciliation after an uncertain dispatch: if a first attempt really
        landed, its receipt resolves it without re-sending.
        """
        for ref, receipt in self._receipts.items():
            if receipt.instruction_id == instruction_id and receipt.delivered:
                return ref
        return None
