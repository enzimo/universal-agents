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
   operator-only transfer route (``TEAM_EXPORT_PASSPHRASE_ROUTE``), writes the
   archive into ``exports/`` and reports the result
   (``TEAM_EXPORT_RESULT_ROUTE``); the page and the gateway's chat message
   show it.

Why the request is a file and the passphrase is not: the supervisor has no
listening socket and keeps one way in, request files under ``runtime/``,
which no agent can reach. A request names an export but proves nothing by
itself: collecting the passphrase needs the operator token, and the broker
hands a passphrase only to the export it was typed for (export id, team and
owner), once.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal, Mapping

from pydantic import Field, ValidationError

from team_transfer.format import (
    CREDENTIALS_DIR,
    TransferManifestV1,
    TransferModel,
    section_files_prefix,
    section_member,
)
from team_transfer.private_files import write_private_json

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
EXPORT_PAGE_FINAL_STATUSES: Final = frozenset({EXPORT_PAGE_SUCCESS, EXPORT_PAGE_FAILED, EXPORT_PAGE_CLOSED})
# Operator-only broker routes (secure_tool_broker.service: /v1/transfer/ is the operator's).
TEAM_EXPORT_PASSPHRASE_ROUTE: Final = "/v1/transfer/export/passphrase"
TEAM_EXPORT_RESULT_ROUTE: Final = "/v1/transfer/export/result"
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
_REQUEST_SUFFIX = ".json"
_FILE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ERROR_MAX_CHARS = 2000


class TeamExportRequestV1(TransferModel):
    """What the supervisor reads: which export to run, for whom. No secret."""

    schema_version: Literal[1] = 1
    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)
    requested_at_utc: datetime


class TeamExportPassphraseRequestV1(TransferModel):
    """The export process asks the broker for the passphrase typed for its export."""

    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)


class TeamExportPassphraseV1(TransferModel):
    passphrase: str = Field(min_length=1, max_length=1024, repr=False)


class TeamExportSectionV1(TransferModel):
    """One part of a finished export, for the page and the chat message."""

    name: str = Field(min_length=1, max_length=64)
    records: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    encrypted: bool = False


class TeamExportResultV1(TransferModel):
    """How an export ended; never holds the passphrase or a record's contents."""

    export_id: str = Field(pattern=EXPORT_ID_PATTERN)
    team_id: str = Field(min_length=1, max_length=120)
    owner_person_id: str = Field(min_length=1, max_length=240)
    ok: bool
    file_name: str = Field(default="", max_length=255)
    size_bytes: int = Field(default=0, ge=0)
    sections: tuple[TeamExportSectionV1, ...] = ()
    error: str = Field(default="", max_length=_ERROR_MAX_CHARS)


def new_export_id() -> str:
    return "exp_" + secrets.token_urlsafe(18)


def export_file_name(instance_name: str, when: datetime) -> str:
    """``<instance>-<UTC time>.uatransfer``: readable, sortable, never reused."""

    stem = _FILE_NAME_UNSAFE.sub("-", instance_name).strip("-")[:80] or "team"
    return f"{stem}-{when.astimezone(timezone.utc):%Y%m%d-%H%M%S}Z{EXPORT_FILE_SUFFIX}"


def size_words(size_bytes: int) -> str:
    """``812 bytes``, ``14 KB``, ``3.2 MB``, ``1.4 GB``."""

    value = max(0, int(size_bytes))
    if value < 1024:
        return f"{value} bytes"
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{round(value / 1024)} KB"


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

    A request is taken once: it is deleted before the export starts, so a
    supervisor restart never runs it twice (the owner asks again). A file
    that is not a valid request is deleted and logged.
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
        path = requests_dir / name
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "An export request could not be read. event=team_export_request_unreadable file=%s error=%s",
                path.name,
                type(exc).__name__,
            )
            raw = ""
        path.unlink(missing_ok=True)
        try:
            return TeamExportRequestV1.model_validate(json.loads(raw))
        except (ValueError, ValidationError) as exc:
            logger.warning(
                "An export request was not valid and was removed. event=team_export_request_invalid "
                "file=%s error=%s",
                path.name,
                type(exc).__name__,
            )
    return None


def export_sections(manifest: TransferManifestV1) -> tuple[TeamExportSectionV1, ...]:
    """Each section's record count and its share of the archive (uncompressed), in archive order."""

    sizes: dict[str, int] = {}
    for entry in manifest.sections:
        paths = (section_member(entry.name), f"{CREDENTIALS_DIR}/{entry.name}.enc")
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
    "EXPORT_PAGE_SUCCESS",
    "EXPORT_REQUESTS_BY_SUPERVISOR",
    "EXPORT_REQUESTS_ENV",
    "NEXT_STEPS",
    "TEAM_EXPORT_PAGE_METHOD",
    "TEAM_EXPORT_PAGE_PROVIDER",
    "TEAM_EXPORT_PASSPHRASE_ROUTE",
    "TEAM_EXPORT_RESULT_ROUTE",
    "TeamExportPassphraseRequestV1",
    "TeamExportPassphraseV1",
    "TeamExportRequestV1",
    "TeamExportResultV1",
    "TeamExportSectionV1",
    "export_file_name",
    "export_sections",
    "new_export_id",
    "section_lines",
    "short_error",
    "size_words",
    "supervisor_runs_exports",
    "take_export_request",
    "write_export_request",
]
