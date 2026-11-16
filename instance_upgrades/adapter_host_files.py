"""The upgrade step that moves files of chat apps an adapter host ran into ``state/uploads``.

Why it exists. Up to 0.9.13 a file a person sent through a chat app the
adapter host runs stayed in the adapter host's transfer folder
(``interactive_apps.adapter_host.upload_dir``, by default
``runtime/adapter-host-uploads``), named ``adapter_upload_<id>``. Its path was
saved in the gateway's attachment record (``gateway_attachments.local_path``)
and in the conversation turns that received it
(``conversation_task_links.attachments_json``). From 0.10 the gateway stores
every file in ``state/uploads`` and agents open files only there
(``custom_agents.conversation_attachments.UploadFolder``); upload retention,
history clean-up and a person's removal delete only files there. Without this
step the earlier files of those conversations could no longer be opened and
would never be deleted.

When it runs. Once, at the first start of 0.10: the launcher runs
``assistant_cli upgrade adapter-host-files`` as a first-start step
(``docker.launch_headless.build_adapter_host_files_task``) after the
credential import and before the gateway, the adapter host and the agents
start, while ``InstanceLayout.adapter_host_files_upgrade_path`` is missing. So
no other process on this host writes ``state/uploads``, the transfer folder or
the two tables; remote workers may share a PostgreSQL conversation store, so
conversation turns change under row locks
(``ConversationRepository.relocate_turn_attachment``).

What it does, per attachment id:

1. finds the files: gateway records and conversation entries naming a file
   0.9.13 wrote, ``<transfer folder>/adapter_upload_<id>`` (the conversation
   pass decodes the stored JSON, because a SQL substring match on JSON text
   is not reliable);
2. checks each: the id is a plain file name (it becomes one), the path lies
   inside the transfer folder, the file is opened without following links
   (``access_control.safe_open``), is a regular file with one link, and its
   sha256 is the gateway record's. Records are not trusted by themselves:
   agent processes write conversation turns. A conversation entry is moved
   with the file only when it names the same path and sha256 as the gateway
   record;
3. moves it to ``state/uploads/<session>/<id><suffix>``, the layout new
   uploads get (``inbound_attachments.store_inbound_attachment_bytes_with_policy``),
   by ``rename`` (which keeps the modification time), or, when the transfer
   folder is on another file system, by a copy that keeps the modification
   time, is checked against the record's sha256 and is renamed into place,
   the source removed only after both records changed;
4. rewrites the conversation entries first and the gateway record second.
   A run that stops part way is finished by the next one: a destination that
   already holds the file (same sha256) counts as ``already_moved`` and both
   records are rewritten again.

Files it cannot move (missing from a folder that opens, refused by a check,
or listed only in a conversation entry with no gateway record, so with no
chat session for its folder): their entries are removed from the turns, which
are marked as not recording all of their request's files
(``attachments_recorded`` 0, the marker 0.9.12 shipped), so the model asks the
person to send the file again; their gateway record, if any, is deleted,
because it points at no file it could open. The file itself is left where it
is. This happens only after every file that can move has moved, so a run that
stops on a folder problem has dropped no record.

Held task records and messages still queued are not rewritten: the
message-bound context id hashes every field of each file record, path
included (``access_control.ownership``), so a rewritten record would no
longer match what the gateway signed. The file tools refuse those records as
outside ``state/uploads``, and the team lead's stuck-task check tells the
person which files to send again (``custom_agents.stuck_tasks``,
``task_files_sent_before_upgrade``).

Whether the files are here is decided from what the step finds, not from the
folder alone: when gateway records wait for files and none of them is in the
transfer folder or already at its destination (the folder is not there, or a
mount point or a folder the 0.10 adapter host created is empty), nothing
changes and no marker is written: one error says to mount the folder and
restart, or, if the files are really gone, to run the command with
``--files-gone``, which handles every file it cannot find as missing. Startup
goes on either way, and the next start tries again.

A folder or file it cannot use (permission denied, an I/O error while
reading or writing, a read-only or full ``state/uploads``, a folder that
vanished during the run) raises ``FolderProblem``, naming the folder and,
when one file was being worked on, that file and its attachment id, so the
operator knows what to fix or move away; the launcher stops startup, and a
restart after the fix finishes the work. So does any other unexpected error
(a database error). An error that comes from one record's names and would
recur at every start (``ENAMETOOLONG``, ``EINVAL``) refuses that file
instead, so one bad record cannot block startup; such a name is also no
sign that the folder holds the files. A source that is not a regular file
(a pipe) is opened without waiting for a writer and refused.

It lives in ``instance_upgrades``, not ``assistant_gateway``, because it
changes the team's conversation store, which the gateway runtime never
imports. It goes with the other 0.9 upgrade steps when 0.11 ships.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from access_control import safe_open
from assistant_gateway.domain.delivery import AttachmentRecord
from assistant_gateway.inbound_attachments import safe_attachment_session_dir_name, sanitize_attachment_filename
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from storage.conversation_repository import ConversationRepository, create_conversation_repository
from storage.gateway_state_repository import GatewayStateRepository, create_gateway_state_repository

logger = logging.getLogger(__name__)

# Why a file could not be moved (logged with its attachment id).
REASON_MISSING = "missing"
REASON_NO_GATEWAY_RECORD = "no_gateway_record"
REASON_UNSAFE_ID = "unsafe_attachment_id"
REASON_OUTSIDE_FOLDER = "outside_transfer_folder"
REASON_SHA256_MISMATCH = "sha256_mismatch"
REASON_DESTINATION_TAKEN = "destination_taken"
REASON_RECORD_MISMATCH = "record_mismatch"
REASON_NAME_REFUSED = "name_refused"  # the file system refused the name (ENAMETOOLONG, EINVAL)
REASON_COPY_MISMATCH = "copy_mismatch"  # a FolderProblem: the cross-disk copy read back differently

# Outcomes per attachment id.
OUTCOME_MOVED = "moved"
OUTCOME_ALREADY_MOVED = "already_moved"
OUTCOME_MISSING = "missing"
OUTCOME_REFUSED = "refused"
OUTCOME_NO_RECORD = "no_record"

# What 0.9.13's adapter host named each file it kept, directly in its transfer folder
# (``new_id("adapter_upload")`` in ``assistant_gateway.adapter_host.service``).
TRANSFER_FILE_PREFIX = "adapter_upload_"

# Errors that come from one record's names and recur at every start: that file is
# refused, so one bad record cannot stop startup for good.
_RECORD_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.EINVAL})

# The source is opened like ``safe_open`` opens files: no link followed, and a planted pipe
# does not wait for a writer before ``fstat`` refuses it (reads of a regular file are unchanged).
_SOURCE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK

_BATCH = 500
_CHUNK = 1024 * 1024


@dataclass
class UpgradeCounts:
    """What the step did, logged in ``event=adapter_host_files_upgraded`` and kept in the marker."""

    moved: int = 0
    already_moved: int = 0
    missing: int = 0
    refused: int = 0
    no_record: int = 0
    gateway_rows_removed: int = 0
    turns_marked_unrecorded: int = 0

    @property
    def not_moved(self) -> int:
        return self.missing + self.refused + self.no_record


@dataclass(frozen=True)
class UpgradeResult:
    """``finished`` once the marker is written; ``folder_missing`` when records wait for a folder that holds none of their files."""

    finished: bool
    counts: UpgradeCounts
    waiting: int = 0
    already_done: bool = False

    @property
    def folder_missing(self) -> bool:
        return not self.finished and self.waiting > 0


@dataclass
class _Entry:
    """One conversation turn's record of a file in the transfer folder."""

    conversation_id: str
    task_id: str
    attachment: UserTaskAttachment
    path: Optional[Path]

    @property
    def turn(self) -> tuple[str, str]:
        return (self.conversation_id, self.task_id)


@dataclass
class _Item:
    """One attachment id with a file in the transfer folder: its gateway record and conversation entries."""

    attachment_id: str
    record: Optional[AttachmentRecord] = None
    entries: list[_Entry] = field(default_factory=list)


class _Unmovable(Exception):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


class FolderProblem(Exception):
    """A folder the step needs cannot be used; nothing more changes, and a restart after the fix finishes the work.

    ``file`` is the file being read or written when the error came, and
    ``attachment_id`` its attachment (set by the caller that knows it), so a
    problem with one file names that file: the folder itself may be fine.
    """

    def __init__(self, folder: Path, reason: str, detail: str, *, file: Optional[Path] = None) -> None:
        super().__init__(detail)
        self.folder = folder
        self.reason = reason
        self.detail = detail
        self.file = file
        self.attachment_id: Optional[str] = None

    def __str__(self) -> str:
        at = f", working on {self.file}" if self.file is not None else ""
        if self.attachment_id is not None:
            at += f" for attachment {self.attachment_id}"
        return (
            f"The upgrade step for adapter-host files cannot use the folder {self.folder} ({self.detail}{at}). "
            "Fix the folder or that file (mount it, its permissions, free space; a file that cannot be read can be "
            "moved away, and the person then sends it again) and restart; nothing was lost."
        )


@contextmanager
def _naming(attachment_id: str) -> Iterator[None]:
    """Add ``attachment_id`` to a ``FolderProblem`` raised while working on its file."""

    try:
        yield
    except FolderProblem as problem:
        if problem.attachment_id is None:
            problem.attachment_id = attachment_id
        raise


def upgrade_adapter_host_files(config: Any, *, files_gone: bool = False) -> UpgradeResult:
    """Run the step for the instance of ``config`` (a ``GatewayInstanceConfig``); see the module docstring."""

    layout = config.layout
    marker = Path(layout.adapter_host_files_upgrade_path)
    if marker.is_file():
        return UpgradeResult(finished=True, counts=UpgradeCounts(), already_done=True)
    transfer = config.interactive_apps.adapter_host.resolved_upload_dir(config.instance_root_path)
    uploads = Path(layout.uploads_dir).resolve()
    gateway = create_gateway_state_repository(config.state_storage_settings, logger=logger)
    conversations = create_conversation_repository(
        config.manager_storage_settings, artifacts_dir=layout.conversation_artifacts_dir, logger=logger
    )
    step = _Step(
        transfer=transfer, uploads=uploads, gateway=gateway, conversations=conversations, files_gone=files_gone
    )
    try:
        items = step.find()
        waiting, found = step.survey(items)
        if waiting and not found and not files_gone:
            logger.error(
                "Files sent through chat apps the adapter host ran are recorded in its transfer folder, but the "
                "folder is not here or holds none of them, so they were not moved into state/uploads and nothing "
                "was changed. Mount the folder (interactive_apps.adapter_host.upload_dir) and restart; if the files "
                "are really gone, run 'assistant_cli upgrade adapter-host-files --config <instance_config.yaml> "
                "--files-gone'. event=adapter_host_files_folder_missing folder=%s waiting=%s",
                transfer,
                waiting,
            )
            return UpgradeResult(finished=False, counts=UpgradeCounts(), waiting=waiting)
        for item in items:
            step.handle(item)
        step.forget_unmovable()
    except FolderProblem as problem:
        logger.error(
            "%s event=adapter_host_files_folder_unusable folder=%s file=%s attachment_id=%s reason=%s",
            problem,
            problem.folder,
            problem.file or "-",
            problem.attachment_id or "-",
            problem.reason,
        )
        raise
    _write_marker(marker, step.counts)
    counts = step.counts
    logger.log(
        logging.WARNING if counts.not_moved else logging.INFO,
        "Files of chat apps the adapter host ran were moved into state/uploads. event=adapter_host_files_upgraded "
        "moved=%s already_moved=%s missing=%s refused=%s no_record=%s gateway_rows_removed=%s "
        "turns_marked_unrecorded=%s",
        counts.moved,
        counts.already_moved,
        counts.missing,
        counts.refused,
        counts.no_record,
        counts.gateway_rows_removed,
        counts.turns_marked_unrecorded,
    )
    return UpgradeResult(finished=True, counts=counts)


class _Step:
    def __init__(
        self,
        *,
        transfer: Path,
        uploads: Path,
        gateway: GatewayStateRepository,
        conversations: ConversationRepository,
        files_gone: bool = False,
    ) -> None:
        self._transfer = transfer
        self._uploads = uploads
        self._gateway = gateway
        self._conversations = conversations
        # With --files-gone a transfer folder that is not there means every file in it is missing.
        self._files_gone = files_gone
        self.counts = UpgradeCounts()
        self._unrecorded_turns: set[tuple[str, str]] = set()
        self._unmovable: list[tuple[_Item, _Unmovable]] = []

    # -- finding ---------------------------------------------------------------

    def find(self) -> list[_Item]:
        """Every attachment id with a gateway record or a conversation entry in the transfer folder."""

        items: dict[str, _Item] = {}
        after_id = ""
        while True:
            records = self._gateway.list_attachments_under(str(self._transfer), after_id=after_id, limit=_BATCH)
            for record in records:
                if self._in_transfer(record.local_path) is not None:
                    items.setdefault(record.attachment_id, _Item(record.attachment_id)).record = record
            if len(records) < _BATCH:
                break
            after_id = records[-1].attachment_id
        for entry in self._entries():
            items.setdefault(entry.attachment.attachment_id, _Item(entry.attachment.attachment_id)).entries.append(entry)
        return [items[key] for key in sorted(items)]

    def _entries(self) -> Iterator[_Entry]:
        after: Optional[tuple[str, str]] = None
        while True:
            turns = self._conversations.scan_turn_attachments(after=after, limit=_BATCH)
            for conversation_id, task_id, attachments in turns:
                for attachment in attachments:
                    path = self._in_transfer(attachment.local_path())
                    if path is not None:
                        yield _Entry(conversation_id, task_id, attachment, path)
            if len(turns) < _BATCH:
                return
            after = (turns[-1][0], turns[-1][1])

    def _in_transfer(self, raw: Optional[str]) -> Optional[Path]:
        """``raw`` normalized when it names a file still waiting in the transfer folder, else None.

        Only what 0.9.13 wrote waits: ``adapter_upload_<id>`` directly in the
        folder, so an operator who pointed the folder at a folder holding
        other files (the instance root, ``workspaces``) has none of those
        moved or dropped. Normalized without following links: the file is
        opened later from the folder, which refuses a link. A path already
        inside ``state/uploads`` is not waiting, also when an operator set the
        transfer folder to, around or inside it: those files are where agents
        open them, so nothing moves or removes them.
        """

        if not raw or not os.path.isabs(raw):
            return None
        path = Path(os.path.normpath(raw))
        if path.parent != self._transfer or not path.name.startswith(TRANSFER_FILE_PREFIX):
            return None
        if self._uploads == path or self._uploads in path.parents:
            return None
        return path

    def survey(self, items: list[_Item]) -> tuple[int, bool]:
        """How many files gateway records wait for, and whether one of those is here.

        A file is here when it is in the transfer folder or already at its
        destination (an earlier run moved it and stopped). When none is, the
        folder is not here or is not the one 0.9.13 wrote (an empty mount
        point, a folder the 0.10 adapter host created), so the caller changes
        nothing. A file name the file system refuses (``name_refused``) says
        nothing about the folder, so it does not count. A folder that answers
        with another error raises ``FolderProblem``.
        """

        waiting = 0
        found = False
        for item in items:
            record = item.record
            source = self._in_transfer(record.local_path) if record is not None else None
            if record is None or source is None:
                continue
            waiting += 1
            if not found:
                with _naming(item.attachment_id):
                    # The destination is looked at only for an id that can be a file name (one handle() moves).
                    moved = _plain_name(item.attachment_id) and (
                        _look(self._uploads, self._destination(record), absent_is_missing=True) is None
                    )
                    found = moved or _entry_there(_look(self._transfer, (source.name,), absent_is_missing=True))
        return waiting, found

    # -- handling one file -------------------------------------------------------

    def handle(self, item: _Item) -> None:
        record = item.record
        try:
            if record is None:
                raise _Unmovable(OUTCOME_NO_RECORD, REASON_NO_GATEWAY_RECORD)
            source = self._in_transfer(record.local_path)
            if not _plain_name(item.attachment_id):
                raise _Unmovable(OUTCOME_REFUSED, REASON_UNSAFE_ID)
            if source is None:
                raise _Unmovable(OUTCOME_REFUSED, REASON_OUTSIDE_FOLDER)
            destination = self._destination(record)
            with _naming(item.attachment_id):
                outcome, remove_source = self._place(source, destination, record.sha256)
        except _Unmovable as unmovable:
            self._unmovable.append((item, unmovable))  # forgotten once every file that can move has moved
            return
        moved_with = [entry for entry in item.entries if entry.path == source and entry.attachment.sha256 == record.sha256]
        others = [entry for entry in item.entries if entry not in moved_with]
        new_path = str(self._uploads.joinpath(*destination))
        if moved_with:
            self._conversations.relocate_turn_attachment(
                sorted({entry.turn for entry in moved_with}), item.attachment_id, local_path=new_path
            )
        self._gateway.set_attachment_local_path(item.attachment_id, new_path)
        if remove_source:
            _remove_quietly(self._transfer, source.relative_to(self._transfer).parts)
        if others:
            self._drop_entries(item.attachment_id, others, REASON_RECORD_MISMATCH)
        setattr(self.counts, outcome, getattr(self.counts, outcome) + 1)

    def _destination(self, record: AttachmentRecord) -> tuple[str, str]:
        suffix = Path(sanitize_attachment_filename(record.filename)).suffix
        return (safe_attachment_session_dir_name(record.session_key), f"{record.attachment_id}{suffix}")

    def _place(self, source: Path, destination: tuple[str, str], sha256: str) -> tuple[str, bool]:
        """Put the file at ``destination``: the outcome, and whether the source copy is to be removed after."""

        source_parts = source.relative_to(self._transfer).parts
        at_destination = _file_digest(self._uploads, destination)
        if at_destination == sha256:
            # An earlier run stopped after moving it; a source still there is
            # the copy's original, removed once the records point here.
            return OUTCOME_ALREADY_MOVED, _same_file_left(self._transfer, source_parts, sha256)
        if at_destination is not None:
            raise _Unmovable(OUTCOME_REFUSED, REASON_DESTINATION_TAKEN)
        moved = _move(self._transfer, source_parts, self._uploads, destination, sha256, files_gone=self._files_gone)
        return OUTCOME_MOVED, moved

    # -- files it cannot move ----------------------------------------------------

    def forget_unmovable(self) -> None:
        """Drop the records of every file that could not move, after the moves (see the module docstring)."""

        for item, unmovable in self._unmovable:
            self._forget(item, unmovable)
        self._unmovable.clear()

    def _forget(self, item: _Item, unmovable: _Unmovable) -> None:
        setattr(self.counts, unmovable.outcome, getattr(self.counts, unmovable.outcome) + 1)
        logger.warning(
            "A file of a chat app the adapter host ran could not be moved into state/uploads; the person must "
            "send it again. event=adapter_host_file_not_moved attachment_id=%s conversation_ids=%s reason=%s",
            item.attachment_id,
            ",".join(sorted({entry.conversation_id for entry in item.entries})) or "-",
            unmovable.reason,
        )
        self._drop_entries(item.attachment_id, item.entries, None)
        if item.record is not None and self._gateway.delete_attachment(item.attachment_id):
            self.counts.gateway_rows_removed += 1

    def _drop_entries(self, attachment_id: str, entries: list[_Entry], reason: Optional[str]) -> None:
        if not entries:
            return
        if reason is not None:
            logger.warning(
                "A conversation's record of a moved file named another path or checksum, so it was removed. "
                "event=adapter_host_file_not_moved attachment_id=%s conversation_ids=%s reason=%s",
                attachment_id,
                ",".join(sorted({entry.conversation_id for entry in entries})),
                reason,
            )
        turns = sorted({entry.turn for entry in entries})
        self._conversations.relocate_turn_attachment(turns, attachment_id, local_path=None)
        self._unrecorded_turns.update(turns)
        self.counts.turns_marked_unrecorded = len(self._unrecorded_turns)


def _plain_name(attachment_id: str) -> bool:
    """Whether ``attachment_id`` can be one file name (the gateway's own ids are ``att_...``)."""

    return (
        bool(attachment_id)
        and attachment_id not in {".", ".."}
        and not any(character in attachment_id for character in ("/", "\\", "\x00"))
    )


def _judge_os(exc: OSError, folder: Path, file: Optional[Path] = None) -> _Unmovable:
    """What an error opening, reading, renaming or writing ``file`` in ``folder`` means for that file.

    The file is missing (``ENOENT``) or refused (a link, or a name error that
    recurs at every start); anything else is a problem with the folder or the
    file (permission denied, I/O error, read-only or full disk) and raises
    ``FolderProblem`` naming both.
    """

    if exc.errno == errno.ENOENT:
        return _Unmovable(OUTCOME_MISSING, REASON_MISSING)
    if exc.errno == errno.ELOOP:
        return _Unmovable(OUTCOME_REFUSED, safe_open.REASON_LINK)
    if exc.errno in _RECORD_ERRNOS:
        return _Unmovable(OUTCOME_REFUSED, REASON_NAME_REFUSED)
    raise FolderProblem(folder, safe_open.REASON_OPEN_FAILED, exc.strerror or str(exc), file=file) from exc


def _judge(
    exc: safe_open.UnsafePath, folder: Path, *, absent_is_missing: bool, file: Optional[Path] = None
) -> _Unmovable:
    """``_judge_os`` for a ``safe_open`` refusal under ``folder``.

    A folder that is not there is missing files only when ``absent_is_missing``
    (no file stored there yet, or ``--files-gone``); otherwise it, like a
    folder that cannot be opened for another reason, raises ``FolderProblem``.
    """

    cause = exc.__cause__
    if exc.reason == safe_open.REASON_NOT_FOUND:
        return _Unmovable(OUTCOME_MISSING, REASON_MISSING)
    if exc.reason == safe_open.REASON_ROOT_UNAVAILABLE:
        if absent_is_missing and isinstance(cause, FileNotFoundError):
            return _Unmovable(OUTCOME_MISSING, REASON_MISSING)
        detail = cause.strerror if isinstance(cause, OSError) and cause.strerror else str(exc)
        raise FolderProblem(folder, exc.reason, detail, file=file) from exc
    if exc.reason == safe_open.REASON_OPEN_FAILED and isinstance(cause, OSError):
        return _judge_os(cause, folder, file)
    return _Unmovable(OUTCOME_REFUSED, exc.reason)


def _look(root: Path, parts: tuple[str, ...], *, absent_is_missing: bool) -> Optional[_Unmovable]:
    """None when ``root/parts`` is there (a link is not followed), else what its absence or refusal means."""

    file = root.joinpath(*parts)
    try:
        status = safe_open.entry_status(root, parts)
    except safe_open.UnsafePath as exc:
        return _judge(exc, root, absent_is_missing=absent_is_missing, file=file)
    except OSError as exc:
        return _judge_os(exc, root, file)
    return None if status is not None else _Unmovable(OUTCOME_MISSING, REASON_MISSING)


def _entry_there(look: Optional[_Unmovable]) -> bool:
    """Whether a ``_look`` saw an entry: there, or refused for what it is (a link, not a regular file).

    A name the file system refused was never looked up, so it is no sign of the file.
    """

    return look is None or (look.outcome != OUTCOME_MISSING and look.reason != REASON_NAME_REFUSED)


def _file_digest(root: Path, parts: tuple[str, ...]) -> Optional[str]:
    """The sha256 of the regular file ``root/parts`` (opened without following links); None when it is missing.

    ``root`` is ``state/uploads``, which may not exist yet. A file that is
    there but cannot be read safely (a link, not a regular file, several hard
    links) raises ``_Unmovable``; a folder problem or a read error raises
    ``FolderProblem`` naming the file.
    """

    file = root.joinpath(*parts)
    try:
        with safe_open.opened_file(root, parts) as (descriptor, _info):
            try:
                return _digest(descriptor)
            except OSError as exc:
                raise _judge_os(exc, root, file) from exc
    except safe_open.UnsafePath as exc:
        unmovable = _judge(exc, root, absent_is_missing=True, file=file)
        if unmovable.outcome == OUTCOME_MISSING:
            return None
        raise unmovable from exc


def _same_file_left(root: Path, parts: tuple[str, ...], sha256: str) -> bool:
    """Whether ``root/parts`` is still a safe copy of the file (sha256 ``sha256``), to be removed.

    Removing it only tidies the transfer folder, so a folder problem or a
    read error here is logged and the copy is left.
    """

    file = root.joinpath(*parts)
    try:
        with safe_open.opened_file(root, parts) as (descriptor, _info):
            return _digest(descriptor) == sha256
    except safe_open.UnsafePath as exc:
        try:
            _judge(exc, root, absent_is_missing=True, file=file)
        except FolderProblem as problem:
            _log_source_left(file, problem.reason)
        return False
    except OSError:  # a read error in _digest
        _log_source_left(file, safe_open.REASON_OPEN_FAILED)
        return False


def _log_source_left(file: Path, reason: str) -> None:
    logger.warning(
        "A moved file's original in the adapter host's transfer folder could not be checked, so it is left there. "
        "event=adapter_host_file_source_left file=%s reason=%s",
        file,
        reason,
    )


def _digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, _CHUNK, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _move(
    transfer: Path,
    source_parts: tuple[str, ...],
    uploads: Path,
    destination: tuple[str, str],
    sha256: str,
    *,
    files_gone: bool,
) -> bool:
    """Move the checked file into ``uploads``; True when it was copied and its source is to be removed later.

    Both folders are opened without following links (the destination's
    session folder is created), the source is opened with ``O_NOFOLLOW`` and
    ``O_NONBLOCK`` (a pipe does not wait for a writer; ``fstat`` refuses it)
    and must be a regular file with one link and the record's sha256. Only the
    gateway writes either folder and it is not running, so nothing changes
    the file between the check and the rename. The transfer folder was found
    before the run, so its absence now is a folder problem, unless
    ``files_gone``.
    """

    try:
        uploads.mkdir(parents=True, exist_ok=True)  # an instance whose gateway stored no file yet
    except OSError as exc:
        raise FolderProblem(uploads, safe_open.REASON_OPEN_FAILED, exc.strerror or str(exc)) from exc
    source_file = transfer.joinpath(*source_parts)
    destination_file = uploads.joinpath(*destination)
    try:
        source_dir = safe_open.open_directory(transfer, source_parts[:-1])
    except safe_open.UnsafePath as exc:
        raise _judge(exc, transfer, absent_is_missing=files_gone, file=source_file) from exc
    try:
        name = source_parts[-1]
        try:
            descriptor = os.open(name, _SOURCE_FLAGS, dir_fd=source_dir)
        except OSError as exc:
            raise _judge_os(exc, transfer, source_file) from exc
        try:
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_NOT_REGULAR_FILE)
                if info.st_nlink != 1:
                    raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_SEVERAL_LINKS)
                digest = _digest(descriptor)
            except OSError as exc:
                raise _judge_os(exc, transfer, source_file) from exc
            if digest != sha256:
                raise _Unmovable(OUTCOME_REFUSED, REASON_SHA256_MISMATCH)
            try:
                destination_dir = safe_open.open_directory(uploads, destination[:-1], create=True)
            except safe_open.UnsafePath as exc:
                raise _judge(exc, uploads, absent_is_missing=False, file=destination_file) from exc
            try:
                try:
                    os.rename(name, destination[-1], src_dir_fd=source_dir, dst_dir_fd=destination_dir)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise _judge_os(exc, uploads, destination_file) from exc
                    try:
                        _copy_into_place(descriptor, info, destination_dir, destination[-1], sha256, uploads)
                    except FolderProblem as problem:
                        problem.file = destination_file
                        raise
                    except OSError as copy_error:
                        raise _judge_os(copy_error, uploads, destination_file) from copy_error
                    return True
                try:
                    os.fsync(destination_dir)
                    os.fsync(source_dir)
                except OSError as exc:
                    # The rename happened: never an _Unmovable, which would drop the moved file's records.
                    detail = exc.strerror or str(exc)
                    raise FolderProblem(uploads, safe_open.REASON_OPEN_FAILED, detail, file=destination_file) from exc
                return False
            finally:
                os.close(destination_dir)
        finally:
            os.close(descriptor)
    finally:
        os.close(source_dir)


def _copy_into_place(
    descriptor: int, info: os.stat_result, directory: int, name: str, sha256: str, folder: Path
) -> None:
    """Copy across file systems: a temporary file in ``directory``, flushed, its mode and times kept, renamed to ``name``.

    The transfer folder may be a network or FUSE mount, where a write can be
    short, so each chunk is written until all of it is, and the copy is read
    back and must carry the record's sha256 before it is renamed into place;
    otherwise it is removed and ``FolderProblem`` raised, so the original
    stays and a restart tries again. The temporary name is fixed-length, so a
    name that fits does not fail only because of the copy.
    """

    temporary = f".moving-{hashlib.sha256(name.encode()).hexdigest()[:16]}"
    try:
        os.unlink(temporary, dir_fd=directory)  # left by a run that stopped while copying
    except FileNotFoundError:
        pass
    target = os.open(
        temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        offset = 0
        while chunk := os.pread(descriptor, _CHUNK, offset):
            pending = memoryview(chunk)
            while pending:
                pending = pending[os.write(target, pending) :]
            offset += len(chunk)
        os.fsync(target)
        if _digest(target) != sha256:
            raise FolderProblem(folder, REASON_COPY_MISMATCH, "the copy did not match the file it was made from")
        os.fchmod(target, stat.S_IMODE(info.st_mode))
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.fsync(target)
    except BaseException:
        os.close(target)
        os.unlink(temporary, dir_fd=directory)
        raise
    os.close(target)
    os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


def _remove_quietly(root: Path, parts: tuple[str, ...]) -> None:
    """Remove the source copy after a cross-file-system move; a failure leaves a stray copy, logged."""

    try:
        safe_open.remove_file(root, parts)
    except safe_open.UnsafePath as exc:
        if exc.reason != safe_open.REASON_NOT_FOUND:
            logger.warning(
                "A moved file's original in the adapter host's transfer folder could not be removed. "
                "event=adapter_host_file_source_left reason=%s",
                exc.reason,
            )


def _write_marker(marker: Path, counts: UpgradeCounts) -> None:
    """Record that the step finished, written to a temporary file and renamed into place."""

    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.writing")
    body = {"finished_at_utc": datetime.now(timezone.utc).isoformat(), **asdict(counts)}
    temporary.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, marker)


__all__ = ["FolderProblem", "UpgradeCounts", "UpgradeResult", "upgrade_adapter_host_files"]
