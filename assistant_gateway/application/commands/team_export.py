"""The ``/upgrade`` chat command: an owner exports the whole team for an upgrade.

It opens the secure broker's export page for the owner who typed it and
watches it (``assistant_gateway.application.team_exports``): the passphrase is
typed on that page and never passes through the gateway, the chat or an
agent. Only owners may run it (``access_control.people.team_export_refusal``);
the broker and the export process check again. A gateway started without the
launcher has nothing to run the export, so it points to the host command.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from access_control.people import team_export_refusal
from assistant_gateway.application.broker_sign_in import BrokerSignInPage, GatewayBrokerSignIns
from assistant_gateway.application.commands.account_link import result_target
from assistant_gateway.application.commands.context import (
    GatewayActorContext,
    GatewayCommandActorResolver,
    resolve_actor_canonical_user_id,
)
from assistant_gateway.application.link_results import GatewayLinkResultWatches
from assistant_gateway.application.ports import GatewaySecureBroker
from assistant_gateway.application.team_exports import (
    EXPORT_PAGE_DESCRIPTION,
    EXPORT_PAGE_RECEIPT,
    NOT_LAUNCHED_TEXT,
    UPGRADE_USAGE,
)
from assistant_gateway.application.transient_presentation import GatewayTransientAction
from secure_tools.chat_commands import UPGRADE_COMMAND
from team_transfer.export_requests import (
    TEAM_EXPORT_PAGE_METHOD,
    TEAM_EXPORT_PAGE_PROVIDER,
    supervisor_runs_exports,
)

LOGGER = logging.getLogger(__name__)


class GatewayTeamExportActorResolver(GatewayCommandActorResolver, Protocol):
    """Resolve the person running ``/upgrade`` and their current roles."""

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        """Return the person's current active roles in this gateway's team."""


@dataclass(frozen=True)
class TeamExportCommandRequest:
    """Input for one ``/upgrade``; the session and correlation id say where its result goes."""

    command: str
    actor: GatewayActorContext
    session_key: str | None = None
    correlation_id: str | None = None


@runtime_checkable
class GatewayTeamExportCommands(Protocol):
    async def execute(self, request: TeamExportCommandRequest) -> str | GatewayTransientAction:
        """Open the export page, or say plainly why not."""


def is_upgrade_command(command: str) -> bool:
    words = command.split()
    return bool(words) and words[0].lower() == UPGRADE_COMMAND


class TeamExportCommandHandler:
    """Execute ``/upgrade`` without access to the concrete gateway."""

    def __init__(
        self,
        *,
        identity: GatewayTeamExportActorResolver,
        secure_broker: GatewaySecureBroker,
        team_id: str,
        link_results: GatewayLinkResultWatches | None = None,
        supervised: Callable[[], bool] = supervisor_runs_exports,
    ) -> None:
        self._identity = identity
        self._sign_ins = GatewayBrokerSignIns(secure_broker=secure_broker, team_id=team_id)
        self._link_results = link_results
        self._supervised = supervised

    async def execute(self, request: TeamExportCommandRequest) -> str | GatewayTransientAction:
        if request.command.split()[1:]:
            return UPGRADE_USAGE
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return "Only people registered in this team can export it."
        roles = self._identity.team_roles_for_person(person_id)
        refusal = team_export_refusal(roles)
        if refusal:
            LOGGER.warning(
                "Team export refused: not an owner. event=gateway_team_export_refused person_id=%s", person_id
            )
            return refusal
        if not self._supervised():
            return NOT_LAUNCHED_TEXT
        if self._sign_ins.unavailable_message:
            return self._sign_ins.unavailable_message
        page = await self._sign_ins.open_page(
            provider=TEAM_EXPORT_PAGE_PROVIDER,
            method=TEAM_EXPORT_PAGE_METHOD,
            context=self._sign_ins.person_context(person_id, roles),
            restart_command=UPGRADE_COMMAND,
            description=EXPORT_PAGE_DESCRIPTION,
            receipt=EXPORT_PAGE_RECEIPT,
        )
        if not isinstance(page, BrokerSignInPage):
            return page
        target = result_target(request, person_id) if self._link_results is not None else None
        if target is not None and self._link_results is not None:
            self._link_results.watch_team_export(page=page, target=target)
        return page.action


__all__ = [
    "GatewayTeamExportCommands",
    "TeamExportCommandHandler",
    "TeamExportCommandRequest",
    "is_upgrade_command",
]
