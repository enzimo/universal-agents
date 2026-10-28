"""Storage that is filling up, as findings on administrators' Status page.

Nothing measured disk, database or message storage before, so a full disk
was first noticed when saving failed. Administrators must hear before a limit
is reached (owner's decision of 2026-10-27). The Status page
(``assistant_gateway.status_page``) adds these findings on every check:

- **Disks.** Free space on the filesystem holding the instance folder, and on
  the folder of each SQLite database (task, gateway, activity) that is on
  another filesystem, as ``df`` counts it: space reserved for the system
  administrator is neither used nor free.
- **Message storage.** The NATS account's JetStream use against the account's
  limit (``js.account_info()``), when the account has one. The percentage
  counts bytes stored, not the space streams reserve (the account
  information does not report reservations; the set-aside stream reserves
  64 MB, ``custom_transport.nats_topology``), and the finding says so. The bundled server
  runs without one (``docker/nats_service.py`` sets no ``max_file_store``), so
  then a note says so plainly instead of inventing a limit. A server that does
  not answer is listed as unavailable on the page.
- **Database sizes** (SQLite files with their write-ahead log, and
  PostgreSQL's ``pg_database_size``) have no limit of their own; they fill the
  disk. They are listed in each finding's details, so an administrator sees
  what takes the space.

Two levels, both settings (``gateway.storage_warnings``): **nearing the
limit** (80% by default) is a warning, and **nearly full** (95%) a problem.
Both are announced to administrators in one Inbox notice when they are
entered (``StatusPage.notify_new_problems``), never again while the percentage
moves within the level. That works through the finding id, which the notice
id is made from: it names the place, the level and when the level was entered
(``storage_<level>:<place>:<entered at>``), never the percentage.

When a level was entered is kept in ``InstanceLayout.storage_levels_path``
(``StorageLevels``), so a restart neither repeats a notice nor forgets one is
due. A level ends only once use falls ``RELEASE_POINTS`` percentage points
below its threshold, so use that moves around a threshold does not announce
the level again on every crossing. After a level ended, entering it again is
new and is announced again: a disk that fills up a second time months later is
not missed.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from assistant_gateway.storage_usage import readable_bytes
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_transport.nats_adapter import JetStreamAccountUsage
from storage.backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings

logger = logging.getLogger(__name__)

Level = Literal["nearing_limit", "nearly_full"]
NEARING_LIMIT: Final = "nearing_limit"
NEARLY_FULL: Final = "nearly_full"
# Findings at the nearing level are warnings, yet administrators are told (status_page).
NEARING_LIMIT_FINDING_PREFIX = f"storage_{NEARING_LIMIT}:"
NO_MESSAGE_LIMIT_FINDING_ID = "message_storage_no_limit"
# A level ends once use falls this many percentage points below its threshold.
RELEASE_POINTS = 3.0
SOURCE = "gateway"


@dataclass(frozen=True)
class SpaceUse:
    """How full one place with a limit is: a filesystem, or the message server's storage."""

    key: str  # stable: names the place in finding ids and in StorageLevels
    label: str  # "The disk holding the instance folder"
    used: int
    capacity: int
    location: str = ""  # the folder measured, for the details

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.capacity if self.capacity > 0 else 0.0

    @property
    def free(self) -> int:
        return max(0, self.capacity - self.used)


@dataclass(frozen=True)
class StoredSize:
    """A store without a limit of its own, such as a database, and its size."""

    label: str
    size: int
    location: str = ""


@dataclass(frozen=True)
class MessageStorage:
    """The account's stored messages of the kind its streams use (``transport.storage``)."""

    kind: str  # "file" or "memory"
    used: int
    limit: int | None

    @classmethod
    def of(cls, usage: JetStreamAccountUsage, *, kind: str) -> "MessageStorage":
        if kind == "memory":
            return cls(kind=kind, used=usage.memory, limit=usage.max_memory)
        return cls(kind="file", used=usage.storage, limit=usage.max_storage)


# -- measuring ----------------------------------------------------------------------------


def disk_spaces(places: Iterable[tuple[str, str, Path]]) -> list[SpaceUse]:
    """One entry per filesystem among ``places`` (key, label, folder); the first place naming it wins."""

    seen: set[int] = set()
    spaces: list[SpaceUse] = []
    for key, label, folder in places:
        existing = _nearest_existing(folder)
        try:
            device = os.stat(existing).st_dev
            stats = os.statvfs(existing)
        except OSError as exc:
            logger.warning(
                "Could not read a disk's free space. event=storage_disk_unreadable place=%s path=%s error=%s",
                key,
                existing,
                exc,
            )
            continue
        if device in seen:
            continue
        seen.add(device)
        # As df counts it: blocks reserved for the system administrator are
        # neither used nor available to the team's processes.
        used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
        spaces.append(SpaceUse(key=key, label=label, used=used, capacity=used + available, location=str(existing)))
    return spaces


def _nearest_existing(folder: Path) -> Path:
    current = folder
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def database_sizes(stores: Iterable[tuple[str, StorageSettings]]) -> list[StoredSize]:
    """Each database's size: SQLite files with their write-ahead log, and each PostgreSQL database once."""

    listed = list(stores)
    sizes = [size for name, settings in listed if (size := _sqlite_size(name, settings)) is not None]
    for dsn, names in _postgres_databases(listed).items():
        measured = _postgres_size(dsn, names)
        if measured is not None:
            sizes.append(StoredSize(label=" and ".join(names), size=measured, location="PostgreSQL"))
    return sizes


def _sqlite_size(name: str, settings: StorageSettings) -> StoredSize | None:
    if settings.backend != STORAGE_BACKEND_SQLITE or not settings.sqlite_path:
        return None
    path = Path(settings.sqlite_path)
    size = sum(_file_size(Path(f"{path}{suffix}")) for suffix in ("", "-wal"))
    return StoredSize(label=name, size=size, location=str(path)) if size else None


def _postgres_databases(stores: list[tuple[str, StorageSettings]]) -> dict[str, list[str]]:
    """The stores on each PostgreSQL connection string, so a shared database is measured once."""

    databases: dict[str, list[str]] = {}
    for name, settings in stores:
        if settings.backend == STORAGE_BACKEND_POSTGRES and settings.postgres_dsn:
            databases.setdefault(settings.postgres_dsn, []).append(name)
    return databases


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _postgres_size(dsn: str, names: list[str]) -> int | None:
    from storage.postgres_connection import connect_postgres

    try:
        with connect_postgres(dsn) as conn:
            row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    except Exception as exc:  # noqa: BLE001 - a size is context; its finding still shows without it
        logger.warning(
            "Could not read a PostgreSQL database's size. event=storage_database_size_unreadable stores=%s error=%s",
            ",".join(names),
            type(exc).__name__,
        )
        return None
    return int(row[0]) if row and row[0] is not None else None


# -- levels -------------------------------------------------------------------------------


class _StorageLevelsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    levels: dict[str, dict[Level, datetime]] = {}


class StorageLevels:
    """When each place entered each level, kept in a file so a restart remembers it.

    Bounded: one entry per measured place (a few disks and the message
    server), each with at most the two levels.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[Level, datetime]] | None = None

    def track(
        self, readings: Iterable[tuple[str, float]], *, nearing: int, nearly_full: int, now: datetime
    ) -> dict[str, tuple[Level, datetime]]:
        """Each place's level now and when it was entered, for places at a level; saves what changed."""

        with self._lock:
            entries = self._load()
            before = {key: dict(levels) for key, levels in entries.items()}
            current: dict[str, tuple[Level, datetime]] = {}
            for key, percent in readings:
                levels = entries.setdefault(key, {})
                _move(levels, NEARLY_FULL, percent, nearly_full, now)
                _move(levels, NEARING_LIMIT, percent, nearing, now)
                if not levels:
                    del entries[key]
                    continue
                level: Level = NEARLY_FULL if NEARLY_FULL in levels else NEARING_LIMIT
                current[key] = (level, levels[level])
            if entries != before:
                self._save(entries)
            return current

    def _load(self) -> dict[str, dict[Level, datetime]]:
        if self._entries is not None:
            return self._entries
        self._entries = {}
        if self._path.is_file():
            try:
                self._entries = _StorageLevelsFile.model_validate_json(self._path.read_text(encoding="utf-8")).levels
            except (OSError, ValidationError) as exc:
                # Starting over may announce a level once more; it never hides one.
                logger.warning(
                    "The record of storage warning levels is unreadable, so it starts over. "
                    "event=storage_levels_unreadable path=%s error=%s",
                    self._path,
                    exc,
                )
        return self._entries

    def _save(self, entries: dict[str, dict[Level, datetime]]) -> None:
        from custom_agents.governance.authorized_operations import atomic_write_bytes

        content = _StorageLevelsFile(levels=entries).model_dump_json(indent=2) + "\n"
        try:
            atomic_write_bytes(self._path, content.encode("utf-8"))
        except OSError as exc:
            # Kept in memory meanwhile; after a restart a level may be announced once more.
            logger.warning(
                "Could not save when storage warning levels were entered. event=storage_levels_unsaved "
                "path=%s error=%s",
                self._path,
                exc,
            )


def _move(levels: dict[Level, datetime], level: Level, percent: float, threshold: int, now: datetime) -> None:
    if percent >= threshold:
        levels.setdefault(level, now)
    elif percent < threshold - RELEASE_POINTS:
        levels.pop(level, None)


# -- findings -----------------------------------------------------------------------------


class StorageCapacity:
    """Measures the places with a limit and turns them into Status findings.

    ``nearing_limit_percent`` and ``nearly_full_percent`` may change while the
    gateway runs (Settings > Team); each check reads the current numbers.
    """

    def __init__(
        self,
        *,
        disks: Callable[[], list[SpaceUse]],
        databases: Callable[[], list[StoredSize]],
        levels: StorageLevels,
        nearing_limit_percent: int,
        nearly_full_percent: int,
        message_storage_kind: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._disks = disks
        self._databases = databases
        self._levels = levels
        self.nearing_limit_percent = nearing_limit_percent
        self.nearly_full_percent = nearly_full_percent
        self._message_storage_kind = message_storage_kind
        self._now = now

    def findings(self, messages: JetStreamAccountUsage | None) -> list[StatusFindingV1]:
        """This check's storage findings; reads disks and databases, so it runs off the gateway's loop."""

        spaces = list(self._disks())
        message_storage = MessageStorage.of(messages, kind=self._message_storage_kind) if messages else None
        if message_storage is not None and message_storage.limit:
            spaces.append(
                SpaceUse(
                    key=f"message_storage:{message_storage.kind}",
                    label="The message server's storage",
                    used=message_storage.used,
                    capacity=message_storage.limit,
                )
            )
        nearing, nearly_full = self.nearing_limit_percent, self.nearly_full_percent
        levels = self._levels.track(
            ((space.key, space.percent) for space in spaces), nearing=nearing, nearly_full=nearly_full, now=self._now()
        )
        sizes = list(self._databases())
        if message_storage is not None:
            sizes.append(StoredSize(label="Stored messages (JetStream)", size=message_storage.used))
        findings = [
            space_finding(space, level=levels[space.key][0], since=levels[space.key][1], nearly_full=nearly_full, sizes=sizes)
            for space in spaces
            if space.key in levels
        ]
        if message_storage is not None and not message_storage.limit:
            findings.append(no_message_limit_finding(message_storage))
        return findings


def space_finding(
    space: SpaceUse, *, level: Level, since: datetime, nearly_full: int, sizes: Sequence[StoredSize]
) -> StatusFindingV1:
    """The finding for a place at a level; its id stays the same while the level lasts."""

    message_server = space.key.startswith("message_storage:")
    if message_server:
        effect = (
            "When it is full, the message server refuses new messages, so requests, replies and agents' work stop. "
            "The percentage counts the bytes stored, not the space streams reserve for themselves, so the server "
            "can refuse a new stream somewhat earlier."
        )
        fix = (
            "Raise this account's JetStream limit in the NATS server's configuration, then restart the server. "
            "Leave room for the streams' reservations (64 MB for set-aside messages)."
        )
    else:
        effect = (
            "When it is full, agents cannot save files, and chats, tasks and settings can no longer be saved."
        )
        fix = "Make room on this disk or give it more space."
    free = f"{readable_bytes(space.free)} of {readable_bytes(space.capacity)} is free."
    if level == NEARLY_FULL:
        severity, summary, action = StatusSeverity.PROBLEM, f"Only {free} {effect}", f"{fix} Do it now."
    else:
        severity, summary = StatusSeverity.WARNING, f"{free} {effect}"
        action = f"{fix} Administrators are told again at {nearly_full}% full."
    return StatusFindingV1(
        finding_id=f"storage_{level}:{space.key}:{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        severity=severity,
        title=f"{space.label} is {space.percent:.0f}% full",
        summary=summary,
        action=action,
        source=SOURCE,
        since=since,
        details=_details(space, sizes)[:2000],
    )


def _details(space: SpaceUse, sizes: Sequence[StoredSize]) -> str:
    lines = [f"Folder: {space.location}"] if space.location else []
    lines.extend(
        f"{item.label}: {readable_bytes(item.size)}" + (f" ({item.location})" if item.location else "") for item in sizes
    )
    return "\n".join(lines)


def no_message_limit_finding(storage: MessageStorage) -> StatusFindingV1:
    """A note: the account has no limit for stored messages, so only the server's own space bounds them."""

    where = "memory" if storage.kind == "memory" else "disk"
    server_share = (
        "the memory of its machine" if storage.kind == "memory" else "the disk space that was free when it started"
    )
    return StatusFindingV1(
        finding_id=NO_MESSAGE_LIMIT_FINDING_ID,
        severity=StatusSeverity.NOTE,
        title="Stored messages have no size limit",
        summary=(
            f"The NATS server sets the team's account no limit for stored messages (JetStream), which take "
            f"{readable_bytes(storage.used)} now. They can grow until the server's {where} is full. Unless the "
            f"server's configuration sets a limit, NATS refuses new messages once they take three quarters of "
            f"{server_share}."
        ),
        action=(
            "Nothing to do now. The disk warnings on this page cover the instance's disk; when the NATS server "
            "keeps its data elsewhere, watch that disk too."
        ),
        source=SOURCE,
    )


def tells_administrators(finding: StatusFindingV1) -> bool:
    """Whether a storage finding is one administrators are told about although it is only a warning."""

    return finding.finding_id.startswith(NEARING_LIMIT_FINDING_PREFIX)


def build_storage_capacity(config: Any) -> StorageCapacity:
    """The gateway's measurement: the instance folder, its databases and its message storage."""

    def stores() -> list[tuple[str, StorageSettings]]:
        resolved: list[tuple[str, StorageSettings]] = []
        for name, attribute in (
            ("Task database", "manager_storage_settings"),
            ("Gateway database", "state_storage_settings"),
            ("Activity database", "activity_collector_storage_settings"),
        ):
            try:
                resolved.append((name, getattr(config, attribute)))
            except ValueError as exc:  # e.g. a PostgreSQL store whose connection string is not set
                logger.info("A database is not configured here. event=storage_database_skipped store=%s error=%s", name, exc)
        return resolved

    def places() -> list[tuple[str, str, Path]]:
        found = [("disk:instance", "The disk holding the instance folder", config.instance_root_path)]
        for name, settings in stores():
            if settings.backend == STORAGE_BACKEND_SQLITE and settings.sqlite_path:
                key = "disk:" + name.lower().replace(" ", "_")
                found.append((key, f"The disk holding the {name.lower()}", Path(settings.sqlite_path).parent))
        return found

    warnings = config.gateway.storage_warnings
    return StorageCapacity(
        disks=lambda: disk_spaces(places()),
        databases=lambda: database_sizes(stores()),
        levels=StorageLevels(config.layout.storage_levels_path),
        nearing_limit_percent=warnings.nearing_limit_percent,
        nearly_full_percent=warnings.nearly_full_percent,
        message_storage_kind=str(config.transport.storage).lower(),
    )


__all__ = [
    "NEARING_LIMIT",
    "NEARLY_FULL",
    "NO_MESSAGE_LIMIT_FINDING_ID",
    "RELEASE_POINTS",
    "MessageStorage",
    "SpaceUse",
    "StorageCapacity",
    "StorageLevels",
    "StoredSize",
    "build_storage_capacity",
    "database_sizes",
    "disk_spaces",
    "no_message_limit_finding",
    "space_finding",
    "tells_administrators",
]
