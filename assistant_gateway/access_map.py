"""The web chat's Access page: the access map (access plan section 9.2).

For the signed-in person it shows three lists. Each is read again from the
access store at every call and paged on the server (``ACCESS_MAP_PAGE_SIZE``):

- **By agent type:** what each agent type (and any single agent, role or the
  whole team) holds now, where each grant came from as a readable chain
  ("You → TeamArchitect → WebResearchAgent", built from
  ``AccessService.chain``), until when, whether it can be handed on, and what
  is blocked. Standard access comes with an agent's role
  (``access_control.standard_grants``); it is listed apart and cannot be
  removed, only blocked. The team lead's type is always listed.
- **By person:** what each person has given and to whom. Members see only
  their own; owners and administrators see everyone (the privacy rule of
  ``/access list``, ``operator_commands.visible_to``). Access given *to* people
  is not shown here.
- **By tool:** every catalog tool, the level each operation needs, and its
  account links. A link shows whose account it is and who linked it, never the
  credential key or where the credential is sent.
- **Files:** what the person shared from their folder and what others shared
  with them (decision D15), and the team's data folders (decision D16), which
  owners and administrators add and remove here.

Agents' access serves one person's work (decision D13). Each grant says whose
("your work", "<name>'s work", "everyone's work"); members see what serves
their own work and what serves everyone's, never what another person
authorized. Owners and administrators see all of it.

Actions use the access model's own functions as the signed-in person:
removing access is ``AccessService.revoke_by_person`` (through the Inbox's
route, which notices use too), blocks are ``AccessRequests.block`` and
``unblock``. The flags this module adds (``can_revoke``, ``can_unblock``,
``block_tools``) only choose which buttons to show; every action is decided
again by the access service when it runs, and a refusal comes back in plain
words.

By agent type also carries two controls for the person's own work:
TeamLead's starter profile, and the switch that lets TeamLead clean up the
person's own chat history without asking each time: full access to
``fn:history_cleanup`` for their work, never handed on
(``custom_agents.history_cleanup``). The switch gives and removes that one
grant with ``AccessService.grant_by_person`` and ``revoke_by_person``; the
grant is also listed with TeamLead's other access and removed the same way.

Roles come from current team memberships at every call, never from the
browser. With authorization off there is nothing to show (trusted
single-person mode, decision D8), and no access service is opened.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence, TypeVar

from access_control.account_sharing import TeamAccountSharing
from access_control.data_folders import DataFolderRegistry
from access_control.decisions import HandDownRefused
from access_control.grant_entries import EntryKey, entry_key, lasting_order
from access_control.entities import (
    AGENT_ROLE_SYSTEM,
    AGENT_ROLE_TEAM_LEAD,
    AGENT_ROLE_WORKER,
    PersonPrincipal,
    agent_type_id,
    role_id,
)
from access_control.operator_commands import list_grants, parse_subject, parse_target_text
from access_control.people import is_administrator
from access_control.requests import LEVEL_WORDS, AccessRequests
from access_control.standard_grants import is_standard_grant, standard_grants
from access_control.starter_profiles import applied_profile, apply_starter_profile, starter_profiles
from assistant_gateway.application.access_map_ports import (
    ACCESS_MAP_PAGE_SIZE,
    ACCESS_MAP_VIEW_AGENTS,
    ACCESS_MAP_VIEW_FILES,
    ACCESS_MAP_VIEW_PEOPLE,
    ACCESS_MAP_VIEWS,
    AccessBlockInput,
)
from assistant_gateway.file_sharing import GatewayFileSharing, share_view
from custom_agents.history_cleanup import HISTORY_CLEANUP_TOOL_ID
from secure_tools.auth_methods import tool_sign_in
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccountLinkV1,
    AccountMode,
    ClassificationSource,
    DelegationMode,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    ToolDefinitionV1,
    ToolKind,
)

YOU = "You"
# The Settings switch for TeamLead's permission to clean up the person's history.
HISTORY_CLEANUP_SWITCH_TEXT = (
    "When you ask TeamLead to delete your old chats or finished tasks, it does so without asking you to approve "
    "each clean-up, and you get a notice of what was deleted. Off: you approve each clean-up in the Inbox. Only "
    "you can turn this on, and TeamLead never passes it on. Remembered facts are kept; you manage them under "
    "Memories."
)
HISTORY_CLEANUP_GRANT_REASON = "History clean-up without asking, turned on in Settings."
ACCESS_OFF_TEXT = (
    "Access control is off on this instance (trusted single-person mode): agents use every tool "
    "for the person they work for without asking, so there is no access to show, remove or block. "
    "Turning on authorization.enabled in instance_config.yaml turns it on."
)
EVERYONE_ALSO_NOTE = (
    "An administrator also gave this agent access to this for everyone's work. Removing it here does not remove that."
)
NOT_A_MEMBER_TEXT = "You are not an active member of this team, so there is no access to show or change."
ROLE_WORDS = {
    AGENT_ROLE_TEAM_LEAD: "Team lead",
    AGENT_ROLE_WORKER: "Worker",
    AGENT_ROLE_SYSTEM: "Built-in system agent",
}
# The Inbox's lifetime words talk about "this task"; on the map there is no
# current task, so the same lifetimes are said without it.
LIFETIME_WORDS = {
    GrantLifetime.ONE_TIME: "one use, in one task",
    GrantLifetime.TASK: "while one task runs",
    GrantLifetime.UNTIL: "until a set time",
    GrantLifetime.UNTIL_REVOKED: "until someone removes it",
}
TARGET_KIND_WORDS = {
    TargetKind.TOOL: "tool",
    TargetKind.OPERATION: "one operation",
    TargetKind.TOOL_SET: "tool set",
    TargetKind.CREDENTIAL: "credential",
    TargetKind.FOLDER: "folder",
}
TOOL_KIND_WORDS = {
    ToolKind.FN: "Built-in tool",
    ToolKind.CLI: "Command-line tool",
    ToolKind.HTTP: "Web API",
    ToolKind.MCP: "MCP server",
    ToolKind.PROCEDURE: "Installed procedure",
    ToolKind.CODE: "Code execution",
}
ACCOUNT_WORDS = {
    AccountMode.NONE: "Needs no account",
    AccountMode.PERSONAL_LOGIN: "Uses the sign-in of the person the task works for",
    AccountMode.LINKED_CREDENTIAL: "Uses a linked account",
}


def account_words(tool: ToolDefinitionV1, sharing: TeamAccountSharing) -> str:
    """Which account the tool uses for this team (``access_control.account_sharing``), naming the command that sets it up."""

    sign_in = tool_sign_in(tool.tool_id)
    team_used = sharing.team_account_used(tool)
    private_note = " (the team keeps it private)" if tool.sharing_choice and not team_used else ""
    if sign_in is not None and team_used:
        return (
            f"Uses the sign-in of the person the task works for ({sign_in.command}), "
            f"else the team's linked account ({sign_in.command} team), which the team shares"
        )
    if sign_in is not None:
        # A sign-in tool uses the person's sign-in whatever its account mode (X keeps a team link).
        return f"{ACCOUNT_WORDS[AccountMode.PERSONAL_LOGIN]} ({sign_in.command}){private_note}"
    if tool.account is not AccountMode.LINKED_CREDENTIAL:
        return ACCOUNT_WORDS[tool.account]
    if not team_used:
        return f"Uses only the linked account of the person the task works for{private_note}"
    if "person" not in tool.link_owners:
        return "Uses the team's linked account"
    return "Uses the linked account of the person the task works for, else the team's, which the team shares"


# The Block form's subject kinds, as operator_commands.parse_subject spells them.
BLOCK_SUBJECT_PREFIXES = {
    SubjectKind.AGENT_TYPE.value: "agent-type",
    SubjectKind.AGENT.value: "agent",
    SubjectKind.USER.value: "user",
    SubjectKind.TEAM.value: "team",
}
# Tool sets administrators can block in the form (internet access off, D14).
BLOCKABLE_TOOL_SETS = ("internet",)
# Agent-side holders the page offers the Block form for (people are blocked
# with /access block or the command line).
BLOCKABLE_HOLDERS = frozenset({SubjectKind.AGENT_TYPE, SubjectKind.AGENT})
_HOLDER_ORDER = {
    SubjectKind.AGENT_TYPE: 1,
    SubjectKind.SCHEDULE: 2,
    SubjectKind.AGENT: 3,
    SubjectKind.ROLE: 4,
    SubjectKind.TEAM: 5,
}

_Item = TypeVar("_Item")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _page(items: Sequence[_Item], offset: int) -> tuple[Sequence[_Item], dict[str, Any]]:
    """One server-bounded page; an offset past the end shows the last page."""

    total = len(items)
    start = max(0, min(int(offset or 0), max(total - 1, 0)))
    start -= start % ACCESS_MAP_PAGE_SIZE
    return items[start : start + ACCESS_MAP_PAGE_SIZE], {
        "total": total,
        "offset": start,
        "page_size": ACCESS_MAP_PAGE_SIZE,
        "has_more": start + ACCESS_MAP_PAGE_SIZE < total,
    }


def _empty_page(view: str, *, enabled: bool, notice: str) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "view": view,
        "notice": notice,
        "items": [],
        "total": 0,
        "offset": 0,
        "page_size": ACCESS_MAP_PAGE_SIZE,
        "has_more": False,
    }


def _lifetime(grant: GrantV1) -> dict[str, Any]:
    return {
        "lifetime": grant.lifetime.value,
        "lifetime_words": LIFETIME_WORDS[grant.lifetime],
        "expires_at_utc": _iso(grant.expires_at_utc),
        "task_id": grant.task_id,
    }


@dataclass
class _Holder:
    """An agent type, agent, role or the team, with what it holds."""

    subject: SubjectRefV1
    grants: list[tuple[GrantV1, list[str]]] = field(default_factory=list)
    blocks: list[GrantV1] = field(default_factory=list)


class _MapRead:
    """One read or change of the map for one person, with its lookups cached."""

    def __init__(
        self,
        requests: AccessRequests,
        person: PersonPrincipal,
        display_name: Callable[[str], str],
        schedule_name: Callable[[str], str] = lambda _schedule_id: "",
    ) -> None:
        self.requests = requests
        self._schedule_name = schedule_name
        self.service = requests.service
        self.person = person
        self.me = SubjectRefV1(kind=SubjectKind.USER, id=person.person_id)
        self.now = self.service.clock()
        self._display_name = display_name
        self._names: dict[str, str] = {}
        self._may_give: dict[tuple[str, str, str, str | None, str | None], bool] = {}
        self._sharing: TeamAccountSharing | None = None

    @property
    def sharing(self) -> TeamAccountSharing:
        """The team's sharing choices, read once for this page (``access_control.account_sharing``)."""

        if self._sharing is None:
            self._sharing = TeamAccountSharing(self.service.store, self.service.team_id).snapshot()
        return self._sharing

    # -- names ------------------------------------------------------------------

    def person_name(self, person_id: str) -> str:
        if person_id == self.person.person_id:
            return YOU
        if person_id not in self._names:
            self._names[person_id] = self._display_name(person_id)
        return self._names[person_id]

    def who(self, subject: SubjectRefV1) -> str:
        """A giver, a recipient or one link of a chain, in plain words."""

        if subject.kind is SubjectKind.USER:
            return self.person_name(subject.id)
        if subject.kind is SubjectKind.AGENT:
            return f"agent {subject.id}"
        if subject.kind is SubjectKind.ROLE:
            return f"everyone with the {self.requests.subject_name(subject)} role"
        if subject.kind is SubjectKind.TEAM:
            return "the team"
        if subject.kind is SubjectKind.SCHEDULE:
            return f"scheduled job {self.schedule_title(subject)}"
        return self.requests.subject_name(subject)

    def schedule_title(self, subject: SubjectRefV1) -> str:
        """A schedule's readable name, or its id when it has none (or was deleted)."""

        schedule_id = subject.id.split("/", 1)[-1]
        return self._schedule_name(schedule_id) or schedule_id

    def inert(self, grant: GrantV1) -> bool:
        """Lasting access for a person's work held by a worker: it no longer counts ("access follows the
        work", ``AccessService.agent_grants``), so the page does not list it as access."""

        return grant.kind is GrantKind.ACCESS and self.service.worker_lasting_refusal(grant) is not None

    def target(self, target: TargetRefV1) -> dict[str, str]:
        return {
            "target": target.id,
            "target_kind": target.kind.value,
            "target_kind_words": TARGET_KIND_WORDS[target.kind],
            "target_name": self.requests.target_name(target),
        }

    def hand_on_words(self, grant: GrantV1) -> str:
        if grant.kind is not GrantKind.ACCESS or not grant.delegation_depth:
            return "Cannot hand it on."
        how = (
            "without asking"
            if grant.delegation_mode is DelegationMode.AUTOMATIC
            else "only after a person approves each time"
        )
        further = "they cannot hand it on further" if grant.delegation_depth == 1 else "they can hand it on once more"
        only = f" Only to {', '.join(self.who(subject) for subject in grant.delegate_to)}." if grant.delegate_to else ""
        return f"Can hand it on to other agents {how}; {further}.{only}"

    # -- what still applies (presentation only) ------------------------------------

    def in_force(self, grant: GrantV1, *, leaf: bool = False) -> bool:
        """Whether a grant or block can still apply now.

        Only hides records that ended (expired, used up, revoked); the access
        service decides every call itself (``AccessService._valid``). A pending
        hand-down is shown as waiting for approval.
        """

        statuses = {GrantStatus.ACTIVE, GrantStatus.PENDING} if leaf else {GrantStatus.ACTIVE}
        if grant.status not in statuses:
            return False
        if grant.expires_at_utc is not None and self.now >= grant.expires_at_utc:
            return False
        return grant.uses_remaining is None or grant.uses_remaining > 0

    def chain(self, grant: GrantV1) -> list[str] | None:
        """Where the grant came from, root first; None when a link of it ended."""

        links = self.service.chain(grant)
        if links[0].parent_grant_id is not None:  # the grant it came from is gone
            return None
        if not self.in_force(grant, leaf=True) or not all(self.in_force(link) for link in links[:-1]):
            return None
        return [self.who(links[0].granted_by), *(self.who(link.subject) for link in links)]

    # -- which buttons to show (each action is decided again when it runs) -----------

    def may_give(
        self, target: TargetRefV1, level: AccessLevel, task_id: str | None, for_person_id: str | None = None
    ) -> bool:
        key = (target.kind.value, target.id, level.value, task_id, for_person_id)
        if key not in self._may_give:
            try:
                self._may_give[key] = self.service.may_give(
                    self.person, target, level, task_id=task_id, for_person_id=for_person_id
                )
            except HandDownRefused:  # the target left the catalog
                self._may_give[key] = False
        return self._may_give[key]

    def can_revoke(self, grant: GrantV1) -> bool:
        """``AccessService.may_revoke``, reusing this page's cached answers; pending hand-downs are decided in the Inbox."""

        if grant.status is not GrantStatus.ACTIVE or is_standard_grant(grant.grant_id):
            return False
        return self.service.may_revoke(self.person, grant, could_give=self.may_give)

    def can_unblock(self, block: GrantV1) -> bool:
        return AccessRequests.may_unblock(self.person, block)

    # -- records ------------------------------------------------------------------

    def work_words(self, grant: GrantV1) -> str:
        if grant.for_person_id is None:
            return "everyone's work"
        if grant.for_person_id == self.person.person_id:
            return "your work"
        return f"{self.person_name(grant.for_person_id)}'s work"

    def sees_grant(self, grant: GrantV1) -> bool:
        """Members see agents' access for their own work and everyone's; administrators all of it (D13)."""

        return (
            grant.for_person_id in {None, self.person.person_id}
            or grant.subject.kind is SubjectKind.USER
            or is_administrator(self.person.roles)
        )

    def grant_view(self, grant: GrantV1, chain: list[str] | None = None) -> dict[str, Any]:
        return {
            "grant_id": grant.grant_id,
            "for_person_id": grant.for_person_id,
            "for_whose_work": self.work_words(grant),
            "for_your_work": grant.for_person_id == self.person.person_id,
            "status": grant.status.value,
            **self.target(grant.target),
            "level": grant.level.value,
            "level_words": LEVEL_WORDS[grant.level],
            **_lifetime(grant),
            "hand_on_words": self.hand_on_words(grant),
            "chain": chain or [],
            "given_by": self.who(grant.granted_by),
            "reason": grant.reason,
            "created_at_utc": grant.created_at_utc.isoformat(),
            "can_revoke": self.can_revoke(grant),
        }

    # -- one entry per tool (access_control.grant_entries) ----------------------------

    def part_words(self, grant: GrantV1, main: GrantV1 | None = None) -> str:
        """How much one grant of an entry gives: "full", "read for gmail_send only", "full while one task runs"."""

        words = LEVEL_WORDS[grant.level]
        if grant.target.kind is TargetKind.OPERATION:
            words += f" for {grant.target.id.partition('#')[2]} only"
        if main is not None and (grant.lifetime, grant.expires_at_utc, grant.task_id) != (
            main.lifetime,
            main.expires_at_utc,
            main.task_id,
        ):
            words += f" {LIFETIME_WORDS[grant.lifetime]}"
        return words

    def entry_rows(self, grants: Sequence[tuple[GrantV1, list[str] | None]]) -> list[dict[str, Any]]:
        """One row per entry: everything a holder has on one tool for one person's work.

        The longest-lasting grant is the row's main grant (its "How long",
        "Came from" and "Why"); the others are step-ups, said in "Access".
        Remove takes the whole entry (``AccessService.revoke_by_person``).
        Hand-downs waiting for approval are not access yet and keep their own
        rows.
        """

        entries: dict[EntryKey, list[tuple[GrantV1, list[str] | None]]] = {}
        rows: list[dict[str, Any]] = []
        for grant, chain in grants:
            if grant.kind is not GrantKind.ACCESS or grant.status is not GrantStatus.ACTIVE:
                rows.append(self.grant_view(grant, chain))
                continue
            entries.setdefault(entry_key(grant), []).append((grant, chain))
        for key, parts in entries.items():
            parts = sorted(parts, key=lambda item: lasting_order(item[0]), reverse=True)
            main, chain = parts[0]
            row = self.grant_view(main, chain)
            row.update(self.target(TargetRefV1(kind=key.target_kind, id=key.target_id)))
            row["level_words"] = ", and ".join(
                [self.part_words(main), *(self.part_words(grant, main) for grant, _ in parts[1:])]
            )
            row["hand_on_words"] = self.hand_on_words(max((grant for grant, _ in parts), key=lambda g: g.delegation_depth))
            row["grant_ids"] = [grant.grant_id for grant, _ in parts]
            row["can_revoke"] = all(self.can_revoke(grant) for grant, _ in parts)
            rows.append(row)
        return rows

    def block_view(self, block: GrantV1) -> dict[str, Any]:
        return {
            "block_id": block.grant_id,
            **self.target(block.target),
            **_lifetime(block),
            "blocked_by": self.who(block.granted_by),
            "reason": block.reason,
            "created_at_utc": block.created_at_utc.isoformat(),
            "can_unblock": self.can_unblock(block),
        }

    def standard_rows(self, agent_type: str) -> tuple[str, list[dict[str, Any]]]:
        """The agent type's role and the standard grants that role holds."""

        role = self.service.agent_role_of(agent_type)
        holder = SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(self.service.team_id, role))
        rows = [
            {**self.target(grant.target), "level": grant.level.value, "level_words": LEVEL_WORDS[grant.level]}
            for grant in standard_grants(self.service.team_id)
            if grant.subject == holder
        ]
        return role, rows

    def link_visible(self, link: AccountLinkV1) -> bool:
        """Team accounts are shown to everyone; a person's own account to them and to administrators."""

        return (
            link.owner_kind is LinkOwnerKind.TEAM
            or link.owner_id == self.person.person_id
            or is_administrator(self.person.roles)
        )

    def link_view(self, link: AccountLinkV1, *, team_unused_reason: str | None) -> dict[str, Any]:
        """One linked account; ``team_unused_reason`` says why the team's account is not used, None when it is."""

        if link.owner_kind is LinkOwnerKind.TEAM:
            owner = "The team's account" + (f" (not used: {team_unused_reason})" if team_unused_reason else "")
        elif link.owner_id == self.person.person_id:
            owner = "Your account"
        else:
            owner = f"{self.person_name(link.owner_id)}'s account"
        return {
            "link_id": link.link_id,
            "owner_kind": link.owner_kind.value,
            "owner_words": owner,
            "linked_by": self.who(link.created_by),
            "created_at_utc": link.created_at_utc.isoformat(),
        }


def _operations_words(tool: ToolDefinitionV1) -> str:
    if not tool.operations:
        return "No operations are listed yet."
    counts = {level: sum(1 for op in tool.operations if op.level is level) for level in AccessLevel}
    parts = [f"{count} need {LEVEL_WORDS[level]}" for level, count in counts.items() if count]
    noun = "operation" if len(tool.operations) == 1 else "operations"
    return f"{len(tool.operations)} {noun}: {', '.join(parts)}."


class GatewayAccessMap:
    """The Access page's reads and changes, as the signed-in person."""

    def __init__(
        self,
        *,
        requests: Callable[[], AccessRequests],
        person: Callable[[str], PersonPrincipal | None],
        display_name: Callable[[str], str],
        lead_agent_type: str,
        authorization_enabled: bool,
        file_sharing: GatewayFileSharing | None = None,
        data_folders: Callable[[], DataFolderRegistry] | None = None,
        protected_locations: tuple[Any, ...] = (),
        schedule_name: Callable[[str], str] = lambda _schedule_id: "",
    ) -> None:
        self._requests = requests
        # A scheduled job's readable name for its card (access follows the work).
        self._schedule_name = schedule_name
        self._person = person
        self._display_name = display_name
        self._lead_agent_type = lead_agent_type
        self._enabled = authorization_enabled
        self._sharing = file_sharing
        self._data_folders = data_folders
        self._protected = tuple(protected_locations)

    def _open(self, person_id: str) -> _MapRead:
        """The person's map, or a plain refusal when there is none to change."""

        if not self._enabled:
            raise HandDownRefused(ACCESS_OFF_TEXT)
        person = self._person(person_id)
        if person is None:
            raise HandDownRefused(NOT_A_MEMBER_TEXT)
        return _MapRead(self._requests(), person, self._display_name, self._schedule_name)

    # -- reading -------------------------------------------------------------------

    def _view(self, person_id: str, view: str, offset: int, agent: str) -> dict[str, Any]:
        if view not in ACCESS_MAP_VIEWS:
            raise ValueError(f"Choose one of the views: {', '.join(ACCESS_MAP_VIEWS)}.")
        if view == ACCESS_MAP_VIEW_FILES:
            # Data folders exist in both authorization modes.
            return {"view": view, **self._files(person_id)}
        if not self._enabled:
            return _empty_page(view, enabled=False, notice=ACCESS_OFF_TEXT)
        person = self._person(person_id)
        if person is None:
            return _empty_page(view, enabled=True, notice=NOT_A_MEMBER_TEXT)
        read = _MapRead(self._requests(), person, self._display_name, self._schedule_name)
        if view == ACCESS_MAP_VIEW_AGENTS:
            page = self._agents(read, offset, str(agent or "").strip())
        elif view == ACCESS_MAP_VIEW_PEOPLE:
            page = self._people(read, offset)
        else:
            page = self._tools(read, offset)
        return {"enabled": True, "view": view, "notice": "", **page}

    async def view(self, person_id: str, *, view: str, offset: int = 0, agent: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self._view, person_id, view, offset, agent)

    def _holders(self, read: _MapRead) -> list[_Holder]:
        lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(read.service.team_id, self._lead_agent_type))
        holders: dict[SubjectRefV1, _Holder] = {lead: _Holder(lead)}
        for grant in list_grants(read.service.store, read.service.team_id):
            if not self._agent_side(read, grant):
                continue
            if grant.kind is GrantKind.BLOCK:
                if read.in_force(grant):
                    holders.setdefault(grant.subject, _Holder(grant.subject)).blocks.append(grant)
                continue
            chain = read.chain(grant)
            if chain is not None:
                holders.setdefault(grant.subject, _Holder(grant.subject)).grants.append((grant, chain))

        def order(holder: _Holder) -> tuple[int, str]:
            rank = 0 if holder.subject == lead else _HOLDER_ORDER.get(holder.subject.kind, 5)
            return rank, read.requests.subject_name(holder.subject).lower()

        return sorted(holders.values(), key=order)

    @staticmethod
    def _agent_side(read: _MapRead, grant: GrantV1) -> bool:
        """Whether a grant belongs on the agents view for this person."""

        if grant.subject.kind is SubjectKind.USER:
            return False  # access given to people is private to them and administrators
        if not read.sees_grant(grant):
            return False  # what another person authorized for their own work (D13)
        # A worker's lasting access from before access followed the work never counts.
        return not read.inert(grant)

    @staticmethod
    def _holder_key(read: _MapRead, subject: SubjectRefV1) -> str:
        return f"{subject.kind.value}:{read.requests.subject_name(subject)}"

    @staticmethod
    def _holder_title(read: _MapRead, subject: SubjectRefV1) -> str:
        name = read.requests.subject_name(subject)
        if subject.kind is SubjectKind.AGENT:
            return f"Agent {name}"
        if subject.kind is SubjectKind.ROLE:
            return f"Everyone with the {name} role"
        if subject.kind is SubjectKind.TEAM:
            return "Everyone in the team"
        if subject.kind is SubjectKind.SCHEDULE:
            # A scheduled job holds its own access; every agent in its runs uses it.
            return f"Scheduled job {read.schedule_title(subject)}"
        return name

    def _block_tools(self, read: _MapRead) -> list[dict[str, str]]:
        """Tools to offer in the Block form.

        Only fills the form: ``AccessRequests.block`` asks the access service
        when the form is sent. Administrators can give (so block) every team
        tool; anyone else only what their delegable grants cover.
        """

        catalog = read.service.catalog
        tool_ids = set(catalog.tools) if is_administrator(read.person.roles) else self._delegable_tool_ids(read)
        tools = [catalog.tools[tool_id] for tool_id in tool_ids if tool_id in catalog.tools]
        sets = [
            {"tool_id": set_id, "display_name": f"{catalog.tool_sets[set_id].display_name} (all of it)"}
            for set_id in BLOCKABLE_TOOL_SETS
            if is_administrator(read.person.roles) and set_id in catalog.tool_sets
        ]
        return sets + [
            {"tool_id": tool.tool_id, "display_name": tool.display_name}
            for tool in sorted(tools, key=lambda tool: (tool.display_name.lower(), tool.tool_id))
        ]

    @staticmethod
    def _delegable_tool_ids(read: _MapRead) -> set[str]:
        """Tools a person who is not an administrator could give, so also block."""

        catalog = read.service.catalog
        tool_ids: set[str] = set()
        for grant in read.service.valid_grants(read.person.subjects(), task_id=None):
            if grant.kind is not GrantKind.ACCESS or grant.delegation_depth < 1:
                continue
            if grant.target.kind is TargetKind.TOOL_SET:
                tool_ids.update(tool.tool_id for tool in catalog.tools.values() if grant.target.id in tool.tool_sets)
            else:
                tool_ids.add(grant.target.id.partition("#")[0])
        return tool_ids

    def _holder_view(self, read: _MapRead, holder: _Holder, block_tools: list[dict[str, str]]) -> dict[str, Any]:
        subject = holder.subject
        name = read.requests.subject_name(subject)
        role, standard = read.standard_rows(name) if subject.kind is SubjectKind.AGENT_TYPE else ("", [])
        rows = sorted(
            read.entry_rows([(grant, chain) for grant, chain in holder.grants]),
            key=lambda row: (str(row["target_name"]).lower(), str(row["created_at_utc"])),
        )
        # Removing access for one person's work leaves access an administrator
        # gave for everyone's work on the same tool; say so on that row.
        for_everyone = {(row["target_kind"], row["target"]) for row in rows if row.get("for_person_id") is None}
        for row in rows:
            if row.get("for_person_id") is not None and (row["target_kind"], row["target"]) in for_everyone:
                row["entry_note"] = EVERYONE_ALSO_NOTE
        return {
            "key": self._holder_key(read, subject),
            "subject_kind": subject.kind.value,
            "subject": name,
            "title": self._holder_title(read, subject),
            "role_words": ROLE_WORDS.get(role, ""),
            "standard": standard,
            "grants": rows,
            "blocks": [read.block_view(block) for block in holder.blocks],
            "can_block": subject.kind in BLOCKABLE_HOLDERS and bool(block_tools),
        }

    def _agents(self, read: _MapRead, offset: int, agent: str) -> dict[str, Any]:
        holders = self._holders(read)
        focus_key, focus_missing = "", False
        if agent:
            wanted = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(read.service.team_id, agent))
            index = next((i for i, holder in enumerate(holders) if holder.subject == wanted), None)
            if index is None:
                focus_missing = True
            else:
                offset = index
                focus_key = self._holder_key(read, wanted)
        page, meta = _page(holders, offset)
        block_tools = self._block_tools(read)
        return {
            **meta,
            "items": [self._holder_view(read, holder, block_tools) for holder in page],
            "block_tools": block_tools,
            "starter_profile": self._profile_view(read),
            "history_cleanup": self._cleanup_view(read),
            "focus_key": focus_key,
            "focus_missing": focus_missing,
        }

    def _people(self, read: _MapRead, offset: int) -> dict[str, Any]:
        # The privacy rule of /access list: administrators see everyone.
        sees_everyone = is_administrator(read.person.roles)
        given: dict[str, list[GrantV1]] = {read.person.person_id: []}
        for grant in list_grants(read.service.store, read.service.team_id):
            if read.inert(grant):
                continue  # a worker's lasting access from before access followed the work
            giver = grant.granted_by
            if giver.kind is not SubjectKind.USER or not (sees_everyone or giver == read.me):
                continue
            current = read.in_force(grant) if grant.kind is GrantKind.BLOCK else read.chain(grant) is not None
            if current:
                given.setdefault(giver.id, []).append(grant)
        people = sorted(
            given,
            key=lambda person_id: (person_id != read.person.person_id, read.person_name(person_id).lower(), person_id),
        )
        page, meta = _page(people, offset)
        return {
            **meta,
            "sees_everyone": sees_everyone,
            "items": [self._person_view(read, person_id, given[person_id]) for person_id in page],
        }

    @staticmethod
    def _person_view(read: _MapRead, person_id: str, grants: list[GrantV1]) -> dict[str, Any]:
        blocks = [grant for grant in grants if grant.kind is GrantKind.BLOCK]
        access = read.entry_rows([(grant, None) for grant in grants if grant.kind is not GrantKind.BLOCK])
        subjects = {grant.grant_id: grant.subject for grant in grants}
        rows = [
            {**view, "kind": GrantKind.ACCESS.value, "recipient": read.who(subjects[view["grant_id"]])} for view in access
        ] + [{**read.block_view(block), "kind": block.kind.value, "recipient": read.who(block.subject)} for block in blocks]
        rows.sort(key=lambda row: str(row["created_at_utc"]), reverse=True)
        return {
            "person_id": person_id,
            "title": read.person_name(person_id),
            "is_you": person_id == read.person.person_id,
            "given": rows,
        }

    def _tools(self, read: _MapRead, offset: int) -> dict[str, Any]:
        catalog = read.service.catalog
        tools = sorted(catalog.tools.values(), key=lambda tool: (tool.display_name.lower(), tool.tool_id))
        page, meta = _page(tools, offset)
        shown = {tool.tool_id for tool in page}
        links: dict[str, list[AccountLinkV1]] = {}
        for link in read.service.store.list_links(read.service.team_id):
            if link.tool_id in shown and read.link_visible(link):
                links.setdefault(link.tool_id, []).append(link)

        def link_order(link: AccountLinkV1) -> tuple[bool, bool, str]:
            # The team's account, then the person's own, then others by name.
            mine = link.owner_kind is LinkOwnerKind.PERSON and link.owner_id == read.person.person_id
            return link.owner_kind is not LinkOwnerKind.TEAM, not mine, read.person_name(link.owner_id).lower()

        return {
            **meta,
            "items": [self._tool_view(read, tool, sorted(links.get(tool.tool_id, []), key=link_order)) for tool in page],
        }

    @staticmethod
    def _tool_view(read: _MapRead, tool: ToolDefinitionV1, links: list[AccountLinkV1]) -> dict[str, Any]:
        tool_sets = read.service.catalog.tool_sets
        sharing = read.sharing
        team_unused_reason = None
        if not sharing.team_account_used(tool):
            team_unused_reason = (
                "the team keeps this service private"
                if tool.sharing_choice
                else "this service uses only each person's own account"
            )
        return {
            "tool_id": tool.tool_id,
            "display_name": tool.display_name,
            "description": tool.description,
            "kind_words": TOOL_KIND_WORDS[tool.kind],
            "account_words": account_words(tool, sharing),
            "uses_accounts": tool.account is not AccountMode.NONE,
            "tool_sets": [tool_sets[item].display_name if item in tool_sets else item for item in tool.tool_sets],
            "operation_count": len(tool.operations),
            "operations_words": _operations_words(tool),
            "unreviewed_count": sum(1 for op in tool.operations if op.source is ClassificationSource.UNREVIEWED),
            "links": [read.link_view(link, team_unused_reason=team_unused_reason) for link in links],
        }

    def _tool_operations(self, person_id: str, tool_id: str, offset: int) -> dict[str, Any]:
        read = self._open(person_id)
        tool = read.service.catalog.tools.get(tool_id)
        if tool is None:
            raise HandDownRefused("There is no such tool in the tool catalog. Refresh the Access page.")
        operations = sorted(tool.operations, key=lambda op: op.name.lower())
        page, meta = _page(operations, offset)
        return {
            **meta,
            "tool_id": tool.tool_id,
            "items": [
                {
                    "name": op.name,
                    "level": op.level.value,
                    "level_words": LEVEL_WORDS[op.level],
                    "description": op.description,
                    "unreviewed": op.source is ClassificationSource.UNREVIEWED,
                }
                for op in page
            ],
        }

    async def tool_operations(self, person_id: str, tool_id: str, *, offset: int = 0) -> dict[str, Any]:
        return await asyncio.to_thread(self._tool_operations, person_id, tool_id, offset)

    # -- changing -------------------------------------------------------------------

    def _block(self, person_id: str, block: AccessBlockInput) -> dict[str, Any]:
        read = self._open(person_id)
        prefix = BLOCK_SUBJECT_PREFIXES.get(block.subject_kind)
        if prefix is None:
            raise HandDownRefused("Block an agent type, one agent, a person, or the whole team.")
        name, tool_id, operation = block.subject.strip(), block.tool_id.strip(), block.operation.strip()
        if not name and prefix != "team":
            raise ValueError("Choose who to block.")
        if not tool_id:
            raise ValueError("Choose the tool to block.")
        subject = parse_subject("team" if prefix == "team" else f"{prefix}:{name}", read.service.team_id)
        target = parse_target_text(f"{tool_id}#{operation}" if operation else tool_id)
        created = read.requests.block(read.person, subject=subject, target=target, reason=block.reason, hours=block.hours)
        return {
            "block": read.block_view(created),
            "message": (
                f"{read.who(subject)} is blocked from {read.requests.target_name(target)}. "
                "It applies from the next action."
            ),
        }

    async def block(self, person_id: str, block: AccessBlockInput) -> dict[str, Any]:
        return await asyncio.to_thread(self._block, person_id, block)

    def _unblock(self, person_id: str, block_id: str) -> dict[str, Any]:
        read = self._open(person_id)
        removed = read.requests.unblock(read.person, block_id)
        return {
            "block_id": removed.grant_id,
            "message": (
                f"The block on {read.who(removed.subject)} for {read.requests.target_name(removed.target)} is removed. "
                "The access it stopped applies again from the next action."
            ),
        }

    async def unblock(self, person_id: str, block_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._unblock, person_id, block_id)

    # -- TeamLead's standing access for the signed-in person's work ---------------------

    def _profile_view(self, read: _MapRead) -> dict[str, Any]:
        """Which starter profile serves this person's work, and the choices (decision D13)."""

        return {
            "current": applied_profile(read.service, read.person.person_id),
            "choices": [
                {"name": profile.name, "display_name": profile.display_name, "description": profile.description}
                for profile in starter_profiles().values()
            ],
        }

    def _set_profile(self, person_id: str, name: str) -> dict[str, Any]:
        read = self._open(person_id)
        apply_starter_profile(read.service, read.person, name, lead_agent_type=self._lead_agent_type)
        profile = starter_profiles()[name]
        return {
            "starter_profile": self._profile_view(read),
            "message": f"{profile.display_name} for your work: {profile.description}",
        }

    async def set_starter_profile(self, person_id: str, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._set_profile, person_id, str(name or "").strip())

    # -- TeamLead's permission to clean up the person's own history -------------------

    def _lead(self, read: _MapRead) -> SubjectRefV1:
        return SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(read.service.team_id, self._lead_agent_type))

    def _cleanup_grants(self, read: _MapRead) -> list[GrantV1]:
        """All the person's cleanup grants, including narrower ones the switch must upgrade or remove."""

        return [
            grant
            for grant in read.service.store.for_subjects(
                read.service.team_id, [self._lead(read)], statuses=(GrantStatus.ACTIVE,)
            )
            if grant.kind is GrantKind.ACCESS
            and grant.target.kind is TargetKind.TOOL
            and grant.target.id == HISTORY_CLEANUP_TOOL_ID
            and grant.for_person_id == read.person.person_id
            and read.in_force(grant)
        ]

    def _cleanup_view(self, read: _MapRead) -> dict[str, Any]:
        grants = [grant for grant in self._cleanup_grants(read) if grant.level is AccessLevel.FULL]
        return {
            "available": HISTORY_CLEANUP_TOOL_ID in read.service.catalog.tools,
            "on": bool(grants),
            "grant_id": grants[0].grant_id if grants else None,
            "description": HISTORY_CLEANUP_SWITCH_TEXT,
        }

    def _set_cleanup(self, person_id: str, on: bool) -> dict[str, Any]:
        read = self._open(person_id)
        grants = self._cleanup_grants(read)
        if on and not any(grant.level is AccessLevel.FULL for grant in grants):
            read.service.grant_by_person(
                read.person,
                GrantV1(
                    grant_id="new",
                    team_id=read.service.team_id,
                    subject=self._lead(read),
                    target=TargetRefV1(kind=TargetKind.TOOL, id=HISTORY_CLEANUP_TOOL_ID),
                    level=AccessLevel.FULL,
                    granted_by=read.me,
                    reason=HISTORY_CLEANUP_GRANT_REASON,
                    created_at_utc=read.service.clock(),
                    for_person_id=read.person.person_id,
                ),
            )
        if not on:
            for grant in grants:
                read.service.revoke_by_person(read.person, grant.grant_id)
        return {
            "history_cleanup": self._cleanup_view(read),
            "message": (
                "TeamLead may now clean up your old chats and finished tasks when you ask, without asking you to "
                "approve each clean-up. You get a notice each time."
                if on
                else "TeamLead now asks you to approve each clean-up of your history in the Inbox."
            ),
        }

    async def set_history_cleanup(self, person_id: str, on: bool) -> dict[str, Any]:
        return await asyncio.to_thread(self._set_cleanup, person_id, bool(on))

    # -- files: shares and data folders ------------------------------------------------

    def _files(self, person_id: str) -> dict[str, Any]:
        person = self._person(person_id)
        if person is None:
            return {"enabled": self._enabled, "notice": NOT_A_MEMBER_TEXT, "shared_by_you": [], "shared_with_you": [],
                    "data_folders": [], "can_manage_folders": False}
        shares: dict[str, list[GrantV1]] = {"by_me": [], "with_me": []}
        if self._enabled and self._sharing is not None:
            shares = self._sharing.shares(person_id)
        records = self._data_folders().records() if self._data_folders is not None else []
        return {
            "enabled": self._enabled,
            "notice": "",
            "shared_by_you": [share_view(grant, self._display_name) for grant in shares["by_me"]],
            "shared_with_you": [share_view(grant, self._display_name) for grant in shares["with_me"]],
            "data_folders": [
                {
                    "name": record.name,
                    "folder_id": record.folder_id,
                    "path": record.path,
                    "writable": record.writable,
                    "access_words": "read and change" if record.writable else "read only",
                    "registered_by": self._display_name(record.registered_by.id),
                    "created_at_utc": record.created_at_utc.isoformat(),
                }
                for record in records
            ],
            "can_manage_folders": is_administrator(person.roles),
        }

    def _stop_sharing(self, person_id: str, share_id: str) -> dict[str, Any]:
        if self._sharing is None:
            raise HandDownRefused("Sharing is not available here.")
        self._sharing.stop_sharing(person_id, share_id)
        return {"share_id": share_id, "message": "Stopped sharing. It applies from the next action."}

    async def stop_sharing(self, person_id: str, share_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._stop_sharing, person_id, share_id)

    def _registry(self, person_id: str) -> tuple[DataFolderRegistry, PersonPrincipal]:
        person = self._person(person_id)
        if person is None:
            raise HandDownRefused(NOT_A_MEMBER_TEXT)
        if self._data_folders is None:
            raise HandDownRefused("Data folders are not available here.")
        return self._data_folders(), person

    def _add_data_folder(self, person_id: str, name: str, path: str, writable: bool) -> dict[str, Any]:
        registry, person = self._registry(person_id)
        record = registry.register(
            person, name=name, path=path, writable=writable, reason="Added on the Access page",
            protected=self._protected, now=datetime.now(timezone.utc),
        )
        reach = "Agents reach it only with folder grants." if self._enabled else "Every agent can use it now."
        return {"name": record.name, "message": f"Registered {record.path} as data/{record.name}. {reach}"}

    async def add_data_folder(self, person_id: str, name: str, path: str, *, writable: bool) -> dict[str, Any]:
        return await asyncio.to_thread(self._add_data_folder, person_id, name, path, writable)

    def _remove_data_folder(self, person_id: str, name: str) -> dict[str, Any]:
        registry, person = self._registry(person_id)
        removed = registry.remove(person, name, now=datetime.now(timezone.utc))
        return {"name": removed.name, "message": f"Removed data/{removed.name}. Agents can no longer reach {removed.path}."}

    async def remove_data_folder(self, person_id: str, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._remove_data_folder, person_id, name)


__all__ = [
    "ACCESS_OFF_TEXT",
    "NOT_A_MEMBER_TEXT",
    "GatewayAccessMap",
]
