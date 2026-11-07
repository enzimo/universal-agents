"""Application owners for identity, memory, topic, and session commands."""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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
from assistant_gateway.application.team_commands import (
    GatewayTeamCommandService,
    TeamCommandRefused,
    TeamServicesUnavailable,
)
from custom_messages.contracts.control_v1 import (
    ManagerTopicCommand,
    PinnedMemoryCommandPayload,
    PinnedMemoryCommandResultPayload,
)
from time_presentation import format_user_timestamp


class GatewayPinnedMemoryCodec(Protocol):
    """The memory command grammar, the contract's bodies and the chat rendering, kept out of the application.

    ``assistant_gateway.pinned_memory_commands.RuntimePinnedMemoryCodec``.
    """

    def parse(self, command: str, *, bank: str) -> PinnedMemoryCommandPayload: ...

    def wire_command(
        self, command: PinnedMemoryCommandPayload, *, timezone_name: str | None
    ) -> tuple[str, dict[str, Any]]: ...

    def result(
        self, command: PinnedMemoryCommandPayload, reply: dict[str, Any], *, team_id: str, person_id: str
    ) -> PinnedMemoryCommandResultPayload: ...

    def render(self, result: PinnedMemoryCommandResultPayload, *, timezone_name: str | None) -> str: ...


# Refusals that name something wrong with what the person asked for.
_MEMORY_INPUT_OUTCOMES = frozenset({"not_found", "invalid_request", "capacity_reached"})


class GatewayMemoryRefused(Exception):
    """The team services answered a memory command with a refusal outcome and its message."""

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message


@dataclass(frozen=True)
class GatewayCommandOperationsSettings:
    """Stable routing values needed by command use cases."""

    default_session_key: str
    default_owner_canonical_user_id: str
    manager_agent_id: str | None


@dataclass(frozen=True)
class GatewayPinnedMemoryOperationResult:
    """The team services' typed result plus gateway-authorized browser actions."""

    result: PinnedMemoryCommandResultPayload
    available_actions: tuple[Literal["edit", "delete"], ...]
    can_create: bool = False


class GatewayPinnedMemoryConflictError(ValueError):
    """The team services refused a change based on an outdated memory version (``changed_since_read``)."""


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
        team_commands: GatewayTeamCommandService,
        pinned_memory: GatewayPinnedMemoryCodec,
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
        # People's memory commands go to the team services (team_commands).
        self._team_commands = team_commands
        self._pinned_memory = pinned_memory
        self._private_authorizer = private_authorizer
        self._logger = logging.getLogger(__name__)

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
        command: ManagerTopicCommand,
        *,
        session: SessionRecord,
        route: GatewayRequestRoute,
        person: GatewayCommandPerson,
    ) -> ManagerTopicCommand:
        result = await self._manager_commands.request(
            GatewayManagerCommandRequest(
                command=command,
                session=session,
                person=person,
                route=route,
            )
        )
        if not isinstance(result, ManagerTopicCommand):
            raise TypeError("Manager returned the wrong command result type.")
        return result

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
        """Run a chat's /coremem or /usermem command at the team services and render the reply."""

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
            principal = self._principal(session, route)
            parsed = self._pinned_memory.parse(command, bank=bank)
            timezone_name = self._person_timezone(session, route, principal)
            result = await self._memory_operation(parsed, principal=principal, timezone_name=timezone_name)
            return self._pinned_memory.render(result, timezone_name=timezone_name)
        except GatewayMemoryRefused as refused:
            return refused.message
        except PermissionError:
            return "Use /user first to register this channel before managing pinned memory."
        except (TeamServicesUnavailable, TeamCommandRefused, ValueError) as exc:
            # Written for the person: the team services' state, or what was wrong with the command.
            return str(exc)
        except Exception:
            self._logger.exception(
                "A memory command failed in the gateway. event=gateway_memory_command_failed bank=%s", bank
            )
            return (
                "The memory command failed because of an error in the gateway. An administrator can find it "
                "in the gateway log."
            )

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
        """Execute one typed Settings operation at the team services."""

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
        try:
            result = await self._memory_operation(
                command, principal=principal, timezone_name=self._person_timezone(session, route, principal)
            )
        except GatewayMemoryRefused as refused:
            if refused.outcome == "changed_since_read":
                raise GatewayPinnedMemoryConflictError(refused.message) from refused
            if refused.outcome == "not_allowed":
                raise PermissionError(refused.message) from refused
            if refused.outcome in _MEMORY_INPUT_OUTCOMES:
                raise ValueError(refused.message) from refused
            raise RuntimeError(refused.message) from refused
        return GatewayPinnedMemoryOperationResult(
            result=result,
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

    async def _memory_operation(
        self,
        command: PinnedMemoryCommandPayload,
        *,
        principal: GatewayCommandPerson,
        timezone_name: str | None,
    ) -> PinnedMemoryCommandResultPayload:
        """Send one memory command for the signed-in person; the team services decide and answer."""

        kind, body = self._pinned_memory.wire_command(command, timezone_name=timezone_name)
        reply = await self._team_commands.request(kind, body, person_id=principal.canonical_user_id)
        if reply.get("outcome") != "ok":
            raise GatewayMemoryRefused(str(reply.get("outcome")), str(reply.get("message") or "Refused."))
        return self._pinned_memory.result(
            command, reply, team_id=principal.team_id, person_id=principal.canonical_user_id
        )

    def _person_timezone(
        self, session: SessionRecord, route: GatewayRequestRoute, principal: GatewayCommandPerson
    ) -> str | None:
        """The person's time zone, for times they write and times shown to them."""

        return self._request_context.requester_presentation_for_session(
            session,
            GatewayRequestRoute(
                channel_name=route.channel_name or session.channel_name,
                user_id=route.user_id or session.user_id,
                chat_id=route.chat_id or session.chat_id,
                thread_id=route.thread_id or session.thread_id,
                canonical_user_id=principal.canonical_user_id,
            ),
        ).timezone

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

