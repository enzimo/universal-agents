"""Failed worker starts: counted from start records, waited out, and named in reviews.

Incident (2026-11-19): every new worker of one type crashed at start (the work
stream had reached its NATS consumer limit). The pool controller counted each
accepted start as a success, started another worker every pass, made 60 starts
in six minutes and then hit the hourly start limit. TeamArchitect only saw the
limit refusal. A start now counts as successful only once its worker is ready,
a worker that ends before that fails its record with the reason, and the
controller waits longer before each next start of the type.

Pure decisions use built records; the exit recording uses the real start-record
store with the scheduled-worker test lead. The controller against a real NATS
queue is in ``tests/test_live_worker_pools.py``.
"""
# ruff: noqa: F811 -- imported pytest fixture is injected by name

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nats.js.errors import BadRequestError

from custom_agents.agent_instantiation_service import (
    CRASHED_BEFORE_READY,
    EXITED_BEFORE_READY,
    PROCESS_HOST_ID,
    record_worker_exit,
    start_failure_reason,
)
from custom_agents.worker_pool_controller import (
    PoolObservation,
    StartHistory,
    decide,
    start_history,
    start_retry_seconds,
)
from custom_agents.worker_pool_limits import PoolLimit
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage.agent_launch_repository import AgentLaunchRepository, LaunchCapacity
from tests.test_schedule_worker_service import lead  # noqa: F401
from tests.test_schedule_worker_transactions import make_launch

NOW = datetime(2026, 11, 19, 11, 15, tzinfo=timezone.utc)
TYPE = "ResearchComparisonAgent"


def _record(minutes_ago: float, state: AgentLaunchState, *, ready: bool = False, reason=None, agent_type=TYPE):
    at = NOW - timedelta(minutes=minutes_ago)
    return make_launch(
        named_agent_type=agent_type,
        state=state,
        created_at_utc=at,
        updated_at_utc=at + timedelta(seconds=1),
        ready_at_utc=at if ready else None,
        reason=reason,
    )


# -- start history --------------------------------------------------------------


def test_failed_starts_are_counted_back_to_the_last_worker_that_became_ready() -> None:
    history = start_history(
        [
            _record(1, AgentLaunchState.FAILED, reason="crashed_before_ready: BadRequestError"),
            _record(2, AgentLaunchState.STARTING),  # still starting: neither
            _record(3, AgentLaunchState.FAILED, reason="older"),
            _record(4, AgentLaunchState.FAILED, agent_type="OtherAgent"),
            _record(5, AgentLaunchState.RETIRED, ready=True),
            _record(6, AgentLaunchState.FAILED, reason="before the last good worker"),
        ],
        TYPE,
    )
    assert history == StartHistory(
        failures_in_row=2,
        last_failure_at=NOW - timedelta(minutes=1) + timedelta(seconds=1),
        last_failure_reason="crashed_before_ready: BadRequestError",
        recovered=False,
    )


def test_a_ready_worker_after_the_failures_means_the_type_recovered() -> None:
    history = start_history(
        [_record(1, AgentLaunchState.READY, ready=True), _record(2, AgentLaunchState.FAILED)], TYPE
    )
    assert history.failures_in_row == 0 and history.recovered


def test_failures_older_than_the_start_limit_window_no_longer_count() -> None:
    launches = [_record(30, AgentLaunchState.FAILED), _record(90, AgentLaunchState.FAILED)]
    assert start_history(launches, TYPE, since=NOW - timedelta(hours=1)).failures_in_row == 1
    assert start_history(launches, TYPE, since=NOW - timedelta(minutes=10)) == StartHistory()


def test_a_type_never_started_has_no_history() -> None:
    assert start_history([], TYPE) == StartHistory()


def test_the_wait_doubles_with_each_failure_in_a_row_up_to_the_maximum() -> None:
    waits = [start_retry_seconds(n) for n in range(0, 9)]
    assert waits == [0.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0, 600.0, 600.0]
    assert start_retry_seconds(10_000) == 600.0
    # 11 tries in the first hour instead of the hourly limit of 60.
    elapsed, tries = 0.0, 1
    while True:
        elapsed += start_retry_seconds(tries)
        if elapsed > 3600:
            break
        tries += 1
    assert tries == 11


# -- decisions ------------------------------------------------------------------


def _observation(**overrides) -> PoolObservation:
    values = dict(
        agent_type=TYPE, waiting=1, asking=0, unacknowledged=0, oldest_wait_seconds=5.0,
        running=0, starting=0, unknown=0, draining=0, cooling_down=0,
        limit=PoolLimit(scope=TYPE, effective=4, default=4, ceiling=16), observed_at=NOW,
    )
    values.update(overrides)
    return PoolObservation(**values)


def test_no_worker_starts_while_the_wait_after_a_failure_runs() -> None:
    decision = decide(
        _observation(failed_starts_in_row=3, last_start_failure="crashed_before_ready: X", start_retry_in_seconds=12.3),
        team_active=0, team_limit_value=32, settings=WorkerPoolsConfig(),
    )
    assert decision.starts == 0
    assert decision.reason == (
        "the last 3 start(s) of this type failed (crashed_before_ready: X); the next start waits 13 s"
    )


def test_one_worker_is_tried_again_once_the_wait_ran_out() -> None:
    decision = decide(
        _observation(failed_starts_in_row=3, start_retry_in_seconds=0.0),
        team_active=0, team_limit_value=32, settings=WorkerPoolsConfig(),
    )
    assert decision.starts == 1


def test_the_pool_view_shows_the_failures_and_the_wait() -> None:
    shown = _observation(failed_starts_in_row=2, last_start_failure="r", start_retry_in_seconds=4.04).as_dict()
    assert (shown["failed_starts_in_row"], shown["last_start_failure"], shown["next_start_waits_seconds"]) == (2, "r", 4.0)


# -- the start record when a worker ends ----------------------------------------


def test_the_reason_keeps_the_error_type_and_a_nats_refusal_but_no_other_text() -> None:
    refusal = BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")
    assert start_failure_reason(refusal) == (
        f"{CRASHED_BEFORE_READY}: BadRequestError (NATS 10026: maximum consumers limit reached)"
    )
    assert start_failure_reason(RuntimeError("token=abc123 leaked")) == f"{CRASHED_BEFORE_READY}: RuntimeError"
    assert start_failure_reason(None) == EXITED_BEFORE_READY


class _Worker:
    def __init__(self, lead, record, *, ready: bool) -> None:
        self.id = record.agent_id
        self.team_id = record.team_id
        self.task_manager = lead.task_manager
        self._worker_launch = record
        self._worker_ready = ready


def _starting(lead):
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    record, _ = repository.reserve(
        make_launch(team_id="team", host_id=PROCESS_HOST_ID, source="pool"), LaunchCapacity.defaults(), join=False
    )
    return repository, repository.update(record, state=AgentLaunchState.STARTING)


def test_a_worker_that_crashes_before_it_is_ready_fails_its_record_with_the_reason(lead) -> None:
    repository, record = _starting(lead)
    crash = BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")
    record_worker_exit(_Worker(lead, record, ready=False), crash)
    stored = repository.get(record.launch_id, "team")
    assert stored.state == AgentLaunchState.FAILED
    assert stored.reason == start_failure_reason(crash)
    assert start_history([stored], stored.named_agent_type).failures_in_row == 1


def test_a_worker_that_was_ready_retires_without_a_failure(lead) -> None:
    repository, record = _starting(lead)
    ready = repository.update(record, state=AgentLaunchState.READY, ready_at_utc=datetime.now(timezone.utc))
    record_worker_exit(_Worker(lead, ready, ready=True), RuntimeError("after work"))
    stored = repository.get(record.launch_id, "team")
    assert (stored.state, stored.reason) == (AgentLaunchState.RETIRED, None)
    assert start_history([stored], stored.named_agent_type).recovered
