"""Helpers behind people's access commands: ``assistant_cli access`` and ``/access``.

Both act as one person: ``--by`` on the command line, the signed-in person in
chat. Their current team roles come from the gateway's membership records, and
the access service checks that the person may give, revoke, block or unblock
that access (``access_control.decisions`` and ``access_control.requests``).
The command line also lists account links and removes them (a person's own, or
any as a team administrator); people create links through the broker's form
(``/link``).

``describe_grant`` is the command line's one-line record; ``summarize_grant``
says the same in plain words for chat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import (
    PersonPrincipal,
    agent_role_resolver,
    agent_type_id,
    role_id,
)
from access_control.folders import FolderIdError, grantable_folder_id
from access_control.people import is_administrator
from access_control.grant_entries import entry_key, lasting_order
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccountLinkV1,
    LinkOwnerKind,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

_DURATION = re.compile(r"^(\d+)([mhd])$")
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


class OperatorCommandError(ValueError):
    """The command's arguments are invalid; the message says how to fix them."""


class MembershipLookup(Protocol):
    def roles_of(self, person_id: str, team_id: str) -> tuple[str, ...] | None: ...


def parse_subject(text: str, team_id: str) -> SubjectRefV1:
    """``agent-type:<Type>``, ``agent:<id>``, ``role:<name>``, ``user:<id>`` or ``team``."""

    kind, _, value = str(text).strip().partition(":")
    value = value.strip()
    if kind == "team" and not value:
        return SubjectRefV1(kind=SubjectKind.TEAM, id=team_id)
    if not value:
        raise OperatorCommandError(f"--to needs a value after '{kind}:'.")
    if kind == "agent-type":
        return SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(team_id, value))
    if kind == "agent":
        return SubjectRefV1(kind=SubjectKind.AGENT, id=value)
    if kind == "role":
        return SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(team_id, value))
    if kind == "user":
        return SubjectRefV1(kind=SubjectKind.USER, id=value)
    raise OperatorCommandError(
        f"--to must be agent-type:<Type>, agent:<id>, role:<name>, user:<id> or team, not {text!r}."
    )


FOLDER_TARGET_PREFIX = "folder:"


def folder_target(folder: str) -> TargetRefV1:
    """A folder below team/, agents/ or data/<name>/ (access_control.folders)."""

    try:
        return TargetRefV1(kind=TargetKind.FOLDER, id=grantable_folder_id(folder))
    except FolderIdError as exc:
        raise OperatorCommandError(str(exc)) from exc


def parse_target(*, tool: str | None, tool_set: str | None, folder: str | None = None) -> TargetRefV1:
    if sum(bool(value) for value in (tool, tool_set, folder)) != 1:
        raise OperatorCommandError("Name exactly one of --tool, --tool-set or --folder.")
    if folder:
        return folder_target(folder)
    if tool:
        kind = TargetKind.OPERATION if "#" in tool else TargetKind.TOOL
        return TargetRefV1(kind=kind, id=tool)
    return TargetRefV1(kind=TargetKind.TOOL_SET, id=str(tool_set))


def parse_target_text(text: str) -> TargetRefV1:
    """``tool`` or ``tool#operation`` (tool ids contain ``:``); ``folder:team/x`` or a
    path with ``/`` (``data/reports/2026``) for a folder; otherwise a tool set id."""

    value = str(text or "").strip()
    if not value:
        raise OperatorCommandError("Name a tool, tool#operation, tool set, or folder.")
    if value.startswith(FOLDER_TARGET_PREFIX):
        return folder_target(value[len(FOLDER_TARGET_PREFIX) :])
    if "/" in value and ":" not in value and "#" not in value:
        return folder_target(value)
    if "#" in value:
        return TargetRefV1(kind=TargetKind.OPERATION, id=value)
    if ":" in value:
        return TargetRefV1(kind=TargetKind.TOOL, id=value)
    return TargetRefV1(kind=TargetKind.TOOL_SET, id=value)


def block_subject(
    *,
    agent_type: str | None,
    agent: str | None,
    person: str | None,
    team_id: str,
    role: str | None = None,
    team: bool = False,
) -> SubjectRefV1:
    """The command line's ``--agent-type``, ``--agent``, ``--person``, ``--role`` or ``--team`` (exactly one)."""

    named = [
        (kind, value)
        for kind, value in (("agent-type", agent_type), ("agent", agent), ("user", person), ("role", role))
        if value
    ]
    if len(named) + int(team) != 1:
        raise OperatorCommandError("Name exactly one of --agent-type, --agent, --person, --role or --team.")
    if team:
        return parse_subject("team", team_id)
    kind, value = named[0]
    return parse_subject(f"{kind}:{value}", team_id)


def block_target(*, tool: str | None, operation: str | None, folder: str | None = None) -> TargetRefV1:
    """The command line's ``--tool``, ``--operation`` (``tool#operation``) or ``--folder``, exactly one."""

    if sum(bool(value) for value in (tool, operation, folder)) != 1:
        raise OperatorCommandError("Name exactly one of --tool, --operation or --folder.")
    if folder:
        return folder_target(folder)
    if operation and "#" not in operation:
        raise OperatorCommandError("--operation takes tool#operation, such as cli:google#command:gmail.send.")
    return parse_target_text(str(tool or operation))


def parse_duration(text: str) -> timedelta:
    match = _DURATION.match(str(text).strip())
    if not match:
        raise OperatorCommandError(f"--for takes a duration such as 30m, 12h or 7d, not {text!r}.")
    return timedelta(**{_DURATION_UNITS[match.group(2)]: int(match.group(1))})


@dataclass
class GrantRequest:
    to: str
    tool: str | None
    tool_set: str | None
    level: str
    folder: str | None = None
    duration: str | None = None
    delegate_depth: int = 0
    delegate_mode: str = DelegationMode.AUTOMATIC.value
    reason: str = ""
    # Agents' access serves the giver's own work unless an administrator gives
    # it for everyone's work (decision D13).
    everyone: bool = False


def build_grant_spec(request: GrantRequest, *, team_id: str, by: str, now: datetime) -> GrantV1:
    try:
        level = AccessLevel(request.level.strip().lower())
        mode = DelegationMode(request.delegate_mode)
    except ValueError as exc:
        raise OperatorCommandError(str(exc)) from exc
    lifetime, expires = GrantLifetime.UNTIL_REVOKED, None
    if request.duration:
        lifetime, expires = GrantLifetime.UNTIL, now + parse_duration(request.duration)
    subject = parse_subject(request.to, team_id)
    for_person = None if request.everyone or subject.kind is SubjectKind.USER else by
    return GrantV1(
        grant_id="new",
        team_id=team_id,
        kind=GrantKind.ACCESS,
        subject=subject,
        target=parse_target(tool=request.tool, tool_set=request.tool_set, folder=request.folder),
        level=level,
        lifetime=lifetime,
        expires_at_utc=expires,
        delegation_depth=request.delegate_depth,
        delegation_mode=mode,
        granted_by=SubjectRefV1(kind=SubjectKind.USER, id=by),
        reason=request.reason,
        created_at_utc=now,
        for_person_id=for_person,
    )


def person_for(lookup: MembershipLookup, person_id: str, team_id: str) -> PersonPrincipal:
    roles = lookup.roles_of(person_id, team_id)
    if roles is None:
        raise OperatorCommandError(f"{person_id} is not an active member of team {team_id}.")
    return PersonPrincipal(person_id, team_id, roles)


def describe_grant(grant: GrantV1) -> str:
    if grant.kind is GrantKind.BLOCK:
        until = (
            f"until {grant.expires_at_utc.isoformat(timespec='minutes')}" if grant.expires_at_utc else "until removed"
        )
        return (
            f"{grant.grant_id} [{grant.status.value}] block: {grant.subject.kind.value} {grant.subject.id} -> "
            f"blocked from {grant.target.kind.value} {grant.target.id} ({until}); "
            f"blocked by {grant.granted_by.kind.value} {grant.granted_by.id}: {grant.reason or 'no reason given'}"
        )
    until = (
        f"until {grant.expires_at_utc.isoformat(timespec='minutes')}"
        if grant.expires_at_utc
        else grant.lifetime.value.replace("_", " ")
    )
    delegation = (
        f"; may hand on {grant.delegation_depth} step(s), {grant.delegation_mode.value.replace('_', ' ')}"
        if grant.delegation_depth
        else ""
    )
    parent = f"; handed down from {grant.parent_grant_id}" if grant.parent_grant_id else ""
    work = f"; for {grant.for_person_id}'s work" if grant.for_person_id else ""
    if grant.for_person_id is None and grant.subject.kind is not SubjectKind.USER:
        work = "; for everyone's work"
    return (
        f"{grant.grant_id} [{grant.status.value}] {grant.kind.value}: {grant.subject.kind.value} {grant.subject.id} -> "
        f"{grant.level.value} on {grant.target.kind.value} {grant.target.id} ({until}{delegation}{parent}{work}); "
        f"given by {grant.granted_by.kind.value} {grant.granted_by.id}"
    )


def describe_link(link: AccountLinkV1) -> str:
    owner = "the team" if link.owner_kind is LinkOwnerKind.TEAM else f"person {link.owner_id}"
    places = ", ".join(f"{item.placement.value} {item.name} <- {item.field}" for item in link.injections) or "fixed field"
    return (
        f"{link.link_id} [{link.status.value}] {link.tool_id} uses {owner}'s credential "
        f"{link.credential_key} ({places}); linked by {link.created_by.id}"
    )


def unlink(store: AccessRepository, person: PersonPrincipal, link_id: str, *, now: datetime) -> AccountLinkV1:
    """The person whose account it is, or a team administrator, removes a link."""

    link = store.get_link(link_id)
    if link is None or link.team_id != person.team_id:
        raise OperatorCommandError("There is no such account link in this team.")
    own = link.owner_kind is LinkOwnerKind.PERSON and link.owner_id == person.person_id
    if not (own or is_administrator(person.roles)):
        raise OperatorCommandError("Only the person whose account it is, or a team administrator, can remove it.")
    return store.revoke_link(link_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id), at=now)


def list_grants(store: AccessRepository, team_id: str, *, include_ended: bool = False) -> list[GrantV1]:
    ended = {GrantStatus.REVOKED, GrantStatus.DENIED}
    return [grant for grant in store.list_team(team_id) if include_ended or grant.status not in ended]


def subject_words(subject: SubjectRefV1) -> str:
    """Who a grant or block is for, in plain words."""

    name = subject.id.split("/", 1)[-1]
    if subject.kind is SubjectKind.AGENT_TYPE:
        return name
    if subject.kind is SubjectKind.AGENT:
        return f"agent {subject.id}"
    if subject.kind is SubjectKind.USER:
        return f"person {subject.id}"
    if subject.kind is SubjectKind.ROLE:
        return f"everyone with the {name} role"
    return "everyone in the team"


def _until_words(grant: GrantV1) -> str:
    if grant.lifetime is GrantLifetime.UNTIL and grant.expires_at_utc is not None:
        return f"until {grant.expires_at_utc.strftime('%Y-%m-%d %H:%M')} UTC"
    if grant.lifetime is GrantLifetime.TASK:
        return f"for task {grant.task_id}"
    if grant.lifetime is GrantLifetime.ONE_TIME:
        return f"for one use in task {grant.task_id}"
    return "until someone removes it"


def summarize_grant(requests: Any, grant: GrantV1) -> str:
    """One grant or block in plain words (``requests`` is an ``AccessRequests``)."""

    from access_control.requests import LEVEL_WORDS

    target = f"{requests.target_name(grant.target)} ({grant.target.id})"
    by = subject_words(grant.granted_by)
    state = "" if grant.status is GrantStatus.ACTIVE else f" [{grant.status.value}]"
    if grant.kind is GrantKind.BLOCK:
        return (
            f"{grant.grant_id}{state}: BLOCKED - {subject_words(grant.subject)} may not use {target}, "
            f"{_until_words(grant)}. Blocked by {by}. Reason: {grant.reason or 'none given'}."
        )
    hand_on = ""
    if grant.delegation_depth:
        mode = "without asking" if grant.delegation_mode is DelegationMode.AUTOMATIC else "asking each time"
        hand_on = f" May hand it on ({mode})."
    parent = f" Handed down from {grant.parent_grant_id}." if grant.parent_grant_id else ""
    return (
        f"{grant.grant_id}{state}: {subject_words(grant.subject)} has {LEVEL_WORDS[grant.level]} access to "
        f"{target}, {_until_words(grant)}. Given by {by}.{hand_on}{parent}"
    )


def summary_lines(requests: Any, grants: list[GrantV1]) -> list[str]:
    """One line per entry (``access_control.grant_entries``) and per block or waiting hand-down.

    An entry's line is its longest-lasting grant; step-ups beside it (a higher
    level for one task) are added to the same line, so one tool never shows
    as several independent grants.
    """

    from access_control.requests import LEVEL_WORDS

    entries: dict[Any, list[GrantV1]] = {}
    lines: list[str] = []
    for grant in grants:
        if grant.kind is GrantKind.ACCESS and grant.status is GrantStatus.ACTIVE:
            entries.setdefault(entry_key(grant), []).append(grant)
        else:
            lines.append(summarize_grant(requests, grant))
    for parts in entries.values():
        parts = sorted(parts, key=lasting_order, reverse=True)
        also = "; ".join(
            f"{grant.grant_id}: {LEVEL_WORDS[grant.level]} {_until_words(grant)}" for grant in parts[1:]
        )
        lines.append(summarize_grant(requests, parts[0]) + (f" Also {also}." if also else ""))
    return lines


def grant_matches(grant: GrantV1, text: str | None) -> bool:
    """Whether a grant is about an agent type, agent, person, tool or tool set named ``text``."""

    wanted = str(text or "").strip().lower()
    if not wanted:
        return True
    names = {
        grant.subject.id.lower(),
        grant.subject.id.split("/", 1)[-1].lower(),
        grant.target.id.lower(),
        grant.target.id.split("#", 1)[0].lower(),
    }
    if grant.granted_by.kind is SubjectKind.USER:
        names.add(grant.granted_by.id.lower())
    return wanted in names


def visible_to(person: PersonPrincipal, grant: GrantV1) -> bool:
    """Administrators see every grant; members see agents' access and what concerns themselves.

    People are separated by default (decision D6), so a member does not see
    access given to other people.
    """

    if is_administrator(person.roles):
        return True
    me = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
    return grant.subject.kind is not SubjectKind.USER or grant.subject == me or grant.granted_by == me


def describe_request(requests: Any, request: Any) -> str:
    """One line for an access request: who asks for what, for whom, and why."""

    from access_control.requests import LEVEL_WORDS

    outcome = request.status.value + (f" ({request.decision.value})" if request.decision else "")
    if request.change is not None:
        effect = "; ".join(request.change.effect)
        return f"{request.request_id} {outcome}: {request.requester_name} asks to make a change: {effect}"
    words = LEVEL_WORDS[request.level]
    recipient = requests.subject_name(request.subject)
    asks = (
        f"asks for {words} access"
        if recipient == request.requester_name
        else f"asks to give {recipient} {words} access"
    )
    person = f" for {request.person_id}" if request.person_id else ""
    lacks = " [the person lacks it too]" if request.person_lacks_access else ""
    return (
        f"{request.request_id} {outcome}: {request.requester_name} {asks} to "
        f"{requests.target_name(request.target)} ({request.lifetime.value.replace('_', ' ')}){person}{lacks} "
        f"- {request.reason}"
    )


def open_access_service(
    *,
    instance_root: Any,
    team_id: str,
    storage_settings: StorageSettings,
    cedar_binary: str | None,
    lead_agent_type: str = "TeamArchitect",
) -> AccessService:
    return AccessService(
        store=AccessRepository(storage_settings),
        catalog=load_catalog(instance_root),
        cedar=CedarAccessClient(binary=cedar_binary),
        team_id=team_id,
        clock=lambda: datetime.now(timezone.utc),
        agent_role_of=agent_role_resolver(lead_agent_type),
        lead_agent_type=lead_agent_type,
    )


__all__ = [
    "GrantRequest",
    "MembershipLookup",
    "OperatorCommandError",
    "block_subject",
    "block_target",
    "build_grant_spec",
    "describe_grant",
    "describe_link",
    "describe_request",
    "grant_matches",
    "list_grants",
    "open_access_service",
    "parse_duration",
    "parse_subject",
    "parse_target",
    "parse_target_text",
    "person_for",
    "subject_words",
    "summarize_grant",
    "unlink",
    "visible_to",
]
