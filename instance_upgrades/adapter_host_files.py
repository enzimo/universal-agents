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

1. finds the files: gateway records whose path lies under the transfer folder,
   and conversation entries whose path does (the conversation pass decodes the
   stored JSON, because a SQL substring match on JSON text is not reliable);
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
   time and is renamed into place, the source removed only after both records
   changed;
4. rewrites the conversation entries first and the gateway record second.
   A run that stops part way is finished by the next one: a destination that
   already holds the file (same sha256) counts as ``already_moved`` and both
   records are rewritten again.

Files it cannot move (missing, refused by a check, or listed only in a
conversation entry with no gateway record, so with no chat session for its
folder): their entries are removed from the turns, which are marked as not
recording all of their request's files (``attachments_recorded`` 0, the
marker 0.9.12 shipped), so the model asks the person to send the file again;
their gateway record, if any, is deleted, because it points at no file it
could open. The file itself is left where it is.

Held task records and messages still queued are not rewritten: the
message-bound context id hashes every field of each file record, path
included (``access_control.ownership``), so a rewritten record would no
longer match what the gateway signed. The file tools refuse those records as
outside ``state/uploads``, and the team lead's stuck-task check tells the
person which files to send again (``custom_agents.stuck_tasks``,
``task_files_sent_before_upgrade``).

When the transfer folder is missing while records point into it (not
mounted), nothing changes and no marker is written: one error says to mount
it and restart, or, if the files are really gone, to run the command with
``--files-gone``, which handles every file it cannot find as missing. Startup
goes on either way, and the next start tries again. An unexpected error (a
database error, a full disk) raises, so the launcher stops startup, and a
restart finishes the work.

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

# Outcomes per attachment id.
OUTCOME_MOVED = "moved"
OUTCOME_ALREADY_MOVED = "already_moved"
OUTCOME_MISSING = "missing"
OUTCOME_REFUSED = "refused"
OUTCOME_NO_RECORD = "no_record"

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
    """``finished`` once the marker is written; ``folder_missing`` when records wait for a missing folder."""

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
    step = _Step(transfer=transfer, uploads=uploads, gateway=gateway, conversations=conversations)
    items = step.find()
    if items and not transfer.is_dir() and not files_gone:
        logger.error(
            "Files sent through chat apps the adapter host ran are recorded in its transfer folder, but the folder "
            "is not here, so they were not moved into state/uploads and nothing was changed. Mount the folder "
            "(interactive_apps.adapter_host.upload_dir) and restart; if the files are really gone, run "
            "'assistant_cli upgrade adapter-host-files --config <instance_config.yaml> --files-gone'. "
            "event=adapter_host_files_folder_missing folder=%s waiting=%s",
            transfer,
            len(items),
        )
        return UpgradeResult(finished=False, counts=UpgradeCounts(), waiting=len(items))
    for item in items:
        step.handle(item)
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
    ) -> None:
        self._transfer = transfer
        self._uploads = uploads
        self._gateway = gateway
        self._conversations = conversations
        self.counts = UpgradeCounts()
        self._unrecorded_turns: set[tuple[str, str]] = set()

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

        Normalized without following links: the file is opened later from the
        folder one component at a time, which refuses any link on the way. A
        path already inside ``state/uploads`` is not waiting, also when an
        operator set the transfer folder to, around or inside it: those files
        are where agents open them, so nothing moves or removes them.
        """

        if not raw or not os.path.isabs(raw):
            return None
        path = Path(os.path.normpath(raw))
        if self._transfer not in path.parents or self._uploads == path or self._uploads in path.parents:
            return None
        return path

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
            outcome, remove_source = self._place(source, destination, record.sha256)
        except _Unmovable as unmovable:
            self._forget(item, unmovable)
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
        return OUTCOME_MOVED, _move(self._transfer, source_parts, self._uploads, destination, sha256)

    # -- files it cannot move ----------------------------------------------------

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


def _file_digest(root: Path, parts: tuple[str, ...]) -> Optional[str]:
    """The sha256 of the regular file ``root/parts`` (opened without following links); None when it is missing.

    A file that is there but cannot be read safely (a link, not a regular
    file, several hard links) raises ``_Unmovable``.
    """

    try:
        with safe_open.opened_file(root, parts) as (descriptor, _info):
            return _digest(descriptor)
    except safe_open.UnsafePath as exc:
        if exc.reason in {safe_open.REASON_NOT_FOUND, safe_open.REASON_ROOT_UNAVAILABLE}:
            return None
        raise _Unmovable(OUTCOME_REFUSED, exc.reason) from exc


def _same_file_left(root: Path, parts: tuple[str, ...], sha256: str) -> bool:
    """Whether ``root/parts`` is still a safe copy of the file (sha256 ``sha256``), to be removed."""

    try:
        return _file_digest(root, parts) == sha256
    except _Unmovable:
        return False


def _digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, _CHUNK, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _move(
    transfer: Path, source_parts: tuple[str, ...], uploads: Path, destination: tuple[str, str], sha256: str
) -> bool:
    """Move the checked file into ``uploads``; True when it was copied and its source is to be removed later.

    Both folders are opened without following links (the destination's
    session folder is created), the source is opened with ``O_NOFOLLOW`` and
    must be a regular file with one link and the record's sha256. Only the
    gateway writes either folder and it is not running, so nothing changes
    the file between the check and the rename.
    """

    uploads.mkdir(parents=True, exist_ok=True)  # an instance whose gateway stored no file yet
    try:
        source_dir = safe_open.open_directory(transfer, source_parts[:-1])
    except safe_open.UnsafePath as exc:
        if exc.reason in {safe_open.REASON_NOT_FOUND, safe_open.REASON_ROOT_UNAVAILABLE}:
            raise _Unmovable(OUTCOME_MISSING, REASON_MISSING) from exc
        raise _Unmovable(OUTCOME_REFUSED, exc.reason) from exc
    try:
        name = source_parts[-1]
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=source_dir)
        except FileNotFoundError as exc:
            raise _Unmovable(OUTCOME_MISSING, REASON_MISSING) from exc
        except OSError as exc:
            reason = safe_open.REASON_LINK if exc.errno == errno.ELOOP else safe_open.REASON_OPEN_FAILED
            raise _Unmovable(OUTCOME_REFUSED, reason) from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_NOT_REGULAR_FILE)
            if info.st_nlink != 1:
                raise _Unmovable(OUTCOME_REFUSED, safe_open.REASON_SEVERAL_LINKS)
            if _digest(descriptor) != sha256:
                raise _Unmovable(OUTCOME_REFUSED, REASON_SHA256_MISMATCH)
            try:
                destination_dir = safe_open.open_directory(uploads, destination[:-1], create=True)
            except safe_open.UnsafePath as exc:
                raise _Unmovable(OUTCOME_REFUSED, exc.reason) from exc
            try:
                try:
                    os.rename(name, destination[-1], src_dir_fd=source_dir, dst_dir_fd=destination_dir)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    _copy_into_place(descriptor, info, destination_dir, destination[-1])
                    return True
                os.fsync(destination_dir)
                os.fsync(source_dir)
                return False
            finally:
                os.close(destination_dir)
        finally:
            os.close(descriptor)
    finally:
        os.close(source_dir)


def _copy_into_place(descriptor: int, info: os.stat_result, directory: int, name: str) -> None:
    """Copy across file systems: a temporary file in ``directory``, flushed, its mode and times kept, renamed to ``name``."""

    temporary = f".{name}.moving"
    try:
        os.unlink(temporary, dir_fd=directory)  # left by a run that stopped while copying
    except FileNotFoundError:
        pass
    target = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        offset = 0
        while chunk := os.pread(descriptor, _CHUNK, offset):
            os.write(target, chunk)
            offset += len(chunk)
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


__all__ = ["UpgradeCounts", "UpgradeResult", "upgrade_adapter_host_files"]
