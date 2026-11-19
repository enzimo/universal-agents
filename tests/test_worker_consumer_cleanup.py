"""Started workers' NATS consumers belong to their process; the team lead removes ones left before.

Incident (2026-11-19): every worker started from a start record kept its
mailbox and control consumer on the team's work stream after it exited,
because ``spawn_agent`` sets the worker's id and an agent with an id set was
treated as a configured, lasting route. After about 500 starts the stream
held NATS 2.15's default limit of 1,000 consumers, every new worker crashed
while creating its mailbox, and the pool controller restarted it until the
hourly start limit refused it.

These tests use the real start-record and status stores with the
scheduled-worker test lead (its launcher records starts instead of running
threads); NATS is replaced by small in-memory doubles. The live check against
a NATS server is in ``tests/test_live_nats_replacement_semantics.py``.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import custom_agents.worker_consumer_cleanup as cleanup
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.strands_agent import _agent_consumer_scope, _direct_consumer_scope
from custom_agents.worker_consumer_cleanup import (
    GONE_WORKER_REASON,
    remove_gone_worker_consumers,
    sweep_gone_worker_consumers_once,
)
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_transport.base import AGENT_CONTROL_ROLE, AGENT_DIRECT_ROLE, ConsumerScope
from custom_transport.nats_consumer_reconciler import AgentConsumer, NatsConsumerReconciler
from custom_transport.nats_pull import CONSUMER_METADATA_SCHEMA
from custom_transport.subjects import NatsSubjectMapper
from tests.test_orphaned_work_takeover import launch_record
from tests.test_schedule_worker_service import lead  # noqa: F401

TEAM = "team"
MAPPER = NatsSubjectMapper(team_id=TEAM)


# -- which agents' consumers belong to their process ---------------------------


def test_a_started_worker_gets_process_consumers_although_its_id_is_set() -> None:
    assert _direct_consumer_scope(configured_fields={"id"}, started_worker=True) == ConsumerScope.PROCESS
    assert _direct_consumer_scope(configured_fields=set(), started_worker=True) == ConsumerScope.PROCESS


def test_an_id_from_configuration_keeps_a_lasting_mailbox_and_a_generated_one_does_not() -> None:
    assert _direct_consumer_scope(configured_fields={"id"}, started_worker=False) == ConsumerScope.ROUTE
    assert _direct_consumer_scope(configured_fields=set(), started_worker=False) == ConsumerScope.PROCESS


def test_the_agent_scope_reads_the_start_record_spawn_agent_attaches() -> None:
    worker = SimpleNamespace(model_fields_set={"id", "team_id"}, _worker_launch=object())
    configured = SimpleNamespace(model_fields_set={"id", "team_id"}, _worker_launch=None)
    assert _agent_consumer_scope(worker) == ConsumerScope.PROCESS
    assert _agent_consumer_scope(configured) == ConsumerScope.ROUTE


# -- listing and deleting agents' consumers ------------------------------------


class NotFoundError(Exception):
    """Named like nats-py's error for a consumer that no longer exists."""


class _JetStream:
    def __init__(self, consumers, *, missing=()) -> None:
        self.consumers = list(consumers)
        self.missing = set(missing)
        self.deleted: list[tuple[str, str]] = []

    async def consumers_info(self, stream_name, *, offset=0):
        listed = self.consumers if stream_name == MAPPER.work_stream_name() else []
        return listed[offset : offset + 256]

    async def delete_consumer(self, stream_name, consumer_name):
        if consumer_name in self.missing:
            raise NotFoundError("consumer not found")
        self.deleted.append((stream_name, consumer_name))


def _info(
    owner: str,
    role: str,
    *,
    scope: str = "route",
    team: str = TEAM,
    name: str | None = None,
    filter_subject: str | None = None,
    waiting: int = 0,
):
    control = role == AGENT_CONTROL_ROLE
    return SimpleNamespace(
        name=name or MAPPER.durable_name(f"agent_{owner}_control" if control else f"agent_{owner}"),
        created=datetime.now(timezone.utc) - timedelta(hours=2),
        config=SimpleNamespace(
            filter_subject=filter_subject
            or (MAPPER.agent_control_subject(owner) if control else MAPPER.agent_work_subject(owner)),
            deliver_subject=None,
            metadata={
                "ua.managed": "true",
                "ua.schema": CONSUMER_METADATA_SCHEMA,
                "ua.team_id": team,
                "ua.consumer_scope": scope,
                "ua.consumer_role": role,
                "ua.owner_agent_id": owner,
            },
        ),
        push_bound=None,
        num_pending=2,
        num_ack_pending=0,
        num_waiting=waiting,
    )


@pytest.mark.asyncio
async def test_only_agents_own_mailbox_and_control_consumers_of_this_team_are_listed() -> None:
    pool = _info("lead", "named_worker", name=MAPPER.durable_name("workers_Research"))
    pool.config.filter_subject = MAPPER.named_type_work_subject("Research")
    jetstream = _JetStream(
        [
            _info("worker_a", AGENT_DIRECT_ROLE),
            _info("worker_a", AGENT_CONTROL_ROLE, scope="process"),
            _info("configured", AGENT_DIRECT_ROLE),
            _info("other_team_worker", AGENT_DIRECT_ROLE, team="team_other"),
            _info("renamed", AGENT_DIRECT_ROLE, name="someone_elses_consumer"),
            _info("refiltered", AGENT_CONTROL_ROLE, filter_subject=MAPPER.agent_work_subject("refiltered")),
            pool,
        ]
    )
    listed = await NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER).agent_consumers()
    assert [(c.owner_agent_id, c.consumer_role, c.consumer_scope) for c in listed] == [
        ("worker_a", AGENT_DIRECT_ROLE, "route"),
        ("worker_a", AGENT_CONTROL_ROLE, "process"),
        ("configured", AGENT_DIRECT_ROLE, "route"),
    ]


@pytest.mark.asyncio
async def test_deleting_skips_a_consumer_already_gone_and_logs_each_deletion(caplog) -> None:
    gone_name = MAPPER.durable_name("agent_worker_b")
    jetstream = _JetStream([], missing={gone_name})
    consumers = [
        AgentConsumer(MAPPER.work_stream_name(), MAPPER.durable_name("agent_worker_a"), "worker_a", AGENT_DIRECT_ROLE, "route", 2, 0),
        AgentConsumer(MAPPER.work_stream_name(), gone_name, "worker_b", AGENT_DIRECT_ROLE, "route", 0, 0),
    ]
    with caplog.at_level(logging.INFO):
        deleted = await NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER).delete_agent_consumers(
            consumers, reason="test_reason"
        )
    assert deleted == 1
    assert jetstream.deleted == [(MAPPER.work_stream_name(), MAPPER.durable_name("agent_worker_a"))]
    assert "event=nats_agent_consumer_deleted" in caplog.text
    assert "owner_agent_id=worker_a num_pending=2" in caplog.text


@pytest.mark.asyncio
async def test_the_offline_scan_also_removes_an_abandoned_process_control_consumer() -> None:
    jetstream = _JetStream(
        [_info("agent_id_aaaaaaaaaaaa", AGENT_CONTROL_ROLE, scope="process")]
    )
    result = await NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER).reconcile(
        apply=True, minimum_age_seconds=60.0
    )
    assert [d.reason for d in result.decisions] == ["abandoned_managed_process_consumer"]
    assert jetstream.deleted == [
        (MAPPER.work_stream_name(), MAPPER.durable_name("agent_agent_id_aaaaaaaaaaaa_control"))
    ]


# -- the team lead's removal ---------------------------------------------------


class _Adapter:
    """The two adapter calls the cleanup makes, over an in-memory list of consumers."""

    def __init__(self, consumers) -> None:
        self.consumers = list(consumers)
        self.deleted: list[AgentConsumer] = []
        self.reasons: list[str] = []
        self.listings = 0

    async def agent_consumers(self):
        self.listings += 1
        return tuple(self.consumers)

    async def delete_agent_consumers(self, consumers, *, reason):
        consumers = list(consumers)
        self.deleted.extend(consumers)
        self.reasons.append(reason)
        return len(consumers)


def _consumer(owner: str, role: str = AGENT_DIRECT_ROLE) -> AgentConsumer:
    suffix = "_control" if role == AGENT_CONTROL_ROLE else ""
    return AgentConsumer(
        MAPPER.work_stream_name(), MAPPER.durable_name(f"agent_{owner}{suffix}"), owner, role, "route", 0, 0
    )


@pytest.fixture
def team(lead, monkeypatch):
    lead._agent_state_store = AgentStateStore(
        storage_settings=lead.task_manager.storage_settings, logger=logging.getLogger("test.cleanup")
    )
    lead.schedule_maintenance = {}
    lead._worker_consumers_swept = False
    lead._worker_consumer_sweep_retry_at = 0.0
    # A start whose process is gone reads "absent"; every other one is running.
    absent = {"crashed_worker"}
    lead._local_team_runtime.inspect_launch = lambda record: (
        "absent" if record.agent_id in absent else "starting"
    )
    return lead


def _attach(team, adapter) -> _Adapter:
    team._ensure_nats_transport_adapter = lambda: adapter
    return adapter


@pytest.mark.asyncio
async def test_the_lead_removes_only_gone_started_workers_consumers(team) -> None:
    launch_record(team, AgentLaunchState.RETIRED, agent_id="retired_worker")
    launch_record(team, AgentLaunchState.FAILED, agent_id="failed_worker")
    launch_record(team, AgentLaunchState.STARTING, agent_id="crashed_worker")
    launch_record(team, AgentLaunchState.READY, agent_id="busy_worker")
    adapter = _attach(
        team,
        _Adapter(
            [
                _consumer("retired_worker"),
                _consumer("retired_worker", AGENT_CONTROL_ROLE),
                _consumer("failed_worker"),
                _consumer("crashed_worker", AGENT_CONTROL_ROLE),
                # Running, and asking for nothing while it works: kept.
                _consumer("busy_worker"),
                _consumer("busy_worker", AGENT_CONTROL_ROLE),
                # No start record: an id from configuration, a lasting mailbox.
                _consumer("configured_agent"),
                _consumer("lead", AGENT_CONTROL_ROLE),
            ]
        ),
    )

    assert await remove_gone_worker_consumers(team) == 4
    assert sorted((c.owner_agent_id, c.consumer_role) for c in adapter.deleted) == [
        ("crashed_worker", AGENT_CONTROL_ROLE),
        ("failed_worker", AGENT_DIRECT_ROLE),
        ("retired_worker", AGENT_CONTROL_ROLE),
        ("retired_worker", AGENT_DIRECT_ROLE),
    ]
    assert adapter.reasons == [GONE_WORKER_REASON]


@pytest.mark.asyncio
async def test_an_empty_stream_reads_no_start_records(team, monkeypatch) -> None:
    adapter = _attach(team, _Adapter([]))
    monkeypatch.setattr(cleanup, "AgentLaunchRepository", None)  # would fail if read
    assert await remove_gone_worker_consumers(team) == 0
    assert adapter.listings == 1


@pytest.mark.asyncio
async def test_the_sweep_runs_once_per_process_and_retries_a_failure_later(team, monkeypatch, caplog) -> None:
    clock = [1000.0]
    monkeypatch.setattr(cleanup, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []

    async def failing(agent):
        calls.append("fail")
        raise RuntimeError("nats unavailable")

    monkeypatch.setattr(cleanup, "remove_gone_worker_consumers", failing)
    with caplog.at_level(logging.ERROR):
        await sweep_gone_worker_consumers_once(team)
    assert "event=worker_consumer_cleanup_failed" in caplog.text
    assert team._worker_consumers_swept is False

    clock[0] += cleanup.RETRY_SECONDS - 1
    await sweep_gone_worker_consumers_once(team)
    assert calls == ["fail"]

    async def succeeding(agent):
        calls.append("ok")
        return 0

    monkeypatch.setattr(cleanup, "remove_gone_worker_consumers", succeeding)
    clock[0] += 1
    await sweep_gone_worker_consumers_once(team)
    await sweep_gone_worker_consumers_once(team)
    assert calls == ["fail", "ok"]
    assert team._worker_consumers_swept is True
