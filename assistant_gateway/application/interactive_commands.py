"""Interactive command use cases owned by the gateway application layer."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from assistant_gateway.application.commands import (
    AccessCommandRequest,
    AccountLinkCommandRequest,
    AccountSharingCommandRequest,
    AuthenticationCommandRequest,
    DEBUG_ONLY_COMMAND_NOTICE,
    GatewayAccessCommands,
    GatewayAccountLinkCommands,
    GatewayAccountSharingCommands,
    GatewayActorContext,
    GatewayAuthenticationCommands,
    GatewayIdentityCommands,
    GatewayManagerControlCommands,
    GatewayMemoryTopicCommands,
    GatewayMessageSubmissionCommands,
    GatewaySessionCommands,
    IdentityCommandRequest,
    ManagerControlCommandRequest,
    MemoryTopicCommandRequest,
    MessageSubmissionCommandRequest,
    SessionCommandRequest,
    is_access_command,
    is_account_link_command,
    is_account_sharing_command,
    is_session_command,
)
from assistant_gateway.application.gateway_application import GatewayCommandResult
from assistant_gateway.application.transient_presentation import GatewayTransientAction
from assistant_gateway.application.command_help import (
    build_interactive_session_help,
)

LOGGER = logging.getLogger(__name__)

_REPLY_CLI_ONLY_NOTICE = "The /reply command is available only in the local CLI."


@dataclass(frozen=True)
class GatewayCommandSettings:
    """Composition-owned values needed by command rendering."""

    docs_base_url: str


@dataclass(frozen=True)
class GatewayCommandRequest:
    """Normalized input for one interactive command execution."""

    raw_text: str
    current_session_key: Optional[str]
    channel_name: Optional[str] = None
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    canonical_user_id: Optional[str] = None
    help_text: Optional[str] = None
    enable_debug_commands: bool = True
    enable_reply_command: bool = True
    enable_channel_binding_commands: bool = True
    query_handler: Optional[object] = None
    # The command's id in its chat (Web Chat's request id). /link tags the
    # message that says how its page ended with it (link_results).
    correlation_id: Optional[str] = None


@runtime_checkable
class GatewayCommandApplication(Protocol):
    """Execute typed interactive commands without exposing gateway internals."""

    async def execute(self, request: GatewayCommandRequest) -> GatewayCommandResult:
        """Execute one normalized command request."""


class GatewayCommandService:
    """Application service that owns interactive command dispatch."""

    def __init__(
        self,
        *,
        settings: GatewayCommandSettings,
        authentication_commands: GatewayAuthenticationCommands,
        account_link_commands: GatewayAccountLinkCommands,
        account_sharing_commands: GatewayAccountSharingCommands,
        access_commands: GatewayAccessCommands,
        identity_commands: GatewayIdentityCommands,
        memory_topic_commands: GatewayMemoryTopicCommands,
        session_commands: GatewaySessionCommands,
        message_submission_commands: GatewayMessageSubmissionCommands,
        manager_control_commands: GatewayManagerControlCommands,
    ) -> None:
        self._settings = settings
        self._authentication_commands = authentication_commands
        self._account_link_commands = account_link_commands
        self._account_sharing_commands = account_sharing_commands
        self._access_commands = access_commands
        self._identity_commands = identity_commands
        self._memory_topic_commands = memory_topic_commands
        self._session_commands = session_commands
        self._message_submission_commands = message_submission_commands
        self._manager_control_commands = manager_control_commands

    async def execute(self, request: GatewayCommandRequest) -> GatewayCommandResult:
        handled, session_key, text = await _dispatch_interactive_command(
            request=request,
            settings=self._settings,
            authentication_commands=self._authentication_commands,
            account_link_commands=self._account_link_commands,
            account_sharing_commands=self._account_sharing_commands,
            access_commands=self._access_commands,
            identity_commands=self._identity_commands,
            memory_topic_commands=self._memory_topic_commands,
            session_commands=self._session_commands,
            message_submission_commands=self._message_submission_commands,
            manager_control_commands=self._manager_control_commands,
        )
        return GatewayCommandResult(
            handled=handled,
            session_key=session_key,
            text=(
                f"{text.description}\n{text.label}: {text.url}"
                if isinstance(text, GatewayTransientAction)
                else text
            ),
            transient_action=text if isinstance(text, GatewayTransientAction) else None,
        )


def _render_docs_link(settings: GatewayCommandSettings) -> str:
    return f"Documentation: {settings.docs_base_url}/docs/"


def _render_config_editor_link(settings: GatewayCommandSettings) -> str:
    return (
        f"Config editor: {settings.docs_base_url}/config-editor\n"
        "This editor is served by the gateway and does not send instance_config.yaml contents to the agent."
    )


def _append_docs_link(text: str, settings: GatewayCommandSettings) -> str:
    docs_link = _render_docs_link(settings)
    if docs_link in text:
        return text
    return f"{text}\n\n{docs_link}"


def parse_reply_command(raw_text: str) -> tuple[Optional[str], Optional[str]]:
    match = re.fullmatch(r"/reply\s+(\S+)\s+(.+)", raw_text.strip(), re.DOTALL)
    if not match:
        return None, None
    return match.group(1), match.group(2).strip()


async def _run_access_command(
    access_commands: GatewayAccessCommands, request: GatewayCommandRequest, command: str
) -> str:
    """Run ``/access``; an unexpected failure is logged and reported plainly."""

    try:
        return await access_commands.execute(
            AccessCommandRequest(
                command=command,
                actor=GatewayActorContext(
                    channel_name=request.channel_name,
                    user_id=request.user_id,
                    chat_id=request.chat_id,
                    thread_id=request.thread_id,
                    canonical_user_id=request.canonical_user_id,
                ),
            )
        )
    except Exception:
        LOGGER.warning("Access command failed. event=access_command_failed", exc_info=True)
        return (
            "The access command did not finish because of an unexpected gateway "
            "error. If you saw no confirmation, nothing changed; try again later."
        )


async def _run_account_sharing_command(
    commands: GatewayAccountSharingCommands, request: GatewayCommandRequest, command: str
) -> str:
    """Run ``/accounts``; an unexpected failure is logged and reported plainly."""

    try:
        return await commands.execute(
            AccountSharingCommandRequest(
                command=command,
                actor=GatewayActorContext(
                    channel_name=request.channel_name,
                    user_id=request.user_id,
                    chat_id=request.chat_id,
                    thread_id=request.thread_id,
                    canonical_user_id=request.canonical_user_id,
                ),
            )
        )
    except Exception:
        # The command holds only its words and a tool id or "model", never a secret.
        LOGGER.warning(
            "Account sharing command failed. event=account_sharing_command_failed person_id=%s channel=%s command=%s",
            request.canonical_user_id,
            request.channel_name,
            command[:200],
            exc_info=True,
        )
        return (
            "The accounts command did not finish because of an unexpected gateway error. "
            "If you saw no confirmation, nothing changed; try again later."
        )


async def _dispatch_interactive_command(
    *,
    request: GatewayCommandRequest,
    settings: GatewayCommandSettings,
    authentication_commands: GatewayAuthenticationCommands,
    account_link_commands: GatewayAccountLinkCommands,
    account_sharing_commands: GatewayAccountSharingCommands,
    access_commands: GatewayAccessCommands,
    identity_commands: GatewayIdentityCommands,
    memory_topic_commands: GatewayMemoryTopicCommands,
    session_commands: GatewaySessionCommands,
    message_submission_commands: GatewayMessageSubmissionCommands,
    manager_control_commands: GatewayManagerControlCommands,
) -> tuple[bool, Optional[str], str | GatewayTransientAction | None]:
    raw_text = request.raw_text
    current_session_key = request.current_session_key
    channel_name = request.channel_name
    user_id = request.user_id
    chat_id = request.chat_id
    thread_id = request.thread_id
    canonical_user_id = request.canonical_user_id
    help_text = request.help_text
    enable_debug_commands = request.enable_debug_commands
    enable_reply_command = request.enable_reply_command
    enable_channel_binding_commands = request.enable_channel_binding_commands
    command = raw_text.strip()
    if not command.startswith("/"):
        return False, current_session_key, None

    LOGGER.info(
        "Interactive session command received. event=session_command_received "
        "command=%s session_key=%s channel_name=%s user_id=%s chat_id=%s thread_id=%s",
        command.split(maxsplit=1)[0],
        current_session_key,
        channel_name,
        user_id,
        chat_id,
        thread_id,
    )

    if command == "/help":
        return (
            True,
            current_session_key,
            _append_docs_link(
                help_text or build_interactive_session_help(),
                settings,
            ),
        )

    if command == "/docs":
        return True, current_session_key, _render_docs_link(settings)

    if command == "/tour":
        submission_result = await message_submission_commands.execute(
            MessageSubmissionCommandRequest(
                command=command,
                current_session_key=current_session_key,
                actor=GatewayActorContext(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    canonical_user_id=canonical_user_id,
                ),
                query_handler=request.query_handler,
            )
        )
        return True, submission_result.session_key, submission_result.text

    if command == "/edit" or command.startswith("/edit "):
        if not enable_debug_commands:
            return True, current_session_key, DEBUG_ONLY_COMMAND_NOTICE
        parts = command.split()
        if len(parts) == 2 and parts[1].lower() == "config":
            return True, current_session_key, _render_config_editor_link(settings)
        return True, current_session_key, "Usage: /edit config"

    if command == "/auth" or command.startswith("/auth "):
        try:
            message = await authentication_commands.execute(
                AuthenticationCommandRequest(
                    command=command,
                    actor=GatewayActorContext(
                        channel_name=channel_name,
                        user_id=user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        canonical_user_id=canonical_user_id,
                    ),
                    session_key=current_session_key,
                    correlation_id=request.correlation_id,
                )
            )
        except Exception as exc:
            message = f"Auth command failed: {exc}"
        return True, current_session_key, message

    if is_account_link_command(command):
        try:
            account_link_message = await account_link_commands.execute(
                AccountLinkCommandRequest(
                    command=command,
                    actor=GatewayActorContext(
                        channel_name=channel_name,
                        user_id=user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        canonical_user_id=canonical_user_id,
                    ),
                    session_key=current_session_key,
                    correlation_id=request.correlation_id,
                )
            )
        except Exception:
            LOGGER.warning(
                "Account link command failed. event=account_link_command_failed",
                exc_info=True,
            )
            account_link_message = (
                "The account link form could not be opened because of an "
                "unexpected gateway error. Try again later."
            )
        return True, current_session_key, account_link_message

    if is_account_sharing_command(command):
        return True, current_session_key, await _run_account_sharing_command(account_sharing_commands, request, command)

    if is_access_command(command):
        return True, current_session_key, await _run_access_command(access_commands, request, command)

    if command == "/user" or command.startswith("/user "):
        try:
            message = await identity_commands.execute(
                IdentityCommandRequest(
                    command=command,
                    actor=GatewayActorContext(
                        channel_name=channel_name,
                        user_id=user_id,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        canonical_user_id=canonical_user_id,
                    ),
                    channel_binding_enabled=enable_channel_binding_commands,
                )
            )
        except Exception as exc:
            LOGGER.warning(
                "User command failed. event=user_command_failed", exc_info=True
            )
            message = f"User command failed: {exc}"
        return True, current_session_key, message

    if (
        command == "/coremem"
        or command.startswith("/coremem ")
        or command == "/usermem"
        or command.startswith("/usermem ")
        or command == "/switch_topic"
        or command.startswith("/switch_topic ")
        or command == "/topics"
        or command.startswith("/topics ")
        or command.startswith("/tag_thread ")
    ):
        memory_topic_result = await memory_topic_commands.execute(
            MemoryTopicCommandRequest(
                command=command,
                current_session_key=current_session_key,
                actor=GatewayActorContext(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    canonical_user_id=canonical_user_id,
                ),
                debug_commands_enabled=enable_debug_commands,
            )
        )
        return True, memory_topic_result.session_key, memory_topic_result.text

    if (
        command == "/stop"
        or command.startswith("/stop ")
        or command == "/objectives"
        or command == "/schedules"
        or command == "/mcp"
        or command.startswith("/mcp ")
        or command.startswith("/pause ")
        or command.startswith("/resume ")
        or command.startswith("/disable schedule ")
        or command.startswith("/abandon ")
        or command.startswith("/delete objective ")
        or command.startswith("/delete schedule ")
    ):
        control_result = await manager_control_commands.execute(
            ManagerControlCommandRequest(
                command=command,
                current_session_key=current_session_key,
                actor=GatewayActorContext(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    canonical_user_id=canonical_user_id,
                ),
            )
        )
        return True, control_result.session_key, control_result.text

    if is_session_command(command):
        session_result = await session_commands.execute(
            SessionCommandRequest(
                command=command,
                current_session_key=current_session_key,
                actor=GatewayActorContext(
                    channel_name=channel_name,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    canonical_user_id=canonical_user_id,
                ),
                debug_commands_enabled=enable_debug_commands,
            )
        )
        return True, session_result.session_key, session_result.text

    if (
        command == "/reply" or command.startswith("/reply ")
    ) and not enable_reply_command:
        return True, current_session_key, _REPLY_CLI_ONLY_NOTICE

    return False, current_session_key, None
