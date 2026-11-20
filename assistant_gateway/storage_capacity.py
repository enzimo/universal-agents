"""Storage that is filling up, as findings on administrators' Status page.

Nothing measured disk, database or message storage before, so a full disk
was first noticed when saving failed. Administrators must hear before a limit
is reached (owner's decision of 2026-10-27). The Status page
(``assistant_gateway.status_page``) adds these findings on every check:

- **Disks.** Free space on the filesystem holding the instance folder, and on
  the folder of each SQLite database (task, gateway, activity) that is on
  another filesystem, as ``df`` counts it: space reserved for the system
  administrator is neither used nor free. An unreadable place is an explicit
  problem; other disks and the message server are still checked.
- **Message storage.** The NATS account's JetStream use against the account's
  limit (``custom_transport.jetstream_account``), when the account has one. The percentage
  counts bytes stored, not the space streams set aside: every stream sets its
  whole byte limit aside when it is created, each team's protocol 1 work and
  dead-letter streams about 2.1 GiB at the default ``transport.queues.max_bytes``
  (``installation_authority.queues``), and the 0.9 set-aside stream 64 MB
  (``custom_transport.nats_topology``), so the finding says so. How full each of
  those streams is against its own limit is ``assistant_gateway.queue_capacity``'s. The bundled server
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
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from assistant_gateway.storage_usage import readable_bytes
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_transport.jetstream_account import BUNDLED_STORAGE_SHARE
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
# The bundled server's room against what its streams set aside (MessageRoom): its key in
# StorageLevels, its finding id prefix, and the reading at the line, as a threshold. No setting:
# the line is NATS's own rule, and a setting would need a change to the team transfer's settings
# section.
MESSAGE_ROOM_KEY: Final = "message_room"
MESSAGE_ROOM_FINDING_PREFIX: Final = f"storage_{MESSAGE_ROOM_KEY}:"
MESSAGE_ROOM_LINE: Final = 100
# The problem that the running bundled server already refuses new streams: its room as it runs,
# taken when it last started, is at or below what its streams set aside. Its own key and finding
# id prefix; its reading is MESSAGE_ROOM_LINE while it refuses and 0 otherwise.
MESSAGE_ROOM_NOW_KEY: Final = "message_room_now"
MESSAGE_ROOM_NOW_FINDING_PREFIX: Final = f"storage_{MESSAGE_ROOM_NOW_KEY}:"
# The source the Status page lists as unavailable when the bundled server's monitoring endpoint
# does not give the room it runs with, so the page keeps the problems it found before
# (StatusPage.notify_new_problems) and shows that it could not ask.
MONITORING_ENDPOINT_SOURCE: Final = "message server's monitoring endpoint"
# The disk holding the bundled server's store, when it cannot be measured.
MESSAGE_STORE_DISK_KEY: Final = "disk:message_store"
MESSAGE_STORE_DISK_LABEL: Final = "The disk holding the message server's store"
# Amounts to free are rounded up to this (and to 100 GB from 1 TB), the step readable_bytes
# shows, so the amount shown is never below what is needed.
ADVICE_STEP_BYTES: Final = 100 * 10**6
QUEUE_BYTES_SETTING: Final = "transport.queues.max_bytes"
# Shared by the room warning and the problem that the server refuses new streams now.
RUNNING_ROOM_TITLE: Final = "The message server refuses new streams now"
_WHOLE_INSTANCE: Final = "the whole instance (the container, or ua start)"
_WHAT_FAILS: Final = (
    "the attachment store Web Chat creates the first time it copies an attachment through the message server, "
    "adding a team, and starting after a change to the message stream settings, which then stops"
)


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
    reserved: int | None  # what the account's streams set aside; None when the server does not report it

    @classmethod
    def of(cls, usage: JetStreamAccountUsage, *, kind: str) -> "MessageStorage":
        if kind == "memory":
            return cls(kind=kind, used=usage.memory, limit=usage.max_memory, reserved=usage.reserved_memory)
        return cls(kind="file", used=usage.storage, limit=usage.max_storage, reserved=usage.reserved_storage)


@dataclass(frozen=True)
class BundledMessageStore:
    """The bundled NATS server's store, present only when the instance runs that server.

    ``folder`` is the server's store folder (``docker.nats_service.bundled_nats_store_dir``);
    the room check measures only the free space of the disk holding it. ``team_set_aside`` is
    what one team's work and dead-letter streams set aside at this instance's settings
    (``custom_messages.wire.queues.streams_set_aside``), for the advice.
    """

    folder: Path
    team_set_aside: int


@dataclass(frozen=True)
class MessageRoom:
    """The bundled server's room at its next start against what its streams set aside.

    nats-server without ``max_file_store`` takes ``BUNDLED_STORAGE_SHARE`` of the free space
    (``f_bavail``) on the disk holding its store as its room, each time it starts, and refuses
    every new or changed stream while its streams set aside as much as that room or more
    (measured on 2.15.1: at a room equal to what is set aside, every new stream is refused).
    """

    disk: SpaceUse  # the disk holding the store
    reserved: int  # what the account's streams set aside

    @property
    def room(self) -> int:
        """The room the server would take if it started now."""

        return int(self.disk.free * BUNDLED_STORAGE_SHARE)

    @property
    def need(self) -> int:
        """The free space whose share equals what is set aside: the server needs more than this."""

        return _free_needed(self.reserved)

    def need_with(self, extra: int) -> int:
        """The free space needed at the server's start for ``extra`` more set aside, such as a new team's streams.

        NATS accepts a new stream with a byte limit only while what is set aside plus that limit
        is no more than its room (measured on 2.15.1), so a team's streams need their whole total
        on top of what the streams already set aside.
        """

        return _free_needed(self.reserved + extra)

    @property
    def short(self) -> bool:
        """Whether the server would refuse new streams after its next start (equality counts)."""

        return self.disk.free <= self.need

    @property
    def release_at(self) -> int:
        """The least free space at which the warning ends: more than ``need`` by ``RELEASE_POINTS`` of the disk."""

        return self.need + math.floor(self.disk.capacity * RELEASE_POINTS / 100) + 1

    @property
    def reading(self) -> float:
        """The level's reading: 100 at the line (free equals need), down ``RELEASE_POINTS`` per that share of the disk.

        So ``StorageLevels``' one threshold (``MESSAGE_ROOM_LINE``) and its ``RELEASE_POINTS``
        rule apply unchanged: entered at ``free <= need``, ended at ``free > need`` plus
        ``RELEASE_POINTS`` percent of the disk, which is ``release_at``. Nothing set aside
        reads 0, so the level ends. The caller leaves out a disk of no capacity.
        """

        if self.reserved == 0:
            return 0.0
        return 100.0 - 100.0 * (self.disk.free - self.need) / self.disk.capacity


def _free_needed(set_aside: int) -> int:
    """The free space on the store's disk whose share (``BUNDLED_STORAGE_SHARE``) equals ``set_aside``."""

    return math.ceil(set_aside / BUNDLED_STORAGE_SHARE)


def refuses_now(*, running: int, reserved: int) -> bool:
    """Whether a server running with ``running`` room refuses every new or changed stream.

    Equality counts: at a room equal to what is set aside every new stream is refused, and below
    it a lowered limit too (measured on 2.15.1). Nothing set aside is never refused by this rule.
    """

    return reserved > 0 and running <= reserved


@dataclass(frozen=True)
class UnmeasuredDisk:
    """A configured disk whose capacity could not be read; it must remain visible on Status."""

    key: str
    label: str
    location: str
    error: str


# -- measuring ----------------------------------------------------------------------------


def disk_spaces(places: Iterable[tuple[str, str, Path]]) -> list[SpaceUse | UnmeasuredDisk]:
    """Each readable filesystem once, plus explicit failures for places that could not be measured."""

    seen: set[int] = set()
    spaces: list[SpaceUse | UnmeasuredDisk] = []
    for key, label, folder in places:
        measured = _measure(key, label, folder)
        if isinstance(measured, UnmeasuredDisk):
            spaces.append(measured)
            continue
        space, device = measured
        if device in seen:
            continue
        seen.add(device)
        spaces.append(space)
    return spaces


def disk_space(key: str, label: str, folder: Path) -> SpaceUse | UnmeasuredDisk:
    """The filesystem holding ``folder``, measured as ``disk_spaces`` measures each place, or an explicit failure.

    It only reads the folder's (or its nearest existing parent's) status and the filesystem's
    figures; it opens nothing in it.
    """

    measured = _measure(key, label, folder)
    return measured if isinstance(measured, UnmeasuredDisk) else measured[0]


def _measure(key: str, label: str, folder: Path) -> tuple[SpaceUse, int] | UnmeasuredDisk:
    """The place's filesystem as ``df`` counts it and its device number, or an explicit failure (logged)."""

    try:
        existing = _nearest_existing(folder)
        device = os.stat(existing).st_dev
        stats = os.statvfs(existing)
    except OSError as exc:
        logger.warning(
            "Could not read a disk's free space. event=storage_disk_unreadable place=%s path=%s error=%s",
            key,
            folder,
            exc,
        )
        return UnmeasuredDisk(key=key, label=label, location=str(folder), error=f"{type(exc).__name__}: {exc.strerror or exc}")
    # As df counts it: blocks reserved for the system administrator are
    # neither used nor available to the team's processes. f_bavail is also the
    # figure nats-server takes its room from (MessageRoom).
    used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    return SpaceUse(key=key, label=label, used=used, capacity=used + available, location=str(existing)), device


def _nearest_existing(folder: Path) -> Path:
    current = folder
    while True:
        try:
            current.stat()
            return current
        except FileNotFoundError:
            if current.parent == current:
                raise
            current = current.parent


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

    Bounded: one entry per measured place at a level (a few disks and the
    message server, each with at most the two levels, and the team's queue
    streams that are filling, each with the one level of ``track_one``).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[Level, datetime]] | None = None

    def track(
        self, readings: Iterable[tuple[str, float]], *, nearing: int, nearly_full: int, now: datetime
    ) -> dict[str, tuple[Level, datetime]]:
        """Each place's level now and when it was entered, for places at a level; saves what changed."""

        return self._track(readings, ((NEARLY_FULL, nearly_full), (NEARING_LIMIT, nearing)), now)

    def track_one(self, readings: Iterable[tuple[str, float]], *, threshold: int, now: datetime) -> dict[str, datetime]:
        """When each place at its one level entered it; saves what changed.

        For places with a single warning level, such as a queue stream against its own limit
        (``assistant_gateway.queue_capacity``). The level is kept under the nearing-limit name
        in the same file, with the same ``RELEASE_POINTS`` margin; callers give their places
        keys of their own, so the two tracks never share an entry.
        """

        return {key: since for key, (_, since) in self._track(readings, ((NEARING_LIMIT, threshold),), now).items()}

    def _track(
        self, readings: Iterable[tuple[str, float]], thresholds: Sequence[tuple[Level, int]], now: datetime
    ) -> dict[str, tuple[Level, datetime]]:
        """Move each place's levels (``thresholds`` from the highest level down); the highest it is at now."""

        with self._lock:
            entries = self._load()
            before = {key: dict(levels) for key, levels in entries.items()}
            current: dict[str, tuple[Level, datetime]] = {}
            for key, percent in readings:
                levels = entries.setdefault(key, {})
                for level, threshold in thresholds:
                    _move(levels, level, percent, threshold, now)
                if not levels:
                    del entries[key]
                    continue
                highest = next(level for level, _ in thresholds if level in levels)
                current[key] = (highest, levels[highest])
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
    ``message_store`` is set only when the instance runs the bundled NATS server; then
    the room check (``MessageRoom``) measures the disk holding its store with
    ``measure_store``, and reads the room the server runs with from its monitoring endpoint
    with ``read_running_room`` (``docker.nats_service.bundled_running_room``).
    """

    def __init__(
        self,
        *,
        disks: Callable[[], Sequence[SpaceUse | UnmeasuredDisk]],
        databases: Callable[[], list[StoredSize]],
        levels: StorageLevels,
        nearing_limit_percent: int,
        nearly_full_percent: int,
        message_storage_kind: str,
        message_store: BundledMessageStore | None = None,
        measure_store: Callable[[Path], SpaceUse | UnmeasuredDisk] | None = None,
        read_running_room: Callable[[], int] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._disks = disks
        self._databases = databases
        # Shared with the queue streams' check (queue_capacity): one object per file.
        self.levels = levels
        self.nearing_limit_percent = nearing_limit_percent
        self.nearly_full_percent = nearly_full_percent
        self._message_storage_kind = message_storage_kind
        self.message_store = message_store
        self._measure_store = measure_store or _store_disk
        self._read_running_room = read_running_room or _bundled_running_room
        self._now = now
        # Set without a lock: two checks at the same moment may both log it, which is harmless.
        self._reserved_unknown_logged = False
        # Whether the bundled server's monitoring endpoint gave its room at the last check (None
        # before the first), so its failing is logged when it starts, not at every check. Also
        # without a lock, for the same reason.
        self._running_room_answering: bool | None = None

    def findings(self, messages: JetStreamAccountUsage | None, *, unavailable: list[str]) -> list[StatusFindingV1]:
        """This check's storage findings; reads disks and databases, so it runs off the gateway's loop.

        ``unavailable`` is the Status page's list of the sources it could not ask at this check;
        the room check adds the bundled server's monitoring endpoint when it does not answer
        (``MONITORING_ENDPOINT_SOURCE``).
        """

        disks = self._disks()
        spaces = [disk for disk in disks if isinstance(disk, SpaceUse)]
        message_storage = MessageStorage.of(messages, kind=self._message_storage_kind) if messages else None
        if message_storage is not None and message_storage.reserved is None:
            self._log_reserved_unknown(message_storage)
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
        levels = self.levels.track(
            ((space.key, space.percent) for space in spaces), nearing=nearing, nearly_full=nearly_full, now=self._now()
        )
        sizes = list(self._databases())
        if message_storage is not None:
            sizes.append(StoredSize(label="Stored messages (JetStream)", size=message_storage.used))
        reserved = message_storage.reserved if message_storage is not None else None
        findings = [
            space_finding(
                space, level=levels[space.key][0], since=levels[space.key][1], nearly_full=nearly_full, sizes=sizes,
                reserved=reserved,
            )
            for space in spaces
            if space.key in levels
        ]
        findings.extend(unmeasured_disk_finding(disk) for disk in disks if isinstance(disk, UnmeasuredDisk))
        if message_storage is not None and not message_storage.limit:
            findings.append(no_message_limit_finding(message_storage, bundled=self.message_store is not None))
        findings.extend(self._room_findings(message_storage, unavailable))
        return findings

    def _room_findings(self, storage: MessageStorage | None, unavailable: list[str]) -> list[StatusFindingV1]:
        """The bundled server's room findings, or the problem that its store's disk cannot be measured.

        Two levels, each under its own key: the warning that the server's room at its next start
        (``MessageRoom``, from the store's disk) is short or only just enough, and the problem that
        the room it runs with now is already short (``refuses_now``, from its monitoring endpoint),
        which ends only once it runs with more room, whatever the disk shows. Both end when the
        check does not apply (no bundled server, memory storage, an account with a limit). Each is
        kept, neither ended nor entered, while it cannot be measured, so it keeps its id when it
        comes back. What keeps it from being announced again then is the Status page's rule for a
        check in which a source could not be asked: it keeps the problems it found before
        (``StatusPage.notify_new_problems``). The message server not answering is such a source
        (the page lists it), and so is the monitoring endpoint not giving the room (listed here,
        ``MONITORING_ENDPOINT_SOURCE``), which hides only the problem. A store disk that cannot be
        read hides only the warning and shows its own problem, as other disks do. A server that
        does not report what its streams set aside hides both; the bundled server always reports
        it, and the no-limit note shows the figure as unknown.
        """

        store = self.message_store
        if store is None or self._message_storage_kind == "memory" or (storage is not None and storage.limit):
            self._track_rooms([(MESSAGE_ROOM_KEY, 0.0), (MESSAGE_ROOM_NOW_KEY, 0.0)])
            return []
        if storage is None or storage.reserved is None:
            return []
        reserved = storage.reserved
        running = self._running_room()
        readings: list[tuple[str, float]] = []
        if running is None:
            if MONITORING_ENDPOINT_SOURCE not in unavailable:
                unavailable.append(MONITORING_ENDPOINT_SOURCE)
        else:
            refusing = refuses_now(running=running, reserved=reserved)
            readings.append((MESSAGE_ROOM_NOW_KEY, float(MESSAGE_ROOM_LINE) if refusing else 0.0))
        findings: list[StatusFindingV1] = []
        room: MessageRoom | None = None
        disk = self._measure_store(store.folder)
        if isinstance(disk, UnmeasuredDisk):
            findings.append(unmeasured_disk_finding(disk))
        elif disk.capacity > 0:
            room = MessageRoom(disk=disk, reserved=reserved)
            readings.append((MESSAGE_ROOM_KEY, room.reading))
        entered = self._track_rooms(readings)
        if running is not None and (since := entered.get(MESSAGE_ROOM_NOW_KEY)) is not None:
            findings.append(
                running_room_finding(running=running, reserved=reserved, room=room, since=since, stored=storage.used)
            )
        if room is not None and (since := entered.get(MESSAGE_ROOM_KEY)) is not None:
            findings.append(
                message_room_finding(
                    room, since=since, running=running, team_set_aside=store.team_set_aside, stored=storage.used
                )
            )
        return findings

    def _track_rooms(self, readings: list[tuple[str, float]]) -> dict[str, datetime]:
        """When each room level given a reading was entered, for those at it; saves what changed."""

        if not readings:
            return {}
        return self.levels.track_one(readings, threshold=MESSAGE_ROOM_LINE, now=self._now())

    def _running_room(self) -> int | None:
        """The room the bundled server runs with, or None when its monitoring endpoint does not give it.

        Logged when the endpoint stops giving it, and when it gives it again, not at every check.
        """

        try:
            room = self._read_running_room()
        except (OSError, ValueError) as exc:
            if self._running_room_answering is not False:
                logger.warning(
                    "The bundled message server's monitoring endpoint did not give the room it runs with, so the "
                    "Status page cannot tell whether it already refuses new streams. "
                    "event=storage_running_room_unknown error=%s",
                    f"{type(exc).__name__}: {exc}",
                )
            self._running_room_answering = False
            return None
        if self._running_room_answering is False:
            logger.info(
                "The bundled message server's monitoring endpoint gives the room it runs with again. "
                "event=storage_running_room_known"
            )
        self._running_room_answering = True
        return room

    def _log_reserved_unknown(self, storage: MessageStorage) -> None:
        if self._reserved_unknown_logged:
            return
        self._reserved_unknown_logged = True
        logger.warning(
            "The message server does not report what its streams set aside, so the Status page shows it as "
            "unknown and cannot check the bundled server's room against it. event=storage_reserved_unknown kind=%s",
            storage.kind,
        )


def _store_disk(folder: Path) -> SpaceUse | UnmeasuredDisk:
    return disk_space(MESSAGE_STORE_DISK_KEY, MESSAGE_STORE_DISK_LABEL, folder)


def _bundled_running_room() -> int:
    # Imported here: the gateway needs the launcher's module only on an instance that runs the
    # bundled server, as team_transfer does.
    from docker.nats_service import bundled_running_room

    return bundled_running_room()


def unmeasured_disk_finding(disk: UnmeasuredDisk) -> StatusFindingV1:
    return StatusFindingV1(
        finding_id=f"storage_unmeasured:{disk.key}",
        severity=StatusSeverity.PROBLEM,
        title=f"{disk.label} could not be measured",
        summary="Its free space is unknown, so the team cannot warn before this disk fills up.",
        action="Check that this folder and its filesystem are available and readable by the running instance, then check Status again.",
        source=SOURCE,
        details=f"Folder: {disk.location}\n{disk.error}"[:2000],
    )


def space_finding(
    space: SpaceUse,
    *,
    level: Level,
    since: datetime,
    nearly_full: int,
    sizes: Sequence[StoredSize],
    reserved: int | None = None,
) -> StatusFindingV1:
    """The finding for a place at a level; its id stays the same while the level lasts.

    ``reserved`` is what the account's streams set aside, shown in the message server's details.
    """

    message_server = space.key.startswith("message_storage:")
    if message_server:
        effect = (
            "When it is full, the message server refuses new messages, so requests, replies and agents' work stop. "
            "The percentage counts the bytes stored, not the space streams set aside: every stream sets its "
            "whole limit aside when it is created, so the server can refuse a new or changed stream well before "
            "then."
        )
        fix = (
            "Raise this account's JetStream limit in the NATS server's configuration, then restart the server. "
            "Leave room for what the streams set aside: each team's work and dead-letter streams set aside their "
            "whole limits (transport.queues.max_bytes), and the set-aside stream 64 MB."
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
        details=_details(space, sizes, also=[_set_aside_line(reserved)] if message_server else [])[:2000],
    )


def _details(space: SpaceUse, sizes: Sequence[StoredSize], *, also: Sequence[str] = ()) -> str:
    lines = [f"Folder: {space.location}"] if space.location else []
    lines.extend(
        f"{item.label}: {readable_bytes(item.size)}" + (f" ({item.location})" if item.location else "") for item in sizes
    )
    lines.extend(also)
    return "\n".join(lines)


def _set_aside_line(reserved: int | None) -> str:
    """What the account's streams set aside, or that the server does not report it (never 0 for unknown)."""

    return "Set aside by its streams: " + ("unknown (the server does not report it)" if reserved is None else readable_bytes(reserved))


def no_message_limit_finding(storage: MessageStorage, *, bundled: bool) -> StatusFindingV1:
    """A note: the account has no limit for stored messages, so only the server's own space bounds them.

    ``bundled`` says the instance runs the bundled server, whose room the page checks (``MessageRoom``).
    """

    where = "memory" if storage.kind == "memory" else "disk"
    server_share = (
        "the memory of its machine" if storage.kind == "memory" else "the disk space that was free when it started"
    )
    if storage.kind == "memory":
        action = (
            "Nothing to do now. The disk warnings on this page cover the instance's disk, not the memory of the "
            "NATS server's machine; watch that too."
        )
    elif bundled:
        action = (
            "This page warns when the free space on the disk holding the bundled server's store would give it less "
            "room than its streams set aside; until then there is nothing to do."
        )
    else:
        action = (
            "Nothing to do now. The disk warnings on this page cover the instance's disk; when the NATS server "
            "keeps its data elsewhere, watch that disk too. Unless the server's configuration sets a limit, that "
            "disk needs a third more free space than the streams set aside at every start of the server."
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
        action=action,
        source=SOURCE,
        details=f"Stored messages (JetStream): {readable_bytes(storage.used)}\n{_set_aside_line(storage.reserved)}",
    )


def message_room_finding(
    room: MessageRoom, *, since: datetime, running: int | None, team_set_aside: int, stored: int
) -> StatusFindingV1:
    """The warning while the bundled server's room at its next start is short or only just enough again.

    Its id stays the same while the level lasts. Three wordings: below the line, how much to
    free and where the warning ends; in the release band above it, that there is room again by
    only a little and where the warning ends; and, when the warning could not end even with
    that much free, that the disk is too small for what the streams set aside. ``running`` is
    the room the server runs with now, None when its monitoring endpoint did not give it: the
    summary says whether it already refuses new streams (``running_room_finding``), or that
    this page cannot tell. The last sentence says what adding a team needs, which is more than
    this warning's line: a new team's streams set their whole limits aside on top.
    """

    disk, need = room.disk, room.need
    ends_at = _rounded_up(room.release_at)
    refusing = running is not None and refuses_now(running=running, reserved=room.reserved)
    rule = (
        "The bundled message server (NATS) takes three quarters of the free space on the disk holding its store as "
        f"its room, measured each time it starts. Its streams set aside {readable_bytes(room.reserved)}, so it needs "
        f"more than {readable_bytes(need)} free there; the disk has {readable_bytes(disk.free)}."
    )
    steps: list[str] = []
    if room.short:
        title = "The message server will have less room than its streams set aside"
        summary = (
            f"{rule} After its next start it will refuse every new or changed stream. The streams it has keep "
            f"working, so messages still flow, but these fail: {_WHAT_FAILS}."
        )
        to_free = _to_free(room)
        if to_free is not None:
            steps.append(f"Free {readable_bytes(to_free)} on that disk before the message server next starts.")
    else:
        title = "The message server's room is only just above what its streams set aside"
        summary = (
            f"{rule} So a start now would give it enough room again for the streams it has, but the disk has only "
            f"{readable_bytes(disk.free - need)} more free than it needs. The warning stays until "
            f"{readable_bytes(ends_at)} is free, so it does not come and go as the disk's use moves."
        )
        keep = _rounded_up(need + 1)
        if keep <= disk.free:
            steps.append(
                f"Keep at least {readable_bytes(keep)} free on that disk: the message server needs it every time it "
                "starts."
            )
        else:  # the rounded figure would be more than the disk has free, although there is enough room
            steps.append(
                "Keep the free space on that disk from falling: it is only just above what the message server needs, "
                "and the server needs that every time it starts."
            )
    if ends_at > disk.capacity:
        too_small = (
            f"This disk is too small for what the streams set aside: the warning ends only once "
            f"{readable_bytes(ends_at)} is free, more than the {readable_bytes(disk.capacity)} it holds. Give it more "
            "space"
        )
        if refusing:  # a server that refuses new streams refuses lowered limits too (running_room_finding)
            steps.append(f"{too_small}.")
        else:
            steps.append(
                f"{too_small}, or lower {QUEUE_BYTES_SETTING} and make a team-wide restart, which lowers the streams' "
                "limits so they set aside less. Do that before the whole instance next starts: a message server that "
                "has started with too little room refuses lowered limits too."
            )
    else:
        steps.append(f"The warning ends once {readable_bytes(ends_at)} is free.")
    steps.append(
        f"The message server measures its room only when {_WHOLE_INSTANCE} starts; a team-wide restart keeps it "
        "running."
    )
    team_need = room.need_with(team_set_aside)
    team_free = _rounded_up(team_need + 1)
    steps.append(
        f"Each team's work and dead-letter streams set aside {readable_bytes(team_set_aside)} at this instance's "
        f"settings ({QUEUE_BYTES_SETTING} sets how much), so adding a team needs the message server to have started "
        f"with at least {readable_bytes(team_free)} free on that disk."
    )
    details = [
        f"Store folder: {disk.location}",
        f"Free on its disk: {readable_bytes(disk.free)} of {readable_bytes(disk.capacity)}",
        f"Room at the next start (three quarters of the free space): {readable_bytes(room.room)}",
        _running_line(running),
        _set_aside_line(room.reserved),
        f"Free space needed: more than {need:,} bytes; the warning ends at {readable_bytes(ends_at)} free",
        f"Stored messages (JetStream): {readable_bytes(stored)}",
        f"Each team's work and dead-letter streams: {readable_bytes(team_set_aside)} ({QUEUE_BYTES_SETTING})",
        f"Free space adding a team needs when the message server starts: more than {team_need:,} bytes",
    ]
    return StatusFindingV1(
        finding_id=f"{MESSAGE_ROOM_FINDING_PREFIX}{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        severity=StatusSeverity.WARNING,
        title=title,
        summary=(
            f"{summary} "
            f"{_running_state(running, refusing=refusing, with_team=room.reserved + team_set_aside, team_free=team_free)}"
        )[:2000],
        action=" ".join(steps)[:1000],
        source=SOURCE,
        since=since,
        details="\n".join(details)[:2000],
    )


def running_room_finding(
    *, running: int, reserved: int, room: MessageRoom | None, since: datetime, stored: int
) -> StatusFindingV1:
    """The problem while the bundled server runs with no more room than its streams set aside.

    Freeing space does not end it: the server took its room when it last started, so only a
    start of the whole instance with enough free space (or streams removed) does. Its id stays
    the same while the level lasts. ``room`` is the store's disk now, None when it could not be
    measured; the action names what to free first when the disk is still short.
    """

    need = _free_needed(reserved)
    summary = (
        f"The bundled message server (NATS) took {readable_bytes(running)} as its room when it last started (three "
        f"quarters of the free space on the disk holding its store then), and its streams set aside "
        f"{readable_bytes(reserved)}, as much or more. So it refuses every new or changed stream now, lowered limits "
        f"too. The streams it has keep working, so messages still flow, but these fail: {_WHAT_FAILS}. Freeing space "
        "alone does not end this: the server takes its room only when it starts."
    )
    if room is None:
        first = (
            f"Make sure the disk holding its store has more than {readable_bytes(need)} free, then restart "
            f"{_WHOLE_INSTANCE}."
        )
    elif room.disk.free >= room.release_at:
        first = (
            f"The disk has enough free space again ({readable_bytes(room.disk.free)}; it needs more than "
            f"{readable_bytes(need)}), so restart {_WHOLE_INSTANCE}."
        )
    elif not room.short:  # above the line by less than the room warning's margin: a restart could start short
        ends_at = _rounded_up(room.release_at)
        where_it_ends = (
            f" (the room warning on this page ends once {readable_bytes(ends_at)} is free)"
            if ends_at <= room.disk.capacity
            else ""
        )
        first = (
            "The disk has enough free space again, but only just. Free more first, so the message server does not "
            f"start short again as the disk's use moves{where_it_ends}, then restart {_WHOLE_INSTANCE}."
        )
    elif (to_free := _to_free(room)) is not None:
        first = f"Free {readable_bytes(to_free)} on that disk, then restart {_WHOLE_INSTANCE}."
    else:
        first = (
            f"This disk is too small for what the streams set aside: it needs more than {readable_bytes(need)} free "
            f"and holds {readable_bytes(room.disk.capacity)}. Give it more space, then restart {_WHOLE_INSTANCE}."
        )
    details = [
        _running_line(running),
        _set_aside_line(reserved),
        f"Free space needed when it starts: more than {need:,} bytes",
    ]
    if room is not None:
        details += [
            f"Store folder: {room.disk.location}",
            f"Free on its disk: {readable_bytes(room.disk.free)} of {readable_bytes(room.disk.capacity)}",
        ]
    details.append(f"Stored messages (JetStream): {readable_bytes(stored)}")
    return StatusFindingV1(
        finding_id=f"{MESSAGE_ROOM_NOW_FINDING_PREFIX}{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        severity=StatusSeverity.PROBLEM,
        title=RUNNING_ROOM_TITLE,
        summary=summary[:2000],
        action=(
            f"{first} A team-wide restart keeps the message server running with the room it has. This problem ends "
            "once it runs with more room than its streams set aside."
        )[:1000],
        source=SOURCE,
        since=since,
        details="\n".join(details)[:2000],
    )


def _running_state(running: int | None, *, refusing: bool, with_team: int, team_free: int) -> str:
    """The room warning's sentence on whether the running server already refuses new streams.

    Above the line (``refuses_now``) it still refuses a new stream whose byte limit does not fit
    beside what is set aside (``MessageRoom.need_with``), so it says whether another team's
    streams fit: ``with_team`` is what the streams would set aside with them, and ``team_free``
    the free space a start of the whole instance needs for them (rounded up). Equality counts as
    not fitting: a team added at exactly the room would leave the server refusing everything new.
    """

    if running is None:
        return (
            "This page cannot tell whether it already refuses new streams, because its monitoring endpoint did not "
            "answer; a start of the whole instance while the disk has enough free space makes sure it does not."
        )
    if refusing:
        return (
            "It already refuses new streams now, because it last started with too little room: see the problem "
            f'"{RUNNING_ROOM_TITLE}".'
        )
    taken = f"Running now, it has {readable_bytes(running)} of room, taken when it last started"
    if running > with_team:
        return f"{taken}: enough for the streams it has and for another team's, so it needs no restart."
    return (
        f"{taken}: enough for the streams it has, but not for another team's too (they would bring what its streams "
        f"set aside to {readable_bytes(with_team)}), so adding a team is refused until {_WHOLE_INSTANCE} starts with "
        f"at least {readable_bytes(team_free)} free on that disk."
    )


def _running_line(running: int | None) -> str:
    """The room the server runs with, for the details, or that it is unknown."""

    figure = "unknown (its monitoring endpoint did not answer)" if running is None else readable_bytes(running)
    return f"Room as it runs (taken when it last started): {figure}"


def _to_free(room: MessageRoom) -> int | None:
    """What to free for enough room at the next start, rounded up; None when freeing all the disk holds is not enough."""

    to_free = _rounded_up(room.need - room.disk.free + 1)
    return to_free if to_free <= room.disk.used else None


def _rounded_up(size: int) -> int:
    """``size`` rounded up to the step ``readable_bytes`` shows (100 MB, or 100 GB above 1 TB)."""

    step = ADVICE_STEP_BYTES * 1000 if size > 10**12 else ADVICE_STEP_BYTES
    return -(-size // step) * step


def tells_administrators(finding: StatusFindingV1) -> bool:
    """Whether a storage finding is one administrators are told about although it is only a warning.

    Storage nearing its limit, and the bundled server's room warning: both come while there is
    still time to act, which is when administrators must hear (one Inbox notice when the level
    is entered, as its id stays the same while the level lasts).
    """

    return finding.finding_id.startswith((NEARING_LIMIT_FINDING_PREFIX, MESSAGE_ROOM_FINDING_PREFIX))


def build_storage_capacity(config: Any, env: Mapping[str, str] | None = None) -> StorageCapacity:
    """The gateway's measurement: the instance folder, its databases and its message storage.

    ``env`` is the gateway's environment (``os.environ`` unless a test gives one): when the
    launcher started the bundled NATS server it names that server's store folder
    (``docker.nats_service.bundled_nats_store_dir``), and only then is the server's room checked.
    """

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
        message_store=_bundled_message_store(config, os.environ if env is None else env),
    )


def _bundled_message_store(config: Any, env: Mapping[str, str]) -> BundledMessageStore | None:
    """The bundled server's store and one team's set-aside figure; None, said once, without that server.

    Called once per gateway start, so the line saying the room is not checked is not repeated
    at every check. A team id the message contract does not accept leaves the room unchecked,
    with a warning, rather than stopping the gateway's start: the installation authority refuses
    that team's streams anyway, and the queue check lists itself as unavailable for it.
    """

    # Imported here: the launcher's module is needed only to read its hand-off.
    from docker.nats_service import bundled_nats_store_dir

    folder = bundled_nats_store_dir(env, logger=logger)
    if folder is None:
        logger.info(
            "The Status page does not check the message server's room: this gateway was not told where a bundled "
            "NATS server keeps its store, so it uses another server. event=storage_room_not_checked"
        )
        return None
    # Imported only now: the stream list comes from the contract's generated models, which cost
    # about half a second to import (queue_capacity imports them at its first check for the same reason).
    from custom_messages.wire.queues import streams_set_aside, team_queue_streams
    from custom_transport.protocol1.contract_files import transport as contract_transport

    queues = config.transport.queues
    try:
        streams = team_queue_streams(contract_transport(), config.manager.team_id, queues.partitions)
    except ValueError as exc:
        logger.warning(
            "The Status page does not check the message server's room: it cannot list this team's streams. "
            "event=storage_room_not_checked team=%s error=%s",
            config.manager.team_id,
            exc,
        )
        return None
    return BundledMessageStore(folder=folder, team_set_aside=streams_set_aside(streams, queues.max_bytes.model_dump()))


__all__ = [
    "MESSAGE_ROOM_FINDING_PREFIX",
    "MESSAGE_ROOM_KEY",
    "MESSAGE_ROOM_LINE",
    "MESSAGE_ROOM_NOW_FINDING_PREFIX",
    "MESSAGE_ROOM_NOW_KEY",
    "MESSAGE_STORE_DISK_KEY",
    "MONITORING_ENDPOINT_SOURCE",
    "NEARING_LIMIT",
    "NEARLY_FULL",
    "NO_MESSAGE_LIMIT_FINDING_ID",
    "RELEASE_POINTS",
    "RUNNING_ROOM_TITLE",
    "BundledMessageStore",
    "MessageRoom",
    "MessageStorage",
    "SpaceUse",
    "StorageCapacity",
    "StorageLevels",
    "StoredSize",
    "UnmeasuredDisk",
    "build_storage_capacity",
    "database_sizes",
    "disk_space",
    "disk_spaces",
    "message_room_finding",
    "no_message_limit_finding",
    "refuses_now",
    "running_room_finding",
    "space_finding",
    "tells_administrators",
]
