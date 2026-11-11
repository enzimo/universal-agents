"""A nats-server of the live test's own, on a port the server picks itself.

The protocol 1 live tests used to pick a free port, close it, and then start nats-server on
it. Two tests starting servers at the same moment could pick the same port: the second
server stopped at once with "address already in use", while its test's wait for the port
saw the first test's server answer and ran against it, sharing its buckets, its locks and
its lifetime (when that test ended, the other's requests waited out their deadlines). The
server now picks a free port itself (``-p -1``) and the test reads it from that server's own
log, once the server says it is ready, so a test only ever talks to its own server.
"""

from __future__ import annotations

import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from tests.owned_processes import stop_owned_process

_LISTENING = re.compile(r"Listening for client connections on 127\.0\.0\.1:(\d+)")
_READY = "Server is ready"


@contextmanager
def own_nats_server(executable: str, folder: Path, *arguments: str) -> Iterator[str]:
    """Run nats-server with JetStream in ``folder`` until the block ends; yields its URL."""

    with own_nats_server_process(executable, folder, *arguments) as (url, _process):
        yield url


@contextmanager
def own_nats_server_process(executable: str, folder: Path,
                            *arguments: str) -> Iterator[tuple[str, subprocess.Popen[bytes]]]:
    """``own_nats_server``, yielding the server's process too, for a test that pauses it (an outage)."""

    log_path = folder / "nats.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([executable, "-js", "-sd", str(folder / "store"), "-a", "127.0.0.1", "-p", "-1",
                                    *arguments], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                text = log_path.read_text()
                listening = _LISTENING.search(text)
                if listening is not None and _READY in text:
                    break
                if process.poll() is not None:
                    pytest.fail(f"NATS stopped while starting: {text}")
                if time.monotonic() >= deadline:
                    pytest.fail("NATS did not start within five seconds")
                time.sleep(0.02)
            yield f"nats://127.0.0.1:{listening.group(1)}", process
        finally:
            # Resumes a paused server, and kills one that does not stop in time, so a
            # hung shutdown neither leaves it running nor hides the test's own error.
            stop_owned_process(process, name=f"nats-server in {folder}")
