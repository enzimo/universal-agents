"""Keeping the files a person sent with a message to a running task.

A guidance or an interrupt answer (``TaskSteering``, handled by
``custom_agents.task_steering_runtime``) and an answer to a question
(``TaskQuery``, handled by ``custom_agents.task_message_runtime``) may bring
files. Each handler calls ``keep_sent_files`` once it has decided to take the
message and before it records or acknowledges anything else, with the
decision ``custom_agents.conversation_attachments`` made from the message as
it arrived. The files kept are written to the task's history as one files
entry and added to its conversation turn, so they stay the task's own after
the message is read and after a restart. The task's entry is written first
and decides: when the task has ended, nothing else is recorded and the
handler drops the message, so a message keeps its files exactly when it is
taken. The handler then carries on with a copy of the message holding only
those files, so a refused file is never saved, queued, shown to the model or
sent on.

A plan step or an isolated recovery task (``custom_agents.task_manager_plans``)
is a task of its own with a conversation id of its own and no conversation
turn, and the questions it asks name it. Files sent to such a task are the
person's files for the request it runs for, so they are also kept on that
request: its execution root, found through the task manager's plan-step and
recovery links (``TaskManager.resolve_root_task_id``). Once the step's entry
is written, they are merged into the root's conversation turn and written to
the root's history as well, so the step's own turn, the request's later
turns and follow-up requests can all name and open them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Optional

from access_control.ownership import OwnershipDecision
from custom_agents.conversation_attachments import (
    UploadFolders,
    keep_upload_files,
    log_refused_files,
    task_files_details,
)
from custom_agents.conversation_runtime import record_turn_attachments
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.enums import TaskHistoryImportance, TaskHistoryType


async def keep_sent_files(
    agent: Any,
    task_info: Any,
    attachments: Sequence[UserTaskAttachment],
    decision: OwnershipDecision,
    *,
    message_id: str,
    message_type: str,
    subject: str,
) -> Optional[list[UserTaskAttachment]]:
    """The files of one message that stay the task's, after recording them; None when the task is gone.

    ``decision`` says whether the gateway vouches for ``attachments``; when it
    does not, every file is dropped and logged. Records outside this host's
    upload folders are dropped too (each logged). The rest are written to
    the task's history as one files entry (``message_id`` and
    ``message_type`` name the message; ``subject`` names it in the entry's
    text, such as "a guidance"); when that entry is not written because the
    task has ended, nothing else is recorded and None is returned. Then they
    are merged into the task's conversation turn. For a plan step or
    recovery task, the turn is its request's, and the request's history gets
    a files entry too (module docstring). A store error propagates: the
    handler has acknowledged nothing yet, so the sender sends the message
    again; recording again adds no file twice and may repeat an entry, which
    the task's files read once per id.
    """

    if not attachments:
        return []
    task_id = task_info.task.task_id
    conversation_id = getattr(task_info.meta, "conversation_id", None)
    if not decision.allowed:
        log_refused_files(attachments, decision.reason, task_id=task_id, conversation_id=conversation_id)
        return []
    folders = await asyncio.to_thread(UploadFolders.for_agent, agent)
    kept = keep_upload_files(attachments, folders, task_id=task_id, conversation_id=conversation_id)
    if not kept:
        return kept
    names = ", ".join(attachment.filename for attachment in kept)
    details = task_files_details(kept, message_id=message_id, message_type=message_type)
    # The task's own entry comes first and decides: a task that has ended
    # takes neither the message nor its files, so nothing else is recorded.
    if await _files_entry(agent, task_id, f"Files sent with {subject} accepted: {names}", details) is None:
        return None
    root_info = await _request_root(agent, task_info)
    # The turn is the request's: the root's when the task is a step of it.
    await asyncio.to_thread(
        record_turn_attachments,
        getattr(agent, "_conversation_store", None),
        root_info or task_info,
        kept,
        logger=agent._logger,
    )
    if root_info is None:
        return kept
    root_id = root_info.task.task_id
    if await _files_entry(agent, root_id, f"Files sent with {subject} in step {task_id} accepted: {names}", details) is None:
        agent._logger.warning(
            "A request ended while files sent to one of its steps were recorded. They are on its conversation "
            "turn and among the step's files, but not among the request's own files. "
            "event=sent_files_root_gone task_id=%s root_task_id=%s attachment_ids=%s",
            task_id,
            root_id,
            _ids(kept),
        )
    return kept


async def _files_entry(agent: Any, task_id: str, summary: str, details: dict[str, Any]) -> Optional[Any]:
    """Write one files entry to ``task_id``'s history; None when this agent no longer holds the task."""

    task_manager = agent.task_manager
    return await task_manager.operations_runtime.run(
        task_manager.append_history_entry,
        task_id,
        summary,
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        details=details,
    )


async def _request_root(agent: Any, task_info: Any) -> Optional[Any]:
    """The request a plan step or recovery task runs for, when files sent to it are also the request's; else None.

    None for a task that is its own request (no plan-step or recovery link).
    The root is found only through the task manager's links, never from the
    message. It must be a top-level task this agent holds, working for the
    same person as the step (``owner_person_id``, recorded at admission from
    verified identity); otherwise the files stay with the step and a warning
    names both tasks.
    """

    task_manager = agent.task_manager
    task_id = task_info.task.task_id
    root_id = await task_manager.operations_runtime.run(task_manager.resolve_root_task_id, task_id)
    if not root_id or root_id == task_id:
        return None
    root_info = await task_manager.operations_runtime.run(task_manager.get_task_info, root_id)
    if (
        root_info is not None
        and root_info.task.parent_task_id is None
        and root_info.owner_person_id == task_info.owner_person_id
    ):
        return root_info
    agent._logger.warning(
        "Files sent to a step cannot be kept on its request, so they stay with the step. "
        "event=sent_files_root_not_usable task_id=%s root_task_id=%s reason=%s",
        task_id,
        root_id,
        "not_held" if root_info is None else ("delegated" if root_info.task.parent_task_id else "other_person"),
    )
    return None


def _ids(attachments: Sequence[UserTaskAttachment]) -> str:
    return ",".join(attachment.attachment_id for attachment in attachments)


__all__ = ["keep_sent_files"]
