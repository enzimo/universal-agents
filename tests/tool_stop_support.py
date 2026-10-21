"""A Strands tool context shaped like the one a real turn gives the command tools.

``shell`` and ``python_env`` read their turn's stop from the tool context
(custom_tools.tool_stop). A turn calls Strands only with keywords
``invoke_async`` declares (custom_agents.sdk.strands.turn_tool_stop), so tools
get the call's own copy of the turn's ``invocation_state``: the turn's values,
the ToolStop among them, beside Strands' ``request_state``.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_tools.tool_stop import TOOL_STOP_STATE_KEY, ToolStop


def tool_context(stop: ToolStop | None = None, *, tool_use_id: str = "test-tool-use") -> SimpleNamespace:
    """The attributes of strands.types.tools.ToolContext that the command tools read."""

    stop = stop if stop is not None else ToolStop(agent_id="test-agent", task_id="test-task")
    return SimpleNamespace(
        invocation_state={TOOL_STOP_STATE_KEY: stop, "request_state": {}},
        tool_use={"toolUseId": tool_use_id, "name": "test", "input": {}},
    )
