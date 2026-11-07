"""Who a person's command acts for, from its authority token only.

The gateway signs a command token for each person's command: bound to that command,
naming the person by their opaque reference (``custom_transport.protocol1.authority_tokens``).
``verify_frame`` has already checked the token's signature, issuer, lifetime and
binding. Here the team services turn the reference into the person and read their
current standing and roles from the gateway's copy of the team's people
(``access_control.people_directory``): a token names no roles, and a person disabled
or removed since the gateway signed it acts no more.

Only gateway hosts present command tokens for people's own commands. Agents' operations
(root task tokens from lead and worker hosts) are not answered here yet: agents still
use their tools in their own process (plan 13B.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from access_control.people_directory import PeopleDirectory
from custom_messages.wire.frame import VerifiedFrame

GATEWAY_ROLE = "gateway"
COMMAND_BINDING = "command"


@dataclass(frozen=True)
class CommandPerson:
    """The active person a gateway command acts for, with their current roles."""

    person_id: str
    team_id: str
    roles: tuple[str, ...]


def command_person(verified: VerifiedFrame, people: PeopleDirectory,
                   logger: logging.Logger) -> tuple[Optional[CommandPerson], str]:
    """The command's person, or None with the reason it is refused (logged)."""

    authority = verified.authority
    certificate = verified.certificate
    reason = ""
    person = None
    if authority is None or certificate is None or GATEWAY_ROLE not in certificate.roles:
        reason = "Only a person's own commands from the gateway are answered here."
    elif authority.token.bound_to.type != COMMAND_BINDING or not isinstance(authority.token.person, str):
        reason = "The command's authority names no person's command."
    else:
        now = people.person_by_ref(authority.token.person)
        if now is None:
            reason = "The command names a person this team does not know."
        elif not now.active or not now.roles:
            reason = f"The person is {now.describe()} in this team."
        else:
            person = CommandPerson(now.person_id, people.team_id, now.roles)
    if person is None:
        logger.warning("Refused a command without an active person. event=team_command_person_refused kind=%s "
                       "sender=%s reason=%s", verified.frame.kind, verified.frame.issuer.runtime, reason)
    return person, reason
