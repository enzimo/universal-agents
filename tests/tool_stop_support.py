"""A Strands tool context carrying a turn's ToolStop, for calling command tools directly.

``shell`` and ``python_env`` read their turn's stop from the tool context
(custom_tools.tool_stop); a model turn always supplies one.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_tools.tool_stop import TOOL_STOP_STATE_KEY, ToolStop


def tool_context(stop: ToolStop | None = None, *, tool_use_id: str = "test-tool-use") -> SimpleNamespace:
    """The attributes of strands.types.tools.ToolContext that the command tools read."""

    stop = stop if stop is not None else ToolStop(agent_id="test-agent", task_id="test-task")
    return SimpleNamespace(
        invocation_state={TOOL_STOP_STATE_KEY: stop},
        tool_use={"toolUseId": tool_use_id, "name": "test", "input": {}},
    )
