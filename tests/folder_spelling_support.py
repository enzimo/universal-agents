"""Shared test helpers for simulating case-insensitive and Unicode-normalizing filesystems.

This host's ext4 filesystem is case-sensitive and does not perform Unicode
normalization. ``SimulatedFS`` wraps real temporary directories on ext4 through
``access_control.folders._SpellingFS`` so production probe, caching, and
link-free walk logic execute end to end without needing a macOS or Docker
Desktop bind mount.
"""

from __future__ import annotations

import os
import stat
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

from access_control import folders, safe_open

_DIR_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _match_key(name: str, *, case_insensitive: bool, unicode_normalizing: bool) -> str:
    # Real APFS/NTFS fold ASCII/simple letter case (using .lower(), not
    # .casefold() which expands 'ß' -> 'ss' and 'ﬁ' -> 'fi'). Matching that
    # rule faithfully reproduces why str.swapcase() fails on 'Straße'/'ﬁle'.
    key = unicodedata.normalize("NFC", name) if unicode_normalizing else name
    return key.lower() if case_insensitive else key


class SimulatedFS:
    """A configurable ``_SpellingFS`` over real temp directories."""

    def __init__(
        self,
        *,
        case_insensitive: bool = True,
        unicode_normalizing: bool = False,
        now_ns: Callable[[], int] = time.time_ns,
        on_stat: Callable[[str, int], None] | None = None,
        fstat_override: Callable[[int, os.stat_result], os.stat_result] | None = None,
        stat_override: Callable[[str, int, os.stat_result], os.stat_result] | None = None,
        case_insensitive_for_fd: Callable[[int], bool] | None = None,
    ) -> None:
        self.case_insensitive = case_insensitive
        self.unicode_normalizing = unicode_normalizing
        self._now_ns = now_ns
        self._on_stat = on_stat
        self._fstat_override = fstat_override
        self._stat_override = stat_override
        self._case_insensitive_for_fd = case_insensitive_for_fd
        self.stat_calls: list[str] = []
        self.open_dir_calls: list[str] = []
        self.fstat_calls = 0
        self.list_calls = 0

    def _is_ci(self, dir_fd: int) -> bool:
        if self._case_insensitive_for_fd is not None:
            return self._case_insensitive_for_fd(dir_fd)
        return self.case_insensitive

    def _resolve_real_name(self, name: str, dir_fd: int) -> str | None:
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            return name
        except FileNotFoundError:
            pass
        ci = self._is_ci(dir_fd)
        if not ci and not self.unicode_normalizing:
            return None
        target = _match_key(name, case_insensitive=ci, unicode_normalizing=self.unicode_normalizing)
        for entry in sorted(os.listdir(dir_fd)):
            if _match_key(entry, case_insensitive=ci, unicode_normalizing=self.unicode_normalizing) == target:
                return entry
        return None

    def open_root(self, root: Path) -> int:
        return safe_open.open_root(root)

    def open_dir(self, name: str, dir_fd: int) -> int:
        self.open_dir_calls.append(name)
        real_name = self._resolve_real_name(name, dir_fd)
        if real_name is None:
            raise FileNotFoundError(name)
        return os.open(real_name, _DIR_FLAGS, dir_fd=dir_fd)

    def fstat_dir(self, dir_fd: int) -> os.stat_result:
        self.fstat_calls += 1
        info = os.fstat(dir_fd)
        if self._fstat_override is not None:
            return self._fstat_override(dir_fd, info)
        return info

    def stat_entry(self, name: str, dir_fd: int) -> os.stat_result:
        self.stat_calls.append(name)
        if self._on_stat is not None:
            self._on_stat(name, dir_fd)
        real_name = self._resolve_real_name(name, dir_fd)
        if real_name is None:
            raise FileNotFoundError(name)
        info = os.stat(real_name, dir_fd=dir_fd, follow_symlinks=False)
        if self._stat_override is not None:
            return self._stat_override(name, dir_fd, info)
        return info

    def list_entries(self, dir_fd: int) -> list[tuple[str, int | None]]:
        self.list_calls += 1
        with os.scandir(dir_fd) as entries:
            return [(entry.name, entry.inode()) for entry in entries]

    def close_fd(self, fd: int) -> None:
        os.close(fd)

    def as_spelling_fs(self) -> folders._SpellingFS:
        return folders._SpellingFS(
            open_root=self.open_root,
            open_dir=self.open_dir,
            fstat_dir=self.fstat_dir,
            stat_entry=self.stat_entry,
            list_entries=self.list_entries,
            close_fd=self.close_fd,
            now_ns=self._now_ns,
        )


def fake_stat_with_dev(info: os.stat_result, st_dev: int) -> os.stat_result:
    """Copy an ``os.stat_result`` with a synthetic ``st_dev`` for mount-point tests."""

    values = list(info)
    values[stat.ST_DEV] = st_dev
    return os.stat_result(
        values,
        {
            "st_atime_ns": info.st_atime_ns,
            "st_mtime_ns": info.st_mtime_ns,
            "st_ctime_ns": info.st_ctime_ns,
        },
    )


class CountingPosixFS:
    """Real POSIX ``_SpellingFS`` wrapper that counts directory listings and stats."""

    def __init__(self) -> None:
        self.stat_calls: list[str] = []
        self.list_calls = 0

    def _stat_entry(self, name: str, dir_fd: int) -> os.stat_result:
        self.stat_calls.append(name)
        return folders._POSIX_FS.stat_entry(name, dir_fd)

    def _list_entries(self, dir_fd: int) -> list[tuple[str, int | None]]:
        self.list_calls += 1
        return folders._POSIX_FS.list_entries(dir_fd)

    def as_spelling_fs(self) -> folders._SpellingFS:
        return folders._SpellingFS(
            open_root=folders._POSIX_FS.open_root,
            open_dir=folders._POSIX_FS.open_dir,
            fstat_dir=folders._POSIX_FS.fstat_dir,
            stat_entry=self._stat_entry,
            list_entries=self._list_entries,
            close_fd=folders._POSIX_FS.close_fd,
            now_ns=folders._POSIX_FS.now_ns,
        )
