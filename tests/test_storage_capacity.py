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
the transport's reading of the account's limits; and, with a real NATS server
(``LIVE_TEST_NATS_SERVER`` or ``nats-server`` on PATH), the account with and
without a JetStream limit.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.config import GatewayInstanceConfig, StorageWarningSettings
from assistant_gateway.status_page import StatusPage
from assistant_gateway.storage_capacity import (
    NEARING_LIMIT,
    NEARLY_FULL,
    NO_MESSAGE_LIMIT_FINDING_ID,
    SpaceUse,
    StorageCapacity,
    StorageLevels,
    StoredSize,
    build_storage_capacity,
    database_sizes,
    disk_spaces,
)
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.status_v1 import StatusSeverity
from custom_messages.enums import MessageMetaType
from custom_transport.nats_adapter import JetStreamAccountUsage, NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper
from storage.config import resolve_storage_settings
from tests.knowledge_support import BACKENDS, knowledge_storage

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

    assert capacity.findings(None) == []
    disk.percent = 83.4
    [nearing] = capacity.findings(None)
    disk.percent = 96.2
    clock.advance()
    [full] = capacity.findings(None)

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

    assert capacity.findings(None) == []
    capacity.nearing_limit_percent, capacity.nearly_full_percent = 70, 90  # changed in Settings
    [finding] = capacity.findings(None)

    assert finding.severity is StatusSeverity.WARNING and "told again at 90% full" in finding.action
    with pytest.raises(ValueError, match="below nearly_full_percent"):
        StorageWarningSettings(nearing_limit_percent=95, nearly_full_percent=95)
    with pytest.raises(ValueError, match="from 1 to 100"):
        StorageWarningSettings(nearly_full_percent=101)


def test_stored_messages_are_measured_against_the_accounts_limit_or_noted_without_one(tmp_path: Path) -> None:
    capacity = _capacity(tmp_path, _Disk(10.0), _Clock())

    [limited] = capacity.findings(JetStreamAccountUsage(storage=9 * GB, memory=0, max_storage=10 * GB, max_memory=None))
    [unlimited] = capacity.findings(JetStreamAccountUsage(storage=12_000_000, memory=0, max_storage=None, max_memory=None))

    assert limited.finding_id.startswith("storage_nearing_limit:message_storage:file:")
    assert limited.title == "The message server's storage is 90% full"
    assert "refuses new messages" in limited.summary and "Stored messages (JetStream): 9 GB" in limited.details
    assert "counts the bytes stored, not the space streams reserve" in limited.summary
    assert unlimited.finding_id == NO_MESSAGE_LIMIT_FINDING_ID
    assert unlimited.severity is StatusSeverity.NOTE
    assert "no limit for stored messages (JetStream), which take 12 MB now" in unlimited.summary
    assert "three quarters of the disk space that was free when it started" in unlimited.summary


def test_the_transport_reads_no_limit_and_zero_as_no_limit() -> None:
    def adapter_with(limits: SimpleNamespace) -> NatsTransportAdapter:
        adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id="team"))

        async def account_info():
            return SimpleNamespace(storage=2048, memory=10, limits=limits)

        adapter._js = SimpleNamespace(account_info=account_info)
        return adapter

    unlimited = asyncio.run(adapter_with(SimpleNamespace(max_storage=-1, max_memory=0)).jetstream_account_usage())
    limited = asyncio.run(adapter_with(SimpleNamespace(max_storage=4096, max_memory=-1)).jetstream_account_usage())
    absent = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id="team"))

    assert unlimited == JetStreamAccountUsage(storage=2048, memory=10, max_storage=None, max_memory=None)
    assert limited.max_storage == 4096 and limited.max_memory is None
    assert asyncio.run(absent.jetstream_account_usage()) is None  # not connected, or no JetStream


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


def _lead() -> AgentStatus:
    return AgentStatus(
        self_id="lead-1", team_id="team_storage", named_agent_type="TeamArchitect", name="Nova", is_paused=False,
        agent_role="lead", is_team_lead=True, status_type=MessageMetaType.RESPONSE, status_findings=[],
    )


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
        config=config, model_key_status=keys, agent_statuses=lambda: [_lead()], send_notice_once=notices,
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
        messages=JetStreamAccountUsage(storage=9 * GB, memory=0, max_storage=10 * GB, max_memory=None),
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

    capacity = build_storage_capacity(config)
    capacity.nearing_limit_percent, capacity.nearly_full_percent = 1, 2  # any used disk is "nearly full" here

    findings = capacity.findings(None)
    keys = [finding.finding_id.split(":")[1:3] for finding in findings]

    assert keys and keys[0] == ["disk", "instance"]
    assert config.layout.storage_levels_path.is_file()


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


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _start_server(tmp_path: Path, account_jetstream: str) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    config = tmp_path / "nats.conf"
    config.write_text(
        f'host: "127.0.0.1"\nport: {port}\n'
        f'jetstream {{ store_dir: "{tmp_path / "store"}" }}\n'
        f'accounts {{ TEAM {{ jetstream: {account_jetstream}, users:  [ {{ user: "team", password: "team-pass" }} ] }} }}\n'
    )
    log = (tmp_path / "nats.log").open("w")
    process = subprocess.Popen([_nats_server(), "-c", str(config)], stdout=log, stderr=log)
    deadline = time.monotonic() + 5
    while True:
        if process.poll() is not None:
            pytest.fail(f"NATS exited during startup: {(tmp_path / 'nats.log').read_text()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return process, f"nats://127.0.0.1:{port}"
        except OSError:
            if time.monotonic() >= deadline:
                process.kill()
                pytest.fail("NATS did not start within five seconds")
            time.sleep(0.02)


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.parametrize(
    ("account_jetstream", "expected_limit"),
    [("enabled", None), ("{ max_file: 1GB, max_mem: -1 }", 1024 * 1024 * 1024)],
    ids=["no-limit", "account-limit"],
)
def test_the_account_reports_its_stored_messages_and_its_limit_when_it_has_one(
    tmp_path: Path, account_jetstream: str, expected_limit: int | None
) -> None:
    process, url = _start_server(tmp_path, account_jetstream)

    async def read() -> JetStreamAccountUsage | None:
        adapter = NatsTransportAdapter(
            config=NatsTransportConfig(servers=[url], user="team", password="team-pass", storage="file"),
            mapper=NatsSubjectMapper(team_id="team_storage"),
        )
        await adapter.connect(reconcile_topology=True)
        try:
            await adapter._js.publish(f"{adapter.mapper.prefix}.presence.lead", b"x" * 4000)
            return await adapter.jetstream_account_usage()
        finally:
            await adapter.close()

    try:
        usage = asyncio.run(read())
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert usage is not None and usage.storage >= 4000
    assert usage.max_storage == expected_limit
    finding = (
        StorageCapacity(
            disks=list, databases=list, levels=StorageLevels(tmp_path / "levels.json"), nearing_limit_percent=80,
            nearly_full_percent=95, message_storage_kind="file",
        ).findings(usage)
    )
    if expected_limit is None:
        assert [item.finding_id for item in finding] == [NO_MESSAGE_LIMIT_FINDING_ID]
    else:
        assert finding == []  # a few kilobytes of 1 GB
