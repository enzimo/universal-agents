"""The team's queue streams filling up, on administrators' Status page (assistant_gateway.queue_capacity).

Covered: a work or dead-letter stream at ``transport.queues.warn_at_percent`` of its own limit
is a problem that names the team, the stream's kind in words and the setting to raise; streams
of other teams, streams the contract does not name and streams without a limit are not judged;
the finding keeps its id while the stream stays at the level, also after a restart, and the
warning ends only below the storage warnings' release margin; the queue and storage levels
share one record; administrators get one notice when a stream fills, none while it stays full
or when it drains, and one again when it fills anew; a notice only for a problem the previous
check did not find (C05 of the step 2a plan review), also after a restart and across a check
whose stream list failed; a failed stream list, or one that names a team stream as missing (a
cluster whose servers holding it are down, C27), marks the message server unavailable while the
measured streams are still judged; the stream list read page by page with the team's subject
filter, the next page after the distinct names of the last; and, with a real NATS server
(``LIVE_TEST_NATS_SERVER`` or ``nats-server`` on PATH), the authority's streams listed for the
gateway's team only, with the full one found.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nats
import pytest
from nats.js.errors import ServiceUnavailableError

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime, NatsGatewayManagerRuntimeSettings
from assistant_gateway.queue_capacity import QueueCapacity, build_queue_capacity
from assistant_gateway.status_page import StatusPage
from assistant_gateway.storage_capacity import SpaceUse, StorageLevels, build_storage_capacity
from custom_messages.wire.frame import SUBJECT_ROOT
from custom_messages.wire.queues import team_queue_streams
from custom_transport.nats_adapter import (
    STREAM_LIST_SUBJECT,
    JetStreamStreamList,
    JetStreamStreamUsage,
    NatsTransportAdapter,
    NatsTransportConfig,
)
from custom_transport.protocol1.contract_files import transport
from custom_transport.subjects import NatsSubjectMapper
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import queue_limits
from tests.test_live_message_contract import nats_server
from tests.test_live_queue_streams import server_config, topology

START = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
TEAM = "team_queues"
OTHER = "team_other"
MIB = 1 << 20
PARTITIONS = 2
PART_1 = f"UA1_PART_{TEAM}~1"
PART_0 = f"UA1_PART_{TEAM}~0"
POOL = f"UA1_POOL_WORK_{TEAM}"
DEAD_LETTERS = f"UA1_DLQ_{TEAM}"


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int = 5) -> None:
        self.now += timedelta(minutes=minutes)


class _Streams:
    """The stream list as the message server reports it: how full each stream is, or an error."""

    def __init__(self, **percent: float) -> None:
        self.percent: dict[str, float] = {}
        self.error: Exception | None = None
        # Streams the server names as missing (their servers do not answer): listed by name only.
        self.missing: tuple[str, ...] = ()
        self.set(**percent)

    def set(self, **percent: float) -> None:
        self.percent = {_NAMES[key]: value for key, value in percent.items()}

    def usages(self) -> list[JetStreamStreamUsage]:
        return [usage(name, value) for name, value in self.percent.items()]

    async def __call__(self) -> JetStreamStreamList:
        if self.error is not None:
            raise self.error
        return JetStreamStreamList(streams=[item for item in self.usages() if item.name not in self.missing],
                                   missing=self.missing)


_NAMES = {"part_0": PART_0, "part_1": PART_1, "pool": POOL, "dead_letters": DEAD_LETTERS}


def usage(name: str, percent: float, limit: int | None = MIB) -> JetStreamStreamUsage:
    return JetStreamStreamUsage(name=name, max_bytes=limit, stored_bytes=int(percent * MIB / 100), messages=7)


class _Notices:
    def __init__(self) -> None:
        self.stored: dict[str, Any] = {}
        self.fail = 0

    def __call__(self, notice) -> bool:
        if self.fail:
            self.fail -= 1
            raise OSError("the access store is locked")
        if notice.notice_id in self.stored:
            return False
        self.stored[notice.notice_id] = notice
        return True


def _capacity(levels: Path, clock: _Clock, *, warn: int = 80, team: str = TEAM) -> QueueCapacity:
    return QueueCapacity(team=team, transport=transport(), partitions=PARTITIONS, levels=StorageLevels(levels),
                         warn_at_percent=warn, now=clock)


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="queues", instance_root=str(tmp_path / "instance"), team_id=TEAM, timezone="UTC"
    )
    config.llm.provider, config.llm.model_id = "openai", "gpt-6-luna"
    return config


def _page(config: GatewayInstanceConfig, streams: _Streams, notices: _Notices, clock: _Clock) -> StatusPage:
    """A gateway's Status page; building a new one is what a restart does."""

    async def keys() -> dict:
        return {"endpoints": [{"address": "https://api.openai.com", "state": "stored"}]}

    return StatusPage(config=config, model_key_status=keys, send_notice_once=notices, now=clock,
                      queues=_capacity(config.layout.storage_levels_path, clock), team_streams=streams)


def _ids(findings) -> list[str]:
    return [finding.finding_id for finding in findings]


# -- findings -------------------------------------------------------------------------------


def test_a_stream_at_the_level_is_a_problem_naming_the_team_its_kind_and_the_setting_to_raise(tmp_path) -> None:
    capacity = _capacity(tmp_path / "levels.json", _Clock())
    listed = [
        usage(PART_1, 85.0), usage(POOL, 50.0), usage(DEAD_LETTERS, 90.0),
        usage(f"UA1_POOL_WORK_{OTHER}", 99.0),  # another team's
        usage("KV_UA1_PLACEMENT", 99.0),  # not a queue stream of the contract
        usage(PART_0, 99.0, limit=None),  # no byte limit: nothing to measure against
    ]

    dead_letters, partition = sorted(capacity.findings(listed), key=lambda item: item.finding_id)

    assert _ids([dead_letters, partition]) == [f"queue_filling:{DEAD_LETTERS}:20261118T120000Z",
                                               f"queue_filling:{PART_1}:20261118T120000Z"]
    assert partition.severity.value == "problem" and partition.since == START
    assert partition.title == (f"Team {TEAM}'s message stream for the inboxes and control queues of agents in "
                               "partition 1 is 85% full")
    assert f"Stream {PART_1} holds 891.3 kB of its 1 MB limit" in partition.summary
    assert "senders keep each one and send it again until it expires" in partition.summary
    assert "hosts are running and taking their work" in partition.action
    assert "raise transport.queues.max_bytes.partition in instance_config.yaml and restart" in partition.action
    assert "Warning level: 80% (transport.queues.warn_at_percent); the warning ends below 77%" in partition.details
    assert dead_letters.title == f"Team {TEAM}'s message stream for set-aside messages (dead letters) is 90% full"
    assert "receivers keep each message they would set aside" in dead_letters.summary
    assert "transport.queues.max_bytes.dead_letter" in dead_letters.action


def test_the_finding_keeps_its_id_while_the_stream_stays_at_the_level_and_ends_only_below_the_margin(tmp_path) -> None:
    clock = _Clock()
    capacity = _capacity(tmp_path / "levels.json", clock)

    def ids(percent: float) -> list[str]:
        clock.advance()
        return _ids(capacity.findings([usage(POOL, percent)]))

    first = ids(81.0)
    assert ids(95.0) == ids(78.0) == first  # the percentage moves; within the margin it stays
    assert _ids(_capacity(tmp_path / "levels.json", clock).findings([usage(POOL, 79.0)])) == first  # a restart
    assert ids(76.9) == []  # more than 3 points below the level: it ends
    later = ids(81.0)
    assert later and later != first  # entered anew: a new id


def test_the_queue_and_storage_levels_share_one_record(tmp_path) -> None:
    """Each StorageLevels holds the whole file, so the gateway gives the queue check the storage check's own."""

    config = _config(tmp_path)
    config.transport.queues.warn_at_percent = 60
    disk = {"used": 85}
    storage = build_storage_capacity(config)
    storage._disks = lambda: [SpaceUse(key="disk:instance", label="The disk", used=disk["used"], capacity=100)]
    storage._databases = list
    queues = build_queue_capacity(config, storage=storage)

    assert queues.team == TEAM and queues.warn_at_percent == 60
    assert len(storage.findings(None)) == 1
    assert _ids(queues.findings([usage(f"UA1_PART_{TEAM}~95", 61.0)]))  # the instance's 96 partitions
    disk["used"] = 96  # the disk's record changes after the queue's: it must keep the queue's level
    assert len(storage.findings(None)) == 1

    saved = json.loads(config.layout.storage_levels_path.read_text(encoding="utf-8"))["levels"]
    assert sorted(saved) == ["disk:instance", f"queue:UA1_PART_{TEAM}~95"]


# -- the Status page and its notices --------------------------------------------------------


@pytest.mark.asyncio
async def test_administrators_hear_once_when_a_stream_fills_not_while_it_stays_full_and_again_when_it_fills_anew(
        tmp_path) -> None:
    config, clock, notices, streams = _config(tmp_path), _Clock(), _Notices(), _Streams(pool=70.0)
    page = _page(config, streams, notices, clock)

    await page.notify_new_problems()
    assert notices.stored == {}
    for percent in (81.0, 85.0, 99.0, 79.0):  # every five minutes
        streams.set(pool=percent)
        clock.advance()
        await page.notify_new_problems()
    [notice] = notices.stored.values()
    assert notice.title == "1 problem on the Status page" and notice.recipient_roles == ("admin",)
    assert f"Team {TEAM}'s message stream for the worker pools is 81% full" in notice.summary
    clock.advance()
    await _page(config, streams, notices, clock).notify_new_problems()  # after a restart
    assert len(notices.stored) == 1

    streams.set(pool=40.0)  # it drained
    clock.advance()
    await page.notify_new_problems()
    assert len(notices.stored) == 1 and (await page.report()).findings == ()
    streams.set(pool=82.0)  # and fills again later
    clock.advance(minutes=60 * 24)
    await page.notify_new_problems()
    assert len(notices.stored) == 2


@pytest.mark.asyncio
async def test_a_notice_only_for_a_problem_the_previous_check_did_not_find(tmp_path) -> None:
    """C05: one stream draining while another stays full, or a restart, sends nothing; a new one beside an old one does."""

    config, clock, notices = _config(tmp_path), _Clock(), _Notices()
    streams = _Streams(part_0=85.0, pool=90.0)
    page = _page(config, streams, notices, clock)

    await page.notify_new_problems()
    assert [notice.title for notice in notices.stored.values()] == ["2 problems on the Status page"]
    streams.set(part_0=85.0, pool=50.0)  # one of two drains
    clock.advance()
    await page.notify_new_problems()
    clock.advance()
    await _page(config, streams, notices, clock).notify_new_problems()  # a restart with the same problem
    assert len(notices.stored) == 1

    streams.set(part_0=85.0, dead_letters=88.0)  # a new problem beside the old one
    clock.advance()
    await page.notify_new_problems()
    newest = list(notices.stored.values())[-1]
    assert len(notices.stored) == 2 and newest.title == "2 problems on the Status page"
    assert "partition 0" in newest.summary and "set-aside messages" in newest.summary
    assert json.loads(config.layout.status_problems_path.read_text(encoding="utf-8"))["finding_ids"] == sorted(
        f"queue_filling:{name}:{since}" for name, since in ((PART_0, "20261118T120000Z"),
                                                            (DEAD_LETTERS, "20261118T121500Z")))


@pytest.mark.asyncio
async def test_a_stream_list_that_fails_marks_the_message_server_unavailable_and_announces_nothing_again(
        tmp_path, caplog) -> None:
    config, clock, notices, streams = _config(tmp_path), _Clock(), _Notices(), _Streams(pool=90.0)
    page = _page(config, streams, notices, clock)
    await page.notify_new_problems()
    assert len(notices.stored) == 1

    streams.error = TimeoutError("no answer")
    clock.advance()
    report = await page.report()
    assert report.unavailable_sources == ("message server",) and report.findings == ()
    await page.notify_new_problems()
    streams.error = None  # it answers again, the stream still full
    clock.advance()
    await page.notify_new_problems()

    assert len(notices.stored) == 1
    assert f"event=status_queue_streams_unreadable team={TEAM} error=no answer" in caplog.text


@pytest.mark.asyncio
async def test_a_team_stream_the_server_could_not_measure_marks_it_unavailable_and_keeps_its_problem(
        tmp_path, caplog) -> None:
    """C27: in a cluster whose servers holding a stream are down, the list names it as missing, never as fine."""

    config, clock, notices, streams = _config(tmp_path), _Clock(), _Notices(), _Streams(pool=90.0, part_0=50.0)
    page = _page(config, streams, notices, clock)
    await page.notify_new_problems()
    [full_pool] = _ids((await page.report()).findings)

    streams.missing = (POOL, f"UA1_POOL_WORK_{OTHER}")  # another team's missing stream is not this gateway's
    streams.set(pool=90.0, part_0=85.0)
    clock.advance()
    report = await page.report()
    assert report.unavailable_sources == ("message server",)
    assert _ids(report.findings) == [f"queue_filling:{PART_0}:20261118T120500Z"]  # the measured ones are judged
    await page.notify_new_problems()  # the partition is new; the pool's problem is kept, not dropped
    streams.missing = ()  # its servers answer again, the pool stream still full
    clock.advance()
    assert sorted(_ids((await page.report()).findings)) == sorted([full_pool, f"queue_filling:{PART_0}:20261118T120500Z"])
    await page.notify_new_problems()

    assert len(notices.stored) == 2
    assert f"event=status_queue_streams_missing team={TEAM} count=1 streams={POOL}" in caplog.text


@pytest.mark.asyncio
async def test_a_notice_that_could_not_be_stored_is_sent_at_the_next_check(tmp_path) -> None:
    config, clock, notices, streams = _config(tmp_path), _Clock(), _Notices(), _Streams(pool=90.0)
    notices.fail = 1
    page = _page(config, streams, notices, clock)

    with pytest.raises(OSError):
        await page.notify_new_problems()
    assert not config.layout.status_problems_path.exists()  # nothing saved: the next check tries again
    clock.advance()
    await page.notify_new_problems()

    assert len(notices.stored) == 1


@pytest.mark.asyncio
async def test_a_failing_queue_check_is_listed_and_the_rest_of_the_page_still_shows(tmp_path, caplog) -> None:
    config, clock = _config(tmp_path), _Clock()
    page = _page(config, _Streams(pool=90.0), _Notices(), clock)

    def unsaved(listed: object) -> list:
        raise OSError("read-only file system")

    page.queues.findings = unsaved  # type: ignore[method-assign]

    report = await page.report()

    assert report.unavailable_sources == ("queue stream checks",)
    assert f"event=status_queue_check_failed team={TEAM}" in caplog.text


@pytest.mark.asyncio
async def test_a_team_id_protocol_1_refuses_fails_the_queue_check_not_the_gateway(tmp_path, caplog) -> None:
    """Over 24 characters: the launcher's id check stops such an instance; a gateway built with one still starts."""

    config, clock = _config(tmp_path), _Clock()
    long_team = "team_" + "x" * 20
    page = _page(config, _Streams(pool=90.0), _Notices(), clock)
    page.queues = _capacity(config.layout.storage_levels_path, clock, team=long_team)

    report = await page.report()

    assert report.unavailable_sources == ("queue stream checks",)
    assert f"event=status_queue_check_failed team={long_team}" in caplog.text
    assert "is not a protocol 1 team id" in caplog.text


# -- the stream list --------------------------------------------------------------------------


def _stream(name: str, max_bytes: int, stored: int) -> dict:
    return {"config": {"name": name, "max_bytes": max_bytes, "subjects": ["x"]},
            "state": {"bytes": stored, "messages": 3}}


def test_the_stream_list_is_read_page_by_page_with_the_teams_subject_filter() -> None:
    pages = [
        {"total": 3, "offset": 0, "limit": 2, "streams": [_stream(POOL, MIB, 10), _stream(PART_0, -1, 0)]},
        {"total": 3, "offset": 2, "limit": 2, "streams": [_stream(DEAD_LETTERS, 2 * MIB, 5)]},
    ]
    asked: list[tuple[str, dict]] = []

    async def request(subject: str, payload: bytes, timeout: float) -> Any:
        asked.append((subject, json.loads(payload)))
        return SimpleNamespace(data=json.dumps(pages[len(asked) - 1]).encode())

    adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id=TEAM))
    adapter._nc, adapter._js = SimpleNamespace(request=request), object()

    listed = asyncio.run(adapter.jetstream_streams(subject=f"ua.v1.{TEAM}.>"))

    assert asked == [(STREAM_LIST_SUBJECT, {"subject": f"ua.v1.{TEAM}.>", "offset": 0}),
                     (STREAM_LIST_SUBJECT, {"subject": f"ua.v1.{TEAM}.>", "offset": 2})]
    assert listed == JetStreamStreamList(
        streams=[JetStreamStreamUsage(POOL, MIB, 10, 3), JetStreamStreamUsage(PART_0, None, 0, 3),
                 JetStreamStreamUsage(DEAD_LETTERS, 2 * MIB, 5, 3)],
        missing=(),
    )


def test_streams_a_cluster_names_as_missing_are_returned_by_name_and_the_next_page_follows_them() -> None:
    """C27, from nats-server 2.15.1's clustered stream list: a stream whose servers are all offline is listed
    with its configuration and no state and is named as missing as well; one whose servers did not answer is
    only named as missing. Every stream of a page is one or both, so the next page starts after its distinct
    names."""

    no_state = {"config": {"name": PART_0, "max_bytes": MIB, "subjects": ["x"]}}
    pages = [
        {"total": 4, "offset": 0, "limit": 256, "streams": [_stream(POOL, MIB, 10), no_state],
         "missing": [PART_0, PART_1]},
        {"total": 4, "offset": 3, "limit": 256, "streams": [_stream(DEAD_LETTERS, 2 * MIB, 5)]},
    ]
    offsets: list[int] = []

    async def request(subject: str, payload: bytes, timeout: float) -> Any:
        offsets.append(json.loads(payload)["offset"])
        return SimpleNamespace(data=json.dumps(pages[len(offsets) - 1]).encode())

    adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id=TEAM))
    adapter._nc, adapter._js = SimpleNamespace(request=request), object()

    listed = asyncio.run(adapter.jetstream_streams(subject=f"ua.v1.{TEAM}.>"))

    assert offsets == [0, 3]
    assert listed == JetStreamStreamList(
        streams=[JetStreamStreamUsage(POOL, MIB, 10, 3), JetStreamStreamUsage(DEAD_LETTERS, 2 * MIB, 5, 3)],
        missing=(PART_0, PART_1),
    )
    capacity = _capacity(Path("/nonexistent/levels.json"), _Clock())
    assert capacity.unmeasured([*listed.missing, f"UA1_PART_{OTHER}~0"]) == [PART_0, PART_1]


def test_a_stream_list_error_raises_and_no_jetstream_lists_nothing() -> None:
    async def refused(subject: str, payload: bytes, timeout: float) -> Any:
        return SimpleNamespace(data=json.dumps({"error": {"code": 503, "err_code": 10039,
                                                          "description": "jetstream not enabled"}}).encode())

    adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id=TEAM))
    adapter._nc, adapter._js = SimpleNamespace(request=refused), object()
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(adapter.jetstream_streams(subject=f"ua.v1.{TEAM}.>"))
    absent = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id=TEAM))
    assert asyncio.run(absent.jetstream_streams(subject=f"ua.v1.{TEAM}.>")) is None


def test_the_gateway_asks_for_its_own_teams_protocol_1_subjects(tmp_path) -> None:
    asked: list[str] = []

    class _Adapter:
        async def jetstream_streams(self, *, subject: str) -> list:
            asked.append(subject)
            return []

    config = _config(tmp_path)
    runtime = NatsGatewayManagerRuntime(settings=NatsGatewayManagerRuntimeSettings.from_gateway_config(config),
                                        adapter=_Adapter())  # type: ignore[arg-type]
    assert asyncio.run(runtime.team_streams(TEAM)) is None  # not connected
    runtime._connected = True
    asyncio.run(runtime.team_streams(TEAM))
    assert asked == [f"{SUBJECT_ROOT}{TEAM}.>"] == [f"ua.v1.{TEAM}.>"]


# -- a real NATS server -----------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
def test_the_gateway_lists_its_teams_streams_on_a_real_server_and_finds_the_full_one(tmp_path) -> None:
    """The authority's streams for two teams; the gateway lists only its own and warns for its full pool stream."""

    filler = b"w" * (100 * 1024)

    async def scenario(url: str) -> tuple[JetStreamStreamList, list]:
        nc = await nats.connect(url, allow_reconnect=False)
        try:
            await topology(nc, queue_limits(), partitions=1).ensure([TEAM, OTHER])
            js = nc.jetstream()
            for team in (TEAM, OTHER):
                for _ in range(9):  # about 88% of the 1 MiB limit
                    await js.publish(f"ua.v1.{team}.lead-host-1.work.pool.researchers", filler)
        finally:
            await nc.close()
        adapter = NatsTransportAdapter(config=NatsTransportConfig(servers=[url], storage="file"),
                                       mapper=NatsSubjectMapper(team_id=TEAM))
        await adapter.connect(reconcile_topology=True)  # the gateway's 0.9 streams too: none is listed
        try:
            listed = await adapter.jetstream_streams(subject=f"{SUBJECT_ROOT}{TEAM}.>")
        finally:
            await adapter.close()
        assert listed is not None
        capacity = QueueCapacity(team=TEAM, transport=transport(), partitions=1,
                                 levels=StorageLevels(tmp_path / "levels.json"), warn_at_percent=80)
        return listed, capacity.findings(listed.streams)

    with own_nats_server(nats_server(), tmp_path, "-c", str(server_config(tmp_path, partitions=1))) as url:
        listed, findings = asyncio.run(scenario(url))

    assert listed.missing == ()
    assert {item.name for item in listed.streams} == {
        stream.name for stream in team_queue_streams(transport(), TEAM, 1)}
    pool = next(item for item in listed.streams if item.name == POOL)
    assert pool.max_bytes == MIB and pool.messages == 9 and 0.8 * MIB < pool.stored_bytes < MIB
    [finding] = findings
    assert finding.finding_id.startswith(f"queue_filling:{POOL}:")
    assert finding.title == f"Team {TEAM}'s message stream for the worker pools is 88% full"
