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
   written at the UTC time the result folder's name starts with
   (``result_folder_name``), else the folder's last change. Agents can write
   these folders, so a name dated in the future counts as no date. Files
   directly in ``research/`` or a task folder are not tool results and stay.
4. **Execution run folders** (``agents/execution``): a run's folder that an
   interrupted run left behind (a finished run deletes its own) and the
   content-addressed Rust build folders (``rust-builds/<source digest>/``),
   each as a whole, by its last change (a build changes its folder). They are
   never deleted younger than ``EXECUTION_MINIMUM_AGE_DAYS``, because no run
   or build lasts longer than a day (``ExecutionLimitsV1.wall_time_seconds``),
   and a build folder only while holding its build lock
   (``runtime_hosting.builds.BUILD_LOCK_FILENAME``); a build that waited on
   that lock starts over in a new folder.

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
    SKIPPED_CANNOT_REMOVE,
    SKIPPED_NOT_FILE_OR_FOLDER,
    OldFilesRemoved,
    note_skipped,
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

# "<YYYYmmddTHHMMSS>[microseconds]Z_" at the start of a saved output folder's name.
_NAME_STAMP = re.compile(r"^(\d{8}T\d{6})(\d{6})?Z_")


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
    """What one cleanup did, per kind."""

    retention_days: int
    kinds: list[OutputsRemoved] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    def instruction_sentence(self) -> str:
        """One sentence for InternalImprovementAgent's run instructions."""

        if not self.enabled:
            return (
                f"Saved tool output cleanup is turned off ({RETENTION_SETTING} is 0), so no saved tool "
                "outputs were deleted before this review."
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
) -> OutputRetentionResult:
    """Delete saved tool outputs written more than ``retention_days`` ago; 0 deletes nothing.

    Each kind is cleaned on its own: a failure in one is logged and reported,
    and the others still run.
    """

    result = OutputRetentionResult(retention_days=retention_days)
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
        counts = OutputsRemoved(kind=kind)
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
            "Old saved tool outputs were deleted. event=output_retention_pruned kind=%s retention_days=%s "
            "items_removed=%s files_removed=%s bytes_removed=%s folders_removed=%s skipped=%s failed=%s",
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


def _named_or_changed(name: str, info: os.stat_result, now: float) -> float:
    """The time in the name, unless it has none or is in the future; then the last change."""

    named = written_at_from_name(name)
    return named if named is not None and named <= now else info.st_mtime


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

WrittenAt = Callable[[int, str, os.stat_result, float], float]


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
) -> None:
    """Delete each result folder in the task folder ``task_name`` written before ``cutoff``.

    The task folder goes too when it is empty afterwards and either this run
    emptied it or it is itself that old. Other entries stay.
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
            if not stat.S_ISDIR(info.st_mode) or written_at(task, name, info, now) >= cutoff:
                continue
            if remove_folder_tree(task, name, shown / name, counts, first=first):
                counts.items_removed += 1
        empty = not os.listdir(task)
    finally:
        os.close(task)
    if empty and (counts.items_removed > removed_before or task_info.st_mtime < cutoff):
        _remove_empty_folder(parent, task_name, counts)


def _remove_empty_folder(parent: int, name: str, counts: OutputsRemoved) -> None:
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
                        written_at=lambda folder, name, entry, moment: _named_or_changed(name, entry, moment),
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
            _remove_old_run_entry(runs, name, info, shown / name, cutoff, counts)
    finally:
        os.close(runs)


def _remove_old_run_entry(
    runs: int, name: str, info: os.stat_result, shown: Path, cutoff: float, counts: OutputsRemoved
) -> None:
    """One entry of ``agents/execution``: the build folders, a run's folder, or a stray file."""

    if stat.S_ISDIR(info.st_mode) and name == EXECUTION_RUST_BUILDS_DIRNAME:
        _remove_old_builds(runs, name, shown, cutoff, counts)
    elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        note_skipped(counts, shown, SKIPPED_NOT_FILE_OR_FOLDER)  # a link is never followed
    elif info.st_mtime >= cutoff:
        return
    elif stat.S_ISDIR(info.st_mode):
        if remove_folder_tree(runs, name, shown, counts):
            counts.items_removed += 1
    else:
        _remove_file(runs, name, info, shown, counts)


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
            if _remove_build_folder(builds, build, shown / build, counts):
                counts.items_removed += 1
    finally:
        os.close(builds)


def _remove_build_folder(parent: int, name: str, shown: Path, counts: OutputsRemoved) -> bool:
    """Delete a build folder while holding its build lock, so no build uses it meanwhile.

    A build waiting on the lock finds its lock file gone afterwards and starts
    over in a new folder (``runtime_hosting.builds``).
    """

    try:
        folder = os.open(name, FOLDER_FLAGS, dir_fd=parent)
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_OPEN)
        return False
    try:
        lock = os.open(
            BUILD_LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=folder
        )
    except OSError:
        note_skipped(counts, shown / BUILD_LOCK_FILENAME, SKIPPED_CANNOT_OPEN)
        return False
    finally:
        os.close(folder)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EWOULDBLOCK, errno.EAGAIN}:
                raise
            note_skipped(counts, shown, SKIPPED_BUILD_IN_USE)
            return False
        return remove_folder_tree(parent, name, shown, counts)
    finally:
        os.close(lock)


def _remove_file(folder: int, name: str, info: os.stat_result, shown: Path, counts: OutputsRemoved) -> None:
    try:
        os.unlink(name, dir_fd=folder)
    except FileNotFoundError:
        return
    except OSError:
        note_skipped(counts, shown, SKIPPED_CANNOT_REMOVE)
        return
    counts.files_removed += 1
    counts.bytes_removed += info.st_size


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


def with_deleted_outputs_marked(artifacts: Any) -> list[dict[str, Any]]:
    """Copies of a turn's artifact entries; one whose saved file is gone says so instead of naming it.

    Its ``storage_path`` becomes ``deleted_output_note()`` and
    ``deleted_under_retention`` is set, so prompts and tool results never
    point at a file that no longer exists; its summary stays. While a
    conversation exists only this cleanup deletes these files (a removed
    person's purge deletes the conversation too), so a missing file is one
    the retention deleted.
    """

    marked: list[dict[str, Any]] = []
    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, Mapping):
            continue
        entry = dict(artifact)
        path = entry.get("storage_path") or entry.get("path")
        if path and not os.path.lexists(str(path)):
            entry["storage_path"] = deleted_output_note()
            entry.pop("path", None)
            entry[DELETED_OUTPUT_FLAG] = True
        marked.append(entry)
    return marked


__all__ = [
    "DELETED_OUTPUT_FLAG",
    "DELETED_OUTPUT_REASON",
    "EXECUTION_MINIMUM_AGE_DAYS",
    "RETENTION_SETTING",
    "OutputKind",
    "OutputRetentionResult",
    "OutputsRemoved",
    "deleted_by_retention",
    "deleted_output_note",
    "remove_old_outputs",
    "retention_days_of",
    "with_deleted_outputs_marked",
    "written_at_from_name",
]
