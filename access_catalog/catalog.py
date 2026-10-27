"""The tool catalog: load definitions and resolve a concrete call to its level.

Sources, all trusted:

- ``ua_definitions/access/tools/*.yaml``: built-in function, code, broker,
  command-line and web API tools, and the tool sets. A built-in web API tool
  for a service the instance runs itself names the setting that holds the
  service's address (``service_url_setting``) instead of hosts; the caller
  passes the configured addresses (``service_urls``);
- ``ua_definitions/access/cli/*.yaml``: command tables for passthrough CLIs;
- ``<instance_root>/control/access/tools/*.yaml``: web API tools the operator
  defined for this instance (only ``http:*`` tools are accepted there).

``resolve_*`` turns a function call, broker action, web request, or command
line into ``OperationResolutionV1`` (tool, operation, level). Anything the
catalog does not know resolves to ``full``, so a narrowed grant never covers an
operation nobody reviewed. The catalog grants nothing; the access service
decides who may use what.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import SplitResult, urlsplit

import yaml

from access_catalog.cli_commands import argv_from_params
from access_catalog.cli_tables import (
    CliPolicy,
    CommandLineRejected,
    classify_passthrough,
    load_cli_policy,
    parse_command_line,
    policy_from_data,
)
from access_catalog.http_routes import METHOD_DEFAULT_LEVELS, classify_http_request
from default_values import WEB_SERVICES_DEFAULTS
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.services import CRAWL4AI_URL_SETTING
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccountMode,
    AccountSharing,
    ClassificationSource,
    CredentialInjectionV1,
    InjectionPlacement,
    HttpRouteV1,
    LinkOwnerKind,
    OperationDefinitionV1,
    OperationResolutionV1,
    OwnerScope,
    TargetKind,
    TargetRefV1,
    ToolDefinitionV1,
    ToolKind,
    ToolSetDefinitionV1,
)
from custom_messages.contracts.cli_tool_v1 import InstalledCliV1
from custom_messages.contracts.tool_setup_spec_v1 import ToolSetupSpecV1
from custom_messages.enums import ChangeRecordKind

logger = logging.getLogger(__name__)

DEFINITIONS_DIR = Path(__file__).resolve().parents[1] / "ua_definitions" / "access" / "tools"
PROCEDURE_HANDLERS_FILE = (
    Path(__file__).resolve().parents[1] / "ua_definitions" / "installed_permissions" / "handlers.yaml"
)
INSTALLED_PROCEDURE_PREFIX = "installed_permission."
UNREGISTERED_FUNCTION_TOOL = "fn:unregistered"
# The one secure_cli action of an installed CLI: run a checked argument list.
INSTALLED_CLI_ACTION = "exec"
UNLISTED_WEB_TOOL = "http:unlisted"
# Every installed MCP server's tool joins this set (declared in functions.yaml;
# members hold it as a standard set, access_control.standard_grants).
MCP_SERVERS_TOOL_SET = "mcp_servers"
_ACTION_MODES = {"command_table", "http", "linked_tool"}
# Settings that hold the address of a service the instance runs itself, and
# the address used when the caller does not pass the configured one. Only the
# broker's catalog decides requests, and the broker passes the configured
# addresses; other processes use the catalog for names, levels and tool sets.
DEFAULT_SERVICE_URLS: Mapping[str, str] = {
    CRAWL4AI_URL_SETTING: str(WEB_SERVICES_DEFAULTS.crawl4ai.value["base_url"]),
}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def web_origin(url: str) -> str | None:
    """``scheme://host:port`` of an absolute http(s) URL, the port always written; None otherwise."""

    parts: SplitResult = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    try:
        port = parts.port or _DEFAULT_PORTS[parts.scheme]
    except ValueError:
        return None
    return f"{parts.scheme}://{host}:{port}"


class CatalogError(ValueError):
    """A definition file is invalid; the catalog refuses to load."""


@dataclass(frozen=True)
class FunctionBinding:
    """How one registered function maps to operations of a tool."""

    tool_id: str
    function: str
    level: AccessLevel | None = None  # fixed level
    argument: str | None = None  # the argument whose value picks the level
    values: Mapping[str, AccessLevel] = field(default_factory=dict)
    # The kind of record the call changes, when its changes run first in a
    # model step (ChangeRecordKind); None changes no such record.
    record_kind: ChangeRecordKind | None = None  # fixed-level functions
    record_kinds: Mapping[str, ChangeRecordKind] = field(default_factory=dict)  # by argument value


@dataclass(frozen=True)
class BrokerActionBinding:
    tool_id: str
    provider: str
    action: str
    level: AccessLevel | None = None
    # "level"; "command_table" (the command's level); "http" (the web API tool
    # owning the URL); "linked_tool" (full access to the tool named by
    # params.tool_id, whose linked credential the action hands to code).
    mode: str = "level"
    command_table: str | None = None


def _level(value: Any, *, where: str) -> AccessLevel:
    try:
        return AccessLevel(str(value))
    except ValueError as exc:
        raise CatalogError(f"{where}: unknown level {value!r}") from exc


# ``record_kind: none``: the operation changes something, but no record whose
# changes run first in a model step.
NO_RECORD_KIND = "none"
_FIXED_FUNCTION_KEYS = frozenset({"level", "record_kind"})
_ARGUMENT_FUNCTION_KEYS = frozenset({"argument", "values", "record_kinds"})


def _record_kind(value: Any, *, where: str) -> ChangeRecordKind | None:
    if str(value) == NO_RECORD_KIND:
        return None
    try:
        return ChangeRecordKind(str(value))
    except ValueError as exc:
        raise CatalogError(f"{where}: unknown record kind {value!r}") from exc


def _check_keys(spec: Mapping[str, Any], allowed: frozenset[str], required: set[str], *, where: str) -> None:
    unknown = sorted(set(map(str, spec)) - allowed)
    if unknown:
        raise CatalogError(f"{where}: unknown keys {unknown}")
    missing = sorted(required - set(map(str, spec)))
    if missing:
        raise CatalogError(f"{where}: missing keys {missing}")


def _function_binding(
    tool_id: str, function: str, spec: Any
) -> tuple[FunctionBinding, dict[str, ChangeRecordKind | None]]:
    """One function's binding, and the record kinds its operations declare (None for ``none``).

    A function is ``function: level``, ``function: {level, record_kind}``, or
    ``function: {argument, values, record_kinds}`` with a level per argument
    value and a record kind per changing value.
    """

    where = f"{tool_id}.{function}"
    if not isinstance(spec, Mapping):
        return FunctionBinding(tool_id, function, level=_level(spec, where=where)), {}
    if "argument" not in spec:
        _check_keys(spec, _FIXED_FUNCTION_KEYS, {"level"}, where=where)
        if "record_kind" not in spec:
            return FunctionBinding(tool_id, function, level=_level(spec["level"], where=where)), {}
        kind = _record_kind(spec["record_kind"], where=where)
        binding = FunctionBinding(tool_id, function, level=_level(spec["level"], where=where), record_kind=kind)
        return binding, {function: kind}
    _check_keys(spec, _ARGUMENT_FUNCTION_KEYS, {"argument", "values"}, where=where)
    values = {str(value): _level(level, where=where) for value, level in spec["values"].items()}
    kinds = {str(value): _record_kind(kind, where=f"{where}.{value}") for value, kind in (spec.get("record_kinds") or {}).items()}
    unknown = sorted(set(kinds) - set(values))
    if unknown:
        raise CatalogError(f"{where}: record kinds for values it does not take: {unknown}")
    binding = FunctionBinding(
        tool_id,
        function,
        argument=str(spec["argument"]),
        values=values,
        record_kinds={value: kind for value, kind in kinds.items() if kind is not None},
    )
    return binding, {f"{function}.{value}": kind for value, kind in kinds.items()}


def _check_record_kinds(
    tool_id: str, operations: Iterable[OperationDefinitionV1], declared: Mapping[str, ChangeRecordKind | None]
) -> None:
    """In a tool with record kinds, every changing function operation names one (or ``none``).

    Otherwise a new action added to such a tool would change its records
    without running first, and nothing would notice.
    """

    if not any(kind is not None for kind in declared.values()):
        return
    unclassified = sorted(
        operation.name
        for operation in operations
        if operation.level is not AccessLevel.READ and operation.name not in declared
    )
    if unclassified:
        raise CatalogError(
            f"{tool_id}: changing operations without a record kind (use none if they change no such record): "
            f"{unclassified}"
        )


CHANGE_OPERATION_PREFIX = "change:"


def change_operation(kind: str) -> str:
    """The catalog operation for one kind of staged change (``changes:`` entries)."""

    return f"{CHANGE_OPERATION_PREFIX}{kind}"


def _tool_fields(raw: Mapping[str, Any], operations: Iterable[OperationDefinitionV1]) -> dict[str, Any]:
    tool_id = str(raw.get("tool_id") or "")
    return {
        "tool_id": tool_id,
        "kind": ToolKind(tool_id.split(":", 1)[0]),
        "display_name": raw.get("display_name") or tool_id,
        "description": str(raw.get("description") or "").strip(),
        "owner_scope": OwnerScope(raw.get("owner_scope") or "team"),
        "operations": tuple(operations),
        "account": AccountMode(raw.get("account") or "none"),
        "hosts": tuple(raw.get("hosts") or ()),
        "routes": tuple(HttpRouteV1(**route) for route in raw.get("routes") or ()),
        "tool_sets": tuple(raw.get("tool_sets") or ()),
        "credential_injections": tuple(
            CredentialInjectionV1(**injection) for injection in raw.get("credential_injections") or ()
        ),
        "setup": ToolSetupSpecV1(**raw["setup"]) if raw.get("setup") else None,
        "link_owners": tuple(raw.get("link_owners") or ("person", "team")),
        "default_sharing": AccountSharing(raw["default_sharing"]) if raw.get("default_sharing") else None,
        "installed_cli": InstalledCliV1(**raw["installed_cli"]) if raw.get("installed_cli") else None,
    }


def _http_operations(raw: Mapping[str, Any]) -> list[OperationDefinitionV1]:
    operations = [
        OperationDefinitionV1(name=method.lower(), level=level)
        for method, level in METHOD_DEFAULT_LEVELS.items()
    ]
    operations += [
        OperationDefinitionV1(name=f"route:{route['name']}", level=_level(route["level"], where=str(route)))
        for route in raw.get("routes") or ()
    ]
    return operations


@dataclass
class ToolCatalog:
    """Every tool and operation, and how concrete calls map onto them."""

    tools: dict[str, ToolDefinitionV1] = field(default_factory=dict)
    tool_sets: dict[str, ToolSetDefinitionV1] = field(default_factory=dict)
    functions: dict[str, FunctionBinding] = field(default_factory=dict)
    function_dispatchers: dict[str, str] = field(default_factory=dict)
    broker_actions: dict[tuple[str, str], BrokerActionBinding] = field(default_factory=dict)
    broker_dispatch_providers: dict[str, str] = field(default_factory=dict)
    # Functions never shown to models (functions.yaml "not_offered").
    not_offered: set[str] = field(default_factory=set)
    # Tools whose functions load on request (functions.yaml "on_request",
    # custom_agents.tool_loading): tool id -> the functions that stay shown.
    on_request: dict[str, frozenset[str]] = field(default_factory=dict)
    http_hosts: dict[str, str] = field(default_factory=dict)
    # Web API tools of services the instance runs itself: the exact origin
    # (scheme://host:port) -> tool id. Such a tool is never matched by host
    # alone, so another service on the same host is not mistaken for it.
    http_origins: dict[str, str] = field(default_factory=dict)
    # Configured addresses for service_url_setting entries (DEFAULT_SERVICE_URLS otherwise).
    service_urls: Mapping[str, str] = field(default_factory=dict)
    # Web API tools that agents reach only through a function tool's functions
    # (reached_through: the tool id), not through the secure_cli dispatcher.
    reached_through: dict[str, str] = field(default_factory=dict)
    mcp_prefixes: dict[str, str] = field(default_factory=dict)  # agent tool prefix -> tool id
    # Kinds of staged changes applied through apply_change -> (tool id, level).
    changes: dict[str, tuple[str, AccessLevel]] = field(default_factory=dict)
    # Command tables of CLIs the team installed (instance files), by CLI name.
    cli_policies: dict[str, CliPolicy] = field(default_factory=dict)

    # Loading -----------------------------------------------------------------

    def add_tool(self, tool: ToolDefinitionV1, *, origin: str | None = None) -> None:
        if tool.tool_id in self.tools:
            raise CatalogError(f"Tool {tool.tool_id!r} is defined twice.")
        self.tools[tool.tool_id] = tool
        if origin is not None:
            owner = self.http_origins.setdefault(origin, tool.tool_id)
            if owner != tool.tool_id:
                raise CatalogError(f"Address {origin!r} belongs to both {owner!r} and {tool.tool_id!r}.")
            return
        for host in tool.hosts:
            owner = self.http_hosts.setdefault(host, tool.tool_id)
            if owner != tool.tool_id:
                raise CatalogError(f"Host {host!r} belongs to both {owner!r} and {tool.tool_id!r}.")

    def _load_function_tool(self, raw: Mapping[str, Any]) -> None:
        tool_id = str(raw["tool_id"])
        operations: list[OperationDefinitionV1] = []
        declared: dict[str, ChangeRecordKind | None] = {}
        for function, spec in (raw.get("functions") or {}).items():
            if function in self.functions or function in self.function_dispatchers:
                raise CatalogError(f"Function {function!r} is listed twice.")
            binding, kinds = _function_binding(tool_id, str(function), spec)
            self.functions[function] = binding
            declared.update(kinds)
            if binding.argument is None:
                assert binding.level is not None
                operations.append(OperationDefinitionV1(name=function, level=binding.level))
            else:
                operations += [
                    OperationDefinitionV1(name=f"{function}.{value}", level=level) for value, level in binding.values.items()
                ]
        # Only function operations: the ones below are decided elsewhere
        # (staged changes are checked when apply_change applies them).
        _check_record_kinds(tool_id, operations, declared)
        # Operations an enforcement point decides itself, not through a
        # function call (the sandbox launcher's code:unconfined run).
        for name, spec in (raw.get("operations") or {}).items():
            operations.append(OperationDefinitionV1(name=str(name), level=_level(spec, where=f"{tool_id}.{name}")))
        for kind, spec in (raw.get("changes") or {}).items():
            if kind in self.changes:
                raise CatalogError(f"Change kind {kind!r} is listed twice.")
            level = _level(spec, where=f"{tool_id}.changes.{kind}")
            self.changes[str(kind)] = (tool_id, level)
            operations.append(OperationDefinitionV1(name=change_operation(str(kind)), level=level))
        self.add_tool(ToolDefinitionV1(**_tool_fields(raw, operations)))

    def _load_broker_tool(self, raw: Mapping[str, Any]) -> None:
        tool_id = str(raw["tool_id"])
        provider = str(raw["broker_provider"])
        table_name = raw.get("command_table")
        operations: list[OperationDefinitionV1] = []
        for action, spec in (raw.get("actions") or {}).items():
            key = (provider, str(action))
            if key in self.broker_actions:
                raise CatalogError(f"Broker action {provider}.{action} is listed twice.")
            if spec in _ACTION_MODES:
                if spec == "command_table" and not table_name:
                    raise CatalogError(f"{tool_id}: {action} uses a command table but none is named.")
                self.broker_actions[key] = BrokerActionBinding(
                    tool_id, provider, str(action), mode=str(spec), command_table=table_name
                )
                continue
            level = _level(spec, where=f"{tool_id}.{action}")
            self.broker_actions[key] = BrokerActionBinding(tool_id, provider, str(action), level=level)
            operations.append(OperationDefinitionV1(name=str(action), level=level))
        if table_name:
            policy = load_cli_policy(str(table_name))
            operations += [
                OperationDefinitionV1(name=f"command:{path}", level=level)
                for path, level in sorted(policy.table.commands.items())
            ]
        self.add_tool(ToolDefinitionV1(**_tool_fields(raw, operations)))

    def load_file(self, path: Path, *, instance: bool = False) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise CatalogError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(data, Mapping):
            raise CatalogError(f"{path} must contain a mapping.")
        if instance:
            # An instance file adds web API tools and nothing else: no function
            # dispatch, no tool sets (joining "standard" would widen every
            # starter profile without anyone reviewing it).
            extra = sorted(set(data) - {"tools"})
            if extra:
                raise CatalogError(f"{path}: instance definitions may only list tools, not {', '.join(extra)}.")
        try:
            for raw in data.get("tools") or ():
                self._load_tool_entry(raw, path=path, instance=instance)
            for function, target in (data.get("dispatchers") or {}).items():
                self.function_dispatchers[str(function)] = str(target)
            for provider, target in (data.get("dispatch_providers") or {}).items():
                self.broker_dispatch_providers[str(provider)] = str(target)
            self.not_offered.update(str(name) for name in data.get("not_offered") or ())
            for tool_id, entry in (data.get("on_request") or {}).items():
                self.on_request[str(tool_id)] = frozenset(str(name) for name in (entry or {}).get("keep_shown") or ())
            for raw_set in data.get("tool_sets") or ():
                tool_set = ToolSetDefinitionV1(**raw_set)
                self.tool_sets[tool_set.tool_set_id] = tool_set
        except CatalogError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CatalogError(f"{path}: {exc}") from exc

    @staticmethod
    def _check_instance_entry(raw: Mapping[str, Any], *, path: Path, tool_id: str) -> None:
        """An instance file adds web API tools and installed CLIs only, in no tool set, reaching nothing."""

        if not tool_id.startswith("http:"):
            raise CatalogError(f"{path}: instance definitions may only add web API tools (http:*) and installed CLIs.")
        if raw.get("reached_through"):
            raise CatalogError(f"{path}: {tool_id} cannot name a function tool from an instance definition.")
        if raw.get("tool_sets"):
            raise CatalogError(f"{path}: {tool_id} cannot join tool sets from an instance definition.")

    def _load_tool_entry(self, raw: Mapping[str, Any], *, path: Path, instance: bool) -> None:
        tool_id = str(raw.get("tool_id") or "")
        if raw.get("installed_cli"):
            # Only from the instance's files: an administrator approved it (access_control.proposal_clis).
            if not instance or raw.get("tool_sets"):
                raise CatalogError(f"{path}: {tool_id}: installed CLIs come only from the instance's files, in no tool set.")
            self.add_installed_cli(raw)
            return
        if instance:
            self._check_instance_entry(raw, path=path, tool_id=tool_id)
        if raw.get("reached_through"):
            self.reached_through[tool_id] = str(raw["reached_through"])
        self._load_by_shape(raw, path=path, instance=instance, tool_id=tool_id)

    def _load_by_shape(self, raw: Mapping[str, Any], *, path: Path, instance: bool, tool_id: str) -> None:
        if "functions" in raw or "operations" in raw:
            self._load_function_tool(raw)
        elif "broker_provider" in raw:
            self._load_broker_tool(raw)
        elif "service_url_setting" in raw:
            if instance or not tool_id.startswith("http:"):
                raise CatalogError(f"{path}: only built-in web API tools take their address from a setting.")
            self._load_service_tool(raw, path=path)
        elif tool_id.startswith("http:"):
            self.add_tool(ToolDefinitionV1(**_tool_fields(raw, _http_operations(raw))))
        else:
            raise CatalogError(f"{path}: {tool_id!r} has no functions, operations, broker actions, or hosts.")

    def add_installed_cli(self, raw: Mapping[str, Any]) -> ToolDefinitionV1:
        """A CLI the team installed: its secure_cli provider is its name, its levels its command table."""

        cli = InstalledCliV1(**raw["installed_cli"])
        name = cli.name
        if name in self.cli_policies or any(provider == name for provider, _action in self.broker_actions):
            raise CatalogError(f"The CLI name {name!r} is already a broker provider.")
        policy = policy_from_data(name, cli.command_table())
        operations = [
            OperationDefinitionV1(name=f"command:{path}", level=level)
            for path, level in sorted(policy.table.commands.items())
        ]
        tool = ToolDefinitionV1(**_tool_fields(raw, operations))
        self.add_tool(tool)
        self.cli_policies[name] = policy
        self.broker_actions[(name, INSTALLED_CLI_ACTION)] = BrokerActionBinding(
            tool.tool_id, name, INSTALLED_CLI_ACTION, mode="command_table", command_table=name
        )
        return tool

    def _load_service_tool(self, raw: Mapping[str, Any], *, path: Path) -> None:
        """A web API tool whose host and port come from the configured address of the service."""

        setting = str(raw["service_url_setting"])
        url = self.service_urls.get(setting) or DEFAULT_SERVICE_URLS.get(setting)
        origin = web_origin(url or "")
        if origin is None:
            raise CatalogError(f"{path}: {raw.get('tool_id')} needs an http(s) address in {setting}, not {url!r}.")
        team_only = list(raw.get("link_owners") or ()) == [LinkOwnerKind.TEAM.value]
        if raw.get("account") == AccountMode.LINKED_CREDENTIAL.value and not team_only:
            # The team's own server opens only with the team's token, so it is
            # always shared (access_control.account_sharing): a person has no
            # account of their own, and keeping it private would stop everyone.
            raise CatalogError(f"{path}: {raw.get('tool_id')} is this team's own server; it takes link_owners: [team].")
        fields = {key: value for key, value in raw.items() if key != "service_url_setting"}
        fields["hosts"] = [urlsplit(origin).hostname]
        self.add_tool(ToolDefinitionV1(**_tool_fields(fields, _http_operations(fields))), origin=origin)

    def load_mcp_connections(self, instance_root: Path) -> None:
        """Active MCP connections, each one team tool in the ``mcp_servers`` set (``mcp_tool_definition``)."""

        from mcp_control.operation_levels import load_operation_levels
        from mcp_control.registry import (
            MCPConnectionLifecycle,
            MCPConnectionRecordV1,
            resolve_mcp_connection_paths,
        )

        paths = resolve_mcp_connection_paths(instance_root)
        records_dir = paths.records
        if not records_dir.is_dir():
            return
        for record_path in sorted(records_dir.glob("mcp_conn_*.json")):
            record = MCPConnectionRecordV1.model_validate_json(record_path.read_text(encoding="utf-8"))
            if record.lifecycle is not MCPConnectionLifecycle.ACTIVE:
                continue
            levels = load_operation_levels(paths, record.connection_id).operations
            self.add_mcp_tool(record, levels)

    def add_mcp_tool(self, record: Any, levels: Mapping[str, Any] | None = None) -> ToolDefinitionV1:
        """Add one MCP connection's tool, and its function-name prefix, to the catalog."""

        tool = mcp_tool_definition(record, levels)
        self.add_tool(tool)
        self.mcp_prefixes[record.descriptor.tool_name_prefix] = tool.tool_id
        return tool

    def load_procedures(self, handlers_file: Path) -> None:
        """Trusted installed-procedure handlers, each needing its reviewed level."""

        data = yaml.safe_load(handlers_file.read_text(encoding="utf-8")) or {}
        for handler_id, raw in (data.get("handlers") or {}).items():
            self.add_tool(
                ToolDefinitionV1(
                    tool_id=f"procedure:{handler_id}",
                    kind=ToolKind.PROCEDURE,
                    display_name=str(raw.get("display_name") or handler_id),
                    description=str(raw.get("impact_description") or "")[:2000],
                    operations=(
                        OperationDefinitionV1(name="invoke", level=_level(raw.get("level"), where=handler_id)),
                    ),
                )
            )

    def resolve_installed_procedure(self, definition_id: str) -> OperationResolutionV1:
        """``installed_permission.<handler>.<digest>`` -> the handler's procedure tool."""

        text = str(definition_id or "")
        handler = text[len(INSTALLED_PROCEDURE_PREFIX):].rsplit(".", 1)[0] if text.startswith(INSTALLED_PROCEDURE_PREFIX) else ""
        tool = self.tools.get(f"procedure:{handler}") if handler else None
        if tool is None:
            return OperationResolutionV1(
                tool_id="procedure:unregistered",
                operation="invoke",
                level=AccessLevel.FULL,
                source=ClassificationSource.UNREVIEWED,
                detail="No trusted handler matches this definition.",
            )
        return OperationResolutionV1(
            tool_id=tool.tool_id, operation="invoke", level=tool.operations[0].level, source=ClassificationSource.TRUSTED
        )

    def resolve_mcp(self, tool_id: str, operation: str) -> OperationResolutionV1:
        tool = self.tools.get(tool_id)
        definition = tool.operation(operation) if tool is not None else None
        if definition is None:
            return OperationResolutionV1(
                tool_id=tool_id,
                operation=operation,
                level=AccessLevel.FULL,
                source=ClassificationSource.UNREVIEWED,
                detail="The operation is not in the installed connection's catalog.",
            )
        return OperationResolutionV1(
            tool_id=tool_id, operation=operation, level=definition.level, source=definition.source
        )

    def validate(self) -> None:
        for tool_id, function_tool in self.reached_through.items():
            if function_tool not in self.tools or not function_tool.startswith("fn:"):
                raise CatalogError(f"{tool_id} is reached through {function_tool!r}, which is not a function tool.")
        for tool in self.tools.values():
            for tool_set in tool.tool_sets:
                if tool_set not in self.tool_sets:
                    raise CatalogError(f"{tool.tool_id} names unknown tool set {tool_set!r}.")
            if "standard" in tool.tool_sets and (
                tool.kind is ToolKind.CODE or tool.owner_scope is not OwnerScope.TEAM
            ):
                raise CatalogError(f"{tool.tool_id}: code, platform and administrator-only tools are never standard.")
            # A person's own tool is opened to agents only by that person
            # (OwnerScope.PERSON): a tool set would give it by role instead.
            if tool.owner_scope is OwnerScope.PERSON and tool.tool_sets:
                raise CatalogError(f"{tool.tool_id}: a person's own tool belongs to no tool set.")
        self._validate_prerequisites()

    def _validate_prerequisites(self) -> None:
        """Setup prerequisites name catalog tools and never loop (set_up_tools orders by them)."""

        for tool in self.tools.values():
            for prerequisite in tool.setup.prerequisites if tool.setup else ():
                if prerequisite not in self.tools:
                    raise CatalogError(f"{tool.tool_id} needs {prerequisite!r} set up first, which is not a tool.")
        for tool_id in self.tools:
            self.setup_order([tool_id])

    def setup_order(self, tool_ids: Iterable[str]) -> list[str]:
        """The tools with their setup prerequisites, prerequisites first; raises on a loop."""

        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(tool_id: str) -> None:
            if tool_id in ordered:
                return
            if tool_id in visiting:
                raise CatalogError(f"Setup prerequisites of {tool_id} form a loop.")
            visiting.add(tool_id)
            tool = self.tools.get(tool_id)
            for prerequisite in tool.setup.prerequisites if tool is not None and tool.setup else ():
                visit(prerequisite)
            visiting.discard(tool_id)
            ordered.append(tool_id)

        for tool_id in tool_ids:
            visit(tool_id)
        return ordered

    # Resolution --------------------------------------------------------------

    def resolve_function(self, function: str, arguments: Mapping[str, Any] | None = None) -> OperationResolutionV1:
        """The operation a function call needs. Dispatchers are resolved by the tool they reach."""

        if self.function_dispatchers.get(function) == "procedure":
            return self.resolve_installed_procedure(str((arguments or {}).get("definition_id") or ""))
        binding = self.functions.get(function)
        if binding is None:
            for prefix, tool_id in self.mcp_prefixes.items():
                if function.startswith(f"{prefix}_"):
                    return self.resolve_mcp(tool_id, function[len(prefix) + 1 :])
            return OperationResolutionV1(
                tool_id=UNREGISTERED_FUNCTION_TOOL,
                operation=function,
                level=AccessLevel.FULL,
                source=ClassificationSource.UNREVIEWED,
                detail="Function is not in the catalog.",
            )
        if binding.argument is None:
            assert binding.level is not None
            return OperationResolutionV1(
                tool_id=binding.tool_id, operation=function, level=binding.level, source=ClassificationSource.TRUSTED
            )
        value = str((arguments or {}).get(binding.argument) or "").strip().lower()
        level = binding.values.get(value)
        return OperationResolutionV1(
            tool_id=binding.tool_id,
            operation=f"{function}.{value or 'unspecified'}",
            level=level or AccessLevel.FULL,
            source=ClassificationSource.TRUSTED if level else ClassificationSource.UNREVIEWED,
        )

    def record_kind(self, function: str, arguments: Mapping[str, Any] | None = None) -> ChangeRecordKind | None:
        """The kind of record a function call changes, when its changes run first in a model step."""

        binding = self.functions.get(function)
        if binding is None:
            return None
        if binding.argument is None:
            return binding.record_kind
        value = str((arguments or {}).get(binding.argument) or "").strip().lower()
        return binding.record_kinds.get(value)

    def resolve_http(self, method: str, url: str) -> OperationResolutionV1:
        """The web API operation a request needs, found by its host."""

        host = (urlsplit(url).hostname or "").lower()
        tool_id = self.http_origins.get(web_origin(url) or "") or self.http_hosts.get(host)
        if tool_id is None:
            return OperationResolutionV1(
                tool_id=UNLISTED_WEB_TOOL,
                operation=f"{str(method or '').lower() or 'request'}:{host or 'unknown'}",
                level=AccessLevel.FULL,
                source=ClassificationSource.UNREVIEWED,
                detail="No web API tool covers this host.",
            )
        return classify_http_request(self.tools[tool_id], method, url)

    def resolve_broker_action(self, provider: str, action: str, params: Mapping[str, Any]) -> OperationResolutionV1:
        """The operation one broker action needs."""

        if self.broker_dispatch_providers.get(provider) == "http":
            method = action if action != "request" else str(params.get("method") or "GET")
            return self.resolve_http(method.upper(), str(params.get("url") or params.get("endpoint_url") or ""))
        binding = self.broker_actions.get((provider, action))
        if binding is None:
            return OperationResolutionV1(
                tool_id=f"cli:{provider}" if provider else UNREGISTERED_FUNCTION_TOOL,
                operation=action,
                level=AccessLevel.FULL,
                source=ClassificationSource.UNREVIEWED,
                detail="Broker action is not in the catalog.",
            )
        if binding.mode == "linked_tool":
            tool_id = str(params.get("tool_id") or "")
            known = tool_id in self.tools and self.tools[tool_id].account is AccountMode.LINKED_CREDENTIAL
            return OperationResolutionV1(
                tool_id=tool_id if known else UNREGISTERED_FUNCTION_TOOL,
                operation=action,
                level=AccessLevel.FULL,
                source=ClassificationSource.TRUSTED if known else ClassificationSource.UNREVIEWED,
                detail="" if known else "The named tool does not use a linked credential.",
            )
        if binding.mode == "http":
            return self.resolve_http(
                str(params.get("method") or "GET").upper(), str(params.get("url") or params.get("endpoint_url") or "")
            )
        if binding.mode == "command_table":
            table = str(binding.command_table)
            try:
                argv = argv_from_params(params, cli=table)
            except ValueError as exc:
                return OperationResolutionV1(
                    tool_id=binding.tool_id,
                    operation="command:unparsed",
                    level=AccessLevel.FULL,
                    source=ClassificationSource.UNREVIEWED,
                    detail=str(exc)[:500],
                )
            return self.resolve_command_line(binding.tool_id, table, argv)
        assert binding.level is not None
        return OperationResolutionV1(
            tool_id=binding.tool_id, operation=action, level=binding.level, source=ClassificationSource.TRUSTED
        )

    def resolve_command_line(self, tool_id: str, table: str, argv: Iterable[str]) -> OperationResolutionV1:
        policy = self.cli_policies.get(table) or load_cli_policy(table)
        try:
            parsed = parse_command_line(list(argv), policy.table)
        except CommandLineRejected as exc:
            return OperationResolutionV1(
                tool_id=tool_id,
                operation="command:unparsed",
                level=AccessLevel.FULL,
                source=ClassificationSource.UNREVIEWED,
                detail=str(exc)[:500],
            )
        level = classify_passthrough(policy, parsed)
        known = policy.table.level_for(parsed.command_path) is not None or parsed.command_path == ("api",)
        return OperationResolutionV1(
            tool_id=tool_id,
            operation=f"command:{parsed.dotted_path or 'global'}",
            level=level,
            source=ClassificationSource.TRUSTED if known else ClassificationSource.UNREVIEWED,
        )

    def tools_in_set(self, tool_set_id: str) -> tuple[ToolDefinitionV1, ...]:
        return tuple(tool for tool in self.tools.values() if tool_set_id in tool.tool_sets)


def mcp_tool_id(connection_name: str) -> str:
    """The catalog tool id of an installed MCP connection."""

    return f"mcp:{connection_name.lower()}"


def missing_operation(tools: Mapping[str, ToolDefinitionV1], target: TargetRefV1) -> str | None:
    """The operation an operation target names that its tool (in ``tools``, a catalog's) no longer has; else None.

    Every way of adding a block checks its operation, so a stored block names
    a missing one only after an update renamed or removed it; such a block
    applies to the whole tool (``access_control.stale_blocks``). None as well
    when the tool itself is not there (an MCP server that is not active is
    left out of the catalog): nothing could then call it.
    """

    if target.kind is not TargetKind.OPERATION:
        return None
    tool_id, _, operation = target.id.partition("#")
    tool = tools.get(tool_id)
    if tool is None or tool.operation(operation) is not None:
        return None
    return operation


def mcp_tool_definition(record: Any, levels: Mapping[str, Any] | None = None) -> ToolDefinitionV1:
    """The catalog tool of one MCP connection record (``mcp_control.registry.MCPConnectionRecordV1``).

    - Every installed server is a team tool in the ``mcp_servers`` set, which
      members hold (``access_control.standard_grants``), so each person
      authorizes agents for their own work with it, as for other team tools.
    - An operation's level is ``full`` until an administrator confirms a
      lower one for its exact definition (``levels``: the connection's
      ``mcp_control.operation_levels`` entries). A grant at ``full`` on the
      whole tool therefore covers every operation, including unconfirmed ones
      and ones the server adds later; confirming levels only matters for
      read-only or read-and-change grants.
    - Credential slots the connection declared become the default injections
      of its account link (``secure_tool_broker.mcp_runtime``).

    A pending record gives the tool as it will be once installed (it has no
    operations yet); access proposals use that to check their grant rows.
    """

    operations = []
    for operation in record.operations:
        entry = (levels or {}).get(operation.operation_name)
        confirmed = (
            entry.confirmed_level
            if entry is not None and entry.operation_digest == operation.operation_digest
            else None
        )
        operations.append(
            OperationDefinitionV1(
                name=operation.operation_name,
                level=confirmed or AccessLevel.FULL,
                description=operation.description[:1000],
                source=ClassificationSource.ADMIN_CONFIRMED if confirmed else ClassificationSource.UNREVIEWED,
                suggested_level=entry.suggested_level if entry is not None else None,
            )
        )
    slots = tuple(
        CredentialInjectionV1(
            placement=InjectionPlacement.HEADER if slot.target_kind == "header" else InjectionPlacement.ENV,
            name=slot.target_name,
            field=slot.field_name,
            template=slot.template or "{value}",
        )
        for slot in record.descriptor.credential_injections
    )
    name = record.descriptor.connection_name
    return ToolDefinitionV1(
        tool_id=mcp_tool_id(name),
        kind=ToolKind.MCP,
        display_name=name,
        description=f"The MCP server {name}; its functions are named {record.descriptor.tool_name_prefix}_<tool>.",
        operations=tuple(operations),
        account=AccountMode.LINKED_CREDENTIAL if slots else AccountMode.NONE,
        credential_injections=slots,
        tool_sets=(MCP_SERVERS_TOOL_SET,),
    )


def load_catalog(
    instance_root: str | Path | None = None,
    *,
    service_urls: Mapping[str, str] | None = None,
) -> ToolCatalog:
    """The built-in catalog plus the instance's web API tools and MCP connections.

    ``service_urls`` maps a ``service_url_setting`` (``CRAWL4AI_URL_SETTING``)
    to the configured address; the broker passes it so the tool matches the
    service the agents' tools call.
    """

    catalog = ToolCatalog(service_urls=dict(service_urls or {}))
    for path in sorted(DEFINITIONS_DIR.glob("*.yaml")):
        catalog.load_file(path)
    catalog.load_procedures(PROCEDURE_HANDLERS_FILE)
    if instance_root:
        layout = InstanceLayout.at(instance_root)
        instance_dir = layout.instance_tools_dir
        for path in sorted(instance_dir.glob("*.yaml")) if instance_dir.is_dir() else ():
            catalog.load_file(path, instance=True)
        catalog.load_mcp_connections(layout.root)
    catalog.validate()
    return catalog


def _instance_paths(instance_root: Path) -> tuple[Path, ...]:
    """Folders whose files change the catalog: team-added tools, MCP records and levels."""

    from mcp_control.operation_levels import operation_levels_dir
    from mcp_control.registry import resolve_mcp_connection_paths

    layout = InstanceLayout.at(instance_root)
    registry = resolve_mcp_connection_paths(layout.root)
    return (layout.instance_tools_dir, registry.records, operation_levels_dir(registry))


class CatalogSource:
    """The current catalog for a long-running process (the broker).

    Instance tool definitions, MCP connection records and confirmed MCP levels
    change while the process runs; ``current()`` reloads the catalog when any of
    those files change (their names, sizes and modification times), so a newly
    confirmed level or connection is decided on the next request. A reload that
    fails keeps the previous catalog and logs the error.
    """

    def __init__(
        self,
        instance_root: str | Path | None,
        *,
        service_urls: Mapping[str, str] | None = None,
    ) -> None:
        self._root = Path(instance_root).expanduser().resolve() if instance_root else None
        self._service_urls = dict(service_urls or {})
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._catalog: ToolCatalog | None = None

    def _current_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        if self._root is None:
            return ()
        entries: list[tuple[str, int, int]] = []
        for directory in _instance_paths(self._root):
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((str(path), stat.st_size, stat.st_mtime_ns))
        return tuple(entries)

    def current(self) -> ToolCatalog:
        fingerprint = self._current_fingerprint()
        if self._catalog is not None and fingerprint == self._fingerprint:
            return self._catalog
        try:
            catalog = load_catalog(self._root, service_urls=self._service_urls)
        except Exception:
            if self._catalog is None:
                raise
            logger.exception(
                "Could not reload the tool catalog; keeping the previous one. event=tool_catalog_reload_failed"
            )
            return self._catalog
        self._catalog, self._fingerprint = catalog, fingerprint
        return catalog


__all__ = [
    "CHANGE_OPERATION_PREFIX",
    "CRAWL4AI_URL_SETTING",
    "DEFAULT_SERVICE_URLS",
    "BrokerActionBinding",
    "CatalogError",
    "CatalogSource",
    "DEFINITIONS_DIR",
    "FunctionBinding",
    "MCP_SERVERS_TOOL_SET",
    "ToolCatalog",
    "UNLISTED_WEB_TOOL",
    "UNREGISTERED_FUNCTION_TOOL",
    "change_operation",
    "load_catalog",
    "mcp_tool_definition",
    "mcp_tool_id",
    "missing_operation",
    "web_origin",
]
