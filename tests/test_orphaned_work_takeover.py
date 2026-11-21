"""A dead agent's unfinished work is handed to a new agent of its type, and only a dead one's.

Real SQLite launch and snapshot stores with the scheduled-worker test lead (its
launcher records starts instead of running threads). Before 2026-10-04 such
work waited until some unrelated start of that type happened, and takeover
trusted a 30-second heartbeat alone. Startup agents and the one shared rule are
covered in tests/test_agent_liveness.py.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import custom_agents.orphaned_work_takeover as takeover
from custom_agents.agent_instantiation_service import launch_id_for
from custom_agents.agent_liveness import LivenessPurpose, OwnerEvidence, gone_owner_ids, owner_liveness
from custom_agents.agent_state_store import AgentStateStore
from custom_agents.liveness_observation import observe_liveness
from custom_agents.orphaned_work_takeover import (
    MAX_TAKEOVER_ATTEMPTS,
    STEP_GIVE_UP,
    STEP_START,
    STEP_WAIT,
    TAKEOVER_RETRY_DELAYS_SECONDS,
    take_over_orphaned_work,
    takeover_request_id,
    takeover_step,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.agent_launch_repository import AgentLaunchRepository
from storage.agent_launch_repository import LaunchCapacity
from tests.test_schedule_worker_service import lead  # noqa: F401
from tests.test_schedule_worker_transactions import make_launch
from tests.test_stuck_tasks import seen
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

DEAD = "agent_dead_worker"
TEST = LivenessPurpose.STARTUP_TAKEOVER


@pytest.mark.asyncio
@pytest.mark.parametrize("reports_after_wake", [True, False])
async def test_resume_does_not_replace_a_startup_worker_with_an_old_heartbeat(lead, monkeypatch, reports_after_wake):
    use_signed_contexts(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    lead._agent_state_store = AgentStateStore(storage_settings=lead.task_manager.storage_settings)
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    held_work(lead, signed=True)
    before = datetime.now(timezone.utc) - timedelta(minutes=18)
    woke = before + timedelta(minutes=18)
    seen(lead, DEAD, "Worker")
    status = lead._agent_state_store.get_by_id(DEAD)
    status.last_seen_at = before.isoformat()
    lead._agent_state_store.upsert(status)
    observe_liveness(lead, now=before)
    clock[0] += 1
    observe_liveness(lead, now=woke)
    assert await take_over_orphaned_work(lead, now=woke, clock=1.0) == 0
    assert await gone_owner_ids(lead, [DEAD], purpose=TEST) == []
    assert lead.starts == []

    # After the wait, actual heartbeat evidence decides whether to replace it.
    if reports_after_wake:
        status.last_seen_at = (woke + timedelta(seconds=5)).isoformat()
        lead._agent_state_store.upsert(status)
    clock[0] += 60
    observe_liveness(lead, now=woke + timedelta(seconds=60))
    replacements = 0 if reports_after_wake else 1
    assert await take_over_orphaned_work(lead, now=woke + timedelta(seconds=60), clock=62.0) == replacements
    assert len(lead.starts) == replacements
    assert await gone_owner_ids(lead, [DEAD], purpose=TEST) == ([] if reports_after_wake else [DEAD])


@pytest.mark.asyncio
async def test_a_proven_worker_exit_is_replaced_during_resume_grace(lead, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("custom_agents.liveness_observation.time", SimpleNamespace(monotonic=lambda: clock[0]))
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    before = datetime.now(timezone.utc)
    observe_liveness(lead, now=before)
    clock[0] += 1
    observe_liveness(lead, now=before + timedelta(minutes=18))
    assert await take_over_orphaned_work(lead, clock=1.0) == 1
    assert await gone_owner_ids(lead, [DEAD], purpose=TEST) == [DEAD]


def launch_record(lead, state: AgentLaunchState, agent_id: str = DEAD):
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    record, _ = repository.reserve(make_launch(agent_id=agent_id), LaunchCapacity.defaults(), join=False)
    starting = repository.update(record, state=AgentLaunchState.STARTING)
    if state == AgentLaunchState.STARTING:
        return starting
    return repository.update(starting, state=state)


def held_work(
    lead, owner: str = DEAD, state: ProgressState = ProgressState.WAITING, *, signed=False, task_id: str = "work-1"
):
    # One agent holds a task at a time (runtime_task_custody), so two holders need two task ids.
    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Fetch candles")
    snapshot = RuntimeSnapshotV3(
        schema_version=3,
        tasks=[
            TaskSnapshotRecord(
                task_id=task_id, state=state, task=task,
                header=MessageHeader(response_routing_key="gateway"),
                meta=MessageMeta(gateway_context=GatewayContext(
                    canonical_user_id="owner",
                    task_context=context_signer().issue(team_id="team", person_id="owner") if signed else None,
                )),
                owner_person_id="owner" if signed else None,
            )
        ],
    )
    lead.task_manager.runtime_state_store.save_task_runtime(
        expect_existing=False,
        owner_agent_id=owner, owner_agent_name=owner, team_id="team",
        owner_named_agent_type="Worker", runtime_state_json=snapshot.model_dump_json(),
    )


def test_a_launch_record_decides_whether_its_worker_is_gone(lead):
    assert owner_liveness(lead, "agent_without_launch", purpose=TEST).launch is None
    launch_record(lead, AgentLaunchState.FAILED)
    ended = owner_liveness(lead, DEAD, purpose=TEST)
    assert (ended.evidence, ended.gone) == (OwnerEvidence.LAUNCH_ENDED, True)
    launch_record(lead, AgentLaunchState.READY, agent_id="agent_alive")
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    running = owner_liveness(lead, "agent_alive", purpose=TEST)
    assert (running.evidence, running.gone) == (OwnerEvidence.LAUNCH_ACTIVE, False)
    lead._local_team_runtime.inspect_launch = lambda record: "absent"
    absent = owner_liveness(lead, "agent_alive", purpose=TEST)
    assert (absent.evidence, absent.gone) == (OwnerEvidence.LAUNCH_PROCESS_ABSENT, True)


@pytest.mark.asyncio
async def test_a_slow_worker_with_a_stale_heartbeat_keeps_its_work(lead):
    """Takeover used to trust a 30-second heartbeat alone."""
    launch_record(lead, AgentLaunchState.READY, agent_id="agent_slow")
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    lead._agent_state_store = None
    assert await gone_owner_ids(lead, ["agent_slow"], purpose=TEST) == []
    lead._local_team_runtime.inspect_launch = lambda record: "absent"
    assert await gone_owner_ids(lead, ["agent_slow"], purpose=TEST) == ["agent_slow"]


@pytest.mark.asyncio
async def test_the_lead_starts_one_replacement_for_a_dead_workers_work(lead):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    lead.is_team_lead = True
    lead.same_role_runtime_takeover_enabled = True
    lead.schedule_maintenance = {}
    assert await take_over_orphaned_work(lead, clock=1.0) == 1
    (started,) = [s for s in lead.starts if s.named_agent_type == "Worker"]
    assert started.owner_canonical_user_id == "owner"
    assert await take_over_orphaned_work(lead, clock=2.0) == 0  # paced
    assert await take_over_orphaned_work(lead, clock=100.0) == 0  # the same start, not a second one
    assert len([s for s in lead.starts if s.named_agent_type == "Worker"]) == 1


@pytest.mark.asyncio
async def test_a_live_or_finished_owner_gets_no_replacement(lead):
    launch_record(lead, AgentLaunchState.READY)
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    held_work(lead)
    lead.is_team_lead = True
    lead.schedule_maintenance = {}
    assert await take_over_orphaned_work(lead, clock=1.0) == 0
    held_work(lead, state=ProgressState.COMPLETED)
    lead._local_team_runtime.inspect_launch = lambda record: "absent"
    assert await take_over_orphaned_work(lead, clock=100.0) == 0
    assert not [s for s in lead.starts if s.named_agent_type == "Worker"]


@pytest.mark.asyncio
async def test_takeover_can_be_turned_off(lead):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    lead.is_team_lead = True
    lead.same_role_runtime_takeover_enabled = False
    assert await take_over_orphaned_work(lead, clock=1.0) == 0


# -- replacements that fail, are refused or join another start -----------------
# Review of 2026-11-20 (FAIL-2): the replacement's start id was fixed, so a
# replacement that crashed before it was ready was found again on every later
# pass and nothing else was started until its record was pruned (~30 days).

CRASH = "crashed_before_ready: BadRequestError (NATS 10026: maximum consumers limit reached)"


def _takeover_lead(lead):
    lead.is_team_lead = True
    lead.same_role_runtime_takeover_enabled = True
    lead.schedule_maintenance = {}
    return lead


def _worker_starts(lead):
    return [start for start in lead.starts if start.named_agent_type == "Worker"]


def _attempt(lead, attempt: int, owner: str = DEAD, person: str = "owner"):
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    return repository.get(launch_id_for("team", person, takeover_request_id(owner, attempt)), "team")


def _heartbeat(lead, agent_id: str, when: datetime) -> None:
    """A started worker's last heartbeat (it reports no process of its own)."""

    from custom_messages.contracts.control_v1 import AgentStatus, ControlType, MessageMetaType as MetaType

    AgentStateStore(storage_settings=lead.task_manager.storage_settings).upsert(
        AgentStatus(
            status_type=MetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
            named_agent_type="Worker", self_id=agent_id, is_paused=False, name=agent_id,
            team_id="team", agent_role="worker", is_exiting=False,
            last_seen_at=when.isoformat(), process_identity=None,
        )
    )


async def _run_series_to_give_up(lead, owner: str, start: datetime, clock: float) -> float:
    """Five replacements for ``owner``'s series, each crashing 5 s after its start; returns the clock."""

    for attempt, minute in enumerate([0, 2, 8, 24, 85], start=1):
        now = start + timedelta(minutes=minute)
        clock += 100.0
        assert await take_over_orphaned_work(lead, now=now, clock=clock) == 1
        _end(lead, attempt, owner=owner, now=now + timedelta(seconds=5))
    return clock


def _saved_at(lead, owner: str, when: datetime) -> None:
    """Set when the owner last saved its work (saves stamp the current time)."""
    with sqlite3.connect(lead.task_manager._db_path) as connection:
        changed = connection.execute(
            "UPDATE active_task_runtime SET updated_at = ? WHERE owner_agent_id = ?", (when.isoformat(), owner)
        ).rowcount
    assert changed == 1


def _end(
    lead,
    attempt: int,
    state: AgentLaunchState = AgentLaunchState.FAILED,
    reason: str | None = CRASH,
    owner: str = DEAD,
    now: datetime | None = None,
):
    AgentLaunchRepository(lead.task_manager.storage_settings).update(
        _attempt(lead, attempt, owner), state=state, reason=reason, now=now
    )


def _later(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _lines(caplog, event: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if f"event={event} " in record.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [AgentLaunchState.FAILED, AgentLaunchState.RETIRED])
async def test_a_replacement_that_failed_or_retired_is_followed_by_another_after_its_delay(lead, caplog, ended):
    """Probe test_probe_takeover_after_failed_replacement.py: no second replacement ever started."""

    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    assert await take_over_orphaned_work(lead, clock=1.0) == 1
    assert _attempt(lead, 1).state == AgentLaunchState.STARTING
    _end(lead, 1, ended, CRASH if ended == AgentLaunchState.FAILED else None)

    # Still within the first delay: nothing new.
    assert await take_over_orphaned_work(lead, now=_later(30), clock=100.0) == 0
    with caplog.at_level(logging.WARNING):
        assert await take_over_orphaned_work(lead, now=_later(TAKEOVER_RETRY_DELAYS_SECONDS[0] + 5), clock=200.0) == 1
    assert len(_worker_starts(lead)) == 2
    assert _attempt(lead, 2).state == AgentLaunchState.STARTING
    line = _lines(caplog, "orphaned_work_takeover")[-1]
    assert f"owner_agent_id={DEAD} agent_type=Worker tasks=1 task_ids=['work-1'] attempt=2" in line
    if ended == AgentLaunchState.FAILED:
        assert f"previous_reason={CRASH}" in line


@pytest.mark.asyncio
async def test_the_lead_gives_up_after_the_last_attempt_and_says_so_once(lead, caplog):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    long_after = _later(timedelta(days=1).total_seconds())
    for attempt in range(1, MAX_TAKEOVER_ATTEMPTS + 1):
        assert await take_over_orphaned_work(lead, now=long_after, clock=100.0 * attempt) == 1
        _end(lead, attempt)
    with caplog.at_level(logging.ERROR):
        assert await take_over_orphaned_work(lead, now=long_after, clock=1000.0) == 0
        assert await take_over_orphaned_work(lead, now=long_after, clock=1100.0) == 0
    assert len(_worker_starts(lead)) == MAX_TAKEOVER_ATTEMPTS
    (gave_up,) = _lines(caplog, "orphaned_work_takeover_gave_up")
    assert (
        f"owner_agent_id={DEAD} agent_type=Worker task_ids=['work-1'] series_owner_agent_id={DEAD} "
        f"attempts={MAX_TAKEOVER_ATTEMPTS} last_reason={CRASH}"
    ) in gave_up


@pytest.mark.asyncio
async def test_a_refusal_without_a_start_record_is_paced_then_tried_hourly_until_a_start_works(
    lead, monkeypatch, caplog
):
    """Refused before any record exists (here: the type is gone for a while; likewise a limit).

    It used to be tried every pass; it is paced like an attempt, then tried
    hourly and never given up, since a limit frees again (review of 2026-11-20).
    """

    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    definition = lead.definitions.pop("Worker")
    tries = []
    real_launch = takeover._launch_takeover

    def counted(agent, owner, attempt):
        tries.append(attempt)
        return real_launch(agent, owner, attempt)

    monkeypatch.setattr(takeover, "_launch_takeover", counted)
    clock = 1.0
    with caplog.at_level(logging.INFO):
        for _ in range(150):  # a pass every 61 s of the lead's monotonic clock, about 150 minutes
            await take_over_orphaned_work(lead, clock=clock)
            clock += 61.0
        # Tries at about 0, 1, 6, 21, 81 and 141 minutes: the delays, then hourly.
        assert tries == [1] * 6
        assert _worker_starts(lead) == [] and _attempt(lead, 1) is None
        lead.definitions["Worker"] = definition
        for _ in range(61):  # the next hourly try starts the replacement
            await take_over_orphaned_work(lead, clock=clock)
            clock += 61.0
    assert len(tries) == 7 and len(_worker_starts(lead)) == 1
    assert _attempt(lead, 1).state == AgentLaunchState.STARTING
    assert lead._orphaned_work_takeovers[DEAD].refusals == 0  # a start resets the count
    (refused,) = _lines(caplog, "orphaned_work_takeover_refused")
    assert f"owner_agent_id={DEAD} agent_type=Worker task_ids=['work-1'] reason=agent_definition_unavailable" in refused
    assert _lines(caplog, "orphaned_work_takeover_gave_up") == []


def _fail_running_replacements(lead, now: datetime) -> None:
    """Every replacement still starting crashes (``now``: when, for the delays)."""
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    for record in repository.list("team"):
        if record.state == AgentLaunchState.STARTING:
            repository.update(record, state=AgentLaunchState.FAILED, reason=CRASH, now=now)


@pytest.mark.asyncio
async def test_dead_workers_of_one_type_and_person_share_one_paced_series_of_replacements(lead, caplog):
    """Run per dead owner, four dead workers started a replacement every minute (review of 2026-11-20)."""

    owners = [DEAD, "agent_dead_worker_2", "agent_dead_worker_3", "agent_dead_worker_4"]
    for index, owner in enumerate(owners):
        launch_record(lead, AgentLaunchState.FAILED, agent_id=owner)
        held_work(lead, owner=owner, task_id=f"work-{index + 1}")
    _takeover_lead(lead)
    base = datetime.now(timezone.utc)
    started_at = []
    with caplog.at_level(logging.INFO):
        for minute in range(100):  # a pass a minute; each replacement crashes 5 s after it starts
            now = base + timedelta(minutes=minute)
            if await take_over_orphaned_work(lead, now=now, clock=1.0 + 60.0 * minute):
                started_at.append(minute)
            _fail_running_replacements(lead, now + timedelta(seconds=5))
        # Three days later the owners that waited behind the series get no new start.
        assert await take_over_orphaned_work(lead, now=base + timedelta(days=3), clock=1e6) == 0
    # 1, 5, 15 and 60 minutes after each crash, then give up: five starts in all.
    assert started_at == [0, 2, 8, 24, 85]
    (leader,) = [owner for owner in owners if _attempt(lead, 1, owner) is not None]
    waiting = _lines(caplog, "orphaned_work_takeover_waiting")
    assert len(waiting) == 3 and all(f"for_owner_agent_id={leader}" in line for line in waiting)
    # The give-up names every owner it covers, each with its own work.
    gave_up = _lines(caplog, "orphaned_work_takeover_gave_up")
    assert len(gave_up) == 4
    for index, owner in enumerate(owners):
        assert any(
            f"owner_agent_id={owner} agent_type=Worker task_ids=['work-{index + 1}'] "
            f"series_owner_agent_id={leader} attempts={MAX_TAKEOVER_ATTEMPTS} last_reason={CRASH}" in line
            for line in gave_up
        )


@pytest.mark.asyncio
async def test_a_worker_that_dies_after_a_series_gave_up_leads_a_new_one(lead, caplog):
    """A give-up stopped replacements for later dead workers of the type for ~30 days (review of 2026-11-20)."""

    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    base = datetime.now(timezone.utc)
    clock = await _run_series_to_give_up(lead, DEAD, base, 0.0)
    later = base + timedelta(days=3)
    assert await take_over_orphaned_work(lead, now=later, clock=clock + 100) == 0  # given up
    # Three days later the type starts again, and another of its workers dies holding work.
    newcomer = "agent_dead_worker_later"
    launch_record(lead, AgentLaunchState.FAILED, agent_id=newcomer)
    held_work(lead, owner=newcomer, task_id="work-2")
    _saved_at(lead, newcomer, later)
    with caplog.at_level(logging.INFO):
        clock = await _run_series_to_give_up(lead, newcomer, later + timedelta(minutes=1), clock + 200)
        (waiting,) = _lines(caplog, "orphaned_work_takeover_waiting")
        assert f"owner_agent_id={DEAD} " in waiting and f"for_owner_agent_id={newcomer}" in waiting
        # The newcomer's series gave up too: each owner is told once, naming it, and nothing starts.
        for hours in (3, 30):
            assert await take_over_orphaned_work(lead, now=later + timedelta(hours=hours), clock=clock + 100 * hours) == 0
    second_give_ups = [line for line in _lines(caplog, "orphaned_work_takeover_gave_up") if f"series_owner_agent_id={newcomer}" in line]
    assert sorted(line.split("owner_agent_id=")[1].split()[0] for line in second_give_ups) == sorted([DEAD, newcomer])
    assert len(_worker_starts(lead)) == 2 * MAX_TAKEOVER_ATTEMPTS


@pytest.mark.asyncio
async def test_a_worker_alive_after_a_give_up_is_replaced_though_its_last_save_was_earlier(lead, caplog):
    """Saves happen only when a task changes: a worker waiting on an approval saves nothing for days."""

    busy = "agent_busy_worker"
    busy_record = launch_record(lead, AgentLaunchState.READY, agent_id=busy)  # alive (inspect: starting)
    held_work(lead, owner=busy, task_id="work-0")
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    base = datetime.now(timezone.utc)
    _saved_at(lead, busy, base + timedelta(minutes=30))
    clock = await _run_series_to_give_up(lead, DEAD, base, 0.0)
    # Three days later the busy worker dies; it heartbeat until then.
    died = base + timedelta(days=3)
    _heartbeat(lead, busy, died - timedelta(seconds=20))
    AgentLaunchRepository(lead.task_manager.storage_settings).update(busy_record, state=AgentLaunchState.FAILED, now=died)
    with caplog.at_level(logging.INFO):
        assert await take_over_orphaned_work(lead, now=died + timedelta(minutes=1), clock=clock + 100) == 1
    assert _attempt(lead, 1, busy).state == AgentLaunchState.STARTING
    assert not [line for line in _lines(caplog, "orphaned_work_takeover_gave_up") if f"_gave_up owner_agent_id={busy} " in line]


@pytest.mark.asyncio
async def test_a_series_begun_after_a_give_up_keeps_leading_once_its_status_row_is_pruned(lead, caplog):
    """Its heartbeat was its only proof of life, and the lead prunes the row ~10 minutes after it died."""

    from custom_agents.agent_liveness import prune_departed_agent_rows

    busy = "agent_busy_worker"
    busy_record = launch_record(lead, AgentLaunchState.READY, agent_id=busy)
    held_work(lead, owner=busy, task_id="work-0")
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    base = datetime.now(timezone.utc) - timedelta(days=4)
    _saved_at(lead, busy, base + timedelta(minutes=30))
    clock = await _run_series_to_give_up(lead, DEAD, base, 0.0)
    died = base + timedelta(days=3)
    _heartbeat(lead, busy, died - timedelta(seconds=20))
    AgentLaunchRepository(lead.task_manager.storage_settings).update(busy_record, state=AgentLaunchState.FAILED, now=died)
    with caplog.at_level(logging.INFO):
        # Its own five attempts, each crashing 5 s after its start; the lead's
        # status-row pruning removes its heartbeat between the third and fourth.
        for attempt, minute in enumerate([1, 3, 9, 25, 90], start=1):
            if attempt == 4:
                assert prune_departed_agent_rows(lead, now=died + timedelta(minutes=11)) == 1
                assert AgentStateStore(storage_settings=lead.task_manager.storage_settings).get_by_id(busy) is None
            now = died + timedelta(minutes=minute)
            clock += 100.0
            assert await take_over_orphaned_work(lead, now=now, clock=clock) == 1, attempt
            _end(lead, attempt, owner=busy, now=now + timedelta(seconds=5))
        assert await take_over_orphaned_work(lead, now=died + timedelta(hours=30), clock=clock + 100) == 0
    # Its own series gave up, and it covers the first dead owner's work too.
    gave_up = _lines(caplog, "orphaned_work_takeover_gave_up")
    assert sorted(line.split("_gave_up owner_agent_id=")[1].split()[0] for line in gave_up) == sorted([busy, DEAD])
    assert all(f"series_owner_agent_id={busy} attempts={MAX_TAKEOVER_ATTEMPTS}" in line for line in gave_up)


@pytest.mark.asyncio
async def test_a_worker_refused_at_first_keeps_its_proof_of_life_after_its_status_row_is_pruned(lead, caplog):
    """Refusals leave no attempt record, so only the remembered heartbeat proves it was alive after the give-up."""

    from custom_agents.agent_liveness import prune_departed_agent_rows

    busy = "agent_busy_worker"
    busy_record = launch_record(lead, AgentLaunchState.READY, agent_id=busy)
    held_work(lead, owner=busy, task_id="work-0")
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    base = datetime.now(timezone.utc) - timedelta(days=4)
    _saved_at(lead, busy, base + timedelta(minutes=30))
    clock = await _run_series_to_give_up(lead, DEAD, base, 0.0) + 1000.0
    died = base + timedelta(days=3)
    _heartbeat(lead, busy, died - timedelta(seconds=20))
    AgentLaunchRepository(lead.task_manager.storage_settings).update(busy_record, state=AgentLaunchState.FAILED, now=died)
    # The type cannot be started for the first eleven minutes (likewise a worker or start limit).
    definition = lead.definitions.pop("Worker")
    started_at = []
    with caplog.at_level(logging.INFO):
        for minute in range(1, 181):  # a pass every minute for three hours
            if minute == 11:
                assert prune_departed_agent_rows(lead, now=died + timedelta(minutes=11)) == 1
                lead.definitions["Worker"] = definition  # the refusal's cause is over
            if await take_over_orphaned_work(lead, now=died + timedelta(minutes=minute), clock=clock + 60.0 * minute):
                started_at.append(minute)
                break
    # Refused at minutes 1, 2 and 7; the next paced try, 15 minutes later, starts it.
    assert started_at == [22]
    assert _attempt(lead, 1, busy).state == AgentLaunchState.STARTING
    assert not [line for line in _lines(caplog, "orphaned_work_takeover_gave_up") if f"_gave_up owner_agent_id={busy} " in line]


@pytest.mark.asyncio
async def test_after_a_give_up_the_later_owner_with_attempts_keeps_leading(lead, caplog):
    """Two owners alive after a give-up: the one whose series runs leads, not the one saved first."""

    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    base = datetime.now(timezone.utc)
    clock = await _run_series_to_give_up(lead, DEAD, base, 0.0)
    later = base + timedelta(days=3)
    newcomer, second = "agent_dead_worker_later", "agent_dead_worker_second"
    launch_record(lead, AgentLaunchState.FAILED, agent_id=newcomer)
    held_work(lead, owner=newcomer, task_id="work-2")
    _saved_at(lead, newcomer, later)
    assert await take_over_orphaned_work(lead, now=later + timedelta(minutes=1), clock=clock + 100) == 1
    # Saved after the give-up but before the newcomer, so it is the first row; it dies now.
    second_record = launch_record(lead, AgentLaunchState.READY, agent_id=second)
    held_work(lead, owner=second, task_id="work-3")
    _saved_at(lead, second, base + timedelta(hours=2))
    AgentLaunchRepository(lead.task_manager.storage_settings).update(second_record, state=AgentLaunchState.FAILED)
    with caplog.at_level(logging.INFO):
        assert await take_over_orphaned_work(lead, now=later + timedelta(minutes=2), clock=clock + 200) == 0
    assert _attempt(lead, 1, second) is None and _attempt(lead, 1, newcomer).state == AgentLaunchState.STARTING
    # It waits for the newcomer's series; it does not start (or join) one of its own.
    assert [line for line in _lines(caplog, "orphaned_work_takeover_waiting") if f"owner_agent_id={second} " in line]
    assert _lines(caplog, "orphaned_work_takeover_joined") == []


@pytest.mark.asyncio
async def test_an_owner_whose_own_series_gave_up_never_leads_again(lead, caplog):
    """Even with a proof of life dated after its series ended (a late or skewed heartbeat)."""

    waiter = "agent_dead_worker_2"
    launch_record(lead, AgentLaunchState.FAILED)
    launch_record(lead, AgentLaunchState.FAILED, agent_id=waiter)
    held_work(lead)
    held_work(lead, owner=waiter, task_id="work-2")
    _takeover_lead(lead)
    base = datetime.now(timezone.utc)
    clock = await _run_series_to_give_up(lead, DEAD, base, 0.0)
    _heartbeat(lead, DEAD, base + timedelta(hours=2))
    with caplog.at_level(logging.ERROR):
        assert await take_over_orphaned_work(lead, now=base + timedelta(hours=3), clock=clock + 100) == 0
    gave_up = _lines(caplog, "orphaned_work_takeover_gave_up")
    assert sorted(line.split("owner_agent_id=")[1].split()[0] for line in gave_up) == sorted([DEAD, waiter])


@pytest.mark.asyncio
async def test_the_owner_with_attempts_keeps_leading_when_an_older_worker_dies_later(lead):
    """Leading by the first saved owner started a second series beside the first."""

    idle = launch_record(lead, AgentLaunchState.READY, agent_id="agent_idle_worker")
    held_work(lead, owner="agent_idle_worker", task_id="work-0")  # saved first: the first row
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    lead._local_team_runtime.inspect_launch = lambda record: "ready"
    _takeover_lead(lead)
    base = datetime.now(timezone.utc)
    assert await take_over_orphaned_work(lead, now=base, clock=1.0) == 1
    _end(lead, 1, now=base + timedelta(seconds=5))
    AgentLaunchRepository(lead.task_manager.storage_settings).update(idle, state=AgentLaunchState.FAILED)
    assert await take_over_orphaned_work(lead, now=base + timedelta(seconds=30), clock=100.0) == 0
    assert await take_over_orphaned_work(lead, now=base + timedelta(minutes=2), clock=200.0) == 1
    assert _attempt(lead, 2) is not None and _attempt(lead, 1, "agent_idle_worker") is None


@pytest.mark.asyncio
async def test_dead_workers_of_one_type_for_different_people_get_their_own_replacements(lead):
    other = "agent_dead_for_someone_else"
    launch_record(lead, AgentLaunchState.FAILED)
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    record, _ = repository.reserve(
        make_launch(agent_id=other, owner_canonical_user_id="someone_else"), LaunchCapacity.defaults(), join=False
    )
    repository.update(repository.update(record, state=AgentLaunchState.STARTING), state=AgentLaunchState.FAILED)
    held_work(lead)
    held_work(lead, owner=other, task_id="work-2")
    _takeover_lead(lead)
    assert await take_over_orphaned_work(lead, clock=1.0) == 2
    assert _attempt(lead, 1) is not None and _attempt(lead, 1, other, person="someone_else") is not None


@pytest.mark.asyncio
async def test_a_failed_replacement_under_the_old_start_id_does_not_block_the_takeover(lead):
    """The running team after the upgrade: a replacement that crashed under ``takeover:<owner>``."""

    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    old, _ = repository.reserve(
        make_launch(
            launch_id=launch_id_for("team", "owner", f"takeover:{DEAD}"),
            agent_id="agent_old_replacement",
            source="delegation",
        ),
        LaunchCapacity.defaults(),
        join=False,
    )
    repository.update(repository.update(old, state=AgentLaunchState.STARTING), state=AgentLaunchState.FAILED, reason=CRASH)
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    assert await take_over_orphaned_work(lead, clock=1.0) == 1
    assert _attempt(lead, 1).state == AgentLaunchState.STARTING


@pytest.mark.asyncio
async def test_the_lead_forgets_an_owner_once_it_holds_no_work(lead, monkeypatch):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead)
    _takeover_lead(lead)
    del lead.definitions["Worker"]  # refused: remembered
    await take_over_orphaned_work(lead, clock=1.0)
    assert lead._orphaned_work_takeovers[DEAD].refusals == 1
    held_work(lead, state=ProgressState.COMPLETED)
    await take_over_orphaned_work(lead, clock=100.0)
    assert DEAD not in lead._orphaned_work_takeovers


def _ended(attempt_ended_at: datetime, *, state=AgentLaunchState.FAILED, ran=timedelta(hours=1)):
    return make_launch(state=state, created_at_utc=attempt_ended_at - ran, updated_at_utc=attempt_ended_at, reason=CRASH)


def test_takeover_step_waits_each_delay_from_the_attempts_end_and_gives_up_after_the_last():
    end = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    second = timedelta(seconds=1)
    first = takeover_step({}, end)
    assert (first.action, first.attempt, first.previous) == (STEP_START, 1, None)
    for attempt, delay in enumerate(TAKEOVER_RETRY_DELAYS_SECONDS, start=1):
        record = _ended(end)  # it ran an hour: the delay counts from its end
        due = end + timedelta(seconds=delay)
        waiting = takeover_step({attempt: record}, due - second)
        starting = takeover_step({attempt: record}, due)
        assert (waiting.action, waiting.attempt) == (STEP_WAIT, attempt)
        assert (starting.action, starting.attempt, starting.previous) == (STEP_START, attempt + 1, record)
    for state in (
        AgentLaunchState.RESERVED,
        AgentLaunchState.STARTING,
        AgentLaunchState.READY,
        AgentLaunchState.UNKNOWN,
    ):
        assert takeover_step({1: _ended(end, state=state)}, end + timedelta(days=30)).action == STEP_WAIT
    retired = takeover_step({2: _ended(end, state=AgentLaunchState.RETIRED)}, end + timedelta(minutes=5))
    assert (retired.action, retired.attempt) == (STEP_START, 3)
    # The highest attempt decides, whatever was pruned before it.
    assert takeover_step({1: _ended(end), 3: _ended(end)}, end + timedelta(days=1)).attempt == 4
    gave_up = takeover_step({MAX_TAKEOVER_ATTEMPTS: _ended(end)}, end + timedelta(days=30))
    assert (gave_up.action, gave_up.attempt) == (STEP_GIVE_UP, MAX_TAKEOVER_ATTEMPTS)


@pytest.mark.asyncio
async def test_a_pruned_first_attempt_does_not_restart_the_series(lead):
    launch_record(lead, AgentLaunchState.FAILED)
    held_work(lead, signed=True)  # whom the work is for, once the dead start's record is pruned too
    _takeover_lead(lead)
    long_after = _later(timedelta(days=1).total_seconds())
    assert await take_over_orphaned_work(lead, now=long_after, clock=1.0) == 1
    _end(lead, 1)
    assert await take_over_orphaned_work(lead, now=long_after, clock=100.0) == 1
    # retention_days later: ended records go, attempt 1 and the dead start's own among them.
    AgentLaunchRepository(lead.task_manager.storage_settings).prune("team", before=_later(60))
    assert _attempt(lead, 1) is None and _attempt(lead, 2) is not None
    _end(lead, 2)
    assert await take_over_orphaned_work(lead, now=long_after, clock=200.0) == 1
    assert _attempt(lead, 3) is not None and _attempt(lead, 2) is not None


@pytest.mark.asyncio
async def test_a_dead_owner_nobody_is_known_for_is_logged_once_while_it_holds_work(lead, caplog):
    """It was logged on every 60-second pass (review, RC-2)."""

    held_work(lead)  # unsigned work and no start record: nobody it works for is known
    _takeover_lead(lead)
    with caplog.at_level(logging.WARNING):
        for clock in (1.0, 100.0, 200.0):
            assert await take_over_orphaned_work(lead, clock=clock) == 0
    (skipped,) = _lines(caplog, "orphaned_work_takeover_skipped")
    assert f"owner_agent_id={DEAD} agent_type=Worker task_ids=['work-1']" in skipped
    # Once it holds no work it is forgotten; a new case is logged again.
    held_work(lead, state=ProgressState.COMPLETED)
    assert await take_over_orphaned_work(lead, clock=300.0) == 0
    held_work(lead)
    with caplog.at_level(logging.WARNING):
        assert await take_over_orphaned_work(lead, clock=400.0) == 0
    assert len(_lines(caplog, "orphaned_work_takeover_skipped")) == 2
    assert _worker_starts(lead) == []
