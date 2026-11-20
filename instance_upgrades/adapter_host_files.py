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
(``ConversationRepository.relocate_turn_attachment``). Every run holds the
step's lock (``InstanceLayout.adapter_host_files_upgrade_lock_path``) from
before it reads anything to its end, and one that finds it held waits, so
the launcher's run and an operator's run never move the same file or share a
temporary copy (``_step_lock``).

The operator's ``--files-gone`` run (``docker exec``, as
``user_docs_source/upgrading.md`` shows) runs beside the running team. That
is safe for three reasons, which a later change to the step must keep:

- no gateway attachment record or conversation entry 0.10 writes names the
  transfer folder (the 0.10 adapter host's transfer copies have new upload ids
  and only its own upload records name them; the gateway saves each into
  ``state/uploads`` and records that path), so the step selects no row or
  file the running team creates;
- conversation turns are rewritten from a fresh read in one write
  transaction under the row locks ``merge_turn_attachments`` takes, never
  from what the step read earlier, so a file an agent merges into the same
  turn meanwhile stays;
- a turn or gateway record the team deletes meanwhile (history clean-up, a
  person's removal) is skipped: ``relocate_turn_attachment`` passes over a
  missing turn, and ``set_attachment_local_path`` and ``delete_attachment``
  return False for a missing record.

A file that turns up and moves during such a run while the gateway deletes
its record stays in ``state/uploads`` without a record. Upload retention
removes it by its age (the move keeps the time it was sent) when retention is
on; with ``retention_days`` 0 it stays until someone deletes it.
``test_a_files_gone_run_beside_the_running_team_keeps_what_the_team_writes``
pins what one process can show of the second and third reasons: a file
merged into the turn between the scan and the rewrite stays (the fresh read),
and a record deleted meanwhile is skipped. The row lock itself is argued, not
tested: pinning it needs concurrent writers on a real PostgreSQL server.
Held guidance copies are rewritten the same way, from a fresh read in one
write transaction, and one the gateway released meanwhile is skipped. One race
there is accepted: the gateway's retry may read a held copy, the step rewrite
it, and the gateway sign and send the old path; the team lead then drops that
file as outside ``state/uploads`` (logged) and the gateway releases the copy
on acceptance, so that guidance arrives without the file and the person can
send it again. Only this operator's run can meet it: the first start runs
before the gateway.

What it does, per attachment id:

1. finds the files: gateway records and conversation entries naming a file
   0.9.13 wrote, ``<transfer folder>/adapter_upload_<id>`` (the conversation
   pass decodes the stored JSON, because a SQL substring match on JSON text
   is not reliable);
2. checks each: the id is a plain file name (it becomes one), the path lies
   inside the transfer folder, the file is opened without following links
   (``access_control.safe_open``), is a regular file with one link, and its
   sha256 is the gateway record's. Records are not trusted by themselves:
   agent processes write conversation turns. A conversation entry or a held
   guidance copy is moved with the file only when it names the same path and
   sha256 as the gateway record;
3. moves it to ``state/uploads/<session>/<id><suffix>``, the layout new
   uploads get (``inbound_attachments.store_inbound_attachment_bytes_with_policy``),
   by ``rename`` (which keeps the modification time), or, when the transfer
   folder is on another file system, by a copy that keeps the modification
   time, is checked against the record's sha256 and is renamed into place,
   the source removed only after both records changed;
4. rewrites the conversation entries first, the gateway's held guidance
   copies next and the gateway record last. A run that stops part way is
   finished by the next one: a destination that already holds the file (same
   sha256) counts as ``already_moved`` without needing the transfer folder,
   and every record is rewritten again.

Whether the transfer folder is here is decided only from the folder itself,
never from a file an earlier run already moved to its destination: it is here
when it opens and holds one of the waiting files, or when its device and inode
are the ones the progress record names
(``InstanceLayout.adapter_host_files_upgrade_progress_path``). A run that will
change anything writes that record first, once the folder opened (by its files,
by the recorded identity, or, with ``--files-gone``, merely present), so a run
that stopped after moving every file still in the folder is finished by the
next one, which finds only absent files left. When records wait for files and
the folder is not here (it is not there, or a mount point, a folder the 0.10
adapter host created or another folder holds none of them and is not the
recorded one), nothing changes and no marker is written: one error says to
copy or mount the folder and restart, or, if the files are really gone, to run the
command with ``--files-gone``, and, when the record names another identity,
logs both. Startup goes on either way, and the next start tries again. A
``--files-gone`` run whose folder is not there writes no record and needs
none; one that stops part way leaves the next normal start waiting, and a
second ``--files-gone`` run finishes. Device numbers are not stable on every
file system (NFS, CIFS, FUSE, 9p, drvfs and overlay assign them at mount
time): a folder mounted again that still holds a waiting file is here by its
files and the record is rewritten; one that holds none waits until
``--files-gone``, which is safe then, because every file it held was moved and
its records point at the destination. The log calls ``--files-gone`` safe only
when the device number changed: the same device with another inode is most
likely another folder on the same disk (the earlier one moved or renamed and an
empty one made at its path), so the log says to put the earlier folder back if
so. It stays tentative because a file system that makes up inode numbers at
mount time can give the same folder mounted again its old device number. Device and inode
rather than a token file in the folder: the transfer folder belongs to the
adapter host and may be read-only, and an identity that does not match at the
start only ever leads to waiting; one found by the re-check before the drops
stops the run with nothing dropped (``FolderProblem``, ``folder_changed``). A
stored name that cannot be looked up (a NUL byte, too long a name) is no sign
either way.

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
   (``event=adapter_host_file_not_moved`` with the transfer-folder files its
   records name (``sources``), the folder, the file, the attachment id,
   ``records=kept`` and the reason) reports it, and it never
   stops startup. The file tools refuse a record outside ``state/uploads``
   (``outside_upload_folders``), so the file cannot be opened until it moves:
   fix the file, delete the marker and restart, and the step tries the
   records it kept again (moved files are no longer found there, so nothing
   moves twice).
2. An error that concerns a folder (the transfer folder or ``state/uploads``
   not there, no permission to read or search a folder, a read-only or full
   ``state/uploads`` or a transfer folder the move cannot remove names from,
   a folder sync that fails, a progress record or marker in
   ``state/upgrades`` that cannot be read or written) changes nothing
   further: every record not yet finished stays and no marker is written. A
   transfer folder that is not here waits, as above; any other folder error
   raises ``FolderProblem``, whose message names the folder and, when one
   file was being worked on, that file and its attachment id; the launcher
   stops startup, and a restart after the fix finishes the work. So does any
   other unexpected error (a database error). When the transfer folder
   vanished during the run the message also names ``--files-gone``, the one
   cause that option changes; an unreadable folder or an I/O error does not.
   Before any record is dropped the transfer folder is opened again and must
   have the identity the run decided on: every look opens it by path, so a
   folder unmounted or replaced meanwhile would look empty, and a changed
   folder is a folder error that drops nothing. A progress record that does
   not parse is logged (``event=adapter_host_files_progress_unreadable``) and
   treated as absent, so the step waits rather than trust it. A sync the file
   system does not offer (``EINVAL`` or ``EOPNOTSUPP``, as some FUSE, 9p and
   shared-folder mounts answer) is no failure: it is logged once per folder
   (``event=adapter_host_files_sync_unsupported``) and the step goes on.
3. A record is dropped only when its file moved and both records were
   rewritten, when the file is proven absent from a transfer folder that is
   here (above), or with ``--files-gone`` (a transfer folder that is not here then
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

After the moves and drops, and before the marker, the step counts the files
left in the transfer folder that no record it found names
(``files_without_records`` in the counts, the marker and
``event=adapter_host_files_upgraded``): files of people removed on 0.9, files
whose records were deleted while the step waited, and copies of messages the
gateway never handled (F16; the owner chose reporting them over deleting them
or leaving them unmentioned). Only regular files named ``adapter_upload_*``
directly in the folder count, the names 0.9.13 wrote (an operator's folder may
hold other files); each is looked at with ``lstat`` only, so no link is
followed and no file is opened, and a file a record names is left out (a file
that did not move has its own warning). So is a file the 0.10 gateway reads
at its start: one of a hosted-chat message still ``accepted`` in the adapter
host's inbox, 0.9.13's included, whose file the gateway resolves, reads and
then removes (``_Step._files_the_gateway_reads``). The adapter host's records
are read only when a file no record names is found, because opening them
creates their tables on an instance that never ran it. A nonzero count logs
one warning (``event=adapter_host_files_without_records``) with the folder,
the count, the names and what an administrator may do (F6 of the review of
PR #150; the owner chose naming them). The gateway of 0.9.13 and 0.10 names
every transfer file ``adapter_upload_`` and 12 lowercase hex digits
(``new_id``), so those names hold no one's text: only names of exactly that
form are shown (``_GATEWAY_NAME``), and any other counted name, which
something else put there and which may be text someone chose, is only
counted (``unlisted=``).
The run that writes the marker writes those names, sorted and one per line,
to ``InstanceLayout.adapter_host_files_without_records_path`` just before the
marker; the warning shows the first ten, then says to keep them when the
folder was at another path under 0.9.13 and no record shows it (below), and
only then gives a command that deletes exactly the listed files. A list is
valid only beside the marker of the run that wrote it: every later run that
does not find the marker removes it, under the step's lock and before it
reads any record, and a run with nothing to list removes it too. Removing it
can fail while the marker is still written, so the marker holds a random id
for the list its run wrote
(``without_records_list_id``, null without one), and the command deletes
nothing, saying so, unless the marker holds its list's id (F1 of PR #154's
review). A failure writing or removing the list is logged
(``event=adapter_host_files_without_records_list_failed``) and changes
nothing else. The team cannot open those files, but the 0.10 adapter host
passes new files through the same folder under the same names, so a list
taken while the team runs (the operator's command) may name its copies. The
advice is therefore to delete them only after the gateway has started on 0.10
since the marker was written (it has then handled the messages taken in
before), and only while the instance is stopped, letting the team run a few
minutes after that start first: a remote adapter host keeps its registration
epoch until its message arrives or a request fails (a heartbeat renews even
an expired lease), so an upload it finished before the list can still be
used just after the start. A folder that is not there
counts 0; one gone or replaced since the step first opened it (its device and
inode differ), any other error listing it and a database error reading the adapter host's
records log ``event=adapter_host_files_without_records_uncounted`` and leave
the count unset: the count only reports, so it never stops startup, keeps the
marker from being written or changes a record. Only the run that writes the
marker reports, so the count is said once; a run that stops part way never
counts, and the run that finishes it does.

A transfer folder that was at another path under 0.9.13 (moved, mounted
elsewhere or reached through another link) makes the list wrong: its records
name that path and match nothing here, so every file people sent looks
unrecorded (F7 of PR #154's review). The step finds this from the records
themselves: before it writes the list, it collects the gateway's names
(``_GATEWAY_NAME``, the same 12-hex rule) of record paths in another folder,
from conversation turns and held guidance copies (noted by ``find``'s own
scans) and from gateway records (``list_attachment_paths_containing``, one
paged scan not limited to the transfer folder, run only when the list has a
name). When any listed name is among them (``NamedElsewhere``), the whole list
is unsafe: one match shows the folder moved, and every other listed file is
equally suspect, its records perhaps cleaned up meanwhile. So the run writes
no list and no list id, gives no command (an earlier warning's command
refuses), never drops only the matched names (a partial list would look
safe), and logs one error instead of the warning
(``event=adapter_host_files_folder_moved``, with counts per kind of record).
Error, not warning: it is said only once, because the run still writes its
marker (the owner's choice of 2026-11-20: startup never waits on it and the
step does not repeat it at every start), and people's files stay where the
team cannot open them until an administrator acts. The marker keeps the count
(``named_elsewhere``), the one trace once the log is gone. The error says
what to do: the step moves a file only with its gateway record, so for the
files a gateway record names, move or mount the folder back at the path those
records name (one at a time when they name several), set ``upload_dir`` to it,
delete the marker and restart; for the others, keep them. Agent processes
write conversation turns, so a turn's folder may withhold the command but is
never kept, logged or shown; only folders from gateway records and held
guidance copies are named. A record in another folder whose name is not
listed changes nothing: its file is not here, so it says nothing about this
folder, and the listed files are named by no record anywhere. When the check
cannot finish (a database error), nothing is listed either and
``event=adapter_host_files_without_records_uncounted`` says how to check
again. The detection opens no file, follows no link and drops no record. It
finds nothing when no record anywhere names a listed file, for which the
warning keeps its sentence before the command.

Guidance the gateway still holds (``gateway_held_guidance``) is rewritten
with its moved file: the gateway signs it again from that copy at each send,
so the team lead receives the new path. A held copy whose file did not move
is left as it is and named in the not-moved warning (``held_guidance_ids``);
it arrives without the file, which the team lead logs, and the stuck-task
check does not see it. Messages still queued are not rewritten: the
message-bound context id hashes every field of each file record, path
included (``access_control.ownership``), so a rewritten record would no
longer match what the gateway signed. Task records an agent already admitted
are not rewritten either, for another reason: the signature was checked once,
at admission, but those records are the agent's saved work (its runtime
snapshot), which only the agent holding it writes, under custody
(``storage.runtime_custody``). The file tools refuse those
records as outside ``state/uploads``, and the team lead's stuck-task check
tells the person which files to send again (``custom_agents.stuck_tasks``,
``task_files_sent_before_upgrade``).

It lives in ``instance_upgrades``, not ``assistant_gateway``, because it
changes the team's conversation store, which the gateway runtime never
imports. It goes with the other 0.9 upgrade steps when 0.11 ships.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import stat
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from pydantic import ValidationError

from access_control import safe_open
from assistant_gateway.adapter_host.domain import AdapterInboxRecord
from assistant_gateway.adapter_host.repository import AdapterHostRepository
from assistant_gateway.contracts.adapter_host_v1 import (
    AdapterHostContractError,
    ChannelInboundMessageV1,
    decode_adapter_host_message_v1,
)
from assistant_gateway.domain.delivery import AttachmentRecord
from assistant_gateway.inbound_attachments import safe_attachment_session_dir_name, sanitize_attachment_filename
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.task_v1 import TaskSteering
from id_generation import RANDOM_ID_SUFFIX_LENGTH, new_id_suffix
from storage.adapter_host_repository import create_adapter_host_repository
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

# Kept records that no retry can move (F6): what their warning says instead of "fix the file and restart".
_NO_RETRY_REASONS = {
    REASON_NO_GATEWAY_RECORD: (
        "no gateway record says which chat sent it, so it cannot be moved and trying again does not help; its "
        "conversation records were kept, the team cannot open it, and the person can send it again"
    ),
    REASON_RECORD_MISMATCH: (
        "these conversation records name another path or sha256 than the gateway's record, so they cannot "
        "follow the moved file and trying again does not help; they were kept, the team cannot open the file "
        "from them, and the person can send it again"
    ),
}

# Added to the not-moved warning when held guidance names the file (its copies are never changed).
_HELD_GUIDANCE_NOTE = (
    " Guidance the gateway holds names it too: that guidance is sent without the file, and the person can send "
    "it again."
)

# The operator's command that declares the waiting files gone (rule 3), named in messages.
FILES_GONE_COMMAND = "assistant_cli upgrade adapter-host-files --config <instance_config.yaml> --files-gone"
# Why a transfer folder that opened is not the one the run decided on (C06).
REASON_FOLDER_CHANGED = "folder_changed"
# Why counted files are left uncounted: the check for records naming them in another folder failed (C06 of F7).
REASON_OTHER_FOLDERS_UNCHECKED = "other_folders_unchecked"

# What 0.9.13's adapter host named each file it kept, directly in its transfer folder
# (``new_id("adapter_upload")`` in ``assistant_gateway.adapter_host.service``).
TRANSFER_FILE_PREFIX = "adapter_upload_"
# The names the gateway itself gives those files: the prefix and exactly the lowercase hex digits ``new_id``
# adds (``RANDOM_ID_SUFFIX_LENGTH``, the same from 0.9.4 to 0.10), matched in full. Only these are listed and
# named (F6 of PR #150's review): any other name an operator's folder holds may be text someone chose, and so
# may a longer hex run (SEC-1 of PR #154's review). The step handles only files 0.9.13 or earlier wrote, so no
# other length needs listing. A hex-only name also has no space, quote, newline or undecodable byte, so a list
# of one name per line is safe for ``xargs`` and always UTF-8.
_GATEWAY_NAME = re.compile(re.escape(TRANSFER_FILE_PREFIX) + f"[0-9a-f]{{{RANDOM_ID_SUFFIX_LENGTH}}}")
# How many of those names the files-without-records warning shows; the list file holds them all, so the log
# line stays bounded.
_NAMED_IN_WARNING = 10
# The marker's field holding the id of the list its run wrote (``None`` when it wrote none). The delete
# command checks the marker for it, so only the marker written with a list lets that list be used (F1 of
# PR #154's review).
LIST_ID_FIELD = "without_records_list_id"
# The marker's field counting the listed names records place in another folder (``NamedElsewhere.names``; None
# when the files were not counted). Above 0 it is the one trace on the instance that the step found the transfer
# folder at another path once its error has left the log (C07 of F7 of PR #154's review); deleting the marker
# and restarting runs the step and logs that error again.
NAMED_ELSEWHERE_FIELD = "named_elsewhere"
# What the delete command prints, with a nonzero status, when the marker is not the one written with its list
# (C07 of F6): ``rm -f`` prints nothing when it deletes, so a silent refusal would look the same as a delete.
DELETE_REFUSED = (
    "Nothing deleted: the marker this command checks is missing or was not written with this list, so the list "
    "must not be used."
)
# How a hosted-chat message names a file in the transfer folder (``AdapterAttachmentV1.storage_reference``).
_UPLOAD_REFERENCE_SCHEME = "adapter-upload"
# The upload status a message's file must have for the gateway to read it (``resolve_upload_reference``).
_UPLOAD_COMPLETED = "completed"

# Errors that come from one record's names and recur at every start (rule 1).
_NAME_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.EINVAL})
# Errors writing into state/uploads that every file would meet: a folder error (rule 2).
_DESTINATION_FOLDER_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EDQUOT})
# Answers of a file system that offers no fsync (some FUSE, 9p and shared-folder mounts): not a failure.
_SYNC_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.EOPNOTSUPP})
# Answers of a file system that offers no file locks (some network state folders): the step goes on, warned.
_LOCK_UNSUPPORTED_ERRNOS = frozenset({errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOTSUP})
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
_PROGRESS_LIMIT = 64 * 1024  # the progress record is a few lines


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
    # Files left in the transfer folder that no record the step found names (F16); None when not counted
    # (the step was already done or waits, or the folder could not be listed), 0 when counted and none.
    files_without_records: Optional[int] = None

    @property
    def not_moved(self) -> int:
        return self.missing + self.refused + self.no_record


@dataclass(frozen=True)
class NamedElsewhere:
    """Which listed names records place in another folder: the transfer folder was most likely at another path under 0.9.13 (F7 of PR #154's review).

    ``names`` counts the listed names any record names in another folder,
    and each ``by_`` field those one kind of record names (a name may count
    in several). ``folders`` holds the other folders from gateway records and
    held guidance copies only, sorted: agent processes write conversation
    turns, so a turn's folder is never kept or shown (A4).
    ``gateway_folders`` holds those from gateway records alone, sorted: a run
    moves a file only with its gateway record, so these are the paths the
    move-back steps name, one per run (C08).
    """

    names: int = 0
    by_gateway: int = 0
    by_held_guidance: int = 0
    by_turns: int = 0
    folders: tuple[Path, ...] = ()
    gateway_folders: tuple[Path, ...] = ()

    @property
    def without_gateway_record(self) -> int:
        """Names no gateway record places in another folder: only turns or held guidance do, so no run can move their files (C05).

        The step moves a file only with its gateway record
        (``REASON_NO_GATEWAY_RECORD``, ``event=adapter_host_files_held_only``),
        so putting the folder back at the other path moves only the
        ``by_gateway`` files.
        """

        return self.names - self.by_gateway


@dataclass(frozen=True)
class UpgradeResult:
    """``finished`` once the marker is written; ``folder_missing`` when records wait for a folder that holds none of their files.

    ``elsewhere`` says which of the files left that no record names here
    records name in another folder: when it names any, nothing was listed
    and no delete command given (``_report_folder_moved``).
    """

    finished: bool
    counts: UpgradeCounts
    waiting: int = 0
    already_done: bool = False
    elsewhere: NamedElsewhere = NamedElsewhere()

    @property
    def folder_missing(self) -> bool:
        return not self.finished and self.waiting > 0


@dataclass(frozen=True)
class _Leftovers:
    """The files left in the transfer folder that no record names (F16): those that may be named, and the rest.

    ``listed`` are the names of the gateway's form (``_GATEWAY_NAME``),
    sorted; ``unlisted`` counts the other names, which are never shown.
    ``elsewhere`` says which listed names records place in another folder.
    """

    listed: tuple[str, ...] = ()
    unlisted: int = 0
    elsewhere: NamedElsewhere = NamedElsewhere()

    @classmethod
    def of(cls, names: set[str]) -> _Leftovers:
        listed = tuple(sorted(name for name in names if _GATEWAY_NAME.fullmatch(name)))
        return cls(listed, len(names) - len(listed))

    @property
    def count(self) -> int:
        return len(self.listed) + self.unlisted

    @property
    def to_list(self) -> tuple[str, ...]:
        """The names the list file may hold: all listed names, or none once records name one of them in another folder.

        One such record means the folder moved, so every other listed file is
        just as likely a live one, including those whose turns were cleaned
        up: the matched names are never taken out to leave a list that looks
        safe (the third correction of PR #154's review critic, DI-2).
        """

        return () if self.elsewhere.names else self.listed


@dataclass(frozen=True)
class _WrittenList:
    """The list of files no record names this run wrote, and the random id its marker holds for it (F1 of PR #154's review)."""

    path: Path
    list_id: str


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


@dataclass(frozen=True)
class _HeldEntry:
    """One held guidance copy's record of a file in the transfer folder (``gateway_held_guidance``)."""

    correlation_id: str
    attachment: UserTaskAttachment
    path: Path


@dataclass
class _Item:
    """One attachment id with a file in the transfer folder: its gateway record, conversation entries and held guidance."""

    attachment_id: str
    record: Optional[AttachmentRecord] = None
    entries: list[_Entry] = field(default_factory=list)
    held: list[_HeldEntry] = field(default_factory=list)


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
    # Held guidance naming the file: never changed or dropped, only named in the warning.
    held: list[_HeldEntry] = field(default_factory=list)
    # The transfer-folder files these records name, for the warning's ``sources`` field. ``file`` may
    # name the state/uploads side (a destination taken, a temporary copy), and the delete advice of
    # the files-without-records warning must point at the transfer file the records keep (C05).
    sources: list[Path] = field(default_factory=list)


class FolderProblem(Exception):
    """A folder the step needs cannot be used (rule 2); nothing more changes, and a restart after the fix finishes the work.

    ``file`` is the file being worked on when the error came, and
    ``attachment_id`` its attachment (set by the caller that knows it).
    ``not_there`` says the folder itself is not there (``FileNotFoundError``),
    the one cause ``--files-gone`` changes; ``upgrade_adapter_host_files``
    sets ``files_gone_helps`` when that folder is the transfer folder, and
    the message then names ``--files-gone``.
    """

    def __init__(
        self, folder: Path, reason: str, detail: str, *, file: Optional[Path] = None, not_there: bool = False
    ) -> None:
        super().__init__(detail)
        self.folder = folder
        self.reason = reason
        self.detail = detail
        self.file = file
        self.not_there = not_there
        self.attachment_id: Optional[str] = None
        self.files_gone_helps = False

    def __str__(self) -> str:
        at = f", working on {self.file}" if self.file is not None else ""
        if self.attachment_id is not None:
            at += f" for attachment {self.attachment_id}"
        text = (
            f"The upgrade step for adapter-host files cannot use the folder {self.folder} ({self.detail}{at}). "
            "Fix the folder (mount it, its permissions, free space) and restart; nothing was lost."
        )
        if self.files_gone_helps:
            text += f" If the files are really gone, run '{FILES_GONE_COMMAND}'."
        return text


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
    leftover_list = Path(layout.adapter_host_files_without_records_path)
    if marker.is_file():
        return UpgradeResult(finished=True, counts=UpgradeCounts(), already_done=True)
    transfer = config.interactive_apps.adapter_host.resolved_upload_dir(config.instance_root_path)
    try:
        with _step_lock(Path(layout.adapter_host_files_upgrade_lock_path)):
            if marker.is_file():  # the run this one waited for finished the step
                return UpgradeResult(finished=True, counts=UpgradeCounts(), already_done=True)
            # An earlier run's list has no marker beside it, so it is not valid: gone before anything can
            # stop or wait (C01), and only the run that writes the marker writes a list again. This must stay
            # after the marker check above, inside the lock: a run that waited for one that finished must leave
            # that run's list, which its warning points at (C05 of F6).
            _remove_leftover_list(leftover_list)
            step = _Step(
                transfer=transfer,
                uploads=Path(layout.uploads_dir).resolve(),
                gateway=create_gateway_state_repository(config.state_storage_settings, logger=logger),
                # Opened only when a file no record names is found: opening it creates the adapter host's
                # tables, which an instance that never ran the adapter host does not have.
                adapter_host=lambda: create_adapter_host_repository(config.state_storage_settings, logger=logger),
                conversations=create_conversation_repository(
                    config.manager_storage_settings, artifacts_dir=layout.conversation_artifacts_dir, logger=logger
                ),
                marker=marker,
                progress=Path(layout.adapter_host_files_upgrade_progress_path),
                files_gone=files_gone,
            )
            items = step.find()
            waiting, here = step.survey(items)
            if waiting and not here and not files_gone:
                step.report_waiting(waiting)
                return UpgradeResult(finished=False, counts=UpgradeCounts(), waiting=waiting)
            step.start()
            for item in items:
                step.handle(item)
            step.forget_unmovable()
            leftovers = step.files_without_records()
            step.counts.files_without_records = None if leftovers is None else leftovers.count
            written = _finish(marker, leftover_list, step.counts, leftovers)
    except FolderProblem as problem:
        problem.files_gone_helps = problem.not_there and problem.folder == transfer
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
        "turns_marked_unrecorded=%s files_without_records=%s",
        counts.moved,
        counts.already_moved,
        counts.missing,
        counts.refused,
        counts.no_record,
        counts.gateway_rows_removed,
        counts.turns_marked_unrecorded,
        "-" if counts.files_without_records is None else counts.files_without_records,
    )
    elsewhere = leftovers.elsewhere if leftovers is not None else NamedElsewhere()
    if leftovers is not None and elsewhere.names:
        _report_folder_moved(transfer, marker, leftovers)
    elif leftovers is not None and leftovers.count:
        _report_files_without_records(transfer, marker, written, leftovers)
    return UpgradeResult(finished=True, counts=counts, elsewhere=elsewhere)


def _finish(
    marker: Path, leftover_list: Path, counts: UpgradeCounts, leftovers: Optional[_Leftovers]
) -> Optional[_WrittenList]:
    """Write the list of the files no record names, or settle that there is none, then the marker.

    Returns this run's list, or None when ``leftover_list`` does not hold
    one. The list goes first: a run stopped between the two leaves a list
    without a marker, which the next run removes at its start, while a list
    written after the marker could be missing or stale beside it with no
    later run to correct it. A marker that cannot be written
    (``FolderProblem``) takes the list just written with it, so no list is
    left without its marker.

    The marker holds a new random id for the list (``LIST_ID_FIELD``, None
    without a list), which the delete command checks. Removing a list a
    later run finds can fail (a bind-mounted file answers ``EBUSY``, an
    immutable one ``EPERM``) while the marker is still written, so a marker
    beside a list does not prove that its run wrote that list; without the
    id a command copied from the earlier run's warning would delete from a
    list the later run did not write, including a file the later run says
    to keep (F1 of PR #154's review). The review's probe showed that
    checking the list's sha256 or giving each run its own list name does
    not stop that: the stale list and its name are unchanged.

    The list holds ``_Leftovers.to_list``: nothing when records name a
    listed file in another folder (F7 of that review), so neither a list
    nor an id is left for any command to use. The number of listed names
    records place in another folder goes beside the id
    (``NAMED_ELSEWHERE_FIELD``), so the instance keeps that finding after
    the log is gone.
    """

    listed = _settle_leftover_list(leftover_list, leftovers.to_list if leftovers is not None else ())
    written = _WrittenList(leftover_list, new_id_suffix()) if listed else None
    body = {
        "finished_at_utc": _now(),
        **asdict(counts),
        LIST_ID_FIELD: written.list_id if written else None,
        NAMED_ELSEWHERE_FIELD: leftovers.elsewhere.names if leftovers is not None else None,
    }
    try:
        _write_state_file(marker, body)
    except FolderProblem:
        if written:
            _remove_leftover_list(leftover_list)
        raise
    return written


def _settle_leftover_list(path: Path, names: tuple[str, ...]) -> bool:
    """Make ``path`` hold ``names``, one per line, or nothing when there are none; whether it holds them.

    The list only reports (A3 of F6): an ``OSError`` is logged
    (``event=adapter_host_files_without_records_list_failed``), never
    raised, so it never stops the start or keeps the marker from being
    written. A list that could not be written leaves no earlier one behind
    where it can be removed.
    """

    if names:
        try:
            _replace_file(path, "".join(f"{name}\n" for name in names))
            return True
        except OSError as exc:
            _log_list_failed(path, "written", exc)
    _remove_leftover_list(path)
    return False


def _remove_leftover_list(path: Path) -> None:
    """Remove a list of the files no record names that has no marker of its run beside it; logged, never raised (C01)."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log_list_failed(path, "removed", exc)
        return
    logger.info(
        "A list of files no record names that has no marker of the run that wrote it beside it is not valid, so it "
        "was removed. event=adapter_host_files_without_records_list_removed list=%s",
        path,
    )


def _log_list_failed(path: Path, action: str, exc: OSError) -> None:
    logger.warning(
        "The list of files left in the adapter host's transfer folder that no record names could not be %s; a list "
        "still at that path is not this run's, so do not use it. Startup goes on. "
        "event=adapter_host_files_without_records_list_failed list=%s reason=%s",
        action,
        path,
        _detail(exc),
    )


def _report_files_without_records(
    transfer: Path, marker: Path, written: Optional[_WrittenList], leftovers: _Leftovers
) -> None:
    """Say once, from the run that wrote the marker, which files no record names are left and what to do (F16).

    The names come from ``_Leftovers``: only the gateway's own names
    (``new_id("adapter_upload")``, ``_GATEWAY_NAME``) are shown, never one
    of another form, which may be text someone chose and is only counted
    (F6 of the review of PR #150). Never contents. The warning shows the
    first ``_NAMED_IN_WARNING`` names and points at the list this run
    ``written``, which holds them all, with a command that deletes exactly
    those (``_delete_command``); ``written`` is None when the list could not
    be written, and then only the names shown may go. With no name of the
    gateway's form there is no delete advice at all, and the warning gives
    none of the causes it gives for the gateway's names (files of people
    removed on 0.9 and the rest), which cannot have produced such names.

    The advice stays bounded by the gateway (F2 of that review): at its
    start the gateway reads the files of messages it took in but had not
    handled yet, 0.9's included, and then removes them. The list leaves
    those out (``_files_the_gateway_reads``), but a list taken while the
    team runs (the operator's command, with or without ``--files-gone``)
    may name the 0.10 adapter host's copy of an upload whose message comes
    later, so the files may go only once the gateway has started on 0.10
    since the marker was written, and while the instance is stopped.

    When the folder was at another path under 0.9.13, its records name that
    path and match nothing here, so every file looks unrecorded. When a
    record names a listed file in another folder, the step finds that and
    this warning is not given (``_report_folder_moved`` instead, with no list
    and no command, F7 of PR #154's review). The warning keeps its sentence
    for the case no record shows (for example, the records of every listed
    file were deleted since): it says to keep the files then and, because
    the step reads only ``upload_dir`` (resolved through links), to set
    ``upload_dir`` to that path with the folder there (F1 of that review).
    """

    # The causes are those of the gateway's names; with none of those, they would not apply (C08 of F6).
    causes = (
        ": files of people removed on 0.9, files whose records were deleted, or files of messages the gateway "
        "never handled. The team cannot open them."
        if leftovers.listed
        else "."
    )
    logger.warning(
        "%s file(s) are left in the adapter host's transfer folder that no record names%s%s%s See upgrading.md. "
        "event=adapter_host_files_without_records folder=%s files_without_records=%s unlisted=%s list=%s",
        leftovers.count,
        causes,
        _listed_advice(transfer, marker, written, leftovers.listed),
        _unlisted_note(leftovers),
        transfer,
        leftovers.count,
        leftovers.unlisted,
        written.path if written else "-",
    )


def _listed_advice(transfer: Path, marker: Path, written: Optional[_WrittenList], listed: tuple[str, ...]) -> str:
    """The names the warning shows, where the rest are, and which of them an administrator may delete, and when."""

    if not listed:
        return ""
    shown = ", ".join(listed[:_NAMED_IN_WARNING])
    if len(listed) > _NAMED_IN_WARNING:
        shown += f" and {len(listed) - _NAMED_IN_WARNING} more"
    if written is not None:
        where = f" Listed in {written.path} ({len(listed)} file(s), one name per line): {shown}."
        what = f"exactly the listed files ({_delete_command(marker, transfer, written)})"
        names = "The list names"
    else:
        where = (
            f" The {len(listed)} file(s) with names the gateway gives: {shown}. The full list could not be written (see "
            "event=adapter_host_files_without_records_list_failed); after fixing state/upgrades, deleting the "
            "marker and restarting lists them all."
        )
        what = "exactly the files named here"
        names = "This warning names"
    # A folder at another path under 0.9.13 makes the list wrong. When a record names a listed file in another
    # folder the step withholds the list and the command (``_report_folder_moved``, F7 of PR #154's review); this
    # sentence stays for the case no record shows, and it comes before the command (C10 of F6): the command deletes
    # on one paste, and a person who acts on it before reading the whole line must already have been told when not
    # to. With no match this text is exactly as before F7 (A5). Both the names and the not-moved warning's sources= come
    # from the same records, so no listed file is one a record keeps (C08 of F6): the administrator removes nothing
    # from the list. A remote adapter host keeps its registration epoch across a short restart until its message
    # arrives or a request fails (a heartbeat renews even an expired lease), so an upload it finished before the
    # list can still resolve after the restart: hence the few minutes (GEN-1 and CRIT-1 of PR #154's review).
    return (
        f"{where} If this folder was at another path under 0.9.13 (moved, mounted elsewhere or reached through "
        "another link), the records of these files may name that path: do not delete them; move or mount the "
        "folder back at that path (a link does not work), set upload_dir to that path, delete the marker and "
        f"restart. Otherwise an administrator may delete {what} only while the instance is stopped and only after "
        f"the gateway has started on 0.10 since the step's marker {marker} was written: at its start the gateway "
        "handles messages taken in but not handled yet (0.9's too) and removes their files, and the 0.10 adapter "
        "host passes new files through this folder, so a list taken while the team runs (the operator's command, "
        "with or without --files-gone) may name one a message will still use: after such a run, restart the "
        "instance once before deleting. Before stopping the instance to delete, let the team run a few minutes "
        "after the gateway's start, so that messages a remote adapter host is still sending arrive. "
        f"{names} no file from the sources= field of an adapter_host_file_not_moved warning, and those must be "
        "kept. With the adapter host turned off, keep the files until it has run again."
    )


def _unlisted_note(leftovers: _Leftovers) -> str:
    """Why the counted names of another form are not shown, and that no advice covers them (C02, C08 of F6)."""

    if not leftovers.unlisted:
        return ""
    scope = "this advice does not cover them" if leftovers.listed else "the step gives no advice for them"
    return (
        f" Not listed: {leftovers.unlisted} file(s) whose names are not ones the gateway gives "
        f"({TRANSFER_FILE_PREFIX} and {RANDOM_ID_SUFFIX_LENGTH} lowercase hex digits), so something else put them "
        f"there and their names may be text someone chose; {scope}."
    )


def _report_folder_moved(transfer: Path, marker: Path, leftovers: _Leftovers) -> None:
    """Say once, instead of the files-without-records warning, that records name listed files in another folder (F7 of PR #154's review).

    The folder was then most likely at another path under 0.9.13, so the
    files are people's: no list and no delete command are given. Error
    rather than warning: the step reports it only once (the marker is
    written, the owner's choice of 2026-11-20 over waiting for the folder)
    and the team cannot open those files until an administrator acts.
    Folders come only from gateway records and held guidance copies (A4); no
    file is named. The advice follows what can move (``_folder_moved_advice``).
    """

    elsewhere = leftovers.elsewhere
    shown = [str(folder) for folder in elsewhere.folders[:_NAMED_IN_WARNING]]
    if not shown:
        where = (
            " (not shown: only conversation records name it, which agent processes write; it is the path "
            "upload_dir named under 0.9.13)"
        )
    else:
        where = f" ({_shown_folders(elsewhere.folders)})"
    logger.error(
        "%s of the %s file(s) left in the adapter host's transfer folder that no record of this folder names are "
        "named by records in another folder%s, so this folder was most likely at another path under 0.9.13 and "
        "they are files people sent. No list and no delete command are given: do not delete any of them. They "
        "were not moved into state/uploads, so the team cannot open them.%s%s See upgrading.md. "
        "event=adapter_host_files_folder_moved folder=%s other_folders=%s files_without_records=%s unlisted=%s "
        "named_elsewhere=%s by_gateway=%s by_held_guidance=%s by_turns=%s",
        elsewhere.names,
        leftovers.count,
        where,
        _folder_moved_advice(marker, elsewhere),
        _unlisted_note(leftovers),
        transfer,
        _listed(shown),
        leftovers.count,
        leftovers.unlisted,
        elsewhere.names,
        elsewhere.by_gateway,
        elsewhere.by_held_guidance,
        elsewhere.by_turns,
    )


def _folder_moved_advice(marker: Path, elsewhere: NamedElsewhere) -> str:
    """How the folder-moved error's files can still move: only those a gateway record names (C05 of F7 of PR #154's review).

    The step moves a file only with its gateway record, so putting the
    folder back at the other path moves the ``by_gateway`` files and no
    others; a file only a turn or held guidance names is reported there as
    not moved (``no_gateway_record``) or held only, and no run can move it.
    With no gateway record the error says so instead of giving steps that
    cannot succeed. The steps stand on their own, because an administrator
    may meet this first after ``--files-gone``, which they ran for another
    reason (C02). A file the step refused at that other path keeps records
    naming it, so a folder moved back from there is reported again for those
    files; the error says so instead of promising that the move back always
    finishes (C04). The steps name the gateway records' folder, not every
    folder the error shows (a held copy may name another, where nothing can
    move), and a run moves only the files whose gateway records name the
    path the folder is at: with several such folders the steps name them
    all and say to repeat them for each (C08).
    """

    keep = "keep them, and the people who sent them can send them again"
    if not elsewhere.by_gateway:
        return f" No gateway record names them, so the step cannot move them at any path: {keep}."
    paths = elsewhere.gateway_folders
    if len(paths) > 1:
        where = f"one of the {len(paths)} paths their gateway records name ({_shown_folders(paths)})"
        source = "one of those paths"
        repeat = (
            " and reports again the files whose gateway records name another of those paths, so do this for each "
            "of them"
        )
    else:
        where, source, repeat = str(paths[0]), "that path", ""
    advice = (
        f" To move the {elsewhere.by_gateway} a gateway record names: move or mount this folder back at {where} "
        f"(a link does not work), set upload_dir to that path, delete the marker {marker} and restart; the step "
        f"then runs again and moves the files a gateway record names at that path{repeat}. If this folder was just "
        f"moved here from {source}, these are most likely files the step could not move there (an "
        "adapter_host_file_not_moved warning of that run names each): fix them, then do the same."
    )
    if elsewhere.without_gateway_record:
        advice += (
            f" The other {elsewhere.without_gateway_record} have no gateway record, so no run can move them (that "
            f"run reports each as adapter_host_file_not_moved or adapter_host_files_held_only): {keep}."
        )
    return advice


def _shown_folders(folders: tuple[Path, ...]) -> str:
    """The first ``_NAMED_IN_WARNING`` of ``folders`` for the folder-moved error's text, then how many more."""

    shown = ", ".join(str(folder) for folder in folders[:_NAMED_IN_WARNING])
    more = len(folders) - _NAMED_IN_WARNING
    return f"{shown} and {more} more" if more > 0 else shown


def _delete_command(marker: Path, transfer: Path, written: _WrittenList) -> str:
    """The shell command that deletes exactly the listed files, and refuses unless the marker was written with them.

    A list is valid only beside the marker of the run that wrote it (C01 and
    C06 of F6): one whose marker was deleted, as the advice for a folder that
    was at another path asks, may name files whose records are live, and an
    earlier list a later run could not remove may name a file that run says
    to keep (F1 of PR #154's review). So the command first looks in the
    marker for the id this run stored for its list (``LIST_ID_FIELD``), with
    ``grep -qF`` (fixed text, in every ``grep``: GNU, BusyBox, macOS); a
    missing marker, another run's or one written without a list stops it
    before anything is deleted, and grep's own message says when the marker
    is missing or unreadable. The text looked for is the field as
    ``json.dumps`` writes it, which ``_write_state_file`` puts on one line.
    The refusal prints ``DELETE_REFUSED`` and exits nonzero (C07): ``rm -f``
    prints nothing when it deletes, and a person who pasted the command
    where the paths are not (a container's paths on its host) must see that
    nothing happened. Each path and the text are quoted for the shell (an
    instance root may hold a space); the names in the list are hex ids,
    which ``xargs`` reads as they are.
    """

    entry = json.dumps({LIST_ID_FIELD: written.list_id})[1:-1]  # '"without_records_list_id": "<id>"'
    return (
        f"if grep -qF -- {shlex.quote(entry)} {shlex.quote(str(marker))}; then cd {shlex.quote(str(transfer))} && "
        f"xargs rm -f -- < {shlex.quote(str(written.path))}; else echo {shlex.quote(DELETE_REFUSED)} >&2; false; fi"
    )


class _Step:
    def __init__(
        self,
        *,
        transfer: Path,
        uploads: Path,
        gateway: GatewayStateRepository,
        adapter_host: Callable[[], AdapterHostRepository],
        conversations: ConversationRepository,
        marker: Path,
        progress: Path,
        files_gone: bool = False,
    ) -> None:
        self._transfer = transfer
        self._uploads = uploads
        self._gateway = gateway
        self._adapter_host = adapter_host
        self._conversations = conversations
        self._marker = marker
        self._progress = progress
        # The transfer folder's (device, inode) when survey opened it, and what the progress record names.
        self._identity: Optional[tuple[int, int]] = None
        self._recorded: Optional[tuple[int, int]] = None
        # With --files-gone a transfer folder that is not here holds none of the files (rule 3).
        self._files_gone = files_gone
        self.counts = UpgradeCounts()
        self._unrecorded_turns: set[tuple[str, str]] = set()
        # Names in the transfer folder that a record ``find`` saw names, held-only ids included (F16).
        self._named: set[str] = set()
        # Names of the gateway's form that ``find`` saw records place in another folder (``_elsewhere``): from
        # turns the name only (A4), from held guidance copies with their folders.
        self._turn_names_elsewhere: set[str] = set()
        self._held_elsewhere: dict[str, set[Path]] = {}
        self._not_moved: list[_NotMoved] = []
        self._absent: dict[Path, bool] = {}
        self._sync_unsupported: set[Path] = set()

    # -- finding ---------------------------------------------------------------

    def find(self) -> list[_Item]:
        """Every attachment id with a gateway record or a conversation entry in the transfer folder.

        Held guidance naming such a file is added to its id. An id only held
        guidance names (no gateway record, no conversation entry) is no item:
        it would never move, so it neither waits nor moves, and is logged
        (``event=adapter_host_files_held_only``); that guidance arrives
        without the file, which the team lead logs.
        """

        items: dict[str, _Item] = {}
        after_id = ""
        while True:
            records = self._gateway.list_attachments_under(str(self._transfer), after_id=after_id, limit=_BATCH)
            for record in records:
                path = self._in_transfer(record.local_path)
                if path is not None:
                    self._named.add(path.name)
                    items.setdefault(record.attachment_id, _Item(record.attachment_id)).record = record
            if len(records) < _BATCH:
                break
            after_id = records[-1].attachment_id
        for entry in self._entries():
            self._named.add(entry.path.name)
            items.setdefault(entry.attachment.attachment_id, _Item(entry.attachment.attachment_id)).entries.append(entry)
        for held in self._held_entries():
            self._named.add(held.path.name)
            item = items.get(held.attachment.attachment_id)
            if item is not None:
                item.held.append(held)
                continue
            logger.warning(
                "Guidance the gateway holds names a file of a chat app the adapter host ran that no other record "
                "names, so it cannot be moved; the guidance is sent without it and the person can send it again. "
                "event=adapter_host_files_held_only attachment_id=%s correlation_id=%s file=%s",
                held.attachment.attachment_id,
                held.correlation_id,
                held.path,
            )
        return [items[key] for key in sorted(items)]

    def _held_entries(self) -> Iterator[_HeldEntry]:
        """Files in the transfer folder that guidance the gateway still holds names.

        Read whole: the table holds only guidance its running request has not
        confirmed yet. A copy that does not decode is one the gateway cannot
        send either; it is logged (``event=adapter_host_files_held_guidance_unreadable``)
        and left alone.
        """

        for held in self._gateway.list_held_guidance():
            try:
                attachments = TaskSteering.model_validate_json(held.steering_json).attachments
            except ValidationError:
                logger.warning(
                    "A held guidance copy could not be read, so its files are not moved with it. "
                    "event=adapter_host_files_held_guidance_unreadable correlation_id=%s",
                    held.correlation_id,
                )
                continue
            for attachment in attachments:
                raw = attachment.local_path()
                path = self._in_transfer(raw)
                if path is not None:
                    yield _HeldEntry(held.correlation_id, attachment, path)
                elif (elsewhere := self._elsewhere(raw)) is not None:
                    self._held_elsewhere.setdefault(elsewhere[1], set()).add(elsewhere[0])

    def _entries(self) -> Iterator[_Entry]:
        """Files in the transfer folder conversation turns name; notes the names turns place in another folder.

        Of a turn naming a file in another folder only the name is noted:
        agent processes write turns, so their paths are never kept or shown (A4).
        """

        after: Optional[tuple[str, str]] = None
        while True:
            turns = self._conversations.scan_turn_attachments(after=after, limit=_BATCH)
            for conversation_id, task_id, attachments in turns:
                for attachment in attachments:
                    raw = attachment.local_path()
                    path = self._in_transfer(raw)
                    if path is not None:
                        yield _Entry(conversation_id, task_id, attachment, path)
                    elif (elsewhere := self._elsewhere(raw)) is not None:
                        self._turn_names_elsewhere.add(elsewhere[1])
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
        if self._transfer_in_uploads or self._uploads == path:
            return None
        return path

    def _elsewhere(self, raw: Optional[str]) -> Optional[tuple[Path, str]]:
        """``(folder, name)`` when ``raw`` names a transfer file of the gateway's form in another folder, else None.

        Normalized like ``_in_transfer`` (no link followed, no file system
        call), so a path that normalizes into the transfer folder is a record
        of this folder, never of another. Only the gateway's own names count
        (``_GATEWAY_NAME``, the form the list holds, matched in full), so a
        name of another length or case, or with a suffix, withholds nothing;
        nor does a relative path, which names no folder.
        """

        if not raw or not os.path.isabs(raw):
            return None
        path = Path(os.path.normpath(raw))
        if path.parent == self._transfer or not _GATEWAY_NAME.fullmatch(path.name):
            return None
        return path.parent, path.name

    @property
    def _transfer_in_uploads(self) -> bool:
        """Whether an operator set the transfer folder to ``state/uploads`` or a folder inside it (EVI-2, U7).

        Files there are where agents open them: nothing moves, drops or
        counts them (C01).
        """

        return self._uploads == self._transfer or self._uploads in self._transfer.parents

    def files_without_records(self) -> Optional[_Leftovers]:
        """The files 0.9.13 could have written that are left in the transfer folder and no record names.

        Counted after the moves and drops: regular files named
        ``adapter_upload_*`` directly in the folder (the step's own scope,
        ``_in_transfer``), checked with ``lstat`` only, so no link is
        followed and no file is opened. Left out: a name a record ``find``
        saw names (a file that did not move is reported by its own warning),
        and a file the gateway reads at its start (``_files_the_gateway_reads``).
        A transfer folder that is not there and never was during the run
        holds none (0, most instances never ran the adapter host), and one
        set to or inside ``state/uploads`` leaves nothing (0). A folder that
        is gone or has another identity than ``survey`` saw is not counted:
        every look opens it by path, so a folder unmounted or replaced
        meanwhile would look empty (``REASON_FOLDER_CHANGED``). That, any
        other error listing it and a database error reading the adapter
        host's records are logged
        (``event=adapter_host_files_without_records_uncounted``) and give
        None: the count only reports, so it never stops the start or keeps
        the marker from being written (rule 2 does not apply). So does a
        database error checking for records that name the listed files in
        another folder (``_named_elsewhere``): until that check finishes it
        is not known whether records name them at all, and no list and so no
        command may be given (C03 of F7 of PR #154's review); its warning
        says the files were counted and how to check again (C06). Which of
        the names may be shown is ``_Leftovers.of``'s rule.
        """

        if self._transfer_in_uploads:
            return _Leftovers()
        unnamed = self._list_unnamed_files()
        if not unnamed:
            return None if unnamed is None else _Leftovers()
        try:
            unnamed -= self._files_the_gateway_reads()
        except Exception as exc:  # a database error: the count only reports (A2), so it is left unset, logged
            self._log_uncounted(f"{type(exc).__name__}: {exc}")
            return None
        leftovers = _Leftovers.of(unnamed)
        if not leftovers.listed:
            return leftovers
        try:
            return replace(leftovers, elsewhere=self._named_elsewhere(leftovers.listed))
        except Exception as exc:  # a database error: never a list without the check (C03)
            self._log_other_folders_unchecked(leftovers.count, f"{type(exc).__name__}: {exc}")
            return None

    def _named_elsewhere(self, listed: tuple[str, ...]) -> NamedElsewhere:
        """Which ``listed`` names records place in another folder (A1 of F7 of PR #154's review).

        Three kinds of record: conversation turns and held guidance copies,
        noted by ``find``'s own scans (``_entries``, ``_held_entries``), and
        the gateway's attachment records, read here in pages with a query not
        limited to the transfer folder (``list_attachment_paths_containing``)
        and kept only for listed names, so what is held stays bounded by the
        list. Each path is checked by ``_elsewhere``, so the gateway query's
        rows count only when they name a listed name exactly.
        """

        wanted = set(listed)
        gateway: dict[str, set[Path]] = {}
        after_id = ""
        while True:
            rows = self._gateway.list_attachment_paths_containing(
                f"/{TRANSFER_FILE_PREFIX}", after_id=after_id, limit=_BATCH
            )
            for _attachment_id, local_path in rows:
                elsewhere = self._elsewhere(local_path)
                if elsewhere is not None and elsewhere[1] in wanted:
                    gateway.setdefault(elsewhere[1], set()).add(elsewhere[0])
            if len(rows) < _BATCH:
                break
            after_id = rows[-1][0]
        held = {name: folders for name, folders in self._held_elsewhere.items() if name in wanted}
        turns = self._turn_names_elsewhere & wanted
        gateway_folders: set[Path] = set().union(*gateway.values())
        return NamedElsewhere(
            names=len(turns | held.keys() | gateway.keys()),
            by_gateway=len(gateway),
            by_held_guidance=len(held),
            by_turns=len(turns),
            folders=tuple(sorted(gateway_folders.union(*held.values()))),
            gateway_folders=tuple(sorted(gateway_folders)),
        )

    def _list_unnamed_files(self) -> Optional[set[str]]:
        """The folder's files no found record names; None, logged, when the folder cannot be listed or changed."""

        try:
            directory = safe_open.open_directory(self._transfer, ())
        except safe_open.UnsafePath as exc:
            if exc.reason == safe_open.REASON_ROOT_UNAVAILABLE and isinstance(exc.__cause__, FileNotFoundError):
                if self._identity is None:
                    return set()
                self._log_uncounted(f"{REASON_FOLDER_CHANGED}: {self._changed_detail(None)}")
                return None
            self._log_uncounted(_detail(exc.__cause__ or exc))
            return None
        except (OSError, ValueError) as exc:
            self._log_uncounted(_detail(exc))
            return None
        try:
            changed = self._changed_since_survey(directory)
            if changed is None:
                return self._unnamed_files(directory)
            self._log_uncounted(f"{REASON_FOLDER_CHANGED}: {changed}")
            return None
        except (OSError, ValueError) as exc:
            self._log_uncounted(_detail(exc))
            return None
        finally:
            os.close(directory)

    def _changed_since_survey(self, directory: int) -> Optional[str]:
        """What changed when the open folder is not the one ``survey`` opened, else None (FA-3 of PR #150's review)."""

        if self._identity is None:
            return None
        info = os.fstat(directory)
        now = (info.st_dev, info.st_ino)
        return None if now == self._identity else self._changed_detail(now)

    def _changed_detail(self, now: Optional[tuple[int, int]]) -> str:
        return "it changed during the run: %s at the start, %s now" % (_identity_text(self._identity), _identity_text(now))

    def _unnamed_files(self, directory: int) -> set[str]:
        """Regular ``adapter_upload_*`` files in the open folder no found record names; raises on a listing error."""

        unnamed: set[str] = set()
        for name in os.listdir(directory):
            if not name.startswith(TRANSFER_FILE_PREFIX) or name in self._named:
                continue
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue  # removed since the listing
            if stat.S_ISREG(info.st_mode):
                unnamed.add(name)
        return unnamed

    def _files_the_gateway_reads(self) -> set[str]:
        """Names in the transfer folder of files the 0.10 gateway reads, then removes, at its start (F2 of PR #150's review).

        At its start the gateway handles every hosted-chat message still
        ``accepted`` in ``gateway_adapter_inbox``, 0.9.13's included (the
        protocol and both tables are unchanged since then):
        ``AdapterHostApplicationService.start``, then
        ``GatewayAdapterRequestHandler._save_uploads``, which resolves each
        file reference in order (``resolve_upload_reference``: a completed
        upload of the same adapter and registration epoch), reads the files
        and removes the transfer copies of those it resolved. It stops at
        the first reference that does not resolve, so the files after it
        stay. A message in ``processing`` is marked ``outcome_unknown`` and
        never read again, and a completed upload no accepted message names
        never resolves after a restart (each registration gets a new epoch),
        so neither is left out. A payload that does not decode fails its
        message without reading a file. Read only once a file no record
        names is found, in pages (``list_inbox_for_recovery``).
        """

        repository = self._adapter_host()
        names: set[str] = set()
        after: Optional[tuple[str, str]] = None
        while True:
            records = repository.list_inbox_for_recovery(limit=_BATCH, after=after)
            for record in records:
                names.update(self._files_read_for(repository, record))
            if len(records) < _BATCH:
                return names
            after = (records[-1].created_at_utc, records[-1].request_id)

    def _files_read_for(self, repository: AdapterHostRepository, record: AdapterInboxRecord) -> Iterator[str]:
        """The transfer-folder names of the files the gateway resolves for one accepted message, in its order."""

        try:
            message = decode_adapter_host_message_v1(json.loads(record.payload_json))
        except (AdapterHostContractError, ValueError, TypeError, AttributeError):
            return  # the gateway fails it without reading a file
        if not isinstance(message, ChannelInboundMessageV1):
            return
        for attachment in message.attachments:
            scheme, _, upload_id = attachment.storage_reference.partition(":")
            upload = repository.get_upload(upload_id) if scheme == _UPLOAD_REFERENCE_SCHEME and upload_id else None
            if upload is None or (
                upload.status,
                upload.plugin_id,
                upload.adapter_instance_id,
                upload.registration_epoch,
            ) != (_UPLOAD_COMPLETED, message.plugin_id, message.adapter_instance_id, message.session_epoch):
                return
            path = self._in_transfer(upload.local_path)
            if path is not None:
                yield path.name

    def _log_uncounted(self, reason: str) -> None:
        logger.warning(
            "The files left in the adapter host's transfer folder could not be counted, so it is not known whether "
            "any are left that no record names; counting them changed nothing and startup goes on. "
            "event=adapter_host_files_without_records_uncounted folder=%s reason=%s",
            self._transfer,
            reason,
        )

    def _log_other_folders_unchecked(self, count: int, detail: str) -> None:
        """The uncounted warning when only the check for records in another folder failed (C06 of F7 of PR #154's review).

        The files were counted, so the warning says so and why nothing is
        listed, and how to run the check again: the marker is written, so
        the step runs again only once it is deleted. The same event as the
        other causes, because the marker's count is unset in both.
        """

        logger.warning(
            "%s file(s) are left in the adapter host's transfer folder that no record of this folder names, but the "
            "check for records naming them in another folder did not finish, so it is not known whether this folder "
            "was at another path under 0.9.13 and they are files people sent. No list and no delete command are "
            "given: do not delete any of them. To check again, delete the marker %s and restart; the step then runs "
            "again. Startup goes on. event=adapter_host_files_without_records_uncounted folder=%s reason=%s: %s",
            count,
            self._marker,
            self._transfer,
            REASON_OTHER_FOLDERS_UNCHECKED,
            detail,
        )

    def survey(self, items: list[_Item]) -> tuple[int, bool]:
        """How many attachment ids wait for files in the transfer folder, and whether that folder is here.

        Decided only from the transfer folder itself, never from a file an
        earlier run already moved to its destination: the folder is here when
        it opens and holds one of the waiting files, or when its device and
        inode are the ones the progress record names (an earlier run started
        on this folder, so a file it lacks is proven absent). Otherwise it is
        not there, or is not the one 0.9.13 wrote (an empty mount point, a
        folder the 0.10 adapter host created, a folder mounted again with a
        new device number), and the caller changes nothing. A name that
        cannot be looked up says nothing about the folder. A folder or record
        that answers with another error raises ``FolderProblem``.
        """

        if not items:
            return 0, False
        self._identity = self._folder_identity(absent_is_missing=True)
        self._recorded = self._read_progress()
        if self._identity is None:
            return len(items), False
        if self._recorded == self._identity:
            return len(items), True
        for item in items:
            with _naming(item.attachment_id):
                if self._holds_one(item):
                    return len(items), True
        return len(items), False

    def _holds_one(self, item: _Item) -> bool:
        """Whether the transfer folder holds a file one of ``item``'s records names."""

        candidates = {entry.path for entry in item.entries}
        if item.record is not None:
            source = self._in_transfer(item.record.local_path)
            if source is not None:
                candidates.add(source)
        return any(
            _look(self._transfer, (path.name,), absent_is_missing=True) == _THERE
            for path in sorted(candidates)
        )

    def report_waiting(self, waiting: int) -> None:
        """Log why the step waits; when an earlier run started, both identities and the way out.

        The remount advice (``--files-gone`` is safe) is given only when the
        device number changed, the one case a remount usually explains. The
        same device with another inode is most likely another folder on the
        same disk, for example the earlier folder moved or renamed and an
        empty one created at its path (Docker's ``-v`` does that): the earlier
        folder may still hold the files, so the log says to put it back (C08).
        That sentence stays tentative because a file system that makes up
        inode numbers at mount time can give the same folder mounted again
        the same device number back and another inode (C12).
        """

        recorded = ""
        if self._recorded is not None:
            now = "not here" if self._identity is None else "device %s inode %s" % self._identity
            recorded = (
                " An earlier run started on the folder at device %s inode %s; it is now %s." % (*self._recorded, now)
            )
            if self._identity is not None and self._identity[0] != self._recorded[0]:
                recorded += (
                    " The device number changed: if it is the same folder mounted again (some file systems give it "
                    "a new device number at each mount), the files that run did not move are not in it, and "
                    "--files-gone finishes the step safely."
                )
            elif self._identity is not None:
                recorded += (
                    " It is probably another folder on the same disk (the earlier one moved or renamed): if so, "
                    "put it back where upload_dir points and restart; the files that run did not move are still "
                    "in it."
                )
        logger.error(
            "Files sent through chat apps the adapter host ran are recorded in the gateway's transfer folder, but "
            "the folder is not here, holds none of them, or is not the folder an earlier run started on, so they "
            "were not moved into state/uploads and nothing was changed. Copy or mount the folder 0.9's gateway "
            "wrote (interactive_apps.adapter_host.upload_dir) and restart; if the files are really gone, run '%s'.%s "
            "event=adapter_host_files_folder_missing folder=%s waiting=%s recorded=%s current=%s",
            FILES_GONE_COMMAND,
            recorded,
            self._transfer,
            waiting,
            _identity_text(self._recorded),
            _identity_text(self._identity),
        )

    def start(self) -> None:
        """Before the first change: record the transfer folder's identity, unless it already names it.

        A run that will change anything writes it when the transfer folder
        opened (here by its files or its recorded identity, or, with
        ``--files-gone``, merely present). A ``--files-gone`` run whose folder
        is not there writes none and needs none: ``--files-gone`` replaces the
        presence decision.
        """

        if self._identity is None or self._identity == self._recorded:
            return
        device, inode = self._identity
        _write_state_file(
            self._progress,
            {"transfer_folder": str(self._transfer), "device": device, "inode": inode, "started_at_utc": _now()},
        )
        self._recorded = self._identity

    def _folder_identity(self, *, absent_is_missing: bool) -> Optional[tuple[int, int]]:
        """The transfer folder's (device, inode) from the folder as it opens now; None when it is not there.

        Opened like every other look (``_open_folder``), so not there and the
        other folder errors follow the same rules.
        """

        descriptor = _open_folder(self._transfer, (), None, absent_is_missing=absent_is_missing)
        if descriptor is None:
            return None
        try:
            info = os.fstat(descriptor)
        except OSError as exc:
            raise FolderProblem(self._transfer, safe_open.REASON_OPEN_FAILED, _detail(exc)) from exc
        finally:
            os.close(descriptor)
        return (info.st_dev, info.st_ino)

    def _read_progress(self) -> Optional[tuple[int, int]]:
        """The (device, inode) the progress record names; None when there is none or it does not parse.

        A record that does not parse (edited by hand, a damaged disk) is
        logged and treated as absent: at worst the step waits and names
        ``--files-gone``; it never drops records on its strength. A record
        that cannot be read is a folder error on ``state/upgrades`` (rule 2).
        """

        try:
            descriptor = os.open(self._progress, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise FolderProblem(self._progress.parent, safe_open.REASON_OPEN_FAILED, _detail(exc), file=self._progress) from exc
        try:
            with os.fdopen(descriptor, "rb") as opened:
                raw = opened.read(_PROGRESS_LIMIT)
        except OSError as exc:
            raise FolderProblem(self._progress.parent, safe_open.REASON_OPEN_FAILED, _detail(exc), file=self._progress) from exc
        try:
            body = json.loads(raw)
            device, inode = body["device"], body["inode"]
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in (device, inode)):
                raise ValueError("device and inode must be numbers")
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "The upgrade step's progress record cannot be read, so it is ignored: the step waits rather than "
                "trust it. event=adapter_host_files_progress_unreadable file=%s reason=%s",
                self._progress,
                exc,
            )
            return None
        return (device, inode)

    def _check_folder_unchanged(self) -> None:
        """Before anything is dropped: the transfer folder must still be the one ``survey`` opened (C06).

        Every later look opens the folder by path again, so a folder unmounted
        or replaced meanwhile (a lazy unmount, a network mount dropping) would
        look empty. A folder no longer there, or with another identity, is a
        folder error: nothing is dropped, and the next run finishes the moves
        done (``already_moved``).
        """

        if self._identity is None or not any(n.drop_entries or n.drop_record for n in self._not_moved):
            return
        now = self._folder_identity(absent_is_missing=False)
        if now != self._identity:
            raise FolderProblem(self._transfer, REASON_FOLDER_CHANGED, self._changed_detail(now))

    # -- handling one file -------------------------------------------------------

    def handle(self, item: _Item) -> None:
        with _naming(item.attachment_id):
            record = item.record
            if record is None:
                unmovable = _Unmovable(OUTCOME_NO_RECORD, REASON_NO_GATEWAY_RECORD, self._transfer)
                self._keep_or_drop(item, unmovable, item.entries, item.held, record_path=None, absent_source=None)
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
                    item.held,
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
            held_with = [held for held in item.held if held.path == source and held.attachment.sha256 == record.sha256]
            held_others = [held for held in item.held if held not in held_with]
            new_path = str(self._uploads.joinpath(*destination))
            # Turns, then held guidance, then the gateway record: the gateway record still naming the
            # transfer folder is what makes the next run finish a run stopped between them.
            if moved_with:
                self._conversations.relocate_turn_attachment(
                    sorted({entry.turn for entry in moved_with}), item.attachment_id, local_path=new_path
                )
            if held_with:
                self._gateway.relocate_held_guidance_attachment(
                    sorted({held.correlation_id for held in held_with}), item.attachment_id, new_path
                )
            self._gateway.set_attachment_local_path(item.attachment_id, new_path)
            if remove_source:
                self._remove_source(source, item.attachment_id)
            if others or held_others:
                # Records naming another path or sha256 never follow the file (agent processes write turns).
                mismatch = _Unmovable(OUTCOME_REFUSED, REASON_RECORD_MISMATCH, self._transfer)
                self._keep_or_drop(item, mismatch, others, held_others, record_path=None, absent_source=None, counted=False)
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
        fits does not fail only because of the copy, and fixed per
        destination, so the next run removes a copy a stopped run left; two
        runs never share it because the step's lock (``_step_lock``) keeps
        runs from overlapping (CONC-4).
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
        held: list[_HeldEntry],
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
        dropped. ``held`` guidance copies are never dropped: the gateway
        releases them when the team lead accepts the guidance, which then
        arrives without the file (the lead logs it), so they are only named.
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
        named = [record_path, *(entry.path for entry in entries), *(copy.path for copy in held)]
        sources = sorted({path for path in named if path is not None})
        self._not_moved.append(
            _NotMoved(item, unmovable, drop_entries, kept_entries, drop_record, has_record and not drop_record, held, sources)
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

        self._check_folder_unchanged()
        for not_moved in self._not_moved:
            self._forget(not_moved)
        self._not_moved.clear()

    def _forget(self, not_moved: _NotMoved) -> None:
        item, unmovable = not_moved.item, not_moved.unmovable
        dropped = bool(not_moved.drop_entries) or not_moved.drop_record
        kept = bool(not_moved.kept_entries) or not_moved.kept_record
        records = RECORDS_PARTLY_DROPPED if dropped and kept else RECORDS_DROPPED if dropped else RECORDS_KEPT
        entries = not_moved.drop_entries + not_moved.kept_entries
        held = sorted({entry.correlation_id for entry in not_moved.held})
        file = unmovable.file or (entries[0].path if entries else not_moved.held[0].path if not_moved.held else None)
        if dropped and not kept:
            advice = "its records were dropped, so the person must send it again"
        elif not kept:  # only held guidance names it with another path or sha256 than the gateway's record
            advice = (
                "held guidance names it with another path or sha256 than the gateway's record, so that guidance "
                "cannot follow the moved file and trying again does not help"
            )
        elif unmovable.reason in _NO_RETRY_REASONS:
            advice = _NO_RETRY_REASONS[unmovable.reason]
        else:
            advice = (
                f"its records were kept, so it cannot be opened until it is moved: fix the file, delete "
                f"{self._marker} and restart to try it again"
            )
        logger.warning(
            "A file of a chat app the adapter host ran was not moved into state/uploads; %s%s.%s "
            "event=adapter_host_file_not_moved attachment_id=%s conversation_ids=%s held_guidance_ids=%s sources=%s "
            "folder=%s file=%s records=%s reason=%s",
            advice,
            f" ({unmovable.detail})" if unmovable.detail else "",
            _HELD_GUIDANCE_NOTE if held else "",
            item.attachment_id,
            _listed(sorted({entry.conversation_id for entry in entries})),
            _listed(held),
            _listed(str(path) for path in not_moved.sources),
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


def _listed(values: Iterable[str]) -> str:
    """``values`` as one log field: comma-separated, or ``-`` when there are none."""

    return ",".join(values) or "-"


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


def _open_folder(root: Path, parts: tuple[str, ...], file: Optional[Path], *, absent_is_missing: bool = False, create: bool = False) -> Optional[int]:
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
            not_there = isinstance(cause, FileNotFoundError)
            if absent_is_missing and not_there:
                return None
            raise FolderProblem(root, exc.reason, _detail(cause or exc), file=file, not_there=not_there) from exc
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


def _identity_text(identity: Optional[tuple[int, int]]) -> str:
    return "-" if identity is None else "device=%s,inode=%s" % identity


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _step_lock(path: Path) -> Iterator[None]:
    """Hold the step's lock (``flock`` on ``path``) for the whole run, so two runs never overlap (F13).

    The launcher's first-start run and an operator's ``--files-gone`` run
    (``docker exec``) can meet. A run that finds the lock held logs
    ``event=adapter_host_files_waiting_for_other_run`` and waits rather than
    refusing: the other run is about to finish, and refusing would stop
    startup or fail the operator's command. Closing the descriptor releases
    it, also when the process dies. Opening or creating the lock file is a
    folder error on ``state/upgrades`` (rule 2). A file system without locks
    (ENOLCK, EOPNOTSUPP on some network state folders) is logged
    (``event=adapter_host_files_lock_unsupported``) and the step goes on
    without the lock, as an unsupported sync does: overlapping runs need an
    operator's command beside the first start, while stopping would block
    every start on that disk with no way out.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise FolderProblem(path.parent, safe_open.REASON_OPEN_FAILED, _detail(exc), file=path) from exc
    try:
        _take_lock(descriptor, path)
        yield
    finally:
        os.close(descriptor)


def _take_lock(descriptor: int, path: Path) -> None:
    for mode in (fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_EX):
        try:
            fcntl.flock(descriptor, mode)
            return
        except BlockingIOError:
            logger.warning(
                "Another run of the upgrade step for adapter-host files is working; this one waits for it to "
                "finish. event=adapter_host_files_waiting_for_other_run lock=%s",
                path,
            )
        except OSError as exc:
            if exc.errno in _LOCK_UNSUPPORTED_ERRNOS:
                logger.warning(
                    "The file system of state/upgrades offers no file locks, so the upgrade step for adapter-host "
                    "files runs without one: do not run it twice at once. event=adapter_host_files_lock_unsupported "
                    "lock=%s reason=%s",
                    path,
                    _detail(exc),
                )
                return
            raise FolderProblem(path.parent, safe_open.REASON_OPEN_FAILED, _detail(exc), file=path) from exc
    # Only a waiting flock that answered "would block" gets here; never run without the lock silently.
    raise FolderProblem(path.parent, safe_open.REASON_OPEN_FAILED, "the lock could not be taken", file=path)


def _write_state_file(path: Path, body: dict[str, Any]) -> None:
    """Write the marker or the progress record in ``state/upgrades`` (``_replace_file``); a failure is rule 2.

    Not synced: losing the progress record only makes a later run wait (it
    is never proof that files are absent), and losing the marker runs the
    step again, which finds nothing left to do.
    """

    try:
        _replace_file(path, json.dumps(body, indent=2) + "\n")
    except OSError as exc:
        raise FolderProblem(path.parent, safe_open.REASON_OPEN_FAILED, _detail(exc), file=path) from exc


def _replace_file(path: Path, text: str) -> None:
    """Write ``text`` to a temporary file renamed onto ``path``, so a reader never sees half of it.

    Raises ``OSError`` after removing the temporary file. Not synced: the
    marker and the progress record say why above; a list of files no record
    names that a crash loses only leads to fewer files being deleted.
    """

    temporary = path.with_name(f".{path.name}.writing")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


__all__ = ["FolderProblem", "UpgradeCounts", "UpgradeResult", "upgrade_adapter_host_files"]
