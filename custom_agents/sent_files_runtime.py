"""Keeping the files a person sent with a message to a running task.

A guidance or an interrupt answer (``TaskSteering``, handled by
``custom_agents.task_steering_runtime``) and an answer to a question
(``TaskQuery``, handled by ``custom_agents.task_message_runtime``) may bring
files. Each handler calls ``keep_sent_files`` once it has decided to take the
message and before it records or acknowledges anything else, with the
decision ``custom_agents.conversation_attachments`` made from the message as
it arrived. The files kept are added to the task's conversation turn and
written to its history as one files entry, so they stay the task's own after
the message is read and after a restart. The handler then carries on with a
copy of the message holding only those files, so a refused file is never
saved, queued, shown to the model or sent on.
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
    upload folders are dropped too (each logged). The rest are merged into
    the task's conversation turn and written to its history as one files
    entry (``message_id`` and ``message_type`` name the message; ``subject``
    names it in the entry's text, such as "a guidance"). A store error
    propagates: the handler has acknowledged nothing yet, so the sender
    sends the message again, and recording again adds no file twice.
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
    await asyncio.to_thread(
        record_turn_attachments,
        getattr(agent, "_conversation_store", None),
        task_info,
        kept,
        logger=agent._logger,
    )
    entry = await agent.task_manager.operations_runtime.run(
        agent.task_manager.append_history_entry,
        task_id,
        f"Files sent with {subject} accepted: " + ", ".join(attachment.filename for attachment in kept),
        entry_type=TaskHistoryType.TASK_EVENT,
        importance=TaskHistoryImportance.NORMAL,
        details=task_files_details(kept, message_id=message_id, message_type=message_type),
    )
    return kept if entry is not None else None


__all__ = ["keep_sent_files"]
