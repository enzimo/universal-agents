"""The shared-file lock (``file_locking``): real exclusion, and no unlocked fallback."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from file_locking import FileLockUnavailable, exclusive_file_lock, lock_file_path

# Another process tries to take the same lock without waiting.
_PROBE = """
import fcntl, os, sys
descriptor = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("blocked")
else:
    print("acquired")
"""


def _other_process_lock_attempt(lock_path: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(lock_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def _log_line(caplog: pytest.LogCaptureFixture, event: str) -> logging.LogRecord:
    return next(record for record in caplog.records if f"event={event}" in record.getMessage())


def test_lock_excludes_another_process_until_released(tmp_path: Path) -> None:
    store = tmp_path / "store.json"
    lock_path = tmp_path / "store.json.lock"
    assert lock_file_path(store) == lock_path

    with exclusive_file_lock(store, purpose="test store"):
        assert _other_process_lock_attempt(lock_path) == "blocked"

    assert _other_process_lock_attempt(lock_path) == "acquired"
    assert not store.exists()


def test_lock_failure_raises_and_never_runs_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr("file_locking.fcntl.flock", refuse)
    lock_path = tmp_path / "store.json.lock"
    body_ran = False

    with caplog.at_level(logging.ERROR, logger="file_locking"):
        with pytest.raises(FileLockUnavailable) as raised:
            with exclusive_file_lock(tmp_path / "store.json", purpose="test store"):
                body_ran = True

    assert body_ran is False
    assert raised.value.lock_path == lock_path
    assert raised.value.purpose == "test store"
    assert raised.value.error_number == errno.ENOLCK
    assert str(lock_path) in str(raised.value)
    assert "supports file locks" in str(raised.value)
    record = _log_line(caplog, "file_lock_unavailable")
    assert record.levelno == logging.ERROR
    assert f"path={lock_path}" in record.getMessage()
    assert "purpose='test store'" in record.getMessage()
    assert "errno=ENOLCK" in record.getMessage()


def test_lock_file_that_cannot_be_opened_is_refused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A link at the lock file's name is not followed; the work does not run."""

    store = tmp_path / "store.json"
    elsewhere = tmp_path / "elsewhere.lock"
    lock_file_path(store).symlink_to(elsewhere)
    body_ran = False

    with caplog.at_level(logging.ERROR, logger="file_locking"):
        with pytest.raises(FileLockUnavailable) as raised:
            with exclusive_file_lock(store, purpose="test store"):
                body_ran = True

    assert body_ran is False
    assert raised.value.error_number == errno.ELOOP
    assert not elsewhere.exists()
    assert "errno=ELOOP" in _log_line(caplog, "file_lock_unavailable").getMessage()


def test_lock_file_is_created_owner_only(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        with exclusive_file_lock(tmp_path / "store.json", purpose="test store"):
            pass
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE((tmp_path / "store.json.lock").stat().st_mode) == 0o600


def test_failed_release_is_logged_and_closing_still_frees_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real_flock = fcntl.flock

    def flock_with_failing_release(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_flock(descriptor, operation)

    monkeypatch.setattr("file_locking.fcntl.flock", flock_with_failing_release)
    body_ran = False

    with caplog.at_level(logging.WARNING, logger="file_locking"):
        with exclusive_file_lock(tmp_path / "store.json", purpose="test store"):
            body_ran = True

    assert body_ran is True
    record = _log_line(caplog, "file_lock_release_failed")
    assert record.levelno == logging.WARNING
    assert "errno=EIO" in record.getMessage()
    assert _other_process_lock_attempt(tmp_path / "store.json.lock") == "acquired"
