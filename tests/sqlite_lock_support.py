"""A SQLite lock held for a set time, for tests of code that waits for it.

Tests of how the stores wait for a locked database take the database's
exclusive file lock in a connection of their own and release it after a set
time, standing in for another process (for example one checkpointing the
write-ahead log). The release must not depend on how fast the disk is. With a
plain connection it does: the commit flushes the write-ahead log (WAL) to disk,
and closing the last connection to a WAL database checkpoints the WAL into the
database, flushing both, and deletes it, all while the lock is still held. On a
slow CI disk (about 0.7 s per flush) those flushes kept the lock past the
waiting call's single lock wait (``busy_timeout``, 2.5 s for the stores), and
the tests failed with "database is locked" although the code they check was
right.

The holder therefore does no disk work of its own:

- it keeps the WAL on close (``SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE``), as the
  stores' own connections do (``storage.sqlite_connection._keep_wal_on_close``);
- it does not flush its commit (``synchronous=OFF``). Only this test connection
  skips flushes; the store code that waits keeps its own settings.

The lock then lasts exactly as long as the test holds it, on any disk.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from storage.sqlite_connection import close_held_open_databases


def hold_exclusive_lock(db_path: str | Path) -> sqlite3.Connection:
    """Take the exclusive file lock every other connection to ``db_path`` must wait for.

    Returns the holder inside an open ``BEGIN EXCLUSIVE`` transaction; the
    caller may write in it and must end it with ``release_exclusive_lock``.
    """

    # Exclusive locking mode cannot start while any connection to the file is
    # open, so the idle connections this process holds are closed first: the
    # lock stands for another process that has none of ours.
    close_held_open_databases()
    holder = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    holder.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    holder.execute("PRAGMA synchronous=OFF")
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def release_exclusive_lock(holder: sqlite3.Connection) -> None:
    """Commit and close the holder; neither step flushes or checkpoints (module docstring)."""

    holder.execute("COMMIT")
    holder.close()
