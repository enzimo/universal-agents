"""Whether the team's account for a service serves everyone's work (shared) or nobody's (private).

Owner's decisions (2026-10-08):

- An administrator chooses, per service, whether the account they set up
  serves the whole team or only their own work, and the same for the team's
  model keys. Onboarding asks first (the Secrets page); ``/accounts`` changes
  it later.
- Shared: the team's account (an account link owned by the team) serves the
  work of anyone without their own account; a person's own account still
  comes first. That was the only rule before this choice existed.
- Private: the team's account is never used for that service, not even for
  work that serves nobody; each person's work uses only their own account. A
  team account that is already linked is kept, unused, so sharing the
  service again needs no new link.
- Which services have a choice: tools that use a linked account and take
  both a person's and the team's (``ToolDefinitionV1.sharing_choice``). X
  takes the team's account as the fallback for people's own sign-ins, so it
  has one too. Tools without a team account are always private: GitHub and
  Google, which use only each person's own sign-in (the owner chose to keep
  them so), and tools that take only a person's account (the GitHub API,
  proposed CLIs). Tools that take only the team's account are always shared:
  the team's own Crawl4AI server, which only the team's token opens
  (``access_catalog.catalog`` requires it of every such server).
- Defaults: a tool's ``default_sharing``, shared when it names none.
  Services that act as a person (X) start private; services that supply a
  resource (search, downloads) start shared.
- Model keys (``MODEL_KEYS_SHARING_SUBJECT``, shared by default): whether
  people added from now on may use the team's model keys. It is the starting
  value of each new person's own switch (``access_control.person_settings``),
  which an administrator can still change per person; work that serves
  nobody always uses the team's keys.

A choice picks accounts; it never grants access. Who may change it is
decided in ``access_control.people.account_sharing_refusal``; this module only
reads and writes. The broker (``secure_tool_broker.access_gate`` and the
account link form), the agents' tool setup check
(``custom_tools.tool_setup_check``) and the gateway all read it through
``TeamAccountSharing``, so they agree on whose account a tool uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Protocol

from custom_messages.contracts.access_v2 import (
    MODEL_KEYS_SHARING_SUBJECT,
    AccountMode,
    AccountSharing,
    AccountSharingV1,
    LinkOwnerKind,
    ToolDefinitionV1,
)
from secure_tools.chat_commands import ACCOUNTS_SHARE, accounts_command, link_command


class AccountSharingStore(Protocol):
    def account_sharing(self, team_id: str, subject: str) -> AccountSharingV1 | None: ...

    def team_account_sharing(self, team_id: str) -> list[AccountSharingV1]: ...

    def set_account_sharing(self, record: AccountSharingV1) -> AccountSharingV1: ...


class SharingNotChosen(ValueError):
    """The service's sharing is fixed (or it uses no account), so the team cannot choose it."""


def uses_account(tool: ToolDefinitionV1) -> bool:
    """Whether the tool acts with an account at all (a sign-in or a linked credential)."""

    return tool.account is not AccountMode.NONE


def fixed_sharing(tool: ToolDefinitionV1) -> AccountSharing | None:
    """The sharing a tool always has, or None when the team chooses (module docstring)."""

    if tool.sharing_choice:
        return None
    if tool.account is AccountMode.LINKED_CREDENTIAL and LinkOwnerKind.TEAM in tool.link_owners:
        return AccountSharing.SHARED
    return AccountSharing.PRIVATE


def default_sharing(tool: ToolDefinitionV1) -> AccountSharing:
    """The sharing a tool has until an administrator chooses."""

    return fixed_sharing(tool) or tool.default_sharing or AccountSharing.SHARED


@dataclass(frozen=True)
class ServiceSharing:
    """One service's sharing as people see it (``/accounts``, onboarding).

    ``chosen`` is True when an administrator's record decides it, ``fixed``
    when nobody can change it.
    """

    subject: str
    display_name: str
    sharing: AccountSharing
    fixed: bool
    chosen: bool


class TeamAccountSharing:
    """One team's sharing choices (module docstring)."""

    def __init__(
        self,
        store: AccountSharingStore,
        team_id: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.team_id = team_id
        self._now = now

    def for_tool(self, tool: ToolDefinitionV1) -> AccountSharing:
        fixed = fixed_sharing(tool)
        if fixed is not None:
            return fixed
        record = self.store.account_sharing(self.team_id, tool.tool_id)
        return record.sharing if record is not None else default_sharing(tool)

    def link_owners(self, tool: ToolDefinitionV1) -> tuple[str, ...]:
        """Whose account link the broker may use for the tool, in order: the tool's owners, without the team when private."""

        if self.for_tool(tool) is AccountSharing.PRIVATE:
            return tuple(owner for owner in tool.link_owners if owner != LinkOwnerKind.TEAM)
        return tool.link_owners

    def team_account_used(self, tool: ToolDefinitionV1) -> bool:
        """Whether a team account link for the tool serves anyone's work."""

        return tool.account is AccountMode.LINKED_CREDENTIAL and LinkOwnerKind.TEAM in self.link_owners(tool)

    def model_keys(self) -> AccountSharing:
        record = self.store.account_sharing(self.team_id, MODEL_KEYS_SHARING_SUBJECT)
        return record.sharing if record is not None else AccountSharing.SHARED

    def new_people_use_team_model_keys(self) -> bool:
        return self.model_keys() is AccountSharing.SHARED

    def choose_tool(self, tool: ToolDefinitionV1, sharing: AccountSharing, *, by: str | None) -> AccountSharingV1:
        """Record the administrator's choice for a tool (the caller checked who may)."""

        if fixed_sharing(tool) is not None:
            raise SharingNotChosen(fixed_sharing_text(tool))
        return self._record(tool.tool_id, sharing, by)

    def choose_model_keys(self, sharing: AccountSharing, *, by: str | None) -> AccountSharingV1:
        """Record whether people added from now on may use the team's model keys (the caller checked who may)."""

        return self._record(MODEL_KEYS_SHARING_SUBJECT, sharing, by)

    def snapshot(self) -> "TeamAccountSharing":
        """The team's choices read once, for a page that asks about many tools (read-only)."""

        return TeamAccountSharing(_ReadOnce(self.store.team_account_sharing(self.team_id)), self.team_id, now=self._now)

    def overview(self, tools: Iterable[ToolDefinitionV1]) -> list[ServiceSharing]:
        """Every service that uses an account, by name, with its sharing."""

        recorded = {record.subject: record for record in self.store.team_account_sharing(self.team_id)}
        return _overview(tools, recorded)

    def _record(self, subject: str, sharing: AccountSharing, by: str | None) -> AccountSharingV1:
        return self.store.set_account_sharing(
            AccountSharingV1(
                team_id=self.team_id,
                subject=subject,
                sharing=sharing,
                changed_by_person_id=by,
                changed_at_utc=self._now(),
            )
        )


class _ReadOnce:
    """The team's sharing records as read once (``TeamAccountSharing.snapshot``)."""

    def __init__(self, records: Iterable[AccountSharingV1]) -> None:
        self._records = {record.subject: record for record in records}

    def account_sharing(self, team_id: str, subject: str) -> AccountSharingV1 | None:
        return self._records.get(subject)

    def team_account_sharing(self, team_id: str) -> list[AccountSharingV1]:
        return list(self._records.values())

    def set_account_sharing(self, record: AccountSharingV1) -> AccountSharingV1:
        raise RuntimeError("A snapshot of the team's sharing choices is read-only; change them through the store.")


def _overview(tools: Iterable[ToolDefinitionV1], recorded: Mapping[str, AccountSharingV1]) -> list[ServiceSharing]:
    rows = []
    for tool in tools:
        if not uses_account(tool):
            continue
        fixed = fixed_sharing(tool)
        record = recorded.get(tool.tool_id) if fixed is None else None
        sharing = fixed or (record.sharing if record is not None else default_sharing(tool))
        rows.append(ServiceSharing(tool.tool_id, tool.display_name, sharing, fixed is not None, record is not None))
    return sorted(rows, key=lambda row: (row.display_name.lower(), row.subject))


def starting_overview(tools: Iterable[ToolDefinitionV1]) -> list[ServiceSharing]:
    """Every service that uses an account with the sharing it starts with (onboarding's Secrets page)."""

    return _overview(tools, {})


def onboarding_choices(
    tools: Mapping[str, ToolDefinitionV1], *, share_with_team: bool, choices: Mapping[str, str]
) -> dict[str, AccountSharing]:
    """The choices onboarding records: the model keys and every service with a choice.

    With ``share_with_team`` off everything is private, the administrator's
    "for my own use only". With it on, each named choice, and the start value
    for the rest. A subject that is neither the model keys nor a service with
    a choice is refused (``ValueError``), as is a value other than shared or
    private.
    """

    choosable = {tool_id: tool for tool_id, tool in tools.items() if uses_account(tool) and fixed_sharing(tool) is None}
    unknown = sorted(set(choices) - set(choosable) - {MODEL_KEYS_SHARING_SUBJECT})
    if unknown:
        raise ValueError(f"These are not services the team can share or keep private: {', '.join(unknown)}.")

    def choice(subject: str, start: AccountSharing) -> AccountSharing:
        if not share_with_team:
            return AccountSharing.PRIVATE
        raw = choices.get(subject)
        return start if raw is None else AccountSharing(str(raw))

    result = {MODEL_KEYS_SHARING_SUBJECT: choice(MODEL_KEYS_SHARING_SUBJECT, AccountSharing.SHARED)}
    result.update({tool_id: choice(tool_id, default_sharing(tool)) for tool_id, tool in sorted(choosable.items())})
    return result


def team_account_private_text(tool: ToolDefinitionV1) -> str:
    """Why the team's account is not linked for a private service, and how to share it.

    The broker's link form, TeamLead's proposals and access workflows all
    refuse a team account link for a private service with this text.
    """

    return (
        f"{tool.display_name} is private in this team: agents use only each person's own account, "
        f"so the team's account would not be used. Link your own with {link_command(tool.tool_id)}, "
        f"or an administrator can share the service with {accounts_command(ACCOUNTS_SHARE, tool.tool_id)}."
    )


def fixed_sharing_text(tool: ToolDefinitionV1) -> str:
    """Why a service's sharing cannot be chosen, in plain words."""

    if not uses_account(tool):
        return f"{tool.display_name} does not use an account, so there is nothing to share."
    if fixed_sharing(tool) is AccountSharing.SHARED:
        return f"{tool.display_name} uses only the team's account, so it is always shared."
    if tool.account is AccountMode.PERSONAL_LOGIN:
        return (
            f"{tool.display_name} uses each person's own sign-in and has no team account, "
            "so it is always private."
        )
    return f"{tool.display_name} uses only each person's own account, so it is always private."


__all__ = [
    "AccountSharingStore",
    "ServiceSharing",
    "SharingNotChosen",
    "TeamAccountSharing",
    "default_sharing",
    "fixed_sharing",
    "fixed_sharing_text",
    "onboarding_choices",
    "starting_overview",
    "team_account_private_text",
    "uses_account",
]
