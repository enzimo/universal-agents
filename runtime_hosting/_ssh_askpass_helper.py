#!/usr/bin/env python3
"""One-shot SSH_ASKPASS helper for broker-owned ssh-add and password bootstrap.

This script is tracked inside the ``runtime_hosting`` package so no executable
file is ever written into the instance's ephemeral runtime directory (keeping
``noexec`` instance mounts working) and no session-folder path ever appears on
a process command line.

Modes (selected via broker-private environment variables):
- ``UA_SSH_ASKPASS_FD``: read one secret from an inherited pipe file descriptor
  (used by ``ssh-add`` when unlocking passphrase-protected keys).
- ``UA_SSH_ASKPASS_FIFO``: read one secret from a relative named pipe inside
  the broker-private session working directory (used by ``ssh`` password
  bootstrap, because ``ssh`` closes file descriptors above stderr before
  executing ``SSH_ASKPASS``).
"""

from __future__ import annotations

import os
import stat
import sys


def _read_from_fd(raw_fd: str) -> bytes:
    fd = int(raw_fd)
    if fd < 3:
        raise ValueError("UA_SSH_ASKPASS_FD must reference an inherited pipe descriptor >= 3.")
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _read_from_fifo(fifo_path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(fifo_path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISFIFO(info.st_mode):
            raise PermissionError("UA_SSH_ASKPASS_FIFO must reference a named pipe.")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def main() -> int:
    raw_fd = os.environ.get("UA_SSH_ASKPASS_FD")
    fifo_path = os.environ.get("UA_SSH_ASKPASS_FIFO")
    if raw_fd:
        payload = _read_from_fd(raw_fd)
    elif fifo_path:
        payload = _read_from_fifo(fifo_path)
    else:
        return 1
    sys.stdout.write(payload.decode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
