"""Check mcp_control.sdk_compat end to end on whichever MCP SDK line is installed (access plan 7e).

Run it with the project's environment (MCP 1.x today) and with a throwaway
environment holding MCP 2.x before moving the pin:

    python scripts/check_mcp_sdk_compat.py

It starts the stdio echo server (mcp/noop_mcp.py) and a low-level server
over streamable HTTP built like the hosted MCP server, then negotiates, lists
tools page by page, and calls a tool on each through the compatibility
helpers. It needs only the ``mcp`` package and its own dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from mcp_control import sdk_compat  # noqa: E402

logger = logging.getLogger("check_mcp_sdk_compat")


async def _exercise(session: ClientSession, tool: str, arguments: dict) -> str:
    await sdk_compat.negotiate(session)
    names: list[str] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(params=sdk_compat.pagination_params(cursor))
        names += [item.name for item in page.tools]
        schemas = [sdk_compat.input_schema(item) for item in page.tools]
        assert all(isinstance(schema, dict) for schema in schemas), schemas
        cursor = sdk_compat.next_cursor(page)
        if not cursor:
            break
    assert tool in names, names
    result = await session.call_tool(tool, arguments)
    assert sdk_compat.is_error(result) is False, result
    wire = sdk_compat.call_result_wire(result)
    assert "resultType" not in wire and "content" in wire, wire
    return str(wire)


async def check_stdio() -> str:
    parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp" / "noop_mcp.py")])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            return await _exercise(session, "noop_echo", {"message": "hello"})


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def check_streamable_http() -> str:
    import uvicorn

    port = _free_port()

    async def list_tools():
        return [sdk_compat.tool_definition(name="echo", description="Echo", input_schema={"type": "object"})]

    async def call_tool(name: str, arguments: dict):
        if name != "echo":
            return sdk_compat.error_result("Unknown tool.")
        return sdk_compat.structured_result({"arguments": arguments})

    app = sdk_compat.streamable_http_app(
        name="compat-check",
        instructions="A compatibility check.",
        host="127.0.0.1",
        port=port,
        path="/mcp",
        list_tools=list_tools,
        call_tool=call_tool,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    try:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                return await _exercise(session, "echo", {"hello": "world"})
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    line = "2.x" if sdk_compat.MCP_V2 else "1.x"
    logger.info("MCP SDK line: %s", line)
    logger.info("stdio: %s", asyncio.run(check_stdio()))
    logger.info("streamable HTTP: %s", asyncio.run(check_streamable_http()))
    logger.info("mcp_control.sdk_compat works on the %s line.", line)


if __name__ == "__main__":
    main()
