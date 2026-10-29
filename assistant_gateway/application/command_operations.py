"""Application owners for identity, memory, topic, and session commands."""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from typing import Literal, Protocol

from assistant_gateway.application.identity_service import GatewayIdentityService
from assistant_gateway.application.interactive_submission import (
    GatewayInteractiveSubmissionRequest,
    GatewayInteractiveSubmissionService,
)
from assistant_gateway.application.person_lifecycle import GatewayPersonLifecycle
from assistant_gateway.application.person_setup import PersonSetupChoices
from assistant_gateway.application.manager_commands import (
    GatewayManagerCommandRequest,
    GatewayManagerCommandService,
)
from assistant_gateway.application.ports import GatewayPrivateResourceAuthorizer
from assistant_gateway.application.private_resources import (
    PRIVATE_ACTION_CREATE,
    PRIVATE_ACTION_DELETE,
    PRIVATE_ACTION_UPDATE,
    RESOURCE_TEAM_MEMORY,
    RESOURCE_USER_MEMORY,
)
from assistant_gateway.application.request_context import (
    GatewayCommandPerson,
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.response_coordinator import QueryHandler
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
    with_parallel_mark_notice,
)
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.application.route_context import GatewayRouteContextV1
from custom_messages.contracts.control_v1 import (
    ManagerKnowledgeCommand,
    ManagerMemoryCommand,
    ManagerTopicCommand,
    PinnedMemoryCommandPayload,
    PinnedMemoryCommandResultPayload,
)
from time_presentation import format_user_timestamp


class GatewayPinnedMemoryCommandParser(Protocol):
    """Parse a pinned-memory command without coupling the application to its SDK."""

    def parse(self, command: str, *, bank: str) -> PinnedMemoryCommandPayload: ...


@dataclass(frozen=True)
class GatewayCommandOperationsSettings:
    """Stable routing values needed by command use cases."""

    default_session_key: str
    default_owner_canonical_user_id: str
    manager_agent_id: str | None


@dataclass(frozen=True)
class GatewayPinnedMemoryOperationResult:
    """Typed manager result plus gateway-authorized browser actions."""

    result: PinnedMemoryCommandResultPayload
    available_actions: tuple[Literal["edit", "delete"], ...]
    can_create: bool = False


class GatewayPinnedMemoryConflictError(ValueError):
    """The manager rejected a mutation bound to an outdated memory revision."""


class GatewayCommandOperationsApplication:
    """Own command-facing identity, session, memory, and topic operations."""

    def __init__(
        self,
        *,
        settings: GatewayCommandOperationsSettings,
        identity: GatewayIdentityService,
        request_context: GatewayRequestContextService,
        sessions: GatewaySessionService,
        manager_commands: GatewayManagerCommandService,
        submission: GatewayInteractiveSubmissionService,
        pinned_memory_parser: GatewayPinnedMemoryCommandParser,
        private_authorizer: GatewayPrivateResourceAuthorizer,
        person_lifecycle: GatewayPersonLifecycle,
    ) -> None:
        self._settings = settings
        self._identity = identity
        # What follows a change to a person's standing: their work, records
        # and memory-review schedules (person_lifecycle).
        self._person_lifecycle = person_lifecycle
        self._request_context = request_context
        self._sessions = sessions
        self._manager_commands = manager_commands
        self._submission = submission
        self._pinned_memory_parser = pinned_memory_parser
        self._private_authorizer = private_authorizer

    def canonical_user_id_for_context(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str | None:
        return self._request_context.canonical_user_id(
            GatewayRequestRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            )
        )

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        """The person's current active roles in this gateway's team."""

        return self._request_context.team_roles(canonical_user_id)

    def gateway_users_enabled(self) -> bool:
        return self._request_context.users_enabled

    def requester_timezone_for_context(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._request_context.requester_presentation(
            GatewayRequestRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            )
        ).timezone

    def _requesting_user(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ):
        return self._request_context.resolve_requesting_user(
            GatewayRequestRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            )
        )

    async def register_new_user_for_channel(
        self,
        *,
        display_name: str | None,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        target_user_id: str | None = None,
        replace: bool = False,
        canonical_user_id: str | None = None,
        choices: PersonSetupChoices | None = None,
    ) -> str:
        actor = self._requesting_user(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            canonical_user_id=canonical_user_id,
        )
        change = self._identity.register_user_for_channel(
            actor=actor,
            display_name=display_name,
            channel_name=channel_name,
            external_user_id=target_user_id or user_id,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
            thread_id=thread_id,
            replace=replace,
        )
        return await self._person_lifecycle.after_added(actor, change, choices)

    def render_new_user_status(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        resolved_user = self._requesting_user(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            canonical_user_id=canonical_user_id,
        )
        text = self._identity.render_user_status(
            channel_name=channel_name,
            external_user_id=user_id,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
            resolved_user=resolved_user,
        )
        if resolved_user is None:
            return text
        lines = self._person_lifecycle.settings_lines(resolved_user.user.canonical_user_id)
        return "\n".join([text, *lines]) if lines else text

    def set_gateway_user_team_keys(
        self,
        *,
        target: str,
        allowed: bool,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._person_lifecycle.set_team_keys(
            actor=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            target=target,
            allowed=allowed,
            channel_name=channel_name,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
        )

    def render_gateway_user_list(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        status: str | None = None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._identity.render_user_list(
            actor=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            status=status,
        )

    async def set_gateway_user_enabled_status(
        self,
        *,
        target: str,
        enabled: bool,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return await self._person_lifecycle.set_enabled(
            actor=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            target=target,
            enabled=enabled,
            channel_name=channel_name,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
        )

    def set_gateway_user_role(
        self,
        *,
        target: str,
        role: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._identity.set_user_role(
            actor=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            target=target,
            role=role,
            channel_name=channel_name,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
        )

    async def remove_gateway_user(
        self,
        *,
        target: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return await self._person_lifecycle.remove(
            actor=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            target=target,
            channel_name=channel_name,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
        )

    async def restore_gateway_user(
        self,
        *,
        target: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return await self._person_lifecycle.restore(
            actor=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            target=target,
            channel_name=channel_name,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
        )

    def create_new_user_web_token(
        self,
        *,
        label: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._identity.create_web_token(
            resolved_user=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            label=label,
            channel_name=channel_name,
        )

    async def create_new_user_web_invite(
        self,
        *,
        target: str | None,
        display_name: str | None,
        label: str,
        create_new: bool,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
        choices: PersonSetupChoices | None = None,
    ) -> str:
        actor = self._requesting_user(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            canonical_user_id=canonical_user_id,
        )
        change = self._identity.create_web_invite(
            actor=actor,
            target=target,
            display_name=display_name,
            label=label,
            create_new=create_new,
            channel_name=channel_name,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
        )
        return await self._person_lifecycle.after_added(actor, change, choices)

    def create_new_user_link_code(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._identity.create_link_code(
            resolved_user=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            channel_name=channel_name,
        )

    def link_new_user_code_for_channel(
        self,
        *,
        code: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        replace: bool = False,
    ) -> str:
        return self._identity.link_code_for_channel(
            code=code,
            channel_name=channel_name,
            external_user_id=user_id,
            chat_scope=self._request_context.chat_scope(
                GatewayRequestRoute(channel_name=channel_name, chat_id=chat_id)
            ),
            thread_id=thread_id,
            replace=replace,
        )

    def set_new_user_default_channel(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        clear: bool = False,
        canonical_user_id: str | None = None,
    ) -> str:
        return self._identity.set_default_channel(
            resolved_user=self._requesting_user(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            channel_name=channel_name,
            chat_id=chat_id,
            thread_id=thread_id,
            clear=clear,
        )

    def start_new_session(
        self,
        *,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> SessionRecord:
        return self._sessions.start_new_session(
            manager_agent_id=self._settings.manager_agent_id,
            owner_canonical_user_id=self.canonical_user_id_for_context(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            route=GatewaySessionRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
        )

    def switch_session_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ) -> SessionRecord | None:
        return self._sessions.switch_conversation(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
        )

    def resolve_session(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> SessionRecord:
        return self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=self._settings.manager_agent_id,
            owner_canonical_user_id=self.canonical_user_id_for_context(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                canonical_user_id=canonical_user_id,
            ),
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            ),
            enforce_owner_scope=self._request_context.users_enabled,
        )

    async def send_user_message(
        self,
        text: str,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        query_handler: QueryHandler | None = None,
        canonical_user_id: str | None = None,
    ) -> GatewayResponse:
        return await self._submission.submit(
            GatewayInteractiveSubmissionRequest(
                text=text,
                session_key=session_key,
                route=GatewayRequestRoute(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    canonical_user_id=canonical_user_id,
                ),
                query_handler=query_handler,
            )
        )

    def _principal(
        self, session: SessionRecord, route: GatewayRequestRoute
    ) -> GatewayCommandPerson:
        return self._request_context.manager_principal(
            GatewayRequestRoute(
                channel_name=route.channel_name or session.channel_name,
                user_id=route.user_id or session.user_id,
                chat_id=route.chat_id or session.chat_id,
                thread_id=route.thread_id or session.thread_id,
                canonical_user_id=session.owner_canonical_user_id,
            )
        )

    async def _manager_request(
        self,
        command: ManagerMemoryCommand | ManagerTopicCommand,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        person: GatewayCommandPerson,
    ) -> ManagerMemoryCommand | ManagerTopicCommand:
        result = await self._manager_commands.request(
            GatewayManagerCommandRequest(
                command=command,
                session=session,
                person=person,
                route=route,
            )
        )
        if not isinstance(result, (ManagerMemoryCommand, ManagerTopicCommand)):
            raise TypeError("Manager returned the wrong command result type.")
        return result

    async def execute_knowledge_command(
        self, command: ManagerKnowledgeCommand, *, context: GatewayRouteContextV1
    ) -> ManagerKnowledgeCommand:
        """Send one Knowledge page request to the manager for the route's signed-in person.

        Resolving the session and the person reads the gateway's records, so
        it runs in a worker thread (website_handlers.off_loop).
        """

        route = GatewayRequestRoute(
            channel_name=context.channel_name,
            user_id=context.user_id,
            chat_id=context.chat_id,
            canonical_user_id=context.canonical_user_id,
        )

        def session_and_person() -> tuple[SessionRecord, GatewayCommandPerson]:
            session = self.resolve_session(
                session_key=context.session_key,
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                canonical_user_id=route.canonical_user_id,
            )
            return session, self._principal(session, route)

        session, person = await asyncio.to_thread(session_and_person)
        response = await self._manager_commands.request(
            GatewayManagerCommandRequest(command=command, session=session, person=person, route=route)
        )
        if not isinstance(response, ManagerKnowledgeCommand):
            raise TypeError("Manager returned the wrong knowledge result type.")
        return response

    async def execute_pinned_memory_command(
        self,
        *,
        bank: str,
        command: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        session_key: str | None = None,
    ) -> str:
        route = GatewayRequestRoute(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        try:
            session = self.resolve_session(
                session_key=session_key,
                channel_name=channel_name,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
            )
            response = await self._manager_request(
                ManagerMemoryCommand(
                    command=self._pinned_memory_parser.parse(command, bank=bank),
                ),
                session=session,
                route=route,
                person=self._principal(session, route),
            )
            if not isinstance(response, ManagerMemoryCommand):
                raise TypeError("Manager returned the wrong memory result type.")
            return response.rendered_result
        except PermissionError:
            return "Use /user first to register this channel before managing pinned memory."
        except Exception as exc:
            return f"Pinned memory command failed: {exc}"

    async def execute_pinned_memory_operation(
        self,
        *,
        command: PinnedMemoryCommandPayload,
        session_key: str,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None,
        canonical_user_id: str,
    ) -> GatewayPinnedMemoryOperationResult:
        """Execute one typed Settings operation through the manager owner."""

        route = GatewayRequestRoute(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            canonical_user_id=canonical_user_id,
        )
        session = self.resolve_session(
            session_key=session_key,
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            canonical_user_id=canonical_user_id,
        )
        principal = self._principal(session, route)
        response = await self._manager_request(
            ManagerMemoryCommand(command=command),
            session=session,
            route=route,
            person=principal,
        )
        if not isinstance(response, ManagerMemoryCommand):
            raise TypeError("Manager returned the wrong memory result type.")
        if response.error_code == "conflict":
            raise GatewayPinnedMemoryConflictError(response.rendered_result)
        if response.error_code == "invalid_request":
            raise ValueError(response.rendered_result)
        if response.error_code == "runtime_error":
            raise RuntimeError(response.rendered_result)
        if response.success is not True or response.result is None:
            raise PermissionError(
                response.rendered_result or "Pinned memory operation was denied."
            )
        return GatewayPinnedMemoryOperationResult(
            result=response.result,
            can_create=self._private_authorizer.can_access(
                actor_canonical_user_id=principal.canonical_user_id,
                owner_canonical_user_id=None
                if command.bank == "coremem"
                else principal.canonical_user_id,
                resource_type=RESOURCE_TEAM_MEMORY
                if command.bank == "coremem"
                else RESOURCE_USER_MEMORY,
                resource_id=f"{command.bank}:collection",
                action=PRIVATE_ACTION_CREATE,
                team_id=principal.team_id,
                roles=tuple(principal.roles),
            ),
            available_actions=self._pinned_memory_available_actions(
                bank=command.bank,
                memory_id=command.memory_id,
                principal=principal,
            ),
        )

    def _pinned_memory_available_actions(
        self,
        *,
        bank: str,
        memory_id: int | None,
        principal: GatewayCommandPerson,
    ) -> tuple[Literal["edit", "delete"], ...]:
        resource_type = (
            RESOURCE_TEAM_MEMORY if bank == "coremem" else RESOURCE_USER_MEMORY
        )
        owner_canonical_user_id = (
            None if bank == "coremem" else principal.canonical_user_id
        )
        resource_id = f"{bank}:{memory_id or 'collection'}"
        actions: list[Literal["edit", "delete"]] = []
        action_choices: tuple[tuple[Literal["edit", "delete"], str], ...] = (
            ("edit", PRIVATE_ACTION_UPDATE),
            ("delete", PRIVATE_ACTION_DELETE),
        )
        for label, action in action_choices:
            if self._private_authorizer.can_access(
                actor_canonical_user_id=principal.canonical_user_id,
                owner_canonical_user_id=owner_canonical_user_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                team_id=principal.team_id,
                roles=tuple(principal.roles),
            ):
                actions.append(label)
        return tuple(actions)

    async def execute_topic_command(
        self,
        *,
        command: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        session_key: str | None = None,
    ) -> tuple[str | None, str]:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return session_key, f"Topic command failed: {exc}"
        if not parts:
            return session_key, _TOPIC_USAGE
        route = GatewayRequestRoute(
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        session = self.resolve_session(
            session_key=session_key,
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        try:
            person = self._principal(session, route)
        except Exception as exc:
            return session.session_key, f"Topic command failed: {exc}"
        timezone_name = self.requester_timezone_for_context(
            channel_name=channel_name or session.channel_name,
            user_id=user_id or session.user_id,
            chat_id=chat_id or session.chat_id,
            canonical_user_id=session.owner_canonical_user_id,
        )

        async def request_topic(request: ManagerTopicCommand) -> ManagerTopicCommand:
            response = await self._manager_request(
                request,
                session=session,
                route=route, person=person,
            )
            if not isinstance(response, ManagerTopicCommand):
                raise TypeError("Manager returned the wrong topic result type.")
            return response

        command_name = parts[0].lower()
        if command_name == "/topics":
            if len(parts) == 1:
                response = await request_topic(
                    ManagerTopicCommand(action="list_tags")
                )
                if not response.success:
                    return (
                        session.session_key,
                        f"Topic command failed: {response.message}",
                    )
                if not response.known_tags:
                    return session.session_key, "No topic tags found."
                return session.session_key, "\n".join(
                    ["Topic tags:"]
                    + [
                        f"- {item['tag']} ({item['thread_count']} thread(s))"
                        for item in response.known_tags
                    ]
                )
            tag = _normalize_topic_tag(" ".join(parts[1:]))
            response = await request_topic(
                ManagerTopicCommand(
                    action="find_threads",
                    tags=[tag],
                )
            )
            if not response.success:
                return session.session_key, f"Topic command failed: {response.message}"
            return session.session_key, _render_topic_matches(
                tag,
                response.thread_matches,
                timezone_name=timezone_name,
            )
        if command_name == "/tag_thread":
            private = "--private" in {part.lower() for part in parts[1:]}
            tag_text = " ".join(
                part for part in parts[1:] if part.lower() != "--private"
            ).strip()
            if not tag_text:
                return session_key, "Usage: /tag_thread <tag[,tag]> [--private]"
            tags = [
                _normalize_topic_tag(tag)
                for chunk in tag_text.split(",")
                for tag in [chunk.strip()]
                if tag
            ]
            response = await request_topic(
                ManagerTopicCommand(
                    action="tag_thread",
                    conversation_id=session.conversation_id,
                    tags=tags,
                    private=private,
                )
            )
            if not response.success:
                return session.session_key, f"Topic tagging failed: {response.message}"
            return session.session_key, (
                f"Tagged thread {session.conversation_id}: "
                f"{', '.join(response.applied_tags)}"
            )
        if command_name == "/switch_topic":
            tag_text = " ".join(parts[1:]).strip()
            if not tag_text:
                return session_key, "Usage: /switch_topic <tag>"
            tag = _normalize_topic_tag(tag_text)
            response = await request_topic(
                ManagerTopicCommand(
                    action="find_threads",
                    tags=[tag],
                )
            )
            if not response.success:
                return session.session_key, f"Topic command failed: {response.message}"
            if not response.thread_matches:
                return session_key, f"No threads matched topic: {tag}"
            if len(response.thread_matches) > 1:
                return session_key, (
                    f"Multiple threads matched topic {tag}; choose one explicitly "
                    "by continuing in the desired thread.\n"
                    + _render_topic_matches(
                        tag,
                        response.thread_matches,
                        timezone_name=timezone_name,
                    )
                )
            target = response.thread_matches[0]
            updated = self._sessions.switch_conversation(
                session_key=session.session_key,
                conversation_id=target["conversation_id"],
                last_task_id=target.get("latest_task_id"),
            )
            if updated is None:
                return session.session_key, (
                    f"Could not switch to topic thread {target['conversation_id']}."
                )
            return session.session_key, with_parallel_mark_notice(
                f"Switched to topic {tag}: conversation {target['conversation_id']}",
                before=session,
                after=updated,
            )
        return session_key, _TOPIC_USAGE


_TOPIC_USAGE = (
    "Usage: /topics [tag], /tag_thread <tag[,tag]> [--private], /switch_topic <tag>"
)


def _normalize_topic_tag(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not normalized:
        raise ValueError("Topic tag must contain at least one letter or number.")
    return normalized


def _render_topic_matches(
    tag: str,
    matches: list[dict[str, object]],
    *,
    timezone_name: str,
) -> str:
    if not matches:
        return f"No threads matched topic: {tag}"
    lines = [f"Threads tagged {tag}:"]
    for index, item in enumerate(matches, start=1):
        preview = str(item.get("subject_preview") or "-").replace("\n", " ").strip()
        if len(preview) > 100:
            preview = preview[:97] + "..."
        updated_at = format_user_timestamp(
            item.get("updated_at"),
            timezone_name=timezone_name,
        )
        lines.append(
            f"{index}. {item.get('conversation_id')} | "
            f"updated={updated_at} | preview={preview}"
        )
    return "\n".join(lines)

