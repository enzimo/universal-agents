"""The gateway's connection to the team services, for the application's people's commands.

``MessageHostTeamServices`` sends one command through the gateway's protocol 1 host
(``assistant_gateway.message_host``) and returns the verified reply's body. It
implements ``GatewayTeamServices`` in ``assistant_gateway.application.team_commands``;
people's references for commands come from ``assistant_gateway.people_directory_sync``.
``AccessStorePersonReferences`` reads the people copy to name the people a reply names.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from assistant_gateway.application.team_commands import (
    TeamCommandRefused,
    TeamServicesNoAnswer,
    TeamServicesUnavailable,
)

if TYPE_CHECKING:
    from assistant_gateway.message_host import GatewayMessageHost
    from storage.access_repository import AccessRepository
    from storage.config import StorageSettings

_LOGGER = logging.getLogger(__name__)


def new_request_id() -> str:
    """A fresh request id for a person's command: a UUIDv7 (the contract's "Fresh ids")."""

    from custom_transport.protocol1.ids import new_message_id  # the gateway's own module stays free of transport code

    return new_message_id()


class MessageHostTeamServices:
    def __init__(self, host: Optional["GatewayMessageHost"], *, timeout_seconds: float,
                 logger: logging.Logger = _LOGGER) -> None:
        self._host = host
        self._timeout = timeout_seconds
        self._logger = logger

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str) -> dict[str, Any]:
        if self._host is None:
            # Only the gateway service runs the gateway's host (assistant_gateway.message_host): command-line
            # chats, and a gateway on an instance the launcher never prepared, cannot reach the team services.
            raise TeamServicesUnavailable("This process cannot reach the team services: only the gateway service the "
                                          "launcher starts sends these commands. Use Web Chat or a chat app.")
        # Imported here, not with the module: only the gateway service runs a host, and command-line runs,
        # which import this module too, should not load the protocol code (about half a second).
        from assistant_gateway.message_host import GatewayHostUnavailable
        from custom_messages.wire.refusals import FrameRefused
        from custom_transport.protocol1.live import NO_ANSWER_IN_TIME, RequestRefused, ServiceUnavailable

        try:
            # The host binds the command token to the body's request id (command_binding_id).
            verified = await self._host.command(kind, body, person_ref=person_ref, timeout=self._timeout)
        except RequestRefused as error:
            raise TeamCommandRefused(kind, error.code) from error
        except ServiceUnavailable as error:
            if error.reason == NO_ANSWER_IN_TIME:
                raise TeamServicesNoAnswer("The team services did not answer in time.") from error
            raise TeamServicesUnavailable("The team services are not running right now. Try again in a moment; "
                                          "if it keeps happening, an administrator can check them.") from error
        except GatewayHostUnavailable as error:
            self._logger.warning("A person's command could not be sent. event=gateway_team_command_not_sent kind=%s "
                                 "request_id=%s reason=%s", kind, request_id, error)
            raise TeamServicesUnavailable("The gateway is still connecting to the team services. Try again in a "
                                          "moment.") from error
        except FrameRefused as error:
            self._logger.error("The team services' reply was refused. event=gateway_team_reply_refused kind=%s "
                               "request_id=%s reason=%s", kind, request_id, error)
            raise TeamServicesUnavailable("The team services' answer could not be checked, so it is not shown. An "
                                          "administrator can find the reason in the gateway log.") from error
        return dict(verified.raw["body"])


class AccessStorePersonReferences:
    """Reads, never writes, the gateway's copy of the team's people: for naming the people a reply names.

    The Knowledge page names its editors with it (``application.knowledge_pages``);
    commands get their person's reference from ``people_directory_sync``, which first
    makes the copy current.
    """

    def __init__(self, storage: "StorageSettings", team_id: str) -> None:
        self._storage = storage
        self._team_id = team_id

    def person_ref(self, person_id: str) -> str | None:
        person = self._store().team_person(self._team_id, person_id)
        return None if person is None else person.person_ref

    def person_id(self, person_ref: str) -> str | None:
        person = self._store().team_person_by_ref(self._team_id, person_ref)
        return None if person is None else person.person_id

    def _store(self) -> "AccessRepository":
        # Imported when first used, as the message host is: command-line runs import this module too.
        from storage.access_repository import AccessRepository

        return AccessRepository(self._storage)
