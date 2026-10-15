"""Classify command lines of broker-run CLIs (``gh``, ``gog``) by access level.

A passthrough action such as ``gog.exec`` runs a caller-supplied argument list.
Its level comes from the *command path* (for example ``gmail messages search``
or ``drive delete``) looked up in a per-CLI table; any path not in the table
requires ``full``.

Safety rule: the parser must never find a lower level than the command the
CLI will actually run. It therefore accepts only unambiguous argument lists
and the broker runs exactly the list it checked:

- a flag is ``-x`` (one letter) or ``--name`` / ``--name=value``; attached or
  combined short flags (``-XPATCH``, ``-Fk=@file``, ``-abc``) are refused,
  because whether ``-abc`` means ``-a bc`` or ``-a -b -c`` depends on the CLI;
- before the first command word only the CLI's known global flags may appear,
  and no flag may appear between command words, because a flag whose value we
  did not know about could consume a command word;
- ``--`` separators are refused.

Flags after the command path are free-form; blocked flags are checked by the
broker on every token regardless of position.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from custom_messages.contracts.access_v2 import AccessLevel


_NEGATIVE_NUMBER = re.compile(r"^-\d+(\.\d+)?$")


class CommandLineRejected(ValueError):
    """The argument list is ambiguous or unsupported; nothing was run."""


@dataclass(frozen=True)
class CliCommandTable:
    """The classified commands of one CLI.

    ``commands`` maps a dotted command path (``gmail.messages.search``) to its
    level. ``global_flags_with_value`` and ``global_flags`` are the only flags
    accepted before the first command word (every spelling: ``--account``,
    ``--acct``, ``-a``). ``aliases`` maps (parent words..., alias) to the
    canonical word, so ``drive list`` is classified as ``drive ls``.
    """

    cli: str
    commands: Mapping[str, AccessLevel]
    global_flags_with_value: frozenset[str] = frozenset()
    global_flags: frozenset[str] = frozenset()
    aliases: Mapping[tuple[str, ...], str] = field(default_factory=dict)
    # Blocked command paths: recognized as commands so they can be refused.
    blocked_paths: tuple[str, ...] = ()
    _prefixes: frozenset[tuple[str, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        prefixes: set[tuple[str, ...]] = set()
        for dotted in (*self.commands, *self.blocked_paths):
            words = tuple(dotted.split("."))
            if not all(words):
                raise ValueError(f"{self.cli}: invalid command path {dotted!r}")
            for end in range(1, len(words) + 1):
                prefixes.add(words[:end])
        object.__setattr__(self, "_prefixes", frozenset(prefixes))

    def is_known_prefix(self, words: tuple[str, ...]) -> bool:
        return words in self._prefixes

    def canonical_word(self, parent: tuple[str, ...], word: str) -> str:
        return self.aliases.get((*parent, word), word)

    def level_for(self, words: tuple[str, ...]) -> AccessLevel | None:
        return self.commands.get(".".join(words))

    def paths_up_to(self, level: AccessLevel) -> tuple[str, ...]:
        """Dotted command paths allowed at ``level`` (for ``--enable-commands-exact``)."""

        return tuple(sorted(path for path, needed in self.commands.items() if level.covers(needed)))


@dataclass(frozen=True)
class ParsedCommandLine:
    """An accepted argument list and the command path it runs."""

    argv: tuple[str, ...]
    command_path: tuple[str, ...]  # canonical words (aliases resolved)
    command_index: int  # index in argv of the first argument after the command path
    flags: tuple[tuple[str, str | None], ...]  # (name, attached value) for every flag token

    @property
    def dotted_path(self) -> str:
        return ".".join(self.command_path)

    def flag_values(self, *names: str) -> list[str | None]:
        """Values of the named flags: attached ``--x=v`` or the following token."""

        values: list[str | None] = []
        wanted = set(names)
        for index, token in enumerate(self.argv):
            name, attached = split_flag(token)
            if name is None or name not in wanted:
                continue
            if attached is not None:
                values.append(attached)
            elif index + 1 < len(self.argv):
                values.append(self.argv[index + 1])
            else:
                values.append(None)
        return values

    def has_flag(self, *names: str) -> bool:
        wanted = set(names)
        return any(name in wanted for name, _ in self.flags)


def split_flag(token: str) -> tuple[str | None, str | None]:
    """``(name, attached value)`` for a flag token, ``(None, None)`` otherwise."""

    if token.startswith("--") and len(token) > 2:
        name, sep, value = token.partition("=")
        return name, (value if sep else None)
    if token.startswith("-") and len(token) == 2 and token != "--":
        return token, None
    return None, None


def _check_token(token: str, *, cli: str) -> None:
    if token == "--":
        raise CommandLineRejected(f"{cli}: '--' argument separators are not accepted.")
    if (
        token.startswith("-")
        and not token.startswith("--")
        and len(token) > 2
        and not _NEGATIVE_NUMBER.fullmatch(token)
    ):
        raise CommandLineRejected(
            f"{cli}: attached or combined short flags such as {token!r} are not accepted. "
            "Pass each flag and its value as separate arguments (for example '-X' 'GET'), "
            "or use the long form '--name=value'."
        )


def parse_command_line(argv: Iterable[str], table: CliCommandTable) -> ParsedCommandLine:
    """Accept ``argv`` and find its command path, or raise ``CommandLineRejected``."""

    tokens = tuple(str(token) for token in argv)
    if not tokens:
        raise CommandLineRejected(f"{table.cli}: a command is required.")
    for token in tokens:
        _check_token(token, cli=table.cli)
    flags = tuple(
        (name, value) for name, value in (split_flag(token) for token in tokens) if name is not None
    )

    index = 0
    # Known global flags before the first command word.
    while index < len(tokens) and tokens[index].startswith("-"):
        name, attached = split_flag(tokens[index])
        if name in table.global_flags_with_value:
            index += 1 if attached is not None else 2
            continue
        if name in table.global_flags and attached is None:
            index += 1
            continue
        raise CommandLineRejected(
            f"{table.cli}: {tokens[index]!r} is not accepted before the command; put command "
            "options after the full command (for example 'gmail messages search --max 5')."
        )
    if index >= len(tokens):
        return ParsedCommandLine(tokens, (), index, flags)

    path: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-") and not _NEGATIVE_NUMBER.fullmatch(token):
            # A later word that would continue the command means an option sits
            # between command words; its value could hide the real command.
            for later in _positionals_after(tokens, index):
                if table.is_known_prefix((*path, table.canonical_word(tuple(path), later))):
                    raise CommandLineRejected(
                        f"{table.cli}: options must come after the full command, not between "
                        f"command words ({token!r} before {later!r})."
                    )
            break
        candidate = (*path, table.canonical_word(tuple(path), token))
        if not table.is_known_prefix(candidate):
            break
        path.append(candidate[-1])
        index += 1
    return ParsedCommandLine(tokens, tuple(path), index, flags)


def _positionals_after(tokens: tuple[str, ...], start: int) -> list[str]:
    return [token for token in tokens[start:] if not token.startswith("-")]


MAX_ARGUMENTS = 120
MAX_ARGUMENT_CHARS = 20_000
MAX_TOTAL_CHARS = 200_000


def argv_from_params(params: Mapping[str, Any], *, cli: str) -> list[str]:
    """The caller's argument list for ``<cli>.exec``, with size limits.

    The catalog resolves the level and the broker checks and runs the command
    from this same list, so the level decided is the level of what runs.
    """

    raw_argv = params.get("argv")
    if raw_argv is None:
        raw_argv = params.get("args")
    if isinstance(raw_argv, str):
        raise ValueError(f"{cli}.exec requires argv as a list of arguments, not a shell string")
    if not isinstance(raw_argv, list):
        raise ValueError(f"{cli}.exec requires argv as a list of arguments")
    argv: list[str] = []
    total = 0
    for index, raw_value in enumerate(raw_argv):
        if raw_value is None:
            raise ValueError(f"{cli}.exec argv[{index}] must not be null")
        value = str(raw_value)
        if not value:
            raise ValueError(f"{cli}.exec argv[{index}] must not be empty")
        if "\x00" in value:
            raise ValueError(f"{cli}.exec argv[{index}] contains a NUL byte")
        if len(value) > MAX_ARGUMENT_CHARS:
            raise ValueError(f"{cli}.exec argv[{index}] is too large")
        total += len(value)
        argv.append(value)
    if not argv:
        raise ValueError(f"{cli}.exec requires at least one argv entry after the {cli} executable")
    if len(argv) > MAX_ARGUMENTS:
        raise ValueError(f"{cli}.exec accepts at most {MAX_ARGUMENTS} argv entries")
    if total > MAX_TOTAL_CHARS:
        raise ValueError(f"{cli}.exec argv is too large")
    if os.path.basename(argv[0]) == cli:
        raise ValueError(f"{cli}.exec argv must not include the {cli} executable name")
    return argv


def classify_command_line(parsed: ParsedCommandLine, table: CliCommandTable) -> AccessLevel:
    """The level the parsed command needs; unclassified commands need ``full``."""

    if not parsed.command_path:
        # Only global flags, such as '--version' or '--help'.
        return AccessLevel.READ if parsed.flags else AccessLevel.FULL
    return table.level_for(parsed.command_path) or AccessLevel.FULL


__all__ = [
    "argv_from_params",
    "CliCommandTable",
    "CommandLineRejected",
    "ParsedCommandLine",
    "classify_command_line",
    "parse_command_line",
    "split_flag",
]
