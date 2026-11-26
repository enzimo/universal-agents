"""Timed task pauses: ``wait`` (without approval) and ``long_wait`` (with approval).

Both tools pause the active task with a local Strands interrupt
(``TIMED_WAIT_KIND``), freeing the worker's execution slot until the saved UTC
resume time passes (``custom_agents.timed_wait_runtime``). Access is never
decided here: the catalog classifies ``fn:wait`` as ``read`` in
``agent_runtime`` and ``fn:long_wait`` as ``write`` outside every standard set,
and the access hook enforces both before the tool runs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from custom_agents import timed_waits as timed_waits_rules
from custom_agents.timed_wait_runtime import check_schedule_next_run
from custom_agents.timed_waits import (
    LONG_WAIT_TOOL_NAME,
    TIMED_WAIT_KIND,
    TIMED_WAIT_REFUSED_RESPONSE_KIND,
    TIMED_WAIT_RESPONSE_KIND,
    WAIT_TOOL_NAME,
    WaitEntry,
    WaitLedger,
    budget_refusal,
    validate_wait_args,
    wait_result,
    wait_summary_text,
)
from custom_tools.provider_contract import ToolProvider, tool_name
from custom_tools.runtime_context import (
    bound_task_id,
    get_agent_ref,
)
from runtime_settings.timed_waits import TimedWaitLimits

logger = logging.getLogger(__name__)

STATUS_REFUSED = "refused"
STATUS_WAITED = "waited"
STATUS_ENDED_EARLY = "ended_early"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _refused(reason: str) -> str:
    return _json({"status": STATUS_REFUSED, "reason": reason})


def _interrupt_reason(
    *,
    call_id: str,
    tool: str,
    tool_use_id: str,
    minutes: int,
    reason: str,
    requested_at: str,
    resume_at: str,
) -> dict[str, Any]:
    return {
        "kind": TIMED_WAIT_KIND,
        "call_id": call_id,
        "tool": tool,
        "tool_use_id": tool_use_id,
        "minutes": minutes,
        "reason": reason,
        "requested_at": requested_at,
        "resume_at": resume_at,
        "summary": wait_summary_text(minutes=minutes, resume_at=resume_at, reason=reason),
    }


def _agent_has_tool(agent: Any, name: str) -> bool:
    """Whether ``name`` is currently offered to this agent's model.

    When a Strands agent with a ``tool_registry`` is attached, reads that
    registry directly (so authorization-on reflects actual granted tools from
    ``GrantToolSync``, and authorization-off reflects ``startup_tools``). Falls
    back to ``agent.tools`` only when no Strands registry is present.
    """

    from custom_agents.strands_parts import turn_strands_agent

    registry = getattr(turn_strands_agent(agent), "tool_registry", None)
    if registry is not None:
        return name in getattr(registry, "registry", {}) or name in getattr(registry, "dynamic_tools", {})
    return any(tool_name(item) == name for item in (getattr(agent, "tools", ()) or ()))


def _call_id(agent: Any, tool_context: ToolContext, tool_use_id: str, tool_id_name: str) -> str:
    """Stable call id for this tool invocation (step tracking_id + toolUseId), falling back to tool_use_id."""

    from custom_agents.access_runtime import step_tool_call_id

    resolved = step_tool_call_id(
        agent,
        getattr(tool_context, "agent", None),
        tool_use_id,
        tool_name=tool_id_name,
    )
    return resolved or tool_use_id


def _handle_resume_response(entry: WaitEntry, response: Any, *, task_id: str) -> str:
    """Turn the interrupt answer into the tool's JSON result (timer wake-up, refusal, or early non-timer answer)."""

    if isinstance(response, Mapping) and response.get("kind") == TIMED_WAIT_REFUSED_RESPONSE_KIND:
        return _refused(str(response.get("reason") or "The timed wait could not be started."))
    now = timed_waits_rules.utc_now()
    if isinstance(response, Mapping) and response.get("kind") == TIMED_WAIT_RESPONSE_KIND:
        return _json(
            wait_result(
                minutes=entry.minutes,
                requested_at=entry.requested_at,
                now=now,
                status=STATUS_WAITED,
            )
        )
    logger.warning(
        "Timed wait ended with a non-timer answer. "
        "event=timed_wait_answered_by_other task_id=%s tool_use_id=%s call_id=%s answer_type=%s",
        task_id,
        entry.tool_use_id,
        entry.call_id,
        type(response).__name__,
    )
    return _json(
        wait_result(
            minutes=entry.minutes,
            requested_at=entry.requested_at,
            now=now,
            status=STATUS_ENDED_EARLY,
            next_step="The wait ended early because another response arrived. Check the task state and continue.",
        )
    )


async def _timed_wait(
    tool_id_name: str,
    minutes: int,
    reason: str,
    tool_context: ToolContext,
) -> str:
    from custom_agents.access_runtime import authorization_enabled

    agent = get_agent_ref()
    if agent is None:
        return _refused("No agent is running this tool.")
    task_id = bound_task_id()
    if not task_id:
        return _refused("Timed waits can only pause a running task.")
    manager = getattr(agent, "task_manager", None)
    if manager is None:
        return _refused("This agent has no task manager to save a wait.")
    tool_use_id = str((getattr(tool_context, "tool_use", None) or {}).get("toolUseId") or "").strip()
    if not tool_use_id:
        return _refused("This tool call has no id, so its wait cannot be tracked.")

    task_info = await manager.operations_runtime.run(manager.get_task_info, task_id)
    if task_info is None:
        return _refused(f"Task {task_id} is no longer tracked.")
    ledger = WaitLedger.from_history(task_info)
    call_id = _call_id(agent, tool_context, tool_use_id, tool_id_name)

    # A resumed call finds its started wait by call_id (model step + tool_use_id) and takes the stored answer.
    existing = ledger.entry_for(call_id)
    if existing is not None:
        response = tool_context.interrupt(
            TIMED_WAIT_KIND,
            reason=_interrupt_reason(
                call_id=existing.call_id,
                tool=existing.tool,
                tool_use_id=existing.tool_use_id,
                minutes=existing.minutes,
                reason=existing.reason,
                requested_at=existing.requested_at,
                resume_at=existing.resume_at,
            ),
        )
        return _handle_resume_response(existing, response, task_id=task_id)

    try:
        valid_minutes, cleaned_reason = validate_wait_args(minutes, reason)
    except ValueError as exc:
        logger.info(
            "Timed wait refused for invalid arguments. "
            "event=timed_wait_refused reason=invalid_arguments task_id=%s tool=%s error=%s",
            task_id,
            tool_id_name,
            exc,
        )
        return _refused(str(exc))

    limits = getattr(agent, "timed_wait_limits", None) or TimedWaitLimits()
    auth_on = authorization_enabled(getattr(agent, "authorization_config", None))
    has_long_wait = _agent_has_tool(agent, LONG_WAIT_TOOL_NAME)
    over_budget = budget_refusal(
        ledger,
        valid_minutes,
        tool_id_name,
        limits,
        authorization_on=auth_on,
        has_long_wait_tool=has_long_wait,
    )
    if over_budget is not None:
        logger.info(
            "Timed wait refused by task budget. "
            "event=timed_wait_refused reason=budget_exceeded task_id=%s tool=%s minutes=%s used_minutes=%s",
            task_id,
            tool_id_name,
            valid_minutes,
            ledger.total_minutes,
        )
        return _refused(over_budget)

    now = timed_waits_rules.utc_now()
    resume_at = now + timedelta(minutes=valid_minutes)
    overlap = await check_schedule_next_run(agent, task_id, now=now, resume_at=resume_at)
    if overlap is not None:
        return _refused(overlap)

    requested_at_iso = now.isoformat()
    resume_at_iso = resume_at.isoformat()
    payload = _interrupt_reason(
        call_id=call_id,
        tool=tool_id_name,
        tool_use_id=tool_use_id,
        minutes=valid_minutes,
        reason=cleaned_reason,
        requested_at=requested_at_iso,
        resume_at=resume_at_iso,
    )
    response = tool_context.interrupt(TIMED_WAIT_KIND, reason=payload)
    # Only reached if the interrupt already carried a response before being charged (e.g. refused at start).
    entry = WaitEntry(
        call_id=call_id,
        tool_use_id=tool_use_id,
        tool=tool_id_name,
        minutes=valid_minutes,
        requested_at=requested_at_iso,
        resume_at=resume_at_iso,
        reason=cleaned_reason,
    )
    return _handle_resume_response(entry, response, task_id=task_id)


async def _run_timed_wait(
    tool_id_name: str,
    minutes: int,
    reason: str,
    tool_context: ToolContext,
) -> str:
    try:
        return await _timed_wait(tool_id_name, minutes, reason, tool_context)
    except InterruptException:
        raise
    except Exception as exc:  # noqa: BLE001 - structured log and plain refusal on any unexpected tool failure
        tool_use_id = str((getattr(tool_context, "tool_use", None) or {}).get("toolUseId") or "").strip()
        logger.warning(
            "Timed wait failed unexpectedly; nothing was charged. "
            "event=timed_wait_failed task_id=%s tool=%s tool_use_id=%s error_type=%s",
            bound_task_id() or "-",
            tool_id_name,
            tool_use_id or "-",
            type(exc).__name__,
            exc_info=exc,
        )
        return _refused(f"Could not start {tool_id_name} ({type(exc).__name__}); nothing was charged.")


@tool(context=True)
async def wait(minutes: int, reason: str, tool_context: ToolContext) -> str:
    """Pause this task for a set number of minutes and continue in the same task when the timer ends.

    Use this whenever you need to wait for an external process (such as a
    redeploy, build, or cooldown) instead of sleeping in shell or Python or
    pushing the remaining work to a later scheduled run. Your worker slot is
    freed during the pause, and the task resumes automatically with the elapsed
    time and current UTC timestamp.

    Do not use ``wait`` to wait for delegated subtasks, a person's reply, or an
    access decision: those already pause and resume the task by themselves.

    Args:
        minutes: Whole number of minutes to pause (>= 1). All waits in a task
            count toward the team's without-approval total (default 20 minutes).
            Wait budgets are tracked per task: a delegated subtask has its own
            budget and its waits do not count against its parent task's budget.
            If a task needs to wait longer than ``wait`` allows (up to 24 hours
            total), ``wait`` refuses and its answer names the next step for
            ``long_wait``.
        reason: Plain-language reason explaining what you are waiting for
            (shown in task history and status summaries; 1 to 500 characters).
    """

    return await _run_timed_wait(WAIT_TOOL_NAME, minutes, reason, tool_context)


@tool(context=True)
async def long_wait(minutes: int, reason: str, tool_context: ToolContext) -> str:
    """Pause this task for a longer duration and continue in the same task.

    Works like ``wait`` but allows a task's total waited time (counting both
    ``wait`` and ``long_wait`` calls) to reach the team's with-approval limit
    (default 1440 minutes / 24 hours). Wait budgets are tracked per task: a
    delegated subtask has its own budget and its waits do not count against its
    parent task's budget. Not in any standard tool set; when access control is
    enabled it requires approval for one run or lasting schedule access for a
    recurring scheduled job.

    In a recurring scheduled run, waits that would reach or pass the schedule's
    next run time are refused so the next run is not skipped.

    In an interactive chat request, pausing longer than the gateway's reply
    timeout (default 60 minutes) causes the chat UI to show a timeout notice
    while the task keeps waiting; the final reply still arrives once the wait
    ends.

    Do not use ``long_wait`` to wait for delegated subtasks, a person's reply,
    or an access decision: those already pause and resume the task by
    themselves.

    Args:
        minutes: Whole number of minutes to pause (>= 1).
        reason: Plain-language reason explaining what you are waiting for
            (shown in task history and status summaries; 1 to 500 characters).
    """

    return await _run_timed_wait(LONG_WAIT_TOOL_NAME, minutes, reason, tool_context)


WAIT_PROVIDER = ToolProvider("wait", (wait,))
LONG_WAIT_PROVIDER = ToolProvider("long_wait", (long_wait,))

__all__ = ["LONG_WAIT_PROVIDER", "WAIT_PROVIDER", "long_wait", "wait"]
