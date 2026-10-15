"""The MCP Python SDK names that differ between its 1.x and 2.x lines (access plan 7e).

The ``mcp`` package renamed and moved several public names in 2.0 (``FastMCP``
became ``MCPServer``, ``McpError`` became ``MCPError``, result fields moved
from camelCase to snake_case, the low-level server takes its handlers in the
constructor, and the ``initialize`` handshake gave way to negotiation). Code
here imports every such name from this module instead of from ``mcp``, so the
move to 2.x changes the requirement pin and this module, not every caller.

This follows Strands' own approach (``strands/tools/mcp/_compat.py``): the
line is feature-probed rather than version-parsed, pure renames are resolved
once, and ``MCP_V2`` is checked only where behavior differs. Both branches were
checked against mcp 1.30 and 2.1.

Not covered: the address-pinning HTTP transport in ``mcp_control.transports``
is built on ``httpx`` and the SDK's 1.x client factory; 2.x uses ``httpx2``,
so the move ports that transport (TODO "mcp 2.x").
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from mcp import ClientSession, types

# ``ClientSession.discover`` is the 2.x replacement for the removed handshake.
MCP_V2: bool = hasattr(ClientSession, "discover")

try:
    from mcp.shared.exceptions import MCPError  # type: ignore[attr-defined]
except ImportError:  # the 1.x line spells it McpError
    from mcp.shared.exceptions import McpError as MCPError  # type: ignore[attr-defined, no-redef]

# Fields 2.x adds to a result's wire form that the broker's result contract
# (custom_messages.contracts.mcp_v1.MCPToolResultV1) does not carry.
_V2_ONLY_RESULT_FIELDS = ("resultType",)

ToolLister = Callable[[], Awaitable[list[types.Tool]]]
ToolCaller = Callable[[str, dict[str, Any]], Awaitable[types.CallToolResult]]


# -- client side ---------------------------------------------------------------------


async def negotiate(session: ClientSession) -> None:
    """Negotiate an entered session: the 1.x handshake, or 2.x's auto negotiation (which falls back to it)."""

    if MCP_V2:
        from mcp.client.client import negotiate_auto  # type: ignore[import-not-found]

        await negotiate_auto(session)
        return
    await session.initialize()


def read_timeout(timeout: timedelta | None) -> Any:
    """A session read timeout: a ``timedelta`` on 1.x, seconds on 2.x."""

    if timeout is None:
        return None
    return timeout.total_seconds() if MCP_V2 else timeout


def pagination_params(cursor: str | None) -> types.PaginatedRequestParams | None:
    """The ``params`` for a paginated list call; the ``cursor`` keyword is gone in 2.x."""

    return types.PaginatedRequestParams(cursor=cursor) if cursor else None


def next_cursor(list_result: Any) -> str | None:
    return list_result.next_cursor if MCP_V2 else list_result.nextCursor


def input_schema(tool: Any) -> dict[str, Any]:
    return dict((tool.input_schema if MCP_V2 else tool.inputSchema) or {})


def output_schema(tool: Any) -> dict[str, Any] | None:
    value = tool.output_schema if MCP_V2 else tool.outputSchema
    return dict(value) if value is not None else None


def is_error(call_result: Any) -> bool | None:
    return call_result.is_error if MCP_V2 else call_result.isError


def call_result_wire(call_result: Any) -> dict[str, Any]:
    """A tool result in its JSON wire form (camelCase on both lines), without 2.x-only fields."""

    wire = call_result.model_dump(mode="json", by_alias=True, exclude_none=True)
    for field in _V2_ONLY_RESULT_FIELDS:
        wire.pop(field, None)
    return wire


def parse_jsonrpc_message(raw: str | bytes) -> Any:
    """One JSON-RPC message; 2.x made ``JSONRPCMessage`` a plain union validated by an adapter."""

    if MCP_V2:
        return types.jsonrpc_message_adapter.validate_json(raw)  # type: ignore[attr-defined]
    return types.JSONRPCMessage.model_validate_json(raw)


def jsonrpc_message(data: Mapping[str, Any]) -> Any:
    """One JSON-RPC message from a Python mapping."""

    if MCP_V2:
        return types.jsonrpc_message_adapter.validate_python(dict(data))  # type: ignore[attr-defined]
    return types.JSONRPCMessage.model_validate(dict(data))


# -- building tools and results (both lines accept camelCase keyword arguments) --------


def tool_definition(
    *,
    name: str,
    description: str,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any] | None = None,
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema=dict(input_schema),
        outputSchema=dict(output_schema) if output_schema is not None else None,
    )


def structured_result(structured: Mapping[str, Any]) -> types.CallToolResult:
    """A successful result carrying ``structured`` both as structured content and as JSON text."""

    value = dict(structured)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, indent=2, sort_keys=True))],
        structuredContent=value,
        isError=False,
    )


def error_result(message: str) -> types.CallToolResult:
    """A tool error the model reads; 2.x no longer turns handler exceptions into one."""

    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


# -- servers -----------------------------------------------------------------------


def streamable_http_app(
    *,
    name: str,
    instructions: str,
    host: str,
    port: int,
    path: str,
    list_tools: ToolLister,
    call_tool: ToolCaller,
) -> Any:
    """A Starlette app serving one low-level MCP server over streamable HTTP (JSON responses, sessions).

    ``call_tool`` returns a complete result and reports errors as error
    results, so both lines answer the same way.
    """

    if MCP_V2:
        from mcp.server import Server

        async def on_list_tools(_context: Any, _params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(tools=await list_tools())

        async def on_call_tool(_context: Any, params: Any) -> types.CallToolResult:
            return await call_tool(params.name, dict(params.arguments or {}))

        server = Server(name, instructions=instructions, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        return server.streamable_http_app(streamable_http_path=path, json_response=True, stateless_http=False, host=host)

    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

    fast = FastMCP(
        name=name,
        instructions=instructions,
        host=host,
        port=port,
        streamable_http_path=path,
        json_response=True,
        stateless_http=False,
    )
    low_level = fast._mcp_server
    low_level.list_tools()(list_tools)
    low_level.call_tool(validate_input=False)(call_tool)
    return fast.streamable_http_app()


def tool_server(name: str, *, host: str | None = None, port: int | None = None) -> Any:
    """A high-level server for ``@server.tool`` functions (``FastMCP`` on 1.x, ``MCPServer`` on 2.x).

    Run it with ``run_tool_server``: 2.x takes the address when it runs.
    """

    if MCP_V2:
        from mcp.server.mcpserver import MCPServer  # type: ignore[import-not-found]

        return MCPServer(name=name)
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

    options = {key: value for key, value in (("host", host), ("port", port)) if value is not None}
    return FastMCP(name, **options)


def run_tool_server(server: Any, transport: str, *, host: str | None = None, port: int | None = None) -> None:
    if MCP_V2 and transport != "stdio":
        options = {key: value for key, value in (("host", host), ("port", port)) if value is not None}
        server.run(transport, **options)
        return
    server.run(transport)


__all__ = [
    "MCPError",
    "MCP_V2",
    "call_result_wire",
    "error_result",
    "input_schema",
    "is_error",
    "jsonrpc_message",
    "negotiate",
    "next_cursor",
    "output_schema",
    "pagination_params",
    "parse_jsonrpc_message",
    "read_timeout",
    "run_tool_server",
    "streamable_http_app",
    "structured_result",
    "tool_definition",
    "tool_server",
]
