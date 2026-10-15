"""Which Strands functions an agent is offered, from the catalog tools it holds (access Phase 7).

Access is decided per call by the access hook; this module only decides which
functions the model sees, so its tool list follows its grants instead of
fixed tool groups (plan section 8.7). A catalog tool maps to functions like
this:

- ``fn:*`` and ``code:*`` tools: the functions the catalog binds to them
  (``code:shell`` is ``shell``, ``code:python`` is ``python_env``);
- web API and command-line tools (``http:*``, ``cli:*``) and stored
  credentials (``procedure:credentials``): the ``secure_cli`` dispatcher,
  except web API tools that trusted function tools call for the agent
  (``reached_through``, such as ``http:exa`` behind ``exa_search``): holding
  those gives no dispatcher, since the agent uses the function tool's
  functions;
- installed procedures (``procedure:<handler>``): ``invoke_installed_permission``;
- MCP servers (``mcp:<name>``) are not functions: a held server's tools are
  loaded from the broker as a whole (``mcp_tool_ids`` here, loaded by
  ``custom_agents.tool_sync``), named ``<prefix>_<tool>``.

Functions the catalog lists as ``not_offered`` are never shown. A tool held
only through a block-free grant counts; a block on the whole tool (or its tool
set) removes it from the list, and a block on one operation leaves the tool
listed because its other operations still work, unless the tool no longer has
that operation: such a block stops the whole tool (``missing_operation``,
``access_control.stale_blocks``).
"""

from __future__ import annotations

from collections.abc import Iterable

from access_catalog.catalog import ToolCatalog, missing_operation
from custom_messages.contracts.access_v2 import GrantKind, GrantV1, TargetKind, ToolKind

BROKER_DISPATCHER = "secure_cli"
PROCEDURE_DISPATCHER = "invoke_installed_permission"
CREDENTIALS_PROCEDURE = "procedure:credentials"


def tool_ids_of_grants(catalog: ToolCatalog, grants: Iterable[GrantV1]) -> set[str]:
    """The catalog tools the grants make usable, minus tools blocked as a whole."""

    held: set[str] = set()
    blocked: set[str] = set()
    for grant in grants:
        tool_ids = _tool_ids_of_target(catalog, grant.target.kind, grant.target.id)
        if grant.kind is GrantKind.ACCESS:
            held |= tool_ids
        elif grant.kind is GrantKind.BLOCK and (
            grant.target.kind is not TargetKind.OPERATION or missing_operation(catalog.tools, grant.target)
        ):
            blocked |= tool_ids
    return held - blocked


def _tool_ids_of_target(catalog: ToolCatalog, kind: TargetKind, target_id: str) -> set[str]:
    if kind is TargetKind.TOOL_SET:
        return {tool.tool_id for tool in catalog.tools_in_set(target_id)}
    if kind in {TargetKind.TOOL, TargetKind.OPERATION}:
        tool_id = target_id.partition("#")[0]
        return {tool_id} if tool_id in catalog.tools else set()
    return set()


def functions_of_tools(catalog: ToolCatalog, tool_ids: Iterable[str], *, broker_enabled: bool = True) -> set[str]:
    """The Strands function names for these catalog tools (see the module docstring)."""

    wanted = set(tool_ids)
    names = {name for name, binding in catalog.functions.items() if binding.tool_id in wanted}
    for tool_id in wanted:
        tool = catalog.tools.get(tool_id)
        if tool is None:
            continue
        if tool_id in catalog.reached_through:
            continue
        if broker_enabled and (tool.kind in {ToolKind.CLI, ToolKind.HTTP} or tool_id == CREDENTIALS_PROCEDURE):
            names.add(BROKER_DISPATCHER)
        elif tool.kind is ToolKind.PROCEDURE and tool_id != CREDENTIALS_PROCEDURE:
            names.add(PROCEDURE_DISPATCHER)
    return names - catalog.not_offered


def mcp_function_names(catalog: ToolCatalog, tool_id: str) -> set[str]:
    """The function names an MCP server's discovered operations get (``<prefix>_<operation>``), for find_tools."""

    prefixes = [prefix for prefix, owner in catalog.mcp_prefixes.items() if owner == tool_id]
    tool = catalog.tools.get(tool_id)
    if tool is None or not prefixes:
        return set()
    return {f"{prefixes[0]}_{operation.name}" for operation in tool.operations}


def mcp_tool_ids(catalog: ToolCatalog, tool_ids: Iterable[str]) -> set[str]:
    """The MCP servers among these catalog tools; their tools load per server, not per function."""

    return {tool_id for tool_id in tool_ids if (tool := catalog.tools.get(tool_id)) is not None and tool.kind is ToolKind.MCP}


def functions_of_tool_sets(catalog: ToolCatalog, tool_set_ids: Iterable[str]) -> set[str]:
    tool_ids = {tool.tool_id for set_id in tool_set_ids for tool in catalog.tools_in_set(set_id)}
    return functions_of_tools(catalog, tool_ids)


__all__ = [
    "BROKER_DISPATCHER",
    "PROCEDURE_DISPATCHER",
    "functions_of_tool_sets",
    "functions_of_tools",
    "mcp_function_names",
    "mcp_tool_ids",
    "tool_ids_of_grants",
]
