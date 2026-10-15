"""mcp_control.sdk_compat on the installed MCP SDK line (access plan 7e).

The same checks run against MCP 2.x with scripts/check_mcp_sdk_compat.py in a
throwaway environment before the pin moves; here they keep the 1.x branch
honest: a real stdio server and a real streamable-HTTP server, negotiation,
paged tool lists, a tool call, and the result's wire form.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from mcp import types

from custom_messages.contracts.mcp_v1 import MCPToolResultV1
from mcp_control import sdk_compat

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_mcp_sdk_compat", ROOT / "scripts" / "check_mcp_sdk_compat.py")
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_a_stdio_server_and_client_work_through_the_compatibility_names() -> None:
    wire = asyncio.run(check.check_stdio())
    assert "hello" in wire


def test_a_low_level_server_over_streamable_http_answers_with_structured_results() -> None:
    wire = asyncio.run(check.check_streamable_http())
    assert "'structuredContent': {'arguments': {'hello': 'world'}}" in wire


def test_the_result_contract_keeps_only_its_own_fields() -> None:
    # 2.x adds fields such as resultType to a result's wire form; the contract leaves them out.
    result = sdk_compat.structured_result({"score": 1})
    portable = MCPToolResultV1.from_sdk_result(result)
    assert portable.structured_content == {"score": 1} and portable.is_error is False
    error = MCPToolResultV1.from_sdk_result(sdk_compat.error_result("No such tool."))
    assert error.is_error is True and error.content[0]["text"] == "No such tool."


def test_messages_and_tools_parse_the_same_way_on_either_line() -> None:
    message = sdk_compat.parse_jsonrpc_message('{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}')
    assert sdk_compat.jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) == message
    tool = sdk_compat.tool_definition(name="t", description="d", input_schema={"type": "object"})
    assert isinstance(tool, types.Tool)
    assert sdk_compat.input_schema(tool) == {"type": "object"} and sdk_compat.output_schema(tool) is None
