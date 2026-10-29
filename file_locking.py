"""Exclusive locks for files that several processes read, change and write back.

Why this exists: some instance files are shared by more than one process. Each
agent runs in its own process (``docker/launch_headless.py``), and the gateway's
onboarding server and ``assistant_cli.py`` open the same files. The model
provider sign-in stores are the sharpest case: an OpenAI Codex refresh token
works only once and xAI replaces its refresh token on every refresh, so two
processes refreshing at the same moment can leave the whole team signed out.
Holding one exclusive lock around "read, check, refresh, write" makes those
steps run in one process at a time.

There is deliberately no unlocked fallback. When the lock cannot be taken (for
example the folder is on a file system without lock support, where ``flock``
fails with ``ENOLCK`` or ``EOPNOTSUPP``), :func:`exclusive_file_lock` logs an
error and raises :class:`FileLockUnavailable`, and the protected work does not
run. Running it unlocked would replace a clear, fixable error with a race that
loses credentials (owner's decision, 2026-10-16). Callers that have their own
error type translate the error and keep its message.

How the lock works: ``flock`` on a separate ``<file>.lock`` next to the
protected file, so the protected file can still be replaced atomically while
the lock is held. A ``flock`` belongs to the open file, so two acquisitions in
one process (from two threads) also exclude each other, and the operating
system releases the lock when the holder exits, so a crash leaves no stale
lock. The same thread must not take the same lock twice: the second
acquisition opens a new file and waits for the first forever.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILE_SUFFIX = ".lock"
# The lock file holds no data; owner-only access keeps the folder tidy, like
# the protected files beside it.
_LOCK_FILE_MODE = 0o600
# Never follow a link planted at the lock file's name, and never pass the
# descriptor to child processes (a child holding it would hold the lock).
_LOCK_FILE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW


class FileLockUnavailable(RuntimeError):
    """The lock could not be taken, so the work it protects did not run."""

    def __init__(self, message: str, *, lock_path: Path, purpose: str, error_number: int | None) -> None:
        super().__init__(message)
        self.lock_path = lock_path
        self.purpose = purpose
        self.error_number = error_number


class FileLockCancelled(RuntimeError):
    """The caller stopped while waiting, so the protected work did not run."""


def lock_file_path(path: Path) -> Path:
    """The lock file that guards ``path``: the same name with ``.lock`` added."""

    return path.with_name(path.name + LOCK_FILE_SUFFIX)


@contextmanager
def exclusive_file_lock(
    path: Path, *, purpose: str, cancelled: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Hold an exclusive lock on ``<path>.lock`` while the body runs.

    Waits as long as another holder keeps the lock. ``purpose`` names the
    protected file in plain words (for example "xAI sign-in store") for the log
    and the error message. The lock file is created owner-only when missing;
    the folder must already exist. Raises :class:`FileLockUnavailable` without
    running the body when the lock file cannot be opened or locked.
    When ``cancelled`` is provided, check it while waiting and raise
    :class:`FileLockCancelled` before entering the body if it reports a stop.
    """

    lock_path = lock_file_path(path)
    descriptor = _open_locked(lock_path, purpose=purpose, cancelled=cancelled)
    try:
        yield
    finally:
        _release(descriptor, lock_path, purpose=purpose)


def _open_locked(lock_path: Path, *, purpose: str, cancelled: Callable[[], bool] | None = None) -> int:
    try:
        descriptor = os.open(lock_path, _LOCK_FILE_FLAGS, _LOCK_FILE_MODE)
    except OSError as exc:
        raise _lock_unavailable(lock_path, purpose=purpose, error=exc) from exc
    locked = False
    try:
        if cancelled is None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:
            _lock_until_stopped(descriptor, lock_path, purpose, cancelled)
        locked = True
    except OSError as exc:
        raise _lock_unavailable(lock_path, purpose=purpose, error=exc) from exc
    finally:
        if not locked:
            os.close(descriptor)
    return descriptor


def _lock_until_stopped(descriptor: int, lock_path: Path, purpose: str, cancelled: Callable[[], bool]) -> None:
    while not cancelled():
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(0.05)
    logger.info("File lock wait stopped. event=file_lock_cancelled purpose=%r path=%s", purpose, lock_path)
    raise FileLockCancelled(f"The {purpose} was left unchanged because the operation was stopped.")


def _release(descriptor: int, lock_path: Path, *, purpose: str) -> None:
    # Unlocking explicitly also frees the lock if a forked child still holds a
    # copy of the descriptor; closing alone frees it in every other case.
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning(
            "File lock release failed; closing the lock file releases it. event=file_lock_release_failed "
            "purpose=%r path=%s errno=%s error=%s",
            purpose,
            lock_path,
            _error_name(exc),
            exc.strerror or exc,
        )
    finally:
        os.close(descriptor)


def _lock_unavailable(lock_path: Path, *, purpose: str, error: OSError) -> FileLockUnavailable:
    reason = error.strerror or str(error)
    logger.error(
        "File lock unavailable; the work it protects did not run. event=file_lock_unavailable "
        "purpose=%r path=%s errno=%s error=%s",
        purpose,
        lock_path,
        _error_name(error),
        reason,
    )
    return FileLockUnavailable(
        f"The {purpose} could not be locked, so it was left unchanged. Lock file: {lock_path} ({reason}). "
        "Keep this folder on a writable file system that supports file locks; a local disk does, "
        "some network and synced folders do not.",
        lock_path=lock_path,
        purpose=purpose,
        error_number=error.errno,
    )


def _error_name(error: OSError) -> str:
    if error.errno is None:
        return "unknown"
    return errno.errorcode.get(error.errno, str(error.errno))
