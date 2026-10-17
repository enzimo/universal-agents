"""A Strands tool context shaped like the one a real turn gives the command tools.

``shell`` and ``python_env`` read their turn's stop from the tool context
(custom_tools.tool_stop). Every turn passes ``callback_handler`` to
``invoke_async`` as an extra keyword (custom_agents.sdk.strands.turn), and
Strands then hands tools ``{**kwargs, "invocation_state": <the turn's dict>}``:
the turn's values, the ToolStop among them, sit one level down. This helper
builds that shape, so a test cannot pass with a flat dict that production
never produces.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_tools.tool_stop import TOOL_STOP_STATE_KEY, ToolStop


def tool_context(stop: ToolStop | None = None, *, tool_use_id: str = "test-tool-use") -> SimpleNamespace:
    """The attributes of strands.types.tools.ToolContext that the command tools read."""

    stop = stop if stop is not None else ToolStop(agent_id="test-agent", task_id="test-task")
    return SimpleNamespace(
        invocation_state={
            "callback_handler": lambda **_: None,
            "invocation_state": {TOOL_STOP_STATE_KEY: stop},
            "request_state": {},
        },
        tool_use={"toolUseId": tool_use_id, "name": "test", "input": {}},
    )
