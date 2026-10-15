"""Stop delegated work whose parent task already ended.

Why: only the explicit stop paths (``/stop``, queue controls, objective
controls, person departure) cascaded to delegated subtasks. A parent that
failed, completed, was failed by restart recovery or by a turn error left its
children running on workers; their results were then dropped as "Parent task
not found" and the workers kept working, or waiting on a person, for nothing
(found 2026-10-04).

Two parts, both trusted code:

- ``register_parent_end_stops``: a task-terminal callback. When a task ends
  with subtasks still pending, it records the cancellation barrier for them
  (``record_execution_graph_cancelled``: admission and the next action publish
  are refused for a cancelled task, including on a worker reading the shared
  database) and sends each the existing stop control
  (``control_runtime.request_subtask_stop``). An explicit stop ends the
  children before the parent, so it finds nothing pending here.
- ``stop_report_for_ended_parent``: when a worker still reports progress or
  asks a question about a task whose parent ended (archived, or covered by a
  cancellation barrier), the lead tells that worker directly to stop. A report
  for a parent this agent never knew, such as one arriving before a restart's
  recovery finished, is only logged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

from custom_messages.contracts.control_v1 import ControlInstruction, Instruction
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, RequesterType

logger = logging.getLogger(__name__)

PARENT_ENDED_REASON = "The request this work belonged to has ended, so this work was stopped."


def register_parent_end_stops(agent: Any) -> None:
    """Register the task-terminal callback once per agent (see module docstring)."""

    callback = getattr(agent, "_parent_end_stop_callback", None)
    if callback is None:

        def callback(task_id: str, state: Any, timestamp: str) -> None:
            # Inside the task transaction: the barrier is written here, before
            # the parent is archived; the messages go out on the owner loop.
            manager = agent.task_manager
            pending = list(manager.get_pending_subtasks(task_id))
            if not pending:
                return
            child_ids = [info.task.task_id for info in pending]
            context = getattr(getattr(manager.get_task_info(task_id), "meta", None), "gateway_context", None)
            try:
                manager.record_execution_graph_cancelled(
                    root_task_id=task_id,
                    task_ids=child_ids,
                    canonical_user_id=getattr(context, "canonical_user_id", None),
                    command_id=f"parent_ended:{task_id}",
                    reason=PARENT_ENDED_REASON,
                    cancelled_at=timestamp or datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                logger.exception(
                    "Could not record that a task's delegated work must stop. "
                    "event=parent_end_barrier_failed task_id=%s subtask_ids=%s",
                    task_id,
                    ",".join(child_ids),
                )
            logger.warning(
                "A task ended with delegated work still running; stopping it. "
                "event=parent_end_subtasks_stopping task_id=%s state=%s subtask_ids=%s",
                task_id,
                getattr(state, "value", state),
                ",".join(child_ids),
            )
            _on_owner_loop(agent, partial(_send_stops, agent, task_id, pending))

        agent._parent_end_stop_callback = callback
    agent.task_manager.add_task_terminal_callback(callback)


def _on_owner_loop(agent: Any, make_coroutine: Any) -> None:
    loop = agent.task_manager.operations_runtime.loop
    if loop is None or not loop.is_running():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running loop to send stop messages on. event=parent_end_stop_unsent")
            return
    loop.call_soon_threadsafe(lambda: loop.create_task(make_coroutine()))


async def _send_stops(agent: Any, parent_task_id: str, pending: list[Any]) -> None:
    for subtask_info in pending:
        try:
            # The agent's stop method (control_runtime.request_subtask_stop);
            # importing control_runtime here would close an import cycle
            # through delegated_work_runtime.
            await agent._request_subtask_stop(
                parent_task_id=parent_task_id,
                subtask_info=subtask_info,
                reason=PARENT_ENDED_REASON,
            )
        except Exception:
            logger.exception(
                "Could not send a stop for delegated work. event=parent_end_stop_failed "
                "task_id=%s subtask_id=%s",
                parent_task_id,
                subtask_info.task.task_id,
            )


def _parent_ended(manager: Any, *, parent_task_id: str, child_task_id: str) -> bool:
    if manager.is_execution_cancelled(child_task_id):
        return True
    return manager.get_task_from_db(parent_task_id, include_all_agents=True) is not None


async def stop_report_for_ended_parent(
    agent: Any,
    *,
    parent_task_id: str,
    child_task_id: str,
    msg_header: MessageHeader,
) -> bool:
    """Tell the reporting worker to stop work whose parent ended; True when sent."""

    worker_id = getattr(msg_header, "from_agent_id", None)
    if not worker_id or worker_id == agent.id:
        return False
    if not await agent.task_manager.operations_runtime.run(
        partial(_parent_ended, agent.task_manager, parent_task_id=parent_task_id, child_task_id=child_task_id)
    ):
        return False
    stop = MessageEnvelope(
        msg_content=ControlInstruction(
            type=ControlType.CONTROL_MSG,
            instruction=Instruction(instruction="stop"),
            target_task_id=child_task_id,
            reason=PARENT_ENDED_REASON,
            cascade_to_subtasks=True,
        ),
        msg_meta=MessageMeta(
            from_agent_name=getattr(agent, "name", None),
            followup_to_task_id=child_task_id,
            requester_type=RequesterType.AGENT_REQUESTER,
            is_response_requested=False,
        ),
        msg_header=MessageHeader(
            from_agent_id=agent.id,
            to_routing_path=worker_id,
            response_routing_key=agent.id,
            agent_signature="",
        ),
    )
    await agent.team_comms.send_message(message=stop, routing_key=worker_id)
    logger.warning(
        "A worker reported on work whose request already ended; told it to stop. "
        "event=orphaned_subtask_stop_sent task_id=%s parent_task_id=%s worker_agent_id=%s",
        child_task_id,
        parent_task_id,
        worker_id,
    )
    return True
