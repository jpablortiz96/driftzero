"""T074 — logical agent identity and the mutation capability broker.

Scope note
----------
This is the **narrowest** minting/verification mechanism T074 needs so the Artifact
Mutation Tool cannot be driven by a forged capability. The broader in-process
authorization broker — keyed on logical identity across *all* tools, with denials
recorded as evidence — is T075 and lives in ``truth_engine/authz_broker.py``. Nothing
here anticipates that work.

Honesty about what this enforces
--------------------------------
``ENFORCEMENT_MODEL`` is ``APPLICATION_LEVEL_ENFORCEMENT`` and
``PLATFORM_ENFORCED_PER_AGENT_IDENTITY`` is ``False``.

In the fallback architecture every agent is a *logical context inside one process*
(`driftzero-api`) running under **one shared runtime service account**. The identities
below are application-level labels, not Google Cloud IAM principals, and a denial
produced here is an application check — not a platform authorization decision. Under
GEAP these would become real Agent Identities and the denial would come from the
platform; that is not what is running. Evidence generated from this module must never
claim per-agent IAM isolation (contracts/agents.md § Honesty rule).

How forgery is prevented
------------------------
A capability carries a ``grant_token`` that is an HMAC over its own bound fields, keyed
by a secret generated per broker instance and never persisted or logged. Constructing
``MutationCapability(holder="driftzero-remediation", ...)`` by hand yields a token that
cannot verify, so the tool boundary rejects it. The broker also keeps a registry, which
adds revocation on top of integrity.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from driftzero.tools.artifact_mutation import MutationCapability

ENFORCEMENT_MODEL = "APPLICATION_LEVEL_ENFORCEMENT"
"""What this broker actually is. Never upgrade this string without the platform."""

PLATFORM_ENFORCED_PER_AGENT_IDENTITY = False
"""False in the fallback: one process, one shared runtime service account."""

SHARED_RUNTIME_SERVICE_ACCOUNT = "driftzero-run-sa"
"""All logical agents share this account. There is no per-agent IAM principal."""


class AgentIdentity(StrEnum):
    """Logical application identities from contracts/agents.md.

    These are labels this process assigns to its own call sites. They are not IAM
    principals and carry no platform-enforced meaning.
    """

    CHANGE_INTELLIGENCE = "driftzero-change-intel"
    REMEDIATION = "driftzero-remediation"
    ENABLEMENT = "driftzero-enablement"
    FIELD_VERIFICATION = "driftzero-field-verify"
    ORCHESTRATOR = "driftzero-orchestrator"


class ToolCapability(StrEnum):
    """Capabilities an agent may be authorized to exercise.

    One member today. Future tools (frontline delivery, field observation) are added
    here and to :data:`AUTHORIZATION_POLICY` — never by standing up a second
    authorization system.
    """

    ARTIFACT_MUTATION = "ARTIFACT_MUTATION"


AUTHORIZATION_POLICY: frozenset[tuple[AgentIdentity, ToolCapability]] = frozenset(
    {
        (AgentIdentity.REMEDIATION, ToolCapability.ARTIFACT_MUTATION),
    }
)
"""**The** authorization policy. There is no second table anywhere in the system.

An allow is an explicit membership. Every other (identity, tool) pair — including
identities and tools this codebase does not yet know about — is denied. There is no
wildcard, no default-allow, and no implicit inheritance: the set is the whole policy.

The orchestrator is deliberately absent. A master/orchestration layer holding write
authority would defeat the separation this boundary exists to create, and a future UI
must request remediation through the application layer rather than be added here.
"""


def is_authorized(identity: AgentIdentity | str, tool: ToolCapability | str) -> bool:
    """Fail-closed policy lookup. Unknown identity or tool is denied, never allowed."""
    try:
        pair = (AgentIdentity(str(identity)), ToolCapability(str(tool)))
    except ValueError:
        return False
    return pair in AUTHORIZATION_POLICY


def authorized_identities_for(tool: ToolCapability) -> frozenset[AgentIdentity]:
    """Derived view of the policy for one tool. Never an independent source of truth."""
    return frozenset(i for i, t in AUTHORIZATION_POLICY if t is tool)


MUTATION_AUTHORIZED_IDENTITIES = authorized_identities_for(ToolCapability.ARTIFACT_MUTATION)
"""Compatibility view, **derived** from :data:`AUTHORIZATION_POLICY`.

Retained so existing call sites and tests keep reading naturally. It is computed, not
declared, so it cannot drift from the policy it describes.
"""


class DenialReason(StrEnum):
    """Deterministic reason codes for a refused capability request."""

    IDENTITY_NOT_AUTHORIZED_FOR_TOOL = "IDENTITY_NOT_AUTHORIZED_FOR_TOOL"
    UNKNOWN_IDENTITY = "UNKNOWN_IDENTITY"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    MALFORMED_REQUEST = "MALFORMED_REQUEST"


class CapabilityDenied(Exception):
    """An identity requested a capability it is not permitted to hold."""

    def __init__(
        self, holder: str, reason: str, record: DenialEvidence | None = None
    ) -> None:
        self.holder = holder
        self.reason = reason
        self.record = record
        """The denial evidence produced for this refusal, when one was recorded."""
        super().__init__(f"capability denied for {holder!r}: {reason}")


@dataclass(frozen=True)
class DenialEvidence:
    """A recorded authorization denial and proof of its non-effect.

    Evidence, never authority. It has no field able to carry a workflow state, a
    verdict, or a proof — a denial records that nothing happened, and structurally
    cannot make anything happen.

    ``artifact_sha256_unchanged`` is optional because the denial path terminates
    *before* the tool reads the repository; it is populated only when a caller can
    supply a genuinely measured before/after pair. Absent that, it stays ``None``
    rather than asserting an unverified true.
    """

    denial_id: str
    requested_by: str
    requested_tool: str
    decision: str = "DENIED"
    reason_code: DenialReason = DenialReason.IDENTITY_NOT_AUTHORIZED_FOR_TOOL
    policy_basis: str = ""
    artifact_id: str | None = None
    change_id: str | None = None
    source_version: str | None = None
    enforcement_model: str = ENFORCEMENT_MODEL
    platform_enforced_per_agent_identity: bool = PLATFORM_ENFORCED_PER_AGENT_IDENTITY
    shared_runtime_service_account: str = SHARED_RUNTIME_SERVICE_ACCOUNT
    artifact_sha256_unchanged: bool | None = None
    dispatch_count_delta: int = 0
    no_state_transition: bool = True
    occurred_at: datetime | None = None

    def as_evidence_ref(self) -> str:
        """Reference string for ``EvidenceManifest.rejected_result_refs``."""
        return (
            f"authorization-denial:{self.denial_id}:{self.requested_by}:"
            f"{self.requested_tool}:{self.reason_code}"
        )


@dataclass(frozen=True)
class CapabilityGrant:
    """Registry entry for one issued capability."""

    capability_id: str
    holder: str
    tool: ToolCapability
    artifact_id: str
    change_id: str
    source_version: str
    revoked: bool = False


class MutationCapabilityBroker:
    """Mutation-specific adapter over the single authorization authority.

    This is **not** a second policy authority. Every allow/deny decision it makes is
    delegated to :func:`is_authorized` against :data:`AUTHORIZATION_POLICY`; what this
    class owns is the capability *mechanism* — HMAC integrity, the grant registry, and
    revocation — for one tool.

    The secret exists only in memory for the lifetime of the instance. Two brokers never
    accept each other's tokens, which is what makes a capability non-transferable
    between contexts.
    """

    tool = ToolCapability.ARTIFACT_MUTATION
    """The single tool this adapter mints for. Bound into every capability it issues."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._secret = secrets.token_bytes(32)
        self._grants: dict[str, CapabilityGrant] = {}
        self._issued_count = 0
        self._denied_count = 0
        self._denials: list[DenialEvidence] = []
        self._clock = clock

    # ------------------------------------------------------------------ minting

    def issue(
        self,
        *,
        holder: AgentIdentity | str,
        artifact_id: str,
        change_id: str,
        source_version: str,
    ) -> MutationCapability:
        """Mint a capability bound to one identity, tool, artifact, change, and version.

        The allow/deny decision is delegated to :func:`is_authorized` — this method owns
        the mechanism, never the policy. Every refusal records :class:`DenialEvidence`
        and raises before any capability material is produced.
        """
        holder_value = str(holder)
        try:
            identity = AgentIdentity(holder_value)
        except ValueError:
            raise self._deny(
                holder_value,
                DenialReason.UNKNOWN_IDENTITY,
                "identity is not a known logical agent identity",
                artifact_id=artifact_id,
                change_id=change_id,
                source_version=source_version,
            ) from None

        if not is_authorized(identity, self.tool):
            raise self._deny(
                holder_value,
                DenialReason.IDENTITY_NOT_AUTHORIZED_FOR_TOOL,
                f"AUTHORIZATION_POLICY has no ({identity}, {self.tool}) entry; only "
                f"{sorted(str(i) for i in authorized_identities_for(self.tool))} may hold it",
                artifact_id=artifact_id,
                change_id=change_id,
                source_version=source_version,
            )

        for name, value in (
            ("artifact_id", artifact_id),
            ("change_id", change_id),
            ("source_version", source_version),
        ):
            if not value or not value.strip():
                raise self._deny(
                    holder_value,
                    DenialReason.MALFORMED_REQUEST,
                    f"{name} must not be blank",
                    artifact_id=artifact_id,
                    change_id=change_id,
                    source_version=source_version,
                )

        self._issued_count += 1
        capability_id = f"cap-{self._issued_count:04d}-{secrets.token_hex(4)}"
        token = self._sign(
            capability_id=capability_id,
            holder=holder_value,
            tool=self.tool,
            artifact_id=artifact_id,
            change_id=change_id,
            source_version=source_version,
        )
        self._grants[capability_id] = CapabilityGrant(
            capability_id=capability_id,
            holder=holder_value,
            tool=self.tool,
            artifact_id=artifact_id,
            change_id=change_id,
            source_version=source_version,
        )
        return MutationCapability(
            capability_id=capability_id,
            holder=holder_value,
            tool=str(self.tool),
            authorized_artifact_ids=frozenset({artifact_id}),
            change_id=change_id,
            source_version=source_version,
            grant_token=token,
        )

    def _deny(
        self,
        holder: str,
        reason: DenialReason,
        detail: str,
        *,
        artifact_id: str | None = None,
        change_id: str | None = None,
        source_version: str | None = None,
    ) -> CapabilityDenied:
        """Record the denial and build the exception. Never mints anything."""
        self._denied_count += 1
        record = DenialEvidence(
            denial_id=f"deny-{self._denied_count:04d}-{secrets.token_hex(4)}",
            requested_by=holder,
            requested_tool=str(self.tool),
            reason_code=reason,
            policy_basis=detail,
            artifact_id=artifact_id or None,
            change_id=change_id or None,
            source_version=source_version or None,
            occurred_at=self._clock() if self._clock else None,
        )
        self._denials.append(record)
        return CapabilityDenied(holder, detail, record)

    def revoke(self, capability_id: str) -> None:
        """Revoke a grant. Verification fails from this point on."""
        grant = self._grants.get(capability_id)
        if grant is not None:
            self._grants[capability_id] = CapabilityGrant(
                capability_id=grant.capability_id,
                holder=grant.holder,
                tool=grant.tool,
                artifact_id=grant.artifact_id,
                change_id=grant.change_id,
                source_version=grant.source_version,
                revoked=True,
            )

    # ------------------------------------------------------------------ verifying

    def verify(self, capability: MutationCapability) -> bool:
        """True only for an unrevoked capability this broker actually issued.

        Checks integrity *and* registry state. A hand-constructed capability fails the
        HMAC comparison; a revoked one fails the registry check even though its token is
        still mathematically valid.
        """
        grant = self._grants.get(capability.capability_id)
        if grant is None or grant.revoked:
            return False
        if (
            grant.holder != capability.holder
            or str(grant.tool) != capability.tool
            or grant.change_id != capability.change_id
            or grant.source_version != capability.source_version
            or grant.artifact_id not in capability.authorized_artifact_ids
        ):
            return False
        # Re-check policy at verification: a capability whose holder lost authorization
        # since minting must stop verifying, not coast on an old signature.
        if not is_authorized(grant.holder, grant.tool):
            return False
        expected = self._sign(
            capability_id=capability.capability_id,
            holder=capability.holder,
            tool=grant.tool,
            artifact_id=grant.artifact_id,
            change_id=capability.change_id,
            source_version=capability.source_version,
        )
        return hmac.compare_digest(expected, capability.grant_token)

    def verify_for_tool(
        self, capability: MutationCapability, tool: ToolCapability | str
    ) -> bool:
        """Verify a capability *and* that it was minted for ``tool``.

        A capability minted for one tool must never authorize another, even when its
        signature is otherwise valid.
        """
        return capability.tool == str(tool) and self.verify(capability)

    def _sign(
        self,
        *,
        capability_id: str,
        holder: str,
        tool: ToolCapability,
        artifact_id: str,
        change_id: str,
        source_version: str,
    ) -> str:
        """HMAC over every bound field, the tool included.

        Tool participation is what stops a mutation capability being replayed against a
        future delivery or verification tool: changing the tool changes the signature.
        """
        payload = "\x1f".join(
            (capability_id, holder, str(tool), artifact_id, change_id, source_version)
        ).encode("utf-8")
        return hmac.new(self._secret, payload, "sha256").hexdigest()

    # ------------------------------------------------------------------ observability

    @property
    def issued_count(self) -> int:
        return self._issued_count

    @property
    def denied_count(self) -> int:
        return self._denied_count

    @property
    def denials(self) -> tuple[DenialEvidence, ...]:
        """Every denial this broker recorded, in order. One record per refused request."""
        return tuple(self._denials)

    def denial_evidence_refs(self) -> tuple[str, ...]:
        """Reference strings for ``EvidenceManifest.rejected_result_refs``."""
        return tuple(record.as_evidence_ref() for record in self._denials)

    def enforcement_disclosure(self) -> dict[str, object]:
        """What this broker may honestly claim about itself.

        Consumed by evidence writers so no artifact overstates the security model.
        """
        return {
            "enforcement_model": ENFORCEMENT_MODEL,
            "platform_enforced_per_agent_identity": PLATFORM_ENFORCED_PER_AGENT_IDENTITY,
            "shared_runtime_service_account": SHARED_RUNTIME_SERVICE_ACCOUNT,
            "per_agent_iam_principals": False,
            "policy_entries": sorted(f"{i}->{t}" for i, t in AUTHORIZATION_POLICY),
            "cloud_iam_enforcement": False,
            "agent_identity_enforcement": False,
            "agent_gateway_enforcement": False,
            "model_armor_enforcement": False,
            "note": (
                "Denials are application-level checks inside a single process. They are "
                "not Google Cloud IAM or GEAP Agent Identity decisions."
            ),
        }
