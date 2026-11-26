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
from custom_messages.wire.authority_bindings import (
    SCHEDULER_ISSUER_ROLE,
    is_schedule_permit_binding,
)
from custom_messages.wire.frame import AUTHORITY_AT, COMMAND_BINDING, VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import HostCertificate

# The registry's role for the gateway's host, the only sender of people's own commands.
GATEWAY_ROLE = "gateway"
AGENT_HOST_ROLES = frozenset({"lead_host", "worker_host"})
ROOT_TASK_BINDING = "root_task"


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


@dataclass(frozen=True)
class InstallActorCheck:
    """Who a ``schedules.install`` request acts for (gateway command or agent root task)."""

    person: Optional[CommandPerson]
    reason: str
    person_id: Optional[str]
    from_gateway: bool = False
    from_agent: bool = False
    permit_schedule_id: Optional[str] = None
    agent_id: Optional[str] = None


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


def schedule_install_actor(
    verified: VerifiedFrame, people: PeopleDirectory, logger: logging.Logger
) -> InstallActorCheck:
    """Resolve who ``schedules.install`` acts for: a gateway command or an agent's root-task token.

    Gateway senders present a ``command`` token signed by themselves; lead/worker hosts
    present a ``root_task`` token signed by the gateway or scheduler. Permit-bound tokens
    (``schedule_permit:*`` / ``schedule_owner_permit:*``) are flagged via
    ``permit_schedule_id`` so the schedule handler validates the live schedule row.
    """

    authority = verified.authority
    certificate = verified.certificate
    if authority is None or certificate is None:
        return InstallActorCheck(None, "Missing authority token for schedule installation.", None)
    bound = authority.token.bound_to
    if bound.type == COMMAND_BINDING:
        check = command_person(verified, people, logger)
        return InstallActorCheck(
            person=check.person,
            reason=check.reason,
            person_id=check.person_id,
            from_gateway=True,
            from_agent=False,
        )
    if bound.type != ROOT_TASK_BINDING:
        return InstallActorCheck(None, "Schedule installation requires a command or root-task authority token.", None)
    if not (set(certificate.roles) & AGENT_HOST_ROLES):
        return InstallActorCheck(None, "Only lead or worker hosts may present a root-task token here.", None)
    if not isinstance(authority.token.person, str):
        return InstallActorCheck(
            None, "A schedule installation requires an active person in the authority token.", None, from_agent=True
        )
    permit_schedule_id: Optional[str] = None
    if is_schedule_permit_binding(bound.id):
        if SCHEDULER_ISSUER_ROLE not in authority.certificate.roles or not isinstance(authority.token.schedule_id, str):
            return InstallActorCheck(
                None,
                "A schedule permit token must be signed by the scheduler and name its schedule.",
                None,
                from_agent=True,
            )
        permit_schedule_id = authority.token.schedule_id
    now = people.person_by_ref(authority.token.person)
    if now is None:
        reason = "The task token names a person this team does not know."
        logger.warning(
            "Refused a schedule installation without a known person. event=team_schedule_install_person_refused "
            "sender=%s reason=%s",
            verified.frame.issuer.runtime,
            reason,
        )
        return InstallActorCheck(None, reason, None, from_agent=True, permit_schedule_id=permit_schedule_id)
    if not now.active or not now.roles:
        reason = f"The person is {now.describe()} in this team."
        logger.warning(
            "Refused a schedule installation for an inactive person. event=team_schedule_install_person_refused "
            "sender=%s person=%s reason=%s",
            verified.frame.issuer.runtime,
            now.person_id,
            reason,
        )
        return InstallActorCheck(None, reason, now.person_id, from_agent=True, permit_schedule_id=permit_schedule_id)
    return InstallActorCheck(
        person=CommandPerson(now.person_id, people.team_id, now.roles),
        reason="",
        person_id=now.person_id,
        from_gateway=False,
        from_agent=True,
        permit_schedule_id=permit_schedule_id,
        agent_id=verified.frame.issuer.agent,
    )


def _signed_by_sender(signer: HostCertificate, sender: HostCertificate) -> bool:
    """Whether the token's signer is the host that sent the frame (by host, across certificate renewals)."""

    return (signer.installation, signer.runtime) == (sender.installation, sender.runtime)
