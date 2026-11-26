"""``run_tool_script``: chain read-only tool calls in one short sandboxed Python script.

Shown only when ``agent.tool_presentation`` is ``index_and_scripts``
(``custom_agents.tool_presentation.withheld_functions``). Every agent and
member holds it (``fn:tool_scripts`` in ``agent_runtime``) because the script
reaches nothing by itself: each call it makes is checked and decided like a
direct call (``custom_agents.tool_script_runtime``).
"""

from __future__ import annotations

import json
from typing import Any

from strands import tool
from strands.types.tools import ToolContext

from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import get_agent_ref
from custom_tools.tool_stop import tracked_tool_call


@tool(context=True)
def run_tool_script(code: str, tool_context: ToolContext) -> dict[str, Any]:
    """Run a short Python script that chains read-only tool calls and returns only what you need.

    Write a function body: call a tool with tools.<name>(input=value, ...)
    (inputs by name) and end with return <value>. Use it for several lookups
    in one step, for loops over results, and to cut large results down before
    you read them. Only functions marked [script] in your tool index may be
    called (a mark like script: action=list|read allows only those values);
    describe_tools gives their exact inputs. A refused or failed call raises
    ToolError, which the script may catch. Example:

        pages = tools.knowledge_search(query="travel policy")
        return [p["title"] for p in pages["results"][:5]]

    The script has no files, no network and no imports beyond Python's
    standard library; calls that change things must be made directly. The
    result lists every call the script made and what it printed.
    """

    from custom_agents.tool_script_runtime import TOOL_SCRIPT_NAME, ScriptRefused, run_script

    with tracked_tool_call(tool_context, TOOL_SCRIPT_NAME) as tool_stop:
        if tool_stop is not None and tool_stop.is_requested():
            return _result("error", {"error": "The script did not run because the task was stopped."})
        agent = get_agent_ref()
        if agent is None:
            return _result("error", {"error": "No agent is running this tool."})
        try:
            run = run_script(agent, tool_context.agent, tool_context.invocation_state, code, tool_stop=tool_stop)
        except ScriptRefused as exc:
            return _result("error", {"error": str(exc)})
        payload = run.payload()
        return _result("error" if payload["status"] == "error" else "success", payload)


def _result(status: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "content": [{"text": json.dumps(payload, default=str, sort_keys=True)}]}


TOOL_SCRIPT_TOOLS = (run_tool_script,)
PROVIDER = ToolProvider("tool_scripts", TOOL_SCRIPT_TOOLS)

__all__ = ["PROVIDER", "TOOL_SCRIPT_TOOLS", "run_tool_script"]
