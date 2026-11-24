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
