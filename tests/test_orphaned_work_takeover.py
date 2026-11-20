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


def _attempt(lead, attempt: int, owner: str = DEAD):
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    return repository.get(launch_id_for("team", "owner", takeover_request_id(owner, attempt)), "team")


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
    assert f"owner_agent_id={DEAD} agent_type=Worker task_ids=['work-1'] attempts={MAX_TAKEOVER_ATTEMPTS}" in gave_up
    assert f"last_reason={CRASH}" in gave_up


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
    # 1, 5, 15 and 60 minutes after each crash, then give up: five starts in all.
    assert started_at == [0, 2, 8, 24, 85]
    leaders = {owner for owner in owners if _attempt(lead, 1, owner) is not None}
    assert len(leaders) == 1
    waiting = _lines(caplog, "orphaned_work_takeover_waiting")
    assert len(waiting) == 3 and all(f"for_owner_agent_id={next(iter(leaders))}" in line for line in waiting)
    (gave_up,) = _lines(caplog, "orphaned_work_takeover_gave_up")
    assert f"attempts={MAX_TAKEOVER_ATTEMPTS} last_reason={CRASH}" in gave_up


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
