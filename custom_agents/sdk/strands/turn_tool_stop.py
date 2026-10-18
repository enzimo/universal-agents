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
``TOOL_STOP_GRACE_SECONDS`` for those calls to end. On a stop it then ends the
processes the turn's commands left running in their process groups
(``server &``): SIGTERM, then SIGKILL after ``BACKGROUND_TERM_GRACE_SECONDS``.
Only then does it pass the outcome on. So everything that waits for the turn
also waits for its commands, without each stop path waiting on its own. A turn
that ends normally leaves background processes alone, as before.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any

from custom_tools.tool_stop import (
    BACKGROUND_KILL_GRACE_SECONDS,
    BACKGROUND_TERM_GRACE_SECONDS,
    TOOL_STOP_GRACE_SECONDS,
    TOOL_STOP_STATE_KEY,
    EndedGroup,
    RunningToolCall,
    ToolStop,
)

_POLL_SECONDS = 0.05

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
    """Finish one bounded cleanup even if its caller is cancelled again.

    A control stop can overlap shutdown or a lease-loss handback. Shielding
    alone would let the caller finish before cleanup; retain and join the
    same cleanup through every cancellation, then propagate cancellation.
    """

    tool_stop.request(reason)
    cleanup = tool_stop.start_cleanup(lambda: _finish_tool_stop(tool_stop, logger))
    cancellation = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = exc
    cleanup.result()
    if cancellation is not None:
        raise cancellation


async def _finish_tool_stop(tool_stop: ToolStop, logger: logging.Logger) -> None:
    """The turn's single cleanup, joined by every stop path."""

    if tool_stop.running():
        await _wait_for_tool_calls(tool_stop, logger)
    await _end_leftover_processes(tool_stop, logger)


async def _wait_for_tool_calls(tool_stop: ToolStop, logger: logging.Logger) -> None:
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


async def _end_leftover_processes(tool_stop: ToolStop, logger: logging.Logger) -> None:
    """End processes the turn's commands left running in their process groups.

    Each pass reads /proc once, in a worker thread, so the agent's event loop
    keeps serving other work however many commands the turn ran.
    """

    terminated = await asyncio.to_thread(tool_stop.signal_process_groups, signal.SIGTERM)
    if not terminated:
        return
    await _wait_for_process_groups(tool_stop, BACKGROUND_TERM_GRACE_SECONDS)
    killed = await asyncio.to_thread(tool_stop.signal_process_groups, signal.SIGKILL)
    remaining = await _wait_for_process_groups(tool_stop, BACKGROUND_KILL_GRACE_SECONDS) if killed else ()
    if remaining:
        logger.warning(
            "Processes remained after SIGKILL; they may be blocked in the kernel. "
            "event=tool_process_groups_outlived_kill agent_id=%s task_id=%s reason=%s groups=%s",
            tool_stop.agent_id,
            tool_stop.task_id,
            tool_stop.reason,
            _describe_groups(remaining),
        )
        return
    logger.info(
        "Stopped a turn's leftover process groups. "
        "event=tool_background_processes_ended agent_id=%s task_id=%s reason=%s groups=%s killed_after_grace=%s",
        tool_stop.agent_id,
        tool_stop.task_id,
        tool_stop.reason,
        _describe_groups(terminated),
        _describe_groups(killed) or "-",
    )


async def _wait_for_process_groups(tool_stop: ToolStop, grace_seconds: float) -> tuple[EndedGroup, ...]:
    """Observe group exit without blocking the loop or waiting without a bound."""

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        still_live = await asyncio.to_thread(tool_stop.live_process_groups)
        if not still_live:
            return ()
        await asyncio.sleep(_POLL_SECONDS)
    return await asyncio.to_thread(tool_stop.live_process_groups)


def _describe_groups(groups: tuple[EndedGroup, ...]) -> str:
    return ",".join(
        f"{group.tool_name}:pgid={group.pgid}:pids={'/'.join(str(pid) for pid in group.pids)}" for group in groups
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
