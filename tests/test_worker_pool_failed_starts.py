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

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from nats.js.errors import BadRequestError
from pydantic import BaseModel, ValidationError

from custom_agents.agent_instantiation_service import (
    CRASHED_BEFORE_READY,
    DEFINITION_CHANGED_BEFORE_START,
    EXITED_BEFORE_READY,
    LAUNCH_POLICY_REVOKED,
    LAUNCHER_RECONCILIATION,
    PROCESS_HOST_ID,
    STARTUP_TIMED_OUT,
    STOPPED_BY_RUNTIME,
    AgentInstantiationService,
    counts_as_failed_start,
    record_worker_exit,
    start_failure_reason,
)
from custom_agents.schedule_repair_guidance import SCHEDULE_LAUNCH_INTENT_CHANGED
from custom_agents.schedule_worker_controller import ScheduleWorkerController
from custom_agents.worker_pool_controller import (
    PoolObservation,
    StartHistory,
    decide,
    start_history,
    start_retry_seconds,
)
from custom_agents.worker_pool_limits import PoolLimit
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState, utc_now
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage.agent_launch_repository import AgentLaunchRepository, LaunchCapacity, LaunchConflict
from tests.test_schedule_worker_service import lead  # noqa: F401
from tests.test_schedule_worker_transactions import make_launch

NOW = datetime(2026, 11, 19, 11, 15, tzinfo=timezone.utc)
TYPE = "ResearchComparisonAgent"
CRASH = "crashed_before_ready: BadRequestError"


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
        starts_in_progress=1,
    )


def test_a_ready_worker_after_the_failures_means_the_type_recovered() -> None:
    history = start_history(
        [_record(1, AgentLaunchState.READY, ready=True), _record(2, AgentLaunchState.FAILED, reason=CRASH)], TYPE
    )
    assert history.failures_in_row == 0 and history.recovered


def test_failures_older_than_the_start_limit_window_no_longer_count() -> None:
    launches = [_record(30, AgentLaunchState.FAILED, reason=CRASH), _record(90, AgentLaunchState.FAILED, reason=CRASH)]
    assert start_history(launches, TYPE, since=NOW - timedelta(hours=1)).failures_in_row == 1
    assert start_history(launches, TYPE, since=NOW - timedelta(minutes=10)) == StartHistory()


def test_a_type_never_started_has_no_history() -> None:
    assert start_history([], TYPE) == StartHistory()


def test_ends_that_say_nothing_about_the_type_are_not_counted_but_shown() -> None:
    """Restart cleanup, a runtime stop, an edit during the start and an old reasonless record hold nothing back."""

    uncounted = [LAUNCHER_RECONCILIATION, STOPPED_BY_RUNTIME, SCHEDULE_LAUNCH_INTENT_CHANGED,
                 LAUNCH_POLICY_REVOKED, DEFINITION_CHANGED_BEFORE_START, None]
    launches = [_record(index + 1, AgentLaunchState.FAILED, reason=reason) for index, reason in enumerate(uncounted)]
    history = start_history(launches, TYPE)
    assert (history.failures_in_row, history.last_failure_at) == (0, None)
    assert (history.uncounted_ends, history.last_uncounted_reason) == (6, LAUNCHER_RECONCILIATION)
    assert not any(counts_as_failed_start(reason) for reason in uncounted)


@pytest.mark.parametrize(
    "reason",
    [
        # Written out, not read from the set, so dropping one fails here.
        "runtime_stopped_before_ready",
        "launcher_reconciliation",
        "launch_policy_revoked_before_readiness",
        "schedule_launch_intent_changed_before_start",
        "agent_definition_changed_before_start",
        "agent_definition_changed_before_construction",
        "agent_definition_changed_during_startup",
        "idle_ttl_limit_changed_before_start",
        "launcher_authorization_mode_changed_restart_required",
        "worker_pools_disabled",
        None,
    ],
)
def test_each_end_that_says_nothing_about_the_type_does_not_count(reason) -> None:
    assert not counts_as_failed_start(reason)


@pytest.mark.parametrize(
    "reason",
    [CRASH, STARTUP_TIMED_OUT, EXITED_BEFORE_READY, "launcher_refused: ValueError", "agent_type_disallowed",
     "schedule_worker_rule_exceeds_operator_limit:startup_timeout_seconds", "construction_failed"],
)
def test_ends_that_say_the_type_failed_count(reason) -> None:
    assert counts_as_failed_start(reason)


def test_crashes_timeouts_and_refused_rules_count_between_uncounted_ends() -> None:
    rule = "schedule_worker_rule_exceeds_operator_limit:startup_timeout_seconds"
    history = start_history(
        [
            _record(1, AgentLaunchState.FAILED, reason=STOPPED_BY_RUNTIME),
            _record(2, AgentLaunchState.FAILED, reason=STARTUP_TIMED_OUT),
            _record(3, AgentLaunchState.FAILED, reason=LAUNCHER_RECONCILIATION),
            _record(4, AgentLaunchState.FAILED, reason=rule),
            _record(5, AgentLaunchState.FAILED, reason=CRASH),
        ],
        TYPE,
    )
    assert (history.failures_in_row, history.last_failure_reason) == (3, STARTUP_TIMED_OUT)
    assert (history.uncounted_ends, history.last_uncounted_reason) == (2, STOPPED_BY_RUNTIME)


def test_only_starts_that_never_became_ready_are_in_progress() -> None:
    """A ready worker the launcher moved to unknown (its lease ran out) is not a start in progress."""

    history = start_history(
        [
            _record(1, AgentLaunchState.UNKNOWN, ready=True),
            _record(2, AgentLaunchState.UNKNOWN),
            _record(3, AgentLaunchState.STARTING),
            _record(4, AgentLaunchState.RESERVED),
            _record(5, AgentLaunchState.FAILED, reason=CRASH),
            _record(90, AgentLaunchState.STARTING),  # outside the window
        ],
        TYPE,
        since=NOW - timedelta(hours=1),
    )
    assert history.starts_in_progress == 3


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


def test_the_projects_own_refusals_keep_their_code_and_nothing_else_does() -> None:
    assert start_failure_reason(LaunchConflict(LAUNCH_POLICY_REVOKED)) == LAUNCH_POLICY_REVOKED
    assert start_failure_reason(PermissionError("agent_type_disallowed")) == "agent_type_disallowed"
    rule = "schedule_worker_rule_exceeds_operator_limit:idle_ttl_seconds"
    assert start_failure_reason(ValueError(rule)) == rule
    # An operating-system refusal names a path; only its type is kept.
    denied = PermissionError(13, "Permission denied", "/srv/team/secret.key")
    assert start_failure_reason(denied) == f"{CRASHED_BEFORE_READY}: PermissionError"
    assert start_failure_reason(ValueError("bad value in /srv/team/x")) == f"{CRASHED_BEFORE_READY}: ValueError"
    # A single word or a hex string is not a project code.
    assert start_failure_reason(ValueError("closed")) == f"{CRASHED_BEFORE_READY}: ValueError"
    assert start_failure_reason(PermissionError("denied")) == f"{CRASHED_BEFORE_READY}: PermissionError"
    token = "3f9a0c7be12d45f6a8b9c0d1e2f3a4b5"
    assert start_failure_reason(ValueError(token)) == f"{CRASHED_BEFORE_READY}: ValueError"


def test_a_project_refusal_under_a_cleanup_error_keeps_its_code() -> None:
    try:
        try:
            raise LaunchConflict(LAUNCH_POLICY_REVOKED)
        except LaunchConflict:
            raise RuntimeError("adapter close failed")
    except RuntimeError as wrapped:
        assert start_failure_reason(wrapped) == LAUNCH_POLICY_REVOKED


def test_a_nats_refusal_under_a_cleanup_error_is_still_named() -> None:
    refusal = BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")
    try:
        try:
            raise refusal
        except BadRequestError:
            raise RuntimeError("cleanup failed")
    except RuntimeError as wrapped:
        assert start_failure_reason(wrapped) == (
            f"{CRASHED_BEFORE_READY}: RuntimeError (NATS 10026: maximum consumers limit reached)"
        )


class _Rules(BaseModel):
    startup_timeout_seconds: int


@pytest.mark.parametrize("make_error", ["path", "validation"])
def test_a_launcher_refusal_stores_only_a_code_or_the_error_type(lead, caplog, make_error) -> None:
    """Refusal text reaches administrators' notices; a path or a validation error's input never does."""

    if make_error == "path":
        error: Exception = ValueError("cannot read /srv/team/private/agent.yaml")
    else:
        try:
            _Rules(startup_timeout_seconds="/srv/team/private")
        except ValidationError as exc:
            error = exc

    def refuse(*_args, **_kwargs):
        raise error

    lead._local_team_runtime.spawn_agent = refuse
    with caplog.at_level(logging.WARNING, logger="custom_agents.agent_instantiation_service"):
        result = AgentInstantiationService(lead).launch(agent_type="Worker", owner_id="owner", request_id="refused")
    expected = f"launcher_refused: {type(error).__name__}"
    stored = AgentLaunchRepository(lead.task_manager.storage_settings).get(result.launch_id, "team")
    assert (result.status, result.reason, stored.state, stored.reason) == (
        "denied", expected, AgentLaunchState.FAILED, expected
    )
    assert "/srv/team/private" not in result.reason
    # The failed start is logged once where it happened, with the full error for operators.
    (line,) = [r.getMessage() for r in caplog.records if "event=worker_start_failed" in r.getMessage()]
    assert f"reason={expected}" in line and "counted=True" in line and "/srv/team/private" in line


@pytest.mark.parametrize("make_error", ["path", "validation"])
def test_a_refusal_before_the_start_is_reserved_stores_only_the_error_type(lead, caplog, monkeypatch, make_error) -> None:
    """No start record, and the reason goes straight into the pool's incident: no path or input values in it."""

    if make_error == "path":
        error: Exception = ValueError("cannot read /srv/team/private/agent.yaml")
    else:
        try:
            _Rules(startup_timeout_seconds="/srv/team/private")
        except ValidationError as exc:
            error = exc

    def refuse(self, *_args, **_kwargs):
        raise error

    monkeypatch.setattr(AgentInstantiationService, "validate_type", refuse)
    with caplog.at_level(logging.WARNING, logger="custom_agents.agent_instantiation_service"):
        result = AgentInstantiationService(lead).launch(agent_type="Worker", owner_id="owner", request_id="early")
    assert (result.status, result.reason) == ("denied", f"launcher_refused: {type(error).__name__}")
    assert (result.launch_id, result.created) == (None, False)
    assert AgentLaunchRepository(lead.task_manager.storage_settings).list("team") == ()
    assert "event=worker_launch_refused" in caplog.text and "/srv/team/private" in caplog.text


def test_a_launcher_refusal_code_is_kept_as_is(lead, caplog) -> None:
    def refuse(*_args, **_kwargs):
        raise ValueError("mcp_launch_profile_unavailable")

    lead._local_team_runtime.spawn_agent = refuse
    with caplog.at_level(logging.INFO, logger="custom_agents.agent_instantiation_service"):
        result = AgentInstantiationService(lead).launch(agent_type="Worker", owner_id="owner", request_id="code")
    assert result.reason == "mcp_launch_profile_unavailable"
    (line,) = [r for r in caplog.records if "event=worker_start_failed" in r.getMessage()]
    assert line.levelno == logging.WARNING
    assert "reason=mcp_launch_profile_unavailable counted=True error=-" in line.getMessage()


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


def test_a_stop_the_runtime_asked_for_is_the_reason_and_is_logged(lead, caplog) -> None:
    repository, record = _starting(lead)
    with caplog.at_level(logging.INFO, logger="custom_agents.agent_instantiation_service"):
        record_worker_exit(_Worker(lead, record, ready=False), RuntimeError("cancelled"), stopped_because=STARTUP_TIMED_OUT)
    stored = repository.get(record.launch_id, "team")
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)
    (line,) = [r for r in caplog.records if "event=worker_start_failed" in r.getMessage()]
    assert line.levelno == logging.WARNING and "counted=True" in line.getMessage()


def test_a_stop_for_the_runtime_stopping_is_logged_as_not_counted(lead, caplog) -> None:
    repository, record = _starting(lead)
    with caplog.at_level(logging.INFO, logger="custom_agents.agent_instantiation_service"):
        record_worker_exit(_Worker(lead, record, ready=False), None, stopped_because=STOPPED_BY_RUNTIME)
    assert repository.get(record.launch_id, "team").reason == STOPPED_BY_RUNTIME
    (line,) = [r for r in caplog.records if "event=worker_start_failed" in r.getMessage()]
    assert line.levelno == logging.INFO and "counted=False" in line.getMessage()


def test_a_worker_that_was_ready_retires_without_a_failure(lead) -> None:
    repository, record = _starting(lead)
    ready = repository.update(record, state=AgentLaunchState.READY, ready_at_utc=datetime.now(timezone.utc))
    record_worker_exit(_Worker(lead, ready, ready=True), RuntimeError("after work"))
    stored = repository.get(record.launch_id, "team")
    assert (stored.state, stored.reason) == (AgentLaunchState.RETIRED, None)
    assert start_history([stored], stored.named_agent_type).recovered


# -- the real worker thread (strands_team.instantiate_agent_type) -----------------

THREAD_TYPE = "Worker"
THREAD_DEFINITION = {"named_agent_type": THREAD_TYPE, "role": "worker", "goal": "Read report", "tools": []}


class _ThreadWorker:
    """Stands in for a UniversalAgent inside the real worker thread; ``connect`` is the test's."""

    def __init__(self, lead, connect) -> None:
        self.name = "worker"
        self.task_manager = lead.task_manager
        self._worker_ready = False
        self._worker_launch = None
        self._exit = asyncio.Event()
        self._connect = connect

    def get_exit_event(self) -> asyncio.Event:
        return self._exit

    def set_exit_flag(self) -> None:
        self._exit.set()

    async def exit_agent(self) -> None:
        self._exit.set()

    async def connect(self) -> None:
        await self._connect(self)


async def _wait_for_exit(worker) -> None:
    await worker.get_exit_event().wait()


def _spawn(lead, monkeypatch, connect):
    """Start a worker through the real instantiate_agent_type and its thread body."""

    from config_loader import Config
    from custom_agents import strands_team
    from custom_agents.schedule_definition_identity import launch_definition_digest

    worker = _ThreadWorker(lead, connect)

    def build(config):
        worker.id, worker.team_id = config["id"], config["team_id"]
        return worker

    monkeypatch.setattr(strands_team, "load_agent_type_map", lambda: {THREAD_TYPE: THREAD_DEFINITION})
    monkeypatch.setattr(strands_team, "create_strands_agent", build)
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    record, _ = repository.reserve(
        make_launch(team_id="team", host_id=PROCESS_HOST_ID, source="pool", named_agent_type=THREAD_TYPE,
                    definition_digest=launch_definition_digest(THREAD_DEFINITION)),
        LaunchCapacity.defaults(), join=False,
    )
    record = repository.update(record, state=AgentLaunchState.STARTING)
    assert strands_team.instantiate_agent_type(THREAD_TYPE, "team", launch=record, runtime_config=Config({})).accepted
    return repository, record, worker


def _ended(repository, record, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stored = repository.get(record.launch_id, "team")
        if stored.state != AgentLaunchState.STARTING:
            return stored
        time.sleep(0.02)
    raise AssertionError("the worker thread did not end its start record")


def _running_loop(record, timeout: float = 10.0) -> None:
    from custom_agents import strands_team

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with strands_team._SPAWNED_AGENT_LOCK:
            handle = strands_team._SPAWNED_AGENTS.get(record.agent_id)
        if handle is not None and handle.runtime_loop is not None:
            return
        time.sleep(0.02)
    raise AssertionError("the worker thread did not start its loop")


def test_a_refused_connect_in_the_real_worker_thread_reaches_the_start_record(lead, monkeypatch) -> None:
    """2026-11-19: the worker's mailbox was refused at the stream's consumer limit (NATS 10026)."""

    async def refused(_worker):
        raise BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")

    repository, record, _ = _spawn(lead, monkeypatch, refused)
    stored = _ended(repository, record)
    assert (stored.state, stored.reason) == (
        AgentLaunchState.FAILED,
        f"{CRASHED_BEFORE_READY}: BadRequestError (NATS 10026: maximum consumers limit reached)",
    )


def test_a_worker_cancelled_at_start_records_the_crash(lead, monkeypatch) -> None:
    async def cancelled(_worker):
        raise asyncio.CancelledError()

    repository, record, _ = _spawn(lead, monkeypatch, cancelled)
    assert _ended(repository, record).reason == f"{CRASHED_BEFORE_READY}: CancelledError"


def test_a_worker_stopped_for_its_startup_timeout_records_that(lead, monkeypatch) -> None:
    """Fast join: the worker's own exit record carries the reason the launcher stopped it for."""

    from custom_agents import strands_team

    repository, record, _ = _spawn(lead, monkeypatch, _wait_for_exit)
    _running_loop(record)
    assert strands_team.stop_worker_launch(record, reason=STARTUP_TIMED_OUT)
    stored = _ended(repository, record)
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)
    assert counts_as_failed_start(stored.reason)


def test_a_worker_still_starting_when_the_runtime_stops_is_not_a_failed_start(lead, monkeypatch) -> None:
    from custom_agents import strands_team

    repository, record, _ = _spawn(lead, monkeypatch, _wait_for_exit)
    _running_loop(record)
    strands_team.shutdown_spawned_agents("team", "lead", 5.0)
    stored = _ended(repository, record)
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STOPPED_BY_RUNTIME)
    assert start_history([stored], THREAD_TYPE).failures_in_row == 0


# -- the launcher's reconciliation of starts still in progress --------------------


def _starting_record(lead, *, source: str = "pool", **changes):
    # reserve() stamps the store's current time; tests pass a later "now" to age it.
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)
    record, _ = repository.reserve(
        make_launch(team_id="team", host_id=PROCESS_HOST_ID, source=source, **changes),
        LaunchCapacity.defaults(), join=False,
    )
    return repository, repository.update(record, state=AgentLaunchState.STARTING)


def _launcher_stops(lead, *, stopped: bool) -> list[str]:
    reasons: list[str] = []

    def stop(record, *, reason):
        reasons.append(reason)
        return stopped

    lead._local_team_runtime.stop_launch = stop
    return reasons


async def _reconcile(lead, now=None) -> None:
    from custom_agents.agent_instantiation_service import current_worker_limits

    await ScheduleWorkerController(lead)._reconcile_launches(now or utc_now(), current_worker_limits(lead))


def _use_config(monkeypatch, data: dict) -> None:
    from config_loader import Config

    monkeypatch.setattr("config_loader._config", Config(data))


@pytest.mark.asyncio
async def test_a_timed_out_start_whose_thread_outlives_the_join_still_ends_startup_timed_out(lead, monkeypatch) -> None:
    """Slow join: unknown first, failed once the thread is gone, and the reason survives both writes."""

    _use_config(monkeypatch, {})
    repository, record = _starting_record(lead)
    reasons = _launcher_stops(lead, stopped=False)
    await _reconcile(lead, utc_now() + timedelta(seconds=150))  # the default startup timeout is 120 s
    assert reasons == [STARTUP_TIMED_OUT]
    unknown = repository.get(record.launch_id, "team")
    assert (unknown.state, unknown.reason) == (AgentLaunchState.UNKNOWN, STARTUP_TIMED_OUT)

    lead._local_team_runtime.inspect_launch = lambda _record: "absent"
    await _reconcile(lead, utc_now() + timedelta(minutes=10))  # after the lease ran out
    failed = repository.get(record.launch_id, "team")
    assert (failed.state, failed.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)
    assert start_history([failed], failed.named_agent_type).failures_in_row == 1


@pytest.mark.asyncio
async def test_a_timed_out_start_whose_thread_is_already_gone_fails_startup_timed_out(lead, monkeypatch) -> None:
    _use_config(monkeypatch, {})
    repository, record = _starting_record(lead)
    _launcher_stops(lead, stopped=True)
    await _reconcile(lead, utc_now() + timedelta(seconds=150))
    stored = repository.get(record.launch_id, "team")
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)


@pytest.mark.asyncio
async def test_a_pool_start_whose_type_rules_are_refused_counts_as_a_failed_start(lead, monkeypatch) -> None:
    """A type rule above an operator maximum stops every start of the type until it is fixed (critique C01)."""

    _use_config(monkeypatch, {
        "agent": {"schedule_workers": {"max_startup_timeout_seconds": 300}},
        "agent_types": {"Worker": {"agent": {"schedule_worker_rules": {"startup_timeout_seconds": 600}}}},
    })
    refused = "schedule_worker_rule_exceeds_operator_limit:startup_timeout_seconds"
    repository, record = _starting_record(lead)
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    stored = repository.get(record.launch_id, "team")
    assert reasons == [refused]
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, refused)
    assert start_history([stored], stored.named_agent_type).failures_in_row == 1


@pytest.mark.asyncio
async def test_a_scheduled_start_whose_policy_was_revoked_does_not_count(lead, monkeypatch) -> None:
    _use_config(monkeypatch, {"agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}}})
    repository, record = _starting_record(lead, source="schedule", schedule_id="removed")
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    stored = repository.get(record.launch_id, "team")
    assert reasons == [LAUNCH_POLICY_REVOKED]
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, LAUNCH_POLICY_REVOKED)
    assert start_history([stored], stored.named_agent_type).failures_in_row == 0
