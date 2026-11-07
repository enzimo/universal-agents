"""The gateway's connection to the team services, for the application's people's commands.

``MessageHostTeamServices`` sends one command through the gateway's protocol 1 host
(``assistant_gateway.message_host``) and returns the verified reply's body;
``AccessStorePersonReferences`` reads a person's opaque reference from the gateway's
copy of the team's people (``storage.access_repository``). Both implement the ports in
``assistant_gateway.application.team_commands``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from assistant_gateway.application.team_commands import (
    TeamCommandRefused,
    TeamServicesNoAnswer,
    TeamServicesUnavailable,
)
from assistant_gateway.message_host import GatewayHostUnavailable, GatewayMessageHost
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.live import NO_ANSWER_IN_TIME, RequestRefused, ServiceUnavailable
from storage.access_repository import AccessRepository
from storage.config import StorageSettings

_LOGGER = logging.getLogger(__name__)


class MessageHostTeamServices:
    def __init__(self, host: Optional[GatewayMessageHost], *, timeout_seconds: float,
                 logger: logging.Logger = _LOGGER) -> None:
        self._host = host
        self._timeout = timeout_seconds
        self._logger = logger

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str) -> dict[str, Any]:
        if self._host is None:
            raise TeamServicesUnavailable("This gateway runs no message host: start the instance with the launcher.")
        try:
            verified = await self._host.command(kind, body, person_ref=person_ref, request_id=request_id,
                                                timeout=self._timeout)
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
    def __init__(self, storage: StorageSettings, team_id: str) -> None:
        self._storage = storage
        self._team_id = team_id

    def person_ref(self, person_id: str) -> str | None:
        person = AccessRepository(self._storage).team_person(self._team_id, person_id)
        return None if person is None else person.person_ref
