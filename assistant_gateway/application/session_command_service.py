"""Session and conversation administration command application."""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands import (
    DEBUG_ONLY_COMMAND_NOTICE,
    UNLINKED_USER_NOTICE,
    GatewayActorContext,
    GatewayCommandActorResolver,
    SessionCommandRequest,
    SessionCommandResult,
    resolve_actor_canonical_user_id,
)
from assistant_gateway.application.ports import GatewayPrivateResourceAuthorizer
from assistant_gateway.application.private_resources import (
    PRIVATE_ACTION_READ,
    RESOURCE_GATEWAY_SESSION,
)
from assistant_gateway.domain.sessions import (
    SessionConversationListRecord,
    SessionListRecord,
    SessionRecord,
    SessionSearchRecord,
)
from assistant_gateway.application.session_service import with_parallel_mark_notice
from assistant_gateway.ports.repositories import RequestRepository, SessionRepository
from time_presentation import format_user_timestamp


@runtime_checkable
class GatewaySessionActorPort(GatewayCommandActorResolver, Protocol):
    """Resolve trusted actor identity and presentation settings."""

    def gateway_users_enabled(self) -> bool: ...

    def requester_timezone_for_context(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...


@runtime_checkable
class GatewaySessionLifecyclePort(Protocol):
    """Session lifecycle mutations used by the administration application."""

    def start_new_session(
        self,
        *,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> SessionRecord: ...

    def switch_session_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ) -> SessionRecord | None: ...


class GatewaySessionCommandService:
    """Own persistence-backed session command behavior behind a typed contract."""

    def __init__(
        self,
        *,
        sessions: SessionRepository,
        requests: RequestRepository,
        actors: GatewaySessionActorPort,
        lifecycle: GatewaySessionLifecyclePort,
        authorizer: GatewayPrivateResourceAuthorizer,
        team_id: str,
    ) -> None:
        self._sessions = sessions
        self._requests = requests
        self._actors = actors
        self._lifecycle = lifecycle
        self._authorizer = authorizer
        self._team_id = team_id

    async def execute(self, request: SessionCommandRequest) -> SessionCommandResult:
        command = request.command.strip()
        if command == "/new":
            return self._start_new_session(request)
        if command == "/list_sessions" or command.startswith("/list_sessions "):
            return self._list_sessions(request)
        if command == "/preview_session" or command.startswith("/preview_session "):
            return self._preview_session(request)
        if command == "/search_session" or command.startswith("/search_session "):
            return self._search_sessions(request)
        if command == "/switch_session" or command.startswith("/switch_session "):
            return self._switch_session(request)
        if command == "/list_conversations":
            return self._list_conversations(request)
        if command == "/switch_conversation" or command.startswith(
            "/switch_conversation "
        ):
            return self._switch_conversation(request)
        if command == "/rename_session" or command.startswith("/rename_session "):
            return self._rename_session(request)
        raise ValueError(f"Unsupported session command: {command}")

    def _start_new_session(
        self,
        request: SessionCommandRequest,
    ) -> SessionCommandResult:
        owner = self._owner_for_actor(request.actor)
        if self._owner_required(request.actor, owner):
            return SessionCommandResult(
                request.current_session_key,
                UNLINKED_USER_NOTICE,
            )
        actor = request.actor
        session = self._lifecycle.start_new_session(
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            canonical_user_id=owner,
        )
        return SessionCommandResult(
            session.session_key,
            (
                f"Started new session: {session.session_key}\n"
                f"Active conversation: {session.conversation_id}"
            ),
        )

    def _list_sessions(self, request: SessionCommandRequest) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, timezone_name = guarded
        range_text = request.command[len("/list_sessions") :].strip()
        try:
            start, end = _parse_session_range(range_text)
        except ValueError as exc:
            return SessionCommandResult(
                request.current_session_key,
                f"Usage: /list_sessions [start-end]\n{exc}",
            )
        total = self._sessions.count_sessions(owner_canonical_user_id=owner)
        if total == 0:
            return SessionCommandResult(
                request.current_session_key, "No sessions found."
            )
        sessions = self._sessions.list_sessions_with_preview(
            limit=end - start + 1,
            offset=start - 1,
            owner_canonical_user_id=owner,
        )
        if not sessions:
            return SessionCommandResult(
                request.current_session_key,
                f"No sessions found in range {start}-{end}. Total sessions: {total}",
            )
        lines = [f"Sessions {start}-{min(end, total)} of {total}:"]
        lines.extend(
            _render_session_list_line(
                index,
                session,
                timezone_name=timezone_name,
                current_session_key=request.current_session_key,
            )
            for index, session in enumerate(sessions, start=start)
        )
        if total > end:
            lines.append(
                f"Use /list_sessions {end + 1}-{min(end + 7, total)} for the next page."
            )
        return SessionCommandResult(request.current_session_key, "\n".join(lines))

    def _preview_session(self, request: SessionCommandRequest) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, timezone_name = guarded
        target = _command_argument(request.command, "/preview_session")
        if target is None:
            return SessionCommandResult(
                request.current_session_key,
                "Usage: /preview_session <session|alias>",
            )
        session = self._sessions.resolve_session_reference(
            target,
            owner_canonical_user_id=owner,
        )
        if session is None:
            return SessionCommandResult(
                request.current_session_key,
                f"Session not found: {target}",
            )
        return SessionCommandResult(
            request.current_session_key,
            self._render_session_preview(
                session,
                timezone_name=timezone_name,
            ),
        )

    def _search_sessions(self, request: SessionCommandRequest) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, timezone_name = guarded
        query = _command_argument(request.command, "/search_session")
        if query is None:
            return SessionCommandResult(
                request.current_session_key,
                "Usage: /search_session <text>",
            )
        matches = self._sessions.search_sessions(
            query,
            owner_canonical_user_id=owner,
        )
        if not matches:
            return SessionCommandResult(
                request.current_session_key,
                f"No sessions matched: {query}",
            )
        lines = ["Session matches:"]
        lines.extend(
            _render_session_search_line(match, timezone_name=timezone_name)
            for match in matches
        )
        return SessionCommandResult(request.current_session_key, "\n".join(lines))

    def _switch_session(self, request: SessionCommandRequest) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, _timezone_name = guarded
        target = _command_argument(request.command, "/switch_session")
        if target is None:
            return SessionCommandResult(
                request.current_session_key,
                "Usage: /switch_session <session|alias>",
            )
        session = self._sessions.resolve_session_reference(
            target,
            owner_canonical_user_id=owner,
        )
        if session is None:
            return SessionCommandResult(
                request.current_session_key,
                f"Session not found: {target}",
            )
        actor = request.actor
        if actor.channel_name and actor.user_id and actor.chat_id:
            # Route rebinding remains one session-owned mutation; adapters only
            # supply normalized coordinates and never write bindings directly.
            self._sessions.upsert_session(
                session_key=session.session_key,
                conversation_id=session.conversation_id,
                last_task_id=session.last_task_id,
                manager_agent_id=session.manager_agent_id,
                channel_name=actor.channel_name,
                user_id=actor.user_id,
                chat_id=actor.chat_id,
                thread_id=actor.thread_id,
                owner_canonical_user_id=owner,
            )
            self._sessions.set_active_session_for_channel(
                channel_name=actor.channel_name,
                user_id=actor.user_id,
                chat_id=actor.chat_id,
                thread_id=actor.thread_id,
                session_key=session.session_key,
            )
        return SessionCommandResult(
            session.session_key,
            (
                f"Switched to session: {session.session_key}\n"
                f"Active conversation: {session.conversation_id or '-'}"
            ),
        )

    def _list_conversations(
        self,
        request: SessionCommandRequest,
    ) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, timezone_name = guarded
        session = self._active_owned_session(request.current_session_key, owner)
        if isinstance(session, SessionCommandResult):
            return session
        conversations = self._sessions.list_session_conversations(
            session.session_key,
            limit=50,
        )
        if not conversations:
            return SessionCommandResult(
                request.current_session_key,
                "No conversations recorded for this session.",
            )
        lines = [f"Conversations for {session.session_key}:"]
        lines.extend(
            _render_conversation_list_line(
                index,
                record,
                active_conversation_id=session.conversation_id,
                timezone_name=timezone_name,
            )
            for index, record in enumerate(conversations, start=1)
        )
        return SessionCommandResult(request.current_session_key, "\n".join(lines))

    def _switch_conversation(
        self,
        request: SessionCommandRequest,
    ) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, _timezone_name = guarded
        session = self._active_owned_session(request.current_session_key, owner)
        if isinstance(session, SessionCommandResult):
            return session
        target = _command_argument(request.command, "/switch_conversation")
        if target is None:
            return SessionCommandResult(
                request.current_session_key,
                "Usage: /switch_conversation <conversation_id|index>",
            )
        conversation = self._resolve_conversation_reference(
            session_key=session.session_key,
            reference=target,
        )
        if conversation is None:
            return SessionCommandResult(
                request.current_session_key,
                f"Conversation not found in session {session.session_key}: {target}",
            )
        selected = self._lifecycle.switch_session_conversation(
            session_key=session.session_key,
            conversation_id=conversation.conversation_id,
            last_task_id=conversation.last_task_id,
        )
        if selected is None:
            return SessionCommandResult(
                request.current_session_key,
                f"Failed to activate conversation: {conversation.conversation_id}",
            )
        return SessionCommandResult(
            request.current_session_key,
            with_parallel_mark_notice(
                f"Active conversation: {conversation.conversation_id}",
                before=session,
                after=selected,
            ),
        )

    def _rename_session(self, request: SessionCommandRequest) -> SessionCommandResult:
        guarded = self._guard_debug_and_owner(request)
        if isinstance(guarded, SessionCommandResult):
            return guarded
        owner, _timezone_name = guarded
        parts = request.command.split()
        usage = (
            "Usage: /rename_session <alias> or /rename_session <session|alias> <alias>"
        )
        if len(parts) == 2:
            target_reference = request.current_session_key
            new_alias = parts[1].strip()
        elif len(parts) == 3:
            target_reference = parts[1].strip()
            new_alias = parts[2].strip()
        else:
            return SessionCommandResult(request.current_session_key, usage)
        if not target_reference:
            return SessionCommandResult(
                request.current_session_key,
                "No active session to rename.",
            )
        session = self._sessions.resolve_session_reference(
            target_reference,
            owner_canonical_user_id=owner,
        )
        if session is None:
            return SessionCommandResult(
                request.current_session_key,
                f"Session not found: {target_reference}",
            )
        existing_alias = self._sessions.get_session_by_alias(new_alias)
        if (
            existing_alias is not None
            and existing_alias.session_key != session.session_key
        ):
            return SessionCommandResult(
                request.current_session_key,
                f"Alias already in use: {new_alias}",
            )
        self._sessions.set_session_alias(session.session_key, new_alias)
        return SessionCommandResult(
            request.current_session_key,
            f"Session {session.session_key} aliased as: {new_alias}",
        )

    def _guard_debug_and_owner(
        self,
        request: SessionCommandRequest,
    ) -> tuple[str | None, str] | SessionCommandResult:
        if not request.debug_commands_enabled:
            return SessionCommandResult(
                request.current_session_key,
                DEBUG_ONLY_COMMAND_NOTICE,
            )
        owner = self._owner_for_actor(request.actor)
        if self._owner_required(request.actor, owner):
            return SessionCommandResult(
                request.current_session_key,
                UNLINKED_USER_NOTICE,
            )
        actor = request.actor
        timezone_name = self._actors.requester_timezone_for_context(
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=owner,
        )
        return owner, timezone_name

    def _owner_for_actor(self, actor: GatewayActorContext) -> str | None:
        return resolve_actor_canonical_user_id(actor, self._actors)

    def _owner_required(
        self,
        actor: GatewayActorContext,
        owner_canonical_user_id: str | None,
    ) -> bool:
        return bool(
            self._actors.gateway_users_enabled()
            and actor.channel_name
            and actor.channel_name != "cli"
            and not owner_canonical_user_id
        )

    def _active_owned_session(
        self,
        session_key: str | None,
        owner_canonical_user_id: str | None,
    ) -> SessionRecord | SessionCommandResult:
        if not session_key:
            return SessionCommandResult(session_key, "No active session.")
        session = self._sessions.get_session(session_key)
        if session is None or not self._can_read_session(
            session,
            owner_canonical_user_id,
        ):
            return SessionCommandResult(
                session_key, f"Session not found: {session_key}"
            )
        return session

    def _can_read_session(
        self,
        session: SessionRecord,
        owner_canonical_user_id: str | None,
    ) -> bool:
        if owner_canonical_user_id is None:
            return True
        return self._authorizer.can_access(
            actor_canonical_user_id=owner_canonical_user_id,
            owner_canonical_user_id=session.owner_canonical_user_id,
            resource_type=RESOURCE_GATEWAY_SESSION,
            resource_id=session.session_key,
            action=PRIVATE_ACTION_READ,
            team_id=self._team_id,
        )

    def _render_session_preview(
        self,
        session: SessionRecord,
        *,
        timezone_name: str,
    ) -> str:
        lines = [
            f"Session: {session.session_key}",
            f"Alias: {session.session_alias or '-'}",
            f"Conversation: {session.conversation_id or '-'}",
            f"Last task: {session.last_task_id or '-'}",
            f"Channel: {session.channel_name or '-'}",
            f"User: {session.user_id or '-'}",
            f"Chat: {session.chat_id or '-'}",
            f"Thread: {session.thread_id or '-'}",
            "Updated: "
            f"{format_user_timestamp(session.updated_at, timezone_name=timezone_name)}",
        ]
        recent_requests = self._requests.list_requests_for_session(
            session.session_key,
            limit=5,
        )
        if recent_requests:
            lines.append("Recent requests:")
            for record in recent_requests:
                preview = record.request_text.replace("\n", " ").strip()
                if len(preview) > 120:
                    preview = preview[:117] + "..."
                created_at = format_user_timestamp(
                    record.created_at,
                    timezone_name=timezone_name,
                )
                lines.append(f"- {created_at}: {preview}")
        return "\n".join(lines)

    def _resolve_conversation_reference(
        self,
        *,
        session_key: str,
        reference: str,
    ) -> SessionConversationListRecord | None:
        conversations = self._sessions.list_session_conversations(
            session_key,
            limit=50,
        )
        if reference.isdigit():
            index = int(reference)
            if index < 1 or index > len(conversations):
                return None
            return conversations[index - 1]
        for record in conversations:
            if record.conversation_id == reference:
                return record
        return None


def _command_argument(command: str, command_name: str) -> str | None:
    argument = command[len(command_name) :].strip()
    return argument or None


def _parse_session_range(raw: str) -> tuple[int, int]:
    text = raw.strip()
    if not text:
        return 1, 7
    match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", text)
    if not match:
        raise ValueError("Range must look like 1-7.")
    start = int(match.group(1))
    end = int(match.group(2))
    if start < 1 or end < start:
        raise ValueError("Range must be positive and ascending, e.g. 1-7.")
    return start, end


def _render_session_line(
    session: SessionRecord,
    *,
    timezone_name: str,
    current_session_key: str | None = None,
) -> str:
    current = " *" if session.session_key == current_session_key else ""
    channel_bits = [
        bit
        for bit in (
            session.channel_name,
            session.user_id,
            session.chat_id,
            session.thread_id,
        )
        if bit
    ]
    channel_text = " | ".join(channel_bits) if channel_bits else "no-channel"
    alias_text = f" alias={session.session_alias} |" if session.session_alias else ""
    return (
        f"{session.session_key}{current} |{alias_text} "
        f"conversation={session.conversation_id or '-'} | "
        "updated="
        f"{format_user_timestamp(session.updated_at, timezone_name=timezone_name)} | "
        f"channel={channel_text}"
    )


def _render_session_list_line(
    index: int,
    session: SessionListRecord,
    *,
    timezone_name: str,
    current_session_key: str | None = None,
) -> str:
    current = " *" if session.session_key == current_session_key else ""
    preview = (
        (session.latest_request_text or "no recent requests").replace("\n", " ").strip()
    )
    if len(preview) > 90:
        preview = preview[:87] + "..."
    return (
        f"{index}. {session.session_key}{current}"
        f"{f' ({session.session_alias})' if session.session_alias else ''} | "
        f"conversation={session.conversation_id or '-'} | "
        "updated="
        f"{format_user_timestamp(session.updated_at, timezone_name=timezone_name)} | "
        f"preview={preview}"
    )


def _render_session_search_line(
    record: SessionSearchRecord,
    *,
    timezone_name: str,
) -> str:
    base = _render_session_line(
        SessionRecord(
            session_key=record.session_key,
            session_alias=record.session_alias,
            conversation_id=record.conversation_id,
            last_task_id=record.last_task_id,
            manager_agent_id=record.manager_agent_id,
            channel_name=record.channel_name,
            user_id=record.user_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
        ),
        timezone_name=timezone_name,
    )
    if record.matched_request_text:
        return f"{base}\nmatch: {record.matched_request_text[:120]}"
    return base


def _render_conversation_list_line(
    index: int,
    record: SessionConversationListRecord,
    *,
    active_conversation_id: str | None,
    timezone_name: str,
) -> str:
    active = " *" if record.conversation_id == active_conversation_id else ""
    preview = (
        (record.latest_request_text or "no recent requests").replace("\n", " ").strip()
    )
    if len(preview) > 90:
        preview = preview[:87] + "..."
    return (
        f"{index}. {record.conversation_id}{active} | "
        f"last_task={record.last_task_id or '-'} | "
        "updated="
        f"{format_user_timestamp(record.updated_at, timezone_name=timezone_name)} | "
        f"preview={preview}"
    )
