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
  then a note says so plainly instead of inventing a limit, and its room is
  checked instead (below). A server that does not answer is listed as
  unavailable on the page.
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

**The bundled server's room.** nats-server without ``max_file_store`` takes as
its room, once, each time it starts, three quarters of the free space on the
disk holding its store plus three quarters of what its streams already store
there (``BUNDLED_STORAGE_SHARE``; it adds the stored bytes back once it has
recovered its streams, ``finalizeDynamicMaxStore``, measured on 2.15.1 to the
byte). It refuses every new or changed stream (error 10047) while its streams
set aside as much as that room or more, and every new message (10023) once its
streams store more than that room. So a server that starts short keeps taking
messages into the streams it has until they hold its room in total, while the
attachment store (``ua_attachments``, created the first time Web Chat copies an
attachment), a new team's streams and a start after a change to the stream
settings fail. Each team's streams set aside about 2.1 GiB at the defaults, so
a disk can reach this well before the percentages above notice. The server
writes every message into what it stores and out of the free space at once, so
the two together (``MessageRoom.free_and_stored``) hardly move while the
streams fill or drain: only other files on that disk change the next start's
room, and removing messages from the server gives its next start no more room
(while it refuses every message, though, its streams storing less again lets
messages through at once, measured on 2.15.1). For the bundled server only, two
more findings:

- **The room warning** (key ``message_room``, a warning that administrators
  are told about once, as nearing the limit is, ``tells_administrators``):
  three quarters of the free space and the stored bytes together is at or
  below what the streams set aside, so the server would refuse new streams
  after its next start, and new messages at once when it already stores more
  than that room. It names one amount to free from files other than the
  message store, rounded up to the 100 MB step shown, which ends the warning:
  the warning ends only once the free space and the stored bytes exceed what is
  needed by ``RELEASE_POINTS`` percent of the disk (``MessageRoom.release_at``),
  the percentage levels' margin applied to this comparison, so use that moves
  around the line does not announce it again. The margin is for other files on
  the same disk (databases, logs, workspaces), since the streams' own growth
  does not move the comparison. A disk too small for that margin is told so.
  Its last sentence says what adding a team needs, which is more: a new team's
  streams set their whole limits aside on top.
- **The problem** (key ``message_room_now``): the room the server runs with
  now, read from its loopback monitoring endpoint (``/varz``,
  ``docker.nats_service.bundled_running_room``), is at or below what its
  streams set aside, so it refuses new streams already. When what its streams
  store, read from the same reply, is more than that room, it refuses every new
  message too, and the problem says that first, under its own title. Freeing
  space does not end it, only a start of the whole instance with enough room
  does, so it ends only once the server runs with more room.

Both use ``StorageLevels`` like the percentage levels (their reading is 100 at
the line, ``MESSAGE_ROOM_LINE``), so a restart neither repeats a notice nor
forgets one. Neither has a setting: the line is NATS's own rule, and a setting
would need a change to the team transfer's settings section.

Streams without a byte limit (``ua_attachments`` by default, and the protocol 0
work stream) can store more than the room while what the streams set aside stays
below it. That alone raises neither finding yet (a finding for stored messages
against the room is a follow-up), but when one of them shows for another reason,
its text says the server refuses, or would refuse at a start, every new message
(``_running_state``, ``_band_outcome``, ``_start_refuses_messages``), instead of
calling the room enough. A start then takes new messages only once the free
space is more than a third of what is stored, which also gives room for what is
set aside; the warning's amount to free can fall short of that, so while a start
would refuse every message both findings name that condition
(``MessageRoom.stored_decides_start``), the problem instead of the amount and
the warning beside it, in place of saying that removing messages does not help.

Only the bundled server gets them. The launchers name it by handing the gateway
its store folder (``docker.nats_service.bundled_nats_store_dir``, accepted only
beside the bundled server's own address); without that the gateway logs once
that the room is not checked. An external server's store is on a disk the
gateway cannot measure, so an external server without an account limit gets no
room findings, and one with a limit keeps the percentage finding, whose details
show what its streams set aside. The check reads the account's file figures
whatever ``transport.storage`` says: the protocol 1 streams are file streams in
the message contract, and only ``ua_attachments`` follows that setting. On the
bundled server one account uses JetStream (its configuration has no ``accounts``
block), so the account's figures are the server-wide ones NATS takes its room
from and checks against (measured on 2.15.1). A server that does not report
what its streams set aside (``reserved_storage``) gets none, logged once. A
store disk that cannot be read shows the unmeasured-disk problem instead of the
warning. While the monitoring endpoint does not give the room, the problem's
level is kept and the endpoint is listed as an unavailable source
(``MONITORING_ENDPOINT_SOURCE``), so the Status page keeps the problem it found
before instead of announcing it again. NATS measures the free space of the
``jetstream`` folder inside its store folder; the gateway measures the store
folder (or its nearest existing parent), the same disk unless a separate
filesystem is mounted at that subfolder, which no launcher does.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Final, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError

from assistant_gateway.storage_usage import readable_bytes
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_transport.jetstream_account import BUNDLED_STORAGE_SHARE
from custom_transport.nats_adapter import JetStreamAccountUsage
from storage.backends import STORAGE_BACKEND_POSTGRES, STORAGE_BACKEND_SQLITE
from storage.config import StorageSettings

if TYPE_CHECKING:
    # Only for its type: the launcher's module is imported when the bundled server is checked.
    from docker.nats_service import RunningRoom

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
# The problem's titles, also named by the room warning: refusing new streams now, and refusing new
# messages now, the worse state, which leads when the server's streams store more than its room.
RUNNING_ROOM_TITLE: Final = "The message server refuses new streams now"
MESSAGES_REFUSED_TITLE: Final = "The message server refuses new messages now"
_WHOLE_INSTANCE: Final = "the whole instance (the container, or ua start)"
_WHAT_FAILS: Final = (
    "the attachment store Web Chat creates the first time it copies an attachment through the message server, "
    "adding a team, and a start after a change to the message stream settings (that start stops with an error)"
)
# nats-server counts its stored messages back into its room when it starts, so deleting them gives
# the next start no more room (MessageRoom.free_and_stored).
_REMOVING_MESSAGES: Final = (
    "Removing messages from the message server does not help: it counts its stored messages back into its room "
    "when it starts."
)
# While a start would refuse every message with more stored than set aside (MessageRoom.stored_decides_start), the
# room warning says what that start needs instead of _REMOVING_MESSAGES: storing less is then part of what lets it
# take messages, and the warning's amount to free can fall short of that free space.
_STORED_DECIDES_START: Final = (
    "Its next start takes new messages only if the free space is more than a third of what its streams store when it "
    "starts."
)
# While the running server refuses every message (_refuses_messages), its streams storing less than its room lets
# messages through again at once (measured on 2.15.1), so the problem says both halves instead of _REMOVING_MESSAGES:
# this one, and _NO_MORE_ROOM, which it leaves out where the warning leaves out _REMOVING_MESSAGES (_flow_again).
_MESSAGES_FLOW_AGAIN: Final = (
    "It takes new messages again as soon as its streams store less than its room, for example once agents take "
    "queued work, but only until they fill again"
)
_NO_MORE_ROOM: Final = "removing messages gives its next start no more room"


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
    """The account's stored messages of the kind ``transport.storage`` names, for the percentage and the note.

    ``file_reserved`` is what the account's file streams set aside whatever that kind is: the
    protocol 1 streams are file streams in the message contract, so with memory storage the note
    shows both set-aside figures (``_set_aside_lines``).
    """

    kind: str  # "file" or "memory"
    used: int
    limit: int | None
    reserved: int | None  # what the account's streams set aside; None when the server does not report it
    file_reserved: int | None

    @classmethod
    def of(cls, usage: JetStreamAccountUsage, *, kind: str) -> "MessageStorage":
        if kind == "memory":
            return cls(
                kind=kind, used=usage.memory, limit=usage.max_memory, reserved=usage.reserved_memory,
                file_reserved=usage.reserved_storage,
            )
        return cls(
            kind="file", used=usage.storage, limit=usage.max_storage, reserved=usage.reserved_storage,
            file_reserved=usage.reserved_storage,
        )


@dataclass(frozen=True)
class BundledMessageStore:
    """The bundled NATS server's store, present only when the instance runs that server.

    ``folder`` is the server's store folder (``docker.nats_service.bundled_nats_store_dir``);
    the room check measures only the disk holding it. ``team_set_aside`` is what one team's
    work and dead-letter streams set aside at this instance's settings
    (``custom_messages.wire.queues.streams_set_aside``), for the advice.
    """

    folder: Path
    team_set_aside: int


@dataclass(frozen=True)
class StoreDisk:
    """The disk holding the bundled server's store, measured for the room check (``store_disk``).

    ``space`` is the disk as ``df`` counts it, as every disk on the page is measured: its size
    gives the room warning's margin, and its used space less the stored messages is what other
    files take. ``free`` is its free space as nats-server measures it when it takes its room,
    ``f_bavail`` blocks of ``f_bsize`` (server/disk_avail.go); it differs from ``space.free``
    (blocks of ``f_frsize``) only on a filesystem whose two block sizes differ.
    """

    space: SpaceUse
    free: int


@dataclass(frozen=True)
class MessageRoom:
    """The bundled server's room at its next start against what its streams set aside.

    nats-server without ``max_file_store`` takes ``BUNDLED_STORAGE_SHARE`` of the free space on
    the disk holding its store plus the same share of what its streams store (the account's
    ``storage``) as its room, each time it starts. It refuses every new or changed stream while
    its streams set aside as much as that room or more (measured on 2.15.1: at a room equal to
    what is set aside, every new stream is refused), and every new message once they store more
    than it (``jetstream.go`` ``wouldExceedLimits``, checked on every message).
    """

    disk: StoreDisk  # the disk holding the store
    reserved: int  # what the account's streams set aside
    stored: int  # what the account's file streams store (the account's storage)

    @property
    def free_and_stored(self) -> int:
        """The free space and the stored bytes together, which the room is a share of.

        The server writes a message into both figures at once, so this hardly moves while the
        streams fill or drain; only other files on the disk change it, which is why every
        amount to free is from files other than the message store.
        """

        return self.disk.free + self.stored

    @property
    def room(self) -> int:
        """The room the server would take if it started now."""

        return int(self.free_and_stored * BUNDLED_STORAGE_SHARE)

    @property
    def need(self) -> int:
        """The free space and stored bytes whose share equals what is set aside: the server needs more than this."""

        return _free_and_stored_needed(self.reserved)

    def need_with(self, extra: int) -> int:
        """The free space and stored bytes needed at the server's start for ``extra`` more set aside, such as a new team's streams.

        NATS accepts a new stream with a byte limit only while what is set aside plus that limit
        is no more than its room (measured on 2.15.1), so a team's streams need their whole total
        on top of what the streams already set aside.
        """

        return _free_and_stored_needed(self.reserved + extra)

    @property
    def short(self) -> bool:
        """Whether the server would refuse new streams after its next start (equality counts)."""

        return self.free_and_stored <= self.need

    @property
    def refuses_messages_at_start(self) -> bool:
        """Whether the server would refuse every new message as soon as it starts: it stores more than that room."""

        return self.stored > self.room

    @property
    def stored_decides_start(self) -> bool:
        """Whether a start now would refuse every new message while the streams store more than they set aside.

        Only streams without a byte limit can store that much. Then a start takes new messages
        only once its room is more than what is stored, which is free space of more than a third
        of the stored bytes (three quarters of both together is then more than the stored
        bytes), and that room is also more than what is set aside. The room warning's amount to
        free (``_to_free``) can fall short of it, so the findings say this condition instead of
        leaving the warning's amount as the remedy. When less is stored than set aside, room for
        what is set aside is enough for what is stored too, and the warning's amount covers both.
        """

        return self.refuses_messages_at_start and self.stored > self.reserved

    @property
    def release_at(self) -> int:
        """The least free space and stored bytes at which the warning ends: more than ``need`` by ``RELEASE_POINTS`` of the disk."""

        return self.need + math.floor(self.disk.space.capacity * RELEASE_POINTS / 100) + 1

    @property
    def other_files(self) -> int:
        """What files other than the message store take on the disk: the most that freeing them can give."""

        return max(0, self.disk.space.used - self.stored)

    @property
    def reading(self) -> float:
        """The level's reading: 100 at the line (``free_and_stored`` equals ``need``), down ``RELEASE_POINTS`` per that share of the disk.

        So ``StorageLevels``' one threshold (``MESSAGE_ROOM_LINE``) and its ``RELEASE_POINTS``
        rule apply unchanged: entered at ``free_and_stored <= need``, ended at more than
        ``need`` plus ``RELEASE_POINTS`` percent of the disk, which is ``release_at``. Nothing
        set aside reads 0, so the level ends. The caller leaves out a disk of no capacity.
        """

        if self.reserved == 0:
            return 0.0
        return 100.0 - 100.0 * (self.free_and_stored - self.need) / self.disk.space.capacity


def _free_and_stored_needed(set_aside: int) -> int:
    """The free space and stored bytes whose share (``BUNDLED_STORAGE_SHARE``) equals ``set_aside``."""

    return math.ceil(set_aside / BUNDLED_STORAGE_SHARE)


def refuses_now(*, running: int, reserved: int) -> bool:
    """Whether a server running with ``running`` room refuses every new or changed stream.

    Equality counts: at a room equal to what is set aside every new stream is refused, and below
    it a lowered limit too (measured on 2.15.1). Nothing set aside is never refused by this rule.
    """

    return reserved > 0 and running <= reserved


def _refuses_messages(running: RunningRoom) -> bool:
    """Whether the running server refuses every new message now: its streams store more than its room.

    Strictly more, as nats-server checks it on every message (``jetstream.go`` ``limitsExceeded``).
    """

    return running.stored > running.room


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


def store_disk(folder: Path) -> StoreDisk | UnmeasuredDisk:
    """The disk holding the bundled server's store folder, for the room check, or an explicit failure.

    Measured as ``disk_spaces`` measures each place, plus its free space as nats-server measures
    it (``StoreDisk``). It only reads the folder's (or its nearest existing parent's) status and
    the filesystem's figures; it opens nothing in it.
    """

    read = _read_filesystem(MESSAGE_STORE_DISK_KEY, MESSAGE_STORE_DISK_LABEL, folder)
    if isinstance(read, UnmeasuredDisk):
        return read
    existing, _, stats = read
    space = _space_use(MESSAGE_STORE_DISK_KEY, MESSAGE_STORE_DISK_LABEL, existing, stats)
    return StoreDisk(space=space, free=stats.f_bavail * stats.f_bsize)


def _measure(key: str, label: str, folder: Path) -> tuple[SpaceUse, int] | UnmeasuredDisk:
    """The place's filesystem as ``df`` counts it and its device number, or an explicit failure (logged)."""

    read = _read_filesystem(key, label, folder)
    if isinstance(read, UnmeasuredDisk):
        return read
    existing, device, stats = read
    return _space_use(key, label, existing, stats), device


def _read_filesystem(key: str, label: str, folder: Path) -> tuple[Path, int, os.statvfs_result] | UnmeasuredDisk:
    """The folder (or its nearest existing parent), its device number and its filesystem's figures; a failure is logged."""

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
    return existing, device, stats


def _space_use(key: str, label: str, existing: Path, stats: os.statvfs_result) -> SpaceUse:
    # As df counts it: blocks reserved for the system administrator are
    # neither used nor available to the team's processes.
    used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    return SpaceUse(key=key, label=label, used=used, capacity=used + available, location=str(existing))


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
    An older build keeps the keys it does not track when it saves, so after a
    downgrade and a later upgrade a level that still holds comes back with its
    old entered-at time, and so its old id.
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
    ``measure_store`` (``store_disk``), and reads the room the server runs with, and what its
    streams store, from its monitoring endpoint with ``read_running_room``
    (``docker.nats_service.bundled_running_room``).
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
        measure_store: Callable[[Path], StoreDisk | UnmeasuredDisk] | None = None,
        read_running_room: Callable[[], RunningRoom] | None = None,
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
        self._measure_store = measure_store or store_disk
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
        message_storage = self._message_storage(messages)
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
        findings.extend(self._room_findings(messages, unavailable))
        return findings

    def _message_storage(self, messages: JetStreamAccountUsage | None) -> MessageStorage | None:
        """The message server's storage as this check reads it, None when it did not answer.

        A server that does not report what its streams set aside is logged once, not at every
        check: the figure shows as unknown, never 0, and the room is not checked against it.
        """

        if not messages:
            return None
        storage = MessageStorage.of(messages, kind=self._message_storage_kind)
        if storage.reserved is None or storage.file_reserved is None:
            self._log_reserved_unknown(storage)
        return storage

    def _room_findings(self, messages: JetStreamAccountUsage | None, unavailable: list[str]) -> list[StatusFindingV1]:
        """The bundled server's room findings, or the problem that its store's disk cannot be measured.

        Two levels, each under its own key: the warning that the server's room at its next start
        (``MessageRoom``, from the store's disk and what the streams store) is short or only just
        enough, and the problem that the room it runs with now is already short (``refuses_now``,
        from its monitoring endpoint), which ends only once it runs with more room, whatever the
        disk shows. Both end when the check does not apply: no bundled server, or an account with a
        file limit. They use the account's file figures whatever ``transport.storage`` says, since
        the protocol 1 streams are file streams in the message contract (only ``ua_attachments``
        follows that setting). On the bundled server one account uses JetStream, so its figures
        are the server-wide ones NATS uses. Each level is kept, neither ended nor entered, while it
        cannot be measured, so it keeps its id when it comes back. What keeps it from being
        announced again then is the Status page's rule for a check in which a source could not be
        asked: it keeps the problems it found before (``StatusPage.notify_new_problems``). The
        message server not answering is such a source (the page lists it), and so is the
        monitoring endpoint not giving the room (listed here, ``MONITORING_ENDPOINT_SOURCE``),
        which hides only the problem. A store disk that cannot be read hides only the warning and
        shows its own problem, as other disks do. A server that does not report what its streams
        set aside hides both; the bundled server always reports it, and the no-limit note shows
        the figure as unknown.
        """

        store = self.message_store
        if store is None or (messages is not None and messages.max_storage):
            self._track_rooms([(MESSAGE_ROOM_KEY, 0.0), (MESSAGE_ROOM_NOW_KEY, 0.0)])
            return []
        if messages is None or messages.reserved_storage is None:
            return []
        reserved = messages.reserved_storage
        running = self._running_room(unavailable)
        readings: list[tuple[str, float]] = []
        if running is not None:
            refusing = refuses_now(running=running.room, reserved=reserved)
            readings.append((MESSAGE_ROOM_NOW_KEY, float(MESSAGE_ROOM_LINE) if refusing else 0.0))
        findings: list[StatusFindingV1] = []
        room: MessageRoom | None = None
        disk = self._measure_store(store.folder)
        if isinstance(disk, UnmeasuredDisk):
            findings.append(unmeasured_disk_finding(disk))
        elif disk.space.capacity > 0:
            room = MessageRoom(disk=disk, reserved=reserved, stored=messages.storage)
            readings.append((MESSAGE_ROOM_KEY, room.reading))
        entered = self._track_rooms(readings)
        if running is not None and (since := entered.get(MESSAGE_ROOM_NOW_KEY)) is not None:
            findings.append(running_room_finding(running=running, reserved=reserved, room=room, since=since))
        if room is not None and (since := entered.get(MESSAGE_ROOM_KEY)) is not None:
            findings.append(message_room_finding(room, since=since, running=running, team_set_aside=store.team_set_aside))
        return findings

    def _track_rooms(self, readings: list[tuple[str, float]]) -> dict[str, datetime]:
        """When each room level given a reading was entered, for those at it; saves what changed."""

        if not readings:
            return {}
        return self.levels.track_one(readings, threshold=MESSAGE_ROOM_LINE, now=self._now())

    def _running_room(self, unavailable: list[str]) -> RunningRoom | None:
        """The room the bundled server runs with and what it stores, or None when its monitoring endpoint does not give them.

        Then the endpoint is added to the page's ``unavailable`` sources at every such check
        (``MONITORING_ENDPOINT_SOURCE``), so the page keeps the problem it found before. Logged
        when the endpoint stops giving it, and when it gives it again, not at every check.
        """

        try:
            room = self._read_running_room()
        except (OSError, ValueError) as exc:
            if MONITORING_ENDPOINT_SOURCE not in unavailable:
                unavailable.append(MONITORING_ENDPOINT_SOURCE)
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


def _bundled_running_room() -> RunningRoom:
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
            "whole limits (transport.queues.max_bytes), the blob store OBJ_UA1_BLOBS sets aside its limit "
            "(transport.queues.max_bytes.blobs), and the set-aside stream 64 MB."
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


def _set_aside_line(reserved: int | None, where: str = "") -> str:
    """What the account's streams set aside (``where`` names memory or disk), or that the server does not report it (never 0 for unknown)."""

    figure = "unknown (the server does not report it)" if reserved is None else readable_bytes(reserved)
    return f"Set aside by its streams{where}: {figure}"


def _set_aside_lines(storage: MessageStorage) -> list[str]:
    """What the account's streams set aside: one line, or with memory storage the memory and the disk figure when they differ.

    The protocol 1 streams are file streams whatever ``transport.storage`` says, so with memory
    storage the disk figure is the one the bundled server's room is checked against.
    """

    if storage.reserved == storage.file_reserved:
        return [_set_aside_line(storage.reserved)]
    return [_set_aside_line(storage.reserved, " in memory"), _set_aside_line(storage.file_reserved, " on disk")]


def no_message_limit_finding(storage: MessageStorage, *, bundled: bool) -> StatusFindingV1:
    """A note: the account has no limit for stored messages, so only the server's own space bounds them.

    ``bundled`` says the instance runs the bundled server, whose room the page checks (``MessageRoom``).
    """

    memory = storage.kind == "memory"
    server_share = "the memory of its machine" if memory else "the disk space that was free or held its messages when it started"
    watch_memory = (
        "The disk warnings on this page cover the instance's disk, not the memory of the NATS server's machine; watch "
        "that too."
    )
    if bundled:
        action = (
            "This page warns when the free space and the stored messages on the disk holding the bundled server's store "
            "would give it less room than its streams set aside; until then there is nothing to do."
        )
        if memory:
            action = f"{action} {watch_memory}"
    elif memory:
        action = f"Nothing to do now. {watch_memory}"
    else:
        action = (
            "Nothing to do now. The disk warnings on this page cover the instance's disk; when the NATS server "
            "keeps its data elsewhere, watch that disk too. Unless the server's configuration sets a limit, that "
            "disk's free space and the server's stored messages together need to be a third more than what the streams "
            "set aside, at every start of the server."
        )
    return StatusFindingV1(
        finding_id=NO_MESSAGE_LIMIT_FINDING_ID,
        severity=StatusSeverity.NOTE,
        title="Stored messages have no size limit",
        summary=(
            f"The NATS server sets the team's account no limit for stored messages (JetStream), which take "
            f"{readable_bytes(storage.used)} now. They can grow until the server's {'memory' if memory else 'disk'} is "
            f"full. Unless the server's configuration sets a limit, NATS refuses new messages once they take three "
            f"quarters of {server_share}."
        ),
        action=action,
        source=SOURCE,
        details="\n".join([f"Stored messages (JetStream): {readable_bytes(storage.used)}", *_set_aside_lines(storage)]),
    )


def message_room_finding(
    room: MessageRoom, *, since: datetime, running: RunningRoom | None, team_set_aside: int
) -> StatusFindingV1:
    """The warning while the bundled server's room at its next start is short or only just enough again.

    Its id stays the same while the level lasts. Below the line it says what fails and when
    messages stop (``_short_outcome``); in the release band above it, that there is room again by
    only a little. Both name one amount to free from files other than the message store, which
    ends the warning, or say that the disk is too small for that (``_free_step``). The next step
    says that removing messages does not help, or, while a start would refuse every message with
    more stored than set aside, the free space that start needs (``_STORED_DECIDES_START``), which
    that amount can fall short of. ``running`` is the room the server runs with now and what it
    stores, None when its monitoring endpoint did not give them: the summary's last sentence says
    whether it already refuses (``running_room_finding``), or that this page cannot tell
    (``_running_state``). The last step says what adding a team needs, which is more than this
    warning's line: a new team's streams set their whole limits aside on top.
    """

    ends_at = _rounded_up(room.release_at)
    figures = (
        "Each time it starts, the bundled message server (NATS) takes three quarters of the free space on the disk "
        "holding its store, plus three quarters of what its stored messages take, as its room. Its streams set aside "
        f"{readable_bytes(room.reserved)}, so the free space and the stored messages must together come to more than "
        f"{readable_bytes(room.need)}; they come to {readable_bytes(room.free_and_stored)} "
        f"({readable_bytes(room.disk.free)} free and {readable_bytes(room.stored)} stored)."
    )
    if room.short:
        title = "The message server will have less room than its streams set aside"
        outcome = _short_outcome(room)
    else:
        title = "The message server's room is only just above what its streams set aside"
        outcome = _band_outcome(room, ends_at)
    refusing = running is not None and refuses_now(running=running.room, reserved=room.reserved)
    team_need = room.need_with(team_set_aside)
    steps = [
        _free_step(room, offer_lower_limits=running is not None and not refusing),
        _STORED_DECIDES_START if room.stored_decides_start else _REMOVING_MESSAGES,
        f"The message server measures its room only when {_WHOLE_INSTANCE} starts; a team-wide restart keeps it "
        "running.",
        f"Each team's work and dead-letter streams set aside {readable_bytes(team_set_aside)} at this instance's "
        f"settings ({QUEUE_BYTES_SETTING} sets how much), so adding a team needs the free space and the stored messages "
        f"on that disk to come to at least {readable_bytes(_rounded_up(team_need + 1))} when the message server starts.",
    ]
    details = [
        f"Store folder: {room.disk.space.location}",
        f"Free on its disk: {readable_bytes(room.disk.free)} of {readable_bytes(room.disk.space.capacity)}",
        f"Stored messages (JetStream, on disk): {readable_bytes(room.stored)}",
        f"Room at the next start (three quarters of the free space and the stored messages): {readable_bytes(room.room)}",
        _running_line(running),
        _set_aside_line(room.reserved),
        f"Free space and stored messages needed: more than {room.need:,} bytes; the warning ends at "
        f"{readable_bytes(ends_at)}",
        f"Each team's work and dead-letter streams: {readable_bytes(team_set_aside)} ({QUEUE_BYTES_SETTING})",
        f"Free space and stored messages adding a team needs when the message server starts: more than "
        f"{team_need:,} bytes",
    ]
    return StatusFindingV1(
        finding_id=f"{MESSAGE_ROOM_FINDING_PREFIX}{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        severity=StatusSeverity.WARNING,
        title=title,
        summary=f"{figures} {outcome} {_running_state(running, reserved=room.reserved, team_set_aside=team_set_aside)}"[
            :2000
        ],
        action=" ".join(steps)[:1000],
        source=SOURCE,
        since=since,
        details="\n".join(details)[:2000],
    )


def _short_outcome(room: MessageRoom) -> str:
    """What the room warning says the next start brings below the line, by its stored messages against that room.

    Messages stop at once only when the streams already store more than that room; otherwise the
    streams keep taking messages until they hold it in total, which their own limits allow, since
    together they set aside at least as much.
    """

    after = f"After its next start it will have {readable_bytes(room.room)} of room"
    if room.refuses_messages_at_start:
        return (
            f"{after}, less than its stored messages take, so it will refuse every new message at once, as well as "
            "every new or changed stream: requests, replies and agents' work stop."
        )
    return (
        f"{after} and refuse every new or changed stream, so these fail: {_WHAT_FAILS}. Its streams keep taking "
        f"messages until they hold {readable_bytes(room.room)} in total ({readable_bytes(room.stored)} now); then it "
        "refuses every new message, so requests, replies and agents' work stop."
    )


def _band_outcome(room: MessageRoom, ends_at: int) -> str:
    """What the room warning says the next start brings in its release band: room for what is set aside, by only a little.

    Streams without a byte limit can store more than that room although less is set aside
    (``refuses_messages_at_start``); then a start now would refuse every new message, and it says
    so instead of calling the room enough.
    """

    if room.refuses_messages_at_start:
        start = (
            f"So a start now would give it {readable_bytes(room.room)} of room, enough for what its streams set aside "
            "but less than their stored messages take, so it would refuse every new message at once. The free space "
            "and the stored messages come to only"
        )
    else:
        start = "So a start now would give it enough room for the streams it has, but they come to only"
    return (
        f"{start} {readable_bytes(room.free_and_stored - room.need)} more than it needs. The warning stays until they "
        f"come to {readable_bytes(ends_at)}, so it does not come and go as other files on that disk grow and shrink."
    )


def _free_step(room: MessageRoom, *, offer_lower_limits: bool) -> str:
    """The room warning's first step: the one amount to free that ends it (``_to_free``), or that the disk is too small for that.

    Lowering the streams' limits is offered only while the running server is known not to refuse
    new streams (``offer_lower_limits``): one that refuses them refuses lowered limits too, and
    then the team's start fails.
    """

    to_free = _to_free(room)
    if to_free is not None:
        when = " before the message server next starts" if room.short else ""
        return f"Free {readable_bytes(to_free)} from other files on that disk{when}; that ends this warning."
    too_small = (
        "This disk is too small to end this warning: it ends only once the free space and the stored messages come to "
        f"{readable_bytes(_rounded_up(room.release_at))}, more than the {readable_bytes(room.disk.space.capacity)} it "
        "holds. Give it more space"
    )
    if not offer_lower_limits:
        return f"{too_small}."
    return (
        f"{too_small}, or lower {QUEUE_BYTES_SETTING} and make a team-wide restart, which lowers the streams' limits so "
        "they set aside less. Do that before the whole instance next starts: a message server that has started with "
        "too little room refuses lowered limits too."
    )


def running_room_finding(
    *, running: RunningRoom, reserved: int, room: MessageRoom | None, since: datetime
) -> StatusFindingV1:
    """The problem while the bundled server runs with no more room than its streams set aside.

    When its streams already store more than that room it refuses every new message too, and the
    title and the summary's first sentence say so (``MESSAGES_REFUSED_TITLE``), and the action
    says that it takes messages again once its streams store less (``_flow_again``);
    otherwise the summary says its streams keep taking messages until they hold that room.
    Freeing space does not end it: the server took its room when it last started, so only a start
    of the whole instance with enough room (or streams removed) does. Its id stays the same while
    the level lasts, also when the title changes. ``room`` is the store's disk now, None when it
    could not be measured; the action names what to do before that start (``_restart_step``).
    """

    messages_refused = _refuses_messages(running)
    if messages_refused:
        title = MESSAGES_REFUSED_TITLE
        summary = (
            "The bundled message server (NATS) refuses every new message now, so requests, replies and agents' work "
            f"stop while its streams store more than its room: they store {readable_bytes(running.stored)}, more than "
            f"the {readable_bytes(running.room)} it took as its room when it last started. Its streams set aside "
            f"{readable_bytes(reserved)}, as much or more, so it refuses every new or changed stream too."
        )
    else:
        title = RUNNING_ROOM_TITLE
        summary = (
            f"The bundled message server (NATS) took {readable_bytes(running.room)} as its room when it last started, "
            f"and its streams set aside {readable_bytes(reserved)}, as much or more. So it refuses every new or changed "
            f"stream now, lowered limits too, and these fail: {_WHAT_FAILS}. Its streams keep taking messages until "
            f"they hold {readable_bytes(running.room)} in total ({readable_bytes(running.stored)} now); then it refuses "
            "every new message too."
        )
    need = _free_and_stored_needed(reserved)
    details = [
        _running_line(running),
        _set_aside_line(reserved),
        f"Free space and stored messages needed when it starts: more than {need:,} bytes",
    ]
    if room is not None:
        details += [
            f"Store folder: {room.disk.space.location}",
            f"Free on its disk: {readable_bytes(room.disk.free)} of {readable_bytes(room.disk.space.capacity)}",
            f"Stored messages (JetStream, on disk): {readable_bytes(room.stored)}",
        ]
    return StatusFindingV1(
        finding_id=f"{MESSAGE_ROOM_NOW_FINDING_PREFIX}{since.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        severity=StatusSeverity.PROBLEM,
        title=title,
        summary=f"{summary} Freeing space alone does not end this: the server takes its room only when it starts."[:2000],
        action=" ".join([
            _restart_step(room, need, messages_refused=messages_refused),
            *([_flow_again(room)] if messages_refused else []),
            "A team-wide restart keeps the message server running with the room it has. This problem ends once it runs "
            "with more room than its streams set aside.",
        ])[:1000],
        source=SOURCE,
        since=since,
        details="\n".join(details)[:2000],
    )


def _flow_again(room: MessageRoom | None) -> str:
    """The problem's sentence while the server refuses every message: it takes them again once its streams store less.

    It ends by saying that removing messages gives its next start no more room (``_NO_MORE_ROOM``),
    except while more is stored than set aside and a start would refuse every message
    (``MessageRoom.stored_decides_start``): storing less is then part of what lets that start take
    messages, as the first step's condition shows (``_start_refuses_messages``).
    """

    if room is not None and room.stored_decides_start:
        return f"{_MESSAGES_FLOW_AGAIN}."
    return f"{_MESSAGES_FLOW_AGAIN}; {_NO_MORE_ROOM}."


def _restart_step(room: MessageRoom | None, need: int, *, messages_refused: bool) -> str:
    """The problem's first step: what to do before a start of the whole instance, which ends it.

    With enough room again and the room warning's margin, restart now. Short, or above the line
    by less than that margin, first free the room warning's amount (``_to_free``), so the server
    does not start short again as other files grow; but while it refuses every message
    (``messages_refused``) and a start now has room, restart first, since that ends the stop
    (``_margin_step``). On a disk too small for that margin, restart now while there is enough
    room, or give the disk more space first when there is not. A start that would refuse every
    message while more is stored than set aside, short or not, is said as such, with the free
    space it needs instead of the room warning's amount (``_start_refuses_messages``).
    """

    restart = f"restart {_WHOLE_INSTANCE}"
    if room is None:
        return (
            "Make sure the free space on the disk holding its store and its stored messages together come to more "
            f"than {readable_bytes(need)}, then {restart}."
        )
    if room.stored_decides_start:
        return _start_refuses_messages(room, restart)
    if room.free_and_stored >= room.release_at:
        return (
            "There is enough room again (the free space and the stored messages come to "
            f"{readable_bytes(room.free_and_stored)}; it needs more than {readable_bytes(need)}), so {restart}."
        )
    to_free = _to_free(room)
    if to_free is not None:
        return _margin_step(room, to_free, restart, messages_refused=messages_refused)
    too_small = (
        "This disk is too small to keep a margin: the room warning on this page ends only once the free space and the "
        f"stored messages come to {readable_bytes(_rounded_up(room.release_at))}, more than the "
        f"{readable_bytes(room.disk.space.capacity)} it holds."
    )
    if room.short:
        return f"Give the disk more space, then {restart}. {too_small}"
    return f"There is enough room again, so {restart} now, while it has enough, then give the disk more space. {too_small}"


def _margin_step(room: MessageRoom, to_free: int, restart: str, *, messages_refused: bool) -> str:
    """The problem's first step when freeing ``to_free`` gives the room warning's margin: free it, and restart.

    Freeing comes first, so the server does not start short again as other files grow, unless the
    server refuses every message (``messages_refused``) and a start now has room: the caller has
    ruled out a start that would refuse every message, so a restart now ends that stop and the
    freeing can follow. While messages are refused, the problem's ``_flow_again`` says what
    removing messages does instead of ``_REMOVING_MESSAGES``.
    """

    amount = f"{readable_bytes(to_free)} from other files on that disk"
    margin = "so the message server does not start short again as their use grows (that also ends the room warning on this page)"
    if messages_refused and not room.short:
        return (
            f"There is enough room for a start now, so {restart} now, which makes it take new messages again; then "
            f"free {amount}, {margin}."
        )
    removing = "" if messages_refused else f" {_REMOVING_MESSAGES}"
    return f"Free {amount} first, {margin}, then {restart}.{removing}"


def _start_refuses_messages(room: MessageRoom, restart: str) -> str:
    """The problem's first step when a start now would refuse every message while more is stored than set aside.

    Streams without a byte limit can store more than what is set aside
    (``MessageRoom.stored_decides_start``). A start's room (three quarters of the free space and
    the stored bytes) is more than what is stored once the free space is more than a third of
    it, which freeing other files or a larger disk gives; that room is then more than what is
    set aside too, so a short start needs nothing more. The room warning's amount can fall short
    of that free space, so this step names the condition, not that amount.
    """

    if room.short:
        start = (
            f"A start now would give it {readable_bytes(room.room)} of room, less than its streams set aside and less "
            f"than their stored messages take ({readable_bytes(room.stored)}), so it would still refuse every new or "
            "changed stream and every new message."
        )
        both = " That start also has more room than they set aside, because they store more than that."
    else:
        start = (
            f"A start now would give it {readable_bytes(room.room)} of room, more than its streams set aside but less "
            f"than their stored messages take ({readable_bytes(room.stored)}), so it would still refuse every new "
            "message."
        )
        both = ""
    return (
        f"{start} Free space on that disk, or give it more, until the free space is more than a third of what the "
        f"stored messages take, then {restart}.{both}"
    )


def _running_state(running: RunningRoom | None, *, reserved: int, team_set_aside: int) -> str:
    """The room warning's last sentence: whether the running server already refuses, or has room for another team.

    When it refuses new streams, the sentence points at the problem, whose title says whether it
    refuses new messages too. Above that line its streams can still store more than its room
    when some have no byte limit; then it refuses every new message although no problem shows
    (``_refuses_messages``), and the sentence says so instead of calling the room enough.
    Otherwise it still refuses a new stream whose byte limit does not fit beside what is set
    aside (``MessageRoom.need_with``), so it says whether another team's streams fit. Equality
    counts as not fitting: a team added at exactly the room would leave the server refusing
    everything new. What adding a team needs is in the action only, so the figure is in one place.
    """

    if running is None:
        return (
            "This page cannot tell whether it already refuses new streams, because its monitoring endpoint did not "
            "answer; a start of the whole instance with enough room makes sure it does not."
        )
    if refuses_now(running=running.room, reserved=reserved):
        title, what = (MESSAGES_REFUSED_TITLE, "new messages and new streams") if _refuses_messages(running) else (
            RUNNING_ROOM_TITLE, "new streams"
        )
        return f'It already refuses {what} now, because it last started with too little room: see the problem "{title}".'
    if _refuses_messages(running):
        return (
            f"Running now, it refuses every new message, because its streams store {readable_bytes(running.stored)}, "
            f"more than the {readable_bytes(running.room)} of room it took when it last started; it takes new messages "
            "again once they store less."
        )
    taken = f"Running now, it has {readable_bytes(running.room)} of room, taken when it last started"
    if running.room > reserved + team_set_aside:
        return f"{taken}: enough for the streams it has and for another team's, so it needs no restart."
    return (
        f"{taken}: enough for the streams it has but not for another team's too, so adding a team is refused until "
        f"{_WHOLE_INSTANCE} starts with enough room for it."
    )


def _running_line(running: RunningRoom | None) -> str:
    """The room the server runs with and what its streams store, for the details, or that they are unknown."""

    if running is None:
        return "Room as it runs (taken when it last started): unknown (its monitoring endpoint did not answer)"
    return (
        f"Room as it runs (taken when it last started): {readable_bytes(running.room)}; its streams store "
        f"{readable_bytes(running.stored)}"
    )


def _to_free(room: MessageRoom) -> int | None:
    """What to free from files other than the message store for the room warning to end, rounded up.

    One figure: freeing it ends the warning, not just the shortfall to the line, because the
    warning's margin is room for other files on the same disk (databases, logs, workspaces) to
    grow before the next start; the streams' own growth does not move the comparison
    (``MessageRoom.free_and_stored``). Removing stored messages does not count for the same
    reason. None when freeing all the other files is not enough: the disk is too small for that
    margin. Called only while the warning's level holds, so the amount is more than 0.
    """

    needed = room.release_at - room.free_and_stored
    return _rounded_up(needed) if needed <= room.other_files else None


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
    from docker.nats_service import BUNDLED_NATS_STORE_DIR_ENV, bundled_nats_store_dir

    folder = bundled_nats_store_dir(env, logger=logger)
    if folder is None:
        if not str(env.get(BUNDLED_NATS_STORE_DIR_ENV) or "").strip():  # an ignored folder was warned about already
            logger.info(
                "The Status page does not check the message server's room: this gateway was not told where a "
                "bundled NATS server keeps its store, so it uses another server. event=storage_room_not_checked"
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
    "MESSAGES_REFUSED_TITLE",
    "RUNNING_ROOM_TITLE",
    "BundledMessageStore",
    "MessageRoom",
    "MessageStorage",
    "SpaceUse",
    "StoreDisk",
    "StorageCapacity",
    "StorageLevels",
    "StoredSize",
    "UnmeasuredDisk",
    "build_storage_capacity",
    "database_sizes",
    "disk_spaces",
    "message_room_finding",
    "no_message_limit_finding",
    "refuses_now",
    "running_room_finding",
    "space_finding",
    "store_disk",
    "tells_administrators",
]
