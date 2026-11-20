"""Storage filling up, on administrators' Status page (assistant_gateway.storage_capacity).

Covered: disks measured once per filesystem as df counts them; people's
stored-data totals that are out of date shown as a warning; database sizes
from SQLite files (with their write-ahead log) and from PostgreSQL when
``LIVE_TEST_POSTGRES_DSN`` is set; the two levels and when each was entered,
kept across restarts, with use moving around a threshold not counting as a
new entry; findings at each level and the note for stored messages without a
limit; administrators told once when a level is entered, not on every check
as the percentage moves, also after a restart, and again when a level is
entered anew; a message server that does not answer listed as unavailable;
the transport's reading of the account's limits and of what its streams set
aside; the bundled server's room warning (raised at the line and below it,
quiet above it, its three wordings, its release margin, its level kept while it
cannot be measured and ended when it no longer applies, an unreported figure
logged once, what adding a team needs, and whether the running server has room
for another team); the problem that the running server already refuses new
streams (from its room as it runs, at the line, kept after space is freed until
it runs with more room, asking for more space when the disk has only just
enough, kept while its monitoring endpoint does not answer and that endpoint
listed as unavailable, beside the warning and with a disk that cannot be
measured); on the Status page, administrators told once about the room
warning, also after a restart, and not again when the problem returns after a
check without the monitoring endpoint; the gateway checking the room only where
the launcher named the bundled server's store; and, with a real NATS server
(``LIVE_TEST_NATS_SERVER`` or ``nats-server`` on PATH), the account with and
without a JetStream limit, the room warning from its real set-aside figure, and
a server running with no more room than its streams set aside shown as a
problem on the Status page through its real monitoring endpoint.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import nats
import pytest
from nats.js.errors import APIError, ServiceUnavailableError

from assistant_gateway.config import GatewayInstanceConfig, StorageWarningSettings
from assistant_gateway.status_page import StatusPage
from assistant_gateway.storage_capacity import (
    MESSAGE_ROOM_KEY,
    MESSAGE_ROOM_NOW_KEY,
    MESSAGE_STORE_DISK_KEY,
    NEARING_LIMIT,
    NEARLY_FULL,
    NO_MESSAGE_LIMIT_FINDING_ID,
    BundledMessageStore,
    MessageRoom,
    SpaceUse,
    StorageCapacity,
    StorageLevels,
    StoreDisk,
    StoredSize,
    UnmeasuredDisk,
    build_storage_capacity,
    database_sizes,
    disk_spaces,
    store_disk,
)
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.contracts.status_v1 import StatusSeverity
from custom_transport.nats_adapter import JetStreamAccountUsage, NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper
from custom_transport.jetstream_account import BUNDLED_STORAGE_SHARE, request_account_info
from docker.nats_service import RunningRoom, bundled_running_room
from storage.config import resolve_storage_settings
from tests.knowledge_support import BACKENDS, knowledge_storage
from tests.own_nats_server import own_nats_server

START = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
GB = 1_000_000_000


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int = 5) -> None:
        self.now += timedelta(minutes=minutes)


class _Disk:
    """The instance's disk, as full as a test sets it."""

    def __init__(self, percent: float = 50.0) -> None:
        self.percent = percent

    def __call__(self) -> list[SpaceUse]:
        return [SpaceUse(key="disk:instance", label="The disk holding the instance folder",
                         used=int(self.percent * GB), capacity=100 * GB, location="/srv/instance")]


def _usage(*, storage: int, max_storage: int | None = None, reserved_storage: int | None = 0) -> JetStreamAccountUsage:
    """The account's stored messages on disk, nothing in memory."""

    return JetStreamAccountUsage(storage=storage, memory=0, max_storage=max_storage, max_memory=None,
                                 reserved_storage=reserved_storage, reserved_memory=0)


def _capacity(tmp_path: Path, disk: _Disk, clock: _Clock, *, nearing: int = 80, nearly_full: int = 95) -> StorageCapacity:
    return StorageCapacity(
        disks=disk,
        databases=lambda: [StoredSize(label="Task database", size=3 * GB, location="/srv/instance/dbs/task_history.db")],
        levels=StorageLevels(tmp_path / "state" / "storage_levels.json"),
        nearing_limit_percent=nearing,
        nearly_full_percent=nearly_full,
        message_storage_kind="file",
        now=clock,
    )


# -- measuring ------------------------------------------------------------------------------


def test_disks_are_measured_once_per_filesystem_as_df_counts_them(tmp_path: Path) -> None:
    (tmp_path / "dbs").mkdir()

    spaces = disk_spaces([
        ("disk:instance", "The disk holding the instance folder", tmp_path),
        ("disk:task_database", "The disk holding the task database", tmp_path / "dbs"),
        ("disk:gateway_database", "The disk holding the gateway database", tmp_path / "not" / "yet"),
    ])

    [space] = spaces  # all three are on one filesystem
    stats = os.statvfs(tmp_path)
    assert space.key == "disk:instance" and space.location == str(tmp_path)
    assert space.capacity == (stats.f_blocks - stats.f_bfree + stats.f_bavail) * stats.f_frsize
    assert 0 <= space.used <= space.capacity and 0.0 <= space.percent <= 100.0
    # The bundled server's store is measured the same way (MessageRoom), with NATS's own free-space figure beside it.
    single = store_disk(tmp_path / "dbs")
    assert isinstance(single, StoreDisk) and single.space.capacity == space.capacity
    assert single.space.location == str(tmp_path / "dbs") and single.space.key == "disk:message_store"


@pytest.mark.parametrize("backend", BACKENDS)
def test_database_sizes_count_sqlite_files_with_their_log_and_each_postgres_database_once(
    backend: str, tmp_path: Path
) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        if backend == "sqlite":
            path = Path(settings.sqlite_path or "")
            path.write_bytes(b"d" * 4096)
            Path(f"{path}-wal").write_bytes(b"w" * 1024)
        sizes = database_sizes([("Task database", settings), ("Gateway database", settings)])

    if backend == "sqlite":
        # Two stores in one file are listed under each name; the file is what it is.
        assert [(item.label, item.size) for item in sizes] == [("Task database", 5120), ("Gateway database", 5120)]
    else:
        [shared] = sizes
        assert shared.label == "Task database and Gateway database" and shared.location == "PostgreSQL"
        assert shared.size > 0


def test_a_database_not_created_yet_is_left_out(tmp_path: Path) -> None:
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "absent.db")}})

    assert database_sizes([("Activity database", settings)]) == []


def test_a_disk_path_permission_failure_is_not_treated_as_a_missing_folder(tmp_path: Path, monkeypatch) -> None:
    stated = Path.stat
    blocked = tmp_path / "blocked"

    def stat_path(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError(errno.EACCES, "permission denied")
        return stated(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_path)
    [reading] = disk_spaces([("disk:blocked", "The blocked disk", blocked)])
    assert reading.key == "disk:blocked" and reading.location == str(blocked)
    assert "PermissionError" in reading.error


# -- levels ----------------------------------------------------------------------------------


def test_a_level_keeps_when_it_was_entered_until_use_falls_clearly_below_it(tmp_path: Path) -> None:
    path = tmp_path / "storage_levels.json"
    levels, clock = StorageLevels(path), _Clock()

    def track(percent: float):
        clock.advance()
        return levels.track([("disk:instance", percent)], nearing=80, nearly_full=95, now=clock())

    assert track(79.9) == {}
    entered = clock.now + timedelta(minutes=5)
    assert track(80.0) == {"disk:instance": (NEARING_LIMIT, entered)}
    assert track(88.0) == {"disk:instance": (NEARING_LIMIT, entered)}
    assert track(78.0) == {"disk:instance": (NEARING_LIMIT, entered)}  # within 3 points of the threshold
    full = clock.now + timedelta(minutes=5)
    assert track(95.5) == {"disk:instance": (NEARLY_FULL, full)}
    assert track(92.5) == {"disk:instance": (NEARLY_FULL, full)}
    assert track(91.0) == {"disk:instance": (NEARING_LIMIT, entered)}  # back to the level it never left
    # A restart reads the same record.
    assert StorageLevels(path).track([("disk:instance", 85.0)], nearing=80, nearly_full=95, now=clock()) == {
        "disk:instance": (NEARING_LIMIT, entered)
    }
    assert track(76.9) == {}
    again = clock.now + timedelta(minutes=5)
    assert track(80.5) == {"disk:instance": (NEARING_LIMIT, again)}
    assert again > entered


def test_an_unreadable_record_starts_over_and_says_so(tmp_path: Path, caplog) -> None:
    path = tmp_path / "storage_levels.json"
    path.write_text("not json")

    with caplog.at_level("WARNING", logger="assistant_gateway.storage_capacity"):
        current = StorageLevels(path).track([("disk:instance", 90.0)], nearing=80, nearly_full=95, now=START)

    assert current == {"disk:instance": (NEARING_LIMIT, START)}
    assert "event=storage_levels_unreadable" in caplog.text
    assert StorageLevels(path).track([("disk:instance", 90.0)], nearing=80, nearly_full=95, now=START + timedelta(hours=1)) == {
        "disk:instance": (NEARING_LIMIT, START)
    }


# -- findings ----------------------------------------------------------------------------------


def test_findings_at_each_level(tmp_path: Path) -> None:
    disk, clock = _Disk(50.0), _Clock()
    capacity = _capacity(tmp_path, disk, clock)

    assert capacity.findings(None, unavailable=[]) == []
    disk.percent = 83.4
    [nearing] = capacity.findings(None, unavailable=[])
    disk.percent = 96.2
    clock.advance()
    [full] = capacity.findings(None, unavailable=[])

    assert nearing.severity is StatusSeverity.WARNING
    assert nearing.finding_id == "storage_nearing_limit:disk:instance:20261027T120000Z"
    assert nearing.title == "The disk holding the instance folder is 83% full"
    assert nearing.summary.startswith("16.6 GB of 100 GB is free. When it is full, agents cannot save files")
    assert "told again at 95% full" in nearing.action
    assert nearing.details == (
        "Folder: /srv/instance\nTask database: 3 GB (/srv/instance/dbs/task_history.db)"
    )
    assert nearing.since == START
    assert full.severity is StatusSeverity.PROBLEM
    assert full.finding_id == "storage_nearly_full:disk:instance:20261027T120500Z"
    assert full.summary.startswith("Only 3.8 GB of 100 GB is free.")


def test_the_thresholds_follow_the_settings(tmp_path: Path) -> None:
    disk = _Disk(72.0)
    capacity = _capacity(tmp_path, disk, _Clock())

    assert capacity.findings(None, unavailable=[]) == []
    capacity.nearing_limit_percent, capacity.nearly_full_percent = 70, 90  # changed in Settings
    [finding] = capacity.findings(None, unavailable=[])

    assert finding.severity is StatusSeverity.WARNING and "told again at 90% full" in finding.action
    with pytest.raises(ValueError, match="below nearly_full_percent"):
        StorageWarningSettings(nearing_limit_percent=95, nearly_full_percent=95)
    with pytest.raises(ValueError, match="from 1 to 100"):
        StorageWarningSettings(nearly_full_percent=101)


def test_stored_messages_are_measured_against_the_accounts_limit_or_noted_without_one(tmp_path: Path) -> None:
    capacity = _capacity(tmp_path, _Disk(10.0), _Clock())

    [limited] = capacity.findings(
        _usage(storage=9 * GB, max_storage=10 * GB, reserved_storage=2_300_000_000), unavailable=[]
    )
    [unlimited] = capacity.findings(_usage(storage=12_000_000, reserved_storage=2_300_000_000), unavailable=[])
    [unknown] = capacity.findings(_usage(storage=12_000_000, reserved_storage=None), unavailable=[])

    assert limited.finding_id.startswith("storage_nearing_limit:message_storage:file:")
    assert limited.title == "The message server's storage is 90% full"
    assert "refuses new messages" in limited.summary and "Stored messages (JetStream): 9 GB" in limited.details
    assert "counts the bytes stored, not the space streams set aside" in limited.summary
    assert "refuse a new or changed stream well before then" in limited.summary
    assert "transport.queues.max_bytes" in limited.action
    assert limited.details.endswith("Stored messages (JetStream): 9 GB\nSet aside by its streams: 2.3 GB")
    assert unlimited.finding_id == NO_MESSAGE_LIMIT_FINDING_ID
    assert unlimited.severity is StatusSeverity.NOTE
    assert "no limit for stored messages (JetStream), which take 12 MB now" in unlimited.summary
    assert "three quarters of the disk space that was free or held its messages when it started" in unlimited.summary
    assert unlimited.details == "Stored messages (JetStream): 12 MB\nSet aside by its streams: 2.3 GB"
    # Not the bundled server: its disk is not one this page measures.
    assert "watch that disk too" in unlimited.action
    assert "free space and the server's stored messages together need to be a third more than what the streams set aside" in (
        unlimited.action
    )
    assert unknown.details.endswith("Set aside by its streams: unknown (the server does not report it)")


def _adapter_answering(reply: dict) -> NatsTransportAdapter:
    """An adapter whose connection answers the raw account information request with ``reply``."""

    adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id="team"))

    async def request(subject: str, data: bytes, timeout: float) -> SimpleNamespace:
        assert subject == "$JS.API.INFO"
        return SimpleNamespace(data=json.dumps(reply).encode())

    adapter._nc = SimpleNamespace(request=request)
    adapter._js = SimpleNamespace()
    return adapter


def test_the_transport_reads_no_limit_and_zero_as_no_limit_and_what_the_streams_set_aside() -> None:
    def usage(**reply: object) -> JetStreamAccountUsage | None:
        return asyncio.run(_adapter_answering({"storage": 2048, "memory": 10, **reply}).jetstream_account_usage())

    unlimited = usage(limits={"max_storage": -1, "max_memory": 0}, reserved_storage=5 << 20, reserved_memory=0)
    limited = usage(limits={"max_storage": 4096, "max_memory": -1}, reserved_storage=0, reserved_memory=1024)
    unreported = usage(limits={"max_storage": -1, "max_memory": -1})
    absent = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id="team"))

    assert unlimited == JetStreamAccountUsage(storage=2048, memory=10, max_storage=None, max_memory=None,
                                              reserved_storage=5 << 20, reserved_memory=0)
    assert limited is not None and limited.max_storage == 4096 and limited.max_memory is None
    assert (limited.reserved_storage, limited.reserved_memory) == (0, 1024)
    # nats-py's AccountInfo has no such figures; a server that leaves them out is unknown, not 0.
    assert unreported is not None and (unreported.reserved_storage, unreported.reserved_memory) == (None, None)
    assert asyncio.run(absent.jetstream_account_usage()) is None  # not connected, or no JetStream


def test_a_refused_account_information_request_raises_so_the_page_lists_the_server_as_unavailable() -> None:
    adapter = _adapter_answering({"error": {"code": 503, "err_code": 10039, "description": "jetstream not enabled"}})

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(adapter.jetstream_account_usage())


# -- the bundled server's room -------------------------------------------------------------------

STORE = Path("/data/services/nats/data")
RESERVED = 3 * GB  # what the account's streams set aside: free space and stored bytes must come to more than 4 GB
NEED = 4 * GB
TEAM_SET_ASIDE = 2_300_000_000
STORE_DISK = 10 * GB  # three percent of it, the release margin, is 300 MB
STORED = 12_000_000  # what the account's streams store, unless a test says otherwise
ROOM_ENOUGH = 6 * GB  # the room a server runs with when it started with 8 GB of free space and stored bytes


class _StoreDisk:
    """The disk holding the bundled server's store, with as much free space as a test sets.

    ``stored`` is the part of the used space the message store takes, so ``free`` plus it is
    what the room is a share of; the rest of the used space is other files.
    """

    def __init__(self, free: int, capacity: int = STORE_DISK) -> None:
        self.free, self.capacity = free, capacity
        self.measured: list[Path] = []

    def __call__(self, folder: Path) -> StoreDisk | UnmeasuredDisk:
        self.measured.append(folder)
        space = SpaceUse(key=MESSAGE_STORE_DISK_KEY, label="The disk holding the message server's store",
                         used=self.capacity - self.free, capacity=self.capacity, location=str(folder))
        return StoreDisk(space=space, free=self.free)


def _counting(together: int, capacity: int = STORE_DISK, stored: int = STORED) -> _StoreDisk:
    """A store disk on which the free space and ``stored`` come to ``together``."""

    return _StoreDisk(free=together - stored, capacity=capacity)


class _RunningRoom:
    """The bundled server's monitoring endpoint: the room it runs with and what it stores, or a failure to answer."""

    def __init__(self, room: int | Exception = ROOM_ENOUGH, stored: int = STORED) -> None:
        self.room, self.stored = room, stored
        self.reads = 0

    def __call__(self) -> RunningRoom:
        self.reads += 1
        if isinstance(self.room, Exception):
            raise self.room
        return RunningRoom(room=self.room, stored=self.stored)


def _bundled(
    tmp_path: Path, store_disk, clock: _Clock, *, kind: str = "file", bundled: bool = True,
    running: _RunningRoom | None = None, disk: _Disk | None = None,
) -> StorageCapacity:
    """The check on the bundled server, its levels in one file per test (a new one is what a restart builds).

    The running room is enough unless the test says otherwise; no test reaches a real monitoring port.
    """

    return StorageCapacity(
        disks=disk or _Disk(10.0), databases=list, levels=StorageLevels(tmp_path / "levels.json"),
        nearing_limit_percent=80, nearly_full_percent=95, message_storage_kind=kind,
        message_store=BundledMessageStore(folder=STORE, team_set_aside=TEAM_SET_ASIDE) if bundled else None,
        measure_store=store_disk, read_running_room=running or _RunningRoom(), now=clock,
    )


def _check(capacity: StorageCapacity, usage: JetStreamAccountUsage | None = None, unavailable: list[str] | None = None):
    """This check's findings; ``unavailable`` collects the sources it could not ask, as the Status page's list."""

    return capacity.findings(
        _usage(storage=STORED, reserved_storage=RESERVED) if usage is None else usage,
        unavailable=[] if unavailable is None else unavailable,
    )


def _one(found, prefix: str):
    matching = [finding for finding in found if finding.finding_id.startswith(prefix)]
    assert len(matching) <= 1
    return matching[0] if matching else None


def _room(capacity: StorageCapacity, usage: JetStreamAccountUsage | None = None):
    """The room warning among this check's findings, or None."""

    return _one(_check(capacity, usage), "storage_message_room:")


def _rooms(capacity: StorageCapacity, usage: JetStreamAccountUsage | None = None, unavailable: list[str] | None = None):
    """This check's problem that the server refuses new streams now, and its room warning; each or None."""

    found = _check(capacity, usage, unavailable)
    return _one(found, "storage_message_room_now:"), _one(found, "storage_message_room:")


def _saved_room_level(tmp_path: Path, key: str = MESSAGE_ROOM_KEY) -> bool:
    saved = json.loads((tmp_path / "levels.json").read_text())["levels"] if (tmp_path / "levels.json").is_file() else {}
    return key in saved


RULE = (
    "Each time it starts, the bundled message server (NATS) takes three quarters of the free space on the disk holding "
    "its store, plus three quarters of what its stored messages take, as its room. Its streams set aside 3 GB, so the "
    "free space and the stored messages must together come to more than 4 GB;"
)
WHAT_FAILS = (
    "the attachment store Web Chat creates the first time it copies an attachment through the message server, "
    "adding a team, and a start after a change to the message stream settings (that start stops with an error)"
)
NO_RESTART_NEEDED = (
    "Running now, it has 6 GB of room, taken when it last started: enough for the streams it has and for another "
    "team's, so it needs no restart."
)
REMOVING = (
    "Removing messages from the message server does not help: it counts its stored messages back into its room when "
    "it starts."
)
# What every room warning's action ends with: when the room is measured, and what adding a team needs (2.3 GB on
# top of the 3 GB, so more than 7.07 GB of free space and stored bytes, shown rounded up as 7.1 GB).
WARNING_ENDS = (
    f"{REMOVING} The message server measures its room only when the whole instance (the container, or ua start) "
    "starts; a team-wide restart keeps it running. Each team's work and dead-letter streams set aside 2.3 GB at this "
    "instance's settings (transport.queues.max_bytes sets how much), so adding a team needs the free space and the "
    "stored messages on that disk to come to at least 7.1 GB when the message server starts."
)


def test_the_room_warning_says_what_fails_when_messages_stop_and_how_much_to_free(tmp_path: Path) -> None:
    # 3 GB free and 240 MB stored: the room at the next start is three quarters of 3.24 GB, 2.43 GB, less than 3 GB.
    store_disk, stored = _StoreDisk(free=3 * GB), 240_000_000

    finding = _room(_bundled(tmp_path, store_disk, _Clock(), running=_RunningRoom(ROOM_ENOUGH, stored=stored)),
                    _usage(storage=stored, reserved_storage=RESERVED))

    assert finding is not None and finding.severity is StatusSeverity.WARNING
    assert finding.finding_id == "storage_message_room:20261027T120000Z" and finding.since == START
    assert finding.title == "The message server will have less room than its streams set aside"
    # Its streams keep taking messages until they hold that room, then every message is refused.
    assert finding.summary == (
        f"{RULE} they come to 3.2 GB (3 GB free and 240 MB stored). After its next start it will have 2.4 GB of room "
        f"and refuse every new or changed stream, so these fail: {WHAT_FAILS}. Its streams keep taking messages until "
        "they hold 2.4 GB in total (240 MB now); then it refuses every new message, so requests, replies and agents' "
        f"work stop. {NO_RESTART_NEEDED}"
    )
    # One amount, which ends the warning: up to 4.3 GB and a byte (the 3% margin), 1.06 GB more, shown as 1.1 GB.
    assert finding.action == (
        f"Free 1.1 GB from other files on that disk before the message server next starts; that ends this warning. "
        f"{WARNING_ENDS}"
    )
    assert finding.details == (
        "Store folder: /data/services/nats/data\n"
        "Free on its disk: 3 GB of 10 GB\n"
        "Stored messages (JetStream, on disk): 240 MB\n"
        "Room at the next start (three quarters of the free space and the stored messages): 2.4 GB\n"
        "Room as it runs (taken when it last started): 6 GB; its streams store 240 MB\n"
        "Set aside by its streams: 3 GB\n"
        "Free space and stored messages needed: more than 4,000,000,000 bytes; the warning ends at 4.4 GB\n"
        "Each team's work and dead-letter streams: 2.3 GB (transport.queues.max_bytes)\n"
        "Free space and stored messages adding a team needs when the message server starts: more than "
        "7,066,666,667 bytes"
    )
    assert store_disk.measured == [STORE]


def test_the_room_warning_says_messages_stop_at_once_when_the_streams_store_more_than_that_room(tmp_path: Path) -> None:
    # 500 MB free and 2 GB stored: the room at the next start, 1.875 GB, is less than what the streams store.
    finding = _room(_bundled(tmp_path, _StoreDisk(free=500_000_000), _Clock()),
                    _usage(storage=2 * GB, reserved_storage=RESERVED))

    assert finding is not None
    assert finding.summary.startswith(
        f"{RULE} they come to 2.5 GB (500 MB free and 2 GB stored). After its next start it will have 1.9 GB of room, "
        "less than its stored messages take, so it will refuse every new message at once, as well as every new or "
        "changed stream: requests, replies and agents' work stop. Running now"
    )
    assert "keep taking messages" not in finding.summary
    assert finding.action.startswith("Free 1.9 GB from other files on that disk before the message server next starts")


@pytest.mark.parametrize(
    ("running", "shown"),
    [(4 * GB, "4 GB"), (RESERVED + TEAM_SET_ASIDE - 40_000_000, "5.3 GB"), (RESERVED + TEAM_SET_ASIDE, "5.3 GB")],
    ids=["between", "40-mb-short", "exactly-with-a-team"],
)
def test_the_room_warning_says_when_the_running_server_refuses_another_team(
    tmp_path: Path, running: int, shown: str
) -> None:
    # Above the line it refuses nothing it has, but a new team's streams set aside 2.3 GB more, and NATS
    # refuses a stream whose limit does not fit beside what is set aside (measured on 2.15.1). A team that
    # fits exactly would leave it refusing everything new, so that counts as not fitting. What adding a
    # team needs is said once, in the action.
    finding = _room(_bundled(tmp_path, _counting(3_250_000_000), _Clock(), running=_RunningRoom(running)))

    assert finding is not None
    assert finding.summary.endswith(
        f"Running now, it has {shown} of room, taken when it last started: enough for the streams it has but not for "
        "another team's too, so adding a team is refused until the whole instance (the container, or ua start) starts "
        "with enough room for it."
    )
    assert finding.action.count("adding a team") == 1


def test_the_room_warning_starts_at_the_line_and_not_above_it(tmp_path: Path) -> None:
    at_line = _room(_bundled(tmp_path / "at", _counting(NEED), _Clock()))
    above = _room(_bundled(tmp_path / "above", _counting(NEED + 1), _Clock()))
    nothing_set_aside = _room(_bundled(tmp_path / "none", _StoreDisk(free=0), _Clock()),
                              _usage(storage=0, reserved_storage=0))

    # NATS refuses every new stream when its room equals what is set aside (measured on 2.15.1).
    assert at_line is not None and at_line.action.startswith("Free 400 MB from other files on that disk")
    assert above is None
    assert nothing_set_aside is None and not _saved_room_level(tmp_path / "none")


def test_the_room_warning_keeps_its_id_through_the_margin_and_a_restart_then_ends_and_starts_anew(
    tmp_path: Path,
) -> None:
    store_disk, clock = _counting(3_500_000_000), _Clock()
    capacity = _bundled(tmp_path, store_disk, clock)

    entered = _room(capacity)
    assert entered is not None
    store_disk.free = 3_800_000_000 - STORED  # still short, by less
    clock.advance()
    assert _room(capacity).finding_id == entered.finding_id
    store_disk.free = 4_200_000_000 - STORED  # enough room again, by only 200 MB
    clock.advance()
    band = _room(capacity)
    clock.advance()
    after_restart = _room(_bundled(tmp_path, store_disk, clock))  # a restart reads the same record
    store_disk.free = NEED + 300_000_000 - STORED  # exactly the margin: kept
    clock.advance()
    edge = _room(capacity)
    store_disk.free = NEED + 300_000_001 - STORED  # past it: ended
    clock.advance()
    ended, kept_after_end = _room(capacity), _saved_room_level(tmp_path)
    store_disk.free = 3_900_000_000 - STORED  # other files fill the disk again later
    clock.advance(minutes=60 * 24)
    again = _room(capacity)

    assert band is not None and band.finding_id == entered.finding_id and band.severity is StatusSeverity.WARNING
    assert band.title == "The message server's room is only just above what its streams set aside"
    assert band.summary == (
        f"{RULE} they come to 4.2 GB (4.2 GB free and 12 MB stored). So a start now would give it enough room for the "
        "streams it has, but they come to only 200 MB more than it needs. The warning stays until they come to 4.4 GB, "
        f"so it does not come and go as other files on that disk grow and shrink. {NO_RESTART_NEEDED}"
    )
    # The same one amount, which ends it: 100 MB and a byte, shown rounded up.
    assert band.action == f"Free 200 MB from other files on that disk; that ends this warning. {WARNING_ENDS}"
    assert after_restart is not None and after_restart.finding_id == entered.finding_id
    assert edge is not None and edge.finding_id == entered.finding_id
    assert ended is None and not kept_after_end
    assert again is not None and again.finding_id != entered.finding_id and again.since == clock.now


def test_removing_stored_messages_neither_ends_the_room_warning_nor_changes_what_to_free(tmp_path: Path) -> None:
    # 2 GB free and 2 GB stored, nothing else on the disk but 6 GB of other files: exactly at the line.
    store_disk, clock = _StoreDisk(free=2 * GB), _Clock()
    capacity = _bundled(tmp_path, store_disk, clock)
    entered = _room(capacity, _usage(storage=2 * GB, reserved_storage=RESERVED))
    # An administrator purges 1.1 GB of messages: the free space grows by what the stored bytes shrink by.
    store_disk.free = 3_100_000_000
    clock.advance()
    purged = _room(capacity, _usage(storage=900_000_000, reserved_storage=RESERVED))
    # Freeing 300 MB and a byte from other files reaches the margin and ends it.
    store_disk.free = 3_400_000_001
    clock.advance()
    freed = _room(capacity, _usage(storage=900_000_000, reserved_storage=RESERVED))

    assert entered is not None and entered.action.startswith("Free 400 MB from other files on that disk")
    assert REMOVING in entered.action
    # The next start's room is still three quarters of 4 GB: the same warning, the same amount to free.
    assert purged is not None and purged.finding_id == entered.finding_id
    assert purged.action.startswith("Free 400 MB from other files on that disk")
    assert "they come to 4 GB (3.1 GB free and 900 MB stored)" in purged.summary
    assert freed is None and not _saved_room_level(tmp_path)


@pytest.mark.parametrize(
    "frees",
    [[GB], [GB, 4_050_000_000]],  # short; entered, then above the line by less than the margin
    ids=["short", "only-just-enough"],
)
def test_a_disk_too_small_for_the_margin_says_so(tmp_path: Path, frees: list[int]) -> None:
    # 4 GB and 3% of 4.1 GB is 4.123 GB, more than the disk holds, so no amount freed ends the warning.
    store_disk, clock = _StoreDisk(free=frees[0], capacity=4_100_000_000), _Clock()
    check = _bundled(tmp_path, store_disk, clock)
    for free in frees:
        store_disk.free = free
        clock.advance()
        finding = _room(check)

    assert finding is not None
    assert finding.action == (
        "This disk is too small to end this warning: it ends only once the free space and the stored messages come to "
        "4.2 GB, more than the 4.1 GB it holds. Give it more space, or lower transport.queues.max_bytes and make a "
        "team-wide restart, which lowers the streams' limits so they set aside less. Do that before the whole instance "
        "next starts: a message server that has started with too little room refuses lowered limits too. "
        f"{WARNING_ENDS}"
    )
    assert len(finding.action) < 1000  # nothing cut off


def test_freeing_files_counts_only_what_other_files_take(tmp_path: Path) -> None:
    # 6 GB set aside needs more than 8 GB; 8 GB and 3% of an 8.2 GB disk is 8.246 GB, more than the disk holds.
    # With 1 GB free and 5 GB stored only 2.2 GB of the used space is other files, less than the 2.246 GB that
    # would take: although 7.2 GB is used, the disk is too small for the margin.
    finding = _room(_bundled(tmp_path, _StoreDisk(free=GB, capacity=8_200_000_000), _Clock()),
                    _usage(storage=5 * GB, reserved_storage=6 * GB))

    assert finding is not None
    assert finding.action.startswith(
        "This disk is too small to end this warning: it ends only once the free space and the stored messages come to "
        "8.3 GB, more than the 8.2 GB it holds."
    )


@pytest.mark.parametrize("silent", [False, True], ids=["refusing", "endpoint-silent"])
def test_a_disk_too_small_offers_lowered_limits_only_while_the_server_is_known_to_accept_them(
    tmp_path: Path, silent: bool
) -> None:
    # A server that refuses new streams refuses lowered limits too, and then the team's start fails; while
    # its monitoring endpoint is silent this page cannot tell, so it offers only more space.
    running = _RunningRoom(TimeoutError("timed out") if silent else RESERVED)
    store_disk = _StoreDisk(free=GB, capacity=3_500_000_000)  # even an empty disk is short

    finding = _room(_bundled(tmp_path, store_disk, _Clock(), running=running))

    assert finding is not None and finding.action.startswith(
        "This disk is too small to end this warning: it ends only once the free space and the stored messages come to "
        f"4.2 GB, more than the 3.5 GB it holds. Give it more space. {REMOVING}"
    )
    assert "transport.queues.max_bytes and" not in finding.action


def test_the_room_warning_keeps_its_level_while_it_cannot_be_measured(tmp_path: Path, monkeypatch, caplog) -> None:
    store_disk, clock = _counting(3_500_000_000), _Clock()
    capacity = _bundled(tmp_path, store_disk, clock)
    entered = _room(capacity)
    assert entered is not None

    clock.advance()
    no_server = capacity.findings(None, unavailable=[])  # the message server does not answer
    clock.advance()
    with caplog.at_level("WARNING", logger="assistant_gateway.storage_capacity"):
        unreported = [capacity.findings(_usage(storage=1, reserved_storage=None), unavailable=[]) for _ in range(3)]
    clock.advance()
    # The store's disk cannot be read: measured for real, its statvfs failing.
    original = os.statvfs

    def read_space(path):
        if Path(path) == tmp_path / "store":
            raise OSError(errno.EIO, "input/output error")
        return original(path)

    (tmp_path / "store").mkdir()
    monkeypatch.setattr(os, "statvfs", read_space)
    unreadable = StorageCapacity(
        disks=list, databases=list, levels=capacity.levels, nearing_limit_percent=80, nearly_full_percent=95,
        message_storage_kind="file", message_store=BundledMessageStore(folder=tmp_path / "store", team_set_aside=1),
        read_running_room=_RunningRoom(), now=clock,
    ).findings(_usage(storage=1, reserved_storage=RESERVED), unavailable=[])
    monkeypatch.setattr(os, "statvfs", original)
    clock.advance()
    back = _room(capacity)

    assert [finding.finding_id for finding in no_server] == []
    assert all(NO_MESSAGE_LIMIT_FINDING_ID in [f.finding_id for f in found] for found in unreported)
    assert all(not any(f.finding_id.startswith("storage_message_room:") for f in found) for found in unreported)
    assert caplog.text.count("event=storage_reserved_unknown") == 1  # logged once, not at every check
    [problem] = [finding for finding in unreadable if finding.finding_id.startswith("storage_unmeasured:")]
    assert problem.finding_id == "storage_unmeasured:disk:message_store" and problem.severity is StatusSeverity.PROBLEM
    assert not any(finding.finding_id.startswith("storage_message_room:") for finding in unreadable)
    assert back is not None and back.finding_id == entered.finding_id  # no second notice


@pytest.mark.parametrize("change", ["account_limit", "external_server"])
def test_the_room_findings_end_when_the_check_no_longer_applies(tmp_path: Path, change: str) -> None:
    store_disk, clock, refusing = _counting(3_500_000_000), _Clock(), _RunningRoom(RESERVED)
    entered_now, entered = _rooms(_bundled(tmp_path, store_disk, clock, running=refusing))
    assert entered_now is not None and entered is not None

    clock.advance()
    changed = _bundled(tmp_path, store_disk, clock, bundled=change != "external_server", running=refusing)
    limit = 100 * GB if change == "account_limit" else None
    found = changed.findings(_usage(storage=STORED, max_storage=limit, reserved_storage=RESERVED), unavailable=[])
    measured, reads = len(store_disk.measured), refusing.reads
    saved = _saved_room_level(tmp_path) or _saved_room_level(tmp_path, MESSAGE_ROOM_NOW_KEY)
    clock.advance()
    back_now, back = _rooms(_bundled(tmp_path, store_disk, clock, running=refusing))

    assert not any(finding.finding_id.startswith("storage_message_room") for finding in found)
    assert not saved  # both levels ended
    assert measured == 1 and reads == 1  # only the first check measured the store's disk and read the endpoint
    # A new notice for each when it returns.
    assert back is not None and back.finding_id != entered.finding_id
    assert back_now is not None and back_now.finding_id != entered_now.finding_id


def test_memory_storage_keeps_the_room_check_on_the_file_streams_and_notes_both_set_aside_figures(
    tmp_path: Path,
) -> None:
    # transport.storage: memory moves only the attachment store into memory; the protocol 1 streams are file
    # streams in the message contract, so their set-aside storage still needs room on the store's disk.
    usage = JetStreamAccountUsage(storage=STORED, memory=5_000_000, max_storage=None, max_memory=None,
                                  reserved_storage=RESERVED, reserved_memory=64_000_000)

    found = _check(_bundled(tmp_path, _counting(3_500_000_000), _Clock(), kind="memory",
                            running=_RunningRoom(RESERVED)), usage)

    note, now, warning = (_one(found, prefix) for prefix in
                          (NO_MESSAGE_LIMIT_FINDING_ID, "storage_message_room_now:", "storage_message_room:"))
    assert now is not None and warning is not None
    assert "Its streams set aside 3 GB" in warning.summary and "Stored messages (JetStream, on disk): 12 MB" in warning.details
    assert note is not None and note.details == (
        "Stored messages (JetStream): 5 MB\nSet aside by its streams in memory: 64 MB\nSet aside by its streams on disk: 3 GB"
    )
    assert note.action.startswith("This page warns when the free space and the stored messages on the disk holding")
    assert note.action.endswith("not the memory of the NATS server's machine; watch that too.")


def test_the_store_disk_counts_free_space_in_the_block_size_nats_uses(tmp_path: Path, monkeypatch) -> None:
    # nats-server takes its room from f_bavail blocks of f_bsize (server/disk_avail.go); df, and every
    # percentage on the page, counts blocks of f_frsize. The two differ only on some filesystems.
    def read_space(path):
        # bsize, frsize, blocks, bfree, bavail, files, ffree, favail, flag, namemax
        return os.statvfs_result((4096, 1024, 1000, 400, 300, 0, 0, 0, 0, 255))

    monkeypatch.setattr(os, "statvfs", read_space)

    disk = store_disk(tmp_path / "store")

    assert isinstance(disk, StoreDisk)
    assert disk.free == 300 * 4096
    assert (disk.space.used, disk.space.capacity, disk.space.free) == (600 * 1024, 900 * 1024, 300 * 1024)
    assert disk.space.key == MESSAGE_STORE_DISK_KEY and disk.space.location == str(tmp_path)


WHOLE_RESTART = "restart the whole instance (the container, or ua start)."
NOW_ENDS = (
    "A team-wide restart keeps the message server running with the room it has. This problem ends once it runs "
    "with more room than its streams set aside."
)


def test_a_server_that_started_with_too_little_room_is_a_problem_beside_the_warning(tmp_path: Path) -> None:
    # It last started with 3.2 GB of free space and stored bytes, so it runs with 2.4 GB, less than the 3 GB set aside.
    store_disk = _counting(3_250_000_000)

    now, warning = _rooms(_bundled(tmp_path, store_disk, _Clock(), running=_RunningRoom(2_400_000_000, stored=240_000_000)))

    assert now is not None and now.severity is StatusSeverity.PROBLEM
    assert now.finding_id == "storage_message_room_now:20261027T120000Z" and now.since == START
    assert now.title == "The message server refuses new streams now"
    assert now.summary == (
        "The bundled message server (NATS) took 2.4 GB as its room when it last started, and its streams set aside "
        "3 GB, as much or more. So it refuses every new or changed stream now, lowered limits too, and these fail: "
        f"{WHAT_FAILS}. Its streams keep taking messages until they hold 2.4 GB in total (240 MB now); then it "
        "refuses every new message too. Freeing space alone does not end this: the server takes its room only when "
        "it starts."
    )
    assert now.action == (
        "Free 1.1 GB from other files on that disk first, so the message server does not start short again as their "
        f"use grows (that also ends the room warning on this page), then {WHOLE_RESTART[:-1]}. {REMOVING} {NOW_ENDS}"
    )
    assert now.details == (
        "Room as it runs (taken when it last started): 2.4 GB; its streams store 240 MB\n"
        "Set aside by its streams: 3 GB\n"
        "Free space and stored messages needed when it starts: more than 4,000,000,000 bytes\n"
        "Store folder: /data/services/nats/data\n"
        "Free on its disk: 3.2 GB of 10 GB\n"
        "Stored messages (JetStream, on disk): 12 MB"
    )
    # The warning still says what to free; it points at the problem instead of saying no restart is needed.
    assert warning is not None and warning.action.startswith("Free 1.1 GB from other files on that disk")
    assert warning.summary.endswith(
        'It already refuses new streams now, because it last started with too little room: see the problem '
        '"The message server refuses new streams now".'
    )


def test_a_server_whose_streams_store_more_than_its_room_refuses_messages_now_under_the_same_problem(
    tmp_path: Path,
) -> None:
    store_disk, clock, running = _counting(3_250_000_000), _Clock(), _RunningRoom(2_400_000_000, stored=2_300_000_000)
    capacity = _bundled(tmp_path, store_disk, clock, running=running)
    streams_only, _ = _rooms(capacity)
    running.stored = 2_400_000_000  # exactly the room: NATS refuses a message only once they store more
    clock.advance()
    at_room, _ = _rooms(capacity)
    running.stored = 2_500_000_000
    clock.advance()
    messages, warning = _rooms(capacity)

    assert streams_only is not None and at_room is not None and at_room.title == "The message server refuses new streams now"
    # The worse state leads, under its own title; the same level, so administrators are not told twice.
    assert messages is not None and messages.finding_id == streams_only.finding_id
    assert messages.title == "The message server refuses new messages now"
    assert messages.summary == (
        "The bundled message server (NATS) refuses every new message now, so requests, replies and agents' work have "
        "stopped: its streams store 2.5 GB, more than the 2.4 GB it took as its room when it last started. Its streams "
        "set aside 3 GB, as much or more, so it refuses every new or changed stream too. Freeing space alone does not "
        "end this: the server takes its room only when it starts."
    )
    assert "keep taking messages" not in messages.summary
    assert warning is not None and warning.summary.endswith(
        'It already refuses new messages and new streams now, because it last started with too little room: see the '
        'problem "The message server refuses new messages now".'
    )


def test_the_problem_starts_when_the_running_room_equals_what_is_set_aside(tmp_path: Path) -> None:
    roomy = _counting(9 * GB)  # no warning: only the running room decides the problem
    at_line = _rooms(_bundled(tmp_path / "at", roomy, _Clock(), running=_RunningRoom(RESERVED)))
    above = _rooms(_bundled(tmp_path / "above", roomy, _Clock(), running=_RunningRoom(RESERVED + 1)))
    nothing_set_aside = _rooms(_bundled(tmp_path / "none", _StoreDisk(free=0), _Clock(), running=_RunningRoom(0)),
                               _usage(storage=0, reserved_storage=0))

    # At a room equal to what is set aside every new stream is refused (measured on 2.15.1).
    assert at_line[0] is not None and at_line[1] is None
    assert at_line[0].action == (
        "There is enough room again (the free space and the stored messages come to 9 GB; it needs more than 4 GB), "
        f"so {WHOLE_RESTART} {NOW_ENDS}"
    )
    assert above == (None, None)
    assert nothing_set_aside == (None, None)
    assert not _saved_room_level(tmp_path / "none", MESSAGE_ROOM_NOW_KEY)


def test_the_problem_stays_after_space_is_freed_until_the_server_runs_with_more_room(tmp_path: Path) -> None:
    store_disk, clock, running = _counting(3_500_000_000), _Clock(), _RunningRoom(RESERVED)
    capacity = _bundled(tmp_path, store_disk, clock, running=running)
    entered, _ = _rooms(capacity)
    assert entered is not None

    store_disk.free = 8 * GB - STORED  # space freed, no restart of the whole instance yet
    clock.advance()
    freed, freed_warning = _rooms(capacity)
    clock.advance()
    after_gateway_restart, _ = _rooms(_bundled(tmp_path, store_disk, clock, running=running))
    running.room = ROOM_ENOUGH  # the whole instance started again with 8 GB of free space and stored bytes
    clock.advance()
    ended = _rooms(capacity)
    kept_after_end = _saved_room_level(tmp_path, MESSAGE_ROOM_NOW_KEY)
    running.room = RESERVED  # it starts with too little room again later
    clock.advance(minutes=60 * 24)
    again, _ = _rooms(capacity)

    assert freed is not None and freed.finding_id == entered.finding_id and freed_warning is None
    assert freed.action.startswith("There is enough room again (the free space and the stored messages come to 8 GB;")
    assert after_gateway_restart is not None and after_gateway_restart.finding_id == entered.finding_id
    assert ended == (None, None) and not kept_after_end
    assert again is not None and again.finding_id != entered.finding_id and again.since == clock.now


@pytest.mark.parametrize(
    ("capacity", "together", "first"),
    [(STORE_DISK, NEED + 100_000_000,
      "Free 300 MB from other files on that disk first, so the message server does not start short again as their "
      f"use grows (that also ends the room warning on this page), then {WHOLE_RESTART[:-1]}. {REMOVING}"),
     # The warning could not end on this disk (4 GB and 3% of 4.1 GB, rounded up), so freeing more first cannot
     # be done: restart while there is enough.
     (4_100_000_000, 4_050_000_000,
      "There is enough room again, so restart the whole instance (the container, or ua start) now, while it has "
      "enough, then give the disk more space. This disk is too small to keep a margin: the room warning on this page "
      "ends only once the free space and the stored messages come to 4.2 GB, more than the 4.1 GB it holds.")],
    ids=["margin-reachable", "disk-too-small-for-the-margin"],
)
def test_the_problem_asks_for_more_space_when_the_disk_has_only_just_enough(
    tmp_path: Path, capacity: int, together: int, first: str
) -> None:
    store_disk, clock = _StoreDisk(free=GB, capacity=capacity), _Clock()
    capacity_check = _bundled(tmp_path, store_disk, clock, running=_RunningRoom(RESERVED))
    _rooms(capacity_check)  # short: both entered
    # Then above the line by less than the room warning's margin: a restart now could start short again.
    store_disk.free = together - STORED
    clock.advance()

    now, warning = _rooms(capacity_check)

    assert now is not None and now.action == f"{first} {NOW_ENDS}"
    # The warning's band wording, while the server refuses: a start would give it room, it has none now.
    assert warning is not None
    assert "So a start now would give it enough room for the streams it has" in warning.summary
    assert warning.summary.endswith(
        'It already refuses new streams now, because it last started with too little room: see the problem '
        '"The message server refuses new streams now".'
    )


@pytest.mark.parametrize(
    "failure",
    [ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), TimeoutError("timed out"),
     ValueError("the monitoring endpoint reports no jetstream.config.max_storage (got None)")],
    ids=["refused", "timeout", "no-figure"],
)
def test_the_problem_keeps_its_level_while_the_monitoring_endpoint_does_not_answer(
    tmp_path: Path, caplog, failure: Exception
) -> None:
    store_disk, clock, running = _counting(3_500_000_000), _Clock(), _RunningRoom(RESERVED)
    capacity = _bundled(tmp_path, store_disk, clock, running=running)
    entered, _ = _rooms(capacity)
    assert entered is not None

    running.room = failure
    with caplog.at_level("INFO", logger="assistant_gateway.storage_capacity"):
        silent, unavailable = [], []
        for _ in range(3):
            clock.advance()
            listed: list[str] = []
            silent.append(_rooms(capacity, unavailable=listed))
            unavailable.append(listed)
        kept = _saved_room_level(tmp_path, MESSAGE_ROOM_NOW_KEY)
        running.room = RESERVED
        clock.advance()
        answering: list[str] = []
        back, _ = _rooms(capacity, unavailable=answering)

    assert all(now is None for now, _ in silent)
    # Listed at every such check, so the Status page shows it and keeps the problems it found before.
    assert unavailable == [["message server's monitoring endpoint"]] * 3 and answering == []
    for _, warning in silent:
        assert warning is not None and warning.finding_id.startswith("storage_message_room:")
        assert warning.summary.endswith(
            "This page cannot tell whether it already refuses new streams, because its monitoring endpoint did not "
            "answer; a start of the whole instance with enough room makes sure it does not."
        )
        assert "Room as it runs (taken when it last started): unknown (its monitoring endpoint did not answer)" in (
            warning.details
        )
    assert kept
    assert caplog.text.count("event=storage_running_room_unknown") == 1  # when it stops answering, not every check
    assert caplog.text.count("event=storage_running_room_known") == 1
    # The same id; the Status page test below shows that this sends no second notice.
    assert back is not None and back.finding_id == entered.finding_id


def test_a_disk_too_small_while_the_server_refuses_asks_for_more_space_first(tmp_path: Path) -> None:
    store_disk = _StoreDisk(free=GB, capacity=3_500_000_000)  # even an empty disk is short

    now, _ = _rooms(_bundled(tmp_path, store_disk, _Clock(), running=_RunningRoom(RESERVED)))

    assert now is not None and now.action == (
        f"Give the disk more space, then {WHOLE_RESTART[:-1]}. This disk is too small to keep a margin: the room warning "
        "on this page ends only once the free space and the stored messages come to 4.2 GB, more than the 3.5 GB it "
        f"holds. {NOW_ENDS}"
    )


def test_the_problem_shows_when_the_store_disk_cannot_be_measured(tmp_path: Path) -> None:
    def unreadable(folder: Path) -> UnmeasuredDisk:
        return UnmeasuredDisk(key=MESSAGE_STORE_DISK_KEY, label="The disk holding the message server's store",
                              location=str(folder), error="OSError: input/output error")

    found = _check(_bundled(tmp_path, unreadable, _Clock(), running=_RunningRoom(RESERVED)))

    assert [finding.finding_id for finding in found] == [
        NO_MESSAGE_LIMIT_FINDING_ID, "storage_unmeasured:disk:message_store", "storage_message_room_now:20261027T120000Z",
    ]
    now = found[-1]
    assert now.action == (
        "Make sure the free space on the disk holding its store and its stored messages together come to more than "
        f"4 GB, then {WHOLE_RESTART} {NOW_ENDS}"
    )
    assert now.details == (
        "Room as it runs (taken when it last started): 3 GB; its streams store 12 MB\n"
        "Set aside by its streams: 3 GB\n"
        "Free space and stored messages needed when it starts: more than 4,000,000,000 bytes"
    )


def test_the_no_limit_note_on_the_bundled_server_points_at_the_room_warning(tmp_path: Path) -> None:
    found = _check(_bundled(tmp_path, _counting(9 * GB), _Clock()))

    [note] = found
    assert note.finding_id == NO_MESSAGE_LIMIT_FINDING_ID
    assert note.action == (
        "This page warns when the free space and the stored messages on the disk holding the bundled server's store "
        "would give it less room than its streams set aside; until then there is nothing to do."
    )
    assert note.details == "Stored messages (JetStream): 12 MB\nSet aside by its streams: 3 GB"



# -- the Status page ----------------------------------------------------------------------------


class _Notices:
    def __init__(self) -> None:
        self.stored: dict[str, object] = {}

    def __call__(self, notice) -> bool:
        if notice.notice_id in self.stored:
            return False
        self.stored[notice.notice_id] = notice
        return True


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="storage", instance_root=str(tmp_path / "instance"), team_id="team_storage", timezone="UTC"
    )
    config.llm.provider, config.llm.model_id = "openai", "gpt-6-luna"
    return config


def _page(
    config, capacity: StorageCapacity, notices: _Notices, clock: _Clock, *, messages=None, person_totals=lambda: None
) -> StatusPage:
    async def keys() -> dict:
        return {"endpoints": [{"address": "https://api.openai.com", "state": "stored"}]}

    async def jetstream():
        if isinstance(messages, Exception):
            raise messages
        return messages

    return StatusPage(
        config=config, model_key_status=keys, send_notice_once=notices,
        now=clock, storage=capacity, jetstream_usage=jetstream, person_storage_totals=person_totals,
    )


@pytest.mark.asyncio
async def test_administrators_hear_once_per_level_entered_not_on_every_check(tmp_path: Path) -> None:
    config, disk, clock, notices = _config(tmp_path), _Disk(70.0), _Clock(), _Notices()
    levels_dir = tmp_path / "levels"

    def page() -> StatusPage:  # a new page is what a gateway restart builds
        return _page(config, _capacity(levels_dir, disk, clock), notices, clock)

    current = page()
    await current.notify_new_problems()
    assert notices.stored == {}
    for percent in (81.0, 82.5, 86.0, 89.9):  # every five minutes, the percentage moving
        disk.percent = percent
        clock.advance()
        await current.notify_new_problems()
    [warning] = notices.stored.values()
    clock.advance()
    await page().notify_new_problems()  # after a restart
    assert len(notices.stored) == 1

    disk.percent = 96.0
    clock.advance()
    await current.notify_new_problems()
    disk.percent = 90.0  # more than 3 points below 95: back to the nearing level
    clock.advance()
    await current.notify_new_problems()
    assert len(notices.stored) == 2  # nearly full announced; back to the nearing level already announced

    disk.percent = 60.0  # space was freed
    clock.advance()
    await current.notify_new_problems()
    disk.percent = 81.0  # and the disk fills up again later
    clock.advance(minutes=60 * 24 * 30)
    await current.notify_new_problems()

    titles = [notice.title for notice in notices.stored.values()]
    assert titles == [
        "1 warning on the Status page",
        "1 problem on the Status page",
        "1 warning on the Status page",
    ]
    assert warning.kind is AccessNoticeKind.STATUS_PROBLEMS and warning.recipient_roles == ("admin",)
    assert "The disk holding the instance folder is 81% full" in warning.summary
    assert "/webchat?view=status" in warning.summary


ACCOUNT = _usage(storage=STORED, reserved_storage=RESERVED)


@pytest.mark.asyncio
async def test_administrators_hear_once_about_the_room_warning_also_after_a_restart(tmp_path: Path) -> None:
    config, clock, notices = _config(tmp_path), _Clock(), _Notices()
    store_disk = _counting(3_500_000_000)

    def page() -> StatusPage:  # a new page and check are what a gateway restart builds
        return _page(config, _bundled(tmp_path / "levels", store_disk, clock), notices, clock, messages=ACCOUNT)

    current = page()
    await current.notify_new_problems()
    [warning] = notices.stored.values()
    store_disk.free = 4_200_000_000 - STORED  # enough room again by only a little: the same warning, reworded
    clock.advance()
    await current.notify_new_problems()
    clock.advance()
    await page().notify_new_problems()  # after a restart
    assert len(notices.stored) == 1

    store_disk.free = 8 * GB - STORED  # ended
    clock.advance()
    await current.notify_new_problems()
    store_disk.free = 3_900_000_000 - STORED  # and short again later
    clock.advance(minutes=60 * 24)
    await current.notify_new_problems()

    assert [notice.title for notice in notices.stored.values()] == ["1 warning on the Status page"] * 2
    assert "- The message server will have less room than its streams set aside" in warning.summary


@pytest.mark.asyncio
async def test_a_monitoring_endpoint_that_misses_a_check_is_listed_and_its_problem_is_not_announced_again(
    tmp_path: Path,
) -> None:
    config, clock, notices = _config(tmp_path), _Clock(), _Notices()
    disk, store_disk, running = _Disk(96.0), _counting(3_500_000_000), _RunningRoom(RESERVED)
    capacity = _bundled(tmp_path / "levels", store_disk, clock, running=running, disk=disk)
    page = _page(config, capacity, notices, clock, messages=ACCOUNT)

    await page.notify_new_problems()  # the instance's disk nearly full, the server refusing, the room warning
    [first] = notices.stored.values()
    disk.percent, store_disk.free = 50.0, 8 * GB - STORED  # space freed; the whole instance not restarted yet
    clock.advance()
    await page.notify_new_problems()
    running.room = TimeoutError("timed out")  # one check without an answer
    clock.advance()
    missed = await page.report()
    await page.notify_new_problems()
    running.room = RESERVED
    clock.advance()
    back = await page.report()
    await page.notify_new_problems()

    assert first.title == "2 problems and 1 warning on the Status page"
    assert missed.unavailable_sources == ("message server's monitoring endpoint",)
    assert not any(finding.finding_id.startswith("storage_message_room_now:") for finding in missed.findings)
    assert [finding.title for finding in back.findings if finding.severity is StatusSeverity.PROBLEM] == [
        "The message server refuses new streams now"
    ]
    assert len(notices.stored) == 1  # the problem that came back was announced in the first notice


@pytest.mark.asyncio
async def test_a_message_server_that_does_not_answer_is_listed_and_the_disks_still_show(tmp_path: Path) -> None:
    clock = _Clock()
    page = _page(_config(tmp_path), _capacity(tmp_path, _Disk(97.0), clock), _Notices(), clock,
                 messages=RuntimeError("timeout"))

    report = await page.report()

    assert report.unavailable_sources == ("message server",)
    assert [finding.severity for finding in report.findings] == [StatusSeverity.PROBLEM]


@pytest.mark.asyncio
async def test_a_failing_storage_check_is_listed_and_the_rest_of_the_page_still_shows(tmp_path: Path, caplog) -> None:
    def broken() -> list[SpaceUse]:
        raise OSError("stale file handle")

    clock = _Clock()
    capacity = StorageCapacity(
        disks=broken, databases=list, levels=StorageLevels(tmp_path / "levels.json"),
        nearing_limit_percent=80, nearly_full_percent=95, message_storage_kind="file", now=clock,
    )

    with caplog.at_level("ERROR", logger="assistant_gateway.status_page"):
        report = await _page(_config(tmp_path), capacity, _Notices(), clock).report()

    assert report.unavailable_sources == ("storage checks",)
    assert "event=status_storage_check_failed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO])
async def test_an_unmeasured_disk_is_a_problem_and_keeps_other_disk_and_message_findings(
    tmp_path: Path, monkeypatch, failure: int
) -> None:
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    original = os.statvfs

    def read_space(path):
        if Path(path) == unreadable:
            raise OSError(failure, "measurement failed")
        return original(path)

    monkeypatch.setattr(os, "statvfs", read_space)
    capacity = StorageCapacity(
        disks=lambda: disk_spaces([
            ("disk:unreadable", "The unreadable disk", unreadable),
            ("disk:instance", "The instance disk", tmp_path),
        ]),
        databases=list, levels=StorageLevels(tmp_path / "levels.json"), nearing_limit_percent=1,
        nearly_full_percent=2, message_storage_kind="file", now=_Clock(),
    )
    report = await _page(
        _config(tmp_path), capacity, _Notices(), _Clock(),
        messages=_usage(storage=9 * GB, max_storage=10 * GB),
    ).report()
    assert any(finding.finding_id == "storage_unmeasured:disk:unreadable" and finding.severity is StatusSeverity.PROBLEM
               for finding in report.findings)
    assert any(finding.finding_id.startswith("storage_nearly_full:disk:instance:") for finding in report.findings)
    assert any("message_storage:file:" in finding.finding_id for finding in report.findings)


@pytest.mark.asyncio
async def test_out_of_date_stored_data_totals_are_a_warning_and_an_unreadable_record_is_listed(tmp_path: Path) -> None:
    from custom_messages.contracts.status_v1 import StatusFindingV1

    clock = _Clock()
    stale = StatusFindingV1(
        finding_id="person_storage_totals_stale", severity=StatusSeverity.WARNING, title="Stored-data totals are out of date"
    )

    def unreadable():
        raise OSError("database is locked")

    shown = await _page(_config(tmp_path), _capacity(tmp_path, _Disk(10.0), clock), _Notices(), clock,
                        person_totals=lambda: stale).report()
    failed = await _page(_config(tmp_path), _capacity(tmp_path, _Disk(10.0), clock), _Notices(), clock,
                         person_totals=unreadable).report()

    assert [finding.finding_id for finding in shown.findings] == ["person_storage_totals_stale"]
    assert failed.findings == () and failed.unavailable_sources == ("stored-data totals",)


def test_the_gateway_measures_its_instance_folder_and_databases(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.layout.dbs_dir.mkdir(parents=True)

    capacity = build_storage_capacity(config, env={})
    capacity.nearing_limit_percent, capacity.nearly_full_percent = 1, 2  # any used disk is "nearly full" here

    findings = capacity.findings(None, unavailable=[])
    keys = [finding.finding_id.split(":")[1:3] for finding in findings]

    assert keys and keys[0] == ["disk", "instance"]
    assert config.layout.storage_levels_path.is_file()


def test_the_gateway_checks_the_room_only_where_the_launcher_named_the_bundled_servers_store(
    tmp_path: Path, caplog
) -> None:
    from assistant_gateway.storage_usage import readable_bytes
    from docker.nats_service import BUNDLED_NATS_STORE_DIR_ENV, local_nats_url

    config = _config(tmp_path)
    store = tmp_path / "nats" / "data"
    bundled_env = {BUNDLED_NATS_STORE_DIR_ENV: str(store), "NATS_URL": local_nats_url(),
                   "NATS_SERVERS": local_nats_url()}

    with caplog.at_level("INFO", logger="assistant_gateway.storage_capacity"):
        bundled = build_storage_capacity(config, env=bundled_env)
        other = build_storage_capacity(config, env={})
        # Beside another server's address the folder is ignored with a warning, and no contradicting
        # "not told" line follows it.
        ignored = build_storage_capacity(config, env={**bundled_env, "NATS_URL": "nats://other.invalid:4222"})
    queues = config.transport.queues
    queues.max_bytes.partition += 1_000_000
    raised = build_storage_capacity(config, env=bundled_env)

    assert bundled.message_store is not None and bundled.message_store.folder == store
    # What one team's streams set aside at this instance's settings: about 2.3 GB at the defaults.
    assert readable_bytes(bundled.message_store.team_set_aside) == "2.3 GB"
    assert raised.message_store is not None
    assert raised.message_store.team_set_aside == bundled.message_store.team_set_aside + queues.partitions * 1_000_000
    assert other.message_store is None and ignored.message_store is None
    assert caplog.text.count("event=storage_room_not_checked") == 1  # once at start, not at every check
    assert caplog.text.count("event=bundled_nats_marker_ignored") == 1
    # A team id the message contract does not accept: the room is not checked, and the gateway still starts.
    config.manager.team_id = "not one token"
    with caplog.at_level("WARNING", logger="assistant_gateway.storage_capacity"):
        caplog.clear()
        unlisted = build_storage_capacity(config, env=bundled_env)
    assert unlisted.message_store is None
    assert "event=storage_room_not_checked team=not one token" in caplog.text
    # The bundled server's note points at the room check.
    bundled._read_running_room = _RunningRoom()  # no test reaches a real monitoring port
    [note] = [finding for finding in bundled.findings(ACCOUNT, unavailable=[])
              if finding.finding_id == NO_MESSAGE_LIMIT_FINDING_ID]
    assert note.action.startswith("This page warns when the free space and the stored messages on the disk holding the")


# -- a real NATS server -------------------------------------------------------------------------


def _nats_server() -> str:
    configured = os.environ.get("LIVE_TEST_NATS_SERVER")
    if configured:
        if not (Path(configured).is_file() and os.access(configured, os.X_OK)):
            pytest.fail(f"LIVE_TEST_NATS_SERVER={configured} is not an executable file")
        return configured
    found = shutil.which("nats-server")
    if found is None:
        pytest.skip("JetStream account limits need nats-server on PATH or LIVE_TEST_NATS_SERVER")
    return found


@contextmanager
def _own_server(tmp_path: Path, account_jetstream: str) -> Iterator[str]:
    """A server whose TEAM account has ``account_jetstream`` (own_nats_server sets address, port and store)."""

    config = tmp_path / "nats.conf"
    config.write_text(
        f'accounts {{ TEAM {{ jetstream: {account_jetstream}, users:  [ {{ user: "team", password: "team-pass" }} ] }} }}\n'
    )
    with own_nats_server(_nats_server(), tmp_path, "-c", str(config)) as url:
        yield url


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.parametrize(
    ("account_jetstream", "expected_limit"),
    [("enabled", None), ("{ max_file: 1GB, max_mem: -1 }", 1024 * 1024 * 1024)],
    ids=["no-limit", "account-limit"],
)
def test_the_account_reports_its_stored_messages_what_its_streams_set_aside_and_its_limit_when_it_has_one(
    tmp_path: Path, account_jetstream: str, expected_limit: int | None
) -> None:
    async def read(url: str) -> tuple[JetStreamAccountUsage | None, JetStreamAccountUsage | None]:
        adapter = NatsTransportAdapter(
            config=NatsTransportConfig(servers=[url], user="team", password="team-pass", storage="file"),
            mapper=NatsSubjectMapper(team_id="team_storage"),
        )
        await adapter.connect(reconcile_topology=True)
        try:
            await adapter._js.publish(adapter.mapper.agent_work_subject("lead"), b"x" * 4000)
            before = await adapter.jetstream_account_usage()
            await adapter._js.add_stream(name="ROOM_PROBE", subjects=["room.probe"], max_bytes=5 << 20)
            return before, await adapter.jetstream_account_usage()
        finally:
            await adapter.close()

    with _own_server(tmp_path, account_jetstream) as url:
        before, usage = asyncio.run(read(url))

    assert usage is not None and usage.storage >= 4000
    assert usage.max_storage == expected_limit
    # NATS sets the new stream's whole byte limit aside before any message; nats-py's own
    # account information leaves this figure out, the shared raw reader does not.
    assert before is not None and before.reserved_storage is not None
    assert usage.reserved_storage == before.reserved_storage + (5 << 20)
    assert usage.reserved_memory == 0
    finding = (
        StorageCapacity(
            disks=list, databases=list, levels=StorageLevels(tmp_path / "levels.json"), nearing_limit_percent=80,
            nearly_full_percent=95, message_storage_kind="file",
        ).findings(usage, unavailable=[])
    )
    if expected_limit is None:
        assert [item.finding_id for item in finding] == [NO_MESSAGE_LIMIT_FINDING_ID]
    else:
        assert finding == []  # a few kilobytes of 1 GB
    # The room check on the real figures, with a stand-in for the store's disk whose free space and the
    # real stored bytes come to exactly what the server needs: warned without an account limit, and
    # nothing new with one.
    need = -(-usage.reserved_storage * 4 // 3)
    store = _StoreDisk(free=need - usage.storage, capacity=need * 10)
    room = _bundled(tmp_path / "room", store, _Clock()).findings(usage, unavailable=[])
    rooms = [item for item in room if item.finding_id.startswith("storage_message_room:")]
    assert len(rooms) == (1 if expected_limit is None else 0)


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.asyncio
async def test_a_real_server_running_with_no_more_room_than_its_streams_set_aside_is_a_problem_on_the_status_page(
    tmp_path: Path,
) -> None:
    # The server's room is fixed here (max_file_store) instead of taken from the disk, so the streams can set
    # aside all of it, as on a bundled server that started while its disk was nearly full.
    room = 256 << 20
    server_config = tmp_path / "nats.conf"
    server_config.write_text(
        f"jetstream {{ max_file_store: {room} }}\n"
        'accounts { TEAM { jetstream: enabled, users: [ { user: "team", password: "team-pass" } ] } }\n'
    )
    clock, notices = _Clock(), _Notices()
    with own_nats_server(_nats_server(), tmp_path, "-c", str(server_config), "-m", "-1") as url:
        monitoring = re.search(r"Starting http monitor on 127\.0\.0\.1:(\d+)", (tmp_path / "nats.log").read_text())
        assert monitoring is not None
        adapter = NatsTransportAdapter(
            config=NatsTransportConfig(servers=[url], user="team", password="team-pass", storage="file"),
            mapper=NatsSubjectMapper(team_id="team_storage"),
        )
        await adapter.connect(reconcile_topology=True)
        try:
            # The team's own streams set aside part of the room; one more stream sets aside the rest.
            before = await adapter.jetstream_account_usage()
            assert before is not None and before.reserved_storage is not None and before.reserved_storage < room
            await adapter._js.add_stream(name="ROOM_REST", subjects=["room.rest"], max_bytes=room - before.reserved_storage)
            # What the problem says: the server refuses every new stream now (10047).
            with pytest.raises(APIError) as refused:
                await adapter._js.add_stream(name="ROOM_MORE", subjects=["room.more"])
            capacity = StorageCapacity(
                disks=list, databases=list, levels=StorageLevels(tmp_path / "levels.json"), nearing_limit_percent=80,
                nearly_full_percent=95, message_storage_kind="file",
                message_store=BundledMessageStore(folder=tmp_path / "store", team_set_aside=TEAM_SET_ASIDE),
                read_running_room=lambda: bundled_running_room(int(monitoring.group(1))), now=clock,
            )

            async def keys() -> dict:
                return {"endpoints": [{"address": "https://api.openai.com", "state": "stored"}]}

            page = StatusPage(config=_config(tmp_path), model_key_status=keys, send_notice_once=notices, now=clock,
                              storage=capacity, jetstream_usage=adapter.jetstream_account_usage)
            report = await page.report()
            await page.notify_new_problems()
            clock.advance()
            await page.notify_new_problems()
        finally:
            await adapter.close()

    assert refused.value.err_code == 10047
    assert report.unavailable_sources == ()
    [problem] = [finding for finding in report.findings if finding.severity is StatusSeverity.PROBLEM]
    assert problem.title == "The message server refuses new streams now"
    assert problem.details.startswith("Room as it runs (taken when it last started): 268.4 MB; its streams store ")
    assert "\nSet aside by its streams: 268.4 MB\n" in problem.details
    assert [notice.title for notice in notices.stored.values()] == ["1 problem on the Status page"]


def _monitor_port(folder: Path) -> int:
    """The monitoring port the test's own server picked (``-m -1``), from its log."""

    monitoring = re.search(r"Starting http monitor on 127\.0\.0\.1:(\d+)", (folder / "nats.log").read_text())
    assert monitoring is not None
    return int(monitoring.group(1))


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
def test_a_restarted_server_counts_its_stored_messages_back_into_its_room_as_the_warning_predicts(
    tmp_path: Path,
) -> None:
    # Set up as the bundled server is: no max_file_store, and one account using JetStream (the server's own).
    payload, count = os.urandom(512 * 1024), 100  # about 52 MB stored

    async def fill(url: str) -> None:
        nc = await nats.connect(url)
        try:
            js = nc.jetstream()
            await js.add_stream(name="ROOM_FILL", subjects=["room.fill"], max_bytes=4 * len(payload) * count)
            for _ in range(count):
                await js.publish("room.fill", payload)
        finally:
            await nc.close()

    async def account(url: str) -> dict:
        nc = await nats.connect(url)
        try:
            return await request_account_info(nc)
        finally:
            await nc.close()

    with own_nats_server(_nats_server(), tmp_path, "-m", "-1") as url:
        asyncio.run(fill(url))
    with own_nats_server(_nats_server(), tmp_path, "-m", "-1") as url:  # the same store, started again
        running = bundled_running_room(_monitor_port(tmp_path))
        disk = store_disk(tmp_path / "store")
        info = asyncio.run(account(url))

    assert isinstance(disk, StoreDisk)
    stored, reserved = info["storage"], info["reserved_storage"]
    assert stored >= len(payload) * count
    assert running.stored == stored  # one account uses JetStream, so its figure is the server-wide one
    predicted = MessageRoom(disk=disk, reserved=reserved, stored=stored).room
    added_back = int(stored * BUNDLED_STORAGE_SHARE)  # about 39 MB
    # Within a quarter of what is added back: other programs may write to this disk meanwhile.
    assert abs(running.room - predicted) < added_back / 4
    # Free space alone, without the stored bytes, misses by about that much.
    assert running.room - int(disk.free * BUNDLED_STORAGE_SHARE) > added_back * 3 / 4


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.asyncio
async def test_a_real_server_whose_streams_store_more_than_its_room_refuses_messages_and_the_problem_says_so_first(
    tmp_path: Path,
) -> None:
    # A fixed room (max_file_store) stands in for a bundled server that started short: one stream without a byte
    # limit, which sets nothing aside, takes messages until they pass the room, and one sets the whole room aside.
    room = 8 << 20
    server_config = tmp_path / "nats.conf"
    server_config.write_text(
        f"jetstream {{ max_file_store: {room} }}\n"
        'accounts { TEAM { jetstream: enabled, users: [ { user: "team", password: "team-pass" } ] } }\n'
    )
    clock = _Clock()
    with own_nats_server(_nats_server(), tmp_path, "-c", str(server_config), "-m", "-1") as url:
        port = _monitor_port(tmp_path)
        # The adapter's account reader on a plain connection: its own streams would not fit this room.
        adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id="team_storage"))
        nc = await nats.connect(url, user="team", password="team-pass")
        adapter._nc, adapter._js = nc, nc.jetstream()
        try:
            await adapter._js.add_stream(name="ROOM_FILL", subjects=["room.fill"])
            await adapter._js.add_stream(name="ROOM_REST", subjects=["room.rest"], max_bytes=room)
            refused: APIError | None = None
            for _ in range(64):  # 16 MB at most, twice the room
                try:
                    await adapter._js.publish("room.fill", b"x" * (256 << 10))
                except APIError as exc:
                    refused = exc
                    break
            capacity = StorageCapacity(
                disks=list, databases=list, levels=StorageLevels(tmp_path / "levels.json"), nearing_limit_percent=80,
                nearly_full_percent=95, message_storage_kind="file",
                message_store=BundledMessageStore(folder=tmp_path / "store", team_set_aside=TEAM_SET_ASIDE),
                read_running_room=lambda: bundled_running_room(port), now=clock,
            )

            async def keys() -> dict:
                return {"endpoints": [{"address": "https://api.openai.com", "state": "stored"}]}

            report = await StatusPage(config=_config(tmp_path), model_key_status=keys, send_notice_once=_Notices(),
                                      now=clock, storage=capacity,
                                      jetstream_usage=adapter.jetstream_account_usage).report()
        finally:
            await nc.close()

    # NATS refuses every new message once its streams store more than its room (10023).
    assert refused is not None and refused.err_code == 10023
    assert report.unavailable_sources == ()
    [problem] = [finding for finding in report.findings if finding.severity is StatusSeverity.PROBLEM]
    assert problem.title == "The message server refuses new messages now"
    assert problem.summary.startswith(
        "The bundled message server (NATS) refuses every new message now, so requests, replies and agents' work have "
        "stopped: its streams store 8."
    )
    assert "more than the 8.4 MB it took as its room when it last started" in problem.summary
