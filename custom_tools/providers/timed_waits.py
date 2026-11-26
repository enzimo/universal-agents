"""Timed task pauses: ``wait`` (without approval) and ``long_wait`` (with approval).

Both tools pause the active task with a local Strands interrupt
(``TIMED_WAIT_KIND``), freeing the worker's execution slot until the saved UTC
resume time passes (``custom_agents.timed_wait_runtime``). Access is never
decided here: the catalog classifies ``fn:wait`` as ``read`` in
``agent_runtime`` and ``fn:long_wait`` as ``write`` outside every standard set,
and the access hook enforces both before the tool runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from strands import tool
from strands.interrupt import InterruptException
from strands.types.tools import ToolContext

from access_control.identity import ISSUER_SCHEDULER
from custom_agents.timed_waits import (
    LONG_WAIT_TOOL_NAME,
    TIMED_WAIT_KIND,
    TIMED_WAIT_RESPONSE_KIND,
    WAIT_TOOL_NAME,
    WaitEntry,
    WaitLedger,
    budget_refusal,
    next_run_refusal,
    utc_now,
    validate_wait_args,
    wait_result,
    wait_summary_text,
)
from custom_tools.provider_contract import ToolProvider, tool_name
from custom_tools.runtime_context import (
    active_schedule_run,
    active_signed_task_context,
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
    tool: str,
    tool_use_id: str,
    minutes: int,
    reason: str,
    requested_at: str,
    resume_at: str,
) -> dict[str, Any]:
    return {
        "kind": TIMED_WAIT_KIND,
        "tool": tool,
        "tool_use_id": tool_use_id,
        "minutes": minutes,
        "reason": reason,
        "requested_at": requested_at,
        "resume_at": resume_at,
        "summary": wait_summary_text(minutes=minutes, resume_at=resume_at, reason=reason),
    }


def _agent_has_tool(agent: Any, name: str) -> bool:
    """Whether ``name`` is in this agent's tools or active Strands registry (auth-off guidance)."""

    for item in getattr(agent, "tools", ()) or ():
        if tool_name(item) == name:
            return True
    from custom_agents.strands_parts import turn_strands_agent

    registry = getattr(turn_strands_agent(agent), "tool_registry", None)
    if registry is None:
        return False
    return name in getattr(registry, "registry", {}) or name in getattr(registry, "dynamic_tools", {})


def _verified_scheduler_schedule_id(agent: Any, task_id: str) -> str | None:
    """The schedule id of a scheduler-issued task context, or None (never trusts message claims)."""

    from custom_agents.access_runtime import verified_task_context

    has_identity = active_signed_task_context(agent) is not None or active_schedule_run(agent) is not None
    try:
        context = verified_task_context(agent)
    except RuntimeError as exc:
        if has_identity:
            logger.warning(
                "Could not verify the active task's schedule identity. "
                "event=timed_wait_identity_unchecked task_id=%s error=%s",
                task_id,
                exc,
            )
        return None
    if context is None or not context.schedule_id:
        return None
    if context.issuer != ISSUER_SCHEDULER:
        logger.warning(
            "Ignoring schedule_id from a non-scheduler task context during timed wait check. "
            "event=schedule_id_from_non_scheduler_ignored issuer=%s schedule_id=%s task_id=%s",
            context.issuer,
            context.schedule_id,
            task_id,
        )
        return None
    return context.schedule_id


def _schedule_store(agent: Any) -> Any | None:
    store = getattr(agent, "_schedule_store", None)
    if store is None and callable(getattr(agent, "_ensure_schedule_store", None)):
        store = agent._ensure_schedule_store()
    return store


async def _check_schedule_next_run(
    agent: Any,
    task_id: str,
    *,
    now: datetime,
    resume_at: datetime,
) -> str | None:
    """Refuse a wait in a recurring scheduled run that would reach or pass its next fire time."""

    schedule_id = _verified_scheduler_schedule_id(agent, task_id)
    if not schedule_id:
        return None
    store = _schedule_store(agent)
    if store is None:
        logger.warning(
            "Timed wait refused because this agent has no schedule store to read the next run time. "
            "event=timed_wait_refused reason=schedule_unreadable task_id=%s schedule_id=%s",
            task_id,
            schedule_id,
        )
        return (
            f"Could not check schedule {schedule_id}'s next run time (no schedule store), "
            "so this wait was refused to avoid overlapping the next run."
        )
    try:
        schedule, finishing = await asyncio.to_thread(store.read_next_fire_schedule, schedule_id)
    except Exception as exc:  # noqa: BLE001 - fail safe on any storage or parse error
        logger.warning(
            "Timed wait refused because the schedule could not be read. "
            "event=timed_wait_refused reason=schedule_unreadable task_id=%s schedule_id=%s error_type=%s",
            task_id,
            schedule_id,
            type(exc).__name__,
        )
        return (
            f"Could not check schedule {schedule_id}'s next run time ({type(exc).__name__}), "
            "so this wait was refused to avoid overlapping the next run."
        )
    if schedule is None:
        logger.info(
            "Scheduled run's schedule row was not found; allowing timed wait. "
            "event=timed_wait_schedule_missing task_id=%s schedule_id=%s",
            task_id,
            schedule_id,
        )
        return None
    refusal = next_run_refusal(schedule, resume_at, now=now, finishing=finishing)
    if refusal is not None:
        logger.info(
            "Timed wait refused because it would overlap the schedule's next run. "
            "event=timed_wait_refused reason=next_run_overlap task_id=%s schedule_id=%s "
            "resume_at=%s next_fire_at=%s",
            task_id,
            schedule_id,
            resume_at.isoformat(),
            schedule.next_fire_at,
        )
    return refusal


def _handle_resume_response(entry: WaitEntry, response: Any, *, task_id: str) -> str:
    """Turn the interrupt answer into the tool's JSON result (timer wake-up or early non-timer answer)."""

    now = utc_now()
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
        "event=timed_wait_answered_by_other task_id=%s tool_use_id=%s answer_type=%s",
        task_id,
        entry.tool_use_id,
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

    # A resumed call finds its started wait by tool_use_id and takes the stored answer.
    existing = ledger.entry_for(tool_use_id)
    if existing is not None:
        response = tool_context.interrupt(
            TIMED_WAIT_KIND,
            reason=_interrupt_reason(
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
    has_long_wait = False if auth_on else _agent_has_tool(agent, LONG_WAIT_TOOL_NAME)
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

    now = utc_now()
    resume_at = now + timedelta(minutes=valid_minutes)
    overlap = await _check_schedule_next_run(agent, task_id, now=now, resume_at=resume_at)
    if overlap is not None:
        return _refused(overlap)

    requested_at_iso = now.isoformat()
    resume_at_iso = resume_at.isoformat()
    payload = _interrupt_reason(
        tool=tool_id_name,
        tool_use_id=tool_use_id,
        minutes=valid_minutes,
        reason=cleaned_reason,
        requested_at=requested_at_iso,
        resume_at=resume_at_iso,
    )
    response = tool_context.interrupt(TIMED_WAIT_KIND, reason=payload)
    # Only reached if the interrupt already carried a response before being charged.
    entry = WaitEntry(
        tool_use_id=tool_use_id,
        tool=tool_id_name,
        minutes=valid_minutes,
        requested_at=requested_at_iso,
        resume_at=resume_at_iso,
        reason=cleaned_reason,
    )
    return _handle_resume_response(entry, response, task_id=task_id)


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
            For longer waits (up to 24 hours total), ask a person for
            ``fn:long_wait`` with ``request_access`` and use ``long_wait``.
        reason: Plain-language reason explaining what you are waiting for
            (shown in task history and status summaries; 1 to 500 characters).
    """

    try:
        return await _timed_wait(WAIT_TOOL_NAME, minutes, reason, tool_context)
    except InterruptException:
        raise


@tool(context=True)
async def long_wait(minutes: int, reason: str, tool_context: ToolContext) -> str:
    """Pause this task for a longer duration (with a person's approval) and continue in the same task.

    Works like ``wait`` but allows a task's total waited time (counting both
    ``wait`` and ``long_wait`` calls) to reach the team's with-approval limit
    (default 1440 minutes / 24 hours). Requires a person's approval through
    ``request_access(tool_id='fn:long_wait', level='write', ...)`` for one run,
    or lasting schedule access for a recurring scheduled job.

    In a recurring scheduled run, waits that would reach or pass the schedule's
    next run time are refused so the next run is not skipped.

    Args:
        minutes: Whole number of minutes to pause (>= 1).
        reason: Plain-language reason explaining what you are waiting for
            (shown in task history and status summaries; 1 to 500 characters).
    """

    try:
        return await _timed_wait(LONG_WAIT_TOOL_NAME, minutes, reason, tool_context)
    except InterruptException:
        raise


WAIT_PROVIDER = ToolProvider("wait", (wait,))
LONG_WAIT_PROVIDER = ToolProvider("long_wait", (long_wait,))

__all__ = ["LONG_WAIT_PROVIDER", "WAIT_PROVIDER", "long_wait", "wait"]
