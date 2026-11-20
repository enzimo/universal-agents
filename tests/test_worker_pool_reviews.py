"""Worker pool problems: when TeamArchitect gets a review and when administrators are told.

Real SQLite worker pool store; the review admission (``open_review``) and the
stuck-work notice engine are captured.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents import worker_pool_reviews as reviews
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


class _Runtime:
    async def run(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture
def world(tmp_path, monkeypatch):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "pools.db")}})
    agent = SimpleNamespace(
        id="lead",
        team_id="team",
        task_manager=SimpleNamespace(storage_settings=storage, operations_runtime=_Runtime()),
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
    failing.start_failed(_observation(), "the type is denied by the operator")
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

    world.agent._pool_rate_limited_types = set()
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
    """Review finding EVD-1 as an outcome (critique C19): an hourly-limit problem stays open after an accepted start.

    The accepted worker may still crash; only one that becomes ready ends the
    problem. Counting acceptance as success closed it at once and told
    administrators it works again.
    """

    from custom_agents.worker_pool_controller import PoolDecision, WorkerPoolController
    from custom_messages.contracts.schedule_worker_v1 import WORKER_LAUNCH_RATE_LIMIT, AgentLaunchResultV1

    world.agent._pool_rate_limited_types = set()
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
