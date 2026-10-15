"""Gateway identity administration command handler."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.command_help import (
    USER_HELP_TEXT,
    USER_REMOVE_USAGE,
    USER_RESTORE_USAGE,
    USER_ROLE_USAGE,
    USER_TEAM_KEYS_USAGE,
)
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.person_setup import (
    OWN_KEYS_OPTION,
    SETUP_OPTIONS_USAGE,
    SHARED_OPTION,
    TEAM_KEYS_OPTION,
    PersonSetupChoices,
    SetupOptionProblem,
    take_setup_options,
)

_CHANNEL_BINDING_COMMAND_NOTICE = (
    "This /user command links an identity on the chat platform where it is "
    "run, so it is not available in Web Chat. Run it in Telegram, Discord, "
    "WhatsApp, Signal, or Photon/iMessage for the identity you want to link. "
    'Use /user invite --new "Display Name" to onboard a Web Chat-only user.'
)
_SETUP_OPTIONS_ONLY_WHEN_ADDING = (
    f"{TEAM_KEYS_OPTION}, {OWN_KEYS_OPTION} and {SHARED_OPTION} apply only when adding a person "
    '(/user <external_user_id> "Name" or /user invite --new "Name"). Use /user team-keys <usr_...> on|off later.'
)
# /user actions that do not add a person, so the setup options do not apply to them.
_NOT_ADDING_ACTIONS = frozenset(
    {
        "help", "--help", "status", "whoami", "identity", "list", "users", "ls", "enable", "disable",
        "role", "team-keys", "remove", "restore", "token", "code", "link", "default-channel",
    }
)
_TEAM_KEYS_WORDS = {"on": True, "off": False}
_WEB_CHAT_USER_HELP_NOTE = (
    "\n- In Web Chat, use invite/list/enable/disable/remove/restore/role for administration and "
    "status/token/code for the authenticated user. Direct external-id "
    "registration and /user link must be run on the external chat platform "
    "being linked."
)


@runtime_checkable
class GatewayIdentityCommandPort(Protocol):
    """Identity use cases exposed to the ``/user`` command handler."""

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
    ) -> str: ...

    def render_new_user_status(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    def render_gateway_user_list(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        status: str | None = None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    async def set_gateway_user_enabled_status(
        self,
        *,
        target: str,
        enabled: bool,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    def set_gateway_user_role(
        self,
        *,
        target: str,
        role: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    async def remove_gateway_user(
        self,
        *,
        target: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    async def restore_gateway_user(
        self,
        *,
        target: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    def set_gateway_user_team_keys(
        self,
        *,
        target: str,
        allowed: bool,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    def create_new_user_web_token(
        self,
        *,
        label: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

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
    ) -> str: ...

    def create_new_user_link_code(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        canonical_user_id: str | None = None,
    ) -> str: ...

    def link_new_user_code_for_channel(
        self,
        *,
        code: str,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        replace: bool = False,
    ) -> str: ...

    def set_new_user_default_channel(
        self,
        *,
        channel_name: str | None,
        user_id: str | None,
        chat_id: str | None,
        thread_id: str | None,
        clear: bool = False,
        canonical_user_id: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class IdentityCommandRequest:
    """Input for one ``/user`` command."""

    command: str
    actor: GatewayActorContext
    channel_binding_enabled: bool = True


@runtime_checkable
class GatewayIdentityCommands(Protocol):
    """Execute typed gateway-identity commands."""

    async def execute(self, request: IdentityCommandRequest) -> str:
        """Execute one identity command."""


class IdentityCommandHandler:
    """Execute ``/user`` through a narrow identity use-case port."""

    def __init__(self, operations: GatewayIdentityCommandPort) -> None:
        self._operations = operations

    async def execute(self, request: IdentityCommandRequest) -> str:
        try:
            parts, choices = take_setup_options(_parse_user_parts(request.command))
        except SetupOptionProblem as exc:
            return str(exc)
        actor = request.actor
        if len(parts) == 1:
            if not request.channel_binding_enabled:
                return _CHANNEL_BINDING_COMMAND_NOTICE
            return await self._register(
                actor=actor,
                display_name=None,
                replace=False,
                choices=choices,
            )

        action = parts[1].lower()
        if choices != PersonSetupChoices() and action in _NOT_ADDING_ACTIONS:
            return _SETUP_OPTIONS_ONLY_WHEN_ADDING
        if action == "--replace":
            if not request.channel_binding_enabled:
                return _CHANNEL_BINDING_COMMAND_NOTICE
            target_user_id, display_name = _parse_user_target_and_name(parts, 2)
            return await self._register(
                actor=actor,
                display_name=display_name,
                target_user_id=target_user_id,
                replace=True,
                choices=choices,
            )
        if action in {"help", "--help"}:
            if request.channel_binding_enabled:
                return str(USER_HELP_TEXT)
            return f"{USER_HELP_TEXT}{_WEB_CHAT_USER_HELP_NOTE}"
        if action in {
            "status",
            "whoami",
            "identity",
            "list",
            "users",
            "ls",
            "enable",
            "disable",
        }:
            return await self._execute_administration_action(
                action=action,
                parts=parts,
                actor=actor,
            )
        if action == "role":
            return self._set_role(parts=parts, actor=actor)
        if action == "team-keys":
            return self._set_team_keys(parts=parts, actor=actor)
        if action in {"remove", "restore"}:
            return await self._remove_or_restore(action=action, parts=parts, actor=actor)
        if action in {"token", "invite", "code"}:
            return await self._execute_web_access_action(
                action=action,
                parts=parts,
                actor=actor,
                choices=choices,
            )
        if action in {"link", "default-channel"}:
            return self._execute_binding_action(
                action=action,
                parts=parts,
                actor=actor,
                channel_binding_enabled=request.channel_binding_enabled,
            )
        if not request.channel_binding_enabled:
            return _CHANNEL_BINDING_COMMAND_NOTICE
        target_user_id, display_name = _parse_user_target_and_name(parts, 1)
        return await self._register(
            actor=actor,
            display_name=display_name,
            target_user_id=target_user_id,
            replace=False,
            choices=choices,
        )

    async def _execute_administration_action(
        self,
        *,
        action: str,
        parts: list[str],
        actor: GatewayActorContext,
    ) -> str:
        if action in {"status", "whoami", "identity"}:
            return self._operations.render_new_user_status(
                channel_name=actor.channel_name,
                user_id=actor.user_id,
                chat_id=actor.chat_id,
                canonical_user_id=actor.canonical_user_id,
            )
        if action in {"list", "users", "ls"}:
            return self._operations.render_gateway_user_list(
                channel_name=actor.channel_name,
                user_id=actor.user_id,
                chat_id=actor.chat_id,
                status=parts[2] if len(parts) > 2 else None,
                canonical_user_id=actor.canonical_user_id,
            )
        if len(parts) < 3:
            return (
                f"Usage: /user {action} "
                "<canonical_user_id|current_channel_external_user_id>"
            )
        return await self._operations.set_gateway_user_enabled_status(
            target=parts[2],
            enabled=action == "enable",
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
        )

    def _set_role(self, *, parts: list[str], actor: GatewayActorContext) -> str:
        if len(parts) != 4:
            return USER_ROLE_USAGE
        return self._operations.set_gateway_user_role(
            target=parts[2],
            role=parts[3],
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
        )

    def _set_team_keys(self, *, parts: list[str], actor: GatewayActorContext) -> str:
        allowed = _TEAM_KEYS_WORDS.get(parts[3].lower()) if len(parts) == 4 else None
        if allowed is None:
            return USER_TEAM_KEYS_USAGE
        return self._operations.set_gateway_user_team_keys(
            target=parts[2],
            allowed=allowed,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
        )

    async def _remove_or_restore(
        self, *, action: str, parts: list[str], actor: GatewayActorContext
    ) -> str:
        if len(parts) != 3:
            return USER_REMOVE_USAGE if action == "remove" else USER_RESTORE_USAGE
        operation = (
            self._operations.remove_gateway_user
            if action == "remove"
            else self._operations.restore_gateway_user
        )
        return await operation(
            target=parts[2],
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
        )

    async def _execute_web_access_action(
        self,
        *,
        action: str,
        parts: list[str],
        actor: GatewayActorContext,
        choices: PersonSetupChoices,
    ) -> str:
        if action == "token":
            return self._operations.create_new_user_web_token(
                label=_join_display_name(parts, 2),
                channel_name=actor.channel_name,
                user_id=actor.user_id,
                chat_id=actor.chat_id,
                canonical_user_id=actor.canonical_user_id,
            )
        if action == "invite":
            return await self._invite(parts=parts, actor=actor, choices=choices)
        return self._operations.create_new_user_link_code(
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
        )

    def _execute_binding_action(
        self,
        *,
        action: str,
        parts: list[str],
        actor: GatewayActorContext,
        channel_binding_enabled: bool,
    ) -> str:
        if action == "link":
            if not channel_binding_enabled:
                return _CHANNEL_BINDING_COMMAND_NOTICE
            if len(parts) < 3:
                return "Usage: /user link <code> [--replace]"
            replace = "--replace" in {part.lower() for part in parts[3:]}
            return self._operations.link_new_user_code_for_channel(
                code=parts[2],
                channel_name=actor.channel_name,
                user_id=actor.user_id,
                chat_id=actor.chat_id,
                thread_id=actor.thread_id,
                replace=replace,
            )
        return self._set_default_channel(parts=parts, actor=actor)

    async def _register(
        self,
        *,
        actor: GatewayActorContext,
        display_name: str | None,
        target_user_id: str | None = None,
        replace: bool,
        choices: PersonSetupChoices,
    ) -> str:
        return await self._operations.register_new_user_for_channel(
            display_name=display_name,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            target_user_id=target_user_id,
            replace=replace,
            canonical_user_id=actor.canonical_user_id,
            choices=choices,
        )

    async def _invite(self, *, parts: list[str], actor: GatewayActorContext, choices: PersonSetupChoices) -> str:
        usage = (
            "Usage: /user invite <canonical_user_id> "
            f'[--label "Initial browser"] {SETUP_OPTIONS_USAGE} or /user invite --new '
            f'"Display Name" [--label "Initial browser"] {SETUP_OPTIONS_USAGE}'
        )
        invite_parts = parts[2:]
        label = "Initial browser"
        if "--label" in invite_parts:
            label_index = invite_parts.index("--label")
            if label_index + 1 >= len(invite_parts):
                return usage
            label = invite_parts[label_index + 1]
            del invite_parts[label_index : label_index + 2]
        create_new = bool(invite_parts and invite_parts[0].lower() == "--new")
        if create_new:
            if len(invite_parts) != 2:
                return usage
            target = None
            display_name = invite_parts[1]
        else:
            if len(invite_parts) != 1:
                return usage
            target = invite_parts[0]
            display_name = None
        return await self._operations.create_new_user_web_invite(
            target=target,
            display_name=display_name,
            label=label,
            create_new=create_new,
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
            choices=choices,
        )

    def _set_default_channel(
        self,
        *,
        parts: list[str],
        actor: GatewayActorContext,
    ) -> str:
        target = parts[2].lower() if len(parts) > 2 else ""
        if target not in {"current", "clear"}:
            return "Usage: /user default-channel current|clear"
        return self._operations.set_new_user_default_channel(
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            clear=target == "clear",
            canonical_user_id=actor.canonical_user_id,
        )


def _parse_user_parts(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Could not parse /user command: {exc}") from exc


def _join_display_name(parts: list[str], start: int = 1) -> str:
    return " ".join(part for part in parts[start:] if part).strip()


def _user_arg_is_target(value: str, *, has_display_name: bool) -> bool:
    token = str(value or "").strip()
    if not token:
        return False
    if token.startswith(("@", "+")):
        return True
    if ":" in token:
        prefix = token.split(":", 1)[0].lower()
        if prefix in {
            "discord",
            "telegram",
            "whatsapp",
            "signal",
            "photon",
            "phone",
            "tel",
        }:
            return True
    return has_display_name


def _parse_user_target_and_name(
    parts: list[str], start_index: int
) -> tuple[str | None, str | None]:
    args = parts[start_index:]
    if not args:
        return None, None
    first = args[0]
    if _user_arg_is_target(first, has_display_name=len(args) > 1):
        return first, _join_display_name(args, 1) or None
    return None, _join_display_name(args, 0) or None
