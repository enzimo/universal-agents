"""Load the ``gh`` and ``gog`` command tables and classify passthrough commands.

The tables (``ua_definitions/access/cli/<cli>.yaml``) hold, for one CLI:

- ``commands``: dotted command path -> level (anything else needs ``full``);
- ``command_aliases``: alternative words for a command path;
- ``blocked_commands`` and ``blocked_flags``: refused whatever access is held,
  because they manage the broker's own credentials or configuration, run local
  programs, or read and write local files outside the task's folders;
- per-command file rules: flags and positional arguments that name local files
  (``gog``), and flags that may only read standard input (``gh``).

The broker enforces the rules (``secure_tool_broker.cli_passthrough``); this
module only loads and classifies, so the runtime and the broker share one
answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from access_catalog.cli_commands import (
    CliCommandTable,
    CommandLineRejected,
    ParsedCommandLine,
    classify_command_line,
    parse_command_line,
    split_flag,
)
from custom_messages.contracts.access_v2 import AccessLevel


CLI_TABLE_DIR = Path(__file__).resolve().parents[1] / "ua_definitions" / "access" / "cli"
GH_API_READ_METHODS = frozenset({"GET", "HEAD"})
_GH_API_FIELD_FLAGS = frozenset({"-F", "--field", "-f", "--raw-field"})


@dataclass(frozen=True)
class CommandFileRules:
    """File-related rules for one command."""

    local_path_flags: frozenset[str] = frozenset()  # value is a local path ('-' allowed)
    file_reference_flags: frozenset[str] = frozenset()  # '@path' (or a plain path) reads a file
    local_path_positionals: tuple[int, ...] = ()  # positional indexes naming local files
    value_flags: frozenset[str] = frozenset()  # flags taking a value (to find positionals)
    stdin_only_flags: frozenset[str] = frozenset()  # value must be '-'
    field_flags: frozenset[str] = frozenset()  # 'key=value'; '@' values are refused
    blocked_flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CliPolicy:
    """Everything the broker needs to check one CLI's passthrough commands."""

    table: CliCommandTable
    blocked_commands: Mapping[str, str]
    blocked_flags: frozenset[str]
    file_rules: Mapping[str, CommandFileRules] = field(default_factory=dict)
    # Read commands that cannot run under gog's --readonly (see the generator).
    readonly_exempt: frozenset[str] = frozenset()

    @property
    def cli(self) -> str:
        return self.table.cli

    def blocked_reason(self, dotted_path: str) -> str | None:
        for blocked, reason in self.blocked_commands.items():
            if dotted_path == blocked or dotted_path.startswith(blocked + "."):
                return reason
        return None

    def rules_for(self, dotted_path: str) -> CommandFileRules:
        return self.file_rules.get(dotted_path, CommandFileRules())


def _load_yaml(name: str) -> dict[str, Any]:
    path = CLI_TABLE_DIR / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("cli") != name:
        raise ValueError(f"{path} is not a command table for {name!r}.")
    return data


def _alias_map(raw: Mapping[str, list[str]] | None) -> dict[tuple[str, ...], str]:
    """(parent path..., alias word) -> canonical word."""

    aliases: dict[tuple[str, ...], str] = {}
    for dotted, words in (raw or {}).items():
        path = tuple(str(dotted).split("."))
        for alias in words or []:
            aliases[(*path[:-1], str(alias))] = path[-1]
    return aliases


def _levels(raw: Mapping[str, str]) -> dict[str, AccessLevel]:
    return {str(path): AccessLevel(str(level)) for path, level in raw.items()}


@lru_cache(maxsize=None)
def load_cli_policy(name: str) -> CliPolicy:
    """The checked command table for ``gh`` or ``gog``."""

    return policy_from_data(name, _load_yaml(name))


def policy_from_data(name: str, data: Mapping[str, Any]) -> CliPolicy:
    """A checked command table from its data: a shipped file, or an installed CLI's approved table."""

    commands = _levels(data.get("commands") or {})
    blocked_commands = {str(key): str(value) for key, value in (data.get("blocked_commands") or {}).items()}
    overlap = [path for path in commands if any(
        path == blocked or path.startswith(blocked + ".") for blocked in blocked_commands
    )]
    if overlap:
        raise ValueError(f"{name}: blocked commands also have levels: {overlap[:5]}")
    table = CliCommandTable(
        cli=name,
        commands=commands,
        global_flags_with_value=frozenset(data.get("global_flags_with_value") or ()),
        global_flags=frozenset(data.get("global_flags") or ()),
        aliases=_alias_map(data.get("command_aliases")),
        blocked_paths=tuple(blocked_commands),
    )
    return CliPolicy(
        table=table,
        blocked_commands=blocked_commands,
        blocked_flags=frozenset(data.get("blocked_flags") or ()),
        file_rules=_file_rules(data),
        readonly_exempt=frozenset(str(path) for path in (data.get("readonly_exempt") or {})),
    )


def _file_rules(data: Mapping[str, Any]) -> dict[str, CommandFileRules]:
    """Per-command file and flag rules of a command table."""

    rules: dict[str, dict[str, Any]] = {}
    for key, target in (
        ("local_path_flags", "local_path_flags"),
        ("file_reference_flags", "file_reference_flags"),
    ):
        for dotted, flags in (data.get(key) or {}).items():
            rules.setdefault(str(dotted), {})[target] = frozenset(flags)
    for dotted, positions in (data.get("local_path_positionals") or {}).items():
        rules.setdefault(str(dotted), {})["local_path_positionals"] = tuple(int(p) for p in positions)
    for dotted, flags in (data.get("positional_value_flags") or {}).items():
        rules.setdefault(str(dotted), {})["value_flags"] = frozenset(flags)
    for dotted, command_rules in (data.get("command_flag_rules") or {}).items():
        entry = rules.setdefault(str(dotted), {})
        for key, target in (
            ("stdin_only", "stdin_only_flags"),
            ("fields", "field_flags"),
            ("blocked", "blocked_flags"),
        ):
            if command_rules.get(key):
                entry[target] = frozenset(command_rules[key])
    return {path: CommandFileRules(**values) for path, values in rules.items()}


def gh_api_method(parsed: ParsedCommandLine) -> str:
    """The HTTP method ``gh api`` will use: explicit, else POST when it sends fields or input."""

    for value in parsed.flag_values("-X", "--method"):
        if value:
            return value.strip().upper()
    if parsed.has_flag(*_GH_API_FIELD_FLAGS, "--input"):
        return "POST"
    return "GET"


def classify_passthrough(policy: CliPolicy, parsed: ParsedCommandLine) -> AccessLevel:
    """The level a checked passthrough command needs."""

    if policy.cli == "gh" and parsed.command_path == ("api",):
        # GraphQL queries and mutations are both POST, so only reads by method
        # are below full.
        return AccessLevel.READ if gh_api_method(parsed) in GH_API_READ_METHODS else AccessLevel.FULL
    return classify_command_line(parsed, policy.table)


__all__ = [
    "CLI_TABLE_DIR",
    "CliPolicy",
    "CommandFileRules",
    "CommandLineRejected",
    "classify_passthrough",
    "gh_api_method",
    "load_cli_policy",
    "parse_command_line",
    "policy_from_data",
    "split_flag",
]
