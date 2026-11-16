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

Whether the files are here is decided from what the step finds, not from the
folder alone: when records wait for files and none of them is in the transfer
folder or already at its destination (the folder is not there, or a mount
point or a folder the 0.10 adapter host created is empty), nothing changes and
no marker is written: one error says to mount the folder and restart, or, if
the files are really gone, to run the command with ``--files-gone``. Startup
goes on either way, and the next start tries again. A stored name that cannot
be looked up (a NUL byte, too long a name) is no sign either way.

One error rule holds for every file and path operation of the step (opening,
reading, writing, syncing, renaming, removing and looking up a file or folder,
and building its path): each catches ``OSError`` and ``ValueError`` and decides
which of three cases it is.

1. An error that concerns one record or one file refuses only that record: a
   stored name the file system or ``safe_open`` refuses (a NUL byte, ``..``,
   too long a name), a file that cannot be read (its permissions, an I/O
   error), is not a regular file (a pipe is opened without waiting), has
   several links, carries another sha256 or finds its destination taken, and
   a copy, sync, rename or removal of that file that fails. Its records stay
   as they are and the file stays where it is; one warning
   (``event=adapter_host_file_not_moved`` with the folder, the file, the
   attachment id, ``records=kept`` and the reason) reports it, and it never
   stops startup. The file tools refuse a record outside ``state/uploads``
   (``outside_upload_folders``), so the file cannot be opened until it moves:
   fix the file, delete the marker and restart, and the step tries the
   records it kept again (moved files are no longer found there, so nothing
   moves twice).
2. An error that concerns a folder (the transfer folder or ``state/uploads``
   not there, no permission to read or search a folder, a read-only or full
   ``state/uploads`` or a transfer folder the move cannot remove names from,
   a folder sync that fails) changes nothing further:
   every record not yet finished stays and no marker is written. A transfer
   folder that is not here or holds none of the files waits, as above; any
   other folder error raises ``FolderProblem``, whose message names the folder
   and, when one file was being worked on, that file and its attachment id;
   the launcher stops startup, and a restart after the fix finishes the work.
   So does any other unexpected error (a database error). A sync the file
   system does not offer (``EINVAL`` or ``EOPNOTSUPP``, as some FUSE, 9p and
   shared-folder mounts answer) is no failure: it is logged once per folder
   (``event=adapter_host_files_sync_unsupported``) and the step goes on.
3. A record is dropped only when its file moved and both records were
   rewritten, when the file is proven absent from a transfer folder that
   opened, or with ``--files-gone`` (a transfer folder that is not here then
   holds none of the files). A dropped file's conversation entries are
   removed from the turns, which are marked as not recording all of their
   request's files (``attachments_recorded`` 0, the marker 0.9.12 shipped), so
   the model asks the person to send the file again, and its gateway record
   is deleted. Records are dropped only after every file that can move has
   moved, so a run that stops on a folder problem has dropped none. Once a
   file has moved, a later error (a folder sync) raises ``FolderProblem``,
   never a refusal, and the next run counts the file ``already_moved``. A
   file is not absent while something is at its destination in
   ``state/uploads`` or that cannot be told: a run that stopped between the
   rename and the record rewrite left it there, so when the next run cannot
   read that copy it refuses the file and keeps its records, and a run after
   the fix counts it ``already_moved``. A conversation entry with no gateway record (no chat session for its folder)
   or naming another path or sha256 than the gateway record is never moved:
   it is dropped when its file is proven absent and kept otherwise.

Held task records and messages still queued are not rewritten: the
message-bound context id hashes every field of each file record, path
included (``access_control.ownership``), so a rewritten record would no
longer match what the gateway signed. The file tools refuse those records as
outside ``state/uploads``, and the team lead's stuck-task check tells the
person which files to send again (``custom_agents.stuck_tasks``,
``task_files_sent_before_upgrade``).

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
from contextlib import contextmanager, suppress
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

# Why a file was not moved (logged with its attachment id); safe_open's reasons
# (link, not_regular_file, several_links, unsafe_name, not_directory) are used as they are.
REASON_MISSING = "missing"
REASON_NO_GATEWAY_RECORD = "no_gateway_record"
REASON_UNSAFE_ID = "unsafe_attachment_id"
REASON_OUTSIDE_FOLDER = "outside_transfer_folder"
REASON_SHA256_MISMATCH = "sha256_mismatch"
REASON_DESTINATION_TAKEN = "destination_taken"
REASON_RECORD_MISMATCH = "record_mismatch"
REASON_NAME_REFUSED = "name_refused"  # the file system refused the name (ENAMETOOLONG, EINVAL)
REASON_UNREADABLE = "unreadable"  # the file could not be opened or read (its permissions, an I/O error)
REASON_MOVE_FAILED = "move_failed"  # renaming or copying that file failed (an I/O error)
REASON_COPY_MISMATCH = "copy_mismatch"  # the cross-disk copy read back differently

# Outcomes per attachment id.
OUTCOME_MOVED = "moved"
OUTCOME_ALREADY_MOVED = "already_moved"
OUTCOME_MISSING = "missing"
OUTCOME_REFUSED = "refused"
OUTCOME_NO_RECORD = "no_record"

# What happened to the records of a file that was not moved (``records=`` in its warning).
RECORDS_DROPPED = "dropped"
RECORDS_KEPT = "kept"
RECORDS_PARTLY_DROPPED = "partly_dropped"

# What 0.9.13's adapter host named each file it kept, directly in its transfer folder
# (``new_id("adapter_upload")`` in ``assistant_gateway.adapter_host.service``).
TRANSFER_FILE_PREFIX = "adapter_upload_"

# Errors that come from one record's names and recur at every start (rule 1).
_NAME_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.EINVAL})
# Errors writing into state/uploads that every file would meet: a folder error (rule 2).
_DESTINATION_FOLDER_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EDQUOT})
# Answers of a file system that offers no fsync (some FUSE, 9p and shared-folder mounts): not a failure.
_SYNC_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.EOPNOTSUPP})
# Permission errors, for which an open is told apart from a folder that cannot be searched.
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})

# Files are opened like ``safe_open`` opens them: no link followed, and a planted pipe
# does not wait for a writer before ``fstat`` refuses it (reads of a regular file are unchanged).
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK

# What ``_look`` saw.
_THERE = "there"
_ABSENT = "absent"
_UNKNOWN = "unknown"  # the name could not be looked up: no sign either way

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
    path: Path

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
    """One record's file does not move (rule 1), or is proven absent (``OUTCOME_MISSING``, rule 3).

    ``folder`` and ``file`` say where, for the warning.
    """

    def __init__(
        self, outcome: str, reason: str, folder: Optional[Path] = None, file: Optional[Path] = None, detail: str = ""
    ) -> None:
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason
        self.folder = folder
        self.file = file
        self.detail = detail


@dataclass
class _NotMoved:
    """Records of a file that did not move: which are dropped once every file that can move has moved."""

    item: _Item
    unmovable: _Unmovable
    drop_entries: list[_Entry]
    kept_entries: list[_Entry]
    drop_record: bool
    kept_record: bool


class FolderProblem(Exception):
    """A folder the step needs cannot be used (rule 2); nothing more changes, and a restart after the fix finishes the work.

    ``file`` is the file being worked on when the error came, and
    ``attachment_id`` its attachment (set by the caller that knows it).
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
            "Fix the folder (mount it, its permissions, free space) and restart; nothing was lost."
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
        transfer=transfer,
        uploads=uploads,
        gateway=gateway,
        conversations=conversations,
        marker=marker,
        files_gone=files_gone,
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
        _write_marker(marker, step.counts)
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
        marker: Path,
        files_gone: bool = False,
    ) -> None:
        self._transfer = transfer
        self._uploads = uploads
        self._gateway = gateway
        self._conversations = conversations
        self._marker = marker
        # With --files-gone a transfer folder that is not here holds none of the files (rule 3).
        self._files_gone = files_gone
        self.counts = UpgradeCounts()
        self._unrecorded_turns: set[tuple[str, str]] = set()
        self._not_moved: list[_NotMoved] = []
        self._absent: dict[Path, bool] = {}
        self._sync_unsupported: set[Path] = set()

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
        open them, so nothing moves or removes them. The name is checked
        later (``_names``), so a bad stored name refuses only its record.
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
        """How many attachment ids wait for files in the transfer folder, and whether one of those files is here.

        A file is here when it is in the transfer folder or already at its
        destination (an earlier run moved it and stopped). When none is, the
        folder is not here or is not the one 0.9.13 wrote (an empty mount
        point, a folder the 0.10 adapter host created), so the caller changes
        nothing. A name that cannot be looked up says nothing about the
        folder. A folder that answers with another error raises ``FolderProblem``.
        """

        waiting = 0
        found = False
        for item in items:
            waiting += 1
            if not found:
                with _naming(item.attachment_id):
                    found = self._here(item)
        return waiting, found

    def _here(self, item: _Item) -> bool:
        candidates = {entry.path for entry in item.entries}
        if item.record is not None:
            try:
                source, destination = self._names(item, item.record)
            except _Unmovable:
                pass  # a record whose names cannot be used is no sign either way
            else:
                if _look(self._uploads, destination, absent_is_missing=True) == _THERE:
                    return True
                candidates.add(source)
        return any(
            _look(self._transfer, (path.name,), absent_is_missing=True) == _THERE
            for path in sorted(candidates)
        )

    # -- handling one file -------------------------------------------------------

    def handle(self, item: _Item) -> None:
        with _naming(item.attachment_id):
            record = item.record
            if record is None:
                unmovable = _Unmovable(OUTCOME_NO_RECORD, REASON_NO_GATEWAY_RECORD, self._transfer)
                self._keep_or_drop(item, unmovable, item.entries, record_path=None, absent_source=None)
                return
            names: Optional[tuple[Path, tuple[str, str]]] = None
            try:
                names = self._names(item, record)
                outcome, remove_source = self._place(*names, record.sha256)
            except _Unmovable as unmovable:
                source_path = self._in_transfer(record.local_path)
                absent = source_path if unmovable.outcome == OUTCOME_MISSING else None
                self._keep_or_drop(
                    item,
                    unmovable,
                    item.entries,
                    record_path=source_path,
                    absent_source=absent,
                    destination=names[1] if names is not None else None,
                )
                return
            source, destination = names
            moved_with = [
                entry for entry in item.entries if entry.path == source and entry.attachment.sha256 == record.sha256
            ]
            others = [entry for entry in item.entries if entry not in moved_with]
            new_path = str(self._uploads.joinpath(*destination))
            if moved_with:
                self._conversations.relocate_turn_attachment(
                    sorted({entry.turn for entry in moved_with}), item.attachment_id, local_path=new_path
                )
            self._gateway.set_attachment_local_path(item.attachment_id, new_path)
            if remove_source:
                self._remove_source(source, item.attachment_id)
            if others:
                # Entries naming another path or sha256 never follow the file (agent processes write turns).
                mismatch = _Unmovable(OUTCOME_REFUSED, REASON_RECORD_MISMATCH, self._transfer)
                self._keep_or_drop(item, mismatch, others, record_path=None, absent_source=None, counted=False)
            setattr(self.counts, outcome, getattr(self.counts, outcome) + 1)

    def _names(self, item: _Item, record: AttachmentRecord) -> tuple[Path, tuple[str, str]]:
        """The record's source path and destination parts, or ``_Unmovable`` when one of its names cannot be used."""

        if not _plain_name(item.attachment_id):
            raise _Unmovable(OUTCOME_REFUSED, REASON_UNSAFE_ID, self._transfer)
        source = self._in_transfer(record.local_path)
        if source is None:
            raise _Unmovable(OUTCOME_REFUSED, REASON_OUTSIDE_FOLDER, self._transfer)
        if not _safe_component(source.name):
            raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_UNSAFE_NAME, self._transfer, source)
        try:
            suffix = Path(sanitize_attachment_filename(record.filename)).suffix
            destination = (safe_attachment_session_dir_name(record.session_key), f"{record.attachment_id}{suffix}")
        except ValueError as exc:
            raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_UNSAFE_NAME, self._uploads, None, str(exc)) from exc
        if not all(_safe_component(part) for part in destination):
            raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_UNSAFE_NAME, self._uploads, self._uploads.joinpath(*destination))
        return source, destination

    def _place(self, source: Path, destination: tuple[str, str], sha256: str) -> tuple[str, bool]:
        """Put the file at ``destination``: the outcome, and whether the source copy is to be removed after."""

        at_destination = self._destination_digest(destination)
        if at_destination == sha256:
            # An earlier run stopped after moving it; a source still there is
            # the copy's original, removed once the records point here.
            return OUTCOME_ALREADY_MOVED, self._same_file_left(source, sha256)
        if at_destination is not None:
            raise _Unmovable(OUTCOME_REFUSED, REASON_DESTINATION_TAKEN, self._uploads, self._uploads.joinpath(*destination))
        return OUTCOME_MOVED, self._move(source, destination, sha256)

    def _destination_digest(self, destination: tuple[str, str]) -> Optional[str]:
        """The sha256 of the file already at ``destination``, or None when there is none (``state/uploads`` may not exist yet)."""

        file = self._uploads.joinpath(*destination)
        with _opened(self._uploads, destination, absent_is_missing=True) as opened:
            if opened is None:
                return None
            return _read_digest(opened[1], self._uploads, file)

    def _same_file_left(self, source: Path, sha256: str) -> bool:
        """Whether ``source`` is still a safe copy of the moved file, to be removed.

        Removing it only tidies the transfer folder, so any error here is
        logged and the copy is left.
        """

        try:
            with _opened(self._transfer, (source.name,), absent_is_missing=True) as opened:
                return opened is not None and _read_digest(opened[1], self._transfer, source) == sha256
        except (_Unmovable, FolderProblem) as exc:
            _log_source_left(source, exc.reason)
            return False

    def _remove_source(self, source: Path, attachment_id: str) -> None:
        """Remove the source copy after a cross-file-system move; a failure leaves a stray copy, logged (rule 1)."""

        try:
            safe_open.remove_file(self._transfer, (source.name,))
        except safe_open.UnsafePath as exc:
            if exc.reason != safe_open.REASON_NOT_FOUND:
                _log_source_left(source, exc.reason, attachment_id)
        except (OSError, ValueError) as exc:
            _log_source_left(source, _detail(exc), attachment_id)

    # -- moving one file ---------------------------------------------------------

    def _move(self, source: Path, destination: tuple[str, str], sha256: str) -> bool:
        """Move the checked file into ``state/uploads``; True when it was copied and its source is to be removed later.

        Both folders are opened without following links (the destination's
        session folder is created), the source is opened with ``O_NOFOLLOW``
        and ``O_NONBLOCK`` and must be a regular file with one link and the
        record's sha256. Only the gateway writes either folder and it is not
        running, so nothing changes the file between the check and the rename.
        The transfer folder was found before the run, so its absence now is a
        folder problem, unless ``--files-gone``.
        """

        try:
            self._uploads.mkdir(parents=True, exist_ok=True)  # an instance whose gateway stored no file yet
        except OSError as exc:
            raise FolderProblem(self._uploads, safe_open.REASON_OPEN_FAILED, _detail(exc)) from exc
        destination_file = self._uploads.joinpath(*destination)
        with _opened(self._transfer, (source.name,), absent_is_missing=self._files_gone) as opened:
            if opened is None:
                raise _Unmovable(OUTCOME_MISSING, REASON_MISSING, self._transfer, source)
            source_dir, descriptor, info = opened
            if _read_digest(descriptor, self._transfer, source) != sha256:
                raise _Unmovable(OUTCOME_REFUSED, REASON_SHA256_MISMATCH, self._transfer, source)
            destination_dir = _open_folder(self._uploads, destination[:-1], destination_file, create=True)
            if destination_dir is None:  # created a moment ago, and gone again
                detail = "the session folder vanished"
                raise FolderProblem(self._uploads, safe_open.REASON_OPEN_FAILED, detail, file=destination_file)
            try:
                try:
                    os.rename(source.name, destination[-1], src_dir_fd=source_dir, dst_dir_fd=destination_dir)
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        self._copy_into_place(source, descriptor, info, destination_dir, destination_file, sha256)
                        return True
                    if exc.errno == errno.ENOENT:
                        raise self._vanished(source_dir, source, destination_file) from exc
                    if exc.errno in _DESTINATION_FOLDER_ERRNOS and not os.access(self._transfer, os.W_OK | os.X_OK):
                        # The rename also removes the name from the transfer folder, which may be the one refused.
                        raise FolderProblem(self._transfer, safe_open.REASON_OPEN_FAILED, _detail(exc), file=source) from exc
                    raise _write_error(exc, self._uploads, destination_file, REASON_MOVE_FAILED) from exc
                except ValueError as exc:
                    raise _file_error(exc, self._uploads, destination_file, REASON_MOVE_FAILED) from exc
                # The rename happened: from here only a FolderProblem, never an _Unmovable, which would drop the
                # moved file's records; the next run counts it already_moved.
                self._sync_folder(destination_dir, self._uploads, destination_file)
                self._sync_folder(source_dir, self._transfer, source)
                return False
            finally:
                os.close(destination_dir)

    def _vanished(self, source_dir: int, source: Path, destination_file: Path) -> Exception:
        """A rename found no file: the source proven absent from its open folder, or the destination folder gone."""

        try:
            os.stat(source.name, dir_fd=source_dir, follow_symlinks=False)
        except FileNotFoundError:
            return _Unmovable(OUTCOME_MISSING, REASON_MISSING, self._transfer, source)
        except OSError:
            pass
        return FolderProblem(
            self._uploads, safe_open.REASON_OPEN_FAILED, "the destination folder vanished", file=destination_file
        )

    def _copy_into_place(
        self, source: Path, descriptor: int, info: os.stat_result, directory: int, destination_file: Path, sha256: str
    ) -> None:
        """Copy across file systems: a temporary file in ``directory``, synced, its mode and times kept, renamed into place.

        The transfer folder may be a network or FUSE mount, where a write can be
        short, so each chunk is written until all of it is, and the copy is read
        back and must carry the record's sha256 before it is renamed into place.
        Any error before that removes the temporary file, so the original and
        its records stay. The temporary name is fixed-length, so a name that
        fits does not fail only because of the copy.
        """

        name = destination_file.name
        temporary = f".moving-{hashlib.sha256(name.encode()).hexdigest()[:16]}"
        temporary_file = destination_file.with_name(temporary)
        try:
            os.unlink(temporary, dir_fd=directory)  # left by a run that stopped while copying
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _write_error(exc, self._uploads, temporary_file, REASON_MOVE_FAILED) from exc
        try:
            target = os.open(
                temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
        except OSError as exc:
            raise _write_error(exc, self._uploads, temporary_file, REASON_MOVE_FAILED) from exc
        try:
            try:
                self._fill(source, descriptor, info, target, temporary_file, sha256)
            except BaseException:
                with suppress(OSError):  # the first error is the one reported
                    os.close(target)
                raise
            try:
                # A network file system may report a delayed write error only at close.
                os.close(target)
            except OSError as exc:
                raise _write_error(exc, self._uploads, temporary_file, REASON_MOVE_FAILED) from exc
            try:
                os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            except OSError as exc:
                raise _write_error(exc, self._uploads, destination_file, REASON_MOVE_FAILED) from exc
        except BaseException:
            self._remove_temporary(directory, temporary_file)
            raise
        # The copy is in place: only a FolderProblem from here (see _move).
        self._sync_folder(directory, self._uploads, destination_file)

    def _fill(
        self, source: Path, descriptor: int, info: os.stat_result, target: int, temporary_file: Path, sha256: str
    ) -> None:
        offset = 0
        while True:
            try:
                chunk = os.pread(descriptor, _CHUNK, offset)
            except OSError as exc:
                raise _file_error(exc, self._transfer, source, REASON_UNREADABLE) from exc
            if not chunk:
                break
            pending = memoryview(chunk)
            while pending:
                try:
                    written = os.write(target, pending)
                except OSError as exc:
                    raise _write_error(exc, self._uploads, temporary_file, REASON_MOVE_FAILED) from exc
                if written <= 0:
                    raise _Unmovable(OUTCOME_REFUSED, REASON_MOVE_FAILED, self._uploads, temporary_file, "nothing written")
                pending = pending[written:]
            offset += len(chunk)
        self._sync_file(target, temporary_file)
        if _read_digest(target, self._uploads, temporary_file) != sha256:
            raise _Unmovable(
                OUTCOME_REFUSED, REASON_COPY_MISMATCH, self._uploads, temporary_file, "the copy read back differently"
            )
        try:
            os.fchmod(target, stat.S_IMODE(info.st_mode))
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
        except OSError as exc:
            raise _write_error(exc, self._uploads, temporary_file, REASON_MOVE_FAILED) from exc
        self._sync_file(target, temporary_file)

    def _remove_temporary(self, directory: int, temporary_file: Path) -> None:
        """Remove a copy that did not finish; a failure is logged, and the next run removes it before copying."""

        try:
            os.unlink(temporary_file.name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "An unfinished copy in state/uploads could not be removed; the next run removes it. "
                "event=adapter_host_file_temporary_left file=%s reason=%s",
                temporary_file,
                _detail(exc),
            )

    def _sync_file(self, descriptor: int, file: Path) -> None:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno in _SYNC_UNSUPPORTED_ERRNOS:
                self._note_sync_unsupported(self._uploads, exc)
                return
            raise _write_error(exc, self._uploads, file, REASON_MOVE_FAILED) from exc

    def _sync_folder(self, descriptor: int, folder: Path, file: Path) -> None:
        """Sync a folder after a rename into or out of it: a failure is a folder error (rule 2)."""

        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno in _SYNC_UNSUPPORTED_ERRNOS:
                self._note_sync_unsupported(folder, exc)
                return
            raise FolderProblem(folder, safe_open.REASON_OPEN_FAILED, f"sync: {_detail(exc)}", file=file) from exc

    def _note_sync_unsupported(self, folder: Path, exc: OSError) -> None:
        if folder in self._sync_unsupported:
            return
        self._sync_unsupported.add(folder)
        logger.warning(
            "The file system of a folder does not offer fsync, so moved files there are not flushed to disk "
            "separately. event=adapter_host_files_sync_unsupported folder=%s reason=%s",
            folder,
            _detail(exc),
        )

    # -- files it does not move ----------------------------------------------------

    def _keep_or_drop(
        self,
        item: _Item,
        unmovable: _Unmovable,
        entries: list[_Entry],
        *,
        record_path: Optional[Path],
        absent_source: Optional[Path],
        destination: Optional[tuple[str, str]] = None,
        counted: bool = True,
    ) -> None:
        """Decide which records of a file that did not move are dropped (rule 3); applied by ``forget_unmovable``.

        Each record is dropped only when the file it names is proven absent:
        ``absent_source`` already was (the open found no file), any other
        path is looked up. So a record refused for one reason whose file is
        also gone is dropped like a missing one. A file is not absent while
        something is at its ``destination`` in ``state/uploads``, or that
        cannot be told: an earlier run may have renamed it there and stopped
        before rewriting its records, so its records stay and a later run
        counts it ``already_moved``. Everything is decided before anything is
        dropped.
        """

        at_destination = destination is not None and self._maybe_at_destination(destination)

        def absent(path: Optional[Path]) -> bool:
            if path is None or at_destination:
                return False
            return path == absent_source or self._proven_absent(path)

        drop_entries: list[_Entry] = []
        kept_entries: list[_Entry] = []
        for entry in entries:
            (drop_entries if absent(entry.path) else kept_entries).append(entry)
        has_record = counted and item.record is not None
        drop_record = has_record and absent(record_path)
        self._not_moved.append(
            _NotMoved(item, unmovable, drop_entries, kept_entries, drop_record, has_record and not drop_record)
        )
        if counted:
            setattr(self.counts, unmovable.outcome, getattr(self.counts, unmovable.outcome) + 1)

    def _maybe_at_destination(self, destination: tuple[str, str]) -> bool:
        """Whether something is at ``destination``, or that cannot be told; only a proven absence says no.

        This decides only whether records are kept for a file already refused
        (rule 1), so an error looking it up keeps them rather than stopping
        startup.
        """

        try:
            return _look(self._uploads, destination, absent_is_missing=True) != _ABSENT
        except FolderProblem:
            return True

    def _proven_absent(self, path: Path) -> bool:
        """Whether ``path`` is missing from a transfer folder that opened (or, with ``--files-gone``, is not here)."""

        if path not in self._absent:
            self._absent[path] = _look(self._transfer, (path.name,), absent_is_missing=self._files_gone) == _ABSENT
        return self._absent[path]

    def forget_unmovable(self) -> None:
        """Report every file that did not move and drop the records decided on, after the moves."""

        for not_moved in self._not_moved:
            self._forget(not_moved)
        self._not_moved.clear()

    def _forget(self, not_moved: _NotMoved) -> None:
        item, unmovable = not_moved.item, not_moved.unmovable
        dropped = bool(not_moved.drop_entries) or not_moved.drop_record
        kept = bool(not_moved.kept_entries) or not_moved.kept_record
        records = RECORDS_PARTLY_DROPPED if dropped and kept else RECORDS_DROPPED if dropped else RECORDS_KEPT
        entries = not_moved.drop_entries + not_moved.kept_entries
        file = unmovable.file or (entries[0].path if entries else None)
        if kept:
            advice = (
                f"its records were kept, so it cannot be opened until it is moved: fix the file, delete "
                f"{self._marker} and restart to try it again"
            )
        else:
            advice = "its records were dropped, so the person must send it again"
        logger.warning(
            "A file of a chat app the adapter host ran was not moved into state/uploads; %s%s. "
            "event=adapter_host_file_not_moved attachment_id=%s conversation_ids=%s folder=%s file=%s records=%s "
            "reason=%s",
            advice,
            f" ({unmovable.detail})" if unmovable.detail else "",
            item.attachment_id,
            ",".join(sorted({entry.conversation_id for entry in entries})) or "-",
            unmovable.folder or self._transfer,
            file or "-",
            records,
            unmovable.reason,
        )
        if not_moved.drop_entries:
            turns = sorted({entry.turn for entry in not_moved.drop_entries})
            self._conversations.relocate_turn_attachment(turns, item.attachment_id, local_path=None)
            self._unrecorded_turns.update(turns)
            self.counts.turns_marked_unrecorded = len(self._unrecorded_turns)
        if not_moved.drop_record and self._gateway.delete_attachment(item.attachment_id):
            self.counts.gateway_rows_removed += 1


def _plain_name(attachment_id: str) -> bool:
    """Whether ``attachment_id`` can be one file name (the gateway's own ids are ``att_...``)."""

    return _safe_component(attachment_id) and "\\" not in attachment_id


def _safe_component(name: str) -> bool:
    """Whether ``name`` is one path component ``safe_open`` accepts (its ``_checked_parts`` rule)."""

    return bool(name) and name not in {".", ".."} and "/" not in name and "\x00" not in name


def _detail(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"{errno.errorcode.get(exc.errno, exc.errno)}: {exc.strerror or exc}"
    return str(exc)


def _file_error(exc: BaseException, folder: Path, file: Optional[Path], reason: str) -> _Unmovable:
    """An error from an operation on one file (its name, opening, reading, copying or renaming it): rule 1."""

    if isinstance(exc, OSError) and exc.errno == errno.ELOOP:
        return _Unmovable(OUTCOME_REFUSED, safe_open.REASON_LINK, folder, file, _detail(exc))
    if isinstance(exc, ValueError):
        return _Unmovable(OUTCOME_REFUSED, safe_open.REASON_UNSAFE_NAME, folder, file, _detail(exc))
    if isinstance(exc, OSError) and exc.errno in _NAME_ERRNOS:
        return _Unmovable(OUTCOME_REFUSED, REASON_NAME_REFUSED, folder, file, _detail(exc))
    return _Unmovable(OUTCOME_REFUSED, reason, folder, file, _detail(exc))


def _write_error(exc: OSError, folder: Path, file: Path, reason: str) -> _Unmovable:
    """An error writing into ``state/uploads``: read-only, full or not permitted is a folder error (rule 2), else rule 1."""

    if exc.errno in _DESTINATION_FOLDER_ERRNOS:
        raise FolderProblem(folder, safe_open.REASON_OPEN_FAILED, _detail(exc), file=file) from exc
    return _file_error(exc, folder, file, reason)


def _open_folder(root: Path, parts: tuple[str, ...], file: Path, *, absent_is_missing: bool = False, create: bool = False) -> Optional[int]:
    """``root/parts`` opened as a folder without following links; None when it is not there.

    ``root`` not there counts as absent only when ``absent_is_missing``; it,
    or any folder, that cannot be opened or created is a folder error (rule 2).
    A session folder whose own name is the problem (a link, not a folder,
    too long a name) refuses only that record (rule 1).
    """

    try:
        return safe_open.open_directory(root, parts, create=create)
    except safe_open.UnsafePath as exc:
        cause = exc.__cause__
        if exc.reason == safe_open.REASON_NOT_FOUND:
            return None
        if exc.reason == safe_open.REASON_ROOT_UNAVAILABLE:
            if absent_is_missing and isinstance(cause, FileNotFoundError):
                return None
            raise FolderProblem(root, exc.reason, _detail(cause or exc), file=file) from exc
        if exc.reason == safe_open.REASON_OPEN_FAILED and isinstance(cause, OSError):
            if cause.errno not in _NAME_ERRNOS:
                raise FolderProblem(root, exc.reason, _detail(cause), file=file) from exc
            raise _Unmovable(OUTCOME_REFUSED, REASON_NAME_REFUSED, root, file, _detail(cause)) from exc
        raise _Unmovable(OUTCOME_REFUSED, exc.reason, root, file, str(exc)) from exc


@contextmanager
def _opened(root: Path, parts: tuple[str, ...], *, absent_is_missing: bool) -> Iterator[Optional[tuple[int, int, os.stat_result]]]:
    """``root/parts`` opened for reading without following links: (its folder, the file, its status), or None when absent.

    The file is absent only when its folder opened and has no such entry, or
    when ``root`` is not there and ``absent_is_missing``. The folder's errors
    are rule 2; the file's own (its name, permissions, kind, links) rule 1. A
    permission error opening the file is told apart from a folder that cannot
    be searched by looking the name up in the open folder.
    """

    file = root.joinpath(*parts)
    directory = _open_folder(root, parts[:-1], file, absent_is_missing=absent_is_missing)
    if directory is None:
        yield None
        return
    try:
        name = parts[-1]
        try:
            descriptor: Optional[int] = os.open(name, _READ_FLAGS, dir_fd=directory)
        except FileNotFoundError:
            descriptor = None
        except (OSError, ValueError) as exc:
            if isinstance(exc, OSError) and exc.errno in _PERMISSION_ERRNOS and not _searchable(directory, name):
                raise FolderProblem(root, safe_open.REASON_OPEN_FAILED, _detail(exc), file=file) from exc
            raise _file_error(exc, root, file, REASON_UNREADABLE) from exc
        if descriptor is None:
            yield None
            return
        try:
            try:
                info = os.fstat(descriptor)
            except OSError as exc:
                raise _file_error(exc, root, file, REASON_UNREADABLE) from exc
            if not stat.S_ISREG(info.st_mode):
                raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_NOT_REGULAR_FILE, root, file)
            if info.st_nlink != 1:
                raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_SEVERAL_LINKS, root, file)
            yield directory, descriptor, info
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _searchable(directory: int, name: str) -> bool:
    """Whether ``name`` can be looked up in the open folder (a file's own permissions do not stop that)."""

    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError as exc:
        return exc.errno not in _PERMISSION_ERRNOS
    return True


def _look(root: Path, parts: tuple[str, ...], *, absent_is_missing: bool) -> str:
    """Whether ``root/parts`` is there (any kind of entry; a link is not followed), absent, or cannot be looked up.

    A name that cannot be looked up (refused by ``safe_open``, a NUL byte,
    too long a name, a session folder that is a link) is ``_UNKNOWN``: no
    sign either way. A folder that cannot be opened or searched raises
    ``FolderProblem``.
    """

    if not all(_safe_component(part) for part in parts):
        return _UNKNOWN
    file = root.joinpath(*parts)
    try:
        directory = _open_folder(root, parts[:-1], file, absent_is_missing=absent_is_missing)
    except _Unmovable:
        return _UNKNOWN
    if directory is None:
        return _ABSENT
    try:
        os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return _ABSENT
    except ValueError:
        return _UNKNOWN
    except OSError as exc:
        if exc.errno in _NAME_ERRNOS:
            return _UNKNOWN
        raise FolderProblem(root, safe_open.REASON_OPEN_FAILED, _detail(exc), file=file) from exc
    finally:
        os.close(directory)
    return _THERE


def _read_digest(descriptor: int, folder: Path, file: Path) -> str:
    """The sha256 of an open file; a read error refuses that file (rule 1)."""

    digest = hashlib.sha256()
    offset = 0
    try:
        while chunk := os.pread(descriptor, _CHUNK, offset):
            digest.update(chunk)
            offset += len(chunk)
    except OSError as exc:
        raise _file_error(exc, folder, file, REASON_UNREADABLE) from exc
    return digest.hexdigest()


def _log_source_left(file: Path, reason: str, attachment_id: Optional[str] = None) -> None:
    logger.warning(
        "A moved file's original in the adapter host's transfer folder could not be checked or removed, so it is "
        "left there. event=adapter_host_file_source_left attachment_id=%s file=%s reason=%s",
        attachment_id or "-",
        file,
        reason,
    )


def _write_marker(marker: Path, counts: UpgradeCounts) -> None:
    """Record that the step finished, written to a temporary file and renamed into place (a failure is rule 2)."""

    temporary = marker.with_name(f".{marker.name}.writing")
    body = {"finished_at_utc": datetime.now(timezone.utc).isoformat(), **asdict(counts)}
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, marker)
    except OSError as exc:
        raise FolderProblem(marker.parent, safe_open.REASON_OPEN_FAILED, _detail(exc), file=marker) from exc


__all__ = ["FolderProblem", "UpgradeCounts", "UpgradeResult", "upgrade_adapter_host_files"]
