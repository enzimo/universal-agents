"""A paused monitor gives live workers time to publish their next heartbeat."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents.liveness_observation import (
    heartbeat_checks_deferred,
    observe_liveness,
)
from custom_agents import scheduled_runtime
from tests.test_schedule_tools import _LoopAgent


@pytest.mark.parametrize("monotonic_gap", [1.0, 1080.0])
def test_host_and_vm_suspension_allow_a_bounded_heartbeat_refresh(monkeypatch, caplog, monotonic_gap):
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    agent = SimpleNamespace(id="lead", _logger=logging.getLogger("liveness-test"))
    before = datetime(2026, 10, 9, 19, 36, 49, tzinfo=timezone.utc)
    observe_liveness(agent, now=before)
    assert not heartbeat_checks_deferred(agent)

    clock[0] += monotonic_gap
    woke = before + timedelta(minutes=18)
    observe_liveness(agent, now=woke)
    assert heartbeat_checks_deferred(agent)
    assert "event=liveness_monitor_paused" in caplog.text

    clock[0] += 59
    observe_liveness(agent, now=woke + timedelta(seconds=59))
    assert heartbeat_checks_deferred(agent)
    clock[0] += 1
    assert not heartbeat_checks_deferred(agent)


def test_regular_scheduler_checks_and_initial_start_do_not_delay_recovery(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    agent = SimpleNamespace(id="lead", _logger=logging.getLogger("liveness-test"))
    now = datetime.now(timezone.utc)
    observe_liveness(agent, now=now)
    for tick in range(1, 12):
        clock[0] += 60
        observe_liveness(agent, now=now + timedelta(seconds=60 * tick))
        assert not heartbeat_checks_deferred(agent)


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_before", ["takeover", "task_alerts"])
async def test_scheduler_detects_a_pause_during_its_pass(monkeypatch, pause_before):
    """A host can sleep during an await, after the initial scheduler check."""
    agent = _LoopAgent(can_start=False)
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    decisions = []

    async def pause(_agent):
        # Simulate elapsed wall time while monotonic time excludes suspension.
        agent._liveness_observation.checked_at -= timedelta(minutes=18)
        clock[0] += 1

    async def takeover(_agent):
        decisions.append(("takeover", heartbeat_checks_deferred(agent)))

    async def alerts(_agent):
        decisions.append(("task_alerts", heartbeat_checks_deferred(agent)))

    monkeypatch.setattr(scheduled_runtime, "take_over_orphaned_work", takeover)
    monkeypatch.setattr(scheduled_runtime, "report_stuck_tasks", alerts)
    if pause_before == "takeover":
        monkeypatch.setattr(scheduled_runtime, "process_pending_handbacks", pause)
    else:
        monkeypatch.setattr(scheduled_runtime, "report_stuck_schedules", pause)
    await scheduled_runtime._scheduler_pass(agent, {}, asyncio.sleep)
    assert decisions == [("takeover", pause_before == "takeover"), ("task_alerts", True)]
