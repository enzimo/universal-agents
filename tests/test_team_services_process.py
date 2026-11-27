"""The team services process around its lock (team_services.process): what it does before serving, how it ends,
and what it serves (team_services.service).

The process on a real NATS server is covered by tests/test_live_team_services.py and
tests/test_live_launcher_message_services.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from team_services import process
from team_services.lock import LockConnectionClosed, LockLost
from team_services.memory import SIDE_BY_SIDE_KINDS, MemoryOperations
from team_services.service import OperationFamily, TeamServices

NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_operation_resources_close_after_serving_also_when_startup_fails(monkeypatch) -> None:
    events = []

    async def failed(*args, **_kwargs):
        events.append("serve")
        raise RuntimeError("startup failed")

    monkeypatch.setattr(process, "_serve_team", failed)
    family = OperationFamily({}, close=lambda: events.append("close"))
    with pytest.raises(RuntimeError, match="startup failed"):
        await process.serve_team(SimpleNamespace(logger=logging.getLogger("test")), "team_one", asyncio.Event(), [family])
    assert events == ["serve", "close"]


@pytest.mark.asyncio
async def test_a_takeover_reads_the_newest_certificate_and_the_current_revocations_before_serving() -> None:
    """A standby may wait for days: the serving instance may have renewed their certificate, and the revocations
    record the standby read when it started lasts a day."""

    steps: list[str] = []

    async def certificate() -> None:
        steps.append("certificate")

    async def revocations() -> None:
        steps.append("revocations")

    await process._prepare(SimpleNamespace(take_newest_certificate=certificate, refresh_trust=revocations))
    assert steps == ["certificate", "revocations"]


class _Adapter:
    def __init__(self, **_kwargs) -> None:
        self.nats_connection = None
        self.closed = False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [LockLost("another instance took the lock"), LockConnectionClosed("NATS closed")])
async def test_losing_the_lock_ends_the_process_with_exit_code_1(monkeypatch, caplog, lost: LockLost) -> None:
    """The launcher's supervisor sees the exit and stops the team, rather than two instances serving."""

    async def serve_team(_host, _team, _stop, _families, **_kwargs) -> None:
        raise lost

    monkeypatch.setattr(process, "NatsTransportAdapter", _Adapter)
    monkeypatch.setattr(process, "MessageHost", lambda *_args, **_kwargs: SimpleNamespace(clock=None, nc=None))
    monkeypatch.setattr(process, "team_operations", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(process, "serve_team", serve_team)
    transport = SimpleNamespace(nats_servers=["nats://127.0.0.1:4222"], nats_url=None, subject_prefix="ua",
                                nats_user=None, nats_password=None, credentials_file=None,
                                nats=SimpleNamespace(credential_lifetime_seconds=3600))
    config = SimpleNamespace(manager=SimpleNamespace(team_id="team_one", named_agent_type="TeamArchitect"),
                             transport=transport, instance_name="test",
                             layout=None)
    with pytest.raises(SystemExit) as exited:
        await process.run_team_services(config)  # type: ignore[arg-type]
    assert exited.value.code == 1
    assert "event=team_services_stopped" in caplog.text


# -- what the team services serve -------------------------------------------------------------------------

async def _answer(_verified: Any) -> dict[str, Any]:
    return {"outcome": "ok"}


def _sessions() -> Any:
    return SimpleNamespace(
        open=_answer,
        renew=_answer,
        close=_answer,
        maintain=AsyncMock(),
        _changing=asyncio.Lock(),
        ensure_partitions=None,
    )


def test_the_team_services_report_each_familys_side_by_side_kinds_and_upkeep() -> None:
    async def purge() -> None:
        return None

    # The first family names memory.create as side by side, but another family answers it, in arrival order.
    reads = OperationFamily({"memory.list": _answer, "memory.show": _answer},
                            frozenset({"memory.list", "memory.create"}), purge)
    changes = OperationFamily({"memory.create": _answer})
    services = TeamServices(_sessions(), [reads, changes])
    assert services.side_by_side == frozenset({"memory.list"})
    assert services.upkeep == [purge]
    assert services.served == frozenset({"sessions.open", "sessions.renew", "sessions.close", "memory.list",
                                         "memory.show", "memory.create"})

    memory = MemoryOperations(SimpleNamespace(), SimpleNamespace(team_id="team_one"), {}, lambda: NOW,
                              logging.getLogger("test"))
    with_memory = TeamServices(_sessions(), [memory.family()])
    assert with_memory.side_by_side == SIDE_BY_SIDE_KINDS
    assert with_memory.upkeep == [memory.purge_outcomes]


@pytest.mark.parametrize("taken", ["memory.list", "sessions.open"])
def test_two_handlers_for_one_kind_are_refused(taken: str) -> None:
    reads = OperationFamily({"memory.list": _answer})
    with pytest.raises(ValueError, match="Two handlers"):
        TeamServices(_sessions(), [reads, OperationFamily({taken: _answer})])


# -- stopping ---------------------------------------------------------------------------------------------------

class _Lock:
    instance = "instance-1"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def acquire(self, _stop: asyncio.Event) -> bool:
        return True

    async def keep(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def release(self) -> None:
        self.events.append("lock released")


class _Live:
    """A host serving the team's address with one request still being handled until ``answered`` is set."""

    def __init__(self, events: list[str], answered: asyncio.Event) -> None:
        self.events, self.answered = events, answered
        self.trust = None
        self.identity = SimpleNamespace(installation="install_example")

    async def serve(self, _subject: str, _receiver: Any, _handler: Any, *, side_by_side_kinds: frozenset[str]) -> Any:
        async def unsubscribe() -> None:
            self.events.append("unsubscribed")

        return SimpleNamespace(unsubscribe=unsubscribe)

    async def drain(self) -> None:
        self.events.append("draining")
        await self.answered.wait()
        self.events.append("request answered")


@pytest.mark.asyncio
async def test_stopping_waits_for_a_request_still_being_handled_before_the_lock_is_released(monkeypatch) -> None:
    """A change the store committed after the lock was released could race the next instance."""

    events: list[str] = []
    answered, serving = asyncio.Event(), asyncio.Event()
    live = _Live(events, answered)

    async def keep_up(stop: asyncio.Event, drained: Any) -> None:
        await stop.wait()

    host = SimpleNamespace(start=AsyncMock(return_value=live), nc=SimpleNamespace(flush=AsyncMock()), clock=lambda: NOW,
                           logger=logging.getLogger("test"), take_newest_certificate=AsyncMock(),
                           refresh_trust=AsyncMock(), records=object(), publish_liveness=AsyncMock(), layout=None,
                           keep_up=keep_up)
    shared_locks: list[asyncio.Lock] = []
    fake_sessions = _sessions()

    class _FakePartitions:
        def __init__(self, _records, _trust, _inst, _team, lock, *_args, **_kwargs) -> None:
            shared_locks.append(lock)

        async def ensure_unowned_assigned(self) -> None:
            return None

        async def maintain(self) -> None:
            return None

        async def run(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _Lock(events)))
    monkeypatch.setattr(process, "SessionService", lambda *_args: fake_sessions)
    monkeypatch.setattr(process, "PartitionService", _FakePartitions)
    monkeypatch.setattr(process, "mark_serving", lambda *_args: serving.set())
    stop = asyncio.Event()

    served = asyncio.create_task(process.serve_team(host, "team_one", stop, []))  # type: ignore[arg-type]
    await asyncio.wait_for(serving.wait(), timeout=5)
    assert shared_locks == [fake_sessions._changing]
    assert fake_sessions.ensure_partitions is not None
    stop.set()
    for _ in range(100):
        if "draining" in events or served.done():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert not served.done() and events == ["unsubscribed", "draining"]
    answered.set()
    await asyncio.wait_for(served, timeout=5)
    assert events == ["unsubscribed", "draining", "request answered", "lock released"]


@pytest.mark.asyncio
async def test_a_drain_that_takes_longer_than_its_limit_is_logged_and_stopping_goes_on(monkeypatch, caplog) -> None:
    monkeypatch.setattr(process, "DRAIN_SECONDS", 0.05)
    never = asyncio.Event()
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(process._drain(SimpleNamespace(drain=never.wait), logging.getLogger("test")),
                               timeout=5)
    assert "event=team_services_drain_timed_out" in caplog.text


@pytest.mark.asyncio
async def test_f9_lock_lost_stops_partition_maintenance_and_upkeep_before_drain(monkeypatch) -> None:
    """F9/FA-8/CR-7: On LockLost, partition_task, maintenance, and upkeep are cancelled and stopped
    immediately BEFORE unsubscribe and _drain run."""

    serving = asyncio.Event()
    trigger_loss = asyncio.Event()
    allow_drain_finish = asyncio.Event()
    observations: dict[str, Any] = {"events": []}

    class _Lock:
        instance = "inst-1"

        async def acquire(self, _stop: asyncio.Event) -> bool:
            return True

        async def keep(self, _stop: asyncio.Event) -> None:
            await trigger_loss.wait()
            raise LockLost("another instance took the lock")

        async def release(self) -> None:
            observations["events"].append("lock_released")

    class _Sub:
        async def unsubscribe(self) -> None:
            observations["events"].append("unsubscribed")

    class _Live:
        identity = SimpleNamespace(installation="install_one")
        trust = SimpleNamespace()

        async def serve(self, *_args: Any, **_kwargs: Any) -> _Sub:
            return _Sub()

        async def drain(self) -> None:
            observations["stop_set_during_drain"] = stop.is_set()
            observations["partition_stopped_during_drain"] = observations.get("partition_stopped", False)
            observations["upkeep_stopped_during_drain"] = observations.get("upkeep_stopped", False)
            observations["events"].append("draining")
            await allow_drain_finish.wait()

    async def keep_up(stop_event: asyncio.Event, *, drained: Any = None) -> None:
        try:
            await stop_event.wait()
        finally:
            observations["upkeep_stopped"] = True

    class _FakePartitions:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_unowned_assigned(self) -> None:
            return None

        async def maintain(self) -> None:
            return None

        async def run(self, stop_event: asyncio.Event) -> None:
            try:
                await stop_event.wait()
            finally:
                observations["partition_stopped"] = True

    live = _Live()
    host = SimpleNamespace(
        start=AsyncMock(return_value=live),
        nc=SimpleNamespace(flush=AsyncMock()),
        clock=lambda: NOW,
        logger=logging.getLogger("test"),
        take_newest_certificate=AsyncMock(),
        refresh_trust=AsyncMock(),
        records=object(),
        publish_liveness=AsyncMock(),
        layout=None,
        keep_up=keep_up,
    )
    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _Lock()))
    monkeypatch.setattr(process, "SessionService", lambda *_args: _sessions())
    monkeypatch.setattr(process, "PartitionService", _FakePartitions)
    monkeypatch.setattr(process, "mark_serving", lambda *_args: serving.set())

    stop = asyncio.Event()
    served = asyncio.create_task(process.serve_team(host, "team_one", stop, []))  # type: ignore[arg-type]
    await asyncio.wait_for(serving.wait(), timeout=5)
    trigger_loss.set()
    for _ in range(100):
        if "draining" in observations["events"] or served.done():
            break
        await asyncio.sleep(0.01)
    assert observations.get("stop_set_during_drain") is True
    assert observations.get("partition_stopped_during_drain") is True
    assert observations.get("upkeep_stopped_during_drain") is True
    allow_drain_finish.set()
    with pytest.raises(LockLost):
        await asyncio.wait_for(served, timeout=5)


@pytest.mark.asyncio
async def test_lock_lost_stops_family_background_runners_before_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """OperationFamily.run (e.g. DeadLetterService.run) starts on serve and cancels immediately on LockLost."""

    serving = asyncio.Event()
    trigger_loss = asyncio.Event()
    allow_drain_finish = asyncio.Event()
    observations: dict[str, Any] = {"runner_started": False, "runner_stopped": False}

    class _Lock:
        instance = "inst-runner"

        async def acquire(self, _stop: asyncio.Event) -> bool:
            return True

        async def keep(self, _stop: asyncio.Event) -> None:
            await trigger_loss.wait()
            raise LockLost("lock lost")

        async def release(self) -> None:
            return None

    class _Sub:
        async def unsubscribe(self) -> None:
            return None

    class _Live:
        identity = SimpleNamespace(installation="install_one")
        trust = SimpleNamespace()

        async def serve(self, *_args: Any, **_kwargs: Any) -> _Sub:
            return _Sub()

        async def drain(self) -> None:
            observations["runner_stopped_during_drain"] = observations["runner_stopped"]
            await allow_drain_finish.wait()

    class _FakePartitions:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_unowned_assigned(self) -> None:
            return None

        async def maintain(self) -> None:
            return None

        async def run(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

    async def keep_up(stop_event: asyncio.Event, *, drained: Any = None) -> None:
        await stop_event.wait()

    async def family_runner(stop_event: asyncio.Event) -> None:
        observations["runner_started"] = True
        try:
            await stop_event.wait()
        finally:
            observations["runner_stopped"] = True

    live = _Live()
    host = SimpleNamespace(
        start=AsyncMock(return_value=live),
        nc=SimpleNamespace(flush=AsyncMock()),
        clock=lambda: NOW,
        logger=logging.getLogger("test"),
        take_newest_certificate=AsyncMock(),
        refresh_trust=AsyncMock(),
        records=object(),
        publish_liveness=AsyncMock(),
        layout=None,
        keep_up=keep_up,
    )
    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _Lock()))
    monkeypatch.setattr(process, "SessionService", lambda *_args: _sessions())
    monkeypatch.setattr(process, "PartitionService", _FakePartitions)
    monkeypatch.setattr(process, "mark_serving", lambda *_args: serving.set())

    stop = asyncio.Event()
    family = OperationFamily({"team.delete_person_data": _answer}, run=family_runner)
    served = asyncio.create_task(process.serve_team(host, "team_one", stop, [family]))  # type: ignore[arg-type]
    await asyncio.wait_for(serving.wait(), timeout=5)
    for _ in range(50):
        if observations["runner_started"]:
            break
        await asyncio.sleep(0.01)
    assert observations["runner_started"] is True
    trigger_loss.set()
    for _ in range(100):
        if "runner_stopped_during_drain" in observations or served.done():
            break
        await asyncio.sleep(0.01)
    assert observations.get("runner_stopped_during_drain") is True
    allow_drain_finish.set()
    with pytest.raises(LockLost):
        await asyncio.wait_for(served, timeout=5)


@pytest.mark.asyncio
async def test_c01_family_prepare_runs_before_serving_svc_team(monkeypatch) -> None:
    """C01: OperationFamily.prepare (e.g. retrying pending blob deletes) finishes before live.serve subscribes."""

    events: list[str] = []
    serving = asyncio.Event()

    async def prepare_step() -> None:
        events.append("prepare")

    class _OrderedLive(_Live):
        async def serve(self, subject: str, _receiver: Any, _handler: Any, *, side_by_side_kinds: frozenset[str]) -> Any:
            events.append(f"serve:{subject}")
            return await super().serve(subject, _receiver, _handler, side_by_side_kinds=side_by_side_kinds)

    answered = asyncio.Event()
    answered.set()
    live = _OrderedLive(events, answered)

    async def keep_up(stop_event: asyncio.Event, *, drained: Any = None) -> None:
        await stop_event.wait()

    host = SimpleNamespace(
        start=AsyncMock(return_value=live),
        nc=SimpleNamespace(flush=AsyncMock()),
        clock=lambda: NOW,
        logger=logging.getLogger("test"),
        take_newest_certificate=AsyncMock(),
        refresh_trust=AsyncMock(),
        records=object(),
        publish_liveness=AsyncMock(),
        layout=None,
        keep_up=keep_up,
    )

    class _FakePartitions:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_unowned_assigned(self) -> None:
            return None

        async def maintain(self) -> None:
            return None

        async def run(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _Lock(events)))
    monkeypatch.setattr(process, "SessionService", lambda *_args: _sessions())
    monkeypatch.setattr(process, "PartitionService", _FakePartitions)
    monkeypatch.setattr(process, "mark_serving", lambda *_args: serving.set())

    stop = asyncio.Event()
    family = OperationFamily({"blobs.register": _answer}, prepare=prepare_step)
    served = asyncio.create_task(process.serve_team(host, "team_one", stop, [family]))  # type: ignore[arg-type]
    await asyncio.wait_for(serving.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(served, timeout=5)
    assert events.index("prepare") < events.index("serve:svc.team")


@pytest.mark.asyncio
async def test_c01_lock_lost_during_family_prepare_never_subscribes(monkeypatch) -> None:
    """C01: Losing the single-instance lock during family prepare cancels prepare and never subscribes svc.team."""

    events: list[str] = []
    prepare_started = asyncio.Event()
    trigger_loss = asyncio.Event()

    async def slow_prepare() -> None:
        events.append("prepare_started")
        prepare_started.set()
        await asyncio.sleep(60)

    class _LosingLock:
        instance = "inst-1"

        async def acquire(self, _stop: asyncio.Event) -> bool:
            return True

        async def keep(self, _stop: asyncio.Event) -> None:
            await trigger_loss.wait()
            raise LockLost("lost during prepare")

        async def release(self) -> None:
            events.append("lock_released")

    class _NeverServeLive:
        identity = SimpleNamespace(installation="install_one")
        trust = SimpleNamespace()

        async def serve(self, *_args: Any, **_kwargs: Any) -> Any:
            events.append("served_unexpectedly")
            raise AssertionError("live.serve must not be called after lock loss during prepare")

    host = SimpleNamespace(
        start=AsyncMock(return_value=_NeverServeLive()),
        nc=SimpleNamespace(flush=AsyncMock()),
        clock=lambda: NOW,
        logger=logging.getLogger("test"),
        take_newest_certificate=AsyncMock(),
        refresh_trust=AsyncMock(),
        records=object(),
        publish_liveness=AsyncMock(),
        layout=None,
        keep_up=AsyncMock(),
    )

    class _FakePartitions:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_unowned_assigned(self) -> None:
            return None

    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _LosingLock()))
    monkeypatch.setattr(process, "SessionService", lambda *_args: _sessions())
    monkeypatch.setattr(process, "PartitionService", _FakePartitions)

    stop = asyncio.Event()
    family = OperationFamily({"blobs.register": _answer}, prepare=slow_prepare)
    task = asyncio.create_task(process.serve_team(host, "team_one", stop, [family]))  # type: ignore[arg-type]
    await asyncio.wait_for(prepare_started.wait(), timeout=5)
    trigger_loss.set()
    with pytest.raises(LockLost):
        await asyncio.wait_for(task, timeout=5)
    assert "served_unexpectedly" not in events
    assert events == ["prepare_started", "lock_released"]


@pytest.mark.asyncio
async def test_c20_dead_letter_family_does_not_block_svc_team_serving_on_large_stream(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C20: TeamPersonDataOperations has prepare=None so svc.team serves immediately without waiting for DLQ scans."""

    from access_control.people_directory import PeopleDirectory
    from storage.access_repository import AccessRepository
    from storage.config import resolve_storage_settings
    from team_services.dead_letters import (
        DeadLetterService,
        TeamPersonDataOperations,
        TeamPersonDepartureService,
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "c20.db")}})
    access = AccessRepository(storage)
    people = PeopleDirectory(access, "team_one")
    dlq = DeadLetterService(
        team_id="team_one",
        live_fn=lambda: None,
        access_repository=access,
        people=people,
    )
    departure = TeamPersonDepartureService(team_id="team_one", live_fn=lambda: None, people=people)
    ops = TeamPersonDataOperations(dead_letters=dlq, departure=departure, people=people)
    family = ops.family()
    assert family.prepare is None

    # Even if maintain() blocks on a huge stream, serve_team subscribes and marks serving immediately!
    maintain_started = asyncio.Event()
    serve_reached = asyncio.Event()
    stop = asyncio.Event()

    async def slow_maintain() -> None:
        maintain_started.set()
        await asyncio.sleep(60)

    async def idle_run(run_stop: asyncio.Event) -> None:
        await run_stop.wait()

    monkeypatch.setattr(dlq, "maintain", slow_maintain)
    monkeypatch.setattr(dlq, "run", idle_run)

    class _FakeSub:
        async def unsubscribe(self) -> None:
            return None

    class _FakeLock:
        instance = "inst-c20"

        async def acquire(self, _stop: asyncio.Event) -> bool:
            return True

        async def keep(self, lock_stop: asyncio.Event) -> None:
            await lock_stop.wait()

        async def release(self) -> None:
            return None

    class _FakeLive:
        identity = SimpleNamespace(installation="install_one")
        trust = SimpleNamespace()

        async def serve(self, *_args: Any, **_kwargs: Any) -> _FakeSub:
            serve_reached.set()
            return _FakeSub()

        async def drain(self) -> None:
            return None

    class _FakePartitions:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_unowned_assigned(self) -> None:
            return None

        async def maintain(self) -> None:
            return None

        async def run(self, part_stop: asyncio.Event) -> None:
            await part_stop.wait()

    marked: list[str] = []
    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _FakeLock()))
    monkeypatch.setattr(process, "SessionService", lambda *_args: _sessions())
    monkeypatch.setattr(process, "PartitionService", _FakePartitions)
    monkeypatch.setattr(process, "mark_serving", lambda _layout, inst, _now: marked.append(inst))

    host = SimpleNamespace(
        start=AsyncMock(return_value=_FakeLive()),
        nc=SimpleNamespace(flush=AsyncMock()),
        clock=lambda: NOW,
        logger=logging.getLogger("test"),
        take_newest_certificate=AsyncMock(),
        refresh_trust=AsyncMock(),
        records=object(),
        publish_liveness=AsyncMock(),
        layout=None,
        keep_up=AsyncMock(),
    )

    task = asyncio.create_task(process.serve_team(host, "team_one", stop, [ops.family()]))  # type: ignore[arg-type]
    await asyncio.wait_for(serve_reached.wait(), timeout=5)
    await asyncio.wait_for(maintain_started.wait(), timeout=5)
    assert marked == ["inst-c20"]
    stop.set()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_team_services_roles_and_svc_scheduler_address_isolation() -> None:
    """team_services carries both team_services and scheduler roles and isolates svc.scheduler from svc.team."""

    from custom_transport.protocol1.live import Unsupported
    from team_services.scheduler import SCHEDULER_ADDRESS

    assert process.TEAM_SERVICES_ROLES == ("team_services", "scheduler")
    assert process.team_services_profile("team_one").roles == ("team_services", "scheduler")

    team_family = OperationFamily({"schedules.list": _answer}, frozenset({"schedules.list"}))
    scheduler_family = OperationFamily(
        {"run_context.sign": _answer},
        frozenset({"run_context.sign"}),
        address=SCHEDULER_ADDRESS,
    )
    services = TeamServices(_sessions(), [team_family, scheduler_family])

    assert "schedules.list" in services.served_at("svc.team")
    assert "run_context.sign" not in services.served_at("svc.team")
    assert services.served_at(SCHEDULER_ADDRESS) == frozenset({"run_context.sign"})
    assert services.side_by_side_at(SCHEDULER_ADDRESS) == frozenset({"run_context.sign"})

    sign_frame = SimpleNamespace(frame=SimpleNamespace(kind="run_context.sign"))
    list_frame = SimpleNamespace(frame=SimpleNamespace(kind="schedules.list"))

    assert await services.handle_address(SCHEDULER_ADDRESS, sign_frame) == {"outcome": "ok"}  # type: ignore[arg-type]
    assert await services.handle_address("svc.team", list_frame) == {"outcome": "ok"}  # type: ignore[arg-type]
    with pytest.raises(Unsupported):
        await services.handle_address("svc.team", sign_frame)  # type: ignore[arg-type]
    with pytest.raises(Unsupported):
        await services.handle_address(SCHEDULER_ADDRESS, list_frame)  # type: ignore[arg-type]


def test_owner_vouched_column_vouches_schedule_and_resets_on_owner_change(tmp_path, monkeypatch) -> None:
    """A schedule with owner_vouched=1 needs no embedded gateway token; changing its owner clears the vouch."""

    from custom_agents.schedule_store import ScheduleStore
    from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
    from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType, ScheduleManagementClass
    from storage.access_repository import AccessRepository
    from storage.config import resolve_storage_settings
    from tests.broker_access_support import broker_person_ref, forget_task_context_keys
    from tests.scheduled_run_support import bind_test_scheduler

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "vouched.db")}})
    store = ScheduleStore(storage_settings=storage)
    AccessRepository(storage).replace_people(
        "team_one",
        [
            TeamPersonV1(person_ref=broker_person_ref("usr_ann"), team_id="team_one", person_id="usr_ann", standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=NOW),
            TeamPersonV1(person_ref=broker_person_ref("usr_bob"), team_id="team_one", person_id="usr_bob", standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=NOW),
        ],
        at=NOW,
    )
    sched = bind_test_scheduler(monkeypatch, store=store, team_id="team_one", storage_settings=storage)
    try:
        schedule = ScheduledMessage(
            schedule_id="sched_vouched_1",
            schedule_name="Daily check",
            management_class=ScheduleManagementClass.USER,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check."),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_ann")),
            interval_ms=86_400_000,
        )
        store.add(schedule, owner_vouched=True)
        assert store.is_owner_vouched("sched_vouched_1") is True
        assert sched.scheduler.renew_permits().issued == ["sched_vouched_1"]
        assert store.run_permit("sched_vouched_1") is not None

        # Saving with a different owner without an explicit vouch clears owner_vouched.
        # Under F2, the unvouched schedule still gets a standing permit carrying the
        # signed "no roles" marker (schedule_unvouched_permit:), never a vouched permit.
        reassigned = schedule.model_copy(deep=True)
        reassigned.payload_meta.gateway_context.canonical_user_id = "usr_bob"
        store.add(reassigned)
        assert store.is_owner_vouched("sched_vouched_1") is False
        result = sched.scheduler.renew_permits()
        assert result.renewed == ["sched_vouched_1"]
        bob_permit = store.run_permit("sched_vouched_1")
        assert bob_permit is not None
        assert bob_permit.bound_to.id == "schedule_unvouched_permit:sched_vouched_1"
        verified_bob = sched.verifier.verify_schedule_permit(bob_permit, team_id="team_one")
        assert (verified_bob.person_id, verified_bob.person_roles, verified_bob.is_unvouched) == ("usr_bob", (), True)
    finally:
        forget_task_context_keys()


def test_team_operations_wires_scheduler_verifier_with_access_repo_and_host_trust(tmp_path) -> None:
    """team_operations passes host (for locked trust_state) and access_repo (PeopleStore) to SchedulerOperations."""

    import threading
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
    from storage.access_repository import AccessRepository
    from team_services.scheduler import SCHEDULER_ADDRESS
    from tests.broker_access_support import (
        broker_person_ref,
        broker_scheduler_identity,
        broker_trust_store,
        signed_task_token,
    )

    config = GatewayInstanceConfig.create(
        instance_name="sched_wire",
        instance_root=str(tmp_path / "instance"),
        team_id="team_one",
        timezone="UTC",
        display_name="Owner",
    )
    config.state.storage.sqlite.path = str(tmp_path / "manager.db")
    config.layout.ensure_directories()
    AccessRepository(config.manager_storage_settings).replace_people(
        "team_one",
        [
            TeamPersonV1(
                person_ref=broker_person_ref("usr_ann"),
                team_id="team_one",
                person_id="usr_ann",
                standing=PersonStanding.ACTIVE,
                roles=("member",),
                changed_at_utc=NOW,
            )
        ],
        at=NOW,
    )
    lock = threading.RLock()
    trust = broker_trust_store()
    fake_host = SimpleNamespace(
        live=SimpleNamespace(identity=broker_scheduler_identity("team_one"), trust=trust),
        trust_state=lambda: (trust, lock),
        nc=None,
        refresh_trust=AsyncMock(),
    )
    families = process.team_operations(config, lambda: datetime.now(timezone.utc), host=fake_host)  # type: ignore[arg-type]
    scheduler_families = [f for f in families if f.address == SCHEDULER_ADDRESS]
    assert len(scheduler_families) == 1
    assert set(scheduler_families[0].handlers) == {"run_context.sign"}
    scheduler_ops = scheduler_families[0].handlers["run_context.sign"].__self__
    verifier = scheduler_ops._verifier_fn()
    assert verifier is not None
    token = signed_task_token("usr_ann", team_id="team_one", roles=("member",))
    verified = verifier.verify_root_task(token, team_id="team_one", require_issuer="gateway")
    assert verified.person_id == "usr_ann" and verified.person_roles == ("member",)
