"""Run a Strands invocation under its turn's ToolStop, and wait for its commands.

Why this exists (details in ``custom_tools.tool_stop``): a stopped turn's
asyncio task ends at once, but a command a tool started keeps running in its
worker thread. The code that waits for the turn then archives the task, tells
the requester, frees the execution slot or hands the task to another worker
while that command still writes to the same folders.

``invoke_with_tool_stop`` is the one place a turn calls Strands. It gives the
invocation the turn's ToolStop (as ``cancel_signal`` and in
``invocation_state``). When the invocation ends by cancellation (a person's
stop, a delegated stop, a lease-loss handback, shutdown) or by an error while
tool calls still run, it asks the turn's commands to stop and waits up to
``TOOL_STOP_GRACE_SECONDS`` for those calls to end before passing the outcome
on. So everything that waits for the turn also waits for its commands, without
each stop path waiting on its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from custom_tools.tool_stop import (
    TOOL_STOP_GRACE_SECONDS,
    TOOL_STOP_STATE_KEY,
    RunningToolCall,
    ToolStop,
)

# Why a turn's commands were stopped: short codes for the ToolStop and its logs.
# A person's or a delegating agent's stop (control_runtime.stop_task_execution).
TASK_STOPPED_REASON = "task_stopped"
# A worker that lost its launch lease hands the task back (lease_loss_wind_down).
HANDBACK_REASON = "lease_loss_handback"
# The turn was cancelled without a stop naming a reason, such as at shutdown.
TURN_CANCELLED_REASON = "turn_cancelled"


def attach_tool_stop(invoke_kwargs: dict[str, Any], tool_stop: ToolStop) -> None:
    """Give an invocation the turn's ToolStop, replacing any earlier turn's.

    ``invocation_state`` is changed in place: the turn reads it again after the
    call (provider-overflow recovery), and a resumed interrupted turn reuses it.
    """

    invoke_kwargs["cancel_signal"] = tool_stop.event
    state = invoke_kwargs.get("invocation_state")
    if state is None:
        state = invoke_kwargs["invocation_state"] = {}
    state[TOOL_STOP_STATE_KEY] = tool_stop


async def invoke_with_tool_stop(
    model: Any,
    prompt: Any,
    *,
    tool_stop: ToolStop,
    invoke_kwargs: dict[str, Any],
    logger: logging.Logger,
) -> Any:
    """``model.invoke_async(prompt, **invoke_kwargs)``; its commands end before it does."""

    attach_tool_stop(invoke_kwargs, tool_stop)
    try:
        result = await model.invoke_async(prompt, **invoke_kwargs)
    except asyncio.CancelledError:
        await stop_tool_calls(tool_stop, logger, reason=TURN_CANCELLED_REASON)
        raise
    except Exception as exc:
        # Strands awaits every tool call before it returns, so a call can still
        # run here only when the invocation broke off; it belongs to nothing now.
        if tool_stop.running():
            await stop_tool_calls(tool_stop, logger, reason=f"invocation_failed:{type(exc).__name__}")
        raise
    _report_tool_calls_after_return(tool_stop, logger)
    return result


async def stop_tool_calls(tool_stop: ToolStop, logger: logging.Logger, *, reason: str) -> None:
    """Ask the turn's commands to stop and wait, bounded, until its tool calls end."""

    tool_stop.request(reason)
    if not tool_stop.running():
        return
    still_running = await tool_stop.wait_until_idle(TOOL_STOP_GRACE_SECONDS)
    if still_running:
        # A process the supervisor started is ended within seconds, so what is
        # left is work it cannot end: an HTTP request, OpenShell provisioning,
        # or a process that left its process group. Its next command will not
        # start, because the stop stays set.
        logger.warning(
            "Tool calls were still running when the stopped turn ended. "
            "event=tool_work_outlived_turn agent_id=%s task_id=%s reason=%s grace_seconds=%s tool_calls=%s",
            tool_stop.agent_id,
            tool_stop.task_id,
            tool_stop.reason,
            TOOL_STOP_GRACE_SECONDS,
            _describe(still_running),
        )
        return
    logger.info(
        "Stopped the running tool calls of a stopped turn. "
        "event=tool_work_stopped agent_id=%s task_id=%s reason=%s",
        tool_stop.agent_id,
        tool_stop.task_id,
        tool_stop.reason,
    )


def _report_tool_calls_after_return(tool_stop: ToolStop, logger: logging.Logger) -> None:
    running = tool_stop.running()
    if running:
        logger.warning(
            "Tool calls were still running after the invocation returned. "
            "event=tool_work_after_invocation agent_id=%s task_id=%s tool_calls=%s",
            tool_stop.agent_id,
            tool_stop.task_id,
            _describe(running),
        )


def _describe(calls: tuple[RunningToolCall, ...]) -> str:
    return ",".join(
        f"{call.tool_name}:{call.tool_use_id or '-'}:{call.running_seconds()}s" for call in calls
    )


__all__ = [
    "HANDBACK_REASON",
    "TASK_STOPPED_REASON",
    "TURN_CANCELLED_REASON",
    "attach_tool_stop",
    "invoke_with_tool_stop",
    "stop_tool_calls",
]
