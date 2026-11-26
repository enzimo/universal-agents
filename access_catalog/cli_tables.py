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

import posixpath
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

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

# Union of `gh api` flags across supported GitHub CLI versions, verified against
# github.com/cli/cli `pkg/cmd/api/api.go` and `gh api --help`.
# Rule for updating these tables: listing a flag that an older installed `gh`
# does not yet support is safe as long as its kind (value vs boolean) matches
# upstream, because the older `gh` rejects the unknown flag before sending any
# request. Flags newer than these tables are refused fail-closed until added.
_GH_API_METHOD_FLAGS = frozenset({"-X", "--method"})
_GH_API_FIELD_FLAGS = frozenset({"-F", "--field", "-f", "--raw-field"})
_GH_API_INPUT_FLAGS = frozenset({"--input"})
_GH_API_BODY_FLAGS = _GH_API_FIELD_FLAGS | _GH_API_INPUT_FLAGS
_GH_API_VALUE_FLAGS = (
    _GH_API_METHOD_FLAGS
    | _GH_API_BODY_FLAGS
    | frozenset(
        {
            "-H",
            "--header",
            "-q",
            "--jq",
            "-t",
            "--template",
            "--cache",
            "-p",
            "--preview",
            "--hostname",
        }
    )
)
_GH_API_BOOL_FLAGS = frozenset(
    {
        "-i",
        "--include",
        "--paginate",
        "--silent",
        "--verbose",
        "-h",
        "--help",
        "--slurp",
        "--allow-escape-sequences",
    }
)
_GH_API_BOOL_VALUES = frozenset({"true", "false"})


@dataclass(frozen=True)
class GhApiCall:
    """Parsed HTTP method and classification signals for one ``gh api`` invocation."""

    method: str
    has_fields: bool
    has_input: bool
    is_graphql: bool


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


def _read_gh_api_flag(tokens: tuple[str, ...], index: int) -> tuple[str, str | None, int]:
    """Read one option token from ``gh api`` arguments in flag position."""

    token = tokens[index]
    name, attached = split_flag(token)
    if name is None:
        raise CommandLineRejected(f"gh api: unknown option {token!r}.")
    if name in _GH_API_VALUE_FLAGS:
        if attached is not None:
            return name, attached, index + 1
        if index + 1 >= len(tokens):
            raise CommandLineRejected(f"gh api: {name} requires a value.")
        return name, tokens[index + 1], index + 2
    if name in _GH_API_BOOL_FLAGS:
        if attached is not None and attached not in _GH_API_BOOL_VALUES:
            raise CommandLineRejected(
                f"gh api: {name} only accepts 'true' or 'false' as an attached value."
            )
        return name, attached, index + 1
    raise CommandLineRejected(f"gh api: unknown option {name!r}.")


def _is_gh_api_graphql_endpoint(endpoint: str) -> bool:
    """Whether an endpoint argument targets GitHub's GraphQL endpoint.

    Mirrors ``gh``'s own URL rule (only strings containing ``://`` are parsed
    as full URLs; everything else, including leading ``//``, is a path), then
    strips query string, fragment, percent-encoding, and leading slashes,
    resolves dot segments, and checks whether the path targets ``graphql`` or
    ``api/graphql`` (case-insensitive).

    ``urlsplit`` raises ``ValueError`` for hosts it cannot read (unbalanced or
    non-numeric square brackets, characters that change under NFKC
    normalization). Such a line is refused as ``CommandLineRejected`` so both
    the catalog (``command:unparsed`` with ``command_line_rejected`` set) and
    the broker (``cli_command_line_rejected``) give the caller a plain reason
    instead of an uncaught error.
    """

    if "://" in endpoint:
        try:
            raw_path = urlsplit(endpoint).path
        except ValueError as exc:
            raise CommandLineRejected("gh api: the endpoint is not a valid address.") from exc
    else:
        raw_path = endpoint.split("?", 1)[0].split("#", 1)[0]
    unquoted = unquote(raw_path)
    normalized = posixpath.normpath("/" + unquoted.lstrip("/")).lstrip("/")
    if not normalized or normalized == ".":
        return False
    segments = [segment.lower() for segment in normalized.split("/")]
    return segments[0] == "graphql" or segments[:2] == ["api", "graphql"]


def _parse_gh_api_method_value(explicit_method: str | None, value: str | None) -> str:
    if explicit_method is not None:
        raise CommandLineRejected("gh api: -X/--method may only be specified once.")
    if not value:
        raise CommandLineRejected("gh api: -X/--method requires a non-empty HTTP method.")
    return value.upper()


def parse_gh_api(parsed: ParsedCommandLine) -> GhApiCall:
    """Parse ``gh api`` arguments in order to find the HTTP method and safety signals.

    Refuses unknown options, missing values, invalid attached boolean values,
    repeated ``-X``/``--method`` options, and explicit empty methods (``--method=``).
    """

    if parsed.command_path != ("api",):
        raise CommandLineRejected("gh api parser requires a parsed 'gh api' command.")
    tokens = parsed.argv
    index = parsed.command_index
    explicit_method: str | None = None
    has_fields = False
    has_input = False
    positionals: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            positionals.append(token)
            index += 1
            continue
        name, value, index = _read_gh_api_flag(tokens, index)
        if name in _GH_API_METHOD_FLAGS:
            explicit_method = _parse_gh_api_method_value(explicit_method, value)
        elif name in _GH_API_FIELD_FLAGS:
            has_fields = True
        elif name in _GH_API_INPUT_FLAGS:
            has_input = True
    method = explicit_method if explicit_method is not None else ("POST" if (has_fields or has_input) else "GET")
    is_graphql = any(_is_gh_api_graphql_endpoint(arg) for arg in positionals)
    return GhApiCall(method=method, has_fields=has_fields, has_input=has_input, is_graphql=is_graphql)


def gh_api_level(call: GhApiCall) -> AccessLevel:
    """The access level one parsed ``gh api`` call requires.

    Plain ``GET`` calls (including ``-f``/``-F`` fields, which ``gh`` sends in
    the URL query string without a body) and ``HEAD`` calls without fields or
    ``--input`` to non-GraphQL endpoints classify as ``read``. ``HEAD`` with
    fields (which ``gh`` sends as a JSON request body), any call with
    ``--input``, any GraphQL call, and all other HTTP methods require ``full``.
    """

    if call.method not in GH_API_READ_METHODS or call.is_graphql or call.has_input:
        return AccessLevel.FULL
    if call.method == "HEAD" and call.has_fields:
        return AccessLevel.FULL
    return AccessLevel.READ


def classify_passthrough_call(policy: CliPolicy, parsed: ParsedCommandLine) -> tuple[AccessLevel, GhApiCall | None]:
    """The level a checked passthrough command needs, and its parsed ``gh api`` call when it is one.

    The one place that decides whether a line is a ``gh api`` call, so the
    catalog's level and the broker's method limit always read the same parse.
    """

    if policy.cli == "gh" and parsed.command_path == ("api",):
        call = parse_gh_api(parsed)
        return gh_api_level(call), call
    return classify_command_line(parsed, policy.table), None


def classify_passthrough(policy: CliPolicy, parsed: ParsedCommandLine) -> AccessLevel:
    """The level a checked passthrough command needs."""

    return classify_passthrough_call(policy, parsed)[0]


__all__ = [
    "CLI_TABLE_DIR",
    "CliPolicy",
    "CommandFileRules",
    "CommandLineRejected",
    "GhApiCall",
    "classify_passthrough",
    "classify_passthrough_call",
    "gh_api_level",
    "load_cli_policy",
    "parse_command_line",
    "parse_gh_api",
    "policy_from_data",
    "split_flag",
]
