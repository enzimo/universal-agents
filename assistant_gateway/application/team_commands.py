"""People's commands to the team services (protocol 1 request/reply on ``svc.team``).

The gateway sends a person's operation with a command token that names the person by
their opaque reference (plan decision 64) and returns the team services' typed reply
body. The team services decide whether the person may (``team_services.people``); the
gateway only renders the reply. Families move here from the old durable manager
commands one at a time (memory first).

Ports keep the application free of NATS: ``GatewayTeamServices`` sends one command
(``assistant_gateway.team_services_client`` over the gateway's message host), and
``GatewayPersonReferences`` reads a person's reference from the gateway's copy of the
team's people in the access store.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from assistant_gateway.application.ports import GatewayIdGenerator


class TeamServicesUnavailable(RuntimeError):
    """The team services could not be asked or did not answer; nothing was decided."""


class TeamCommandRefused(RuntimeError):
    """The team services refused the request itself (README "Refused requests"): ``code`` names why."""

    def __init__(self, kind: str, code: str) -> None:
        super().__init__(f"The team services refused {kind}: {code}.")
        self.kind = kind
        self.code = code


class GatewayTeamServices(Protocol):
    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str,
                      request_id: str) -> dict[str, Any]:
        """Send one command and return its reply body; raises the two errors above."""


class GatewayPersonReferences(Protocol):
    def person_ref(self, person_id: str) -> str | None:
        """The person's opaque reference, or None when the gateway has not recorded them."""

    def person_id(self, person_ref: str) -> str | None:
        """Who a reference in a reply names, or None when no recorded person has it."""


class GatewayTeamCommandService:
    """Send one person's command to the team services and return the reply body."""

    def __init__(self, *, team_services: GatewayTeamServices, references: GatewayPersonReferences,
                 id_generator: GatewayIdGenerator, logger: logging.Logger | None = None) -> None:
        self._team_services = team_services
        self._references = references
        self._id_generator = id_generator
        self._logger = logger or logging.getLogger(__name__)

    async def request(self, kind: str, body: dict[str, Any], *, person_id: str) -> dict[str, Any]:
        """``body`` without its ``request_id``: each call is a new command with a fresh id."""

        reference = await asyncio.to_thread(self._references.person_ref, person_id)
        if reference is None:
            self._logger.warning("A person's command has no person reference to send. "
                                 "event=gateway_team_command_person_unrecorded kind=%s person=%s", kind, person_id)
            # Not the person's fault: the gateway's copy of the people is behind (it records everyone at start).
            raise TeamServicesUnavailable("The gateway has not recorded this person yet; try again in a moment.")
        request_id = self._id_generator.new_correlation_id()
        return await self._team_services.command(kind, {"request_id": request_id, **body}, person_ref=reference,
                                                 request_id=request_id)
