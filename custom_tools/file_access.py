"""The one way agent tools open files (access plan, Phase 5).

Every tool that reads or writes a file for an agent (file tools, attachments,
local media, Python environments, research and web artifacts, settings and
skill drafts) goes through this module. For a requested path it:

1. makes a relative path absolute against the task's default folder;
2. resolves links once, to find where the path really points;
3. judges that real location with the folder rules
   (``custom_tools.path_policy``): the task's folders, the read-only skill
   folders, and never control files, runtime records, another person's folder
   or a sensitive name;
4. with authorization on, asks the access service whether the agent (and
   the person its task works for) may read or write that folder
   (``custom_agents.folder_access``; folder ids from ``access_control.folders``);
5. opens the real location from the folder it belongs to through
   ``access_control.safe_open``, one component at a time without following
   links. A write replaces a file in one rename, so readers and other writers
   never see it half written.

So a link inside the person's folder that points at their own files works, a
link that points anywhere else is judged by where it points, and a link
swapped in between the check and the open makes the open fail. Sandboxed code
may create links in the folders it can write; trusted code never follows them.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from access_control import safe_open
from access_control.folders import FolderRoots
from access_control.safe_open import UnsafePath
from custom_tools.path_policy import (
    PathPolicyError,
    ensure_allowed_path,
    readable_skill_dir,
)
from custom_messages.contracts.access_v2 import AccessLevel
from custom_tools.runtime_context import get_agent_ref
from custom_tools.workspace_scope import configured_data_folders, current_workspace_scope
from runtime_settings.instance_layout import USERS_DIRNAME, WorkspaceScope

logger = logging.getLogger(__name__)

LINK_NOT_FOLLOWED = "link_not_followed"
SEVERAL_HARD_LINKS = "several_hard_links"
NOT_A_REGULAR_FILE = "not_a_regular_file"
NOT_A_FOLDER = "not_a_folder"
FILE_TOO_LARGE = "file_too_large"
UNSAFE_PATH = "unsafe_path"
ACCESS_DENIED = "access_denied"
_REASON_CODES = {
    safe_open.REASON_LINK: LINK_NOT_FOLLOWED,
    safe_open.REASON_SEVERAL_LINKS: SEVERAL_HARD_LINKS,
    safe_open.REASON_NOT_REGULAR_FILE: NOT_A_REGULAR_FILE,
    safe_open.REASON_NOT_DIRECTORY: NOT_A_FOLDER,
    safe_open.REASON_TOO_LARGE: FILE_TOO_LARGE,
}
@dataclass(frozen=True)
class FileLocation:
    """A checked file or folder: the trusted folder it is in, and the path below it.

    ``task_folder`` marks one of the task's own folders (the person's folder,
    ``team/``, ``agents/``), which trusted code creates on first write.
    """

    root: Path
    parts: tuple[str, ...]
    task_folder: bool = False

    @property
    def path(self) -> Path:
        return self.root.joinpath(*self.parts)


def _real_roots(scope: WorkspaceScope) -> tuple[Path, ...]:
    return tuple(Path(os.path.realpath(root)) for root in scope.roots)


def _root_of(real: Path, scope: WorkspaceScope, agent: Any) -> tuple[Path, bool]:
    """The trusted folder holding ``real``, and whether it is one of the task's own."""

    skill_dir = readable_skill_dir(real, agent)
    if skill_dir is not None:
        return Path(os.path.realpath(skill_dir)), False
    for root in _real_roots(scope):
        if real == root or root in real.parents:
            return root, True
    for folder in configured_data_folders(agent):
        root = Path(os.path.realpath(folder.path))
        if real == root or root in real.parents:
            return root, False
    users = Path(os.path.realpath(scope.workspaces_dir)) / USERS_DIRNAME
    if users in real.parents:
        # Another person's folder, reached only through what they shared with
        # the task's person; the access service decides (decision D15).
        return users / real.relative_to(users).parts[0], False
    # ensure_allowed_path refuses every other path first.
    raise PathPolicyError(f"No registered folder holds {real}.", reason_code=UNSAFE_PATH)


def locate(
    raw: str | Path, *, for_write: bool = False, agent: Any = None, is_file: bool | None = True
) -> FileLocation:
    """Check ``raw`` for the agent's current task; raises ``PathPolicyError`` when refused.

    ``agent`` defaults to the agent running the current tool call. ``is_file``
    says whether the path is used as a file or a folder (None: look).
    """

    agent = agent if agent is not None else get_agent_ref()
    scope = current_workspace_scope(agent)
    real = ensure_allowed_path(str(raw), for_write=for_write, agent=agent)
    root, task_folder = _root_of(real, scope, agent)
    _decide(real, scope, for_write=for_write, agent=agent, is_file=is_file)
    return FileLocation(root, real.relative_to(root).parts, task_folder)


def _decide(real: Path, scope: WorkspaceScope, *, for_write: bool, agent: Any, is_file: bool | None) -> None:
    """Ask the access service about a path in a registered folder root.

    The read-only skill folders are judged by the folder rules alone.
    """

    roots = FolderRoots(Path(os.path.realpath(scope.workspaces_dir)), configured_data_folders(agent))
    target_id = roots.folder_id_of(real)
    if target_id is None or agent is None:
        return
    if is_file is None:
        is_file = not os.path.isdir(real)
    from custom_agents.folder_access import decide_folder_use

    try:
        decision = decide_folder_use(
            agent, target_id, is_file=is_file, level=AccessLevel.WRITE if for_write else AccessLevel.READ
        )
    except RuntimeError as exc:
        raise PathPolicyError(f"The access check for {real} could not run: {exc}", reason_code=ACCESS_DENIED) from exc
    if decision is not None and not decision.allowed:
        raise PathPolicyError(f"{decision.reason} {decision.next_step}".strip(), reason_code=ACCESS_DENIED)


def _for_writing(raw: str | Path, agent: Any, *, is_file: bool = True) -> FileLocation:
    """Check ``raw`` for a write; a missing task folder is created first (a person's on first use)."""

    location = locate(raw, for_write=True, agent=agent, is_file=is_file)
    if location.task_folder:
        location.root.mkdir(parents=True, exist_ok=True)
    return location


def _translated(exc: UnsafePath, location: FileLocation) -> OSError:
    """A missing file stays ``FileNotFoundError``; every other refusal is a policy error."""

    if exc.reason == safe_open.REASON_NOT_FOUND:
        return FileNotFoundError(f"Not found: {location.path}")
    reason_code = _REASON_CODES.get(exc.reason, UNSAFE_PATH)
    logger.warning(
        "A file was not opened because it could not be opened safely. event=file_access_refused "
        "reason_code=%s path=%s",
        reason_code,
        location.path,
    )
    return PathPolicyError(str(exc), reason_code=reason_code)


@contextmanager
def _safely(location: FileLocation) -> Iterator[None]:
    try:
        yield
    except UnsafePath as exc:
        raise _translated(exc, location) from exc


def read_bytes(raw: str | Path, *, max_bytes: int | None = None, agent: Any = None) -> tuple[FileLocation, bytes]:
    location = locate(raw, agent=agent)
    with _safely(location):
        return location, safe_open.read_bytes(location.root, location.parts, max_bytes=max_bytes)


def read_text(
    raw: str | Path, *, max_bytes: int | None = None, errors: str = "strict", agent: Any = None
) -> tuple[FileLocation, str]:
    location, data = read_bytes(raw, max_bytes=max_bytes, agent=agent)
    return location, data.decode("utf-8", errors=errors)


@contextmanager
def writable(
    raw: str | Path, *, create_directories: bool = True, agent: Any = None
) -> Iterator[tuple[FileLocation, BinaryIO]]:
    """A binary writer for a file in the task's folders, new or replaced.

    The content replaces the file in one rename when the block ends
    (``access_control.safe_open.writable_file``): readers see the old content
    or the new, never a mix, and a block that raises leaves the file as it was.
    """

    location = _for_writing(raw, agent)
    with _safely(location):
        with safe_open.writable_file(location.root, location.parts, create_directories=create_directories) as writer:
            yield location, writer


def write_bytes(
    raw: str | Path, data: bytes, *, create_directories: bool = True, agent: Any = None
) -> FileLocation:
    with writable(raw, create_directories=create_directories, agent=agent) as (location, writer):
        writer.write(data)
    return location


def write_text(
    raw: str | Path, text: str, *, create_directories: bool = True, errors: str = "strict", agent: Any = None
) -> FileLocation:
    return write_bytes(
        raw, text.encode("utf-8", errors=errors), create_directories=create_directories, agent=agent
    )


def make_directories(raw: str | Path, *, agent: Any = None) -> FileLocation:
    """Create a folder (and missing parents) in the task's folders, never through a link."""

    location = _for_writing(raw, agent, is_file=False)
    with _safely(location):
        os.close(safe_open.open_directory(location.root, location.parts, create=True))
    return location


def remove_tree(raw: str | Path, *, agent: Any = None) -> FileLocation:
    """Delete a folder (or file) in the task's folders; missing is fine.

    The parent is opened without following links and the removal runs relative
    to it (``shutil.rmtree`` with ``dir_fd`` never follows links inside), so a
    link swapped in anywhere on the path cannot redirect the deletion.
    """

    location = locate(raw, for_write=True, agent=agent, is_file=None)
    if not location.parts:
        raise PathPolicyError(f"A task folder itself cannot be removed: {location.path}", reason_code=UNSAFE_PATH)
    with _safely(location):
        parent = safe_open.open_directory(location.root, location.parts[:-1])
    try:
        name = location.parts[-1]
        try:
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return location
        if stat.S_ISDIR(entry.st_mode):
            shutil.rmtree(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
    finally:
        os.close(parent)
    return location


def status(raw: str | Path, *, agent: Any = None) -> tuple[FileLocation, os.stat_result | None]:
    """The checked location and its own status (a link is not followed); None when missing."""

    location = locate(raw, agent=agent, is_file=None)
    with _safely(location):
        return location, safe_open.entry_status(location.root, location.parts)


def copy_to(
    raw: str | Path, destination: Path, *, max_bytes: int | None = None, agent: Any = None
) -> FileLocation:
    """Copy a checked file to a new file at ``destination``, a path trusted code chose."""

    location = locate(raw, agent=agent)
    with _safely(location):
        safe_open.copy_file(location.root, location.parts, destination, max_bytes=max_bytes)
    return location


MAX_MOVE_BYTES = 256 * 1024 * 1024


def move_file(
    source: str | Path, destination: str | Path, *, keep_original: bool = False, agent: Any = None
) -> tuple[FileLocation, FileLocation]:
    """Copy or move one file between folders the agent may use; never overwrites.

    A move needs write access where the file is (it is removed there) and
    where it goes; a copy needs read access to the source. The content is read
    once without following links and written as a new file, so a file that
    already exists at the destination is left alone.
    """

    origin = locate(source, for_write=not keep_original, agent=agent)
    target = _for_writing(destination, agent)
    with _safely(origin):
        data = safe_open.read_bytes(origin.root, origin.parts, max_bytes=MAX_MOVE_BYTES)
    with _safely(target):
        with safe_open.writable_file(target.root, target.parts, create_directories=True, exclusive=True) as writer:
            writer.write(data)
    if not keep_original:
        with _safely(origin):
            safe_open.remove_file(origin.root, origin.parts)
    return origin, target


def list_directory(
    raw: str | Path, *, agent: Any = None
) -> tuple[FileLocation, list[tuple[str, os.stat_result]]]:
    location = locate(raw, agent=agent, is_file=False)
    with _safely(location):
        return location, safe_open.list_directory(location.root, location.parts)


__all__ = [
    "ACCESS_DENIED",
    "FILE_TOO_LARGE",
    "FileLocation",
    "LINK_NOT_FOLLOWED",
    "NOT_A_FOLDER",
    "NOT_A_REGULAR_FILE",
    "PathPolicyError",
    "SEVERAL_HARD_LINKS",
    "UNSAFE_PATH",
    "copy_to",
    "list_directory",
    "move_file",
    "locate",
    "make_directories",
    "remove_tree",
    "read_bytes",
    "read_text",
    "status",
    "writable",
    "write_bytes",
    "write_text",
]
