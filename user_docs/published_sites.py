"""Published pages live in the person's folder and are served only through a trusted record.

``publish_single_page_site`` (``custom_tools.single_page_site_tools``) renders a
page in trusted code and writes it into the folder of the person the task
works for, ``users/<person>/sites/<site_id>/`` (``agents/sites/<site_id>/``
for work for no person), so a person's deliverables stay with their other
files. Agents' code can write that folder, so what is there is never served
as it stands: the tool also writes a record,
``state/published_sites/<site_id>.json``, in a folder agents cannot reach. The
record names the page's folder (relative to ``workspaces/``), the person it
was made for, and the SHA-256 of each file.

The site host (``user_docs.service`` on the Web Chat origin) finds a page only
through its record, opens each file below ``workspaces/`` one component at a
time without following links (``access_control.safe_open``), and serves it
only while its digest matches. Agent code therefore cannot change what the
Web Chat origin serves, nor point it at another file.

Anyone who has a page link can open the page: people open pages from chat
messages, often on another device, without signing in. That is why new site
ids end with a random part (``SITE_ID_RANDOM_HEX_CHARS``). The page index
lists only the signed-in person's pages, and a page's chat works only for the
person it was made for (``assistant_gateway.interactive_apps.website_handlers.sites``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from access_control import safe_open
from access_control.safe_open import UnsafePath
from runtime_settings.instance_layout import sites_folder_parts

LOGGER = logging.getLogger(__name__)

SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SITE_INDEX_FILENAME = "index.html"
SITE_RECORD_SUFFIX = ".json"
# Random hex characters at the end of every new site id (a page link is the
# only thing that opens a page, so it must not be guessable).
SITE_ID_RANDOM_HEX_CHARS = 8
# The largest page file the host reads into memory to check and serve.
MAX_SITE_FILE_BYTES = 16 * 1024 * 1024
# Where a page's chat sends people who are not signed in as its owner (the
# Web Chat route, assistant_gateway.interactive_apps.website_support).
SITE_CHAT_SIGN_IN_PATH = "/webchat"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Why a page file was not served (SiteFileUnavailable.reason).
SITE_FILE_NOT_PUBLISHED = "not_published"
SITE_FILE_MISSING = "missing"
SITE_FILE_CHANGED = "changed"
SITE_FILE_UNSAFE = "unsafe"


def site_folder_parts(owner_person_id: str | None, site_id: str) -> tuple[str, ...]:
    """Where a site's files live, relative to ``workspaces/``."""

    return (*sites_folder_parts(owner_person_id), site_id)


class PublishedSiteRecordV1(BaseModel):
    """The trusted record of one published page (``state/published_sites/<site_id>.json``).

    Written only by ``publish_single_page_site``; the site host serves a page
    only as this record describes it. ``folder`` must be the one derived from
    the owner and the site id, so a record can never point elsewhere.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    site_id: str
    folder: str = Field(description="The site's folder relative to workspaces/, '/'-separated.")
    owner_person_id: str | None = Field(
        default=None, description="The person the page was made for (from the task's signed context); None for work for no person."
    )
    title: str
    site_kind: str
    created_at: str
    updated_at: str
    update_count: int = 0
    files: dict[str, str] = Field(description="File name to SHA-256 of the bytes the tool wrote.")
    # Where the page was made, so its chat can continue that conversation.
    conversation_id: str | None = None
    session_key: str | None = None
    channel_name: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    team_id: str | None = None
    agent_id: str | None = None
    named_agent_type: str | None = None

    @field_validator("site_id")
    @classmethod
    def _valid_site_id(cls, value: str) -> str:
        if not SITE_ID_PATTERN.match(value):
            raise ValueError("site_id must be letters, digits, dots, underscores or hyphens.")
        return value

    @field_validator("files")
    @classmethod
    def _valid_files(cls, value: dict[str, str]) -> dict[str, str]:
        for name, digest in value.items():
            if not name or "/" in name or name in {".", ".."} or not _SHA256_PATTERN.match(digest):
                raise ValueError(f"Invalid published file entry: {name!r}")
        return value

    @model_validator(mode="after")
    def _folder_matches_owner(self) -> "PublishedSiteRecordV1":
        expected = "/".join(site_folder_parts(self.owner_person_id, self.site_id))
        if self.folder != expected:
            raise ValueError(f"folder must be {expected!r} for this site and owner.")
        return self

    @property
    def folder_parts(self) -> tuple[str, ...]:
        return tuple(self.folder.split("/"))


class SiteFileUnavailable(Exception):
    """A page file is not served; ``reason`` is one of ``SITE_FILE_*``."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PublishedSiteStore:
    """Records in ``records_dir`` (``state/published_sites``); files below ``workspaces_dir``."""

    records_dir: Path
    workspaces_dir: Path

    def record_path(self, site_id: str) -> Path:
        return self.records_dir / f"{site_id}{SITE_RECORD_SUFFIX}"

    def exists(self, site_id: str) -> bool:
        return self.record_path(site_id).exists()

    def record(self, site_id: str) -> PublishedSiteRecordV1 | None:
        """The record of ``site_id``, or None when there is none (or it is unreadable, logged)."""

        if not SITE_ID_PATTERN.match(site_id or ""):
            return None
        path = self.record_path(site_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOGGER.warning(
                "A published page record could not be read. event=generated_site_record_invalid "
                "site_id=%s error=%s",
                site_id,
                exc,
            )
            return None
        try:
            record = PublishedSiteRecordV1.model_validate_json(raw)
        except ValidationError as exc:
            LOGGER.warning(
                "A published page record is invalid and is not served. event=generated_site_record_invalid "
                "site_id=%s error=%s",
                site_id,
                exc.errors()[:3],
            )
            return None
        if record.site_id != site_id:
            LOGGER.warning(
                "A published page record names another site and is not served. "
                "event=generated_site_record_invalid site_id=%s record_site_id=%s",
                site_id,
                record.site_id,
            )
            return None
        return record

    def records_for(self, person_id: str) -> list[PublishedSiteRecordV1]:
        """The pages made for ``person_id``, newest first."""

        if not person_id or not self.records_dir.is_dir():
            return []
        records = [
            record
            for path in self.records_dir.glob(f"*{SITE_RECORD_SUFFIX}")
            if (record := self.record(path.name[: -len(SITE_RECORD_SUFFIX)])) is not None
            and record.owner_person_id == person_id
        ]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def write_files(self, folder_parts: tuple[str, ...], files: Mapping[str, bytes]) -> dict[str, str]:
        """Write a site's files below ``workspaces/`` without following links; returns their digests.

        Folders are created with ``mkdirat`` and an existing file is replaced
        in place only when it is a one-link regular file, so a link planted in
        the person's folder makes the write fail instead of being followed.
        """

        digests: dict[str, str] = {}
        # The workspaces folder itself is trusted (links are refused only below it).
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            safe_open.write_bytes(self.workspaces_dir, (*folder_parts, name), data, create_directories=True)
            digests[name] = sha256_hex(data)
        return digests

    def save(self, record: PublishedSiteRecordV1) -> None:
        """Write the record atomically (a reader sees the old or the new record, never half)."""

        self.records_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{record.site_id}.", suffix=".tmp", dir=self.records_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as writer:
                writer.write(record.model_dump_json(indent=2) + "\n")
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.record_path(record.site_id))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def read_file(self, record: PublishedSiteRecordV1, name: str = SITE_INDEX_FILENAME) -> bytes:
        """The bytes of one published file, only while they match the record.

        Opens the file below ``workspaces/`` without following links; a missing
        file, a link, or changed bytes raise ``SiteFileUnavailable`` and are
        logged as ``event=generated_site_changed`` with the site id.
        """

        expected = record.files.get(name)
        if expected is None:
            raise SiteFileUnavailable(f"{name} is not part of site {record.site_id}.", reason=SITE_FILE_NOT_PUBLISHED)
        try:
            data = safe_open.read_bytes(
                self.workspaces_dir, (*record.folder_parts, name), max_bytes=MAX_SITE_FILE_BYTES
            )
        except UnsafePath as exc:
            reason = SITE_FILE_MISSING if exc.reason == safe_open.REASON_NOT_FOUND else SITE_FILE_UNSAFE
            self._log_unavailable(record, name, reason, detail=exc.reason)
            raise SiteFileUnavailable(str(exc), reason=reason) from exc
        if not hmac.compare_digest(sha256_hex(data), expected):
            self._log_unavailable(record, name, SITE_FILE_CHANGED, detail="digest_mismatch")
            raise SiteFileUnavailable(f"{name} of site {record.site_id} changed after it was published.", reason=SITE_FILE_CHANGED)
        return data

    @staticmethod
    def _log_unavailable(record: PublishedSiteRecordV1, name: str, reason: str, *, detail: str) -> None:
        LOGGER.warning(
            "A published page is not served because its files no longer match its record. "
            "event=generated_site_changed site_id=%s file=%s reason=%s detail=%s owner_person_id=%s",
            record.site_id,
            name,
            reason,
            detail,
            record.owner_person_id or "-",
        )


__all__ = [
    "MAX_SITE_FILE_BYTES",
    "PublishedSiteRecordV1",
    "PublishedSiteStore",
    "SITE_FILE_CHANGED",
    "SITE_FILE_MISSING",
    "SITE_FILE_NOT_PUBLISHED",
    "SITE_CHAT_SIGN_IN_PATH",
    "SITE_FILE_UNSAFE",
    "SITE_ID_PATTERN",
    "SITE_ID_RANDOM_HEX_CHARS",
    "SITE_INDEX_FILENAME",
    "SiteFileUnavailable",
    "sha256_hex",
    "site_folder_parts",
]
