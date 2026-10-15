"""Worker pool problems: when TeamArchitect gets a review and when administrators are told.

Real SQLite worker pool store; the review admission (``open_review``) and the
stuck-work notice engine are captured.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_agents import worker_pool_reviews as reviews
from custom_agents.worker_pool_controller import PoolObservation
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
    assert "A pool size does not fix this" in text(INCIDENT_START_FAILED)
    for kind in (INCIDENT_CAPACITY, INCIDENT_PROVIDER_LIMITED, INCIDENT_START_FAILED):
        assert "D. fewer workers" in text(kind)
