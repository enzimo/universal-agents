"""Folders as access targets (access plan, Phase 5).

A folder is named by an id below a registered root, never by a host path, so
grants stay valid when an instance moves:

- ``team`` and ``agents``: the shared folders under ``workspaces/``; every
  role holds ``write`` on them as a standard grant;
- ``data/<name>``: folders an administrator registers outside the workspace
  (``access_control.data_folders``, kept in the access store); they need
  grants, and one registered as not writable refuses writes whatever the
  grants say;
- ``users/<folder>``: one person's private folder, owned by that person. An
  agent uses the folder of the person its task works for through a standard
  rule. Only the owner may give access to it, and only to other people: a
  share (decision D15). An agent working for the person it was shared with
  may then use it at the shared level. Administrators cannot open it.

Everything else (control files, runtime records, databases, another
instance's paths) is outside every root, so no grant can name it. A grant on a
folder covers everything below it: the Cedar entity of a file is inside the
entity of each folder up to its root (``access_control.entities``).
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from access_control import safe_open
from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    TEAM_DIRNAME,
    USERS_DIRNAME,
    InstanceLayout,
    person_folder_name,
)

logger = logging.getLogger(__name__)

FOLDER_ROOT_TEAM = TEAM_DIRNAME
FOLDER_ROOT_AGENTS = AGENTS_DIRNAME
FOLDER_ROOT_USERS = USERS_DIRNAME
FOLDER_ROOT_DATA = "data"
# Folders every role holds write access to without a grant row.
STANDARD_SHARED_FOLDERS = (FOLDER_ROOT_TEAM, FOLDER_ROOT_AGENTS)
# Stands in for the owner of a person folder that is not the task person's, so
# the owner policy never matches it (no User has this id).
_OTHER_PERSON_OWNER_PREFIX = "folder-owner:"

PATH_SPELLING_MISMATCH = "path_spelling_mismatch"
PATH_SPELLING_UNVERIFIABLE = "path_spelling_unverifiable"


class FolderIdError(ValueError):
    """A folder id is malformed or names no registered root."""


class PathSpellingError(FolderIdError):
    """An existing path component differed from disk or could not be verified."""

    def __init__(
        self,
        message: str,
        *,
        typed: str,
        disk: str,
        unicode_only: bool = False,
        reason_code: str = PATH_SPELLING_MISMATCH,
        errno_value: int | None = None,
    ) -> None:
        super().__init__(message)
        self.typed = typed
        self.disk = disk
        self.unicode_only = unicode_only
        self.reason_code = reason_code
        self.errno_value = errno_value


class PathSpellingUnverifiableError(PathSpellingError):
    """On-disk spelling could not be verified (for example, unreadable folder or I/O error)."""

    def __init__(
        self,
        message: str,
        *,
        typed: str,
        errno_value: int | None = None,
    ) -> None:
        super().__init__(
            message,
            typed=typed,
            disk="",
            unicode_only=False,
            reason_code=PATH_SPELLING_UNVERIFIABLE,
            errno_value=errno_value,
        )


class FolderGrantOnFileError(FolderIdError):
    """A folder grant targeted an existing file or non-directory."""

    def __init__(
        self,
        message: str,
        *,
        target: str,
        suggested_folder: str | None = None,
    ) -> None:
        super().__init__(message)
        self.target = target
        self.suggested_folder = suggested_folder


def _components(raw: str) -> list[str]:
    text = str(raw or "").strip().strip("/")
    if not text or "\\" in text or "\x00" in text:
        raise FolderIdError(f"Not a folder id: {raw!r}. Use team/…, agents/… or data/<name>/….")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise FolderIdError(f"A folder id cannot contain empty, '.' or '..' parts: {raw!r}")
    return parts


def root_length(parts: list[str] | tuple[str, ...]) -> int:
    """How many leading parts name the root (``team`` is one; ``data/x`` is two)."""

    if parts and parts[0] in {FOLDER_ROOT_TEAM, FOLDER_ROOT_AGENTS}:
        return 1
    if len(parts) >= 2 and parts[0] in {FOLDER_ROOT_DATA, FOLDER_ROOT_USERS}:
        return 2
    raise FolderIdError(
        f"{'/'.join(parts)!r} is not below a folder root; use team/…, agents/… or data/<name>/…."
    )


def normalize_folder_id(raw: str) -> str:
    """The canonical id of a folder (or file) below a registered root."""

    parts = _components(raw)
    root_length(parts)
    return "/".join(parts)


def grantable_folder_id(raw: str) -> str:
    """A folder (or file) id that grants, requests and blocks may name.

    A person's folder is accepted here; the access service decides who may
    give access to it (its owner only, ``std:owner``).
    """

    return normalize_folder_id(raw)


def is_person_folder(folder_id: str) -> bool:
    """Whether the id names a person's folder or something inside it."""

    return normalize_folder_id(folder_id).split("/", 1)[0] == FOLDER_ROOT_USERS


def person_folder_id(person_id: str) -> str:
    """The folder id of one person's own folder (``users/<folder>``)."""

    return f"{FOLDER_ROOT_USERS}/{person_folder_name(person_id)}"


def folder_chain(target_id: str) -> list[str]:
    """``target_id`` and each folder above it, up to its root, nearest first."""

    parts = normalize_folder_id(target_id).split("/")
    stop = root_length(parts)
    return ["/".join(parts[:end]) for end in range(len(parts), stop - 1, -1)]


def covers(parent_id: str, child_id: str) -> bool:
    """Whether the folder ``parent_id`` is ``child_id`` or contains it."""

    parent = normalize_folder_id(parent_id)
    child = normalize_folder_id(child_id)
    return child == parent or child.startswith(parent + "/")


def paths_inside(folder_id: str, paths: Iterable[str]) -> list[str]:
    """The ``paths`` strictly inside the folder ``folder_id``, sorted.

    Used with the targets of blocks: a block inside a folder keeps the whole
    folder from being handed on (``AccessService._prepare_hand_down``),
    because the worker would get the folder with everything in it. A block on
    the folder itself or above it is left out: Cedar already refuses the
    folder for it. Raises ``FolderIdError`` when an id is not a folder id.
    """

    # custom_agents.code_access._blocked_inside applies the same rule for the
    # sandbox; it can call this once the parallel sandbox change is merged.

    return sorted(path for path in paths if path != folder_id and covers(folder_id, path))


@dataclass(frozen=True)
class DataFolder:
    """An operator-registered folder outside the workspace."""

    name: str
    path: Path
    writable: bool = True


@dataclass(frozen=True)
class FolderRoots:
    """The registered roots of one instance, with their real paths."""

    workspaces_dir: Path
    data_folders: tuple[DataFolder, ...] = field(default=())

    @classmethod
    def for_layout(cls, layout: InstanceLayout, data_folders: tuple[DataFolder, ...] = ()) -> "FolderRoots":
        return cls(Path(os.path.realpath(layout.workspaces_dir)), data_folders)

    def _roots(self) -> list[tuple[str, Path]]:
        roots = [
            (FOLDER_ROOT_TEAM, self.workspaces_dir / TEAM_DIRNAME),
            (FOLDER_ROOT_AGENTS, self.workspaces_dir / AGENTS_DIRNAME),
        ]
        roots += [(f"{FOLDER_ROOT_DATA}/{folder.name}", Path(os.path.realpath(folder.path))) for folder in self.data_folders]
        return roots

    def data_folder(self, folder_id: str) -> DataFolder | None:
        parts = normalize_folder_id(folder_id).split("/")
        if parts[0] != FOLDER_ROOT_DATA:
            return None
        return next((folder for folder in self.data_folders if folder.name == parts[1]), None)

    def folder_id_of(self, real_path: Path) -> str | None:
        """The folder id of a real (link-free) path, or None when it is under no root."""

        users = self.workspaces_dir / USERS_DIRNAME
        if users in real_path.parents:
            relative = real_path.relative_to(users).parts
            return "/".join((FOLDER_ROOT_USERS, *relative))
        for root_id, root in self._roots():
            if real_path == root or root in real_path.parents:
                return "/".join((root_id, *real_path.relative_to(root).parts))
        return None

    def root_and_parts(self, folder_id: str) -> tuple[Path, tuple[str, ...]]:
        """The trusted root directory and relative path components below it.

        Workspace paths (``team``, ``agents``, ``users/<person>``, and anything
        below them) anchor at ``workspaces_dir`` so walking every component
        verifies ``team``, ``agents``, ``users``, ``users/<person>`` and each
        subfolder without following links. Data folders anchor at the
        operator-registered data folder path (canonicalized once) and return
        the components below it.
        """

        parts = normalize_folder_id(folder_id).split("/")
        if parts[0] == FOLDER_ROOT_DATA:
            folder = self.data_folder(folder_id)
            if folder is None:
                raise FolderIdError(f"{FOLDER_ROOT_DATA}/{parts[1]} is not a registered folder.")
            return Path(os.path.realpath(folder.path)), tuple(parts[2:])
        return Path(os.path.realpath(self.workspaces_dir)), tuple(parts)


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
# Bounded per-process cache of exact directory entry names on case- or
# Unicode-normalizing filesystems, used only for exact-match fast hits when a
# directory's timestamps are safely in the past (avoiding coarse-timestamp
# races on Docker Desktop bind mounts).
_DIR_NAMES_CACHE_MAX = 128
_DIR_CACHE_MIN_AGE_NS = 2_000_000_000


@dataclass(frozen=True)
class _SpellingFS:
    """Filesystem operations seam for exact-spelling verification and tests."""

    open_root: Callable[[Path], int]
    open_dir: Callable[[str, int], int]
    fstat_dir: Callable[[int], os.stat_result]
    stat_entry: Callable[[str, int], os.stat_result]
    list_entries: Callable[[int], list[tuple[str, int | None]]]
    close_fd: Callable[[int], None]
    now_ns: Callable[[], int] = time.time_ns


def _posix_open_dir(name: str, dir_fd: int) -> int:
    return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)


def _posix_stat_entry(name: str, dir_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _posix_list_entries(dir_fd: int) -> list[tuple[str, int | None]]:
    with os.scandir(dir_fd) as entries:
        return [(entry.name, entry.inode()) for entry in entries]


_POSIX_FS = _SpellingFS(
    open_root=safe_open.open_root,
    open_dir=_posix_open_dir,
    fstat_dir=os.fstat,
    stat_entry=_posix_stat_entry,
    list_entries=_posix_list_entries,
    close_fd=os.close,
    now_ns=time.time_ns,
)
# Tests may monkeypatch _ACTIVE_FS to simulate case-insensitive or
# Unicode-normalizing filesystems over real case-sensitive temp folders.
_ACTIVE_FS: _SpellingFS = _POSIX_FS

_SPELLING_CACHE_LOCK = threading.Lock()
# Keyed by (st_dev, st_ino, st_mtime_ns, st_ctime_ns) of the parent directory.
_dir_names_cache: OrderedDict[tuple[int, int, int, int], frozenset[str]] = OrderedDict()


def clear_spelling_cache() -> None:
    """Reset the bounded exact-match directory-name cache."""

    with _SPELLING_CACHE_LOCK:
        _dir_names_cache.clear()


def _ascii_swapcase(name: str) -> str | None:
    """Swap only ASCII letters [A-Za-z]; return None when ``name`` has none.

    Using ``str.swapcase`` would expand non-ASCII characters such as ``ß`` ->
    ``SS`` or ``ﬁ`` -> ``FI`` into names that do not exist even on
    case-insensitive APFS/NTFS, which would miss the case-insensitive match.
    """

    chars: list[str] = []
    changed = False
    for ch in name:
        if "a" <= ch <= "z":
            chars.append(chr(ord(ch) - 32))
            changed = True
        elif "A" <= ch <= "Z":
            chars.append(chr(ord(ch) + 32))
            changed = True
        else:
            chars.append(ch)
    return "".join(chars) if changed else None


def _unicode_alt_form(name: str) -> str | None:
    """An alternate canonical Unicode normalization form of ``name``, or None."""

    nfc = unicodedata.normalize("NFC", name)
    if name != nfc:
        return nfc
    nfd = unicodedata.normalize("NFD", name)
    if name != nfd:
        return nfd
    return None


def _probe_same_entry(
    fs: _SpellingFS,
    current: int,
    alt: str,
    entry_info: os.stat_result,
) -> bool:
    """Whether ``alt`` inside ``current`` resolves to the same inode as ``entry_info``."""

    try:
        other = fs.stat_entry(alt, current)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return True
        raise
    return (other.st_dev, other.st_ino) == (entry_info.st_dev, entry_info.st_ino)


def _needs_directory_listing(
    fs: _SpellingFS,
    current: int,
    part: str,
    entry_info: os.stat_result,
) -> bool:
    """Whether ``current`` must be listed to verify ``part``'s exact spelling.

    Decides per parent directory rather than caching per device, because ext4
    and f2fs casefold (``chattr +F``), NTFS and WSL/Docker Desktop ``drvfs``
    allow case-sensitive and case-insensitive directories on the same ``st_dev``.
    """

    swapped = _ascii_swapcase(part)
    if swapped is not None:
        if _probe_same_entry(fs, current, swapped, entry_info):
            return True
    elif not part.isascii():
        # Non-ASCII names with no ASCII letters cannot be case-probed via ASCII
        # swap; list the directory directly rather than guessing.
        return True
    if not part.isascii():
        unicode_alt = _unicode_alt_form(part)
        if unicode_alt is not None and _probe_same_entry(fs, current, unicode_alt, entry_info):
            return True
    return False


def _lookup_cached_names(dir_key: tuple[int, int, int, int]) -> frozenset[str] | None:
    with _SPELLING_CACHE_LOCK:
        names = _dir_names_cache.get(dir_key)
        if names is not None:
            _dir_names_cache.move_to_end(dir_key)
        return names


def _store_cached_names(dir_key: tuple[int, int, int, int], names: frozenset[str]) -> None:
    with _SPELLING_CACHE_LOCK:
        _dir_names_cache[dir_key] = names
        _dir_names_cache.move_to_end(dir_key)
        while len(_dir_names_cache) > _DIR_NAMES_CACHE_MAX:
            _dir_names_cache.popitem(last=False)


def _stat_matches_target(
    fs: _SpellingFS,
    current: int,
    name: str,
    target: tuple[int, int],
) -> bool:
    try:
        st = fs.stat_entry(name, current)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == target


def _fallback_match_candidates(
    part: str,
    dir_info: os.stat_result,
    entry_info: os.stat_result,
    entries: list[tuple[str, int | None]],
) -> list[str]:
    """Candidate names when ``d_ino`` did not match (mount points or missing d_ino)."""

    part_folded = unicodedata.normalize("NFC", part).casefold()
    scan_all = entry_info.st_dev != dir_info.st_dev
    likely: list[str] = []
    remaining: list[str] = []
    for name, entry_ino in entries:
        if entry_ino == entry_info.st_ino:
            continue
        if unicodedata.normalize("NFC", name).casefold() == part_folded:
            likely.append(name)
        elif scan_all or entry_ino is None:
            remaining.append(name)
    return likely + remaining


def _match_disk_entry(
    fs: _SpellingFS,
    current: int,
    part: str,
    dir_info: os.stat_result,
    entry_info: os.stat_result,
    entries: list[tuple[str, int | None]],
) -> str | None:
    """Find the on-disk entry name inside ``current`` matching ``entry_info``."""

    target = (entry_info.st_dev, entry_info.st_ino)
    for name, entry_ino in entries:
        if entry_ino == entry_info.st_ino and _stat_matches_target(fs, current, name, target):
            return name
    for name in _fallback_match_candidates(part, dir_info, entry_info, entries):
        if _stat_matches_target(fs, current, name, target):
            return name
    return None


def _maybe_cache_dir_names(
    fs: _SpellingFS,
    current: int,
    dir_info: os.stat_result,
    names: frozenset[str],
) -> os.stat_result:
    """Cache ``names`` only when ``current`` did not change and its mtime is safely past."""

    after_dir = fs.fstat_dir(current)
    unchanged = (after_dir.st_mtime_ns, after_dir.st_ctime_ns) == (
        dir_info.st_mtime_ns,
        dir_info.st_ctime_ns,
    )
    old_enough = fs.now_ns() - max(dir_info.st_mtime_ns, dir_info.st_ctime_ns) >= _DIR_CACHE_MIN_AGE_NS
    if unchanged and old_enough:
        dir_key = (dir_info.st_dev, dir_info.st_ino, dir_info.st_mtime_ns, dir_info.st_ctime_ns)
        _store_cached_names(dir_key, names)
    return after_dir


def _first_folded_match(part: str, entries: list[tuple[str, int | None]]) -> str:
    part_folded = unicodedata.normalize("NFC", part).casefold()
    for name, _ in entries:
        if unicodedata.normalize("NFC", name).casefold() == part_folded:
            return name
    return ""


def _resolve_disk_name(
    fs: _SpellingFS,
    current: int,
    part: str,
    dir_info: os.stat_result,
    entry_info: os.stat_result,
) -> tuple[str | None, bool]:
    """Return ``(disk_name, exists)`` for ``part`` inside ``current``.

    Exact matches may be served from ``_dir_names_cache``; any mismatch always
    re-lists fresh so a rename or create is never hidden by a stale cache entry.
    """

    dir_key = (dir_info.st_dev, dir_info.st_ino, dir_info.st_mtime_ns, dir_info.st_ctime_ns)
    cached_names = _lookup_cached_names(dir_key)
    if cached_names is not None and part in cached_names:
        return part, True

    current_dir = dir_info
    current_entry = entry_info
    for attempt in range(2):
        entries = fs.list_entries(current)
        names = frozenset(name for name, _ in entries)
        after_dir = _maybe_cache_dir_names(fs, current, current_dir, names)
        if part in names:
            return part, True
        matched = _match_disk_entry(fs, current, part, current_dir, current_entry, entries)
        if matched is not None:
            return matched, True
        try:
            refreshed = fs.stat_entry(part, current)
        except FileNotFoundError:
            return None, False
        if attempt == 0 and (refreshed.st_dev, refreshed.st_ino) != (current_entry.st_dev, current_entry.st_ino):
            current_entry = refreshed
            current_dir = after_dir
            continue
        return _first_folded_match(part, entries), True
    return "", True


def _format_display_path(display_prefix: str | None, parts: Iterable[str]) -> str:
    joined = "/".join(parts)
    if display_prefix and joined:
        return f"{display_prefix}/{joined}"
    return display_prefix or joined


def _validate_spelling_parts(parts: tuple[str, ...]) -> None:
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\x00" in part:
            raise FolderIdError(f"Unsafe path component: {part!r}")


def _check_component_spelling(
    fs: _SpellingFS,
    current: int,
    part: str,
) -> tuple[str | None, os.stat_result | None, bool]:
    """Check one component inside ``current``.

    Returns ``(disk_name, entry_info, unresolved_churn)``; ``disk_name is None``
    means the component does not exist on disk.
    """

    try:
        entry_info = fs.stat_entry(part, current)
    except FileNotFoundError:
        return None, None, False
    if not _needs_directory_listing(fs, current, part, entry_info):
        return part, entry_info, False
    dir_info = fs.fstat_dir(current)
    disk_name, exists = _resolve_disk_name(fs, current, part, dir_info, entry_info)
    if not exists or disk_name is None:
        return None, None, False
    if disk_name == "":
        return "", entry_info, True
    return disk_name, entry_info, False


def _raise_spelling_mismatch(
    display_prefix: str | None,
    parts: tuple[str, ...],
    disk_parts: list[str],
    *,
    unresolved_churn: bool,
) -> None:
    typed_display = _format_display_path(display_prefix, parts)
    if unresolved_churn:
        raise PathSpellingUnverifiableError(
            f"Path {typed_display!r} could not be verified against an exact on-disk name because the folder changed while checking it.",
            typed=typed_display,
        )
    disk_display = _format_display_path(display_prefix, disk_parts)
    unicode_only = unicodedata.normalize("NFC", typed_display) == unicodedata.normalize("NFC", disk_display)
    if unicode_only:
        message = (
            f"Path {typed_display!r} does not match the exact name on disk {disk_display!r} "
            f"(different Unicode normalization). Use the exact on-disk spelling {disk_display!r}."
        )
    else:
        message = (
            f"Path {typed_display!r} does not match the exact name on disk {disk_display!r}. "
            f"Use the exact on-disk spelling {disk_display!r}."
        )
    raise PathSpellingError(message, typed=typed_display, disk=disk_display, unicode_only=unicode_only)


def _raise_spelling_unverifiable(
    display_prefix: str | None,
    parts: tuple[str, ...],
    exc: OSError,
) -> NoReturn:
    typed_display = _format_display_path(display_prefix, parts)
    err_no = (
        exc.__cause__.errno
        if isinstance(exc, safe_open.UnsafePath) and isinstance(exc.__cause__, OSError)
        else exc.errno
    )
    logger.warning(
        "Could not verify the exact on-disk spelling of a path. "
        "event=path_spelling_unverifiable path=%s errno=%s",
        typed_display,
        err_no,
    )
    raise PathSpellingUnverifiableError(
        f"Could not verify the exact on-disk spelling of {typed_display!r}.",
        typed=typed_display,
        errno_value=err_no,
    ) from exc


_STOP_WALK_ERRNOS = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR})


def _open_next_dir(
    active_fs: _SpellingFS,
    current: int,
    disk_name: str,
    *,
    had_mismatch: bool,
) -> int | None:
    """Open the next directory component without following links, or return None to stop."""

    try:
        return active_fs.open_dir(disk_name, current)
    except OSError as exc:
        if had_mismatch or exc.errno in _STOP_WALK_ERRNOS:
            return None
        raise


def _walk_exact_spelling(
    active_fs: _SpellingFS,
    current: int,
    parts: tuple[str, ...],
) -> tuple[list[str], bool, bool]:
    """Walk ``parts`` below open root fd ``current`` without following links."""

    disk_parts: list[str] = []
    had_mismatch = False
    unresolved_churn = False
    try:
        for index, part in enumerate(parts):
            disk_name, entry_info, churn = _check_component_spelling(active_fs, current, part)
            if disk_name is None or entry_info is None:
                disk_parts.extend(parts[index:])
                break
            if churn:
                had_mismatch = True
                unresolved_churn = True
                disk_parts.extend(parts[index:])
                break
            had_mismatch = had_mismatch or disk_name != part
            disk_parts.append(disk_name)
            if index + 1 >= len(parts) or not stat.S_ISDIR(entry_info.st_mode):
                disk_parts.extend(parts[index + 1 :])
                break
            following = _open_next_dir(active_fs, current, disk_name, had_mismatch=had_mismatch)
            if following is None:
                disk_parts.extend(parts[index + 1 :])
                break
            active_fs.close_fd(current)
            current = following
    finally:
        active_fs.close_fd(current)
    return disk_parts, had_mismatch, unresolved_churn


def check_exact_spelling(
    root: Path,
    parts: tuple[str, ...],
    *,
    display_prefix: str | None = None,
    fs: _SpellingFS | None = None,
) -> None:
    """Verify every existing component of ``root/parts`` matches its on-disk name.

    Walks from ``root`` one directory descriptor at a time without following
    links (matching ``access_control.safe_open``). Stops as soon as a component
    does not exist on disk so newly created files and future targets are
    accepted as typed. Raises ``PathSpellingError`` when any existing component
    differs in letter case or Unicode normalization from its directory entry,
    or ``PathSpellingUnverifiableError`` (a ``PathSpellingError`` subclass) when
    an ``OSError`` prevents verifying spelling, failing closed without leaking
    host paths.
    """

    if not parts:
        return
    _validate_spelling_parts(parts)

    active_fs = _ACTIVE_FS if fs is None else fs
    try:
        current = active_fs.open_root(root)
    except safe_open.UnsafePath as exc:
        if exc.reason == safe_open.REASON_ROOT_UNAVAILABLE and isinstance(exc.__cause__, FileNotFoundError):
            return
        _raise_spelling_unverifiable(display_prefix, parts, exc)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return
        _raise_spelling_unverifiable(display_prefix, parts, exc)

    try:
        disk_parts, had_mismatch, unresolved_churn = _walk_exact_spelling(active_fs, current, parts)
    except OSError as exc:
        _raise_spelling_unverifiable(display_prefix, parts, exc)
    if had_mismatch:
        _raise_spelling_mismatch(display_prefix, parts, disk_parts, unresolved_churn=unresolved_churn)


def check_folder_id_spelling(
    roots: FolderRoots,
    folder_id: str,
    *,
    fs: _SpellingFS | None = None,
) -> None:
    """Verify ``folder_id`` matches the exact on-disk spelling below its registered root.

    Messages always report logical folder ids (never host paths). Unregistered
    ``data/<name>`` targets are ignored here so access rules refuse them with
    their own message.
    """

    canonical = normalize_folder_id(folder_id)
    parts = canonical.split("/")
    if parts[0] == FOLDER_ROOT_DATA:
        folder = roots.data_folder(canonical)
        if folder is None:
            return
        rel_parts = tuple(parts[2:])
        if not rel_parts:
            return
        check_exact_spelling(
            Path(os.path.realpath(folder.path)),
            rel_parts,
            display_prefix=f"{FOLDER_ROOT_DATA}/{parts[1]}",
            fs=fs,
        )
        return
    check_exact_spelling(Path(os.path.realpath(roots.workspaces_dir)), tuple(parts), fs=fs)


def _parent_folder_id(canonical: str) -> str | None:
    parts = canonical.split("/")
    if len(parts) <= root_length(parts):
        return None
    return "/".join(parts[:-1])


def _unsafe_path_refusal(canonical: str, exc: safe_open.UnsafePath) -> str | None:
    if exc.reason == safe_open.REASON_ROOT_UNAVAILABLE and isinstance(exc.__cause__, FileNotFoundError):
        return None
    if exc.reason == safe_open.REASON_NOT_DIRECTORY:
        return f"Folder grants only open folders, not a path inside a file: '{canonical}'."
    if exc.reason == safe_open.REASON_LINK:
        return f"Folder grants only open real folders, not symbolic links: '{canonical}'."
    return f"Folder grants only open accessible folders: '{canonical}' could not be checked."


def _non_directory_refusal(canonical: str, mode: int) -> str:
    if stat.S_ISREG(mode):
        if is_person_folder(canonical):
            return (
                f"Folder grants only open folders, not the file '{canonical}'. "
                "A file in a person's folder is shared by its owner with /share or Share in the workspace browser."
            )
        parent = _parent_folder_id(canonical)
        if parent is not None:
            return f"Folder grants only open folders, not the file '{canonical}'. Grant the folder '{parent}' instead."
        return f"Folder grants only open folders, not the file '{canonical}'."
    if stat.S_ISLNK(mode):
        return f"Folder grants only open real folders, not the symbolic link '{canonical}'."
    return f"Folder grants only open folders, not '{canonical}'."


def folder_grant_file_refusal(roots: FolderRoots, folder_id: str) -> str | None:
    """Why a folder grant on ``folder_id`` must be refused, or None if allowed.

    Folder grants only open folders; a grant targeting an existing file would
    silently do nothing because file tools check ``File::<path>``. Returns a
    plain-language message naming the folder to grant instead (or, for a file
    in a person's folder, that its owner shares it). Missing paths and real
    directories return None.
    """

    canonical = normalize_folder_id(folder_id)
    parts = canonical.split("/")
    if parts[0] == FOLDER_ROOT_DATA and roots.data_folder(canonical) is None:
        return None
    base, rel_parts = roots.root_and_parts(canonical)
    try:
        info = safe_open.entry_status(base, rel_parts)
    except safe_open.UnsafePath as exc:
        return _unsafe_path_refusal(canonical, exc)
    if info is None or stat.S_ISDIR(info.st_mode):
        return None
    return _non_directory_refusal(canonical, info.st_mode)


def check_folder_target(
    roots: FolderRoots,
    target_id: str,
    *,
    for_block: bool = False,
    fs: _SpellingFS | None = None,
) -> None:
    """Validate an access target's on-disk spelling and (for grants) that it is not a file.

    Raises ``PathSpellingError`` when any existing component differs from its
    on-disk spelling. When ``for_block`` is False, also raises
    ``FolderGrantOnFileError`` if the target names an existing file or
    non-directory. Blocks on file paths remain allowed.
    """

    canonical = normalize_folder_id(target_id)
    check_folder_id_spelling(roots, canonical, fs=fs)
    if for_block:
        return
    refusal = folder_grant_file_refusal(roots, canonical)
    if refusal is not None:
        suggested = None if is_person_folder(canonical) else _parent_folder_id(canonical)
        raise FolderGrantOnFileError(refusal, target=canonical, suggested_folder=suggested)


def data_folder_problem(path: Path, protected: Iterable[Path]) -> str | None:
    """Why a data folder cannot be registered, or None.

    A data folder may neither hold nor sit inside a protected location (the
    instance root, the broker's credentials, the instance configuration), or
    folder grants and code mounts would reach control files and secrets.
    """

    real = Path(os.path.realpath(path))
    for location in protected:
        guarded = Path(os.path.realpath(location))
        if real == guarded or guarded in real.parents:
            return f"{path} is inside {location}, which agents must never reach."
        if real in guarded.parents:
            return f"{path} contains {location}, which agents must never reach."
    return None


def folder_owner(folder_id: str, task_person_id: str | None) -> str | None:
    """The owner of a folder for a decision: the task person for their own folder.

    Another person's folder gets an owner no principal has, so only the owner
    policy could ever allow it, and it never does. Team-owned roots have none.
    """

    parts = normalize_folder_id(folder_id).split("/")
    if parts[0] != FOLDER_ROOT_USERS:
        return None
    if task_person_id and parts[1] == person_folder_name(task_person_id):
        return task_person_id
    return _OTHER_PERSON_OWNER_PREFIX + parts[1]


__all__ = [
    "DataFolder",
    "FOLDER_ROOT_AGENTS",
    "FOLDER_ROOT_DATA",
    "FOLDER_ROOT_TEAM",
    "FOLDER_ROOT_USERS",
    "FolderGrantOnFileError",
    "FolderIdError",
    "FolderRoots",
    "PATH_SPELLING_MISMATCH",
    "PATH_SPELLING_UNVERIFIABLE",
    "PathSpellingError",
    "PathSpellingUnverifiableError",
    "STANDARD_SHARED_FOLDERS",
    "check_exact_spelling",
    "check_folder_id_spelling",
    "check_folder_target",
    "clear_spelling_cache",
    "covers",
    "data_folder_problem",
    "folder_chain",
    "folder_grant_file_refusal",
    "folder_owner",
    "grantable_folder_id",
    "is_person_folder",
    "normalize_folder_id",
    "paths_inside",
    "person_folder_id",
    "root_length",
]
