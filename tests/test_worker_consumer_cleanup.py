"""Started workers' NATS consumers belong to their process; the team lead removes ones left before.

Incident (2026-11-19): every worker started from a start record kept its
mailbox and control consumer on the team's work stream after it exited,
because ``spawn_agent`` sets the worker's id and an agent with an id set was
treated as a configured, lasting route. After about 500 starts the stream
held NATS 2.15's default limit of 1,000 consumers, every new worker crashed
while creating its mailbox, and the pool controller restarted it until the
hourly start limit refused it.

Review of 2026-11-20: the first version removed only owners that still had a
start record, so leftovers whose record the 30-day retention had pruned stayed
for good and were counted as configured agents, and push-era consumers were
never listed.

These tests use the real start-record and status stores with the
scheduled-worker test lead (its launcher records starts instead of running
threads) and the real reconciler over a small in-memory JetStream double. The
live check against a NATS server is in ``tests/test_live_nats_replacement_semantics.py``.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_agents.worker_consumer_cleanup as cleanup
from config_loader import Config
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.strands_agent import _agent_consumer_scope, _direct_consumer_scope
from custom_agents.worker_consumer_cleanup import (
    CAPACITY_CHECK_SECONDS,
    GONE_WORKER_REASON,
    RESWEEP_SECONDS,
    RETRY_SECONDS,
    SweepResult,
    remove_gone_worker_consumers,
    sweep_worker_consumers_at_start,
    sweep_worker_consumers_if_due,
)
from custom_messages.contracts.control_v1 import AgentStatus, ControlType, MessageMetaType
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_transport.base import AGENT_CONTROL_ROLE, AGENT_DIRECT_ROLE, ConsumerScope
from custom_transport.nats_consumer_reconciler import AgentConsumer, NatsConsumerReconciler
from custom_transport.nats_consumer_state import (
    LIMIT_FROM_ACCOUNT,
    LIMIT_FROM_NATS_DEFAULT,
    LIMIT_FROM_STREAM,
    consumer_has_open_request,
    log_capacity_if_high,
    server_version_of,
    stream_consumer_capacity,
    version_label,
)
from custom_transport.nats_pull import CONSUMER_METADATA_SCHEMA, PUSH_CONSUMER_METADATA_SCHEMA
from custom_transport.subjects import NatsSubjectMapper
from storage.agent_launch_repository import AgentLaunchRepository
from tests.test_orphaned_work_takeover import launch_record
from tests.test_schedule_worker_service import lead  # noqa: F401

TEAM = "team"
MAPPER = NatsSubjectMapper(team_id=TEAM)
# A drawn start id, as ``AgentInstantiationService.launch`` draws them.
PRUNED_WORKER = "agent_" + "0123456789abcdef" * 2
# A startup agent's generated id (its consumers belong to its process).
STARTUP_AGENT = "agent_id_aaaaaaaaaaaa"


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
    """A work stream's consumers; stream and account info count what is left after deletions."""

    def __init__(
        self, consumers, *, missing=(), stream_limit=-1, account_limit=-1, stream_missing=False, extra_consumers=0
    ) -> None:
        self.consumers = list(consumers)
        self.missing = set(missing)
        self.deleted: list[tuple[str, str]] = []
        self.stream_limit = stream_limit
        self.account_limit = account_limit
        self.stream_missing = stream_missing
        # Consumers of the stream that are not agents' (worker queues, other roles).
        self.extra_consumers = extra_consumers

    def _left(self) -> int:
        return len(self.consumers) - len(self.deleted) + self.extra_consumers

    async def consumers_info(self, stream_name, *, offset=0):
        listed = self.consumers if stream_name == MAPPER.work_stream_name() else []
        return listed[offset : offset + 256]

    async def delete_consumer(self, stream_name, consumer_name):
        if consumer_name in self.missing:
            raise NotFoundError("consumer not found")
        self.deleted.append((stream_name, consumer_name))

    async def stream_info(self, stream_name):
        if self.stream_missing:
            raise NotFoundError("stream not found")
        return SimpleNamespace(
            config=SimpleNamespace(max_consumers=self.stream_limit),
            state=SimpleNamespace(consumer_count=self._left()),
        )

    async def account_info(self):
        return SimpleNamespace(consumers=self._left(), limits=SimpleNamespace(max_consumers=self.account_limit))


def _name(owner: str, role: str) -> str:
    return MAPPER.durable_name(f"agent_{owner}_control" if role == AGENT_CONTROL_ROLE else f"agent_{owner}")


def _info(
    owner: str,
    role: str,
    *,
    scope: str = "route",
    team: str = TEAM,
    name: str | None = None,
    filter_subject: str | None = None,
    waiting: int = 0,
    held: int = 0,
    schema: str = CONSUMER_METADATA_SCHEMA,
    push_bound: bool | None = None,
):
    """A consumer as nats-py reports it; push-era ones have a deliver subject."""

    control = role == AGENT_CONTROL_ROLE
    push_era = schema == PUSH_CONSUMER_METADATA_SCHEMA
    return SimpleNamespace(
        name=name or _name(owner, role),
        created=datetime.now(timezone.utc) - timedelta(hours=2),
        config=SimpleNamespace(
            filter_subject=filter_subject
            or (MAPPER.agent_control_subject(owner) if control else MAPPER.agent_work_subject(owner)),
            deliver_subject=f"deliver.{owner}" if push_era else None,
            metadata={
                "ua.managed": "true",
                "ua.schema": schema,
                "ua.team_id": team,
                "ua.consumer_scope": scope,
                "ua.consumer_role": role,
                "ua.owner_agent_id": owner,
            },
        ),
        push_bound=push_bound,
        num_pending=2,
        num_ack_pending=held,
        num_waiting=waiting,
    )


def test_only_an_open_request_proves_a_receiver_is_alive() -> None:
    # A receiver that died while holding a message leaves it pending for good (NATS 2.15.3).
    assert not consumer_has_open_request(_info("dead", AGENT_DIRECT_ROLE, held=1))
    assert consumer_has_open_request(_info("asking", AGENT_DIRECT_ROLE, waiting=1))
    assert consumer_has_open_request(
        _info("bound", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA, push_bound=True)
    )
    assert not consumer_has_open_request(_info("unbound", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA))


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
            # Push-delivery releases: listed with their schema; bound means a receiver is there.
            _info("pushed", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA),
            _info("pushed", AGENT_CONTROL_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA, push_bound=True),
            _info("other_team_pushed", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA, team="team_other"),
            _info("unknown_schema", AGENT_DIRECT_ROLE, schema="consumer-lifecycle-v0"),
            _info("asking", AGENT_DIRECT_ROLE, waiting=1),
            _info("holding", AGENT_DIRECT_ROLE, held=1),
        ]
    )
    listed = await NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER).agent_consumers()
    v2, v1 = CONSUMER_METADATA_SCHEMA, PUSH_CONSUMER_METADATA_SCHEMA
    assert [(c.owner_agent_id, c.consumer_role, c.consumer_scope, c.schema, c.asking) for c in listed] == [
        ("worker_a", AGENT_DIRECT_ROLE, "route", v2, False),
        ("worker_a", AGENT_CONTROL_ROLE, "process", v2, False),
        ("configured", AGENT_DIRECT_ROLE, "route", v2, False),
        ("pushed", AGENT_DIRECT_ROLE, "route", v1, False),
        ("pushed", AGENT_CONTROL_ROLE, "route", v1, True),
        ("asking", AGENT_DIRECT_ROLE, "route", v2, True),
        ("holding", AGENT_DIRECT_ROLE, "route", v2, False),
    ]


@pytest.mark.asyncio
async def test_deleting_skips_a_consumer_already_gone_and_logs_each_deletion(caplog) -> None:
    gone_name = MAPPER.durable_name("agent_worker_b")
    jetstream = _JetStream([], missing={gone_name})
    consumers = [
        AgentConsumer(
            MAPPER.work_stream_name(), MAPPER.durable_name("agent_worker_a"), "worker_a", AGENT_DIRECT_ROLE,
            "route", 2, 0, PUSH_CONSUMER_METADATA_SCHEMA, False,
        ),
        AgentConsumer(
            MAPPER.work_stream_name(), gone_name, "worker_b", AGENT_DIRECT_ROLE, "route", 0, 0,
            CONSUMER_METADATA_SCHEMA, False,
        ),
    ]
    with caplog.at_level(logging.INFO):
        deleted = await NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER).delete_agent_consumers(
            consumers, reason="test_reason"
        )
    assert deleted == 1
    assert jetstream.deleted == [(MAPPER.work_stream_name(), MAPPER.durable_name("agent_worker_a"))]
    assert "event=nats_agent_consumer_deleted" in caplog.text
    assert f"schema={PUSH_CONSUMER_METADATA_SCHEMA} owner_agent_id=worker_a num_pending=2" in caplog.text


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


# -- the offline script: lasting leftovers with the runtime stopped -----------
# Review of 2026-11-20 (DATA-4, CRIT-1): the documented fallback skipped every
# lasting consumer and every push-era one, so it removed none of the leftovers.


def _offline(consumers, **kwargs):
    jetstream = _JetStream(consumers)
    reconciler = NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER, stable_agent_ids={"stable_agent"})
    return jetstream, reconciler


def _reasons(result) -> dict[str, str]:
    return {d.consumer_name: d.reason for d in result.decisions}


@pytest.mark.asyncio
async def test_the_offline_scan_removes_lasting_leftovers_and_keeps_what_may_be_in_use() -> None:
    young = _info("young_worker", AGENT_DIRECT_ROLE)
    young.created = datetime.now(timezone.utc) - timedelta(minutes=5)
    lead_route = _info("lead", "team_lead", name=MAPPER.durable_name(f"lead_{TEAM}"))
    jetstream, reconciler = _offline(
        [
            # Died holding a message: num_ack_pending stays 1 for good (NATS 2.15.3).
            _info("dead_worker", AGENT_DIRECT_ROLE, held=1),
            _info("dead_worker", AGENT_CONTROL_ROLE),
            _info("push_worker", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA),
            _info("asking_worker", AGENT_DIRECT_ROLE, waiting=1),
            _info("bound_worker", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA, push_bound=True),
            _info("stable_agent", AGENT_DIRECT_ROLE),
            young,
            lead_route,
            # Not an agent's own consumer of this team: never a candidate.
            _info("unknown_schema", AGENT_DIRECT_ROLE, schema="consumer-lifecycle-v0"),
            _info("renamed", AGENT_DIRECT_ROLE, name="someone_elses_consumer"),
            _info("refiltered", AGENT_DIRECT_ROLE, filter_subject=MAPPER.agent_work_subject("someone_else")),
            _info("other_team_worker", AGENT_DIRECT_ROLE, team="team_other"),
        ]
    )
    result = await reconciler.reconcile(apply=True, minimum_age_seconds=3600.0)
    lasting, unmanaged = "abandoned_lasting_agent_consumer", "unmanaged_or_persistent"
    assert _reasons(result) == {
        _name("dead_worker", AGENT_DIRECT_ROLE): lasting,
        _name("dead_worker", AGENT_CONTROL_ROLE): lasting,
        _name("push_worker", AGENT_DIRECT_ROLE): lasting,
        _name("asking_worker", AGENT_DIRECT_ROLE): "live_subscription",
        _name("bound_worker", AGENT_DIRECT_ROLE): "live_subscription",
        _name("stable_agent", AGENT_DIRECT_ROLE): "configured_stable_agent",
        _name("young_worker", AGENT_DIRECT_ROLE): "younger_than_minimum_age",
        MAPPER.durable_name(f"lead_{TEAM}"): unmanaged,
        _name("unknown_schema", AGENT_DIRECT_ROLE): unmanaged,
        "someone_elses_consumer": unmanaged,
        _name("refiltered", AGENT_DIRECT_ROLE): unmanaged,
        _name("other_team_worker", AGENT_DIRECT_ROLE): unmanaged,
    }
    assert sorted(name for _stream, name in jetstream.deleted) == sorted(
        [
            _name("dead_worker", AGENT_DIRECT_ROLE),
            _name("dead_worker", AGENT_CONTROL_ROLE),
            _name("push_worker", AGENT_DIRECT_ROLE),
        ]
    )


@pytest.mark.asyncio
async def test_the_launchers_mode_removes_only_lasting_leftovers_and_reports_the_rest() -> None:
    from custom_transport.nats_consumer_reconciler import KIND_LASTING_AGENT

    jetstream, reconciler = _offline(
        [
            _info("dead_worker", AGENT_DIRECT_ROLE),
            _info("agent_id_aaaaaaaaaaaa", AGENT_DIRECT_ROLE, scope="process"),
        ]
    )
    result = await reconciler.reconcile(
        apply=True, minimum_age_seconds=3600.0, delete_kinds=frozenset({KIND_LASTING_AGENT})
    )
    assert result.candidate_count == 2
    assert jetstream.deleted == [(MAPPER.work_stream_name(), _name("dead_worker", AGENT_DIRECT_ROLE))]
    assert result.deleted_count == 1


def test_the_script_deletes_only_with_the_runtime_confirmed_stopped(monkeypatch) -> None:
    import scripts.reconcile_nats_consumers as script
    from custom_transport.nats_consumer_reconciler import KIND_LASTING_AGENT

    calls = []

    async def recorded(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            decisions=(), applied=kwargs["apply"], scanned_count=0, candidate_count=0, deleted_count=0,
            account_consumer_count=None, account_max_consumers=None, stream_capacity=None,
        )

    monkeypatch.setattr(script, "reconcile_nats_consumers", recorded)
    monkeypatch.setattr(script, "local_nats_enabled_from_env", lambda env, default: False)
    with pytest.raises(SystemExit):
        script.main(["--config", "c.yaml", "--remove-lasting-agent-leftovers"])
    assert calls == []
    for flags, apply, kinds in (
        ([], False, None),
        (["--remove-lasting-agent-leftovers", "--confirm-runtime-stopped"], True, frozenset({KIND_LASTING_AGENT})),
        (["--apply", "--confirm-runtime-stopped"], True, None),
    ):
        assert script.main(["--config", "c.yaml", *flags]) == 0
        assert (calls[-1]["apply"], calls[-1]["delete_kinds"]) == (apply, kinds)


# -- how full the work stream is ------------------------------------------------
# Review of 2026-11-20 (GEN-1, GEN-2): the start-up check compared the account's
# count with the account's limit, which NATS 2.15 leaves at -1 while it refuses a
# stream's 1,001st consumer, so it could never warn; and it read the count before
# its own deletions.


def _stream_info(limit=-1, count=0):
    return SimpleNamespace(config=SimpleNamespace(max_consumers=limit), state=SimpleNamespace(consumer_count=count))


def _account_info(limit=-1):
    return SimpleNamespace(consumers=0, limits=SimpleNamespace(max_consumers=limit))


@pytest.mark.parametrize(
    ("stream_limit", "account_limit", "version", "expected"),
    [
        (-1, -1, (2, 15), (1000, LIMIT_FROM_NATS_DEFAULT)),  # the incident: both fields read -1
        (1500, -1, (2, 15), (1500, LIMIT_FROM_STREAM)),  # a raised stream limit replaces the default
        (-1, 1500, (2, 15), (1500, LIMIT_FROM_ACCOUNT)),
        (1200, 1500, (2, 15), (1200, LIMIT_FROM_STREAM)),
        (20, -1, (2, 16), (20, LIMIT_FROM_STREAM)),
        (-1, -1, (2, 10), (None, None)),  # no default before 2.15
        (-1, -1, None, (None, None)),  # unknown version: no guessed default
        (300, -1, (2, 10), (300, LIMIT_FROM_STREAM)),
    ],
)
def test_the_enforced_stream_limit(stream_limit, account_limit, version, expected) -> None:
    capacity = stream_consumer_capacity("S", _stream_info(stream_limit, 10), _account_info(account_limit), version)
    assert (capacity.consumer_limit, capacity.limit_source) == expected
    assert capacity.consumer_count == 10


def test_the_server_version_is_read_or_unknown() -> None:
    from nats.aio.client import ServerVersion

    assert server_version_of(SimpleNamespace(connected_server_version=ServerVersion("2.15.3"))) == (2, 15)
    assert server_version_of(SimpleNamespace(connected_server_version=ServerVersion("0.0.0-unknown"))) is None
    assert server_version_of(SimpleNamespace(connected_server_version=ServerVersion("not a version"))) is None
    assert server_version_of(None) is None
    assert version_label(None) == "unknown"


def test_the_warning_starts_at_ninety_percent_of_the_limit(caplog) -> None:
    def capacity(count):
        return stream_consumer_capacity("S", _stream_info(-1, count), _account_info(), (2, 15))

    with caplog.at_level(logging.WARNING):
        assert not log_capacity_if_high(logging.getLogger("test.capacity"), capacity(899), reporter="tester")
        assert log_capacity_if_high(logging.getLogger("test.capacity"), capacity(900), reporter="tester")
        assert not log_capacity_if_high(logging.getLogger("test.capacity"), None, reporter="tester")
    (line,) = [r.getMessage() for r in caplog.records]
    assert (
        "event=nats_stream_consumer_capacity_high stream_name=S consumer_count=900 consumer_limit=1000 "
        "limit_source=nats_default nats_server_version=2.15 reporter=tester"
    ) in line


@pytest.mark.asyncio
async def test_a_new_team_without_a_work_stream_reads_no_capacity_and_warns_nothing(caplog) -> None:
    reconciler = NatsConsumerReconciler(jetstream=_JetStream([], stream_missing=True), mapper=MAPPER)
    with caplog.at_level(logging.DEBUG):
        assert await reconciler.stream_capacity() is None
    assert caplog.records == []


@pytest.mark.asyncio
async def test_an_unreadable_stream_is_logged_and_gives_no_capacity(caplog) -> None:
    class _Broken(_JetStream):
        async def stream_info(self, stream_name):
            raise RuntimeError("timeout")

    with caplog.at_level(logging.WARNING):
        assert await NatsConsumerReconciler(jetstream=_Broken([]), mapper=MAPPER).stream_capacity() is None
    assert "event=nats_consumer_capacity_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_the_check_reports_capacity_after_its_own_deletions() -> None:
    from custom_transport.nats_consumer_reconciler import KIND_LASTING_AGENT

    jetstream = _JetStream(
        [_info(f"gone_{index}", AGENT_DIRECT_ROLE) for index in range(3)] + [_info("asking", AGENT_DIRECT_ROLE, waiting=1)],
        extra_consumers=5,
    )
    reconciler = NatsConsumerReconciler(jetstream=jetstream, mapper=MAPPER, server_version=(2, 15))
    result = await reconciler.reconcile(
        apply=True, minimum_age_seconds=3600.0, delete_kinds=frozenset({KIND_LASTING_AGENT})
    )
    assert result.deleted_count == 3
    assert result.stream_capacity.consumer_count == 6  # 9 before: the asking one and 5 others are left
    assert result.account_consumer_count == 6


def test_the_scripts_report_names_the_stream_and_warns_near_its_limit(caplog) -> None:
    import scripts.reconcile_nats_consumers as script

    near = stream_consumer_capacity(MAPPER.work_stream_name(), _stream_info(-1, 950), _account_info(), (2, 15))
    result = SimpleNamespace(
        decisions=(), applied=True, scanned_count=0, candidate_count=0, deleted_count=0,
        account_consumer_count=1200, account_max_consumers=1000, stream_capacity=near,
    )
    with caplog.at_level(logging.INFO):
        script._report(result, apply=True, delete_kinds=None, verbose=False)
    assert "stream_consumer_count=950 stream_consumer_limit=1000 stream_limit_source=nats_default" in caplog.text
    assert "nats_server_version=2.15" in caplog.text
    assert "event=nats_stream_consumer_capacity_high" in caplog.text and "reporter=consumer_check" in caplog.text
    # The whole account's count against a per-stream limit is no longer a warning.
    assert "event=nats_consumer_capacity_high" not in caplog.text


# -- the team lead's removal ---------------------------------------------------


class _Adapter:
    """The adapter's calls, through the real reconciler over the in-memory JetStream."""

    def __init__(self, consumers, *, server_version=(2, 15), **jetstream) -> None:
        self.jetstream = _JetStream(consumers, **jetstream)
        self.reconciler = NatsConsumerReconciler(jetstream=self.jetstream, mapper=MAPPER, server_version=server_version)
        self.reasons: list[str] = []
        self.listings = 0
        self.capacity_reads = 0

    async def agent_consumers(self):
        self.listings += 1
        return await self.reconciler.agent_consumers()

    async def stream_consumer_capacity(self):
        self.capacity_reads += 1
        return await self.reconciler.stream_capacity()

    async def delete_agent_consumers(self, consumers, *, reason):
        self.reasons.append(reason)
        return await self.reconciler.delete_agent_consumers(consumers, reason=reason)

    @property
    def deleted(self) -> list[str]:
        return sorted(name for _stream, name in self.jetstream.deleted)


@pytest.fixture
def team(lead, monkeypatch):
    lead._agent_state_store = AgentStateStore(
        storage_settings=lead.task_manager.storage_settings, logger=logging.getLogger("test.cleanup")
    )
    lead.schedule_maintenance = {}
    lead._worker_consumer_sweep_at = None
    lead._stream_capacity_check_at = None
    # A start whose process is gone reads "absent"; every other one is running.
    absent = {"crashed_worker"}
    lead._local_team_runtime.inspect_launch = lambda record: (
        "absent" if record.agent_id in absent else "starting"
    )
    return lead


def _attach(team, adapter) -> _Adapter:
    team._ensure_nats_transport_adapter = lambda: adapter
    return adapter


def _configure_agent_id(monkeypatch, agent_id: str) -> None:
    """The saved configuration names ``agent.agent_id`` (``current_launch_config``)."""

    monkeypatch.setattr("config_loader._config", Config({"agent": {"agent_id": agent_id}}))


def _reports_in(team, agent_id: str) -> None:
    """A status row with a fresh heartbeat and no process: the heartbeat decides."""

    team._agent_state_store.upsert_if_newer(
        AgentStatus(
            status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
            named_agent_type="Research", self_id=agent_id, is_paused=False, name=agent_id,
            team_id=TEAM, agent_role="worker", is_exiting=False,
            last_seen_at=datetime.now(timezone.utc).isoformat(), process_identity=None,
        )
    )


def _prune_ended_records(team) -> None:
    """What the scheduled-worker controller does after ``retention_days``."""

    AgentLaunchRepository(team.task_manager.storage_settings).prune(
        TEAM, before=datetime.now(timezone.utc) + timedelta(days=1)
    )


@pytest.mark.asyncio
async def test_a_gone_worker_whose_start_record_was_pruned_loses_its_lasting_consumers(
    team, monkeypatch, caplog
) -> None:
    """Review of 2026-11-20 (probe test_probe_pruned_start_records.py): it was kept as configured."""

    _configure_agent_id(monkeypatch, "configured_agent")
    launch_record(team, AgentLaunchState.RETIRED, agent_id=PRUNED_WORKER)
    _prune_ended_records(team)
    launch_record(team, AgentLaunchState.RETIRED, agent_id="retired_worker")
    launch_record(team, AgentLaunchState.FAILED, agent_id="failed_worker")
    launch_record(team, AgentLaunchState.STARTING, agent_id="crashed_worker")
    launch_record(team, AgentLaunchState.READY, agent_id="busy_worker")
    launch_record(team, AgentLaunchState.READY, agent_id="live_worker")
    _reports_in(team, "reporting_agent")
    adapter = _attach(
        team,
        _Adapter(
            [
                _info(PRUNED_WORKER, AGENT_DIRECT_ROLE),
                _info(PRUNED_WORKER, AGENT_CONTROL_ROLE),
                _info("retired_worker", AGENT_DIRECT_ROLE),
                _info("retired_worker", AGENT_CONTROL_ROLE),
                _info("failed_worker", AGENT_DIRECT_ROLE),
                _info("crashed_worker", AGENT_CONTROL_ROLE),
                # Running, and asking for nothing while it works: kept.
                _info("busy_worker", AGENT_DIRECT_ROLE),
                _info("busy_worker", AGENT_CONTROL_ROLE),
                # No start record, but its heartbeat is fresh: the liveness rule keeps it.
                _info("reporting_agent", AGENT_DIRECT_ROLE),
                _info("reporting_agent", AGENT_CONTROL_ROLE),
                # Configured agents: no start record and no status row, kept anyway.
                _info("configured_agent", AGENT_DIRECT_ROLE),
                _info("lead", AGENT_CONTROL_ROLE),
                # Process-owned: kept without a start record (a startup agent that
                # has not written its status row yet) or while its record is
                # active; removed when its start record proves it gone.
                _info(STARTUP_AGENT, AGENT_DIRECT_ROLE, scope="process"),
                _info("live_worker", AGENT_DIRECT_ROLE, scope="process"),
                _info("failed_worker", AGENT_CONTROL_ROLE, scope="process"),
                _info("other_team_worker", AGENT_DIRECT_ROLE, team="team_other"),
            ]
        ),
    )

    with caplog.at_level(logging.INFO):
        result = await sweep_worker_consumers_at_start(team)

    assert adapter.deleted == sorted(
        [
            _name(PRUNED_WORKER, AGENT_DIRECT_ROLE),
            _name(PRUNED_WORKER, AGENT_CONTROL_ROLE),
            _name("retired_worker", AGENT_DIRECT_ROLE),
            _name("retired_worker", AGENT_CONTROL_ROLE),
            _name("failed_worker", AGENT_DIRECT_ROLE),
            _name("failed_worker", AGENT_CONTROL_ROLE),
            _name("crashed_worker", AGENT_CONTROL_ROLE),
        ]
    )
    assert adapter.reasons == [GONE_WORKER_REASON]
    kept_evidence = {"heartbeat_fresh": 1, "launch_active": 1}  # reporting_agent; busy_worker
    assert result == SweepResult(
        agent_consumers=15,
        lasting_consumers=12,
        configured_agents=2,
        kept_not_gone=2,
        gone_with_start_record=3,
        gone_without_start_record=1,
        deleted=7,
        kept_evidence=kept_evidence,
    )
    # The stream's count after the deletions (16 on it, one with another team's
    # metadata; 7 deleted) against NATS 2.15's default.
    assert (
        "event=worker_consumer_cleanup_done team_id=team sweep=start agent_consumers=15 lasting_consumers=12 "
        f"push_era_consumers=0 configured_agents=2 kept_listening=0 kept_not_gone=2 kept_evidence={kept_evidence} "
        "gone_with_start_record=3 gone_without_start_record=1 deleted=7 next_sweep_in_seconds=3600.0 "
        "stream_consumers=9 stream_consumer_limit=1000 nats_server_version=2.15"
    ) in caplog.text


@pytest.mark.asyncio
async def test_push_era_leftovers_are_removed_and_a_bound_one_is_kept(team) -> None:
    """Started workers of the push-delivery releases got lasting push consumers too."""

    adapter = _attach(
        team,
        _Adapter(
            [
                _info("push_worker", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA),
                _info("push_worker", AGENT_CONTROL_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA),
                _info("bound_worker", AGENT_DIRECT_ROLE, schema=PUSH_CONSUMER_METADATA_SCHEMA, push_bound=True),
            ]
        ),
    )
    result = await remove_gone_worker_consumers(team)
    assert adapter.deleted == sorted(
        [_name("push_worker", AGENT_DIRECT_ROLE), _name("push_worker", AGENT_CONTROL_ROLE)]
    )
    assert (result.push_era_consumers, result.gone_without_start_record, result.kept_listening) == (3, 1, 1)


@pytest.mark.asyncio
async def test_an_owner_asking_for_messages_keeps_its_consumers_whatever_its_records_say(team) -> None:
    """Only a live process holds a request open; held messages prove nothing."""

    launch_record(team, AgentLaunchState.FAILED, agent_id="misjudged")
    adapter = _attach(
        team,
        _Adapter(
            [
                _info("misjudged", AGENT_DIRECT_ROLE, waiting=1),
                _info("misjudged", AGENT_CONTROL_ROLE),
                # Died while holding a message: still gone.
                _info("died_holding", AGENT_DIRECT_ROLE, held=1),
            ]
        ),
    )
    result = await remove_gone_worker_consumers(team)
    assert adapter.deleted == [_name("died_holding", AGENT_DIRECT_ROLE)]
    assert (result.kept_listening, result.gone_without_start_record, result.kept_owners) == (1, 1, 1)


@pytest.mark.asyncio
async def test_nothing_is_deleted_when_the_configuration_cannot_be_read(team, monkeypatch) -> None:
    def unreadable(agent):
        raise ValueError("instance_config.yaml is malformed")

    monkeypatch.setattr(cleanup, "current_launch_config", unreadable)
    adapter = _attach(team, _Adapter([_info("retired_worker", AGENT_DIRECT_ROLE)]))
    with pytest.raises(ValueError, match="malformed"):
        await remove_gone_worker_consumers(team)
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_an_empty_stream_reads_no_records_or_configuration(team, monkeypatch) -> None:
    monkeypatch.setattr(cleanup, "AgentLaunchRepository", None)  # would fail if read
    monkeypatch.setattr(cleanup, "current_launch_config", None)
    adapter = _attach(team, _Adapter([]))
    assert await remove_gone_worker_consumers(team) == SweepResult()
    assert adapter.listings == 1


# -- when the lead sweeps -------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cleanup, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.mark.asyncio
async def test_a_failed_sweep_is_tried_again_after_the_retry_delay(team, clock, monkeypatch, caplog) -> None:
    calls = []

    async def failing(agent):
        calls.append("fail")
        raise RuntimeError("nats unavailable")

    monkeypatch.setattr(cleanup, "remove_gone_worker_consumers", failing)
    with caplog.at_level(logging.ERROR):
        assert await sweep_worker_consumers_at_start(team) is None  # never raises
    assert "event=worker_consumer_cleanup_failed team_id=team sweep=start" in caplog.text
    assert team._worker_consumer_sweep_at == 1000.0 + RETRY_SECONDS

    clock[0] += RETRY_SECONDS - 1
    await sweep_worker_consumers_if_due(team)
    assert calls == ["fail"]

    async def succeeding(agent):
        calls.append("ok")
        return SweepResult()

    monkeypatch.setattr(cleanup, "remove_gone_worker_consumers", succeeding)
    clock[0] += 1
    await sweep_worker_consumers_if_due(team)
    assert calls == ["fail", "ok"]
    assert team._worker_consumer_sweep_at is None
    clock[0] += RESWEEP_SECONDS
    await sweep_worker_consumers_if_due(team)
    assert calls == ["fail", "ok"]  # nothing was kept: no further sweep


@pytest.mark.asyncio
async def test_an_owner_kept_at_start_is_removed_by_a_later_sweep_once_it_is_gone(team, clock) -> None:
    """Review of 2026-11-20 (FAIL-5): the sweep ran once per process."""

    record = launch_record(team, AgentLaunchState.READY, agent_id="busy_worker")
    adapter = _attach(
        team, _Adapter([_info("busy_worker", AGENT_DIRECT_ROLE), _info("busy_worker", AGENT_CONTROL_ROLE)])
    )
    kept = await sweep_worker_consumers_at_start(team)
    assert (kept.kept_not_gone, kept.deleted) == (1, 0)
    assert team._worker_consumer_sweep_at == 1000.0 + RESWEEP_SECONDS

    clock[0] += RESWEEP_SECONDS - 1
    assert await sweep_worker_consumers_if_due(team) is None
    assert adapter.listings == 1  # not due: no NATS request

    AgentLaunchRepository(team.task_manager.storage_settings).update(record, state=AgentLaunchState.RETIRED)
    clock[0] += 1
    removed = await sweep_worker_consumers_if_due(team)
    assert (removed.gone_with_start_record, removed.deleted) == (1, 2)
    assert team._worker_consumer_sweep_at is None

    clock[0] += 10 * RESWEEP_SECONDS
    await sweep_worker_consumers_if_due(team)
    assert adapter.listings == 2  # nothing kept: sweeping stopped


@pytest.mark.asyncio
async def test_the_lead_sweeps_once_before_its_own_consumers_and_before_any_worker_can_start(
    tmp_path, monkeypatch
) -> None:
    """Review of 2026-11-20 (FAIL-1, EVD-3): the sweep ran inside the pool controller.

    That was after the first scheduled starts and beside the startup takeover,
    and no test noticed its call being removed. The lead's schedule and
    scheduler loops are created at the end of ``_connect_nats``, after every
    subscription, so a sweep before the first subscription comes before them.
    """

    from tests.test_schedule_startup_delivery import SubscriptionProbeComplete, startup_probe

    order: list[str] = []

    async def sweep(agent):
        order.append("sweep")

    async def subscribe_route(**kwargs):
        order.append(f"subscribe {kwargs['consumer_role']}")
        raise SubscriptionProbeComplete()

    monkeypatch.setattr(cleanup, "sweep_worker_consumers_at_start", sweep)
    for is_team_lead, team_runtime, expected in (
        (True, object(), ["sweep", "subscribe agent_direct"]),
        (False, object(), ["subscribe agent_direct"]),
        (True, None, ["subscribe agent_direct"]),
    ):
        order.clear()
        agent = startup_probe(tmp_path)
        agent.is_team_lead = is_team_lead
        agent._local_team_runtime = team_runtime
        agent._ensure_nats_transport_adapter = lambda: SimpleNamespace(
            connect=AsyncMock(), subscribe_route=subscribe_route, subscribe_subject=AsyncMock()
        )
        with pytest.raises(SubscriptionProbeComplete):
            await agent._connect_nats()
        assert order == expected


@pytest.mark.asyncio
async def test_each_schedule_loop_pass_sweeps_again_if_due_before_it_starts_workers(monkeypatch) -> None:
    import custom_agents.schedule_worker_controller as controller

    order: list[str] = []
    exit_event = asyncio.Event()

    async def step(name):
        order.append(name)

    async def last_step(agent):
        order.append("pools")
        exit_event.set()

    monkeypatch.setattr(cleanup, "sweep_worker_consumers_if_due", lambda agent: step("sweep_if_due"))
    monkeypatch.setattr(controller, "reconcile_schedule_workers", lambda agent: step("scheduled_starts"))
    monkeypatch.setattr("custom_agents.worker_pool_controller.reconcile_worker_pools", last_step)
    monkeypatch.setattr(
        "custom_agents.memory_review_cleanup_runtime.ReviewCleanupController",
        lambda agent: SimpleNamespace(reconcile=AsyncMock()),
    )
    monkeypatch.setattr(controller, "current_worker_limits", lambda agent: SimpleNamespace(reconcile_interval_seconds=0))
    agent = SimpleNamespace(
        is_team_lead=False,
        team_id=TEAM,
        _logger=logging.getLogger("test.cleanup"),
        get_exit_event=lambda: exit_event,
        task_manager=SimpleNamespace(operations_runtime=TaskOperationRuntime()),
        _schedule_store=SimpleNamespace(get_all=lambda: []),
    )
    await asyncio.wait_for(controller.local_schedule_runtime_loop(agent), timeout=5)
    assert order == ["sweep_if_due", "scheduled_starts", "pools"]


# -- the lead's reads of how full its work stream is -----------------------------


@pytest.mark.asyncio
async def test_the_lead_warns_near_the_limit_also_when_it_found_no_agent_consumers(team, clock, caplog) -> None:
    _attach(team, _Adapter([], extra_consumers=950))  # worker queues and other consumers only
    with caplog.at_level(logging.INFO):
        await sweep_worker_consumers_at_start(team)
    assert "stream_consumers=950 stream_consumer_limit=1000 nats_server_version=2.15" in caplog.text
    (warning,) = [r.getMessage() for r in caplog.records if "nats_stream_consumer_capacity_high" in r.getMessage()]
    assert "consumer_count=950 consumer_limit=1000 limit_source=nats_default" in warning
    assert "reporter=team_lead" in warning


@pytest.mark.asyncio
async def test_a_failed_capacity_read_leaves_the_finished_sweep_alone(team, clock, caplog) -> None:
    adapter = _attach(team, _Adapter([_info("retired_worker", AGENT_DIRECT_ROLE)]))
    launch_record(team, AgentLaunchState.RETIRED, agent_id="retired_worker")

    async def unreadable():
        raise RuntimeError("not connected")

    adapter.stream_consumer_capacity = unreadable
    with caplog.at_level(logging.INFO):
        result = await sweep_worker_consumers_at_start(team)
    assert result is not None and result.deleted == 1
    assert team._worker_consumer_sweep_at is None  # nothing kept: no retry was scheduled
    assert "event=worker_consumer_cleanup_failed" not in caplog.text
    assert "event=nats_consumer_capacity_unavailable" in caplog.text
    assert "event=worker_consumer_cleanup_done" in caplog.text and "stream_consumers=None" in caplog.text


@pytest.mark.asyncio
async def test_the_lead_reads_capacity_hourly_after_its_sweeps_stopped(team, clock, caplog) -> None:
    """The incident built up over weeks of running; a start-only check would not have seen it."""

    adapter = _attach(team, _Adapter([], extra_consumers=10))
    await sweep_worker_consumers_at_start(team)
    assert team._worker_consumer_sweep_at is None  # nothing kept: no further sweep
    assert team._stream_capacity_check_at == 1000.0 + CAPACITY_CHECK_SECONDS
    assert (adapter.listings, adapter.capacity_reads) == (1, 1)

    clock[0] += CAPACITY_CHECK_SECONDS - 1
    await sweep_worker_consumers_if_due(team)
    assert adapter.capacity_reads == 1

    adapter.jetstream.extra_consumers = 960  # the stream filled while the team ran
    clock[0] += 1
    with caplog.at_level(logging.WARNING):
        assert await sweep_worker_consumers_if_due(team) is None
    assert (adapter.listings, adapter.capacity_reads) == (1, 2)  # read again, no sweep
    assert "event=nats_stream_consumer_capacity_high" in caplog.text and "consumer_count=960" in caplog.text
    assert team._stream_capacity_check_at == clock[0] + CAPACITY_CHECK_SECONDS
