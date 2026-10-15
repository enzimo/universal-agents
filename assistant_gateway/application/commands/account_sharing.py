"""The ``/accounts`` chat command: whose account agents use for each service.

``/accounts`` lists every service that acts with an account: whether the
team shares it (the team's account serves anyone without their own) or keeps
it private (agents use only each person's own), and whether the team's and the
person's own account are linked. Administrators change it:

- ``/accounts share <tool id>`` / ``/accounts private <tool id>``;
- ``/accounts share model`` / ``/accounts private model``: whether people
  added from now on may use the team's model keys.

``/setup`` shows the person what they still set up for their own work: their
own model when they may not use the team's keys, and their own accounts for
the services the team keeps private (``assistant_gateway.personal_setup``; a
newly added person sees the same list on their first message).

The rules are ``access_control.account_sharing``; the gateway's side
(``GatewayAccountSharingPort``, implemented by
``assistant_gateway.account_sharing``) reads and changes the team's record as
the person who typed the command, with the roles they hold now. Answers are
chat Markdown: bold names, code for tool ids and commands, and underlined
(``__text__``) labels, which the web chat, Telegram and Discord show and
other chat apps show as typed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands.context import (
    GatewayActorContext,
    GatewayCommandActorResolver,
    resolve_actor_canonical_user_id,
)
from secure_tools.chat_commands import (
    ACCOUNTS_COMMAND,
    ACCOUNTS_MODEL_SUBJECT,
    ACCOUNTS_PRIVATE,
    ACCOUNTS_SHARE,
    SETUP_COMMAND,
    accounts_command,
)

PERSONAL_SETUP_USAGE = (
    f"Usage: {SETUP_COMMAND}\nShows what you still set up for your own work: your own model and your own accounts."
)
ACCOUNT_SHARING_USAGE = (
    f"Usage: {ACCOUNTS_COMMAND} [{ACCOUNTS_SHARE}|{ACCOUNTS_PRIVATE} <tool_id|{ACCOUNTS_MODEL_SUBJECT}>]\n"
    f"{ACCOUNTS_COMMAND} lists whose account agents use for each service. Shared: the team's account serves "
    "anyone without their own. Private: agents use only each person's own account.\n"
    f"Administrators change it: {accounts_command(ACCOUNTS_SHARE, 'http:exa')}, "
    f"{accounts_command(ACCOUNTS_PRIVATE, 'cli:x')}. {accounts_command(ACCOUNTS_SHARE, ACCOUNTS_MODEL_SUBJECT)} or "
    f"{accounts_command(ACCOUNTS_PRIVATE, ACCOUNTS_MODEL_SUBJECT)} decides whether people added from now on "
    "may use the team's model keys."
)
_UNREGISTERED_NOTICE = "Account sharing is available only to registered users."


def is_account_sharing_command(command: str) -> bool:
    """``/accounts`` or ``/setup``: both about whose account agents use."""

    return any(
        command == name or command.startswith(f"{name} ") for name in (ACCOUNTS_COMMAND, SETUP_COMMAND)
    )


@dataclass(frozen=True)
class AccountSharingCommandRequest:
    command: str
    actor: GatewayActorContext


class GatewayAccountSharingPort(Protocol):
    """The gateway's side: read and change the team's sharing as one person (module docstring)."""

    def overview(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        """Every service that uses an account, with its sharing and the accounts linked."""

    def change(self, *, subject: str, shared: bool, person_id: str, roles: tuple[str, ...]) -> str:
        """Share a service (or the model keys) or make it private; the answer says what changed or why not."""

    def setup(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        """What the person still sets up for their own work (``/setup``)."""


class GatewayAccountSharingActorResolver(GatewayCommandActorResolver, Protocol):
    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        """Return the person's current active roles in this gateway's team."""


@runtime_checkable
class GatewayAccountSharingCommands(Protocol):
    async def execute(self, request: AccountSharingCommandRequest) -> str:
        """Execute one ``/accounts`` command."""


class AccountSharingCommandHandler:
    """Run ``/accounts`` for the person who typed it."""

    def __init__(self, *, identity: GatewayAccountSharingActorResolver, sharing: GatewayAccountSharingPort) -> None:
        self._identity = identity
        self._sharing = sharing

    async def execute(self, request: AccountSharingCommandRequest) -> str:
        parts = request.command.split()
        if parts and parts[0] == SETUP_COMMAND:
            return await self._setup(parts, request)
        if len(parts) not in {1, 3}:
            return ACCOUNT_SHARING_USAGE
        action = parts[1].lower() if len(parts) == 3 else ""
        if action and action not in {ACCOUNTS_SHARE, ACCOUNTS_PRIVATE}:
            return ACCOUNT_SHARING_USAGE
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return _UNREGISTERED_NOTICE
        roles = self._identity.team_roles_for_person(person_id)
        if not action:
            return await asyncio.to_thread(self._sharing.overview, person_id=person_id, roles=roles)
        return await asyncio.to_thread(
            self._sharing.change, subject=parts[2], shared=action == ACCOUNTS_SHARE, person_id=person_id, roles=roles
        )

    async def _setup(self, parts: list[str], request: AccountSharingCommandRequest) -> str:
        if len(parts) != 1:
            return PERSONAL_SETUP_USAGE
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return _UNREGISTERED_NOTICE
        roles = self._identity.team_roles_for_person(person_id)
        return await asyncio.to_thread(self._sharing.setup, person_id=person_id, roles=roles)


__all__ = [
    "ACCOUNT_SHARING_USAGE",
    "AccountSharingCommandHandler",
    "AccountSharingCommandRequest",
    "GatewayAccountSharingCommands",
    "GatewayAccountSharingPort",
    "PERSONAL_SETUP_USAGE",
    "is_account_sharing_command",
]
