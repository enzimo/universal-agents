"""``/upgrade``: an owner's team export, run by the supervisor (guided upgrade phase 2).

Who does what:

1. The gateway's ``/upgrade`` command (owners only) opens the broker's export
   page (``secure_tool_broker.team_export_page``) and watches it.
2. The owner types a passphrase twice. The broker keeps it in memory only and
   writes an export request into ``runtime/team-export/requests/``
   (``write_export_request``): the export id, the team and the owner, never
   the passphrase.
3. The supervisor (``docker/launch_headless.py``) takes the request
   (``take_export_request``) and starts ``assistant_cli team export-requested``
   with its full environment: only the supervisor holds the broker root secret
   (for the operator token) and every operator secret the export carries.
4. That process collects the passphrase from the broker once, through an
   operator-only transfer route (``TEAM_EXPORT_PASSPHRASE_ROUTE`` in
   ``secure_tool_broker.defaults``), with a token for that one collection,
   writes the archive into ``exports/`` and reports the result with the same
   token (``TEAM_EXPORT_RESULT_ROUTE``); the page and the gateway's chat
   message show it. When the process ends with an error without reporting
   (killed, out of memory), the supervisor reports that instead
   (``TEAM_EXPORT_ENDED_ROUTE``); the broker ignores it for an export that
   already reported.

Why the request is a file and the passphrase is not: the supervisor has no
listening socket and keeps one way in, request files under ``runtime/``,
which no agent can reach. A request names an export but proves nothing by
itself: collecting the passphrase needs the operator token, and the broker
hands a passphrase only to the export it was typed for (export id, team and
owner), once. A request file is deleted before it is read as a request, so a
damaged one is logged and dropped and can never stop the team.

What the passphrase protects: the file's stored sign-ins and keys
(``broker_credentials``) and operator secrets (``operator_secrets``) are
encrypted with a key derived from it, and the whole file is signed with it so
any change is detected. The other records (people, access, knowledge, pinned
memories, schedules, settings, team files) are plain JSON inside the file:
anyone holding the file can read them, so the file must stay private.

``EXPORT_REQUESTS_ENV`` is how processes know a supervisor reads these
requests: the launcher sets it for every process it starts. A gateway started
by hand has no supervisor, so ``/upgrade`` points to ``assistant_cli team
export`` instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final, Literal, Mapping

from pydantic import ConfigDict, Field, ValidationError

from size_presentation import size_words
from team_transfer.format import (
    TransferManifestV1,
    TransferModel,
    section_files_prefix,
    section_member,
)
from team_transfer.private_files import write_private_json
from team_transfer.sections import encrypted_member

logger = logging.getLogger(__name__)

# Set by the launcher (docker/launch_headless.py) for every process it starts.
EXPORT_REQUESTS_ENV: Final = "TEAM_EXPORT_REQUESTS"
EXPORT_REQUESTS_BY_SUPERVISOR: Final = "supervisor"
# The broker page an owner types the passphrase on (secure_tool_broker.team_export_page).
TEAM_EXPORT_PAGE_PROVIDER: Final = "team"
TEAM_EXPORT_PAGE_METHOD: Final = "export"
# The page's states, in its status route; the gateway's watch stops at the final ones.
EXPORT_PAGE_CREATED: Final = "created"
EXPORT_PAGE_EXPORTING: Final = "exporting"
EXPORT_PAGE_SUCCESS: Final = "success"
EXPORT_PAGE_FAILED: Final = "failed"
EXPORT_PAGE_CLOSED: Final = "closed"
# The broker restarted after the page opened: it forgot any waiting passphrase
# or running export, so the page can neither start nor follow one.
EXPORT_PAGE_RESTARTED: Final = "restarted"
EXPORT_PAGE_FINAL_STATUSES: Final = frozenset(
    {EXPORT_PAGE_SUCCESS, EXPORT_PAGE_FAILED, EXPORT_PAGE_CLOSED, EXPORT_PAGE_RESTARTED}
)
EXPORT_FILE_SUFFIX: Final = ".uatransfer"
EXPORT_ID_PATTERN: Final = r"^exp_[A-Za-z0-9_-]{16,64}$"
# What to do with a finished export; the page and the chat message show the same steps.
NEXT_STEPS: Final = (
    "Stop this instance. Its chat bot tokens and model sign-ins move with the file, and two running "
    "instances must not use them at once.",
    "Start the new version with a blank data folder.",
    "Copy the file into the new data folder's import folder, then choose Import a team export on the "
    "Let's Begin page.",
)
# What the page and the chat say when the supervisor reports an export process
# that exited with an error before reporting (TEAM_EXPORT_ENDED_ROUTE).
PROCESS_ENDED_MESSAGE: Final = (
    "The export process ended unexpectedly (exit code {exit_code}) before it reported how the export went, so it "
    "is not known whether the file was written: check the exports folder of this instance's data folder. Any "
    "half-written file was removed. The launcher's log has details."
)
_REQUEST_SUFFIX = ".json"
# A request is a few hundred bytes; anything larger is not one.
_REQUEST_MAX_BYTES = 64 * 1024
_FILE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ERROR_MAX_CHARS = 2000
# Collection tokens are URL-safe base64 (secrets.token_urlsafe): ASCII only,
# so comparing them can never fail on the characters themselves.
_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{0,128}$"
_ISSUED_TOKEN_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"
# The longest passphrase /upgrade's page accepts; the page refuses longer ones
# before anything is stored, so the passphrase reply always validates.
EXPORT_PASSPHRASE_MAX_CHARS: Final = 1024
# Set in a failed page result whose outcome nobody reported (the supervisor's
# "process ended" report): a file may or may not exist.
RESULT_UNCONFIRMED: Final = "unconfirmed"


class RepeatedProblemLog:
    """Say a repeating problem once in full, then at most once per interval with how often it came back.

    The supervisor checks for export requests every second, so a problem
    that persists (a request file it cannot delete, a missing entry point)
    would otherwise log on every pass. ``check`` answers 0 for a problem seen
    for the first time (log it fully), the number of repeats since the last
    line once the interval has passed (log a short line), and None meanwhile.
    A different problem is new and logs at once.
    """

    _MAX_PROBLEMS = 64

    def __init__(self, *, interval_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval_seconds
        self._clock = clock
        self._seen: dict[str, tuple[float, int]] = {}

    def check(self, problem: str) -> int | None:
        now = self._clock()
        seen = self._seen.get(problem)
        if seen is None:
            if len(self._seen) >= self._MAX_PROBLEMS:
                self._seen.clear()
            self._seen[problem] = (now, 0)
            return 0
        logged_at, repeats = seen[0], seen[1] + 1
        if now - logged_at >= self._interval:
            self._seen[problem] = (now, 0)
            return repeats
        self._seen[problem] = (logged_at, repeats)
        return None


# Request files the supervisor could not delete (take_export_request).
_STUCK_FILES = RepeatedProblemLog()


class TeamExportRequestV1(TransferModel):
    """What the supervisor reads: which export to run, for whom. No secret."""

    schema_version: Literal[1] = 1
    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)
    requested_at_utc: datetime


class TeamExportPassphraseRequestV1(TransferModel):
    """The export process asks the broker for the passphrase typed for its export.

    ``process_id`` is the asking process's own id: the supervisor's report
    that an export process ended (``TeamExportProcessEndedV1``) counts only
    for the process that collected.
    """

    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)
    process_id: int = Field(ge=1)


class TeamExportPassphraseV1(TransferModel):
    """The passphrase, and the token this one collection's result must carry (``TeamExportResultV1``)."""

    # A validation error never repeats the passphrase (logs, error pages).
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    passphrase: str = Field(min_length=1, max_length=EXPORT_PASSPHRASE_MAX_CHARS, repr=False)
    collection_token: str = Field(pattern=_ISSUED_TOKEN_PATTERN, repr=False)


class TeamExportSectionV1(TransferModel):
    """One part of a finished export, for the page and the chat message."""

    name: str = Field(min_length=1, max_length=64)
    records: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    encrypted: bool = False


class TeamExportResultV1(TransferModel):
    """How an export ended; never holds the passphrase or a record's contents.

    ``collection_token`` is the token the broker gave with the passphrase.
    The broker accepts a result for a collected export only with that token,
    so no other process can end it. A process that failed before collecting
    (``""``) may report only a failure, and only while nothing was collected.
    ``note`` is a warning beside a success (the file could not be given to
    the data folder's owner); the page and the chat show it.
    """

    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)
    ok: bool
    collection_token: str = Field(default="", pattern=_TOKEN_PATTERN, repr=False)
    file_name: str = Field(default="", max_length=255)
    size_bytes: int = Field(default=0, ge=0)
    sections: tuple[TeamExportSectionV1, ...] = ()
    error: str = Field(default="", max_length=_ERROR_MAX_CHARS)
    note: str = Field(default="", max_length=_ERROR_MAX_CHARS)


class TeamExportProcessEndedV1(TransferModel):
    """The supervisor's report that an export process exited with an error (``TEAM_EXPORT_ENDED_ROUTE``).

    ``process_id`` is the child the supervisor started; for a collected
    export it must be the process that collected, or the report is ignored.
    """

    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)
    exit_code: int
    process_id: int = Field(ge=1)


def new_export_id() -> str:
    return "exp_" + secrets.token_urlsafe(18)


def export_file_name(instance_name: str, when: datetime) -> str:
    """``<instance>-<UTC time>.uatransfer``: readable, sortable, never reused."""

    stem = _FILE_NAME_UNSAFE.sub("-", instance_name).strip("-")[:80] or "team"
    return f"{stem}-{when.astimezone(timezone.utc):%Y%m%d-%H%M%S}Z{EXPORT_FILE_SUFFIX}"


def section_lines(result: TeamExportResultV1) -> tuple[str, ...]:
    """One readable line per part: ``People: 3 records, 4 KB (encrypted)``."""

    from team_transfer.titles import section_title

    lines = []
    for section in result.sections:
        records = "1 record" if section.records == 1 else f"{section.records} records"
        encrypted = " (encrypted)" if section.encrypted else ""
        lines.append(f"{section_title(section.name)}: {records}, {size_words(section.size_bytes)}{encrypted}")
    return tuple(lines)


def short_error(text: str) -> str:
    return " ".join(str(text or "").split())[:_ERROR_MAX_CHARS]


def write_export_request(requests_dir: Path, request: TeamExportRequestV1) -> Path:
    """Leave one request for the supervisor (0600, in a 0700 folder); oldest first by name."""

    name = f"{request.requested_at_utc.astimezone(timezone.utc):%Y%m%dT%H%M%S%fZ}-{request.export_id}{_REQUEST_SUFFIX}"
    path = requests_dir / name
    write_private_json(path, request.model_dump(mode="json"))
    logger.info(
        "Team export requested. event=team_export_requested export_id=%s owner_person_id=%s",
        request.export_id,
        request.owner_person_id,
    )
    return path


def take_export_request(requests_dir: Path) -> TeamExportRequestV1 | None:
    """The oldest waiting request, removed from the folder; None when none waits.

    A request is taken once: it is deleted before it is read as a request,
    so a supervisor restart never runs it twice (the owner asks again) and a
    damaged file (not UTF-8, not JSON, wrong fields) is logged and gone
    instead of failing on every pass. A file that cannot be deleted is never
    run.
    """

    try:
        names = sorted(
            entry.name
            for entry in os.scandir(requests_dir)
            if entry.name.endswith(_REQUEST_SUFFIX)
            and not entry.name.startswith(".")
            and entry.is_file(follow_symlinks=False)
        )
    except FileNotFoundError:
        return None
    for name in names:
        raw = _take_request_file(requests_dir / name)
        if raw is None:
            continue
        try:
            return TeamExportRequestV1.model_validate(json.loads(raw))
        except (ValueError, ValidationError, RecursionError) as exc:
            # UnicodeDecodeError and JSONDecodeError are ValueErrors too.
            logger.warning(
                "An export request was not valid and was removed. event=team_export_request_invalid "
                "file=%s error=%s",
                name,
                type(exc).__name__,
            )
    return None


def _take_request_file(path: Path) -> bytes | None:
    """The file's bytes, after deleting it; None when it could not be read or deleted.

    Opened without following a link (the folder listing checked one moment
    earlier). A file that cannot be deleted is skipped: running it would run
    the same export again on the next pass.
    """

    raw: bytes | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(_REQUEST_MAX_BYTES + 1)
    except OSError as exc:
        logger.warning(
            "An export request could not be read and was removed. event=team_export_request_unreadable "
            "file=%s error=%s",
            path.name,
            type(exc).__name__,
        )
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        repeats = _STUCK_FILES.check(path.name)
        if repeats is not None:
            logger.error(
                "An export request could not be removed, so it is not run. event=team_export_request_stuck "
                "file=%s error=%s repeats=%s",
                path.name,
                type(exc).__name__,
                repeats,
            )
        return None
    if raw is not None and len(raw) > _REQUEST_MAX_BYTES:
        logger.warning(
            "An export request was too large and was removed. event=team_export_request_invalid file=%s "
            "error=too_large",
            path.name,
        )
        return None
    return raw


def export_sections(manifest: TransferManifestV1) -> tuple[TeamExportSectionV1, ...]:
    """Each section's record count and its share of the archive (uncompressed), in archive order."""

    sizes: dict[str, int] = {}
    for entry in manifest.sections:
        paths = (section_member(entry.name), encrypted_member(entry.name))
        prefix = section_files_prefix(entry.name)
        sizes[entry.name] = sum(
            member.size for member in manifest.members if member.path in paths or member.path.startswith(prefix)
        )
    return tuple(
        TeamExportSectionV1(
            name=entry.name, records=entry.record_count, size_bytes=sizes[entry.name], encrypted=entry.encrypted
        )
        for entry in manifest.sections
    )


def supervisor_runs_exports(environment: Mapping[str, str] | None = None) -> bool:
    """Whether this process was started by the launcher, whose supervisor runs export requests."""

    source: Mapping[str, str] = environment if environment is not None else os.environ
    return str(source.get(EXPORT_REQUESTS_ENV) or "") == EXPORT_REQUESTS_BY_SUPERVISOR


__all__ = [
    "EXPORT_FILE_SUFFIX",
    "EXPORT_PAGE_CLOSED",
    "EXPORT_PAGE_CREATED",
    "EXPORT_PAGE_EXPORTING",
    "EXPORT_PAGE_FAILED",
    "EXPORT_PAGE_FINAL_STATUSES",
    "EXPORT_PAGE_RESTARTED",
    "EXPORT_PASSPHRASE_MAX_CHARS",
    "EXPORT_PAGE_SUCCESS",
    "EXPORT_REQUESTS_BY_SUPERVISOR",
    "EXPORT_REQUESTS_ENV",
    "NEXT_STEPS",
    "PROCESS_ENDED_MESSAGE",
    "RepeatedProblemLog",
    "RESULT_UNCONFIRMED",
    "TEAM_EXPORT_PAGE_METHOD",
    "TEAM_EXPORT_PAGE_PROVIDER",
    "TeamExportPassphraseRequestV1",
    "TeamExportPassphraseV1",
    "TeamExportProcessEndedV1",
    "TeamExportRequestV1",
    "TeamExportResultV1",
    "TeamExportSectionV1",
    "export_file_name",
    "export_sections",
    "new_export_id",
    "section_lines",
    "short_error",
    "supervisor_runs_exports",
    "take_export_request",
    "write_export_request",
]
