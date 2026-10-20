"""What a tool returned, for tests: its JSON text, or the text of a failed result.

Tools return JSON text when a call succeeds and a failed Strands result when it
does not (``custom_tools.tool_results``); tests read both the same way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import yaml

from custom_tools.tool_results import result_text


def tool_json(result: str | Mapping[str, Any]) -> Any:
    return json.loads(result_text(result))


def tool_yaml(result: str | Mapping[str, Any]) -> Any:
    return yaml.safe_load(result_text(result))


async def sdk_tool_result(tool: Any, inputs: Mapping[str, Any], *, context: Any = None) -> dict[str, Any]:
    """Invoke the real Strands decorator and return the result the executor reads."""

    tool_use = {"toolUseId": "tooluse_result_test", "name": tool.tool_name, "input": dict(inputs)}
    async for event in tool.stream(tool_use, {}, _tool_context=context):
        if event.get("type") == "tool_result":
            return event["tool_result"]
    raise AssertionError("The tool did not return a result.")
