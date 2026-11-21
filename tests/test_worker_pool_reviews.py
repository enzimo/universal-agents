"""Worker pool problems: when TeamArchitect gets a review and when administrators are told.

Real SQLite worker pool store; the review admission (``open_review``) and the
stuck-work notice engine are captured.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents import worker_pool_reviews as reviews
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.worker_pool_controller import PoolObservation, _report_start_history
from custom_agents.worker_pool_limits import PoolLimit, worker_pool_repository
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage import resolve_storage_settings
from storage.worker_pool_repository import (
    INCIDENT_CAPACITY,
    INCIDENT_PROVIDER_LIMITED,
    INCIDENT_START_FAILED,
    ProviderLimitEventV1,
)

WORKER_TYPE = "WebResearchAgent"
# The real review admission, kept before the ``world`` fixture replaces it.
REAL_OPEN_REVIEW = reviews.open_review
CRASH = "crashed_before_ready: BadRequestError (NATS 10026: maximum consumers limit reached)"


@pytest.fixture
def world(tmp_path, monkeypatch):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "pools.db")}})
    agent = SimpleNamespace(
        id="lead",
        team_id="team",
        # The real lane: it copies every argument and result, so a call that hands it
        # the agent itself fails here as in the runtime (2026-11-20: every pool pass).
        task_manager=SimpleNamespace(storage_settings=storage, operations_runtime=TaskOperationRuntime()),
        _pool_rate_limited_types=set(),
        _pool_refusals_in_row={},
        # Like the real agent's locks and running tasks: cannot be copied.
        _uncopyable=threading.Lock(),
    )
    opened: list[dict] = []

    async def record_review(_agent, incident, *, recurrences, recurring, settings):
        opened.append({"kind": incident.kind, "scope": incident.agent_type, "recurring": recurring, "details": incident.details})
        return f"review_{len(opened)}"

    told: list = []

    async def capture_stuck_work(_agent, source, *, notice_after, now):
        told.append(source.findings(now))

    monkeypatch.setattr(reviews, "open_review", record_review)
    monkeypatch.setattr(reviews, "report_stuck_work", capture_stuck_work)
    return SimpleNamespace(agent=agent, opened=opened, told=told)


def _settings(**overrides) -> WorkerPoolsConfig:
    return WorkerPoolsConfig(**overrides)


def _observation(**overrides) -> PoolObservation:
    values = dict(
        agent_type=WORKER_TYPE,
        waiting=5,
        asking=0,
        unacknowledged=0,
        oldest_wait_seconds=60.0,
        running=2,
        starting=0,
        unknown=0,
        draining=0,
        cooling_down=0,
        limit=PoolLimit(scope=WORKER_TYPE, effective=4, default=4, ceiling=16),
        observed_at=datetime.now(timezone.utc),
    )
    values.update(overrides)
    return PoolObservation(**values)


def _provider_event(agent_type: str = WORKER_TYPE, minutes_ago: float = 1) -> ProviderLimitEventV1:
    return ProviderLimitEventV1(
        team_id="team",
        agent_type=agent_type,
        agent_id="w1",
        provider="litellm",
        model_id="model-x",
        status_code=429,
        at_utc=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_repeated_provider_limits_open_one_review_until_the_repeat_time(world):
    repository = worker_pool_repository(world.agent)
    for _ in range(3):
        repository.record_provider_limit(_provider_event())
    settings = _settings()

    first = reviews.WorkerPoolIncidents(world.agent, settings)
    await first.observe_provider_limits([_observation(cooling_down=1)])
    await first.settle()
    assert [(item["kind"], item["scope"]) for item in world.opened] == [(INCIDENT_PROVIDER_LIMITED, WORKER_TYPE)]
    details = world.opened[0]["details"]
    assert (details["provider_limit_events_in_window"], details["status_codes"]) == (3, [429])
    assert details["workers_waiting_out_the_limit"] == 1

    again = reviews.WorkerPoolIncidents(world.agent, settings)
    await again.observe_provider_limits([_observation(cooling_down=1)])
    await again.settle()
    assert len(world.opened) == 1  # still open, reviewed recently


@pytest.mark.asyncio
async def test_fewer_provider_limit_events_than_the_threshold_open_nothing(world):
    worker_pool_repository(world.agent).record_provider_limit(_provider_event())
    incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
    await incidents.observe_provider_limits([_observation()])
    await incidents.settle()
    assert world.opened == []


@pytest.mark.asyncio
async def test_a_failed_start_is_reviewed_and_administrators_are_told_until_a_start_works(world):
    settings = _settings()
    failing = reviews.WorkerPoolIncidents(world.agent, settings)
    failing.start_failed(_observation(failed_starts_in_row=2, last_start_failure=CRASH), CRASH)
    await failing.settle()
    assert [item["kind"] for item in world.opened] == [INCIDENT_START_FAILED]
    (findings,) = world.told
    assert [finding.subject_id for finding in findings] == [f"{WORKER_TYPE}:{INCIDENT_START_FAILED}"]
    assert findings[0].tell_administrators

    recovered = reviews.WorkerPoolIncidents(world.agent, settings)
    recovered.start_succeeded(WORKER_TYPE)
    await recovered.settle()
    assert worker_pool_repository(world.agent).list_open_incidents("team") == ()
    assert world.told[-1] == []  # nothing left to report; the engine sends the recovery notice


@pytest.mark.asyncio
async def test_one_failed_start_opens_nothing_and_a_second_in_a_row_opens_review_and_notice(world):
    """The lead's decision on CRIT-2: a single passing failure no longer reaches TeamArchitect or administrators."""

    crash = "crashed_before_ready: BadRequestError (NATS 10026: maximum consumers limit reached)"
    settings = _settings()
    first = reviews.WorkerPoolIncidents(world.agent, settings)
    _report_start_history(_observation(failed_starts_in_row=1, last_start_failure=crash, start_retry_in_seconds=9.0), first)
    await first.settle()
    assert world.opened == [] and world.told == [[]]

    second = reviews.WorkerPoolIncidents(world.agent, settings)
    _report_start_history(_observation(failed_starts_in_row=2, last_start_failure=crash, start_retry_in_seconds=19.0), second)
    await second.settle()
    assert [(item["kind"], item["details"]["start_refused_because"]) for item in world.opened] == [
        (INCIDENT_START_FAILED, crash)
    ]
    assert [finding.subject_id for finding in world.told[-1]] == [f"{WORKER_TYPE}:{INCIDENT_START_FAILED}"]


@pytest.mark.asyncio
async def test_a_problem_that_keeps_coming_back_is_raised_to_administrators(world):
    repository = worker_pool_repository(world.agent)
    now = datetime.now(timezone.utc)
    for hours_ago in (5, 3):
        incident, _ = repository.record_incident(
            team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_CAPACITY, details={},
            now=now - timedelta(hours=hours_ago),
        )
        repository.close_incident(incident, now=now - timedelta(hours=hours_ago) + timedelta(minutes=30))

    incidents = reviews.WorkerPoolIncidents(world.agent, _settings(recurring_incident_count=3))
    decision = SimpleNamespace(at_capacity=True, reason="the pool is at its size")
    await incidents.observe_type(_observation(), decision)
    await incidents.settle()
    assert world.opened[-1]["kind"] == INCIDENT_CAPACITY
    assert world.opened[-1]["recurring"] is True
    (findings,) = world.told
    assert [finding.reason for finding in findings] == ["worker_pool_capacity"]
    assert "came back 3 times within 24 hours" in findings[0].message.text


@pytest.mark.asyncio
async def test_a_one_off_capacity_problem_is_not_sent_to_administrators(world):
    incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
    await incidents.observe_type(_observation(), SimpleNamespace(at_capacity=True, reason="the pool is at its size"))
    await incidents.settle()
    assert world.opened[-1]["recurring"] is False
    assert world.told == [[]]


def test_review_instructions_steer_the_choice_by_problem():
    now = datetime.now(timezone.utc)
    from storage.worker_pool_repository import WorkerPoolIncidentV1

    def text(kind: str, **kwargs) -> str:
        incident = WorkerPoolIncidentV1(
            incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=kind,
            opened_at_utc=now, last_seen_at_utc=now,
        )
        return reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings(), **kwargs)

    assert "A and B make this worse" in text(INCIDENT_PROVIDER_LIMITED)
    assert "Neither a larger pool nor a higher start limit fixes this" in text(INCIDENT_START_FAILED)
    for kind in (INCIDENT_CAPACITY, INCIDENT_PROVIDER_LIMITED, INCIDENT_START_FAILED):
        assert "D. fewer workers" in text(kind)


# -- what TeamArchitect and administrators read about a start_failed problem ---------

FAILED_DETAILS = {
    "pool": {"failed_starts_in_row": 2, "last_start_failure": CRASH},
    "start_refused_because": CRASH,
}
UNCOUNTED = "launch_policy_revoked_before_readiness"
LIMIT_DETAILS = {
    "pool": {"failed_starts_in_row": 0, "starts_ended_not_counted": 4, "last_start_end_not_counted": UNCOUNTED},
    "start_refused_because": "worker_launch_rate_limit",
    "hourly_start_limit_reached": True,
}


async def _review_instructions(monkeypatch, details: dict) -> str:
    """The text TeamArchitect's review task starts with, from the real open_review, before its evidence."""

    from storage.worker_pool_repository import WorkerPoolIncidentV1

    monkeypatch.setattr(
        "custom_agents.task_context_runtime.sign_for_maintenance_owner", lambda _agent, meta, **_kw: meta
    )
    captured: dict = {}

    async def admit(*, task_request, msg_header, msg_meta) -> None:
        captured["task"] = task_request

    agent = SimpleNamespace(id="lead", name="lead", team_id="team", _handle_task_request=admit)
    now = datetime.now(timezone.utc)
    incident = WorkerPoolIncidentV1(
        incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_START_FAILED,
        opened_at_utc=now, last_seen_at_utc=now, details=details,
    )
    assert await REAL_OPEN_REVIEW(agent, incident, recurrences=1, recurring=False, settings=_settings())
    instructions, _, evidence = captured["task"].problem_statement.partition("\n\nEvidence:\n")
    assert evidence  # the evidence still follows
    return instructions


def _notice_text(world, details: dict) -> str:
    worker_pool_repository(world.agent).record_incident(
        team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_START_FAILED, details=details,
    )
    (finding,) = reviews.RecurringPoolProblems(world.agent, _settings()).findings(datetime.now(timezone.utc))
    return finding.message.text


@pytest.mark.asyncio
async def test_the_review_names_the_failure_and_says_a_higher_limit_does_not_help(monkeypatch):
    """Review finding EVD-2: the review said to check the start limits, the advice of the 2026-11-19 incident."""

    instructions = await _review_instructions(monkeypatch, FAILED_DETAILS)
    assert f"Latest failure: {CRASH}." in instructions
    assert "Neither a larger pool nor a higher start limit fixes this" in instructions
    assert "last_start_failure" in instructions
    assert "and start limits" not in instructions


def test_the_administrators_notice_names_the_failure_not_pool_sizes(world):
    text = _notice_text(world, FAILED_DETAILS)
    assert f"Latest failure: {CRASH}." in text
    assert "Neither a larger pool nor a higher start limit fixes this" in text
    assert "already waits longer before each next start" in text
    assert "event=agent_thread_crash" in text and "event=worker_start_failed" in text
    assert "pool sizes" not in text


@pytest.mark.asyncio
async def test_a_refusal_with_no_start_record_says_the_launcher_refuses_not_that_it_waits(monkeypatch, world):
    """Critique C21: no start was made, so there is no wait and no event=worker_start_failed line."""

    details = {"pool": {"failed_starts_in_row": 0}, "start_refused_because": "launcher_refused: ValueError"}
    for shown in (await _review_instructions(monkeypatch, details), _notice_text(world, details)):
        assert "The launcher refuses to start workers of this type: launcher_refused: ValueError." in shown
        assert "refuses every start of this type until the cause is fixed" in shown
        assert "waits longer" not in shown
        assert "event=worker_launch_refused" in shown


@pytest.mark.asyncio
async def test_the_evidence_the_controller_writes_reaches_both_texts(world):
    """Critique C22: the real writers (the controller's hourly-limit refusal, the pool view) feed the texts."""

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import WORKER_LAUNCH_RATE_LIMIT, AgentLaunchResultV1
    from storage.worker_pool_repository import WorkerPoolIncidentV1

    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT)
    incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
    observation = _observation(uncounted_start_ends=5, last_uncounted_start_end=UNCOUNTED)
    await controller._start(observation, PoolDecision(1, "start"), incidents)
    await incidents.settle()
    ((opened),) = world.opened
    now = datetime.now(timezone.utc)
    incident = WorkerPoolIncidentV1(
        incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_START_FAILED,
        opened_at_utc=now, last_seen_at_utc=now, details=opened["details"],
    )
    instructions = reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings())
    (finding,) = world.told[-1]
    for shown in (instructions, finding.message.text):
        assert "no start failed since a worker of this type was last ready" in shown
        assert f"5 start(s) ended for a reason that does not count as a failure; the latest: {UNCOUNTED}." in shown


@pytest.mark.asyncio
async def test_the_hourly_limit_with_no_failed_start_points_at_how_often_workers_start(monkeypatch, world):
    """Critique C03: then the limit, or why workers start so often, is what to look at."""

    instructions = await _review_instructions(monkeypatch, LIMIT_DETAILS)
    text = _notice_text(world, LIMIT_DETAILS)
    for shown in (instructions, text):
        assert "no start failed since a worker of this type was last ready" in shown
        assert "max_launches_per_type_per_hour, 60" in shown
        assert f"4 start(s) ended for a reason that does not count as a failure; the latest: {UNCOUNTED}." in shown
        assert "started so often" in shown
        # Critique C20: workers that crash right after they become ready are retired, not failed.
        assert "workers that stop soon after they become ready" in shown and "event=agent_thread_crash" in shown
        assert "Neither a larger pool nor a higher start limit fixes this" not in shown


@pytest.mark.asyncio
async def test_failed_starts_decide_the_text_when_the_hourly_limit_also_refused(monkeypatch):
    details = {**FAILED_DETAILS, "hourly_start_limit_reached": True,
               "start_refused_because": f"worker_launch_rate_limit; the last 2 start(s) failed: {CRASH}"}
    instructions = await _review_instructions(monkeypatch, details)
    assert f"the last 2 start(s) failed: {CRASH}" in instructions
    assert "no start failed since" not in instructions


@pytest.mark.asyncio
async def test_an_accepted_start_leaves_an_open_start_problem_open(world):
    """Review finding EVD-1 as an outcome (critique C19): an accepted start leaves a start problem open.

    Here the hourly limit opened it while a failed start was in view. The
    accepted worker may still crash; only one that becomes ready ends a problem
    of failed starts (and one opened otherwise ends after the quiet time, see
    test_a_start_problem_without_failed_starts_ends_after_the_quiet_time). Counting
    acceptance as success closed it at once and told
    administrators it works again.
    """

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import WORKER_LAUNCH_RATE_LIMIT, AgentLaunchResultV1

    results = [
        AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT),
        AgentLaunchResultV1(status="starting", agent_id="w", launch_id="l"),
    ]
    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: results.pop(0)
    for _ in range(2):
        incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
        observation = _observation(failed_starts_in_row=1, last_start_failure=CRASH)
        _report_start_history(observation, incidents)
        await controller._start(observation, PoolDecision(1, "start"), incidents)
        await incidents.settle()
    assert results == []
    assert [incident.kind for incident in worker_pool_repository(world.agent).list_open_incidents("team")] == [
        INCIDENT_START_FAILED
    ]
    assert [item["kind"] for item in world.opened] == [INCIDENT_START_FAILED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "passes"),
    [
        ("hourly_limit", (1, 0, 1, 0, 1)),  # refused, no attempt, refused, no attempt, refused
        # A refusal before any start record opens the problem on the second in a row.
        ("no_start_record", (1, 1, 0, 1, 1)),
    ],
)
async def test_a_start_problem_no_failed_start_opened_stays_one_problem_while_workers_are_ready(world, refusal, passes):
    """Critique C24: a pass with no refusal while a worker of the type is ready does not close and reopen it.

    The hourly limit with workers that do become ready, or a launcher refusal
    made before any start record while older workers still run: no failed
    start keeps the type from counting as recovered, so a ready worker must
    not end the problem. It stays one problem with one review until it has not
    been seen for the quiet time.
    """

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import WORKER_LAUNCH_RATE_LIMIT, AgentLaunchResultV1

    refused = (
        AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT)
        if refusal == "hourly_limit"
        else AgentLaunchResultV1(status="denied", reason="agent_definition_unavailable")
    )
    results = [refused] * sum(passes)
    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: results.pop(0)
    # A worker of the type became ready since the last failure, so the type counts as recovered.
    healthy = _observation(running=1, start_recovered=True)
    for starts in passes:
        incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
        _report_start_history(healthy, incidents)
        await controller._start(healthy, PoolDecision(starts, "start" if starts else "a worker is free"), incidents)
        await incidents.settle()
    assert results == []
    repository = worker_pool_repository(world.agent)
    assert [incident.kind for incident in repository.list_open_incidents("team")] == [INCIDENT_START_FAILED]
    assert [item["kind"] for item in world.opened] == [INCIDENT_START_FAILED]
    assert len(repository.recent_incidents("team", since=datetime.now(timezone.utc) - timedelta(hours=1))) == 1


@pytest.mark.asyncio
async def test_a_problem_of_failed_starts_ends_with_the_first_ready_worker(world):
    """The control for C24: failed starts in its evidence, so a ready worker ends it at once."""

    settings = _settings()
    failing = reviews.WorkerPoolIncidents(world.agent, settings)
    _report_start_history(_observation(failed_starts_in_row=2, last_start_failure=CRASH), failing)
    await failing.settle()
    recovered = reviews.WorkerPoolIncidents(world.agent, settings)
    _report_start_history(_observation(waiting=0, start_recovered=True), recovered)
    await recovered.settle()
    assert worker_pool_repository(world.agent).list_open_incidents("team") == ()


@pytest.mark.asyncio
async def test_a_start_problem_without_failed_starts_ends_after_the_quiet_time(world):
    """Critique C26: with a ready worker reported, it ends only once unseen for review_repeat_after_minutes."""

    settings = _settings()
    quiet = timedelta(minutes=settings.review_repeat_after_minutes)
    opening = reviews.WorkerPoolIncidents(world.agent, settings)
    opening.start_failed(_observation(), "agent_definition_unavailable")  # a refusal before any start record
    await opening.settle()
    assert [finding.subject_id for finding in world.told[-1]] == [f"{WORKER_TYPE}:{INCIDENT_START_FAILED}"]

    for after, still_open in ((quiet - timedelta(minutes=1), True), (quiet + timedelta(minutes=1), False)):
        later = reviews.WorkerPoolIncidents(world.agent, settings)
        later.now = opening.now + after
        _report_start_history(_observation(waiting=0, start_recovered=True), later)  # a ready worker, problem not seen
        await later.settle()
        open_now = worker_pool_repository(world.agent).list_open_incidents("team")
        assert bool(open_now) is still_open
        assert bool(world.told[-1]) is still_open  # the notice stands, then ends (the engine sends "working again")


@pytest.mark.asyncio
async def test_an_older_releases_free_error_text_never_reaches_the_review_or_notice(world):
    """Review finding GEN-2: a failed record with the previous launcher's str(exc) is shown as 'no reason recorded'."""

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController, start_history
    from custom_messages.contracts.schedule_worker_v1 import (
        WORKER_LAUNCH_RATE_LIMIT,
        AgentLaunchResultV1,
        AgentLaunchState,
    )
    from storage.worker_pool_repository import WorkerPoolIncidentV1
    from tests.test_schedule_worker_transactions import make_launch

    old_text = "cannot read /srv/team/private/agent.yaml"
    now = datetime.now(timezone.utc)
    history = start_history(
        [make_launch(named_agent_type=WORKER_TYPE, state=AgentLaunchState.FAILED, reason=old_text,
                     created_at_utc=now - timedelta(minutes=5), updated_at_utc=now - timedelta(minutes=5))],
        WORKER_TYPE,
    )
    observation = _observation(
        uncounted_start_ends=history.uncounted_ends, last_uncounted_start_end=history.last_uncounted_reason,
    )
    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT)
    incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
    await controller._start(observation, PoolDecision(1, "start"), incidents)
    await incidents.settle()
    ((opened),) = world.opened
    incident = WorkerPoolIncidentV1(
        incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_START_FAILED,
        opened_at_utc=now, last_seen_at_utc=now, details=opened["details"],
    )
    instructions = reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings())
    (finding,) = world.told[-1]
    for shown in (instructions, finding.message.text, str(opened["details"])):
        assert old_text not in shown
    assert "the latest: no reason recorded." in instructions


@pytest.mark.asyncio
async def test_refusals_before_any_start_record_open_review_and_notice_on_the_second_and_stay_seen(world):
    """Review finding GEN-3 with real incidents: one refusal opens nothing; the second does; the third keeps it fresh."""

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import AgentLaunchResultV1

    refused = AgentLaunchResultV1(status="denied", reason="agent_definition_unavailable")
    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: refused
    repository = worker_pool_repository(world.agent)
    start = datetime.now(timezone.utc)
    seen = []
    for minute in range(3):
        incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
        incidents.now = start + timedelta(minutes=minute)
        await controller._start(_observation(), PoolDecision(1, "start"), incidents)
        await incidents.settle()
        seen.append([incident.last_seen_at_utc for incident in repository.list_open_incidents("team")])
    assert seen[0] == []  # one refusal: nothing opens
    assert seen[1] == [start + timedelta(minutes=1)] and seen[2] == [start + timedelta(minutes=2)]
    assert [item["kind"] for item in world.opened] == [INCIDENT_START_FAILED]
    assert [finding.subject_id for finding in world.told[-1]] == [f"{WORKER_TYPE}:{INCIDENT_START_FAILED}"]


def test_the_review_and_notice_name_hung_starts(world):
    """Review finding RC-1: a hung start is named, and that it still takes a pool slot."""

    details = {"pool": {"failed_starts_in_row": 2, "starts_hung": 1}, "start_refused_because": CRASH}
    text = _notice_text(world, details)
    now = datetime.now(timezone.utc)
    from storage.worker_pool_repository import WorkerPoolIncidentV1

    incident = WorkerPoolIncidentV1(incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_START_FAILED,
                                    opened_at_utc=now, last_seen_at_utc=now, details=details)
    instructions = reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings())
    for shown in (text, instructions):
        assert "1 start(s) of this type stopped responding" in shown
        assert "still takes a pool slot until it ends or the runtime restarts" in shown


@pytest.mark.asyncio
async def test_a_rate_limit_episode_ends_when_its_type_is_no_longer_registered(world, monkeypatch, caplog):
    """Review finding RC-2 through reconcile: removed type ends its episode; an unreadable queue does not."""

    from custom_agents.worker_pool_controller import WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import WORKER_LAUNCH_RATE_LIMIT, AgentLaunchResultV1

    settings = _settings()
    monkeypatch.setattr("custom_agents.worker_pool_settings.current_worker_pool_settings", lambda _agent: settings)
    world.agent._worker_pool_snapshot = {}
    team = PoolLimit(scope="*", effective=32, default=32, ceiling=64)
    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT)

    def passes(registered: set[str], observed: bool):
        async def observe(_settings):
            controller.registered_types = set(registered)
            return ([_observation(running=0)] if observed else []), 0, team

        return observe

    with caplog.at_level(logging.INFO, logger="custom_agents.worker_pool_controller"):
        for registered, observed in (({WORKER_TYPE}, True), ({WORKER_TYPE}, False), (set(), False), ({WORKER_TYPE}, True)):
            controller.observe = passes(registered, observed)
            await controller.reconcile()
    messages = [r.getMessage() for r in caplog.records]
    began = [m for m in messages if "event=worker_pool_start_rate_limited " in m]
    ended = [m for m in messages if "event=worker_pool_start_rate_limit_ended" in m]
    assert len(began) == 2
    assert len(ended) == 1 and "because=no longer a worker type" in ended[0]


class _Queues:
    """The two queue reads observe() makes; one type's queue cannot be read this pass."""

    def __init__(self, unreadable: str) -> None:
        self.unreadable = unreadable

    async def ensure_worker_queue(self, agent_type: str) -> None:
        return None

    async def worker_queue_state(self, agent_type: str):
        if agent_type == self.unreadable:
            return None
        return SimpleNamespace(waiting=2, asking=0, unacknowledged=0)

    async def oldest_worker_queue_message_at(self, agent_type: str):
        return datetime.now(timezone.utc) - timedelta(seconds=30)


@pytest.mark.asyncio
@pytest.mark.parametrize(("lease_seconds", "hung", "in_progress"), [(120, 1, 0), (900, 0, 1)])
async def test_observe_counts_hung_starts_and_keeps_unobserved_types_registered(
    world, monkeypatch, lease_seconds, hung, in_progress
):
    """Critique C10: the real observe() feeds hung starts and the registered types to the controller."""

    from custom_agents.agent_instantiation_service import PROCESS_HOST_ID
    from custom_agents.worker_pool_controller import WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState, utc_now
    from runtime_settings.schedule_workers import ScheduleWorkersConfig
    from storage.agent_launch_repository import AgentLaunchRepository, LaunchCapacity
    from tests.test_schedule_worker_transactions import make_launch

    monkeypatch.setattr(
        "custom_agents.agent_instantiation_service.current_worker_limits",
        lambda _agent: ScheduleWorkersConfig(lease_seconds=lease_seconds),
    )
    world.agent._local_team_runtime = SimpleNamespace(
        agent_types=lambda: (WORKER_TYPE, "Unreadable", "TeamArchitect"), local_workers=lambda _team: ()
    )
    world.agent._ensured_worker_queues = set()
    queues = _Queues("Unreadable")
    world.agent._ensure_nats_transport_adapter = lambda: queues
    repository = AgentLaunchRepository(world.agent.task_manager.storage_settings)
    record, _ = repository.reserve(
        make_launch(team_id="team", named_agent_type=WORKER_TYPE, source="pool", host_id=PROCESS_HOST_ID),
        LaunchCapacity.defaults(), join=False,
    )
    # Stopped for its startup timeout, its thread never ended: unknown since 6 minutes ago.
    repository.update(repository.update(record, state=AgentLaunchState.STARTING),
                      state=AgentLaunchState.UNKNOWN, reason="startup_timed_out",
                      now=utc_now() - timedelta(minutes=6))
    controller = WorkerPoolController(world.agent)
    observations, _, _ = await controller.observe(_settings())
    assert controller.registered_types == {WORKER_TYPE, "Unreadable"}
    ((observation),) = observations
    assert (observation.agent_type, observation.hung_starts, observation.starts_in_progress) == (
        WORKER_TYPE, hung, in_progress
    )
    assert observation.as_dict()["starts_hung"] == hung


def test_hung_starts_are_named_in_the_capacity_review_and_notice(world):
    """Critique C11: starts that hang without failing fill the pool; a larger pool does not help."""

    from storage.worker_pool_repository import WorkerPoolIncidentV1

    details = {"pool": {"starts_hung": 4}, "controller": "the pool is at its size"}
    now = datetime.now(timezone.utc)
    incident = WorkerPoolIncidentV1(incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_CAPACITY,
                                    opened_at_utc=now, last_seen_at_utc=now, details=details)
    instructions = reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings())
    notice = reviews.RecurringPoolProblems(world.agent, _settings())._next_steps(incident)
    for shown in (instructions, notice):
        assert "4 start(s) of this type stopped responding" in shown
        assert "a larger pool does not help while they hang" in shown
    plain = incident.model_copy(update={"details": {"pool": {"starts_hung": 0}}})
    assert "stopped responding" not in reviews._instructions(plain, recurring=False, recurrences=1, settings=_settings())


# -- round 3: advice by the kind of failure, and a queue that cannot be created ----


def _nats_crash(wrapped: bool = False) -> str:
    from custom_agents.agent_instantiation_service import start_failure_reason
    from nats.js.errors import BadRequestError

    refusal = BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")
    if not wrapped:
        return start_failure_reason(refusal)
    try:
        try:
            raise refusal
        except BadRequestError:
            raise RuntimeError("cleanup failed")
    except RuntimeError as error:
        return start_failure_reason(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_a_nats_refusal_points_at_the_nats_server_not_the_definition(monkeypatch, world, wrapped):
    """Review finding GEN-2 (round 3): the 2026-11-19 failure gets NATS advice, built from the real error."""

    reason = _nats_crash(wrapped)
    details = {"pool": {"failed_starts_in_row": 2, "last_start_failure": reason}, "start_refused_because": reason}
    for shown in (await _review_instructions(monkeypatch, details), _notice_text(world, details)):
        assert "NATS 10026" in shown and "check the NATS server and the work stream's limits" in shown
        assert "10026 means the work stream has reached its consumer limit" in shown
        assert "agent type's definition" not in shown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "advice"),
    [
        (None, "read the worker's error in the runtime log (event=agent_thread_crash for this type)"),
        ("startup_timed_out", "find what is slow while the worker starts"),
        ("agent_definition_unavailable", "check the agent type's definition and the operator's denied types"),
    ],
)
async def test_the_advice_follows_the_kind_of_failure(monkeypatch, world, reason, advice):
    from custom_agents.agent_instantiation_service import start_failure_reason

    reason = reason or start_failure_reason(RuntimeError("worker crashed"))
    failed = 0 if reason == "agent_definition_unavailable" else 2
    details = {"pool": {"failed_starts_in_row": failed, "last_start_failure": reason if failed else None},
               "start_refused_because": reason}
    for shown in (await _review_instructions(monkeypatch, details), _notice_text(world, details)):
        assert advice in shown


@pytest.mark.asyncio
async def test_an_hourly_limit_after_nats_crashes_gets_the_advice_of_the_crash(monkeypatch):
    crash = _nats_crash()
    details = {"pool": {"failed_starts_in_row": 2, "last_start_failure": crash}, "hourly_start_limit_reached": True,
               "start_refused_because": f"worker_launch_rate_limit; the last 2 start(s) failed: {crash}"}
    assert "check the NATS server" in await _review_instructions(monkeypatch, details)


class _QueueAdapter:
    """observe()'s queue reads; one type's queue creation fails with ``error`` until ``error`` is None."""

    def __init__(self, failing: str, error) -> None:
        self.failing, self.error = failing, error

    async def ensure_worker_queue(self, agent_type: str) -> None:
        if agent_type == self.failing and self.error is not None:
            raise self.error

    async def worker_queue_state(self, agent_type: str):
        return SimpleNamespace(waiting=0, asking=0, unacknowledged=0)

    async def oldest_worker_queue_message_at(self, agent_type: str):
        return None


def _queue_world(world, monkeypatch, error):
    from custom_agents.worker_pool_controller import WorkerPoolController
    from runtime_settings.schedule_workers import ScheduleWorkersConfig

    settings = _settings()
    monkeypatch.setattr("custom_agents.worker_pool_settings.current_worker_pool_settings", lambda _agent: settings)
    monkeypatch.setattr("custom_agents.agent_instantiation_service.current_worker_limits",
                        lambda _agent: ScheduleWorkersConfig())
    world.agent._local_team_runtime = SimpleNamespace(agent_types=lambda: (WORKER_TYPE, "Queueless"),
                                                      local_workers=lambda _team: ())
    world.agent._ensured_worker_queues = set()
    world.agent._worker_pool_snapshot = {}
    adapter = _QueueAdapter("Queueless", error)
    world.agent._ensure_nats_transport_adapter = lambda: adapter
    return adapter, lambda: WorkerPoolController(world.agent).reconcile()


@pytest.mark.asyncio
async def test_a_queue_nats_refuses_to_create_is_reported_from_the_second_pass(world, monkeypatch):
    """Review finding GEN-3 (round 3): the type's queue at the consumer limit reaches TeamArchitect and administrators."""

    from nats.js.errors import BadRequestError

    adapter, reconcile = _queue_world(
        world, monkeypatch, BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")
    )
    await reconcile()
    view = world.agent._worker_pool_snapshot["pools"]["Queueless"]
    assert view == {"agent_type": "Queueless", "queue_unavailable":
                    "worker_queue_unavailable: BadRequestError (NATS 10026: maximum consumers limit reached)"}
    assert world.opened == []  # one pass: nothing opens

    await reconcile()
    ((opened),) = world.opened
    assert (opened["kind"], opened["scope"]) == (INCIDENT_START_FAILED, "Queueless")
    now = datetime.now(timezone.utc)
    from storage.worker_pool_repository import WorkerPoolIncidentV1

    incident = WorkerPoolIncidentV1(incident_id="i", team_id="team", agent_type="Queueless", kind=INCIDENT_START_FAILED,
                                    opened_at_utc=now, last_seen_at_utc=now, details=opened["details"])
    instructions = reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings())
    (finding,) = [f for f in world.told[-1] if f.subject_id == f"Queueless:{INCIDENT_START_FAILED}"]
    for shown in (instructions, finding.message.text):
        assert "cannot be created" in shown and "NATS 10026" in shown
        assert "no worker of this type can take tasks until its queue exists" in shown
        assert "check the NATS server" in shown
        assert "launcher refuses" not in shown
        # Critique C09: the log line written for it, not the start events.
        assert "event=worker_pool_queue_create_failed" in shown and "event=worker_start_failed" not in shown

    adapter.error = None  # the queue can be created again: the row ends
    await reconcile()
    assert "Queueless" not in world.agent._pool_refusals_in_row
    assert "queue_unavailable" not in world.agent._worker_pool_snapshot["pools"]["Queueless"]


@pytest.mark.asyncio
async def test_a_queue_that_times_out_is_logged_but_not_reported(world, monkeypatch, caplog):
    """Critique C04: an error without a NATS code is a connection problem; a stall after a restart opens nothing."""

    _, reconcile = _queue_world(world, monkeypatch, TimeoutError("nats: timeout"))
    with caplog.at_level(logging.WARNING, logger="custom_agents.worker_pool_controller"):
        for _ in range(3):
            await reconcile()
    assert world.opened == [] and "Queueless" not in world.agent._worker_pool_snapshot["pools"]
    assert caplog.text.count("event=worker_pool_queue_create_failed") == 3


@pytest.mark.asyncio
async def test_a_launcher_refusal_after_an_older_failed_start_gets_the_refusals_advice(world):
    """Critique C08: one NATS crash earlier in the hour, then the launcher refuses twice; the advice is the refusal's."""

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import AgentLaunchResultV1
    from storage.worker_pool_repository import WorkerPoolIncidentV1

    crash = _nats_crash()
    controller = WorkerPoolController(world.agent)
    controller._launch = lambda _agent_type: AgentLaunchResultV1(status="denied", reason="agent_definition_unavailable")
    observation = _observation(failed_starts_in_row=1, last_start_failure=crash)
    for _ in range(2):
        incidents = reviews.WorkerPoolIncidents(world.agent, _settings())
        _report_start_history(observation, incidents)
        await controller._start(observation, PoolDecision(1, "start"), incidents)
        await incidents.settle()
    ((opened),) = world.opened
    now = datetime.now(timezone.utc)
    incident = WorkerPoolIncidentV1(incident_id="i", team_id="team", agent_type=WORKER_TYPE, kind=INCIDENT_START_FAILED,
                                    opened_at_utc=now, last_seen_at_utc=now, details=opened["details"])
    instructions = reviews._instructions(incident, recurring=False, recurrences=1, settings=_settings())
    (finding,) = world.told[-1]
    for shown in (instructions, finding.message.text):
        assert "agent_definition_unavailable" in shown
        assert "check the agent type's definition and the operator's denied types" in shown
        assert "check the NATS server" not in shown


@pytest.mark.asyncio
async def test_a_queue_nats_answers_503_for_is_logged_but_not_reported(world, monkeypatch, caplog):
    """Critique C10: 'JetStream system temporarily unavailable' carries a code but is temporary, like a timeout."""

    from nats.js.errors import ServiceUnavailableError

    _, reconcile = _queue_world(world, monkeypatch, ServiceUnavailableError(
        code=503, err_code=10008, description="JetStream system temporarily unavailable"))
    with caplog.at_level(logging.WARNING, logger="custom_agents.worker_pool_controller"):
        for _ in range(3):
            await reconcile()
    assert world.opened == [] and "Queueless" not in world.agent._worker_pool_snapshot["pools"]
    assert caplog.text.count("event=worker_pool_queue_create_failed") == 3


@pytest.mark.asyncio
async def test_the_inspect_tools_own_fresh_view_shows_a_queue_nats_refuses(world, monkeypatch):
    """Critique C11: with no recent controller snapshot, inspect_worker_pools observes itself and still shows it."""

    from custom_tools.providers.worker_pools import _snapshot
    from nats.js.errors import BadRequestError

    _queue_world(world, monkeypatch,
                 BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached"))
    world.agent._worker_pool_snapshot = {}
    view = await _snapshot(world.agent, _settings())
    assert view["pools"]["Queueless"] == {
        "agent_type": "Queueless",
        "queue_unavailable": "worker_queue_unavailable: BadRequestError (NATS 10026: maximum consumers limit reached)",
    }
