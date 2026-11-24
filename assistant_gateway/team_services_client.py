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
    from custom_transport.protocol1.delivery_refs import DeliveryRefs
    from custom_transport.protocol1.knowledge_blobs import ClientBlobStore
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
        # Command-line runs import this module too, without starting a message host.
        from storage.access_repository import AccessRepository

        return AccessRepository(self._storage)


def gateway_blob_store(
    host: Optional["GatewayMessageHost"],
    *,
    timeout_seconds: float,
    team: Optional[str] = None,
) -> Optional["ClientBlobStore"]:
    """Client blob store over ``GatewayMessageHost`` for ``UA1_BLOBS`` reads and writes, or ``None`` without a host."""

    if host is None:
        return None
    from custom_transport.protocol1.knowledge_blobs import ClientBlobStore

    target_team = host.team if team is None else team

    async def _get_js() -> Any:
        return await host.blob_store_handle(timeout=timeout_seconds)

    async def _register(body: dict[str, Any]) -> dict[str, Any]:
        verified = await host.request_team("blobs.register", body, team=target_team, timeout=timeout_seconds)
        return dict(verified.raw["body"])

    return ClientBlobStore(
        get_js=_get_js,
        team=target_team,
        register=_register,
        request_ids=new_request_id,
    )


def gateway_delivery_refs(config: Any) -> Optional["DeliveryRefs"]:
    """The instance's delivery reference key, which the launcher creates; None (logged) without it."""

    from custom_transport.protocol1.delivery_refs import DeliveryRefs

    try:
        return DeliveryRefs.load(config.layout, config.manager.team_id)
    except Exception:
        _LOGGER.warning("The delivery reference key cannot be read; a chat's own schedule list is unavailable until "
                        "the launcher creates it. event=gateway_delivery_ref_key_missing")
        return None
