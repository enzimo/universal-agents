"""Who this host is when it signs: its certificate's ids and the agent sessions it holds.

A frame's issuer names the host's installation, team, runtime, membership and
revocation epochs and key id, all from its current certificate, plus the agent and
session when the host signs for one of its agents (README "Who sent it"). The
receiver looks every one of them up; nothing here is trusted by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from custom_messages.wire.kinds import INSTALLATION_SCOPE
from custom_messages.wire.trust import HostCertificate
from custom_transport.protocol1.host_keys import HostKeys


class IdentityError(ValueError):
    """The host's keys and certificate do not belong together, or it signs for an agent it holds no session for."""


@dataclass(frozen=True)
class HostIdentity:
    """A host's signing identity: its keys, the certificate that names them, and its agents' sessions."""

    keys: HostKeys
    certificate: HostCertificate
    # agent id -> session id, for the agents this host runs (sessions.open).
    sessions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.certificate.runtime != self.keys.runtime or self.certificate.key_id != self.keys.key_id:
            raise IdentityError("the certificate names another host or key than this host's keys")

    @property
    def installation(self) -> str:
        return self.certificate.installation

    @property
    def runtime(self) -> str:
        return self.certificate.runtime

    def with_certificate(self, certificate: HostCertificate) -> "HostIdentity":
        """The same host after a renewal: its new certificate, its agents' sessions kept."""

        return HostIdentity(self.keys, certificate, dict(self.sessions))

    def with_session(self, agent: str, session: str) -> "HostIdentity":
        return HostIdentity(self.keys, self.certificate, {**self.sessions, agent: session})

    def without_session(self, agent: str) -> "HostIdentity":
        return HostIdentity(self.keys, self.certificate, {key: value for key, value in self.sessions.items() if key != agent})

    def issuer(self, team: str, agent: Optional[str] = None) -> dict[str, Any]:
        """The frame's issuer: this host for ``team``, and the agent and its session when it signs for one."""

        if team not in self.certificate.teams and team != INSTALLATION_SCOPE:
            raise IdentityError(f"the certificate does not serve team {team!r}")
        issuer: dict[str, Any] = {
            "installation": self.installation,
            "team": team,
            "runtime": self.runtime,
            "membership_epoch": self.certificate.membership_epoch,
            "revocation_epoch": self.certificate.revocation_epoch,
            "key_id": self.keys.key_id,
        }
        if agent is not None:
            session = self.sessions.get(agent)
            if session is None:
                raise IdentityError(f"this host holds no session for agent {agent!r}")
            issuer = {**issuer, "agent": agent, "session": session}
        return issuer
