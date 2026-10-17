"""The programs of installed CLIs: where they live, how they are fetched, and how a run gets a checked copy.

An installed CLI's program lives in the team workspace at
``team/tools/<name>/<file>`` (``custom_messages.contracts.cli_tool_v1``), a
folder agents can write. Two trusted callers use this module:

- the gateway, when an administrator approves the proposal
  (``access_control.proposals``): it checks the program already in the
  workspace against the approved SHA-256, or downloads it from the approved
  https address (size and time limited, redirects only to https), takes the
  one file out of a ``.tar.gz`` or ``.zip`` when the proposal names it, checks
  the digest, and writes it without following links;
- the broker, on every run (``secure_tool_broker.installed_clis``): it copies
  the workspace file into the run's private folder without following links
  and checks the copy's digest, so a program an agent swapped after the
  approval never runs.

Files are always opened with ``access_control.safe_open``: from the workspaces
root one component at a time, never through a link, one-link regular files
only.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import logging
import os
import socket
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from access_control import safe_open
from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

MAX_PROGRAM_BYTES = 200 * 1024 * 1024
# How long one network step (connecting, one read) may wait for the server.
# It does not bound a whole download: a server that keeps sending a little
# never trips it.
DOWNLOAD_TIMEOUT_SECONDS = 120
# How long a whole download may take, from connecting to its last byte,
# redirects, TLS and headers included (``_DownloadDeadline``). An approving
# decision renews its claim on the request before and after each download, so
# this must stay well under the claim's limit
# (access_control.requests.DECISION_CLAIM_SECONDS, 600).
DOWNLOAD_DEADLINE_SECONDS = 300
# Redirects a download may follow (all https).
MAX_DOWNLOAD_REDIRECTS = 5
_CHUNK = 1024 * 1024
REASON_DIGEST_MISMATCH = "cli_program_digest_mismatch"
REASON_PROGRAM_MISSING = "cli_program_missing"
REASON_PROGRAM_UNSAFE = "cli_program_unsafe"
REASON_DOWNLOAD_FAILED = "cli_download_failed"

Downloader = Callable[[str], bytes]


class ProgramRefused(ValueError):
    """The program cannot be installed or run; ``message`` says why in plain words."""

    def __init__(self, message: str, *, reason_code: str, actual_sha256: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.actual_sha256 = actual_sha256


def workspaces_root(instance_root: str | Path) -> Path:
    """The folder that holds ``team/`` (every path here is relative to it)."""

    return InstanceLayout.at(instance_root).workspaces_dir


def _digest_of_descriptor(descriptor: int, *, max_bytes: int) -> str:
    digest = hashlib.sha256()
    read = 0
    with os.fdopen(os.dup(descriptor), "rb") as reader:
        while chunk := reader.read(_CHUNK):
            read += len(chunk)
            if read > max_bytes:
                raise ProgramRefused("The program is larger than the size limit.", reason_code=REASON_PROGRAM_UNSAFE)
            digest.update(chunk)
    return digest.hexdigest()


def workspace_digest(root: Path, parts: tuple[str, ...], *, max_bytes: int = MAX_PROGRAM_BYTES) -> str | None:
    """The SHA-256 of the program in the workspace, or None when there is none (never through a link)."""

    if not root.is_dir() or safe_open.entry_status(root, parts) is None:
        return None
    try:
        with safe_open.opened_file(root, parts) as (descriptor, _info):
            return _digest_of_descriptor(descriptor, max_bytes=max_bytes)
    except safe_open.UnsafePath as exc:
        raise ProgramRefused(
            f"The program at {'/'.join(parts)} cannot be used: {exc}", reason_code=REASON_PROGRAM_UNSAFE
        ) from exc


class _HttpsOnlyRedirects(HTTPRedirectHandler):
    max_redirections = MAX_DOWNLOAD_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlsplit(newurl).scheme != "https":
            raise URLError(f"A redirect left https: {urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _DownloadDeadline:
    """One deadline for a whole download, from connecting to its last byte.

    A socket timeout bounds one blocking step only: a server that answers
    slowly to connecting, TLS, each of several redirects or the headers, or
    that keeps sending the body a little at a time, never trips it. So a
    timer shuts down every connection of the download when the deadline
    passes, which ends whatever step is waiting, and each new connection
    gets at most the time left. The clock decides; the server cannot extend it.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.expired = False
        self._ends = time.monotonic() + seconds
        self._connections: list[http.client.HTTPConnection] = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def __enter__(self) -> "_DownloadDeadline":
        self._timer.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._timer.cancel()

    def remaining(self) -> float:
        return max(0.0, self._ends - time.monotonic())

    def track(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            if self.expired:
                raise TimeoutError("the download deadline passed")
            self._connections.append(connection)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            connections = list(self._connections)
        for connection in connections:
            sock = getattr(connection, "sock", None)
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed


class _DeadlineHTTPSHandler(HTTPSHandler):
    """Opens each connection of a download (redirects too) under its deadline."""

    def __init__(self, deadline: _DownloadDeadline) -> None:
        super().__init__()
        self._deadline = deadline

    def https_open(self, req: Request) -> Any:
        deadline = self._deadline

        def connection(host: str, **kwargs: Any) -> http.client.HTTPSConnection:
            timeout = min(float(kwargs.pop("timeout", None) or DOWNLOAD_TIMEOUT_SECONDS), deadline.remaining())
            opened = http.client.HTTPSConnection(host, timeout=max(timeout, 0.001), **kwargs)
            deadline.track(opened)
            return opened

        # No context given: http.client uses its default, certificate-checking one.
        return self.do_open(connection, req)


def fetch_https(url: str) -> bytes:
    """Download ``url`` (https only, redirects too), at most ``MAX_PROGRAM_BYTES`` and ``DOWNLOAD_DEADLINE_SECONDS``."""

    if urlsplit(url).scheme != "https":
        raise ProgramRefused("A program is downloaded only over https.", reason_code=REASON_DOWNLOAD_FAILED)
    host = urlsplit(url).hostname or url
    with _DownloadDeadline(DOWNLOAD_DEADLINE_SECONDS) as deadline:
        opener = build_opener(_HttpsOnlyRedirects(), _DeadlineHTTPSHandler(deadline))
        try:
            with opener.open(Request(url, headers={"User-Agent": "universal-agents"}), timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                data = _read_body(response, deadline)
        except ProgramRefused:
            raise
        except (URLError, OSError, ValueError, http.client.HTTPException) as exc:
            if deadline.expired:
                raise _too_slow(host, deadline) from exc
            raise ProgramRefused(
                f"The program could not be downloaded from {host}: {exc}", reason_code=REASON_DOWNLOAD_FAILED
            ) from exc
        if deadline.expired:
            raise _too_slow(host, deadline)  # the shutdown may end a read as if the body were complete
    if len(data) > MAX_PROGRAM_BYTES:
        raise ProgramRefused("The download is larger than the size limit.", reason_code=REASON_DOWNLOAD_FAILED)
    return data


def _read_body(response: Any, deadline: _DownloadDeadline) -> bytes:
    """At most ``MAX_PROGRAM_BYTES + 1`` bytes; ``read1`` returns what arrived, so the deadline check runs often."""

    chunks: list[bytes] = []
    size = 0
    while size <= MAX_PROGRAM_BYTES and not deadline.expired:
        chunk = response.read1(min(_CHUNK, MAX_PROGRAM_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _too_slow(host: str, deadline: _DownloadDeadline) -> ProgramRefused:
    logger.warning(
        "A program download took too long and was stopped. event=cli_download_deadline host=%s seconds=%s",
        host,
        deadline.seconds,
    )
    return ProgramRefused(
        f"The download from {host} was stopped after {deadline.seconds:g} seconds in total (connecting, "
        "redirects and the whole file count) because it was too slow. Put the program into the workspace "
        "yourself, or propose a download address that answers faster.",
        reason_code=REASON_DOWNLOAD_FAILED,
    )


def extract_member(data: bytes, member: str) -> bytes:
    """The one regular file ``member`` of a ``.tar.gz`` or ``.zip`` download."""

    try:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.getinfo(member)
                if info.is_dir() or info.file_size > MAX_PROGRAM_BYTES:
                    raise KeyError(member)
                return archive.read(info)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            entry = archive.getmember(member)
            if not entry.isreg() or entry.size > MAX_PROGRAM_BYTES:
                raise KeyError(member)
            extracted = archive.extractfile(entry)
            assert extracted is not None
            return extracted.read()
    except (KeyError, zipfile.BadZipFile, tarfile.TarError) as exc:
        raise ProgramRefused(
            f"The download has no program file at {member!r}.", reason_code=REASON_DOWNLOAD_FAILED
        ) from exc


def _mismatch(parts: tuple[str, ...], expected: str, actual: str) -> ProgramRefused:
    return ProgramRefused(
        f"The program at {'/'.join(parts)} is not the approved one (its SHA-256 is {actual[:12]}..., "
        f"the approved one is {expected[:12]}...). Propose the update with its new digest.",
        reason_code=REASON_DIGEST_MISMATCH,
        actual_sha256=actual,
    )


def install_program(root: Path, parts: tuple[str, ...], data: bytes, sha256: str) -> None:
    """Write a downloaded program into the workspace after checking its digest (never over an existing file)."""

    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise _mismatch(parts, sha256, actual)
    root.mkdir(parents=True, exist_ok=True)  # the instance's own workspaces folder
    try:
        with safe_open.writable_file(root, parts, create_directories=True, exclusive=True) as writer:
            writer.write(data)
            os.fchmod(writer.fileno(), 0o755)
    except safe_open.UnsafePath as exc:
        raise ProgramRefused(
            f"The program could not be written to {'/'.join(parts)}: {exc}", reason_code=REASON_PROGRAM_UNSAFE
        ) from exc
    logger.warning(
        "An installed CLI's program was written to the team workspace. event=cli_program_installed path=%s sha256=%s",
        "/".join(parts),
        sha256,
    )


def remove_program(root: Path, parts: tuple[str, ...]) -> None:
    """Take back a program written for a proposal that could not be applied."""

    try:
        safe_open.remove_file(root, parts)
    except safe_open.UnsafePath:
        return
    logger.warning("A program written for a proposal was removed again. event=cli_program_removed path=%s", "/".join(parts))


def checked_copy(root: Path, parts: tuple[str, ...], sha256: str, destination: Path) -> None:
    """Copy the workspace program to ``destination`` (a new trusted path) and check the copy's digest.

    Only the copy is ever run: an agent that changes the workspace file after
    the check changes nothing that runs. A mismatch removes the copy.
    """

    if not root.is_dir() or safe_open.entry_status(root, parts) is None:
        raise ProgramRefused(
            f"The program is missing from {'/'.join(parts)}. Propose the CLI again, or put the approved file back.",
            reason_code=REASON_PROGRAM_MISSING,
        )
    try:
        safe_open.copy_file(root, parts, destination, max_bytes=MAX_PROGRAM_BYTES)
    except safe_open.UnsafePath as exc:
        raise ProgramRefused(
            f"The program at {'/'.join(parts)} cannot be used: {exc}", reason_code=REASON_PROGRAM_UNSAFE
        ) from exc
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        actual = _digest_of_descriptor(descriptor, max_bytes=MAX_PROGRAM_BYTES)
    finally:
        os.close(descriptor)
    if actual != sha256:
        destination.unlink(missing_ok=True)
        raise _mismatch(parts, sha256, actual)
    os.chmod(destination, 0o500)


__all__ = [
    "DOWNLOAD_DEADLINE_SECONDS",
    "DOWNLOAD_TIMEOUT_SECONDS",
    "Downloader",
    "MAX_DOWNLOAD_REDIRECTS",
    "MAX_PROGRAM_BYTES",
    "ProgramRefused",
    "REASON_DIGEST_MISMATCH",
    "REASON_DOWNLOAD_FAILED",
    "REASON_PROGRAM_MISSING",
    "REASON_PROGRAM_UNSAFE",
    "checked_copy",
    "extract_member",
    "fetch_https",
    "install_program",
    "remove_program",
    "workspace_digest",
    "workspaces_root",
]
