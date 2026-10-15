"""External result transformers used only by live test scenarios."""

import os
import time

from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import TaskHistoryImportance, TaskHistoryType


_CRASHED_AGENT_IDS: set[str] = set()


def crash_first_task_before_reply(agent, incoming_content, result):
    """Terminate the current process after the first task result is produced but before reply dispatch."""
    if not isinstance(incoming_content, Task):
        return result
    if agent.id in _CRASHED_AGENT_IDS:
        return result

    _CRASHED_AGENT_IDS.add(agent.id)
    agent.task_manager.append_history_entry(
        incoming_content.task_id,
        "Live test forced the worker process to exit before sending its reply.",
        entry_type=TaskHistoryType.ERROR_EVENT,
        importance=TaskHistoryImportance.CRITICAL,
    )
    agent._logger.error(f"[{agent.id}] Live test crashing process before reply for task {incoming_content.task_id}.")
    time.sleep(1.5)
    os._exit(91)
