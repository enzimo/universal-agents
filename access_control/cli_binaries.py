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
import io
import logging
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from access_control import safe_open
from runtime_settings.instance_layout import InstanceLayout

logger = logging.getLogger(__name__)

MAX_PROGRAM_BYTES = 200 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120
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
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlsplit(newurl).scheme != "https":
            raise URLError(f"A redirect left https: {urlsplit(newurl).hostname}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(url: str) -> bytes:
    """Download ``url`` (https only, redirects too), at most ``MAX_PROGRAM_BYTES`` in time."""

    if urlsplit(url).scheme != "https":
        raise ProgramRefused("A program is downloaded only over https.", reason_code=REASON_DOWNLOAD_FAILED)
    opener = build_opener(_HttpsOnlyRedirects())
    try:
        with opener.open(Request(url, headers={"User-Agent": "universal-agents"}), timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_PROGRAM_BYTES + 1)
    except (URLError, OSError, ValueError) as exc:
        raise ProgramRefused(
            f"The program could not be downloaded from {urlsplit(url).hostname}: {exc}", reason_code=REASON_DOWNLOAD_FAILED
        ) from exc
    if len(data) > MAX_PROGRAM_BYTES:
        raise ProgramRefused("The download is larger than the size limit.", reason_code=REASON_DOWNLOAD_FAILED)
    return data


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
    "DOWNLOAD_TIMEOUT_SECONDS",
    "Downloader",
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
