"""Publish complete private broker files, without exposing interrupted writes.

Only trusted broker code uses this writer. Imports publish exclusively so a
credential created by another request is kept; ordinary saves atomically replace
the complete prior file. The open destination directory pins both operations,
and publishing never follows a destination link.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from access_control import safe_open

TEMPORARY_FILE_PREFIX = ".credential-write-"


def write_private_file(
    root: Path, parts: tuple[str, ...], content: bytes, *, exclusive: bool = False
) -> None:
    """Durably publish a 0600 file below a trusted root, never a partial value."""

    # This checks every component, including the filename, before making folders.
    existing = safe_open.entry_status(root, parts)
    if not parts:
        raise ValueError("A private file needs a filename.")
    if existing is not None:
        # Refuse linked or nonregular files even for ordinary replacement.
        with safe_open.opened_file(root, parts):
            pass
        if exclusive:
            raise FileExistsError(f"A destination credential already exists: {root.joinpath(*parts)}")
    directory = safe_open.open_directory(root, parts[:-1], create=True)
    temporary = f"{TEMPORARY_FILE_PREFIX}{secrets.token_hex(16)}"
    try:
        _sync_parent_directories(root, parts[:-1])
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        if exclusive:
            # link is atomic and refuses an existing name, including a link that
            # appeared after the initial check. Neither source nor target is followed.
            os.link(
                temporary, parts[-1],
                src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False,
            )
        else:
            os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.fsync(directory)
        finally:
            os.close(directory)


def _sync_parent_directories(root: Path, parts: tuple[str, ...]) -> None:
    """Persist newly created directory entries before publishing their file."""

    for depth in range(len(parts)):
        descriptor = safe_open.open_directory(root, parts[:depth])
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
