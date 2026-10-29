"""Which execution lane runs a task's turns (``runtime_hosting.agent_execution``).

A request a person sent in a thread set to Web Chat's Run concurrently
(``GatewayContext.parallel_thread``) runs in a lane of its own, named after its
root task, so the team lead works on it at the same time as its other work. By
turning the switch on the person says the request does not overlap their other
work, so both may run at once (owner, 2026-10-27).
Work set up from a parallel thread runs in parallel too (owner, 2026-10-28):
a schedule or Objective work created there copies the request's context,
mark and delivery mode included (``custom_tools.providers.schedules``
``_build_schedule_payload_meta``, the turn action outbox's
``ScheduledMessage`` branch), so each of its runs, a new root task, gets a
parallel lane of its own and counts toward the limit below. Repeated runs of
one schedule still never overlap: the scheduler skips a fire while the
previous run is active (``custom_agents.scheduled_runtime``). Everything else
— other chats, New Chat, ``/new``, schedules created anywhere else, heartbeat
runs, other chat apps — shares the main lane, one turn at a time, as before.

- **Only the team lead opens parallel lanes.** People's requests reach it. A
  worker runs one task at a time (it asks for work only while it can start
  it, ADR-019), so the mark on a delegated task's copied context changes
  nothing there.
- **Only interactive requests count** (``delivery_mode`` ``interactive``).
  Heartbeat runs take the heartbeat launcher's context, which the gateway
  never marks, so they stay in the main lane.
- **One lane per request.** All turns of a request share its lane: its first
  turn and every resume after workers report, approvals and answered
  questions. Its turns therefore never overlap each other.
- **A limit.** At most ``MAX_PARALLEL_LANES`` parallel lanes run at once; a
  further parallel request waits, listed as queued, until one ends. The
  number is fixed (owner, 2026-10-27): more turns at once reach model
  provider rate limits sooner.

Overlapping changes are checked only where the stores check them:

- The change ledger (``storage.change_ledger``) refuses the later of two of
  a person's requests' changes to the same personal knowledge page, its only
  user today. ``agent.request_conflict_checks: false`` turns this off.
- Memory facts, schedules, heartbeat items and Objectives (their definition
  and success criteria) refuse only a change made from an out-of-date read
  (``changed_since_read``); a request that read the newest version can still
  change what another request changed.
- Shell, Python and file-tool writes (``file_write`` overwrites) and
  everything else are not checked; the person who launched the parallel
  thread accepted that.

The functions here read task state, so they run on the task manager's
operations lane (``TaskManager.operations_runtime``).
"""

from __future__ import annotations

from typing import Any

from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_messages.contracts.routing_v1 import DELIVERY_MODE_INTERACTIVE
from runtime_hosting.agent_execution import MAIN_LANE

MAX_PARALLEL_LANES = 4
PARALLEL_LANE_PREFIX = "parallel:"


def is_parallel_request(meta: Any) -> bool:
    """Whether a request's metadata marks it as sent in a parallel thread (module docstring)."""

    gateway_context = getattr(meta, "gateway_context", None)
    return bool(
        getattr(gateway_context, "parallel_thread", False)
        and getattr(gateway_context, "delivery_mode", None) == DELIVERY_MODE_INTERACTIVE
    )


def parallel_lane(root_task_id: str) -> str:
    return f"{PARALLEL_LANE_PREFIX}{root_task_id}"


def is_parallel_lane(lane: str | None) -> bool:
    return bool(lane) and str(lane).startswith(PARALLEL_LANE_PREFIX)


def root_of_lane(lane: str) -> str | None:
    """The root task a parallel lane serves, or None for the main lane."""

    return lane[len(PARALLEL_LANE_PREFIX):] if is_parallel_lane(lane) else None


def lane_for_task(manager: Any, task_id: str | None) -> str:
    """The lane of ``task_id``: its request's own lane when it is a parallel request, else the main lane.

    Runs on the task manager's operations lane. A task this agent does not
    track runs in the main lane.
    """

    if not task_id or not getattr(manager, "parallel_lanes_enabled", False):
        return MAIN_LANE
    root_task_id = manager.resolve_root_task_id(task_id) or task_id
    root = manager.get_task_info(root_task_id)
    if root is None or root.task.parent_task_id is not None:
        return MAIN_LANE
    return parallel_lane(root_task_id) if is_parallel_request(root.meta) else MAIN_LANE


def executing_parallel_task_ids(manager: Any) -> list[str]:
    """Locally executable tasks executing now in a parallel lane (not the main slot)."""

    return sorted(
        task_id
        for task_id, info in list(manager.task_state.tasks.items())
        if manager.is_locally_executable_task(task_id)
        and manager.is_executing_state(info.state)
        and lane_for_task(manager, task_id) != MAIN_LANE
    )


def task_of_message(content: Any) -> str | None:
    """The local task a message that needs a turn concerns (a worker's result goes to its parent)."""

    if isinstance(content, Task):
        if content.task_type == MessageMetaType.RESPONSE:
            return content.parent_task_id
        return content.task_id
    return getattr(content, "parent_task_id", None) or getattr(content, "source_task_id", None)


def lane_for_message(manager: Any, content: Any) -> str:
    """The lane that handles a message needing a turn; runs on the task manager's operations lane."""

    return lane_for_task(manager, task_of_message(content))


def lanes_for_messages(manager: Any, contents: list[tuple[str, Any]]) -> dict[str, str]:
    """The lane of each ``(message_id, content)``, read in one pass of the operations lane."""

    return {message_id: lane_for_message(manager, content) for message_id, content in contents}


__all__ = [
    "MAX_PARALLEL_LANES",
    "executing_parallel_task_ids",
    "PARALLEL_LANE_PREFIX",
    "is_parallel_lane",
    "is_parallel_request",
    "lane_for_message",
    "lane_for_task",
    "lanes_for_messages",
    "parallel_lane",
    "root_of_lane",
    "task_of_message",
]
