"""The trust kernel's private files: host and authority keys, the bootstrap bundle and kept trust records.

Every file is written whole and survives a power loss:
- the content goes to a temporary file in the same folder, created exclusively, never
  through a link, readable only by its owner (0600), and flushed to disk;
- it is then published under its name, replacing an older file, or, with
  ``exclusive``, only when no file has that name yet (``os.link`` refuses an existing
  name, so two processes creating one host's key at once keep the first one's);
- the folder is flushed so the new name survives too, and a temporary file left by
  a failed write is removed.

Readers see the old file or the new one, never a part. Folders this creates are
owner-only (0700) at every level, so ``trust/`` and ``trust/hosts/`` are as closed
as each host's own folder (plan 13B.3).

``read_private_file`` opens without following a link or blocking on a FIFO, and
refuses anything but a regular file readable only by its owner.

The broker has its own writer (``secure_tool_broker.private_files``), which opens
folders below a trusted root through ``access_control.safe_open``; the trust folder is
the launcher's, not an agent-reachable one, and needs owner-only folders, so it uses
this one.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

TEMPORARY_PREFIX = ".trust-write-"
_FOLDER_MODE = 0o700
_FILE_MODE = 0o600
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class NotAPrivateFile(ValueError):
    """The path names something other than a regular file readable only by its owner."""


def ensure_private_folder(folder: Path) -> None:
    """Create ``folder`` and every missing folder above it, each owner-only (0700)."""

    missing: list[Path] = []
    current = folder
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            os.mkdir(path, _FOLDER_MODE)
        except FileExistsError:
            continue  # another process made it first; it is checked when a file in it is opened
        # mkdir's mode is narrowed by the process umask; set it exactly.
        os.chmod(path, _FOLDER_MODE)


def sync_folder(folder: Path) -> None:
    """Flush a folder's entries, so a file published or removed in it survives a power loss."""

    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_private_file(path: Path, content: bytes, *, exclusive: bool = False) -> bool:
    """Publish ``content`` as a complete 0600 file; with ``exclusive``, only if none exists.

    Returns whether this call published it (always True without ``exclusive``).
    """

    ensure_private_folder(path.parent)
    temporary = path.with_name(f"{TEMPORARY_PREFIX}{secrets.token_hex(8)}")
    descriptor = os.open(temporary, _WRITE_FLAGS, _FILE_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                return False
        else:
            os.replace(temporary, path)
        sync_folder(path.parent)
        return True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass  # os.replace moved it


def read_private_file(path: Path) -> bytes:
    """The bytes of a private file.

    Raises ``FileNotFoundError`` when it is missing, ``NotAPrivateFile`` when it is a
    folder, a link, a device or readable by others, and ``OSError`` when it cannot be read.
    """

    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as error:
        if isinstance(error, FileNotFoundError):
            raise
        # O_NOFOLLOW fails with ELOOP on a link (EMLINK on some BSDs).
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise NotAPrivateFile(f"{path} is a link") from error
        raise
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise NotAPrivateFile(f"{path} is not a regular file")
        if mode & 0o077:
            raise NotAPrivateFile(f"{path} is readable by others than its owner")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
