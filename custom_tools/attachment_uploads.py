"""Find a file the person sent by its ``attachment_id``, for the current tool call's task.

``inspect_user_attachment`` and ``load_local_media_for_model_context`` open a
file by id through ``upload_by_id``. The id is looked up only among the files
the task may use (``custom_agents.conversation_attachments``): its request's
files and the files the person sent while it ran, and for the team lead's own
request the files of its conversation's earlier turns, with the person taken
from the task's signed context. An id outside them is "not found", with no hint
that it exists elsewhere.

Uploads are runtime records out of agents' reach (``state/uploads``): the file
opened is the one the record names, and only when it is inside this
instance's uploads folder. Results name a file by id, name, type, size and
turn, never where it is stored.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from custom_agents.access_runtime import verified_person_id
from custom_agents.conversation_attachments import (
    UPLOAD_DELETED,
    UPLOAD_REMOTE,
    AttachmentScope,
    SentFile,
    attachment_scope,
    attachment_summary,
    locate_upload,
    request_files,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_tools.runtime_context import bound_task_id, get_agent_ref
from custom_tools.workspace_scope import runtime_layout

logger = logging.getLogger(__name__)

STATUS_ERROR = "error"
STATUS_REMOTE_COPY_REQUIRED = "remote_copy_required"
# What the team lead can do when this runtime has no copy of a file.
REMOTE_COPY_ACTION = "Request a gateway-backed NATS Object Store mirror for this attachment."


def tool_result(status: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A tool result whose one text block is ``payload`` as JSON."""

    return {"status": status, "content": [{"text": json.dumps(payload, indent=2, sort_keys=True)}]}


@dataclass(frozen=True)
class OpenedUpload:
    """A file the person sent that this runtime holds, found by id for the current task."""

    sent: SentFile
    path: Path  # the real path, inside this instance's uploads folder
    size_bytes: int

    @property
    def attachment(self) -> UserTaskAttachment:
        return self.sent.attachment

    def summary(self) -> dict[str, Any]:
        """How results name the file (``attachment_summary``): never where it is stored."""

        return attachment_summary(self.sent)


def current_attachment_scope(agent: Any) -> Optional[AttachmentScope]:
    """The files the current tool call's task may use; None outside a task this agent holds."""

    task_id = bound_task_id()
    task_manager = getattr(agent, "task_manager", None)
    if not task_id or task_manager is None:
        return None
    # The task and its request links are read in the task manager's lane.
    request = task_manager.operations_runtime.run_sync(lambda: request_files(task_manager, task_id))
    if request is None:
        return None
    return attachment_scope(
        request,
        conversation_store=getattr(agent, "_conversation_store", None),
        is_team_lead=bool(getattr(agent, "is_team_lead", False)),
        person_id=verified_person_id(agent),
    )


def upload_by_id(attachment_id: str) -> OpenedUpload | dict[str, Any]:
    """The file with this id that the current task may use, or the tool result that says why there is none.

    - not one of the task's files: ``error`` listing the files it may use
      (at most 20, newest first after the request's own);
    - removed from this instance (its retention ended, or its person was
      removed): ``error`` saying it is no longer stored;
    - no copy on this runtime: ``remote_copy_required``, for the team lead.
    """

    agent = get_agent_ref()
    wanted = str(attachment_id or "").strip()
    scope = current_attachment_scope(agent) if agent is not None else None
    found = scope.find(wanted) if scope is not None else None
    if found is None:
        logger.info(
            "A tool asked for a file the task may not use. event=attachment_id_not_found task_id=%s attachment_id=%s",
            bound_task_id(),
            wanted[:128],
        )
        available = scope.available() if scope is not None else []
        return tool_result(
            STATUS_ERROR,
            {
                "error": "No file with this attachment_id was sent with this request or earlier in its conversation.",
                "attachment_id": wanted,
                "available_attachments": [attachment_summary(file) for file in available],
            },
        )
    location = locate_upload(found.attachment, runtime_layout(agent).uploads_dir)
    size_bytes = _size(location.path) if location.path is not None else None
    if location.state == UPLOAD_REMOTE:
        return tool_result(
            STATUS_REMOTE_COPY_REQUIRED,
            {
                "attachment": attachment_summary(found),
                "reason": "This runtime has no local copy of the file.",
                "team_lead_action": REMOTE_COPY_ACTION,
            },
        )
    if location.state == UPLOAD_DELETED or location.path is None or size_bytes is None:
        logger.info(
            "A file the person sent is no longer stored. event=attachment_upload_deleted task_id=%s attachment_id=%s",
            bound_task_id(),
            found.attachment.attachment_id,
        )
        return tool_result(
            STATUS_ERROR,
            {
                "error": (
                    "This file is no longer stored: files people send are kept only for the team's file "
                    "retention period. Ask the person to send it again if it is still needed."
                ),
                "attachment": attachment_summary(found),
            },
        )
    return OpenedUpload(found, location.path, size_bytes)


def _size(path: Path) -> Optional[int]:
    """The file's size, or None when it was removed after it was found."""

    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None
