"""Check ``gh`` and ``gog`` command lines before the broker runs them.

Every ``gog`` command line (typed Google actions and ``gog.exec``) and every
``gh.exec`` command line passes through ``check_command_line``. It:

1. parses the arguments with the shared unambiguous parser
   (``access_catalog.cli_commands``) and finds the command path and its level;
2. refuses blocked commands and blocked flags wherever they appear (the
   broker's own credentials and configuration, local programs and servers,
   local repositories);
3. for ``gh``, lets file-reading flags read only standard input and refuses
   ``@file`` field values;
4. for ``gog``, copies local files named by the command through a private
   temporary folder, limited to the requesting person's folders
   (``secure_tool_broker.task_files``);
5. for ``gog``, adds ``--disable-commands`` with the blocked commands,
   ``--enable-commands-exact`` with the one command it checked, and, for read
   commands, ``--readonly``, so gog itself refuses anything else as a second
   check.

Installed CLIs (``check_installed_command_line``) get the same parsing and
blocked command and flag checks against their approved table.

The broker runs exactly the argument list returned here. The access gate
(``secure_tool_broker.access_gate``) decides the level of the same argument
list before this runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from access_catalog.cli_commands import argv_from_params
from access_catalog.cli_tables import (
    CliPolicy,
    CommandFileRules,
    CommandLineRejected,
    classify_passthrough,
    gh_api_method,
    load_cli_policy,
    parse_command_line,
    split_flag,
)
from custom_messages.contracts.access_v2 import AccessLevel
from secure_tool_broker.task_files import TaskFileRejected, TaskFileStage


# Until grants enforce levels, 'gh api' keeps its earlier limit to GET and
# POST; other methods need 'full' (access_catalog.cli_tables).
GH_API_INTERIM_METHODS = frozenset({"GET", "POST"})
# Enables nothing: global-only invocations (--version, --help) run no command.
GOG_NO_COMMAND = "ua.no-command"
GOG_READONLY_FLAG = "--readonly"
_GOG_OUTPUT_FLAGS = frozenset({"--out", "--output", "-o"})
_GOG_OUTPUT_DIRECTORY_FLAGS = frozenset({"--out-dir"})

REASON_COMMAND_LINE_REJECTED = "cli_command_line_rejected"
REASON_COMMAND_BLOCKED = "cli_command_blocked"
REASON_FLAG_BLOCKED = "cli_flag_blocked"
REASON_LOCAL_FILE_REJECTED = "cli_local_file_rejected"


class CommandRefused(ValueError):
    """The command line was refused; nothing ran."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class CheckedCommand:
    """A command line the broker may run exactly as given."""

    cli: str
    argv: list[str]  # without the executable
    command_path: str
    level: AccessLevel
    stage: TaskFileStage | None = None


def _flag_occurrences(argv: Sequence[str], names: frozenset[str]) -> list[tuple[str, int, bool]]:
    """(name, index of the value, value attached to the flag token) for each use."""

    found: list[tuple[str, int, bool]] = []
    for index, token in enumerate(argv):
        name, attached = split_flag(token)
        if name is None or name not in names:
            continue
        if attached is not None:
            found.append((name, index, True))
        elif index + 1 < len(argv):
            found.append((name, index + 1, False))
        else:
            raise CommandRefused(f"{name} needs a value.", reason_code=REASON_COMMAND_LINE_REJECTED)
    return found


def _value(argv: Sequence[str], index: int, attached: bool) -> str:
    if attached:
        return split_flag(argv[index])[1] or ""
    return argv[index]


def _replace(argv: list[str], index: int, attached: bool, value: str) -> None:
    if attached:
        name = split_flag(argv[index])[0]
        argv[index] = f"{name}={value}"
    else:
        argv[index] = value


def _refuse_blocked_flags(policy: CliPolicy, rules: CommandFileRules, argv: Sequence[str]) -> None:
    blocked = policy.blocked_flags | rules.blocked_flags
    for token in argv:
        name, _ = split_flag(token)
        if name is not None and name in blocked:
            raise CommandRefused(
                f"{policy.cli}: the {name} option is not available through the broker.",
                reason_code=REASON_FLAG_BLOCKED,
            )


def _check_gh_files(rules: CommandFileRules, argv: Sequence[str], dotted: str) -> None:
    for name, index, attached in _flag_occurrences(argv, rules.stdin_only_flags):
        if _value(argv, index, attached) != "-":
            raise CommandRefused(
                f"gh {dotted.replace('.', ' ')}: {name} reads a file on the broker host; pass '-' "
                "and send the content as params.stdin.",
                reason_code=REASON_LOCAL_FILE_REJECTED,
            )
    for name, index, attached in _flag_occurrences(argv, rules.field_flags):
        value = _value(argv, index, attached)
        field_value = value.split("=", 1)[1] if "=" in value else value
        if field_value.startswith("@"):
            raise CommandRefused(
                f"gh {dotted.replace('.', ' ')}: {name} values starting with '@' read a file on "
                "the broker host; pass the content itself.",
                reason_code=REASON_LOCAL_FILE_REJECTED,
            )


def _positional_indexes(argv: Sequence[str], start: int, value_flags: frozenset[str]) -> list[int]:
    indexes: list[int] = []
    index = start
    while index < len(argv):
        token = argv[index]
        name, attached = split_flag(token)
        if name is not None:
            index += 2 if name in value_flags and attached is None else 1
            continue
        indexes.append(index)
        index += 1
    return indexes


def _stage_gog_files(
    rules: CommandFileRules,
    argv: list[str],
    start: int,
    stage_factory: Any,
) -> TaskFileStage | None:
    stage: TaskFileStage | None = None

    def current() -> TaskFileStage:
        nonlocal stage
        if stage is None:
            stage = stage_factory()
        return stage

    try:
        for name, index, attached in _flag_occurrences(argv, rules.local_path_flags):
            value = _value(argv, index, attached)
            if value == "-":
                continue
            if name in _GOG_OUTPUT_DIRECTORY_FLAGS:
                staged = current().output_directory(value)
            elif name in _GOG_OUTPUT_FLAGS:
                staged = current().output_path(value)
            else:
                staged = current().input_file(value)
            _replace(argv, index, attached, staged)
        for name, index, attached in _flag_occurrences(argv, rules.file_reference_flags):
            value = _value(argv, index, attached)
            stripped = value.strip()
            if stripped == "-" or stripped.startswith(("{", "[")):
                continue  # standard input or inline JSON
            if stripped.startswith("@"):
                _replace(argv, index, attached, "@" + current().input_file(stripped[1:]))
            else:
                _replace(argv, index, attached, current().input_file(stripped))
        positionals = _positional_indexes(argv, start, rules.value_flags)
        for position in rules.local_path_positionals:
            if position < len(positionals):
                argv[positionals[position]] = current().input_file(argv[positionals[position]])
    except TaskFileRejected as exc:
        if stage is not None:
            stage.cleanup()
        raise CommandRefused(str(exc), reason_code=REASON_LOCAL_FILE_REJECTED) from exc
    except BaseException:
        if stage is not None:
            stage.cleanup()
        raise
    return stage


def check_command_line(
    cli: str,
    argv: Sequence[str],
    *,
    instance_root: str | None,
    person_id: str | None,
) -> CheckedCommand:
    """Check one ``gh`` or ``gog`` argument list (without the executable)."""

    policy = load_cli_policy(cli)
    try:
        parsed = parse_command_line(argv, policy.table)
    except CommandLineRejected as exc:
        raise CommandRefused(str(exc), reason_code=REASON_COMMAND_LINE_REJECTED) from exc
    dotted = parsed.dotted_path
    reason = policy.blocked_reason(dotted)
    if reason:
        raise CommandRefused(
            f"{cli} {dotted.replace('.', ' ')} is not available through the broker: it {reason}.",
            reason_code=REASON_COMMAND_BLOCKED,
        )
    rules = policy.rules_for(dotted)
    level = classify_passthrough(policy, parsed)
    _refuse_blocked_flags(policy, rules, parsed.argv)
    checked_argv = list(parsed.argv)
    stage: TaskFileStage | None = None
    if cli == "gh":
        _check_gh_files(rules, parsed.argv, dotted)
        if parsed.command_path == ("api",) and gh_api_method(parsed) not in GH_API_INTERIM_METHODS:
            raise CommandRefused(
                "gh api allows only GET or POST through the broker for now.",
                reason_code=REASON_COMMAND_BLOCKED,
            )
    elif cli == "gog":
        stage = _stage_gog_files(
            rules,
            checked_argv,
            parsed.command_index,
            lambda: TaskFileStage.create(instance_root=instance_root, person_id=person_id),
        )
        disabled = ",".join(sorted(policy.blocked_commands))
        # gog itself refuses anything but the one command checked here, so a
        # parser difference between gog and the broker cannot run another one.
        enabled = dotted or GOG_NO_COMMAND
        guards = [f"--disable-commands={disabled}", f"--enable-commands-exact={enabled}"]
        # A read command also runs under gog's --readonly, which blocks any
        # request that changes data, in case the table misclassified it.
        if level is AccessLevel.READ and dotted not in policy.readonly_exempt:
            guards.append(GOG_READONLY_FLAG)
        checked_argv = [*guards, *checked_argv]
    return CheckedCommand(
        cli=cli,
        argv=checked_argv,
        command_path=dotted,
        level=level,
        stage=stage,
    )


def check_installed_command_line(policy: CliPolicy, argv: Sequence[str]) -> CheckedCommand:
    """Check one installed CLI's argument list against its approved command table.

    The same unambiguous parser as ``gh`` and ``gog``; blocked commands and
    flags are refused wherever they appear; unlisted commands need ``full``
    (the access gate decided the level of this same list). The broker runs
    exactly the returned list, with the checked copy of the program.
    """

    try:
        parsed = parse_command_line(argv, policy.table)
    except CommandLineRejected as exc:
        raise CommandRefused(str(exc), reason_code=REASON_COMMAND_LINE_REJECTED) from exc
    dotted = parsed.dotted_path
    reason = policy.blocked_reason(dotted)
    if reason:
        raise CommandRefused(
            f"{policy.cli} {dotted.replace('.', ' ')} is not available through the broker: {reason}.",
            reason_code=REASON_COMMAND_BLOCKED,
        )
    _refuse_blocked_flags(policy, policy.rules_for(dotted), parsed.argv)
    return CheckedCommand(
        cli=policy.cli,
        argv=list(parsed.argv),
        command_path=dotted,
        level=classify_passthrough(policy, parsed),
    )


__all__ = [
    "CheckedCommand",
    "CommandRefused",
    "GH_API_INTERIM_METHODS",
    "REASON_COMMAND_BLOCKED",
    "REASON_COMMAND_LINE_REJECTED",
    "REASON_FLAG_BLOCKED",
    "REASON_LOCAL_FILE_REJECTED",
    "argv_from_params",
    "check_command_line",
    "check_installed_command_line",
]
