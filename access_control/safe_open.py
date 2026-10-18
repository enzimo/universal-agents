"""Open files below a trusted folder without following links.

Agents' code may create symbolic links in the folders it can write, and it runs
at the same time as trusted code. A tool that checks a path and then opens it
by name can therefore be sent somewhere else between the check and the open,
or through a link planted beforehand. The rules here close both:

- a path is opened from a trusted root one component at a time, relative to
  the folder opened before it, with ``O_NOFOLLOW``, so a link anywhere on the
  path fails instead of being followed, whenever it appeared;
- only regular files with exactly one link are read or replaced, so a hard
  link cannot expose or change a file somewhere else;
- new files are created with ``O_EXCL`` and existing ones are replaced in
  place through the checked descriptor; folders are created with ``mkdirat``.

Callers decide which root and which path are allowed; this module only
guarantees that the file opened is the one below that root. The agent file
library (``custom_tools.file_access``), the broker's staged CLI files
(``secure_tool_broker.task_files``), the gateway workspace browser and the
holding of a removed person's folder (``access_control.held_person_folders``)
and the deletion of a removed person's credentials folder
(``secure_tool_broker.credential_state``) use it.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

REASON_NOT_FOUND = "not_found"
REASON_LINK = "link"
REASON_NOT_DIRECTORY = "not_directory"
REASON_NOT_REGULAR_FILE = "not_regular_file"
REASON_SEVERAL_LINKS = "several_links"
REASON_TOO_LARGE = "too_large"
REASON_UNSAFE_NAME = "unsafe_name"
REASON_ROOT_UNAVAILABLE = "root_unavailable"
REASON_OPEN_FAILED = "open_failed"
REASON_DESTINATION_EXISTS = "destination_exists"

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_WRITE_FLAGS = os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_NEW_FILE_MODE = 0o644
_NEW_DIRECTORY_MODE = 0o777  # narrowed by the process umask, as mkdir does
_COPY_CHUNK = 1024 * 1024


class UnsafePath(OSError):
    """A path below a root cannot be opened safely; ``reason`` says why."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _checked_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\x00" in part:
            raise UnsafePath(f"Unsafe path component: {part!r}", reason=REASON_UNSAFE_NAME)
    return parts


def _display(root: Path, parts: tuple[str, ...]) -> Path:
    return root.joinpath(*parts)


def _is_link(name: str, directory: int) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open_error(exc: OSError, name: str, directory: int, shown: Path) -> UnsafePath:
    """Translate an ``open`` failure into a reason a person can act on."""

    if exc.errno == errno.ENOENT:
        return UnsafePath(f"Not found: {shown}", reason=REASON_NOT_FOUND)
    # O_NOFOLLOW reports a link as ELOOP, or as ENOTDIR when a folder was expected.
    if exc.errno == errno.ELOOP or (exc.errno == errno.ENOTDIR and _is_link(name, directory)):
        return UnsafePath(f"A link on this path is not followed: {shown}", reason=REASON_LINK)
    if exc.errno == errno.ENOTDIR:
        return UnsafePath(f"Not a folder: {shown}", reason=REASON_NOT_DIRECTORY)
    return UnsafePath(f"Cannot open {shown}: {exc.strerror}", reason=REASON_OPEN_FAILED)


def open_root(root: Path) -> int:
    """Open a trusted root folder. The root itself may be reached through links."""

    try:
        return os.open(root, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    except OSError as exc:
        raise UnsafePath(f"The folder is unavailable: {root}", reason=REASON_ROOT_UNAVAILABLE) from exc


def open_directory(root: Path, parts: tuple[str, ...], *, create: bool = False) -> int:
    """Open ``root/parts`` as a folder without following links; the caller closes it.

    With ``create`` missing folders are made, never through a link.
    """

    parts = _checked_parts(parts)
    current = open_root(root)
    try:
        for index, part in enumerate(parts):
            shown = _display(root, parts[: index + 1])
            if create:
                try:
                    os.mkdir(part, _NEW_DIRECTORY_MODE, dir_fd=current)
                except FileExistsError:
                    pass  # an existing folder is opened below; a link fails there
                except OSError as exc:
                    raise UnsafePath(f"Cannot create {shown}: {exc.strerror}", reason=REASON_OPEN_FAILED) from exc
            try:
                following = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as exc:
                raise _open_error(exc, part, current, shown) from exc
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _require_single_link_regular(descriptor: int, shown: Path) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePath(f"Not a regular file: {shown}", reason=REASON_NOT_REGULAR_FILE)
    if info.st_nlink != 1:
        raise UnsafePath(
            f"Files with more than one hard link are not opened: {shown}. Copy it to a new file first.",
            reason=REASON_SEVERAL_LINKS,
        )
    return info


def _split(parts: tuple[str, ...], root: Path) -> tuple[tuple[str, ...], str]:
    if not parts:
        raise UnsafePath(f"A folder is not a file: {root}", reason=REASON_NOT_REGULAR_FILE)
    return parts[:-1], parts[-1]


@contextmanager
def opened_file(root: Path, parts: tuple[str, ...]) -> Iterator[tuple[int, os.stat_result]]:
    """A read descriptor for the one-link regular file ``root/parts``, and its status."""

    folder_parts, name = _split(_checked_parts(parts), root)
    shown = _display(root, parts)
    directory = open_directory(root, folder_parts)
    try:
        try:
            descriptor = os.open(name, _FILE_READ_FLAGS, dir_fd=directory)
        except OSError as exc:
            raise _open_error(exc, name, directory, shown) from exc
    finally:
        os.close(directory)
    try:
        info = _require_single_link_regular(descriptor, shown)
        yield descriptor, info
    finally:
        os.close(descriptor)


def read_bytes(root: Path, parts: tuple[str, ...], *, max_bytes: int | None = None) -> bytes:
    """The contents of ``root/parts``; larger than ``max_bytes`` is refused."""

    too_large = UnsafePath(
        f"The file is larger than {max_bytes} bytes: {_display(root, parts)}", reason=REASON_TOO_LARGE
    )
    with opened_file(root, parts) as (descriptor, info):
        if max_bytes is not None and info.st_size > max_bytes:
            raise too_large
        with os.fdopen(os.dup(descriptor), "rb") as reader:
            if max_bytes is None:
                return reader.read()
            data = reader.read(max_bytes + 1)  # it may have grown since the size check
    if len(data) > max_bytes:
        raise too_large
    return data


def entry_status(root: Path, parts: tuple[str, ...]) -> os.stat_result | None:
    """The status of ``root/parts`` itself (a link is reported, not followed); None if missing."""

    if not parts:
        descriptor = open_root(root)
        try:
            return os.fstat(descriptor)
        finally:
            os.close(descriptor)
    folder_parts, name = _split(_checked_parts(parts), root)
    try:
        directory = open_directory(root, folder_parts)
    except UnsafePath as exc:
        if exc.reason == REASON_NOT_FOUND:
            return None
        raise
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    finally:
        os.close(directory)


@contextmanager
def writable_file(
    root: Path, parts: tuple[str, ...], *, create_directories: bool = False, exclusive: bool = False
) -> Iterator[BinaryIO]:
    """A binary writer for ``root/parts``: a new file, or an existing one replaced in place.

    An existing file must be a one-link regular file; with ``exclusive`` it
    must not exist at all.
    """

    folder_parts, name = _split(_checked_parts(parts), root)
    shown = _display(root, parts)
    directory = open_directory(root, folder_parts, create=create_directories)
    try:
        descriptor = _open_for_write(directory, name, shown, exclusive=exclusive)
    finally:
        os.close(directory)
    with os.fdopen(descriptor, "wb") as writer:
        yield writer


def _open_for_write(directory: int, name: str, shown: Path, *, exclusive: bool) -> int:
    try:
        return os.open(name, _FILE_WRITE_FLAGS | os.O_CREAT | os.O_EXCL, _NEW_FILE_MODE, dir_fd=directory)
    except FileExistsError:
        if exclusive:
            raise UnsafePath(f"Already exists: {shown}", reason=REASON_OPEN_FAILED) from None
    except OSError as exc:
        raise _open_error(exc, name, directory, shown) from exc
    # It exists: replace it in place, but only a one-link regular file, never a link.
    try:
        descriptor = os.open(name, _FILE_WRITE_FLAGS, dir_fd=directory)
    except OSError as exc:
        raise _open_error(exc, name, directory, shown) from exc
    try:
        _require_single_link_regular(descriptor, shown)
        os.ftruncate(descriptor, 0)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def write_bytes(root: Path, parts: tuple[str, ...], data: bytes, *, create_directories: bool = False) -> int:
    """Write ``data`` to ``root/parts``; returns the number of bytes written."""

    with writable_file(root, parts, create_directories=create_directories) as writer:
        writer.write(data)
    return len(data)


def copy_file(root: Path, parts: tuple[str, ...], destination: Path, *, max_bytes: int | None = None) -> int:
    """Copy ``root/parts`` to a new file at ``destination`` (a trusted path)."""

    with opened_file(root, parts) as (descriptor, info):
        if max_bytes is not None and info.st_size > max_bytes:
            raise UnsafePath(
                f"The file is larger than {max_bytes} bytes: {_display(root, parts)}", reason=REASON_TOO_LARGE
            )
        copied = 0
        with os.fdopen(os.dup(descriptor), "rb") as reader, open(destination, "xb") as writer:
            while chunk := reader.read(_COPY_CHUNK):
                copied += len(chunk)
                if max_bytes is not None and copied > max_bytes:
                    raise UnsafePath(
                        f"The file grew past {max_bytes} bytes while it was copied: {_display(root, parts)}",
                        reason=REASON_TOO_LARGE,
                    )
                writer.write(chunk)
        return copied


def remove_file(
    root: Path, parts: tuple[str, ...], *, expected_identity: tuple[int, int, int, int] | None = None
) -> None:
    """Remove the regular file ``root/parts`` (never a link, a folder, or a path through a link)."""

    folder_parts, name = _split(_checked_parts(parts), root)
    shown = _display(root, parts)
    directory = open_directory(root, folder_parts)
    try:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise UnsafePath(f"Not found: {shown}", reason=REASON_NOT_FOUND) from exc
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePath(f"A link is not removed through this path: {shown}", reason=REASON_LINK)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath(f"Not a regular file: {shown}", reason=REASON_NOT_REGULAR_FILE)
        if expected_identity is not None and (
            info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns
        ) != expected_identity:
            return  # changed after the caller checked its ownership/content
        os.unlink(name, dir_fd=directory)
    finally:
        os.close(directory)


def move_directory(
    source_root: Path,
    source_parts: tuple[str, ...],
    destination_root: Path,
    destination_parts: tuple[str, ...],
) -> None:
    """Move the folder ``source_root/source_parts`` to ``destination_root/destination_parts`` in one rename.

    Both parent folders are opened without following links (the destination's
    are created), the source must be a real folder, not a link, and the move
    is a single ``rename`` of the folder entry, so nothing inside the folder
    is opened or followed and a link planted inside moves as a link. The
    destination must not exist, or be an empty folder. Both roots must be on
    the same file system.
    """

    source_folder, source_name = _split(_checked_parts(source_parts), source_root)
    destination_folder, destination_name = _split(_checked_parts(destination_parts), destination_root)
    shown = _display(source_root, source_parts)
    source = open_directory(source_root, source_folder)
    try:
        try:
            info = os.stat(source_name, dir_fd=source, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise UnsafePath(f"Not found: {shown}", reason=REASON_NOT_FOUND) from exc
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePath(f"A link is not moved as a folder: {shown}", reason=REASON_LINK)
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafePath(f"Not a folder: {shown}", reason=REASON_NOT_DIRECTORY)
        destination = open_directory(destination_root, destination_folder, create=True)
        try:
            os.rename(source_name, destination_name, src_dir_fd=source, dst_dir_fd=destination)
        except OSError as exc:
            target = _display(destination_root, destination_parts)
            reason = REASON_DESTINATION_EXISTS if exc.errno in {errno.ENOTEMPTY, errno.EEXIST} else REASON_OPEN_FAILED
            raise UnsafePath(f"Cannot move {shown} to {target}: {exc.strerror}", reason=reason) from exc
        finally:
            os.close(destination)
    finally:
        os.close(source)


def remove_directory(root: Path, parts: tuple[str, ...]) -> bool:
    """Delete the folder ``root/parts`` and everything in it; False when it does not exist.

    The parent folders are opened without following links and the folder must
    be a real folder: a link in its place is refused, not removed, so the
    caller hears about it. The removal walks by file descriptor
    (``shutil.rmtree`` with ``dir_fd``), so a link inside is deleted as a link
    and what it points at is never touched.
    """

    if not shutil.rmtree.avoids_symlink_attacks:
        # Every supported platform walks by descriptor; refuse rather than risk following a link.
        raise RuntimeError("This platform cannot delete a folder without following links.")
    folder_parts, name = _split(_checked_parts(parts), root)
    shown = _display(root, parts)
    try:
        directory = open_directory(root, folder_parts)
    except UnsafePath as exc:
        if exc.reason == REASON_NOT_FOUND:
            return False
        raise
    try:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            raise UnsafePath(f"A link is not deleted as a folder: {shown}", reason=REASON_LINK)
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafePath(f"Not a folder: {shown}", reason=REASON_NOT_DIRECTORY)
        shutil.rmtree(name, dir_fd=directory)
        return True
    finally:
        os.close(directory)

def list_directory(root: Path, parts: tuple[str, ...]) -> list[tuple[str, os.stat_result]]:
    """Entries of the folder ``root/parts`` with their own status (links not followed)."""

    directory = open_directory(root, parts)
    try:
        entries: list[tuple[str, os.stat_result]] = []
        for name in os.listdir(directory):
            try:
                entries.append((name, os.stat(name, dir_fd=directory, follow_symlinks=False)))
            except OSError:
                continue  # removed while listing
        return entries
    finally:
        os.close(directory)


__all__ = [
    "REASON_DESTINATION_EXISTS",
    "REASON_LINK",
    "REASON_NOT_DIRECTORY",
    "REASON_NOT_FOUND",
    "REASON_NOT_REGULAR_FILE",
    "REASON_OPEN_FAILED",
    "REASON_ROOT_UNAVAILABLE",
    "REASON_SEVERAL_LINKS",
    "REASON_TOO_LARGE",
    "REASON_UNSAFE_NAME",
    "UnsafePath",
    "copy_file",
    "entry_status",
    "list_directory",
    "move_directory",
    "open_directory",
    "open_root",
    "opened_file",
    "read_bytes",
    "remove_directory",
    "remove_file",
    "writable_file",
    "write_bytes",
]
