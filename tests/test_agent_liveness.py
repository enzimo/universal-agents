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
from uuid import uuid4

import pytest

import custom_agents.agent_liveness as agent_liveness
from custom_agents.agent_liveness import (
    LivenessPurpose,
    OwnerEvidence,
    gone_owner_ids,
    own_process_identity,
    owner_liveness,
    parse_utc,
    prune_departed_agent_rows,
    prune_departed_agent_rows_when_due,
    received_status_time,
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
from storage.agent_state_repository import create_agent_state_repository
from storage.config import resolve_storage_settings
from tests.test_orphaned_work_takeover import held_work, launch_record
from tests.test_schedule_worker_service import lead  # noqa: F401

STARTUP = "agent_startup_worker"
TEST = LivenessPurpose.STARTUP_TAKEOVER
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
    assert owner_liveness(team, STARTUP, purpose=TEST).evidence is OwnerEvidence.PROCESS_ALIVE
    assert await gone_owner_ids(team, [STARTUP], purpose=TEST) == []
    assert await take_over_orphaned_work(team, clock=1.0) == 0
    assert stuck_since(team, STARTUP) is None


@pytest.mark.asyncio
async def test_an_exited_startup_agent_is_taken_over_at_once(team):
    held_work(team, owner=STARTUP, signed=True)
    report(team, STARTUP, identity=exited_identity())
    decision = owner_liveness(team, STARTUP, purpose=TEST)
    assert (decision.evidence, decision.gone) == (OwnerEvidence.PROCESS_GONE, True)
    assert await gone_owner_ids(team, [STARTUP], purpose=TEST) == [STARTUP]
    assert await take_over_orphaned_work(team, clock=1.0) == 1


def test_a_startup_agent_from_an_earlier_run_is_gone(team, live_process, monkeypatch):
    current = current_process_identity()
    now_run = ProcessIdentity("run_now", current.hostname, os.getpid(), current.start_ticks)
    monkeypatch.setattr(agent_liveness, "current_process_identity", lambda: now_run)
    monkeypatch.setattr(agent_liveness, "current_run_proven", lambda: True)
    report(team, STARTUP, identity=identity_of(live_process.pid, run_id="run_before"))
    assert owner_liveness(team, STARTUP, purpose=TEST).evidence is OwnerEvidence.PROCESS_GONE


@pytest.mark.asyncio
async def test_an_unchecked_process_falls_back_to_the_heartbeat(team, live_process, caplog):
    """An unproven run and a recreated container: the identity names a host this process cannot inspect.

    Until review on 2026-10-17 such an owner kept its work for good: no
    replacement, no notice, no pruning, and schedule coalescing folded every
    new fire into its dead run.
    """
    held_work(team, owner=STARTUP, signed=True)
    elsewhere = identity_of(live_process.pid, hostname="old-container")
    report(team, STARTUP, seen_ago=timedelta(seconds=30), identity=elsewhere)
    with caplog.at_level(logging.INFO):
        fresh = owner_liveness(team, STARTUP, purpose=TEST)
        owner_liveness(team, STARTUP, purpose=TEST)  # the same state is not logged again
    assert (fresh.evidence, fresh.gone, fresh.unchecked_process) == (OwnerEvidence.HEARTBEAT_FRESH, False, elsewhere)
    assert caplog.text.count("event=owner_liveness_unknown") == 1
    assert caplog.text.count("event=owner_liveness_decided") == 1

    report(team, STARTUP, seen_ago=GONE_AFTER + timedelta(seconds=1), identity=elsewhere)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert owner_liveness(team, STARTUP, purpose=TEST).gone
        assert _orphaned_owners(team, datetime.now(timezone.utc))
        assert stuck_since(team, STARTUP) is not None
        assert await gone_owner_ids(team, [STARTUP], purpose=LivenessPurpose.SCHEDULE_COALESCING) == [STARTUP]
    # The first decision path saw the change to a stale heartbeat, and did not
    # report the unknown process again; each other path reports it once.
    assert "evidence=heartbeat_stale previous_evidence=heartbeat_fresh" in caplog.text
    assert caplog.text.count("event=owner_liveness_unknown") == 3
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert await take_over_orphaned_work(team, clock=1.0) == 1
        assert stuck_since(team, STARTUP) is not None
    assert "event=owner_liveness" not in caplog.text  # unchanged decisions are not logged again
    assert prune_departed_agent_rows(team) == 1


def test_the_process_rule_applies_only_to_another_process(team, live_process):
    """The process says nothing about another agent inside it (tests, in-process workers)."""
    silent = GONE_AFTER * 2
    report(team, STARTUP, seen_ago=silent, identity=identity_of(live_process.pid))
    assert owner_liveness(team, STARTUP, purpose=TEST).evidence is OwnerEvidence.PROCESS_ALIVE
    report(team, STARTUP, seen_ago=silent, identity=current_process_identity().text())
    assert owner_liveness(team, STARTUP, purpose=TEST).evidence is OwnerEvidence.HEARTBEAT_STALE
    report(team, STARTUP, identity=current_process_identity().text())
    assert owner_liveness(team, STARTUP, purpose=TEST).evidence is OwnerEvidence.HEARTBEAT_FRESH


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
    assert (await gone_owner_ids(team, [STARTUP], purpose=TEST) == [STARTUP]) is gone
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
        _pending_work_timers=set(),
        _owned_tasks=set(),
        _local_schedule_task=None,
        _presence_task=None,
        _scheduler_task=None,
    )
    agent.get_self_status = lambda is_exiting=False, **_: status(agent_id, exiting=is_exiting, agent_type="TeamArchitect")
    agent._refresh_self_agent_state = partial(UniversalAgent._refresh_self_agent_state, agent)
    agent._cancel_scheduler_pass = partial(UniversalAgent._cancel_scheduler_pass, agent)
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
    assert await gone_owner_ids(pool_worker, [STARTUP], purpose=TEST) == []
    assert team.task_manager.runtime_state_store.load_task_runtime_by_owner(STARTUP)


@pytest.mark.asyncio
async def test_exited_agents_taken_over_immediately_after_full_restart(team):
    """A graceful restart leaves each old agent's own row marked exiting."""
    held_work(team, owner="old_startup", signed=True)
    held_work(team, owner="old_worker", signed=True)
    report(team, "old_startup", exiting=True, identity=exited_identity())
    report(team, "old_worker", exiting=True)
    assert await gone_owner_ids(team, ["old_startup", "old_worker"], purpose=TEST) == ["old_startup", "old_worker"]


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
    stored = team._agent_state_store.get_by_id(STARTUP).last_seen_at
    assert parse_utc(stored) == parse_utc(sent.last_seen_at)
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
    assert owner_liveness(team, "crashed_startup", purpose=TEST).evidence is OwnerEvidence.PROCESS_GONE
    assert owner_liveness(team, "crashed_worker", purpose=TEST).evidence is OwnerEvidence.HEARTBEAT_STALE


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


# -- the lead bounds and orders the times it stores ------------------------------


@pytest.mark.asyncio
async def test_a_sender_clock_ahead_is_capped_and_logged_when_it_starts_and_ends(team, caplog):
    """A sender two days ahead stayed fresh and unpruned for two days."""
    lead = new_lead(team)
    with caplog.at_level(logging.INFO):
        await receive(lead, status("skewed_worker", seen_ago=-timedelta(days=2)))
        await receive(lead, status("skewed_worker", seen_ago=-timedelta(days=2)))
        stored = parse_utc(team._agent_state_store.get_by_id("skewed_worker").last_seen_at)
        await receive(lead, status("skewed_worker"))
    assert stored <= datetime.now(timezone.utc)
    assert caplog.text.count("event=agent_status_clock_ahead ") == 1
    assert caplog.text.count("event=agent_status_clock_ahead_cleared") == 1
    # The stored time is the arrival, so the agent ages out on the lead's clock.
    arrived = datetime.now(timezone.utc) - GONE_AFTER * 2
    capped = received_status_time(lead, status("skewed_worker", seen_ago=-timedelta(days=2)), now=arrived)
    assert parse_utc(capped) == arrived


@pytest.mark.asyncio
async def test_a_delayed_older_status_cannot_overwrite_a_newer_row(team):
    report(team, STARTUP, seen_ago=timedelta(seconds=5), exiting=True)  # the agent's own last write
    await receive(new_lead(team), status(STARTUP, seen_ago=timedelta(seconds=25)))  # presence sent earlier
    row = team._agent_state_store.get_by_id(STARTUP)
    assert row.is_exiting is True
    assert datetime.now(timezone.utc) - parse_utc(row.last_seen_at) < timedelta(seconds=20)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def status_repository(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL status rows")
        config = {"backend": "postgres", "postgres": {"dsn": dsn}}
    else:
        config = {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "status.db")}}
    return create_agent_state_repository(resolve_storage_settings(config))


def test_a_received_status_is_written_only_when_newer(status_repository):
    """One statement on both databases, whatever form the stored time text takes."""
    agent_id = f"agent_{uuid4().hex}"

    def at(text, *, exiting=False):
        return status(agent_id, seen_ago=None, exiting=exiting).model_copy(update={"last_seen_at": text})

    try:
        assert status_repository.upsert_if_newer(at("2026-10-17T12:00:05+00:00")) is True
        status_repository.upsert(at("2026-10-17T12:00:10.000001+00:00", exiting=True))
        assert status_repository.upsert_if_newer(at("2026-10-17T12:00:09.999999+00:00")) is False
        assert status_repository.get_by_id(agent_id).is_exiting is True
        assert status_repository.upsert_if_newer(at("2026-10-17T12:00:11Z")) is True
        row = status_repository.get_by_id(agent_id)
        assert parse_utc(row.last_seen_at) == datetime(2026, 10, 17, 12, 0, 11, tzinfo=timezone.utc)
    finally:
        status_repository.remove(agent_id)


# -- process identity stays between agents ----------------------------------------


def test_process_identity_is_not_shown_to_models_or_people(team, monkeypatch):
    """Run ids, host names and process numbers help nobody reading a status."""
    from assistant_gateway.application.delivery_presentation import GatewayDeliveryPresentation
    from custom_messages.contracts.envelope_v1 import MessageEnvelope
    from custom_tools.providers.agent_management import get_known_agents, query_agent_state
    from custom_tools.providers.runtime_settings import get_self_status

    shown = report(team, STARTUP, identity=current_process_identity().text())
    agent = SimpleNamespace(
        _agent_state_store=team._agent_state_store,
        known_agents=AgentStatusMap(agent_map={STARTUP: shown}),
        get_self_status=lambda info_type=None: shown,
    )
    for module in ("agent_management", "runtime_settings"):
        monkeypatch.setattr(f"custom_tools.providers.{module}.get_agent_ref", lambda: agent)
    envelope = MessageEnvelope(msg_content=shown, msg_header=MessageHeader(from_agent_id=STARTUP), msg_meta=MessageMeta())
    outputs = {
        "get_known_agents": get_known_agents(),
        "query_agent_state": query_agent_state(team_id="team"),
        "get_self_status": get_self_status(),
        "chat reply": GatewayDeliveryPresentation.render_envelope(envelope),
    }
    for where, text in outputs.items():
        assert STARTUP in text, where
        assert "process_identity" not in text, where


@pytest.mark.asyncio
async def test_decision_records_keep_only_owners_still_seen(team):
    """The per-owner record behind change-only logging must not grow for good (review F2)."""
    held_work(team, owner="owner_a", signed=True)
    report(team, "owner_a", seen_ago=GONE_AFTER * 2)
    now = datetime.now(timezone.utc)
    assert _orphaned_owners(team, now)
    memory = team._liveness_decisions
    assert (LivenessPurpose.REPLACEMENT_PASS, "", "owner_a") in memory
    team.task_manager.runtime_state_store.clear_task_runtime("owner_a")  # its saved work is gone
    _orphaned_owners(team, now)
    assert (LivenessPurpose.REPLACEMENT_PASS, "", "owner_a") not in memory

    await gone_owner_ids(team, ["owner_b"], purpose=TEST)
    await gone_owner_ids(team, ["owner_c"], purpose=TEST)
    assert {key for key in memory if key[0] is TEST} == {(TEST, "", "owner_c")}

    assert prune_departed_agent_rows(team) == 1  # owner_a's silent row
    assert (LivenessPurpose.STATUS_ROW_PRUNE, "", "owner_a") in memory
    prune_departed_agent_rows(team)
    assert (LivenessPurpose.STATUS_ROW_PRUNE, "", "owner_a") not in memory



@pytest.mark.asyncio
async def test_alternating_schedules_log_an_unchanged_decision_once(team, caplog):
    """Coalescing asks per schedule: one schedule's fire must not forget the other's owners."""
    report(team, "w_sched_a", seen_ago=timedelta(seconds=30))
    report(team, "w_sched_b", seen_ago=timedelta(seconds=30))
    coalescing = LivenessPurpose.SCHEDULE_COALESCING
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            await gone_owner_ids(team, ["w_sched_a"], purpose=coalescing, scope="schedule_a")
            await gone_owner_ids(team, ["w_sched_b"], purpose=coalescing, scope="schedule_b")
    decided = [r.getMessage() for r in caplog.records if "event=owner_liveness_decided" in r.getMessage()]
    assert sum("owner_agent_id=w_sched_a " in line for line in decided) == 1
    assert sum("owner_agent_id=w_sched_b " in line for line in decided) == 1
    # The bound holds per scope: a fire with no unfinished run forgets that schedule's owners.
    await gone_owner_ids(team, [], purpose=coalescing, scope="schedule_a")
    assert {key for key in team._liveness_decisions if key[0] is coalescing} == {(coalescing, "schedule_b", "w_sched_b")}


def test_a_status_row_is_removed_by_the_time_as_sent_or_as_stored(status_repository):
    """Times are stored in one fixed-width form; the conditional delete accepts both."""
    agent_id = f"agent_{uuid4().hex}"
    sent = "2026-10-17T12:00:05+00:00"
    status_repository.upsert(status(agent_id, seen_ago=None).model_copy(update={"last_seen_at": sent}))
    stored = status_repository.get_by_id(agent_id).last_seen_at
    assert stored == "2026-10-17T12:00:05.000000+00:00"
    assert status_repository.remove_unseen(agent_id, "2026-10-17T12:00:04+00:00") is False
    assert status_repository.remove_unseen(agent_id, sent) is True
    status_repository.upsert(status(agent_id, seen_ago=None).model_copy(update={"last_seen_at": sent}))
    assert status_repository.remove_unseen(agent_id, stored) is True
