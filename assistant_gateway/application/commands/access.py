"""The ``/access`` chat command: people give, remove, block and review agents' access.

It acts as the signed-in person with their current team roles, within their
own authority, through the same helpers as ``assistant_cli access``
(``access_control.operator_commands``, reached through
``GatewayAccessCommandPort``; the gateway's implementation is
``assistant_gateway.access_commands``). Answers are plain text. With
authorization off (trusted single-person mode) nothing needs giving, so every
subcommand except help says so.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.commands.context import (
    GatewayActorContext,
    GatewayCommandActorResolver,
    resolve_actor_canonical_user_id,
)

ACCESS_COMMAND = "/access"
# Sharing files between people (decision D15) runs through the same handler.
SHARE_COMMAND = "/share"
UNSHARE_COMMAND = "/unshare"
SHARES_COMMAND = "/shares"
SEND_COMMAND = "/send"
FILE_SHARING_COMMANDS = (SHARE_COMMAND, UNSHARE_COMMAND, SHARES_COMMAND, SEND_COMMAND)
FILE_SHARING_HELP_TEXT = "\n".join(
    (
        "Sharing files with people in your team:",
        "- /share <file or folder in your folder> with <person> [read|write] [--for 7d] - let them, and "
        "agents working for them, read (or read and change) it.",
        "- /unshare <share id> - stop sharing. /shares - what you share and what is shared with you.",
        "- /send <file> to <person> [--move] - put a copy in their received/ folder once they (or an "
        "administrator) approve it; /send <file> to team/<folder> [--move] - copy it to the team folder at once.",
    )
)
ACCESS_HELP_TEXT = "\n".join(
    (
        "Access to tools and folders (/access):",
        "- /access list [name] - grants and blocks, optionally only those about one agent type, agent, "
        "person, tool or tool set. Administrators see everyone's; members see agents' access and their own.",
        "- /access grant <who> <tool|tool set|folder> <read|write|full> [--for 7d] "
        "[--delegate-depth 1] [--delegate-mode automatic|ask_owner] [--everyone] [--reason \"why\"] - give "
        "access you are allowed to give: a level on a whole tool, never one operation. What you give agents "
        "serves your own work: when they work for someone else, that person decides. Give agents' access to "
        "the team lead (with --delegate-depth 1): workers get it for each task from there. Administrators may "
        "add --everyone, which a worker type may also hold.",
        "- /access revoke <grant id> - remove all of that agent's access to that tool, and everything handed "
        "down from it.",
        "- /access block <who> <tool|tool#operation|tool set|folder> --reason \"why\" "
        "[--hours 24 | --replace <stale block id>] - stop "
        "an agent type, agent, person, role or the whole team from using a tool, one operation, a tool set "
        "or a folder, whatever grants exist. It applies from the next action. Internet access is on for "
        "everyone; /access block team internet --reason \"...\" turns it off. --replace swaps a block on a "
        "removed operation for a current operation of the same tool and subject in one step, keeping its expiry.",
        "- /access unblock <block id> - remove a block (the person who added it, or an owner or administrator).",
        "- /access requests - agents' access requests waiting for your decision, with the Inbox link.",
        "- /access profile [trusted|careful|manual] - how much TeamLead may do for your own work without "
        "asking you each time (it can hand that on to workers); without a name, shows your choice.",
        "- /access folders - data folders (folders outside the workspace agents can be given access to). "
        "Owners and administrators: /access folders add <name> <absolute path> [--read-only] "
        "[--reason \"why\"], /access folders remove <name>. Agents name them data/<name>.",
        "<who> is agent-type:<Type>, agent:<id>, user:<person id>, role:<name> or team. Only owners and "
        "administrators block people, roles or the team. A folder is "
        "named below team/, agents/ or data/<name>/, such as data/reports/2026 (or folder:team).",
        "Examples: /access grant agent-type:BackgroundOperationsAgent cli:google read --for 7d",
        "/access grant agent-type:TeamArchitect fn:history_cleanup full - let TeamLead clean up your own chat history "
        "without asking each time (only you can give it, and it is never handed on; /access revoke ends it).",
        "/access block agent-type:BackgroundOperationsAgent cli:google --reason \"Sending too much mail\"",
    )
)
ACCESS_AUTHORIZATION_OFF_NOTICE = (
    "Access control is off on this instance (trusted single-person mode): agents "
    "use every tool they have without asking, so there is nothing to give, block "
    "or decide. Turning on authorization.enabled in instance_config.yaml makes "
    "these commands take effect."
)
_UNREGISTERED_NOTICE = "Access commands are available only to registered users."
_FLAGS_WITH_VALUES = frozenset({"--for", "--delegate-depth", "--delegate-mode", "--reason", "--hours", "--replace"})
_FLAGS_WITHOUT_VALUES = frozenset({"--everyone", "--read-only", "--move"})


class AccessCommandUsageError(ValueError):
    """The command's words do not match its usage; the message says how."""


@dataclass(frozen=True)
class AccessGrantInput:
    to: str
    target: str
    level: str
    duration: str | None = None
    delegate_depth: int = 0
    delegate_mode: str = "automatic"
    reason: str = ""
    everyone: bool = False


@dataclass(frozen=True)
class DataFolderInput:
    name: str
    path: str
    writable: bool = True
    reason: str = ""


@dataclass(frozen=True)
class AccessBlockInput:
    subject: str
    target: str
    reason: str
    hours: int | None = None
    replace_block_id: str | None = None


@runtime_checkable
class GatewayAccessCommandPort(Protocol):
    """Access operations for one signed-in person; each returns the plain-text answer."""

    def authorization_enabled(self) -> bool: ...

    async def list_access(self, person_id: str, *, match: str | None) -> str: ...

    async def grant(self, person_id: str, grant: AccessGrantInput) -> str: ...

    async def revoke(self, person_id: str, grant_id: str) -> str: ...

    async def block(self, person_id: str, block: AccessBlockInput) -> str: ...

    async def unblock(self, person_id: str, grant_id: str) -> str: ...

    async def waiting_requests(self, person_id: str) -> str: ...

    async def list_data_folders(self, person_id: str) -> str: ...

    async def starter_profile(self, person_id: str, name: str | None) -> str: ...

    async def add_data_folder(self, person_id: str, folder: DataFolderInput) -> str: ...

    async def remove_data_folder(self, person_id: str, name: str) -> str: ...

    async def share(self, person_id: str, item: str, recipient: str, level: str, hours: int | None) -> str: ...

    async def unshare(self, person_id: str, share_id: str) -> str: ...

    async def list_shares(self, person_id: str) -> str: ...

    async def send_file(self, person_id: str, item: str, destination: str, move: bool) -> str: ...


@dataclass(frozen=True)
class AccessCommandRequest:
    """Input for one ``/access`` command."""

    command: str
    actor: GatewayActorContext


@runtime_checkable
class GatewayAccessCommands(Protocol):
    """Execute typed ``/access`` commands."""

    async def execute(self, request: AccessCommandRequest) -> str:
        """Execute one access command."""


def is_access_command(command: str) -> bool:
    """``/access`` and the file sharing commands, which the same handler runs."""

    return any(command == name or command.startswith(f"{name} ") for name in (ACCESS_COMMAND, *FILE_SHARING_COMMANDS))


def _split_flags(words: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(words):
        word = words[index]
        if word in _FLAGS_WITHOUT_VALUES:
            flags[word] = "yes"
            index += 1
            continue
        if word.startswith("--"):
            if word not in _FLAGS_WITH_VALUES:
                raise AccessCommandUsageError(f"Unknown option {word}. Use /access help for the options.")
            if index + 1 >= len(words):
                raise AccessCommandUsageError(f"{word} needs a value.")
            flags[word] = words[index + 1]
            index += 2
            continue
        positional.append(word)
        index += 1
    return positional, flags


def _whole_number(flags: dict[str, str], name: str) -> int | None:
    if name not in flags:
        return None
    try:
        return int(flags[name])
    except ValueError as exc:
        raise AccessCommandUsageError(f"{name} takes a whole number, not {flags[name]!r}.") from exc


def parse_grant(words: list[str]) -> AccessGrantInput:
    positional, flags = _split_flags(words)
    if len(positional) != 3 or set(flags) - {"--for", "--delegate-depth", "--delegate-mode", "--reason", "--everyone"}:
        raise AccessCommandUsageError(
            "Usage: /access grant <who> <tool|tool set|folder> <read|write|full> "
            '[--for 7d] [--delegate-depth 1] [--delegate-mode automatic|ask_owner] [--everyone] [--reason "why"]'
        )
    to, target, level = positional
    return AccessGrantInput(
        to=to,
        target=target,
        level=level,
        duration=flags.get("--for"),
        delegate_depth=_whole_number(flags, "--delegate-depth") or 0,
        delegate_mode=flags.get("--delegate-mode", "automatic"),
        reason=flags.get("--reason", ""),
        everyone="--everyone" in flags,
    )


def parse_block(words: list[str]) -> AccessBlockInput:
    positional, flags = _split_flags(words)
    if len(positional) != 2 or not flags.get("--reason", "").strip() or set(flags) - {"--reason", "--hours", "--replace"}:
        raise AccessCommandUsageError(
            "Usage: /access block <agent-type:Type|agent:id|user:id|role:name|team> "
            '<tool|tool#operation|tool set|folder> --reason "why" [--hours 24 | --replace <stale block id>]'
        )
    subject, target = positional
    return AccessBlockInput(
        subject=subject, target=target, reason=flags["--reason"], hours=_whole_number(flags, "--hours"),
        replace_block_id=flags.get("--replace"),
    )


def parse_folders(words: list[str]) -> tuple[str, DataFolderInput | str | None]:
    """``/access folders [list | add <name> <path> [--read-only] [--reason "why"] | remove <name>]``."""

    positional, flags = _split_flags(words)
    action = positional[0].lower() if positional else "list"
    if action == "list" and len(positional) <= 1 and not flags:
        return "list", None
    if action == "add" and len(positional) == 3 and not set(flags) - {"--read-only", "--reason"}:
        return "add", DataFolderInput(
            name=positional[1], path=positional[2], writable="--read-only" not in flags, reason=flags.get("--reason", "")
        )
    if action == "remove" and len(positional) == 2 and not flags:
        return "remove", positional[1]
    raise AccessCommandUsageError(
        'Usage: /access folders [list] | add <name> <absolute path> [--read-only] [--reason "why"] | remove <name>'
    )


def _hours(duration: str | None) -> int | None:
    """``--for 12h`` / ``7d`` as whole hours."""

    if duration is None:
        return None
    text = str(duration).strip().lower()
    unit = text[-1:] if text[-1:] in {"h", "d"} else ""
    try:
        amount = int(text[:-1] if unit else text)
    except ValueError as exc:
        raise AccessCommandUsageError(f"--for takes hours or days, such as 12h or 7d, not {duration!r}.") from exc
    return amount * 24 if unit == "d" else amount


def parse_share(words: list[str]) -> tuple[str, str, str, int | None]:
    """``<item> with <person> [read|write] [--for 7d]``."""

    positional, flags = _split_flags(words)
    usage = "Usage: /share <file or folder> with <person> [read|write] [--for 7d]"
    if "with" not in positional or set(flags) - {"--for"}:
        raise AccessCommandUsageError(usage)
    split = positional.index("with")
    item, rest = " ".join(positional[:split]), positional[split + 1 :]
    level = "read"
    if rest and rest[-1].lower() in {"read", "write", "change"}:
        level = rest[-1].lower()
        rest = rest[:-1]
    if not item or not rest:
        raise AccessCommandUsageError(usage)
    return item, " ".join(rest), level, _hours(flags.get("--for"))


def parse_send(words: list[str]) -> tuple[str, str, bool]:
    """``<file> to <person|team/folder> [--move]``."""

    positional, flags = _split_flags(words)
    if "to" not in positional or set(flags) - {"--move"}:
        raise AccessCommandUsageError("Usage: /send <file> to <person|team/folder> [--move]")
    split = positional.index("to")
    item, destination = " ".join(positional[:split]), " ".join(positional[split + 1 :])
    if not item or not destination:
        raise AccessCommandUsageError("Usage: /send <file> to <person|team/folder> [--move]")
    return item, destination, "--move" in flags


class AccessCommandHandler:
    """Run ``/access`` as the signed-in person through the gateway's access port."""

    def __init__(self, *, identity: GatewayCommandActorResolver, access: GatewayAccessCommandPort) -> None:
        self._identity = identity
        self._access = access

    async def execute(self, request: AccessCommandRequest) -> str:
        try:
            words = shlex.split(request.command)
        except ValueError as exc:
            name = (request.command.split() or [ACCESS_COMMAND])[0]
            return f"Could not read the {name} command: {exc}"
        if words and words[0] in FILE_SHARING_COMMANDS:
            return await self._file_sharing(words, request)
        action = words[1].lower() if len(words) > 1 else "help"
        enabled = self._access.authorization_enabled()
        if action in {"help", "--help"}:
            return ACCESS_HELP_TEXT if enabled else f"{ACCESS_AUTHORIZATION_OFF_NOTICE}\n\n{ACCESS_HELP_TEXT}"
        # Data folders exist in both modes: with authorization off every agent
        # uses them at their registered writability.
        if not enabled and action != "folders":
            return ACCESS_AUTHORIZATION_OFF_NOTICE
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return _UNREGISTERED_NOTICE
        try:
            return await self._run(action, words[2:], person_id)
        except AccessCommandUsageError as exc:
            return str(exc)

    async def _file_sharing(self, words: list[str], request: AccessCommandRequest) -> str:
        person_id = resolve_actor_canonical_user_id(request.actor, self._identity)
        if not person_id:
            return _UNREGISTERED_NOTICE
        command, rest = words[0], words[1:]
        try:
            if command == SHARES_COMMAND:
                return await self._access.list_shares(person_id)
            if command == UNSHARE_COMMAND:
                if len(rest) != 1:
                    raise AccessCommandUsageError("Usage: /unshare <share id> (see /shares)")
                return await self._access.unshare(person_id, rest[0])
            if command == SHARE_COMMAND:
                if not rest or rest[0] in {"help", "--help"}:
                    return FILE_SHARING_HELP_TEXT
                item, recipient, level, hours = parse_share(rest)
                return await self._access.share(person_id, item, recipient, level, hours)
            item, destination, move = parse_send(rest)
            return await self._access.send_file(person_id, item, destination, move)
        except AccessCommandUsageError as exc:
            return f"{exc}\n\n{FILE_SHARING_HELP_TEXT}"

    async def _run(self, action: str, rest: list[str], person_id: str) -> str:
        if action == "list":
            if len(rest) > 1:
                raise AccessCommandUsageError("Usage: /access list [agent type|agent|person|tool|tool set]")
            return await self._access.list_access(person_id, match=rest[0] if rest else None)
        if action == "grant":
            return await self._access.grant(person_id, parse_grant(rest))
        if action in {"revoke", "unblock"}:
            if len(rest) != 1:
                raise AccessCommandUsageError(f"Usage: /access {action} <{'grant' if action == 'revoke' else 'block'} id>")
            if action == "revoke":
                return await self._access.revoke(person_id, rest[0])
            return await self._access.unblock(person_id, rest[0])
        if action == "block":
            return await self._access.block(person_id, parse_block(rest))
        if action == "requests":
            return await self._access.waiting_requests(person_id)
        if action == "folders":
            return await self._folders(rest, person_id)
        if action == "profile":
            if len(rest) > 1:
                raise AccessCommandUsageError("Usage: /access profile [trusted|careful|manual]")
            return await self._access.starter_profile(person_id, rest[0].lower() if rest else None)
        return f"Unknown /access command {action!r}.\n\n{ACCESS_HELP_TEXT}"

    async def _folders(self, rest: list[str], person_id: str) -> str:
        action, value = parse_folders(rest)
        if action == "add" and isinstance(value, DataFolderInput):
            return await self._access.add_data_folder(person_id, value)
        if action == "remove" and isinstance(value, str):
            return await self._access.remove_data_folder(person_id, value)
        return await self._access.list_data_folders(person_id)


__all__ = [
    "ACCESS_AUTHORIZATION_OFF_NOTICE",
    "ACCESS_COMMAND",
    "ACCESS_HELP_TEXT",
    "AccessBlockInput",
    "AccessCommandHandler",
    "AccessCommandRequest",
    "AccessCommandUsageError",
    "AccessGrantInput",
    "DataFolderInput",
    "FILE_SHARING_COMMANDS",
    "FILE_SHARING_HELP_TEXT",
    "parse_folders",
    "parse_send",
    "parse_share",
    "GatewayAccessCommandPort",
    "GatewayAccessCommands",
    "is_access_command",
    "parse_block",
    "parse_grant",
]
