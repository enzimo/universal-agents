"""Authorized request/response exchange for gateway manager commands."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from access_control.knowledge_access import knowledge_target
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import (
    GatewayIdGenerator,
    GatewayPrivateResourceAuthorizer,
)
from assistant_gateway.application.private_resources import (
    PRIVATE_ACTION_CREATE,
    PRIVATE_ACTION_DELETE,
    PRIVATE_ACTION_READ,
    PRIVATE_ACTION_UPDATE,
    RESOURCE_CONVERSATION,
    RESOURCE_OBJECTIVE,
    RESOURCE_SCHEDULE,
    RESOURCE_TEAM_MEMORY,
    RESOURCE_USER_MEMORY,
    RESOURCE_TASK,
)
from assistant_gateway.application.request_context import (
    GatewayCommandPerson,
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.control_v1 import (
    ManagerKnowledgeCommand,
    ManagerMemoryCommand,
    ManagerRuntimeWorkCommand,
    ManagerTopicCommand,
    PersonStandingCommandV1,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ManagerScheduleCommand
from custom_messages.enums import RequesterType


GatewayManagerCommand = (
    ManagerScheduleCommand
    | ManagerMemoryCommand
    | ManagerKnowledgeCommand
    | ManagerRuntimeWorkCommand
    | ManagerTopicCommand
)
# PersonStandingCommandV1 is not tied to a chat session; its gateway context
# carries only the acting administrator's signed context.
PERSON_STANDING_SESSION_KEY = "person_standing"


@dataclass(frozen=True)
class GatewayManagerCommandSettings:
    """Stable gateway identity and timeout settings for manager commands."""

    instance_name: str
    response_route: str
    timeout_seconds: float
    unavailable_text: str


@dataclass(frozen=True)
class GatewayManagerCommandRequest:
    """One authorized manager command associated with a gateway session."""

    command: GatewayManagerCommand
    session: SessionRecord
    # The signed-in person, for the gateway's own checks and the signed
    # context the envelope carries; the command itself names nobody.
    person: GatewayCommandPerson
    route: GatewayRequestRoute = GatewayRequestRoute()
    target_agent_id: str | None = None


@dataclass(frozen=True)
class _PrivateResourceTarget:
    resource_type: str
    resource_id: str
    action: str
    # False for records every team member shares (team memory, team knowledge pages).
    owned_by_person: bool = True


class GatewayManagerCommandService:
    """Own authorization, correlation, publication, and response validation."""

    def __init__(
        self,
        *,
        settings: GatewayManagerCommandSettings,
        manager: GatewayManagerClient,
        request_context: GatewayRequestContextService,
        private_authorizer: GatewayPrivateResourceAuthorizer,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._manager = manager
        self._request_context = request_context
        self._private_authorizer = private_authorizer
        self._id_generator = id_generator
        self._logger = logger or logging.getLogger(__name__)

    async def request(
        self,
        request: GatewayManagerCommandRequest,
    ) -> GatewayManagerCommand:
        """Publish one command and wait for its exact typed response.

        Checking the command and building the envelope read the people's
        records, so they run in a worker thread; the gateway's loop also runs
        the chat apps (website_handlers.off_loop). Publishing and waiting stay
        on the loop.
        """

        manager_id, correlation_id, envelope = await asyncio.to_thread(self._prepared_command, request)
        async with self._manager.control_channel(correlation_id) as response_queue:
            await self._manager.publish_durable(envelope, routing_key=manager_id)
            return await self._wait_for_response(
                request.command,
                response_queue=response_queue,
                manager_id=manager_id,
            )

    def _prepared_command(self, request: GatewayManagerCommandRequest) -> tuple[str, str, MessageEnvelope]:
        """Check the command and build its envelope: (manager id, correlation id, envelope)."""

        self._authorize(request.command, session=request.session, person=request.person)
        if request.target_agent_id is not None and not (
            isinstance(request.command, ManagerRuntimeWorkCommand)
            and request.command.action in {"list_tasks", "list_finished_tasks", "remove_task", "purge_tasks"}
        ):
            raise ValueError("Only task controls may target a team member.")
        manager_id = request.target_agent_id or self._require_manager_id()
        correlation_id = self._id_generator.new_correlation_id()
        envelope = self._build_envelope(
            request,
            manager_id=manager_id,
            correlation_id=correlation_id,
        )
        return manager_id, correlation_id, envelope

    async def request_person_standing(
        self, command: PersonStandingCommandV1, *, actor_person_id: str
    ) -> PersonStandingCommandV1:
        """Tell the manager a person's standing changed and wait for its answer.

        No private-resource check: the identity service already decided that
        the actor (an administrator) may disable, remove or restore the
        person. The envelope carries the actor's gateway-signed context,
        which the manager requires (``custom_agents.person_departure_runtime``).
        Raises ``RuntimeError`` when no manager is available and
        ``TimeoutError`` when it does not answer in time; the command stays
        queued durably either way.
        """

        manager_id = self._require_manager_id()
        correlation_id = self._id_generator.new_correlation_id()
        route = GatewayRequestRoute(canonical_user_id=actor_person_id)
        envelope = MessageEnvelope(
            msg_content=command,
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
                idempotency_key=command.get_id(),
            ),
            msg_meta=MessageMeta(
                from_agent_name=f"{self._settings.instance_name}_gateway",
                msg_correlation_id=correlation_id,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=PERSON_STANDING_SESSION_KEY,
                    delivery_mode="manager_control",
                    route=route,
                    manager_agent_id=manager_id,
                ),
            ),
        )
        async with self._manager.control_channel(correlation_id) as response_queue:
            await self._manager.publish_durable(envelope, routing_key=manager_id)
            response = await self._wait_for_response(
                command, response_queue=response_queue, manager_id=manager_id
            )
        assert isinstance(response, PersonStandingCommandV1)
        return response

    def _require_manager_id(self) -> str:
        manager_id = self._manager.manager_id
        if manager_id is None:
            raise RuntimeError(self._settings.unavailable_text)
        return manager_id

    def _authorize(
        self,
        command: GatewayManagerCommand,
        *,
        session: SessionRecord,
        person: GatewayCommandPerson,
    ) -> None:
        target = self._private_resource_target(command, session=session, person=person)
        principal = person
        allowed = self._private_authorizer.can_access(
            actor_canonical_user_id=principal.canonical_user_id,
            owner_canonical_user_id=(
                principal.canonical_user_id if target.owned_by_person else None
            ),
            resource_type=target.resource_type,
            resource_id=target.resource_id,
            action=target.action,
            team_id=principal.team_id,
            roles=tuple(principal.roles),
        )
        if allowed:
            return
        self._logger.warning(
            "Gateway denied manager command before publication. "
            "event=gateway_manager_command_authorization_denied "
            "command_type=%s command_id=%s actor=%s action=%s "
            "resource_type=%s resource_id=%s team_id=%s",
            type(command).__name__,
            command.get_id(),
            principal.canonical_user_id,
            target.action,
            target.resource_type,
            target.resource_id,
            principal.team_id,
        )
        raise PermissionError("Private resource access was denied.")

    @staticmethod
    def _private_resource_target(
        command: GatewayManagerCommand,
        *,
        session: SessionRecord,
        person: GatewayCommandPerson,
    ) -> _PrivateResourceTarget:
        if isinstance(command, ManagerScheduleCommand):
            return _PrivateResourceTarget(
                resource_type=RESOURCE_SCHEDULE,
                resource_id=(
                    command.schedule_id
                    or f"session:{command.session_key or session.session_key}"
                ),
                action=(
                    PRIVATE_ACTION_READ
                    if command.action == "list"
                    else PRIVATE_ACTION_DELETE
                    if command.action == "remove"
                    else PRIVATE_ACTION_UPDATE
                ),
            )
        if isinstance(command, ManagerKnowledgeCommand):
            # The same rule the manager applies (access_control.knowledge_access).
            knowledge = knowledge_target(command.command, person_id=person.canonical_user_id)
            return _PrivateResourceTarget(
                resource_type=knowledge.resource_type,
                resource_id=knowledge.resource_id,
                action=knowledge.action,
                owned_by_person=knowledge.owner_person_id is not None,
            )
        if isinstance(command, ManagerMemoryCommand):
            memory_command = command.command
            return _PrivateResourceTarget(
                owned_by_person=memory_command.bank != "coremem",
                resource_type=(
                    RESOURCE_TEAM_MEMORY
                    if memory_command.bank == "coremem"
                    else RESOURCE_USER_MEMORY
                ),
                resource_id=(
                    f"{memory_command.bank}:"
                    f"{memory_command.memory_id or memory_command.event_id or 'collection'}"
                ),
                action=(
                    PRIVATE_ACTION_READ
                    if memory_command.action in {"list", "show"}
                    else PRIVATE_ACTION_CREATE
                    if memory_command.action == "create"
                    else PRIVATE_ACTION_DELETE
                    if memory_command.action == "delete"
                    else PRIVATE_ACTION_UPDATE
                ),
            )
        if isinstance(command, ManagerRuntimeWorkCommand):
            if command.action in {"list_tasks", "list_finished_tasks", "remove_task", "purge_tasks"}:
                return _PrivateResourceTarget(
                    resource_type=RESOURCE_TASK,
                    resource_id=command.target_task_id or f"user:{person.canonical_user_id}",
                    action=PRIVATE_ACTION_READ if command.action in {"list_tasks", "list_finished_tasks"} else PRIVATE_ACTION_DELETE,
                )
            if command.scope == "objective":
                return _PrivateResourceTarget(
                    resource_type=RESOURCE_OBJECTIVE,
                    resource_id=(
                        command.target_objective_id
                        or f"owner:{person.canonical_user_id}"
                    ),
                    action=(
                        PRIVATE_ACTION_READ
                        if command.action == "list_objectives"
                        else PRIVATE_ACTION_DELETE
                        if command.action == "delete_objective"
                        else PRIVATE_ACTION_UPDATE
                    ),
                )
            if command.scope == "task":
                resource_id = command.target_task_id or "missing-task"
            elif command.scope == "conversation":
                resource_id = (
                    command.conversation_id
                    or session.conversation_id
                    or "missing-conversation"
                )
            elif command.scope == "session":
                resource_id = command.session_key or session.session_key
            else:
                resource_id = f"user:{person.canonical_user_id}"
            return _PrivateResourceTarget(
                resource_type=RESOURCE_CONVERSATION,
                resource_id=resource_id,
                action=(
                    PRIVATE_ACTION_READ
                    if command.action == "inventory"
                    else PRIVATE_ACTION_UPDATE
                ),
            )
        return _PrivateResourceTarget(
            resource_type=RESOURCE_CONVERSATION,
            resource_id=(
                command.conversation_id
                or session.conversation_id
                or f"session:{session.session_key}"
            ),
            action=(
                PRIVATE_ACTION_UPDATE
                if command.action == "tag_thread"
                else PRIVATE_ACTION_READ
            ),
        )

    def _build_envelope(
        self,
        request: GatewayManagerCommandRequest,
        *,
        manager_id: str,
        correlation_id: str,
    ) -> MessageEnvelope:
        command = request.command
        session = request.session
        route = GatewayRequestRoute(
            channel_name=request.route.channel_name or session.channel_name,
            user_id=request.route.user_id or session.user_id,
            chat_id=request.route.chat_id or session.chat_id,
            chat_scope=request.route.chat_scope,
            thread_id=request.route.thread_id or session.thread_id,
            original_message_id=request.route.original_message_id,
            canonical_user_id=request.person.canonical_user_id,
        )
        presentation = self._request_context.requester_presentation_for_session(
            session,
            route,
        )
        return MessageEnvelope(
            msg_content=command,
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
                idempotency_key=command.get_id(),
            ),
            msg_meta=MessageMeta(
                from_agent_name=f"{self._settings.instance_name}_gateway",
                msg_correlation_id=correlation_id,
                conversation_id=session.conversation_id,
                request_timezone=presentation.timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="manager_control",
                    route=route,
                    manager_agent_id=manager_id,
                ),
            ),
        )

    async def _wait_for_response(
        self,
        command: GatewayManagerCommand | PersonStandingCommandV1,
        *,
        response_queue: asyncio.Queue[MessageEnvelope],
        manager_id: str,
    ) -> GatewayManagerCommand | PersonStandingCommandV1:
        deadline = asyncio.get_running_loop().time() + self._settings.timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for manager control response.")
            try:
                response = await asyncio.wait_for(
                    response_queue.get(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "Timed out waiting for manager control response."
                ) from exc
            content = response.msg_content
            if (
                response.msg_header.from_agent_id == manager_id
                and type(content) is type(command)
                and content.get_id() == command.get_id()
            ):
                return content
