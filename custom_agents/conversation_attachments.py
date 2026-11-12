"""Which uploaded files a task may name, and where their bytes may be read from.

A person's files reach the team through the gateway, which stores the bytes
in this host's upload folders and puts a ``UserTaskAttachment`` record on the
task's messages: the request's own files on ``Task.attachments``, and files
sent while it runs on its guidance and answers (``TaskSteering``). Trusted
runtime code keeps those records in two places: the task's own history (one
``TASK_FILES_EVENT`` entry per accepted guidance or answer with files, written
by ``custom_agents.task_steering_runtime``) and the conversation turn the task
belongs to (``attachments_json`` in ``storage.conversation_repository``,
written by ``custom_agents.conversation_runtime``). Both writers first drop
records that point outside this host's upload folders (``keep_upload_files``).
This module is the one place that decides which of them a task may use; the
attachment tools, the prompt's file list and delegation all ask it.

What a task may use (``TaskAttachments``):

- its own files: ``Task.attachments`` plus the files of every guidance and
  answer it accepted, read from its durable history, so they survive the
  guidance being read and a restart. Only entries this task's runtime wrote
  count: a lead's history also holds copies of its workers' important entries
  (``TaskManager.bubble_subtask_result`` keeps their details), and a worker
  must not add files to its lead's task that way;
- for a top-level task only, the files earlier turns of its own conversation
  received, while it works for the conversation's person
  (``access_control.ownership.conversation_files_access``, with the person
  recorded at admission from the signed task context). A delegated task
  (a worker's) never reads conversation records: it uses only the files it
  was given.

Models name files by ``attachment_id`` only. Records hold where the bytes are
stored, so they leave this module only as descriptive summaries
(``storage.turn_attachments.TURN_ATTACHMENT_SUMMARY_FIELDS``), never with
storage references or paths.

Where bytes are read from (``UploadFolders``, ``locate_upload``): only from
inside this host's upload folders, ``state/uploads`` and the adapter host's
upload folder. Guidance and answers arrive on unsigned steering messages that
other agent processes also send, so a record's path is not trusted by
itself: a record pointing anywhere else is refused and logged.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from access_control.ownership import conversation_files_access
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.enums import TaskHistoryType
from runtime_settings.instance_layout import InstanceLayout
from storage.turn_attachments import TurnAttachment, attachment_summary

logger = logging.getLogger(__name__)

# The most files the prompt lists and an error offers as choices (approved bound).
INVENTORY_LIMIT = 20
# The task-history event that records the files a task accepted with a
# guidance or an answer, and the ``details`` key holding them (full
# ``UserTaskAttachment`` dumps). Written only by ``task_files_details``.
TASK_FILES_EVENT = "task_files_accepted"
TASK_FILES_KEY = "task_files"
# Set by ``TaskManager`` on the copies of a worker's entries in its lead's history.
_BUBBLED_ENTRY_KEY = "bubbled_entry_id"

# Why an upload cannot be read here (``locate_upload``).
UPLOAD_NO_LOCAL_COPY = "no_local_copy"
UPLOAD_NOT_ON_THIS_HOST = "not_on_this_host"
UPLOAD_FILE_MISSING = "file_missing"
UPLOAD_OUTSIDE_FOLDERS = "outside_upload_folders"


def task_files_details(
    attachments: Sequence[UserTaskAttachment], *, steering_id: str, steering_type: str
) -> dict[str, Any]:
    """History ``details`` that make ``attachments`` part of the task's own files.

    For the files of one accepted guidance or answer (``steering_id``), after
    ``keep_upload_files`` checked them.
    """

    return {
        "event": TASK_FILES_EVENT,
        "steering_id": steering_id,
        "steering_type": steering_type,
        TASK_FILES_KEY: [attachment.model_dump(mode="json") for attachment in attachments],
    }


def _file_basics(summary: dict[str, Any]) -> str:
    return (
        f"`{summary.get('attachment_id')}` {summary.get('filename')} "
        f"({summary.get('content_type')}, {summary.get('kind')}, {summary.get('size_bytes')} bytes"
    )


def describe_file(summary: dict[str, Any]) -> str:
    """One line naming a file for a model: id, name, type and size, and the turn it came with."""

    turn_index = summary.get("turn_index")
    if turn_index is None:
        return _file_basics(summary) + "; sent with this task)"
    return _file_basics(summary) + f"; turn {turn_index}, task {summary.get('task_id')})"


def sent_file_lines(attachments: Sequence[UserTaskAttachment], *, subject: str) -> list[str]:
    """Lines naming the files sent with a guidance or an answer (``subject``), for the model.

    Empty without files. Names each file by id, name, type and size only.
    """

    if not attachments:
        return []
    lines = [
        f"Files sent with this {subject} (open one by its attachment_id with "
        "inspect_user_attachment or load_local_media_for_model_context):"
    ]
    lines.extend(f"- {_file_basics(attachment_summary(attachment))})" for attachment in attachments)
    return lines


class AttachmentSelectionError(ValueError):
    """Attachment ids a task named that are not files it may use.

    The message names the unknown ids and lists, newest first, the files the
    task may use (at most ``INVENTORY_LIMIT``), without storage paths.
    """

    def __init__(self, unknown_ids: Sequence[str], choices: Sequence[dict[str, Any]]) -> None:
        self.unknown_ids = tuple(unknown_ids)
        self.choices = tuple(choices)
        super().__init__(self._message())

    def _message(self) -> str:
        head = "These attachment ids are not files this task may use: " + ", ".join(self.unknown_ids) + "."
        if not self.choices:
            return head + " This task has no files it may use."
        lines = [head, f"Files it may use (newest first, at most {INVENTORY_LIMIT}):"]
        lines.extend(f"- {describe_file(choice)}" for choice in self.choices)
        return "\n".join(lines)


@dataclass(frozen=True)
class TaskAttachments:
    """The files one task may name: its own, and for a top-level task its conversation's.

    Build it with ``load``. ``conversation_id`` is set only when the task may
    use its conversation's files (module docstring).
    """

    task_id: str
    own: tuple[UserTaskAttachment, ...] = ()
    conversation_id: Optional[str] = None
    store: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, task_info: Any, conversation_store: Any) -> "TaskAttachments":
        """The scope of the task in ``task_info`` (None gives an empty scope)."""

        task = getattr(task_info, "task", None)
        if task is None:
            return cls(task_id="")
        conversation_id = _conversation_in_scope(task_info, conversation_store)
        return cls(
            task_id=task.task_id,
            own=_own_files(task),
            conversation_id=conversation_id,
            store=conversation_store if conversation_id else None,
        )

    def find(self, attachment_ids: Iterable[str]) -> tuple[list[UserTaskAttachment], list[str]]:
        """The records for ``attachment_ids`` in request order, and the ids that are not in scope.

        Ids are stripped and deduplicated; the task's own files are checked
        first, then its conversation's.
        """

        wanted = list(dict.fromkeys(str(item or "").strip() for item in attachment_ids))
        wanted = [item for item in wanted if item]
        own = {attachment.attachment_id: attachment for attachment in self.own}
        remaining = [item for item in wanted if item not in own]
        earlier: dict[str, UserTaskAttachment] = {}
        if remaining and self.conversation_id:
            listed = self.store.list_turn_attachments(self.conversation_id, attachment_ids=remaining)
            earlier = {entry.attachment.attachment_id: entry.attachment for entry in listed}
        found: list[UserTaskAttachment] = []
        unknown: list[str] = []
        for item in wanted:
            record = own.get(item) or earlier.get(item)
            if record is None:
                unknown.append(item)
            else:
                found.append(record)
        return found, unknown

    def require(self, attachment_ids: Iterable[str]) -> list[UserTaskAttachment]:
        """The records for ``attachment_ids``; ``AttachmentSelectionError`` when any is not in scope."""

        found, unknown = self.find(attachment_ids)
        if unknown:
            raise AttachmentSelectionError(unknown, self.choices())
        return found

    def conversation_files(
        self, *, limit: int = INVENTORY_LIMIT, exclude_ids: Iterable[str] = ()
    ) -> tuple[list[TurnAttachment], bool]:
        """Up to ``limit`` of the conversation's files, newest first, and whether there are more.

        ``exclude_ids`` are left out (files listed elsewhere, such as the
        current request's). The store is asked for at most ``limit`` plus the
        excluded count plus one files, never the whole conversation.
        """

        if not self.conversation_id or limit <= 0:
            return [], False
        excluded = set(exclude_ids)
        listed = self.store.list_turn_attachments(self.conversation_id, limit=limit + len(excluded) + 1)
        kept = [entry for entry in listed if entry.attachment.attachment_id not in excluded]
        return kept[:limit], len(kept) > limit

    def choices(self, limit: int = INVENTORY_LIMIT) -> list[dict[str, Any]]:
        """Descriptive summaries of the files the task may use, newest first: its own, then its conversation's."""

        own = [
            {**attachment_summary(attachment), "turn_index": None, "task_id": self.task_id}
            for attachment in reversed(self.own)
        ][:limit]
        earlier, _more = self.conversation_files(
            limit=limit - len(own), exclude_ids=[attachment.attachment_id for attachment in self.own]
        )
        return own + [entry.summary() for entry in earlier]


def _own_files(task: Any) -> tuple[UserTaskAttachment, ...]:
    """``Task.attachments`` then the files recorded with accepted guidance and answers, one per id."""

    collected = [item for item in list(task.attachments or []) if isinstance(item, UserTaskAttachment)]
    for entry in list(task.execution_history or []):
        details = getattr(entry, "details", None) or {}
        if _written_by_this_task(entry, details):
            collected.extend(_recorded_files(task.task_id, details.get(TASK_FILES_KEY)))
    unique: dict[str, UserTaskAttachment] = {}
    for attachment in collected:
        unique.setdefault(attachment.attachment_id, attachment)
    return tuple(unique.values())


def _written_by_this_task(entry: Any, details: dict[str, Any]) -> bool:
    """A files entry this task's runtime wrote (``task_files_details``).

    A lead's copies of its workers' entries keep their details but are
    subtask events marked with the copied entry's id; they never count, so a
    worker cannot add files to its lead's task.
    """

    return (
        details.get("event") == TASK_FILES_EVENT
        and getattr(entry, "entry_type", None) != TaskHistoryType.SUBTASK_EVENT
        and _BUBBLED_ENTRY_KEY not in details
    )


def _recorded_files(task_id: str, items: Any) -> list[UserTaskAttachment]:
    """The records in one files entry's ``TASK_FILES_KEY`` value; unreadable ones are logged and skipped.

    Only ``task_files_details`` writes them, so an unreadable value is a
    defect worth a warning.
    """

    if items is None:
        return []
    if not isinstance(items, list):
        logger.warning(
            "A task-history file entry was unreadable and was skipped. "
            "event=task_files_entry_unreadable task_id=%s reason=not_a_list",
            task_id,
        )
        return []
    records: list[UserTaskAttachment] = []
    for item in items:
        try:
            records.append(UserTaskAttachment.model_validate(item))
        except ValidationError:
            logger.warning(
                "A task-history file record was unreadable and was skipped. "
                "event=task_files_entry_unreadable task_id=%s attachment_id=%s",
                task_id,
                item.get("attachment_id") if isinstance(item, dict) else None,
            )
    return records


def _conversation_in_scope(task_info: Any, conversation_store: Any) -> Optional[str]:
    """The task's conversation when it may use that conversation's files, else None."""

    task = task_info.task
    if task.parent_task_id is not None or conversation_store is None:
        return None
    conversation_id = str(getattr(getattr(task_info, "meta", None), "conversation_id", "") or "").strip()
    if not conversation_id:
        return None
    thread = conversation_store.get_thread(conversation_id)
    if not thread:
        logger.warning(
            "A task's conversation has no record, so its earlier files are not offered. "
            "event=conversation_files_thread_missing conversation_id=%s task_id=%s",
            conversation_id,
            task.task_id,
        )
        return None
    owner = thread.get("owner_canonical_user_id")
    person = getattr(task_info, "owner_person_id", None)
    decision = conversation_files_access(person_id=person, owner_id=owner, conversation_id=conversation_id)
    if not decision.allowed:
        logger.warning(
            "A task works for someone other than its conversation's person, so the conversation's "
            "files are not offered. event=conversation_files_owner_mismatch conversation_id=%s task_id=%s "
            "conversation_owner=%s task_person=%s",
            conversation_id,
            task.task_id,
            owner or "-",
            person or "-",
        )
        return None
    return conversation_id


@dataclass(frozen=True)
class UploadFolders:
    """This host's upload folders: the only places a file is read from by id.

    ``state/uploads`` (``InstanceLayout.uploads_dir``) and the adapter host's
    upload folder (``AdapterHostSettings.resolved_upload_dir``), resolved.
    """

    paths: tuple[Path, ...]

    @classmethod
    def for_instance(cls, instance_root: str | Path, adapter_host: Any = None) -> "UploadFolders":
        """The folders of the instance at ``instance_root``; ``adapter_host`` is its adapter-host settings."""

        layout = InstanceLayout.at(instance_root)
        folders = [layout.uploads_dir.resolve()]
        if adapter_host is not None:
            folders.append(adapter_host.resolved_upload_dir(layout.root))
        return cls(tuple(dict.fromkeys(folders)))

    @classmethod
    def for_agent(cls, agent: Any) -> "UploadFolders":
        """The folders of the instance ``agent`` runs in, read from its saved settings."""

        from custom_agents.runtime_paths import infer_instance_root

        return cls.for_instance(infer_instance_root(agent), _adapter_host_settings(agent))

    def containing(self, path: Path) -> Optional[Path]:
        """The folder that holds the resolved ``path``, or None."""

        return next((folder for folder in self.paths if folder in path.parents), None)


def _adapter_host_settings(agent: Any) -> Any:
    """The agent's adapter-host settings, or None without an instance config.

    An unreadable config is logged; then only ``state/uploads`` is trusted, so
    adapter-host files are refused rather than read from an unchecked place.
    """

    config_path = str(getattr(agent, "instance_config_path", "") or "").strip()
    if not config_path:
        return None
    from assistant_gateway.config import GatewayInstanceConfig

    try:
        return GatewayInstanceConfig.load_for_process(config_path).interactive_apps.adapter_host
    except Exception:
        logger.warning(
            "The instance settings could not be read, so only state/uploads is used to open files by id. "
            "event=upload_folders_settings_unreadable agent_id=%s instance_config_path=%s",
            getattr(agent, "id", None),
            config_path,
            exc_info=True,
        )
        return None


@dataclass(frozen=True)
class LocatedUpload:
    """An upload's bytes on this host: a regular file inside an upload folder."""

    path: Path
    size_bytes: int


@dataclass(frozen=True)
class UnavailableUpload:
    """Why an upload cannot be read here (``UPLOAD_*``), with a plain explanation for the model."""

    reason: str
    message: str


def locate_upload(
    attachment: UserTaskAttachment,
    folders: UploadFolders,
    *,
    task_id: str,
    conversation_id: Optional[str],
) -> LocatedUpload | UnavailableUpload:
    """Where to read ``attachment``'s bytes on this host, or why they cannot be read.

    The record's local path is resolved (links followed) and must lie inside
    one of ``folders``; anything else is refused and logged with the ids.
    Inside a folder that exists here, a missing file was deleted (upload
    retention, or its person left the team); when the folder is not here, the
    file is on another host.
    """

    local_path = attachment.local_path()
    if not local_path:
        return UnavailableUpload(UPLOAD_NO_LOCAL_COPY, "This file has no copy stored on a host's disk.")
    resolved = Path(local_path).expanduser().resolve()
    folder = folders.containing(resolved)
    if folder is None:
        _log_outside(attachment, task_id, conversation_id, action="it was not opened")
        return UnavailableUpload(
            UPLOAD_OUTSIDE_FOLDERS,
            "This file's record does not point into this host's upload folders, so it cannot be opened.",
        )
    try:
        entry = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        entry = None
    if entry is not None and stat.S_ISREG(entry.st_mode):
        return LocatedUpload(resolved, entry.st_size)
    if folder.is_dir():
        logger.info(
            "An upload's file is gone from this host's upload folder. "
            "event=attachment_upload_missing attachment_id=%s task_id=%s conversation_id=%s",
            attachment.attachment_id,
            task_id,
            conversation_id or "-",
        )
        return UnavailableUpload(
            UPLOAD_FILE_MISSING,
            "This file is no longer stored: uploads are deleted after the team's upload retention period "
            "or when their person leaves the team. Ask the person to send it again.",
        )
    return UnavailableUpload(UPLOAD_NOT_ON_THIS_HOST, "This file is stored on another host, not where this agent runs.")


def keep_upload_files(
    attachments: Iterable[UserTaskAttachment],
    folders: Optional[UploadFolders],
    *,
    task_id: str,
    conversation_id: Optional[str],
) -> list[UserTaskAttachment]:
    """The records trusted code may save for a task: those it could later read by id.

    A record with no local copy is kept (it opens as ``no_local_copy``). A
    record whose local path resolves outside ``folders`` (None: no folder is
    trusted) is dropped and logged with the ids, so it is never saved in the
    task's history or its conversation (module docstring). The file need not
    exist: a deleted upload is reported when it is opened.
    """

    kept: list[UserTaskAttachment] = []
    for attachment in attachments:
        local_path = attachment.local_path()
        if local_path and (folders is None or folders.containing(Path(local_path).expanduser().resolve()) is None):
            _log_outside(attachment, task_id, conversation_id, action="it was not saved")
            continue
        kept.append(attachment)
    return kept


def _log_outside(
    attachment: UserTaskAttachment, task_id: str, conversation_id: Optional[str], *, action: str
) -> None:
    logger.warning(
        "A file record points outside this host's upload folders, so %s. "
        "event=attachment_outside_upload_folders attachment_id=%s task_id=%s conversation_id=%s",
        action,
        attachment.attachment_id,
        task_id,
        conversation_id or "-",
    )


__all__ = [
    "INVENTORY_LIMIT",
    "TASK_FILES_EVENT",
    "TASK_FILES_KEY",
    "UPLOAD_FILE_MISSING",
    "UPLOAD_NOT_ON_THIS_HOST",
    "UPLOAD_NO_LOCAL_COPY",
    "UPLOAD_OUTSIDE_FOLDERS",
    "AttachmentSelectionError",
    "LocatedUpload",
    "TaskAttachments",
    "UnavailableUpload",
    "UploadFolders",
    "describe_file",
    "keep_upload_files",
    "locate_upload",
    "sent_file_lines",
    "task_files_details",
]
