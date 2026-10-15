"""A read-only echo MCP server for live tests, on the MCP SDK's 1.x or 2.x line (mcp_control.sdk_compat)."""

import os
import sys

# Run as a script from mcp/, so add the project root for mcp_control.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mcp.types import ToolAnnotations  # noqa: E402

from mcp_control.sdk_compat import run_tool_server, tool_server  # noqa: E402

mcp = tool_server("noop_mcp")


@mcp.tool(
    name="noop_echo",
    annotations=ToolAnnotations(
        title="Noop Echo",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def noop_echo(message: str = "ok") -> str:
    """Echo the provided message unchanged."""
    return message


if __name__ == "__main__":
    run_tool_server(mcp, "stdio")
