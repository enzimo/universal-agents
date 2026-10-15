"""Resume a waiting task through the model with a directive from trusted code.

Credential forms, tool setup pages and delegated-work wake-ups
(``custom_agents.delegation_wake``) share this one path. It lives apart from
``task_message_runtime`` because the wake-up module is imported by it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from custom_agents.control_runtime import handle_interrupted_turn
from custom_agents.task_records import TaskInfo
from custom_agents.turn_failure_runtime import settle_failed_turn
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import ProgressState


async def resume_task_with_directive(self: Any, parent_task_info: TaskInfo, directive: str) -> None:
    """Resume a task that waited for an outside result, through the model.

    Records the directive the next model turn reads, moves the task out of
    WAITING, runs the turn, and completes the task when the turn answered and
    nothing else is pending. Credential forms and tool setup pages share it.
    """

    parent_task_id = parent_task_info.task.task_id
    await self.task_manager.operations_runtime.run(
        self.task_manager.append_runtime_resume_directive, parent_task_id, directive
    )
    if parent_task_info.state in {ProgressState.WAITING, ProgressState.PAUSED}:
        await self.task_manager.operations_runtime.run(
            self.task_manager.update_main_task_state,
            parent_task_id,
            ProgressState.IN_PROGRESS,
            datetime.now(timezone.utc).isoformat(),
        )
    try:
        result = await self.process_llm_interaction(
            msg_content=parent_task_info.task,
            msg_header=parent_task_info.header,
            msg_meta=parent_task_info.meta,
        )
    except asyncio.CancelledError:
        # Runtime shutdown cancels this owner; a user stop cancels its child turn.
        if (owner := asyncio.current_task()) is not None and owner.cancelling():
            raise
        await handle_interrupted_turn(self, task_id=parent_task_id, task_info=parent_task_info)
        return
    except Exception as exc:
        # The task holds the execution slot (IN_PROGRESS above); release it.
        await settle_failed_turn(self, parent_task_id, parent_task_info, exc)
        return
    if not (isinstance(result, Task) and result.answer):
        return
    run = self.task_manager.operations_runtime.run
    if (
        not await run(self.task_manager.is_final_review_pending, parent_task_id)
        and await run(self.task_manager.get_task_info, parent_task_id) is not None
        and not await run(self.task_manager.has_pending_subtasks, parent_task_id)
    ):
        await self._complete_task_and_schedule_aar_review(
            parent_task_id, datetime.now(timezone.utc).isoformat()
        )
        self._logger.info("[%s] Task %s is now marked as COMPLETED", self.id, parent_task_id)


__all__ = ["resume_task_with_directive"]
