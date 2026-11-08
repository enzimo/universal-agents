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

    async def failed(*args):
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

    async def serve_team(_host, _team, _stop, _families) -> None:
        raise lost

    monkeypatch.setattr(process, "NatsTransportAdapter", _Adapter)
    monkeypatch.setattr(process, "MessageHost", lambda *_args, **_kwargs: SimpleNamespace(clock=None))
    monkeypatch.setattr(process, "team_operations", lambda _config, _clock: [])
    monkeypatch.setattr(process, "serve_team", serve_team)
    transport = SimpleNamespace(nats_servers=["nats://127.0.0.1:4222"], nats_url=None, subject_prefix="ua",
                                nats_user=None, nats_password=None, credentials_file=None)
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
    return SimpleNamespace(open=_answer, renew=_answer, close=_answer, maintain=AsyncMock())


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
    monkeypatch.setattr(process, "SingleInstanceLock", SimpleNamespace(on_connection=lambda *_args: _Lock(events)))
    monkeypatch.setattr(process, "SessionService", lambda *_args: _sessions())
    monkeypatch.setattr(process, "mark_serving", lambda *_args: serving.set())
    stop = asyncio.Event()

    served = asyncio.create_task(process.serve_team(host, "team_one", stop, []))  # type: ignore[arg-type]
    await asyncio.wait_for(serving.wait(), timeout=5)
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
