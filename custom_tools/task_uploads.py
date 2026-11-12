"""Open the current task's uploads by attachment id, for agent tools.

``inspect_user_attachment`` and ``load_local_media_for_model_context`` name a
person's uploads by ``attachment_id`` only. This module asks the trusted
resolver (``custom_agents.conversation_attachments``) which files the task the
tool call runs for may use, and where on this host their bytes are:

- the task is the tool call's bound task (``custom_tools.runtime_context``),
  read from the agent's task records, never from tool arguments;
- the task may use its own files (its request's, and those of guidance and
  answers it accepted, from its durable history) and, for a top-level task
  that works for its conversation's person, the files earlier turns of that
  conversation received (``TaskAttachments``). A delegated task (a worker's)
  uses only the files its delegation named;
- bytes are read only from below this host's upload folders, without
  following links (``locate_upload``, ``LocatedUpload``).

A file the task may not use, or whose bytes cannot be read here, comes back as
the tool result to return. Results describe files by their descriptive fields
(``storage.turn_attachments.attachment_summary``) and checksum: no storage
reference or path reaches the model, so a model neither sees nor supplies
where uploads are stored.

Tool results here use the statuses Strands and every model provider accept,
``success`` and ``error``; ``result`` in the payload names the outcome the
agent instructions refer to (``remote_copy_required``, ``upload_unavailable``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from access_control.safe_open import UnsafePath
from custom_agents.conversation_attachments import (
    INVENTORY_LIMIT,
    UNRECORDED_TURNS_NOTE,
    UPLOAD_FILE_MISSING,
    UPLOAD_OUTSIDE_FOLDERS,
    LocatedUpload,
    TaskAttachments,
    UnavailableUpload,
    UploadFolders,
    locate_upload,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_tools.runtime_context import bound_task_id, get_agent_ref
from storage.turn_attachments import attachment_summary

logger = logging.getLogger(__name__)

# ``result`` values of a by-id tool result that did not open the file.
RESULT_UNKNOWN_ATTACHMENT = "unknown_attachment_id"
RESULT_UPLOAD_UNAVAILABLE = "upload_unavailable"
RESULT_REMOTE_COPY_REQUIRED = "remote_copy_required"
RESULT_REFUSED = "refused"
REMOTE_COPY_ACTION = "Request a gateway-backed NATS Object Store mirror for this attachment."


def tool_result(status: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A Strands tool result with ``payload`` as its JSON text (``status``: success or error)."""

    return {"status": status, "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}]}


def described(attachment: UserTaskAttachment) -> dict[str, Any]:
    """How a by-id result names a file: its descriptive fields and checksum, never where it is stored."""

    return {**attachment_summary(attachment), "sha256": attachment.sha256}


def read_failed(attachment: UserTaskAttachment, exc: OSError) -> dict[str, Any]:
    """The error payload when an upload, or the copy of it written to the task's folder, fails.

    Opening the upload itself fails with ``safe_open.UnsafePath`` (a link or
    a second hard link appeared, it grew past the limit, or it is gone),
    whose text names where it is stored, so only its reason is shown. Any
    other error comes from writing the copy into the task's own folder
    (``file_access``), whose text names only that folder, and is shown as is.
    Both are logged with the attachment id.
    """

    logger.warning(
        "An upload could not be read or copied. event=attachment_read_failed attachment_id=%s error=%s",
        attachment.attachment_id,
        exc,
    )
    if isinstance(exc, UnsafePath):
        return {
            "error": "This file could not be opened safely, so it was not read.",
            "reason": exc.reason,
            "attachment": described(attachment),
        }
    return {"error": str(exc), "attachment": described(attachment)}


def remote_copy_required(payload: dict[str, Any]) -> dict[str, Any]:
    """The result telling the agent to ask the team lead for a copy of a file this host cannot read."""

    return tool_result(
        "error", {**payload, "result": RESULT_REMOTE_COPY_REQUIRED, "team_lead_action": REMOTE_COPY_ACTION}
    )


@dataclass(frozen=True)
class TaskUpload:
    """An upload the current task may use, and its bytes on this host."""

    attachment: UserTaskAttachment
    located: LocatedUpload


def open_task_upload(attachment_id: str) -> TaskUpload | dict[str, Any]:
    """The upload ``attachment_id`` names for the current tool call's task, or the tool result saying why not."""

    agent = get_agent_ref()
    wanted = str(attachment_id or "").strip()
    scope = TaskAttachments.for_task(
        getattr(agent, "task_manager", None), _task_info(agent), getattr(agent, "_conversation_store", None)
    )
    found, _unknown = scope.find([wanted])
    if not found:
        return _not_in_scope(scope, wanted)
    attachment = found[0]
    located = locate_upload(
        attachment, UploadFolders.for_agent(agent), task_id=scope.task_id, conversation_id=scope.conversation_id
    )
    if isinstance(located, UnavailableUpload):
        return _unavailable(attachment, located)
    return TaskUpload(attachment, located)


def _task_info(agent: Any) -> Any:
    """The record of the task the tool call runs for, or None outside a task's handling."""

    task_id = bound_task_id()
    get_task_info = getattr(getattr(agent, "task_manager", None), "get_task_info", None)
    if not task_id or not callable(get_task_info):
        return None
    return get_task_info(task_id)


def _not_in_scope(scope: TaskAttachments, attachment_id: str) -> dict[str, Any]:
    """The result for an id that is not a file the task may use: the files it may use instead."""

    logger.info(
        "An attachment id is not a file this task may use. "
        "event=attachment_id_not_available attachment_id=%s task_id=%s conversation_id=%s",
        attachment_id or "-",
        scope.task_id or "-",
        scope.conversation_id or "-",
    )
    payload: dict[str, Any] = {
        "error": (
            "This attachment_id is not a file this task may use. available_attachments lists the files it may "
            f"use, newest first (at most {INVENTORY_LIMIT})."
        ),
        "result": RESULT_UNKNOWN_ATTACHMENT,
        "attachment_id": attachment_id,
        "available_attachments": scope.choices(),
    }
    unrecorded: Optional[int] = scope.last_unrecorded_turn()
    if unrecorded is not None:
        payload["note"] = UNRECORDED_TURNS_NOTE.format(turn=unrecorded)
    return tool_result("error", payload)


def _unavailable(attachment: UserTaskAttachment, located: UnavailableUpload) -> dict[str, Any]:
    """The result for a file the task may use whose bytes cannot be read here (``locate_upload`` logged it)."""

    payload = {"attachment": described(attachment), "reason": located.reason, "error": located.message}
    if located.reason == UPLOAD_FILE_MISSING:
        return tool_result("error", {**payload, "result": RESULT_UPLOAD_UNAVAILABLE})
    if located.reason == UPLOAD_OUTSIDE_FOLDERS:
        return tool_result("error", {**payload, "result": RESULT_REFUSED})
    return remote_copy_required(payload)


__all__ = [
    "REMOTE_COPY_ACTION",
    "RESULT_REFUSED",
    "RESULT_REMOTE_COPY_REQUIRED",
    "RESULT_UNKNOWN_ATTACHMENT",
    "RESULT_UPLOAD_UNAVAILABLE",
    "TaskUpload",
    "described",
    "open_task_upload",
    "read_failed",
    "remote_copy_required",
    "tool_result",
]
