"""Who a person's command acts for, from its authority token only.

The gateway signs a command token for each person's command: bound to that command,
naming the person by their opaque reference (``custom_transport.protocol1.authority_tokens``).
``verify_frame`` has already checked the token's signature, issuer, lifetime and
binding. Here the team services check what only the handler can (README "Who may
present a token"): a command token comes only from the command's sender, the gateway host
that signed it. Then they turn the reference into the person and read their current
standing and roles from the gateway's copy of the team's people
(``access_control.people_directory``): a token names no roles, and a person disabled or
removed since the gateway signed it acts no more. The gateway brings that copy up to date
for the person before it sends their command (``assistant_gateway.team_services_client``).

Only gateway hosts present command tokens for people's own commands. Agents' operations
(root task tokens from lead and worker hosts) are not answered here yet: agents still
use their tools in their own process (plan 13B.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from access_control.people_directory import PeopleDirectory
from custom_messages.wire.frame import AUTHORITY_AT, COMMAND_BINDING, VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import HostCertificate

# The registry's role for the gateway's host, the only sender of people's own commands.
GATEWAY_ROLE = "gateway"


@dataclass(frozen=True)
class CommandPerson:
    """The active person a gateway command acts for, with their current roles."""

    person_id: str
    team_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class PersonCheck:
    """Who a command acts for: ``person`` when they may act, else ``reason``.

    ``person_id`` names the person the token names whenever the team knows them, also
    when they may not act (disabled or removed): their refusal is a final answer, stored
    under their command like any other.
    """

    person: Optional[CommandPerson]
    reason: str
    person_id: Optional[str]


def command_person(verified: VerifiedFrame, people: PeopleDirectory, logger: logging.Logger) -> PersonCheck:
    """The command's person, or the reason it is refused (logged).

    Raises ``FrameRefused`` (``bad_authority``) when the token's signer is not the frame's
    sender: the whole request is refused, as the contract has the handler do.
    """

    authority = verified.authority
    certificate = verified.certificate
    reason = ""
    person = None
    person_id = None
    if authority is None or certificate is None or GATEWAY_ROLE not in certificate.roles:
        reason = "Only a person's own commands from the gateway are answered here."
    elif authority.token.bound_to.type != COMMAND_BINDING or not isinstance(authority.token.person, str):
        reason = "The command's authority names no person's command."
    elif not _signed_by_sender(authority.certificate, certificate):
        logger.warning("Refused a command token presented by another host than the one that signed it. "
                       "event=team_command_token_wrong_sender kind=%s sender=%s signer=%s", verified.frame.kind,
                       certificate.runtime, authority.certificate.runtime)
        raise FrameRefused("bad_authority", "a command token is accepted only from the command's sender",
                           at=AUTHORITY_AT)
    else:
        now = people.person_by_ref(authority.token.person)
        if now is None:
            reason = "The command names a person this team does not know."
        else:
            person_id = now.person_id
            if not now.active or not now.roles:
                reason = f"The person is {now.describe()} in this team."
            else:
                person = CommandPerson(now.person_id, people.team_id, now.roles)
    if person is None:
        logger.warning("Refused a command without an active person. event=team_command_person_refused kind=%s "
                       "sender=%s person=%s reason=%s", verified.frame.kind, verified.frame.issuer.runtime, person_id,
                       reason)
    return PersonCheck(person, reason, person_id)


def _signed_by_sender(signer: HostCertificate, sender: HostCertificate) -> bool:
    """Whether the token's signer is the host that sent the frame (by host, across certificate renewals)."""

    return (signer.installation, signer.runtime) == (sender.installation, sender.runtime)
