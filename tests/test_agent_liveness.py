"""One rule decides whether an agent holding saved work is gone, and status rows survive a lead's exit.

Real SQLite launch, status and snapshot stores with the scheduled-worker test
lead (its launcher records starts instead of running threads) and real child
processes standing in for startup agents. Before 2026-10-17 a startup agent was
judged by its heartbeat alone: 30 seconds when another agent of its type
started, 600 for the lead's replacement pass. The exiting lead also deleted
every status row of its team, and stored replayed presence with the time it
arrived. A live startup agent could lose its work and keep running it.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_agents.agent_liveness as agent_liveness
from custom_agents.agent_liveness import (
    OwnerEvidence,
    gone_owner_ids,
    own_process_identity,
    owner_liveness,
    prune_departed_agent_rows,
    prune_departed_agent_rows_when_due,
)
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.orphaned_work_takeover import _orphaned_owners, take_over_orphaned_work
from custom_agents.runtime_recovery import recover_runtime_after_restart
from custom_agents.strands_agent import UniversalAgent
from custom_agents.stuck_tasks import OwnerWork, StuckTasks, stuck_task_settings
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.control_v1 import AgentStatus, AgentStatusMap
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_messages.enums import ControlType, MessageMetaType
from runtime_hosting.blocking_io import BlockingIOLane
from runtime_hosting.process_run import ProcessIdentity, current_process_identity, process_start_ticks
from tests.test_orphaned_work_takeover import held_work, launch_record
from tests.test_schedule_worker_service import lead  # noqa: F401

STARTUP = "agent_startup_worker"
GONE_AFTER = timedelta(seconds=600)


@pytest.fixture
def team(lead):
    lead._agent_state_store = AgentStateStore(
        storage_settings=lead.task_manager.storage_settings, logger=logging.getLogger("test.liveness")
    )
    lead.schedule_maintenance = {}
    lead.same_role_runtime_takeover_enabled = True
    return lead


@pytest.fixture
def live_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield child
    finally:
        child.kill()
        child.wait()


def identity_of(pid: int, *, run_id: str | None = None, hostname: str | None = None) -> str:
    current = current_process_identity()
    return ProcessIdentity(
        run_id if run_id is not None else current.run_id,
        hostname or current.hostname,
        pid,
        process_start_ticks(pid),
    ).text()


def exited_identity() -> str:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return identity_of(child.pid)


def status(agent_id, *, seen_ago=timedelta(0), identity=None, exiting=False, agent_type="Worker"):
    seen = None if seen_ago is None else (datetime.now(timezone.utc) - seen_ago).isoformat()
    return AgentStatus(
        status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
        named_agent_type=agent_type, self_id=agent_id, is_paused=False, name=agent_id,
        team_id="team", agent_role="worker", is_exiting=exiting, last_seen_at=seen,
        process_identity=identity,
    )


def report(team, agent_id, **fields) -> AgentStatus:
    value = status(agent_id, **fields)
    team._agent_state_store.upsert(value)
    return value


def stuck_since(team, owner_id):
    [row] = [r for r in team.task_manager.runtime_state_store.peek_task_runtime_by_team("team") if r.owner_agent_id == owner_id]
    owner = OwnerWork(owner_id, row.owner_named_agent_type, None, decode_runtime_snapshot(row.runtime_state_json))
    return StuckTasks(team, stuck_task_settings(team)).owner_gone_since(owner, datetime.now(timezone.utc))


def replacements(team):
    return [start for start in team.starts if start.named_agent_type == "Worker"]


# -- a startup agent is judged by its own process ----------------------------------


@pytest.mark.asyncio
async def test_a_live_startup_agent_with_an_old_heartbeat_keeps_its_work(team, live_process):
    """Host sleep left the heartbeat 20 minutes old; the process still runs."""
    held_work(team, owner=STARTUP, signed=True)
    report(team, STARTUP, seen_ago=timedelta(minutes=20), identity=identity_of(live_process.pid))
    assert owner_liveness(team, STARTUP).evidence is OwnerEvidence.PROCESS_ALIVE
    assert await gone_owner_ids(team, [STARTUP], purpose="test") == []
    assert await take_over_orphaned_work(team, clock=1.0) == 0
    assert stuck_since(team, STARTUP) is None


@pytest.mark.asyncio
async def test_an_exited_startup_agent_is_taken_over_at_once(team):
    held_work(team, owner=STARTUP, signed=True)
    report(team, STARTUP, identity=exited_identity())
    decision = owner_liveness(team, STARTUP)
    assert (decision.evidence, decision.gone) == (OwnerEvidence.PROCESS_GONE, True)
    assert await gone_owner_ids(team, [STARTUP], purpose="test") == [STARTUP]
    assert await take_over_orphaned_work(team, clock=1.0) == 1


def test_a_startup_agent_from_an_earlier_run_is_gone(team, live_process, monkeypatch):
    current = current_process_identity()
    now_run = ProcessIdentity("run_now", current.hostname, os.getpid(), current.start_ticks)
    monkeypatch.setattr(agent_liveness, "current_process_identity", lambda: now_run)
    monkeypatch.setattr(agent_liveness, "current_run_proven", lambda: True)
    report(team, STARTUP, identity=identity_of(live_process.pid, run_id="run_before"))
    assert owner_liveness(team, STARTUP).evidence is OwnerEvidence.PROCESS_GONE


def test_unknown_process_evidence_keeps_the_owner(team, live_process):
    report(team, STARTUP, seen_ago=timedelta(days=1), identity=identity_of(live_process.pid, hostname="another-host"))
    decision = owner_liveness(team, STARTUP)
    assert (decision.evidence, decision.gone) == (OwnerEvidence.PROCESS_UNKNOWN, False)


def test_a_status_naming_this_process_is_judged_by_its_heartbeat(team):
    """The process says nothing about another agent inside it (tests, in-process workers)."""
    report(team, STARTUP, seen_ago=timedelta(minutes=20), identity=current_process_identity().text())
    assert owner_liveness(team, STARTUP).evidence is OwnerEvidence.HEARTBEAT_STALE
    report(team, STARTUP, identity=current_process_identity().text())
    assert owner_liveness(team, STARTUP).evidence is OwnerEvidence.HEARTBEAT_FRESH


def test_only_agents_that_are_their_own_process_publish_it(team):
    assert own_process_identity(SimpleNamespace(_worker_launch=None)) == current_process_identity().text()
    assert own_process_identity(SimpleNamespace(_worker_launch=object())) is None
    stored = report(team, STARTUP, identity="run1|-|host|1|2")
    assert team._agent_state_store.get_by_id(STARTUP).process_identity == stored.process_identity


# -- one heartbeat threshold everywhere ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("silent_for", "gone"), [(timedelta(seconds=35), False), (GONE_AFTER + timedelta(seconds=1), True)])
async def test_every_caller_uses_one_heartbeat_threshold(team, silent_for, gone):
    """35 seconds of silence used to cost a startup agent its work at another agent's start."""
    held_work(team, owner=STARTUP, signed=True)
    report(team, STARTUP, seen_ago=silent_for)
    now = datetime.now(timezone.utc)
    # Startup takeover and schedule coalescing both ask gone_owner_ids.
    assert (await gone_owner_ids(team, [STARTUP], purpose="test") == [STARTUP]) is gone
    assert bool(_orphaned_owners(team, now)) is gone
    assert (stuck_since(team, STARTUP) is not None) is gone


# -- status rows across a lead's exit and restart -------------------------------------


def exiting_agent(team, agent_id, *, is_lead):
    """The parts of an agent its exit cleanup touches."""
    agent = SimpleNamespace(
        id=agent_id,
        team_id="team",
        is_team_lead=is_lead,
        _logger=logging.getLogger("test.liveness.exit"),
        _agent_state_store=team._agent_state_store,
        _agent_state_io=BlockingIOLane(),
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _local_schedule_task=None,
        _presence_task=None,
        _scheduler_task=None,
    )
    agent.get_self_status = lambda is_exiting=False, **_: status(agent_id, exiting=is_exiting, agent_type="TeamArchitect")
    agent._refresh_self_agent_state = partial(UniversalAgent._refresh_self_agent_state, agent)
    agent.set_exit_flag = agent._should_exit.set
    return agent


async def run_exit_cleanup(agent, monkeypatch):
    monkeypatch.setattr("custom_agents.strands_agent._publish_exit_presence", AsyncMock())
    await UniversalAgent._cleanup_nats_runtime_on_exit(agent, AsyncMock())


@pytest.mark.asyncio
async def test_lead_exit_keeps_other_agents_rows(team, live_process, monkeypatch):
    startup = report(team, STARTUP, identity=identity_of(live_process.pid))
    report(team, "lead", agent_type="TeamArchitect")
    await run_exit_cleanup(exiting_agent(team, "lead", is_lead=True), monkeypatch)
    assert team._agent_state_store.get_by_id(STARTUP) == startup
    assert team._agent_state_store.get_by_id("lead").is_exiting is True


@pytest.mark.asyncio
async def test_signal_exit_marks_own_row_exiting(team, monkeypatch):
    worker = exiting_agent(team, "agent_worker", is_lead=False)
    report(team, "agent_worker")
    # exit_agent no longer writes the row; the cleanup that follows any exit does.
    await UniversalAgent.exit_agent(worker)
    assert team._agent_state_store.get_by_id("agent_worker").is_exiting is False
    worker.set_exit_flag()  # what a SIGTERM does
    await run_exit_cleanup(worker, monkeypatch)
    assert team._agent_state_store.get_by_id("agent_worker").is_exiting is True


def new_lead(team):
    """The parts of a lead that store received presence."""
    team.known_agents = AgentStatusMap()
    team._agent_id_by_name = {}
    team._resolve_pending_agent_status_probes = lambda *args: None
    return team


async def receive(team, received: AgentStatus):
    await UniversalAgent.handle_agent_status_message(
        team, agent_status=received.model_copy(), msg_header=MessageHeader(from_agent_id=received.self_id), msg_meta=MessageMeta()
    )


@pytest.mark.asyncio
async def test_new_lead_sees_startup_worker_alive_after_manager_restart(team, live_process, monkeypatch):
    held_work(team, owner=STARTUP, signed=True)
    startup = report(team, STARTUP, seen_ago=timedelta(seconds=15), identity=identity_of(live_process.pid))
    await run_exit_cleanup(exiting_agent(team, "old_lead", is_lead=True), monkeypatch)
    # The new lead's presence subscription replays the startup agent's last status.
    await receive(new_lead(team), startup)
    assert team._agent_state_store.get_by_id(STARTUP).last_seen_at == startup.last_seen_at
    assert await take_over_orphaned_work(team, clock=1.0) == 0
    # A pool worker of the same type starting now leaves the work where it is.
    pool_worker = SimpleNamespace(id="agent_pool", _logger=team._logger, task_manager=team.task_manager,
                                  _agent_state_store=team._agent_state_store, schedule_maintenance={},
                                  team_id="team", _local_team_runtime=team._local_team_runtime)
    assert await gone_owner_ids(pool_worker, [STARTUP], purpose="test") == []
    assert team.task_manager.runtime_state_store.load_task_runtime_by_owner(STARTUP)


@pytest.mark.asyncio
async def test_exited_agents_taken_over_immediately_after_full_restart(team):
    """A graceful restart leaves each old agent's own row marked exiting."""
    held_work(team, owner="old_startup", signed=True)
    held_work(team, owner="old_worker", signed=True)
    report(team, "old_startup", exiting=True, identity=exited_identity())
    report(team, "old_worker", exiting=True)
    assert await gone_owner_ids(team, ["old_startup", "old_worker"], purpose="test") == ["old_startup", "old_worker"]


def test_prune_removes_rows_unseen_past_gone_after(team, live_process):
    old = GONE_AFTER + timedelta(seconds=100)
    report(team, "silent", seen_ago=old)
    report(team, "exited", seen_ago=old, exiting=True)
    report(team, "ended_worker", seen_ago=old)
    launch_record(team, AgentLaunchState.FAILED, agent_id="ended_worker")
    report(team, "live_startup", seen_ago=old, identity=identity_of(live_process.pid))
    report(team, "recent", seen_ago=timedelta(seconds=10), exiting=True)
    report(team, "lead", seen_ago=old)
    assert prune_departed_agent_rows(team) == 3
    remaining = {s.self_id for s in team._agent_state_store.query({"team_id": "team"})}
    assert remaining == {"live_startup", "recent", "lead"}


def test_prune_skipped_while_heartbeat_checks_deferred(team, monkeypatch):
    report(team, "silent", seen_ago=GONE_AFTER * 2)
    monkeypatch.setattr(agent_liveness, "heartbeat_checks_deferred", lambda agent: True)
    assert prune_departed_agent_rows(team) == 0
    assert team._agent_state_store.get_by_id("silent") is not None


def test_prune_keeps_row_refreshed_after_read(team, monkeypatch):
    report(team, "slow", seen_ago=GONE_AFTER * 2)
    decide = agent_liveness.owner_liveness

    def reports_in_meanwhile(agent, agent_id, **kwargs):
        decision = decide(agent, agent_id, **kwargs)
        report(team, agent_id)  # the agent writes a fresh heartbeat during the check
        return decision

    monkeypatch.setattr(agent_liveness, "owner_liveness", reports_in_meanwhile)
    assert prune_departed_agent_rows(team) == 0
    assert team._agent_state_store.get_by_id("slow") is not None


@pytest.mark.asyncio
async def test_the_lead_prunes_on_its_own_timer(team):
    report(team, "silent", seen_ago=GONE_AFTER * 2)
    assert await prune_departed_agent_rows_when_due(team, clock=100.0) == 1
    report(team, "silent", seen_ago=GONE_AFTER * 2)
    assert await prune_departed_agent_rows_when_due(team, clock=130.0) == 0
    assert await prune_departed_agent_rows_when_due(team, clock=161.0) == 1


@pytest.mark.asyncio
async def test_lead_stores_sender_last_seen(team, caplog):
    sent = status(STARTUP, seen_ago=timedelta(seconds=45))
    await receive(new_lead(team), sent)
    assert team._agent_state_store.get_by_id(STARTUP).last_seen_at == sent.last_seen_at
    # A status without its time is stored with the arrival time, and that is logged.
    with caplog.at_level(logging.WARNING):
        await receive(team, status("agent_untimed", seen_ago=None))
    assert team._agent_state_store.get_by_id("agent_untimed").last_seen_at is not None
    assert "event=agent_status_without_sent_time" in caplog.text


@pytest.mark.asyncio
async def test_replayed_snapshot_cannot_revive_dead_agent(team):
    held_work(team, owner="crashed_startup", signed=True)
    held_work(team, owner="crashed_worker", signed=True)
    lead = new_lead(team)
    await receive(lead, status("crashed_startup", identity=exited_identity()))
    await receive(lead, status("crashed_worker", seen_ago=GONE_AFTER + timedelta(seconds=5)))
    assert owner_liveness(team, "crashed_startup").evidence is OwnerEvidence.PROCESS_GONE
    assert owner_liveness(team, "crashed_worker").evidence is OwnerEvidence.HEARTBEAT_STALE


# -- startup takeover that loses a race ---------------------------------------------


@pytest.mark.asyncio
async def test_startup_takeover_logs_a_lost_race_and_continues(team, monkeypatch, caplog):
    """Two agents of one type start together; the one that loses must keep starting."""
    held_work(team, owner="gone_owner", signed=True)
    rival = TaskManager(db_path=team.task_manager._db_path)
    rival.set_owner_identity(agent_id="rival", agent_name="rival", team_id="team", named_agent_type="Worker")
    starting = TaskManager(db_path=team.task_manager._db_path)
    starting.set_owner_identity(agent_id="starting", agent_name="starting", team_id="team", named_agent_type="Worker")
    store = starting.runtime_state_store
    read = store.load_task_runtime_by_owner

    def rival_commits_after_this_read(owner_agent_id):
        rows = read(owner_agent_id)
        if owner_agent_id == "gone_owner":
            rival.restore_runtime_state_for_owner_ids(["gone_owner"])
        return rows

    monkeypatch.setattr(store, "load_task_runtime_by_owner", rival_commits_after_this_read)
    agent = SimpleNamespace(
        id="starting", team_id="team", named_agent_type="Worker", same_role_runtime_takeover_enabled=True,
        task_manager=starting, _agent_state_store=team._agent_state_store, schedule_maintenance={},
        _local_team_runtime=None, _logger=logging.getLogger("test.liveness.race"),
    )
    agent.deferred_inbox = UniversalAgent.deferred_inbox.fget(SimpleNamespace(
        _deferred_inbox=None, id="starting", name="starting", team_id="team", named_agent_type="Worker",
        task_manager=starting, _logger=agent._logger, deferred_message_warning_threshold=0, max_deferred_messages=10,
    ))
    with caplog.at_level(logging.WARNING):
        await recover_runtime_after_restart(agent)
    assert "event=role_takeover_lost_race" in caplog.text
    assert starting.list_task_infos() == []
    assert [info.task.task_id for info in rival.list_task_infos()] == ["work-1"]
