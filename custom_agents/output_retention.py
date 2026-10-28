"""Trusted cleanup of saved tool outputs after the team's retention period.

Agents' tools save four kinds of output while they work. Nothing deleted them,
so they grew without a bound. On each run of the internal-improvement
maintenance schedule the manager deletes every item written more than
``tool_policy.output_retention_days`` ago (90 days by default; 0 keeps them),
before InternalImprovementAgent's review, and puts the counts and bytes per
kind in that run's instructions (``custom_agents.internal_improvement_runtime``).
One period covers all four (owner's decision 2026-10-27): they are all copies
of what a tool returned, and a tool can be run again.

1. **Tool results** (``state/tool_results/<task>/<stamp>_<tool use>_<tool>_<hash>/``):
   oversized results saved by ``custom_agents.tool_result_offloader`` and the
   inner-loop context manager. Written at the UTC time the folder name starts
   with; when a name has none, the manifest's ``created_at``, then the
   folder's last change. The whole folder goes, ``manifest.json`` first, so
   ``inspect_tool_result_artifact`` never finds a manifest without its blocks.
2. **Conversation artifacts**: long tool output offloaded from conversation
   turns, a ``conversation_artifacts`` record and its file in
   ``dbs/conversation_artifacts/<conversation>/``. Records created before the
   cutoff are deleted oldest first, in batches, then their files (record
   first: a file that cannot go is left without a record, never a record
   without its file). Then every file there last changed before the cutoff,
   which also removes files no record names any more, and the folders left
   empty. A removed person's held artifacts keep their files in place until
   the purge (``access_control.held_person_folders``); old ones go here too, as
   they would have if the person had stayed.
3. **Research results** (``research/<task folder>/<result folder>/`` in each
   person's folder and in ``agents/``, ``custom_tools.research_identity``):
   only result folders whose names start with the research tools' UTC stamp,
   aged by that stamp: ``<stamp>_<labels>_<12 hex>`` (``result_folder_name``)
   and the older ``<stamp>_<kind>_<label>``. People and agents also keep
   their own folders and files under ``research/``, so a folder without that
   stamp, or with one dated in the future, is never deleted, nor is any loose
   file, and a task folder goes only when this run emptied it.
4. **Execution run folders** (``agents/execution``): a run's folder that an
   interrupted run left behind (a finished run deletes its own) and the
   content-addressed Rust build folders (``rust-builds/<source digest>/``),
   each as a whole, by its last change (a build changes its folder). They are
   never deleted younger than ``EXECUTION_MINIMUM_AGE_DAYS``, because no run
   or build lasts longer than a day (``ExecutionLimitsV1.wall_time_seconds``),
   and a build folder only while holding its build lock
   (``runtime_hosting.builds.BUILD_LOCK_FILENAME``), after checking its age
   again under the lock, and with the lock file deleted last; a build that
   waited on that lock starts over in a new folder.

A week's grace (owner's decision 2026-10-28): when retention first becomes
active on an installation (the first run with a period above 0 after an
upgrade, or after the period was 0), nothing is deleted for ``GRACE_DAYS``.
Each run in that week is a dry run that counts what it would delete, through
the same walks (``OldFilesRemoved.dry_run``); administrators get one Inbox
notice (``grace_notice``) and the Status page shows the latest counts. The
start is kept in the run record (``state/output_retention.json``), so a
restart does not start the week again; a period of 0 forgets it.
Shortening an active period is the administrators' explicit choice and
applies at once.

Not deleted here: the content-addressed execution artifacts
(``state/execution_artifacts``). One stored object can serve many owners'
execution records (code, builds, revisions, run logs), which are kept, and
deleting one an execution record still names breaks every later read of it
(``ArtifactIntegrityError``). Deleting them safely needs a retention period for
those records first and a record of which objects they name.

Kept for good, whatever the period: conversations (transcripts, task links,
turn summaries and context summaries) and completed tasks. An old
conversation keeps its summary of an offloaded output, and every reader says
plainly when the output itself was deleted (``deleted_output_note``): the
tools that open saved outputs (``inspect_tool_result_artifact``,
``web_artifact_inspect``) answer with that note and ``retryable: false``
instead of "not found", and the context renderers, compactors and the
conversation tools show it in place of the file's path
(``with_deleted_outputs_marked``). None of them retries or makes up content.

Trusted manager code does this rather than the maintenance agent because the
agent works for no person and cannot open people's folders or ``state/``.
No walk follows a link (``old_file_cleanup``); research and execution folders
are agent-writable, so links there are removed as links, never followed.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Iterator, Mapping

from access_control import safe_open
from access_control.safe_open import UnsafePath
from custom_agents.scratch_retention import person_area_folders
from default_values import TOOL_POLICY_DEFAULTS
from old_file_cleanup import (
    FOLDER_FLAGS,
    SECONDS_PER_DAY,
    SKIPPED_CANNOT_OPEN,
    SKIPPED_NOT_FILE_OR_FOLDER,
    OldFilesRemoved,
    note_skipped,
    remove_file_if_old,
    remove_folder_tree,
    remove_listed_file,
    remove_old_files,
    retention_cutoff,
)
from runtime_hosting.builds import BUILD_LOCK_FILENAME
from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    EXECUTION_DIRNAME,
    EXECUTION_RUST_BUILDS_DIRNAME,
    PERSON_RESEARCH_DIRNAME,
    InstanceLayout,
)

if TYPE_CHECKING:
    from custom_agents.conversation_store import ConversationStore

logger = logging.getLogger(__name__)

RETENTION_SETTING = "tool_policy.output_retention_days"
TOOL_RESULT_MANIFEST_FILENAME = "manifest.json"
# Records deleted per database statement.
RECORD_BATCH = 500
# No execution run or build lasts longer than a day; keep a day more.
EXECUTION_MINIMUM_AGE_DAYS = 2
# A tool result manifest is a few kilobytes; a larger one is not read for its date.
MANIFEST_MAX_BYTES = 1024 * 1024

SKIPPED_BUILD_IN_USE = "build_in_use"

# "<YYYYmmddTHHMMSS>[microseconds]Z_<label>" at the start of a saved output folder's name.
_NAME_STAMP = re.compile(r"^(\d{8}T\d{6})(\d{6})?Z_.")


class OutputKind(StrEnum):
    TOOL_RESULTS = "tool_results"
    CONVERSATION_ARTIFACTS = "conversation_artifacts"
    RESEARCH_RESULTS = "research_results"
    EXECUTION_RUNS = "execution_runs"


# What the maintenance run's instructions call each kind, and its items.
_KIND_WORDS: dict[OutputKind, tuple[str, str]] = {
    OutputKind.TOOL_RESULTS: ("saved tool results", "results"),
    OutputKind.CONVERSATION_ARTIFACTS: ("long tool output saved from conversations", "records"),
    OutputKind.RESEARCH_RESULTS: ("research results", "results"),
    OutputKind.EXECUTION_RUNS: ("execution run and build folders", "folders"),
}


@dataclass(kw_only=True)
class OutputsRemoved(OldFilesRemoved):
    """What the cleanup did to one kind of saved output."""

    SKIPPED_EVENT: ClassVar[str] = "output_retention_skipped"

    kind: OutputKind
    # Result folders, artifact records, or run and build folders.
    items_removed: int = 0
    failed: bool = False

    def phrase(self) -> str:
        label, items = _KIND_WORDS[self.kind]
        if self.failed:
            return f"{label}: the cleanup failed (manager log event=output_retention_failed kind={self.kind})"
        return f"{label}: {self.items_removed} {items}, {self.files_removed} files, {self.bytes_removed} bytes"


@dataclass
class OutputRetentionResult:
    """What one cleanup did, per kind, or, in a dry run, what it would have deleted."""

    retention_days: int
    kinds: list[OutputsRemoved] = field(default_factory=list)
    dry_run: bool = False
    # When retention became active on this installation (``remove_old_outputs_and_record``).
    active_since: float | None = None
    # During the grace period: when deletion starts.
    deletes_from: float | None = None

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    @property
    def items(self) -> int:
        return sum(kind.items_removed for kind in self.kinds)

    @property
    def bytes(self) -> int:
        return sum(kind.bytes_removed for kind in self.kinds)

    def instruction_sentence(self) -> str:
        """One sentence for InternalImprovementAgent's run instructions."""

        if not self.enabled:
            return (
                f"Saved tool output cleanup is turned off ({RETENTION_SETTING} is 0), so no saved tool "
                "outputs were deleted before this review."
            )
        if self.dry_run:
            return (
                f"Saved tool output cleanup is in its {GRACE_DAYS}-day grace period, so nothing was deleted. "
                f"From {_date_text(self.deletes_from)} it deletes saved tool outputs written more than "
                f"{self.retention_days} days ago ({RETENTION_SETTING}); today it would delete: "
                + "; ".join(kind.phrase() for kind in self.kinds)
                + ". Administrators were told in the Inbox. Include these counts in your run record; these folders "
                "are closed to you, so do not try to inspect them."
            )
        skipped = sum(kind.skipped for kind in self.kinds)
        failed = any(kind.failed for kind in self.kinds)
        sentence = (
            f"Before this review, trusted maintenance deleted saved tool outputs written more than "
            f"{self.retention_days} days ago ({RETENTION_SETTING}): "
            + "; ".join(kind.phrase() for kind in self.kinds)
            + f". It left {skipped} entries in place (links, build folders in use, or entries it could not "
            "remove; manager log event=output_retention_skipped). Include these counts in your run record; these "
            "folders are closed to you, so do not try to inspect them."
        )
        if failed:
            sentence += " Report the failed cleanup to TeamArchitect."
        return sentence


# -- the whole cleanup -----------------------------------------------------------------


def remove_old_outputs(
    layout: InstanceLayout,
    *,
    retention_days: int,
    conversation_records: ConversationStore | None,
    now: float | None = None,
    dry_run: bool = False,
) -> OutputRetentionResult:
    """Delete saved tool outputs written more than ``retention_days`` ago; 0 deletes nothing.

    Each kind is cleaned on its own: a failure in one is logged and reported,
    and the others still run. With ``dry_run`` nothing is deleted and the
    counts say what would have been (``OldFilesRemoved.dry_run``).
    """

    result = OutputRetentionResult(retention_days=retention_days, dry_run=dry_run)
    if not result.enabled:
        logger.info(
            "Saved tool output cleanup is turned off. event=output_retention_disabled retention_days=%s",
            retention_days,
        )
        return result
    moment = datetime.now(timezone.utc).timestamp() if now is None else now
    cutoff = retention_cutoff(retention_days, moment)
    steps: list[tuple[OutputKind, Callable[[OutputsRemoved], None]]] = [
        (OutputKind.TOOL_RESULTS, lambda counts: _remove_old_tool_results(layout.tool_results_dir, cutoff, moment, counts)),
        (
            OutputKind.CONVERSATION_ARTIFACTS,
            lambda counts: _remove_old_conversation_artifacts(
                conversation_records, layout.conversation_artifacts_dir, cutoff, counts
            ),
        ),
        (OutputKind.RESEARCH_RESULTS, lambda counts: _remove_old_research_results(layout.workspaces_dir, cutoff, moment, counts)),
        (OutputKind.EXECUTION_RUNS, lambda counts: _remove_old_execution_runs(layout.workspaces_dir, cutoff, moment, counts)),
    ]
    for kind, step in steps:
        counts = OutputsRemoved(kind=kind, dry_run=dry_run)
        try:
            step(counts)
        except Exception:
            counts.failed = True
            logger.exception(
                "Saved tool output cleanup failed for one kind; the other kinds still run and the next run tries "
                "this one again. "
                "event=output_retention_failed kind=%s",
                kind,
            )
        result.kinds.append(counts)
        logger.info(
            "Old saved tool outputs were %s. event=%s kind=%s retention_days=%s "
            "items_removed=%s files_removed=%s bytes_removed=%s folders_removed=%s skipped=%s failed=%s",
            "counted, not deleted (grace period)" if dry_run else "deleted",
            "output_retention_counted" if dry_run else "output_retention_pruned",
            kind,
            retention_days,
            counts.items_removed,
            counts.files_removed,
            counts.bytes_removed,
            counts.folders_removed,
            counts.skipped,
            counts.failed,
        )
    return result


def remove_old_outputs_and_record(
    layout: InstanceLayout,
    *,
    retention_days: int,
    conversation_records: ConversationStore | None,
    now: float | None = None,
) -> OutputRetentionResult:
    """``remove_old_outputs`` with the grace period, then save what it did; a failed save is logged.

    Retention becomes active at the first run with a period above 0 after the
    record says it was not (a new installation or upgrade, or the period
    changed from 0); that time is kept in the record (``active_since``) and
    survives restarts. For ``GRACE_DAYS`` after it every run is a dry run, so
    administrators can see what will go and change the period first
    (``grace_notice``). A period of 0 forgets it. Shortening an active period
    is an administrator's explicit choice and takes effect at once.
    """

    moment = datetime.now(timezone.utc).timestamp() if now is None else now
    path = layout.output_retention_record_path
    active_since = _active_since(read_run_record(path), retention_days, moment)
    deletes_from = None if active_since is None else active_since + GRACE_DAYS * SECONDS_PER_DAY
    dry_run = deletes_from is not None and moment < deletes_from
    result = remove_old_outputs(
        layout,
        retention_days=retention_days,
        conversation_records=conversation_records,
        now=moment,
        dry_run=dry_run,
    )
    result.active_since = active_since
    result.deletes_from = deletes_from if dry_run else None
    try:
        write_run_record(path, result, now=datetime.fromtimestamp(moment, timezone.utc))
    except OSError:
        logger.exception(
            "The record of the saved tool output cleanup could not be saved. event=output_retention_record_failed "
            "path=%s",
            layout.output_retention_record_path,
        )
    return result


def _active_since(record: Mapping[str, Any], retention_days: int, now: float) -> float | None:
    """When retention became active: as recorded, or now when it was not active; None with a period of 0."""

    if retention_days <= 0:
        return None
    recorded = _time_of(record.get("active_since"))
    return recorded if recorded is not None else now


# -- the record of each run ----------------------------------------------------------------

RUN_RECORD_VERSION = 1
# The Status page warns when the cleanup has not succeeded for this long.
RUN_OVERDUE_SECONDS = 2 * SECONDS_PER_DAY
# Owner's decision 2026-10-28: when retention first becomes active, nothing is
# deleted for a week; each run only counts, and administrators are told.
GRACE_DAYS = 7


def write_run_record(path: Path, result: OutputRetentionResult, *, now: datetime | None = None) -> dict[str, Any]:
    """Save when this run happened and, per kind, what it deleted and whether it failed.

    ``last_success_at`` moves only when no kind failed, so the Status page
    can tell a cleanup that keeps failing (``overdue_since``). The file is
    replaced in one step.
    """

    moment = (now or datetime.now(timezone.utc)).isoformat()
    previous = read_run_record(path)
    succeeded = not any(kind.failed for kind in result.kinds)
    record: dict[str, Any] = {
        "record_version": RUN_RECORD_VERSION,
        "first_recorded_at": previous.get("first_recorded_at") or moment,
        "last_run_at": moment,
        "last_success_at": moment if succeeded else previous.get("last_success_at"),
        "retention_days": result.retention_days,
        "active_since": _iso(result.active_since),
        "dry_run": result.dry_run,
        "deletes_from": _iso(result.deletes_from),
        "kinds": {
            str(kind.kind): {
                "items": kind.items_removed,
                "files": kind.files_removed,
                "bytes": kind.bytes_removed,
                "folders": kind.folders_removed,
                "skipped": kind.skipped,
                "failed": kind.failed,
            }
            for kind in result.kinds
        },
    }
    _write_json_atomically(path, record)
    return record


def ensure_run_record(path: Path, *, retention_days: int, now: datetime | None = None) -> None:
    """At manager start: start the record when there is none, and forget the active period when it is 0.

    The record lets a cleanup that never runs be noticed two days later.
    Forgetting the active period here too means a period set to 0 and back
    between two maintenance runs still gets a new grace period.
    """

    record = read_run_record(path)
    if record and (retention_days > 0 or not record.get("active_since")):
        return
    moment = (now or datetime.now(timezone.utc)).isoformat()
    _write_json_atomically(
        path,
        {
            "record_version": RUN_RECORD_VERSION,
            "first_recorded_at": record.get("first_recorded_at") or moment,
            "last_run_at": record.get("last_run_at"),
            "last_success_at": record.get("last_success_at"),
            "retention_days": retention_days if record else None,
            "active_since": None,
            "dry_run": False,
            "deletes_from": None,
            "kinds": record.get("kinds") or {},
        },
    )


def read_run_record(path: Path) -> dict[str, Any]:
    """The saved record, or ``{}`` when there is none or it cannot be read (logged)."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning(
            "The record of the saved tool output cleanup could not be read. event=output_retention_record_unreadable "
            "path=%s",
            path,
            exc_info=True,
        )
        return {}
    return data if isinstance(data, dict) else {}


def overdue_since(record: Mapping[str, Any], *, now: float | None = None) -> float | None:
    """When the cleanup last succeeded (or, never yet, was first recorded), if that is too long ago."""

    since = _time_of(record.get("last_success_at")) or _time_of(record.get("first_recorded_at"))
    moment = datetime.now(timezone.utc).timestamp() if now is None else now
    if since is None or moment - since <= RUN_OVERDUE_SECONDS:
        return None
    return since


def grace_notice(result: OutputRetentionResult) -> tuple[str, str]:
    """Title and text of the administrators' notice about a grace period's first dry run."""

    when = _date_text(result.deletes_from)
    days = result.retention_days
    title = f"Saved tool outputs older than {days} days will be deleted from {when}"
    summary = (
        f"What will happen: on {when}, the team deletes about {result.items} items ({size_text(result.bytes)}) of "
        f"saved tool output older than {days} days: oversized tool results, long tool output saved from "
        "conversations (conversation artifacts), research results and execution run folders. After that, the "
        f"same kinds are deleted automatically once they are {days} days old.\n\n"
        "Why: these are copies of what tools returned, which a tool can produce again, and deleting them keeps "
        "the disk from growing without limit. Chat transcripts, task links, conversation summaries and "
        "completed tasks are kept.\n\n"
        "Your options: keep this default; change the number of days in Settings > Team > File Retention; or "
        "set it to 0 to keep everything. A change applies after the next restart.\n\n"
        "Found today: " + "; ".join(kind.phrase() for kind in result.kinds) + "."
    )
    return title, summary


def _date_text(moment: float | None) -> str:
    if moment is None:
        return "an unknown date"
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def size_text(size: int) -> str:
    """Bytes as people read them: "512 bytes", "3.4 MB", "1.2 GB"."""

    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _iso(moment: float | None) -> str | None:
    return None if moment is None else datetime.fromtimestamp(moment, timezone.utc).isoformat()


def _write_json_atomically(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


# -- when an output was written ----------------------------------------------------------


def written_at_from_name(name: str) -> float | None:
    """The UTC time a saved output folder's name starts with (``YYYYmmddTHHMMSS[ffffff]Z_``), if any."""

    match = _NAME_STAMP.match(name)
    if match is None:
        return None
    try:
        moment = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.timestamp() + (int(match.group(2)) / 1_000_000 if match.group(2) else 0.0)


def _research_written_at(folder: int, name: str, info: os.stat_result, now: float) -> float | None:
    """The stamp a research tool put in the result folder's name; None (keep) without one or in the future.

    The folder's last change says nothing here: a person may keep their own
    folders under ``research/`` and edit files in them.
    """

    named = written_at_from_name(name)
    return named if named is not None and named <= now else None


def _manifest_created_at(folder: int, name: str) -> float | None:
    """``created_at`` of the tool result manifest in the folder ``name``, never through a link."""

    try:
        child = os.open(name, FOLDER_FLAGS, dir_fd=folder)
    except OSError:
        return None
    try:
        descriptor = os.open(
            TOOL_RESULT_MANIFEST_FILENAME, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=child
        )
    except OSError:
        return None
    finally:
        os.close(child)
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(MANIFEST_MAX_BYTES + 1)
    if len(raw) > MANIFEST_MAX_BYTES:
        return None
    try:
        created_at = json.loads(raw).get("created_at")
        return datetime.fromisoformat(str(created_at)).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _tool_result_written_at(folder: int, name: str, info: os.stat_result, now: float) -> float:
    named = written_at_from_name(name)
    if named is not None and named <= now:
        return named
    created = _manifest_created_at(folder, name)
    return created if created is not None and created <= now else info.st_mtime


def _entries(folder: int) -> Iterator[tuple[str, os.stat_result]]:
    """The entries of an open folder with their own status (links not followed), in name order."""

    for name in sorted(os.listdir(folder)):
        try:
            yield name, os.stat(name, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            continue  # removed while we looked


# -- result folders in task folders (tool results, research results) --------------------

# When a result folder was written; None means it is not a tool's result and stays.
WrittenAt = Callable[[int, str, os.stat_result, float], float | None]


def _remove_old_results_in_task_folder(
    parent: int,
    task_name: str,
    shown: Path,
    *,
    cutoff: float,
    now: float,
    written_at: WrittenAt,
    counts: OutputsRemoved,
    first: str | None = None,
    remove_old_empty: bool = False,
) -> None:
    """Delete each result folder in the task folder ``task_name`` written before ``cutoff``.

    The task folder goes too when it is empty afterwards and this run emptied
    it, or, with ``remove_old_empty`` (a folder only trusted code writes), when
    it is itself that old. Other entries stay.
    """

    try:
        task_info = os.stat(task_name, dir_fd=parent, follow_symlinks=False)
        task = os.open(task_name, FOLDER_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_OPEN)
        return
    removed_before = counts.items_removed
    try:
        for name, info in _entries(task):
            written = written_at(task, name, info, now) if stat.S_ISDIR(info.st_mode) else None
            if written is None or written >= cutoff:
                continue
            if remove_folder_tree(task, name, shown / name, counts, first=first):
                counts.items_removed += 1
        empty = not os.listdir(task)
    finally:
        os.close(task)
    emptied_now = counts.items_removed > removed_before
    if empty and (emptied_now or (remove_old_empty and task_info.st_mtime < cutoff)):
        _remove_empty_folder(parent, task_name, counts)


def _remove_empty_folder(parent: int, name: str, counts: OutputsRemoved) -> None:
    if counts.dry_run:
        return
    try:
        os.rmdir(name, dir_fd=parent)  # only an empty folder, never through a link
    except OSError:
        return  # something was added meanwhile
    counts.folders_removed += 1


def _remove_old_tool_results(root_dir: Path, cutoff: float, now: float, counts: OutputsRemoved) -> None:
    try:
        root = safe_open.open_root(root_dir)
    except UnsafePath:
        return  # no tool result saved yet
    try:
        for task_name, info in _entries(root):
            if not stat.S_ISDIR(info.st_mode):
                note_skipped(counts, root_dir / task_name, SKIPPED_NOT_FILE_OR_FOLDER)
                continue
            _remove_old_results_in_task_folder(
                root,
                task_name,
                root_dir / task_name,
                cutoff=cutoff,
                now=now,
                written_at=_tool_result_written_at,
                counts=counts,
                first=TOOL_RESULT_MANIFEST_FILENAME,
                remove_old_empty=True,
            )
    finally:
        os.close(root)


def _remove_old_research_results(workspaces_dir: Path, cutoff: float, now: float, counts: OutputsRemoved) -> None:
    for parts in person_area_folders(workspaces_dir, PERSON_RESEARCH_DIRNAME, counts):
        shown = workspaces_dir.joinpath(*parts)
        try:
            research = safe_open.open_directory(workspaces_dir, parts)
        except UnsafePath as exc:
            if exc.reason != safe_open.REASON_NOT_FOUND:
                note_skipped(counts, shown, exc.reason)
            continue
        try:
            for task_name, info in _entries(research):
                if stat.S_ISDIR(info.st_mode):  # anything else here is not a tool result
                    _remove_old_results_in_task_folder(
                        research,
                        task_name,
                        shown / task_name,
                        cutoff=cutoff,
                        now=now,
                        written_at=_research_written_at,
                        counts=counts,
                    )
        finally:
            os.close(research)


# -- conversation artifacts ------------------------------------------------------------


def _remove_old_conversation_artifacts(
    records: ConversationStore | None,
    artifacts_dir: Path,
    cutoff: float,
    counts: OutputsRemoved,
) -> None:
    root_dir = artifacts_dir.expanduser().resolve()
    if records is None:
        # Files are still removed by their age below; the records stay until a run with the store.
        logger.warning(
            "Old conversation artifact records were not deleted because this runtime has no conversation "
            "store. event=output_retention_records_skipped"
        )
    elif counts.dry_run:
        # Their files are as old as they are, so the age walk below counts them.
        cutoff_text = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        counts.items_removed += records.count_artifacts_created_before(cutoff_text)
    else:
        _remove_old_artifact_records(records, root_dir, cutoff, counts)
    # Then every older file, which also removes files no record names any more.
    try:
        root = safe_open.open_root(root_dir)
    except UnsafePath:
        return  # nothing offloaded yet
    try:
        remove_old_files(root, root_dir, cutoff, counts)
    finally:
        os.close(root)


def _remove_old_artifact_records(
    records: ConversationStore, root_dir: Path, cutoff: float, counts: OutputsRemoved
) -> None:
    """Records first, oldest first and in batches, then the files they named."""

    cutoff_text = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    while True:
        paths = records.delete_artifacts_created_before(cutoff_text, limit=RECORD_BATCH)
        counts.items_removed += len(paths)
        for path in filter(None, paths):
            remove_listed_file(root_dir, Path(path), counts)
        if len(paths) < RECORD_BATCH:
            return


# -- execution run and build folders ----------------------------------------------------


def _remove_old_execution_runs(workspaces_dir: Path, cutoff: float, now: float, counts: OutputsRemoved) -> None:
    cutoff = min(cutoff, now - EXECUTION_MINIMUM_AGE_DAYS * SECONDS_PER_DAY)
    parts = (AGENTS_DIRNAME, EXECUTION_DIRNAME)
    shown = workspaces_dir.joinpath(*parts)
    try:
        runs = safe_open.open_directory(workspaces_dir, parts)
    except UnsafePath as exc:
        if exc.reason != safe_open.REASON_NOT_FOUND:
            note_skipped(counts, shown, exc.reason)
        return
    try:
        for name, info in _entries(runs):
            _remove_old_run_entry(runs, name, info, shown, cutoff, counts)
    finally:
        os.close(runs)


def _remove_old_run_entry(
    runs: int, name: str, info: os.stat_result, runs_shown: Path, cutoff: float, counts: OutputsRemoved
) -> None:
    """One entry of ``agents/execution``: the build folders, a run's folder, or a stray file."""

    shown = runs_shown / name
    if stat.S_ISDIR(info.st_mode) and name == EXECUTION_RUST_BUILDS_DIRNAME:
        _remove_old_builds(runs, name, shown, cutoff, counts)
    elif stat.S_ISREG(info.st_mode):
        remove_file_if_old(runs, name, info, runs_shown, cutoff, counts)
    elif not stat.S_ISDIR(info.st_mode):
        note_skipped(counts, shown, SKIPPED_NOT_FILE_OR_FOLDER)  # a link is never followed
    elif info.st_mtime < cutoff and remove_folder_tree(runs, name, shown, counts):
        counts.items_removed += 1


def _remove_old_builds(parent: int, name: str, shown: Path, cutoff: float, counts: OutputsRemoved) -> None:
    """Delete each build folder last built before ``cutoff``; the ``rust-builds`` folder stays."""

    try:
        builds = os.open(name, FOLDER_FLAGS, dir_fd=parent)
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_OPEN)
        return
    try:
        for build, info in _entries(builds):
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= cutoff:
                continue
            if _remove_build_folder(builds, build, shown / build, cutoff, counts):
                counts.items_removed += 1
    finally:
        os.close(builds)


def _remove_build_folder(parent: int, name: str, shown: Path, cutoff: float, counts: OutputsRemoved) -> bool:
    """Delete a build folder while holding its build lock, so no build uses it meanwhile.

    The lock file goes last: until then a build arriving waits on the same
    lock instead of creating a new one and writing into a folder being
    emptied. A build waiting on the lock finds its lock file gone afterwards
    and starts over in a new folder (``runtime_hosting.builds``). After the
    lock is taken the folder's age is checked again, because a build may
    have finished between the listing and the lock. A folder without a lock
    file was never locked by a build; the lock is not created, since that
    would change the folder's age.
    """

    try:
        folder = os.open(name, FOLDER_FLAGS, dir_fd=parent)
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_OPEN)
        return False
    try:
        try:
            lock = _open_build_lock(folder)
        except OSError:
            note_skipped(counts, shown / BUILD_LOCK_FILENAME, SKIPPED_CANNOT_OPEN)
            return False
        try:
            if lock is not None and not _locked_now(lock):
                note_skipped(counts, shown, SKIPPED_BUILD_IN_USE)
                return False
            if os.fstat(folder).st_mtime >= cutoff:
                return False  # built again since the listing
            return remove_folder_tree(parent, name, shown, counts, last=BUILD_LOCK_FILENAME)
        finally:
            if lock is not None:
                os.close(lock)
    finally:
        os.close(folder)


def _open_build_lock(folder: int) -> int | None:
    """The build folder's lock file, never through a link; None when it has none."""

    try:
        return os.open(BUILD_LOCK_FILENAME, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=folder)
    except FileNotFoundError:
        return None


def _locked_now(lock: int) -> bool:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in {errno.EWOULDBLOCK, errno.EAGAIN}:
            raise
        return False
    return True


# -- what readers say about a deleted output ---------------------------------------------

DELETED_OUTPUT_FLAG = "deleted_under_retention"
DELETED_OUTPUT_REASON = "deleted_by_retention"


def retention_days_of(agent: Any) -> int:
    """The agent's saved output retention period in days (0 keeps outputs)."""

    try:
        return max(0, int(getattr(agent, "output_retention_days", TOOL_POLICY_DEFAULTS.output_retention_days.value)))
    except (TypeError, ValueError):
        return int(TOOL_POLICY_DEFAULTS.output_retention_days.value)


def deleted_by_retention(written_at: float | None, retention_days: int, *, now: float | None = None) -> bool:
    """Whether an output written at ``written_at`` is past the period, so a missing one was deleted under it."""

    if written_at is None or retention_days <= 0:
        return False
    return written_at < retention_cutoff(retention_days, now)


def deleted_output_note(retention_days: int | None = None) -> str:
    """What a reader says in place of a saved output the cleanup deleted; plain, and never a retry hint."""

    kept = f"are kept {retention_days} days after they were written" if retention_days else "are kept for a limited time"
    return (
        f"The saved output was deleted under the team's retention policy: saved tool outputs {kept}. "
        "Only its summary remains. Run the tool again if you need the content."
    )


@dataclass(frozen=True)
class SavedOutputRetention:
    """What a reader needs to tell that the cleanup deleted a saved output: the period and its folders.

    ``folders`` are the instance's ``tool_results_dir`` and
    ``conversation_artifacts_dir``, the only folders whose files the cleanup
    deletes while their conversation exists. With no folders (no instance
    layout) or a period of 0 nothing is ever said to be deleted.
    """

    retention_days: int = 0
    folders: tuple[Path, ...] = ()

    @classmethod
    def for_layout(cls, layout: InstanceLayout, retention_days: int) -> SavedOutputRetention:
        return cls(
            retention_days=max(0, int(retention_days)),
            folders=(layout.tool_results_dir, layout.conversation_artifacts_dir),
        )

    @classmethod
    def for_agent(cls, agent: Any) -> SavedOutputRetention:
        from custom_agents.runtime_paths import agent_layout

        return cls.for_layout(agent_layout(agent), retention_days_of(agent))

    def deleted(self, path: Any, *, created_at: Any = None, now: float | None = None) -> bool:
        """Whether the cleanup deleted the saved output at ``path``.

        All must hold: the period is above 0; ``path`` is absolute and, once
        normalized, inside one of the cleaned folders; when ``created_at`` is
        known, it is before the cutoff; and nothing is there any more. A
        relative path, a URL, a placeholder such as "(inline)", a path a
        summarizer reworded, one in an older layout, or a recent output is
        never called deleted.
        """

        raw = str(path or "")
        if self.retention_days <= 0 or not raw or not os.path.isabs(raw):
            return False
        normalized = Path(os.path.normpath(raw))
        if not any(folder in normalized.parents for folder in self.folders):
            return False
        written = _time_of(created_at)
        if written is not None and written >= retention_cutoff(self.retention_days, now):
            return False
        return not os.path.lexists(normalized)


def _time_of(value: Any) -> float | None:
    """An ISO 8601 time as a timestamp (UTC when it names no zone); None when unknown."""

    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def with_deleted_outputs_marked(
    artifacts: Any, retention: SavedOutputRetention | None, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Copies of a turn's artifact entries; one the cleanup deleted says so instead of naming its file.

    Its ``storage_path`` becomes ``deleted_output_note()`` and
    ``deleted_under_retention`` is set, so prompts and tool results never
    point at a file the retention deleted; its summary stays. Only entries
    ``retention.deleted`` confirms are changed; every other entry is copied
    as it is, and without a ``retention`` nothing is marked.
    """

    marked: list[dict[str, Any]] = []
    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, Mapping):
            continue
        entry = dict(artifact)
        path = entry.get("storage_path") or entry.get("path")
        if retention is not None and retention.deleted(path, created_at=entry.get("created_at"), now=now):
            entry["storage_path"] = deleted_output_note(retention.retention_days)
            entry.pop("path", None)
            entry[DELETED_OUTPUT_FLAG] = True
        marked.append(entry)
    return marked


__all__ = [
    "DELETED_OUTPUT_FLAG",
    "DELETED_OUTPUT_REASON",
    "EXECUTION_MINIMUM_AGE_DAYS",
    "GRACE_DAYS",
    "RETENTION_SETTING",
    "SavedOutputRetention",
    "OutputKind",
    "OutputRetentionResult",
    "OutputsRemoved",
    "deleted_by_retention",
    "deleted_output_note",
    "ensure_run_record",
    "grace_notice",
    "overdue_since",
    "read_run_record",
    "remove_old_outputs",
    "remove_old_outputs_and_record",
    "retention_days_of",
    "size_text",
    "with_deleted_outputs_marked",
    "write_run_record",
    "written_at_from_name",
]
