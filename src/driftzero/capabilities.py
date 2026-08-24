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
from dataclasses import dataclass
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


MUTATION_AUTHORIZED_IDENTITIES = frozenset({AgentIdentity.REMEDIATION})
"""The Remediation Agent is the *only* identity permitted the mutation capability.

The orchestrator is deliberately absent: the frozen architecture names one authorized
identity, and a master/orchestration layer holding write authority would defeat the
separation the boundary exists to create.
"""


class CapabilityDenied(Exception):
    """An identity requested a capability it is not permitted to hold."""

    def __init__(self, holder: str, reason: str) -> None:
        self.holder = holder
        self.reason = reason
        super().__init__(f"capability denied for {holder!r}: {reason}")


@dataclass(frozen=True)
class CapabilityGrant:
    """Registry entry for one issued capability."""

    capability_id: str
    holder: str
    artifact_id: str
    change_id: str
    source_version: str
    revoked: bool = False


class MutationCapabilityBroker:
    """Mints and verifies mutation capabilities for a single process.

    The secret exists only in memory for the lifetime of the instance. Two brokers never
    accept each other's tokens, which is what makes a capability non-transferable
    between contexts.
    """

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._grants: dict[str, CapabilityGrant] = {}
        self._issued_count = 0
        self._denied_count = 0

    # ------------------------------------------------------------------ minting

    def issue(
        self,
        *,
        holder: AgentIdentity | str,
        artifact_id: str,
        change_id: str,
        source_version: str,
    ) -> MutationCapability:
        """Mint a capability bound to exactly one artifact, change, and version.

        Raises :class:`CapabilityDenied` for any identity outside
        ``MUTATION_AUTHORIZED_IDENTITIES`` — including unknown identities, which are
        denied rather than treated as unrecognised-therefore-harmless.
        """
        holder_value = str(holder)
        try:
            identity = AgentIdentity(holder_value)
        except ValueError:
            self._denied_count += 1
            raise CapabilityDenied(holder_value, "unknown logical identity") from None
        if identity not in MUTATION_AUTHORIZED_IDENTITIES:
            self._denied_count += 1
            raise CapabilityDenied(
                holder_value,
                "not authorized for the Artifact Mutation Tool; only "
                f"{sorted(str(i) for i in MUTATION_AUTHORIZED_IDENTITIES)} may hold it",
            )
        for name, value in (
            ("artifact_id", artifact_id),
            ("change_id", change_id),
            ("source_version", source_version),
        ):
            if not value or not value.strip():
                raise CapabilityDenied(holder_value, f"{name} must not be blank")

        self._issued_count += 1
        capability_id = f"cap-{self._issued_count:04d}-{secrets.token_hex(4)}"
        token = self._sign(
            capability_id=capability_id,
            holder=holder_value,
            artifact_id=artifact_id,
            change_id=change_id,
            source_version=source_version,
        )
        self._grants[capability_id] = CapabilityGrant(
            capability_id=capability_id,
            holder=holder_value,
            artifact_id=artifact_id,
            change_id=change_id,
            source_version=source_version,
        )
        return MutationCapability(
            capability_id=capability_id,
            holder=holder_value,
            authorized_artifact_ids=frozenset({artifact_id}),
            change_id=change_id,
            source_version=source_version,
            grant_token=token,
        )

    def revoke(self, capability_id: str) -> None:
        """Revoke a grant. Verification fails from this point on."""
        grant = self._grants.get(capability_id)
        if grant is not None:
            self._grants[capability_id] = CapabilityGrant(
                capability_id=grant.capability_id,
                holder=grant.holder,
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
            or grant.change_id != capability.change_id
            or grant.source_version != capability.source_version
            or grant.artifact_id not in capability.authorized_artifact_ids
        ):
            return False
        expected = self._sign(
            capability_id=capability.capability_id,
            holder=capability.holder,
            artifact_id=grant.artifact_id,
            change_id=capability.change_id,
            source_version=capability.source_version,
        )
        return hmac.compare_digest(expected, capability.grant_token)

    def _sign(
        self,
        *,
        capability_id: str,
        holder: str,
        artifact_id: str,
        change_id: str,
        source_version: str,
    ) -> str:
        payload = "".join(
            (capability_id, holder, artifact_id, change_id, source_version)
        ).encode("utf-8")
        return hmac.new(self._secret, payload, "sha256").hexdigest()

    # ------------------------------------------------------------------ observability

    @property
    def issued_count(self) -> int:
        return self._issued_count

    @property
    def denied_count(self) -> int:
        return self._denied_count

    def enforcement_disclosure(self) -> dict[str, object]:
        """What this broker may honestly claim about itself.

        Consumed by evidence writers so no artifact overstates the security model.
        """
        return {
            "enforcement_model": ENFORCEMENT_MODEL,
            "platform_enforced_per_agent_identity": PLATFORM_ENFORCED_PER_AGENT_IDENTITY,
            "shared_runtime_service_account": SHARED_RUNTIME_SERVICE_ACCOUNT,
            "per_agent_iam_principals": False,
            "note": (
                "Denials are application-level checks inside a single process. They are "
                "not Google Cloud IAM or GEAP Agent Identity decisions."
            ),
        }
