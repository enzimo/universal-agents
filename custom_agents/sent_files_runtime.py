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

A worker that needs the person escalates to the team lead
(``manager_escalation``); the lead holds the escalation as a delegated task
of its own, and asks the person in that task. The person's answer names the
escalation. Which delegated tasks keep its files is decided by what the task
is (``_delegated_task_keeps_files``):

- An escalation about work the lead delegated: its execution root (through
  the delegation links) is the lead's request, and the files are kept like a
  step's, on the escalation and on the request.
- An escalation from work the lead never held: a worker's scheduled job run
  or maintenance run is a top-level task in the worker's own process, so the
  escalation runs for no task here. The files are kept on the escalation
  only; the run's conversation turn is in the worker, which keeps them there
  when the result arrives.
- Any other delegated task keeps none: a worker's own tasks (only the team
  lead holds escalations, and answers go only to it), and an escalation
  linked to a task here that cannot take them, such as another person's
  request.

The gateway's rule still decides first in every case: the answer's own
signed context names the escalation, its conversation and person, and is
bound to the answer and its files.

The lead's result for the escalation then carries the files of those
answers, and only those (``result_with_answer_files``); the model never
chooses them. The worker keeps the ones inside this host's upload folders as
its escalating task's own, and on that task's conversation turn when it is
its own request (a job run) (``keep_escalation_result_files``): it trusts
the result as it trusts its task's ``Task.attachments``, because it comes
from the agent that held the escalation, and it never reads the lead's
conversations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import partial
from typing import Any, Optional

from access_control.ownership import OwnershipDecision
from custom_agents.conversation_attachments import (
    DELEGATED_TASK_REASON,
    ESCALATION_RESULT_MESSAGE_TYPE,
    RequestLink,
    UploadFolders,
    answer_files,
    keep_upload_files,
    linked_request,
    log_refused_files,
    task_files_details,
)
from custom_agents.conversation_runtime import record_turn_attachments
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, TaskHistoryImportance, TaskHistoryType


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
    are merged into the task's conversation turn. For a plan step, a
    recovery task or a worker's escalation, the turn is its request's, and
    the request's history gets a files entry too; a delegated task with no
    such request keeps them only when it is the team lead's escalation from
    work it never held, and then on itself only (module docstring); any
    other delegated task keeps none (logged with ``delegated_task``). A
    store error propagates: the
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
    # Only read here; the request is written to after the task's own entry.
    request = await _request_root(agent, task_info)
    if task_info.task.parent_task_id is not None and not _delegated_task_keeps_files(agent, task_info, request):
        log_refused_files(attachments, DELEGATED_TASK_REASON, task_id=task_id, conversation_id=conversation_id)
        return []
    root_info = request.info
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
    if await _files_entry(agent, root_id, f"Files sent with {subject} in task {task_id} accepted: {names}", details) is None:
        agent._logger.warning(
            "A request ended while files sent to one of its steps or delegated tasks were recorded. They are on "
            "its conversation turn and among that task's files, but not among the request's own files. "
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


async def _request_root(agent: Any, task_info: Any) -> RequestLink:
    """The request a step or delegated task runs for, and whether files sent to it are also the request's.

    Found by ``conversation_attachments.linked_request`` on the task
    manager's operation lane, only through its plan-step, recovery and
    delegation links, never from the message. When the task runs for a task
    here that may not share its files, a warning names both tasks and the
    reason, and the files stay with a step or are refused for a delegated
    task (``keep_sent_files``).
    """

    task_manager = agent.task_manager
    # Bound into the operation: the lane copies an operation's arguments.
    link: RequestLink = await task_manager.operations_runtime.run(partial(linked_request, task_manager), task_info)
    if link.linked and link.info is None:
        agent._logger.warning(
            "Files sent to a step or delegated task cannot be kept on its request. "
            "event=sent_files_root_not_usable task_id=%s root_task_id=%s reason=%s",
            task_info.task.task_id,
            link.root_task_id,
            link.reason,
        )
    return link


def _delegated_task_keeps_files(agent: Any, task_info: Any, request: RequestLink) -> bool:
    """Whether a delegated task keeps the files of an answer the gateway vouched for (module docstring).

    Yes when it runs for a request this agent holds for the same person
    (``request``). Otherwise only the team lead's escalation from work it
    never held (no link to any task here) keeps them, on itself. A task
    linked to a task here that cannot take them keeps none: the link says
    whose work it is, and that work is not the answer's person's.
    """

    if request.info is not None:
        return True
    return not request.linked and _team_lead_escalation(agent, task_info)


def _team_lead_escalation(agent: Any, task_info: Any) -> bool:
    """The team lead's record of a worker's escalation (``manager_escalation`` in its admitted meta).

    The same test as ``task_context_runtime.sign_system_escalation``. The
    meta is the one the worker's runtime set when it escalated
    (``turn_action_outbox_runtime``); only the team lead accepts
    escalations, and answers reach only the team lead.
    """

    return bool(getattr(agent, "is_team_lead", False)) and getattr(task_info.meta, "manager_escalation", None) is not None


async def result_with_answer_files(agent: Any, content: Any, incoming: Any) -> Any:
    """``content`` with the files people's answers brought to the delegated task it is the result of.

    Only for a ``Task`` response to a delegated task (``incoming`` has a
    parent): the team lead's result for a worker's escalation carries the
    files of the person's answers to its questions (``answer_files``), read
    from the task's history when the result is sent, never chosen by the
    model, and nothing else. Any other content is returned unchanged; a
    request's own result goes to the gateway without files.
    """

    if not (
        isinstance(content, Task)
        and content.task_type == MessageMetaType.RESPONSE
        and isinstance(incoming, Task)
        and incoming.parent_task_id is not None
    ):
        return content
    task_manager = agent.task_manager
    task_info = await task_manager.operations_runtime.run(task_manager.get_task_info, incoming.task_id)
    files = list(answer_files(task_info.task)) if task_info is not None else []
    if not files:
        return content
    agent._logger.info(
        "A delegated task's result carries the files a person sent with answers to its questions. "
        "event=delegated_result_answer_files task_id=%s parent_task_id=%s attachment_ids=%s",
        incoming.task_id,
        incoming.parent_task_id,
        _ids(files),
    )
    return content.model_copy(update={"attachments": files})


async def keep_escalation_result_files(agent: Any, subtask_info: Any, result: Task) -> None:
    """Keep the files the result of this agent's escalation brought as the escalating task's own.

    ``subtask_info`` is the escalation as this agent recorded it when it
    escalated (its parent is the task that waits) and ``result`` the result
    its holder sent (``task_message_runtime._handle_task_response`` checked
    the sender). Only an escalation's result brings files
    (``result_with_answer_files``); records outside this host's upload
    folders are dropped. The rest are written to the parent's history as one
    files entry, so the parent's turn lists them and the attachment tools
    open them by id. When the parent is its own request (a scheduled job or
    maintenance run that escalated), they are then merged into its
    conversation turn as well, as the team lead does for a request, so later
    turns of that conversation list and open them; for a delegated parent
    this adds nothing. The entry comes first and decides, as in
    ``keep_sent_files``. Each refused file is logged; a store error
    propagates before the result is recorded, so its redelivery records the
    files again, each once.
    """

    if not result.attachments:
        return
    parent_id = subtask_info.task.parent_task_id
    conversation_id = getattr(subtask_info.meta, "conversation_id", None)
    if getattr(subtask_info.meta, "manager_escalation", None) is None:
        for attachment in result.attachments:
            agent._logger.warning(
                "A delegated task's result brought a file, but only an escalation's result may; it was not kept. "
                "event=result_file_refused reason=not_an_escalation attachment_id=%s task_id=%s subtask_id=%s",
                attachment.attachment_id,
                parent_id,
                subtask_info.task.task_id,
            )
        return
    folders = await asyncio.to_thread(UploadFolders.for_agent, agent)
    kept = keep_upload_files(result.attachments, folders, task_id=parent_id, conversation_id=conversation_id)
    if not kept:
        return
    names = ", ".join(attachment.filename for attachment in kept)
    details = task_files_details(
        kept, message_id=subtask_info.task.task_id, message_type=ESCALATION_RESULT_MESSAGE_TYPE
    )
    summary = f"Files sent with the result of escalation {subtask_info.task.task_id} accepted: {names}"
    if await _files_entry(agent, parent_id, summary, details) is None:
        agent._logger.warning(
            "A task ended before the files its escalation's result brought were recorded. "
            "event=escalation_result_files_task_gone task_id=%s escalation_task_id=%s attachment_ids=%s",
            parent_id,
            subtask_info.task.task_id,
            _ids(kept),
        )
        return
    task_manager = agent.task_manager
    parent_info = await task_manager.operations_runtime.run(task_manager.get_task_info, parent_id)
    if parent_info is not None:
        await asyncio.to_thread(
            record_turn_attachments,
            getattr(agent, "_conversation_store", None),
            parent_info,
            kept,
            logger=agent._logger,
        )


def _ids(attachments: Sequence[UserTaskAttachment]) -> str:
    return ",".join(attachment.attachment_id for attachment in attachments)


__all__ = ["keep_escalation_result_files", "keep_sent_files", "result_with_answer_files"]
