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

The tools read and copy an upload only through ``read_upload`` and
``copy_upload``. Upload retention, a history clean-up or a person's removal
delete uploads from the gateway's process, so a file found here can be gone by
the time a tool reads it, and no lock spans the two processes. Opening the
file is therefore the real check: its failure becomes ``UploadUnreadable``,
whose result says "no longer stored" when the file was removed and gives a
fixed message otherwise, never an operating-system error, which would name
the storage path (its folder name carries chat identifiers). Finding the file
follows the same rule (``conversation_attachments.locate_upload``): removed
(``UPLOAD_GONE_ERRORS``) is "no longer stored", any other fault is the fixed
message, logged as ``event=attachment_upload_unreadable`` with the task and
attachment ids and the error number, and the size comes from that one
look-up, so nothing between the check and the read can raise with the path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Optional

from custom_agents.access_runtime import verified_person_id
from custom_agents.conversation_attachments import (
    UPLOAD_AVAILABLE,
    UPLOAD_GONE_ERRORS,
    UPLOAD_REMOTE,
    UPLOAD_UNREADABLE,
    AttachmentScope,
    SentFile,
    attachment_scope,
    attachment_summary,
    locate_upload,
    log_unreadable_upload,
    request_files,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_tools import file_access
from custom_tools.runtime_context import bound_task_id, get_agent_ref
from custom_tools.workspace_scope import runtime_layout

logger = logging.getLogger(__name__)

STATUS_ERROR = "error"
STATUS_REMOTE_COPY_REQUIRED = "remote_copy_required"
# What the team lead can do when this runtime has no copy of a file.
REMOTE_COPY_ACTION = "Request a gateway-backed NATS Object Store mirror for this attachment."
COPY_CHUNK_BYTES = 1024 * 1024


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
    - there but unreadable (a permission or disk fault): ``error`` with a
      fixed message, logged with the ids and the error number;
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
    location = locate_upload(found.attachment, runtime_layout(agent).uploads_dir, task_id=bound_task_id())
    if location.state == UPLOAD_REMOTE:
        return tool_result(
            STATUS_REMOTE_COPY_REQUIRED,
            {
                "attachment": attachment_summary(found),
                "reason": "This runtime has no local copy of the file.",
                "team_lead_action": REMOTE_COPY_ACTION,
            },
        )
    if location.state == UPLOAD_UNREADABLE:
        return _could_not_read(found)
    if location.state != UPLOAD_AVAILABLE or location.path is None or location.size_bytes is None:
        return _no_longer_stored(found)
    return OpenedUpload(found, location.path, location.size_bytes)


class UploadUnreadable(Exception):
    """An upload found by id could not be read; ``result`` is the tool result to return instead.

    Deliberately not an ``OSError``: the tools' handlers for files named by
    path turn an ``OSError`` into its text, which would show the storage path.
    """

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("A file the person sent could not be read.")
        self.result = result


def read_upload(upload: OpenedUpload, max_bytes: int) -> bytes:
    """At most ``max_bytes`` bytes of the upload; ``UploadUnreadable`` when it cannot be read or is larger."""

    with _opened(upload) as reader:
        data = _read(upload, reader, max_bytes + 1)
    if len(data) > max_bytes:
        raise UploadUnreadable(
            tool_result(
                STATUS_ERROR,
                {"error": f"The file is larger than {max_bytes} bytes.", "attachment": upload.summary()},
            )
        )
    return data


def copy_upload(upload: OpenedUpload, target: Path) -> Path:
    """Copy the upload to ``target`` in the task's folders (``file_access.writable``); the copy's real path.

    The upload is opened first, so a removed one creates nothing in the
    task's folders, and a read failing part way leaves no partial file (the
    writer replaces the target only when the block ends). Read failures raise
    ``UploadUnreadable``; failures writing the target are raised unchanged.
    """

    with _opened(upload) as reader, file_access.writable(target) as (location, writer):
        while chunk := _read(upload, reader, COPY_CHUNK_BYTES):
            writer.write(chunk)
    return location.path


def _opened(upload: OpenedUpload) -> BinaryIO:
    try:
        return upload.path.open("rb")
    except OSError as exc:
        raise _unreadable(upload, exc) from None


def _read(upload: OpenedUpload, reader: BinaryIO, size: int) -> bytes:
    try:
        return reader.read(size)
    except OSError as exc:
        raise _unreadable(upload, exc) from None


def _unreadable(upload: OpenedUpload, exc: OSError) -> UploadUnreadable:
    """The failure as the tools report it: removed means no longer stored; anything else, a fixed message."""

    if isinstance(exc, UPLOAD_GONE_ERRORS):
        return UploadUnreadable(_no_longer_stored(upload.sent))
    # The error's text names the storage path, so only its type and number are logged.
    log_unreadable_upload(bound_task_id(), upload.attachment.attachment_id, type(exc).__name__, exc.errno)
    return UploadUnreadable(_could_not_read(upload.sent))


def _could_not_read(found: SentFile) -> dict[str, Any]:
    """The result for a stored file that cannot be read (a permission or disk fault); the fault is logged by the caller."""

    return tool_result(
        STATUS_ERROR,
        {
            "error": "This file could not be read. Try again later; if it still fails, ask the person to send it again.",
            "attachment": attachment_summary(found),
        },
    )


def _no_longer_stored(found: SentFile) -> dict[str, Any]:
    """The result for a file removed from this instance (its retention ended, a clean-up or its person's removal)."""

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
