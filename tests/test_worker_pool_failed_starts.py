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
from types import SimpleNamespace

import pytest
from nats.js.errors import BadRequestError
from pydantic import BaseModel, ValidationError

from custom_agents.agent_instantiation_service import (
    CONSTRUCTION_FAILED,
    CRASHED_BEFORE_READY,
    DEFINITION_CHANGED_BEFORE_START,
    EXITED_BEFORE_READY,
    KNOWN_START_CODES,
    LAUNCH_POLICY_REVOKED,
    LAUNCHER_RECONCILIATION,
    LAUNCHER_REJECTED_START,
    PROCESS_HOST_ID,
    SCHEDULE_STARTUP_TIMED_OUT,
    STARTUP_TIMED_OUT,
    STOPPED_BY_RUNTIME,
    UNCOUNTED_START_ENDS,
    AgentInstantiationService,
    counts_as_failed_start,
    is_current_reason,
    launcher_refusal_reason,
    record_worker_exit,
    refusal_code,
    start_failure_reason,
)
from custom_agents.schedule_repair_guidance import SCHEDULE_LAUNCH_INTENT_CHANGED
from custom_agents.schedule_worker_controller import ScheduleWorkerController
from custom_agents.worker_pool_controller import (
    START_FAILURES_BEFORE_REVIEW,
    PoolDecision,
    PoolObservation,
    StartHistory,
    WorkerPoolController,
    _report_start_history,
    decide,
    start_history,
    start_retry_in,
    start_retry_seconds,
)
from custom_agents.worker_pool_limits import PoolLimit
from custom_messages.contracts.schedule_worker_v1 import (
    WORKER_LAUNCH_RATE_LIMIT,
    AgentLaunchResultV1,
    AgentLaunchState,
    utc_now,
)
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
            _record(3, AgentLaunchState.FAILED, reason=STARTUP_TIMED_OUT),
            _record(4, AgentLaunchState.FAILED, agent_type="OtherAgent"),
            _record(5, AgentLaunchState.RETIRED, ready=True),
            _record(6, AgentLaunchState.FAILED, reason=CRASH),  # before the last good worker
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


def test_failed_starts_count_by_when_they_ended() -> None:
    """Critique C17: two starts made over an hour ago that ended within it are two failures in a row."""

    first = make_launch(named_agent_type=TYPE, state=AgentLaunchState.FAILED, reason=STARTUP_TIMED_OUT,
                        created_at_utc=NOW - timedelta(minutes=95), updated_at_utc=NOW - timedelta(minutes=35))
    second = make_launch(named_agent_type=TYPE, state=AgentLaunchState.FAILED, reason=CRASH,
                         created_at_utc=NOW - timedelta(minutes=90), updated_at_utc=NOW - timedelta(minutes=50))
    history = start_history([first, second], TYPE, since=NOW - timedelta(hours=1))
    assert history.failures_in_row == 2
    # The newest failure is the one that ended last, which is the older start here.
    assert (history.last_failure_at, history.last_failure_reason) == (NOW - timedelta(minutes=35), STARTUP_TIMED_OUT)


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
        "agent_type_ineligible_for_automatic_launch",
        "schedule_startup_timed_out",
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


def _observed(launches, **overrides) -> PoolObservation:
    """The observation the controller builds from start records (as in WorkerPoolController.observe)."""

    history = start_history(launches, TYPE, since=NOW - timedelta(hours=1))
    values = dict(
        failed_starts_in_row=history.failures_in_row,
        last_start_failure=history.last_failure_reason,
        start_retry_in_seconds=start_retry_in(history, NOW),
        start_recovered=history.recovered,
        starts_in_progress=history.starts_in_progress,
        uncounted_start_ends=history.uncounted_ends,
        last_uncounted_start_end=history.last_uncounted_reason,
    )
    values.update(overrides)
    return _observation(**values)


def _three_failures_the_last_50_seconds_ago():
    # The wait after a third failure is 40 s, so it is over.
    return [
        make_launch(named_agent_type=TYPE, state=AgentLaunchState.FAILED, reason=CRASH,
                    created_at_utc=NOW - timedelta(seconds=100 - index * 20),
                    updated_at_utc=NOW - timedelta(seconds=90 - index * 20))
        for index in range(3)
    ]


def test_the_wait_after_failed_starts_holds_the_next_start() -> None:
    """Critique C18: three failures, the last 10 s ago: 30 s of the 40 s wait remain, and nothing starts."""

    launches = [
        make_launch(named_agent_type=TYPE, state=AgentLaunchState.FAILED, reason=CRASH,
                    created_at_utc=NOW - timedelta(seconds=60 - index * 20),
                    updated_at_utc=NOW - timedelta(seconds=50 - index * 20))
        for index in range(3)
    ]
    observation = _observed(launches, waiting=6, oldest_wait_seconds=600.0)
    assert observation.start_retry_in_seconds == 30.0
    decision = decide(observation, team_active=0, team_limit_value=20, settings=WorkerPoolsConfig())
    assert decision.starts == 0 and decision.reason.endswith("the next start waits 30 s")
    assert _observed([]).start_retry_in_seconds == 0.0


def test_no_more_starts_while_the_retry_after_failures_is_still_starting() -> None:
    """Review probe probe_backoff_inflight: the retry made 6 s ago is still starting; 8f13f6af started 4 more."""

    launches = _three_failures_the_last_50_seconds_ago() + [
        make_launch(named_agent_type=TYPE, state=AgentLaunchState.STARTING,
                    created_at_utc=NOW - timedelta(seconds=6), updated_at_utc=NOW - timedelta(seconds=6)),
    ]
    observation = _observed(
        launches, waiting=6, oldest_wait_seconds=600.0, starting=1,
        limit=PoolLimit(scope=TYPE, effective=5, default=5, ceiling=10),
    )
    assert observation.start_retry_in_seconds == 0.0
    decision = decide(observation, team_active=1, team_limit_value=20, settings=WorkerPoolsConfig())
    assert decision.starts == 0
    assert "one more start is in progress" in decision.reason


def test_a_slow_start_left_unknown_still_holds_the_next_start() -> None:
    """A timed-out start whose thread outlived the join stays unknown, never ready, until its lease ends."""

    launches = _three_failures_the_last_50_seconds_ago() + [
        make_launch(named_agent_type=TYPE, state=AgentLaunchState.UNKNOWN, reason=STARTUP_TIMED_OUT,
                    created_at_utc=NOW - timedelta(seconds=30), updated_at_utc=NOW - timedelta(seconds=5)),
    ]
    observation = _observed(launches, waiting=6, oldest_wait_seconds=600.0, unknown=1)
    assert decide(observation, team_active=1, team_limit_value=20, settings=WorkerPoolsConfig()).starts == 0


def test_a_ready_worker_left_unknown_does_not_hold_the_next_start() -> None:
    """Its lease ran out while it worked; it is not a start in progress (critique C04)."""

    launches = _three_failures_the_last_50_seconds_ago() + [
        make_launch(named_agent_type=TYPE, state=AgentLaunchState.UNKNOWN, ready_at_utc=NOW - timedelta(hours=2),
                    created_at_utc=NOW - timedelta(minutes=20), updated_at_utc=NOW - timedelta(minutes=1)),
    ]
    observation = _observed(launches, waiting=6, oldest_wait_seconds=600.0, unknown=1)
    decision = decide(observation, team_active=1, team_limit_value=20, settings=WorkerPoolsConfig())
    assert decision.starts == 1


def test_after_failures_one_start_is_made_even_with_ready_workers_running() -> None:
    observation = _observed(
        _three_failures_the_last_50_seconds_ago(), waiting=6, oldest_wait_seconds=600.0, running=2,
    )
    decision = decide(observation, team_active=2, team_limit_value=20, settings=WorkerPoolsConfig())
    assert decision.starts == 1
    # Without failures the same pass may start up to max_starts_per_pass.
    healthy = _observation(waiting=6, oldest_wait_seconds=600.0, running=2)
    assert decide(healthy, team_active=2, team_limit_value=20, settings=WorkerPoolsConfig()).starts > 1


class _Incidents:
    def __init__(self) -> None:
        self.failed: list[tuple[str, dict]] = []
        self.succeeded: list[str] = []

    def start_failed(self, observation, reason, **flags) -> None:
        self.failed.append((reason, flags))

    def start_succeeded(self, agent_type) -> None:
        self.succeeded.append(agent_type)


def test_one_failure_opens_no_review_and_two_in_a_row_do() -> None:
    """The lead's decision on CRIT-2: a single passing failure only holds the next start."""

    assert START_FAILURES_BEFORE_REVIEW == 2
    one, two = _Incidents(), _Incidents()
    _report_start_history(_observation(failed_starts_in_row=1, last_start_failure=CRASH, start_retry_in_seconds=9.0), one)
    _report_start_history(_observation(failed_starts_in_row=2, last_start_failure=CRASH, start_retry_in_seconds=0.0), two)
    assert one.failed == []
    assert two.failed == [(CRASH, {})]  # also in the pass that retries (no wait left)


def test_no_review_while_no_tasks_wait() -> None:
    idle, busy = _Incidents(), _Incidents()
    _report_start_history(_observation(waiting=0, failed_starts_in_row=3, last_start_failure=CRASH, start_retry_in_seconds=5.0), idle)
    _report_start_history(_observation(waiting=1, failed_starts_in_row=3, last_start_failure=CRASH, start_retry_in_seconds=5.0), busy)
    assert idle.failed == [] and len(busy.failed) == 1


def _controller(results: list) -> WorkerPoolController:
    class _Runtime:
        async def run(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    agent = SimpleNamespace(team_id="team", task_manager=SimpleNamespace(operations_runtime=_Runtime()),
                            _pool_rate_limited_types=set())
    controller = WorkerPoolController(agent)
    controller._launch = lambda _agent_type: results.pop(0)
    return controller


@pytest.mark.asyncio
async def test_an_accepted_start_is_not_counted_as_a_success() -> None:
    """Only a worker that became ready ends a start_failed incident (review finding EVD-1)."""

    incidents = _Incidents()
    started = await _controller([AgentLaunchResultV1(status="starting", agent_id="w", launch_id="l")])._start(
        _observation(failed_starts_in_row=2, last_start_failure=CRASH), PoolDecision(1, "retry"), incidents
    )
    assert started == 1 and incidents.succeeded == []


@pytest.mark.asyncio
async def test_each_hourly_limit_episode_is_logged_when_it_begins_and_ends(caplog) -> None:
    """Tasks stop waiting while the limit refuses: the episode ends; a later refusal begins a new one (CONC-5)."""

    refused = AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT)
    controller = _controller([refused, refused])
    incidents = _Incidents()
    with caplog.at_level(logging.INFO, logger="custom_agents.worker_pool_controller"):
        await controller._start(_observation(waiting=3), PoolDecision(1, "start"), incidents)
        await controller._start(_observation(waiting=0), PoolDecision(0, "no tasks waiting"), incidents)
        await controller._start(_observation(waiting=3), PoolDecision(1, "start"), incidents)
    messages = [r.getMessage() for r in caplog.records]
    assert sum("event=worker_pool_start_rate_limited " in m for m in messages) == 2
    (ended,) = [m for m in messages if "event=worker_pool_start_rate_limit_ended" in m]
    assert "because=no tasks wait" in ended
    assert [flags for _, flags in incidents.failed] == [{"hourly_start_limit": True}] * 2


@pytest.mark.asyncio
async def test_an_hourly_limit_refusal_after_only_uncounted_ends_names_them() -> None:
    """Critique C01: ends that do not count still used up the limit, so the review names them."""

    incidents = _Incidents()
    controller = _controller([AgentLaunchResultV1(status="denied", reason=WORKER_LAUNCH_RATE_LIMIT)])
    observation = _observation(uncounted_start_ends=7, last_uncounted_start_end=LAUNCH_POLICY_REVOKED)
    await controller._start(observation, PoolDecision(1, "start"), incidents)
    ((reason, flags),) = incidents.failed
    assert reason == (
        f"{WORKER_LAUNCH_RATE_LIMIT}; no start failed, 7 start(s) ended for a reason that does not count, "
        f"the latest: {LAUNCH_POLICY_REVOKED}"
    )
    assert flags == {"hourly_start_limit": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "opens"),
    [
        # Refused before a record was reserved: tried every pass, so it opens at once.
        (AgentLaunchResultV1(status="denied", reason="agent_definition_unavailable"), True),
        # Its reason does not count (pools turned off meanwhile).
        (AgentLaunchResultV1(status="denied", reason="worker_pools_disabled"), False),
        # Refused after its record was reserved: the next pass counts the failed record.
        (AgentLaunchResultV1(status="denied", reason="agent_type_disallowed", launch_id="l", created=True), False),
    ],
)
async def test_a_launcher_refusal_opens_an_incident_at_once_only_when_it_will_repeat(result, opens) -> None:
    incidents = _Incidents()
    await _controller([result])._start(_observation(), PoolDecision(1, "start"), incidents)
    assert bool(incidents.failed) is opens


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


# -- only known codes, and only reasons this release writes (second review round) ----


@pytest.mark.parametrize("text", ["alex_smith", "agent_3f9a0c", "team_secret_value", "closed"])
def test_text_that_only_looks_like_a_code_is_not_kept(lead, text) -> None:
    """Review finding RC-3: a name or id with underscores is not a project code, so it is never stored."""

    assert refusal_code(ValueError(text)) is None
    assert start_failure_reason(ValueError(text)) == f"{CRASHED_BEFORE_READY}: ValueError"

    def refuse(*_args, **_kwargs):
        raise PermissionError(text)

    lead._local_team_runtime.spawn_agent = refuse
    result = AgentInstantiationService(lead).launch(agent_type="Worker", owner_id="owner", request_id=f"looks-{text}")
    assert result.reason == "launcher_refused: PermissionError"


def test_every_known_code_and_each_refused_rule_is_kept() -> None:
    from runtime_settings.schedule_workers import OPERATOR_LIMITED_RULES, RULE_EXCEEDS_OPERATOR_LIMIT

    for code in sorted(KNOWN_START_CODES):
        assert refusal_code(LaunchConflict(code)) == code
    for field in OPERATOR_LIMITED_RULES:
        assert refusal_code(ValueError(f"{RULE_EXCEEDS_OPERATOR_LIMIT}:{field}")) == f"{RULE_EXCEEDS_OPERATOR_LIMIT}:{field}"
    assert refusal_code(ValueError(f"{RULE_EXCEEDS_OPERATOR_LIMIT}:anything_else")) is None


def _raised_codes(module) -> dict[str, int]:
    """Code-shaped texts raised as PermissionError, ValueError or LaunchConflict in ``module``, with their lines."""

    import ast
    import inspect
    import re

    from custom_agents import agent_instantiation_service, schedule_repair_guidance

    namespaces = (module, agent_instantiation_service, schedule_repair_guidance)
    found: dict[str, int] = {}
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args):
            continue
        if not (isinstance(node.exc.func, ast.Name) and node.exc.func.id in {"PermissionError", "ValueError", "LaunchConflict"}):
            continue
        argument = node.exc.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            text = argument.value
        elif isinstance(argument, ast.Name):
            values = [getattr(space, argument.id) for space in namespaces if hasattr(space, argument.id)]
            assert values, f"cannot resolve {argument.id} raised in {module.__name__}:{node.lineno}"
            text = values[0]
        else:
            continue  # computed text: the rule-limit form, checked on its own
        if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", text):
            found[text] = node.lineno
    return found


@pytest.mark.parametrize(
    "module_name",
    [
        "custom_agents.agent_instantiation_service",
        "storage.agent_launch_repository",
        "custom_agents.strands_team",
        "custom_agents.schedule_definition_identity",
    ],
)
def test_every_code_the_launch_path_raises_is_a_known_code(module_name) -> None:
    """A new refusal code on the launch path must be listed, or it would reach people as 'launcher_refused: <type>'."""

    import importlib

    raised = _raised_codes(importlib.import_module(module_name))
    assert raised, f"no codes found in {module_name}; the scan is broken"
    assert {code: line for code, line in raised.items() if code not in KNOWN_START_CODES} == {}


@pytest.mark.parametrize(
    "error",
    [
        None,
        RuntimeError("worker crashed: token=abc"),
        BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached [stream: work] (a: b)"),
        LaunchConflict(LAUNCH_POLICY_REVOKED),
        ValueError("cannot read /srv/team/x"),
    ],
)
def test_every_crash_reason_the_thread_writes_is_current(error) -> None:
    """Critique C02: a reason this release writes is always recognised, so failures keep counting."""

    reason = start_failure_reason(error)
    assert is_current_reason(reason)
    assert counts_as_failed_start(reason) is (reason not in UNCOUNTED_START_ENDS)


def test_a_wrapped_nats_refusal_reason_is_current() -> None:
    try:
        try:
            raise BadRequestError(code=400, err_code=10026, description="limit: reached [x]")
        except BadRequestError:
            raise RuntimeError("cleanup failed")
    except RuntimeError as wrapped:
        reason = start_failure_reason(wrapped)
    assert reason.startswith(f"{CRASHED_BEFORE_READY}: RuntimeError (NATS 10026")
    assert counts_as_failed_start(reason)


def test_every_reason_the_launcher_and_reconciler_write_is_current() -> None:
    from custom_messages.contracts.schedule_worker_v1 import (
        AUTOMATIC_WORKER_CAPACITY_LIMIT,
        TEAM_WORKER_CAPACITY_LIMIT,
        WORKER_LAUNCH_RATE_LIMIT,
    )

    for error in (ValueError("not a code"), PermissionError("agent_type_disallowed")):
        assert is_current_reason(launcher_refusal_reason(error))
    for written in (EXITED_BEFORE_READY, STARTUP_TIMED_OUT, SCHEDULE_STARTUP_TIMED_OUT, STOPPED_BY_RUNTIME,
                    LAUNCHER_RECONCILIATION, LAUNCH_POLICY_REVOKED, CONSTRUCTION_FAILED, LAUNCHER_REJECTED_START):
        assert is_current_reason(written), written
    assert UNCOUNTED_START_ENDS <= KNOWN_START_CODES
    assert {AUTOMATIC_WORKER_CAPACITY_LIMIT, TEAM_WORKER_CAPACITY_LIMIT, WORKER_LAUNCH_RATE_LIMIT} <= KNOWN_START_CODES


@pytest.mark.parametrize(
    "old_text",
    ["idle_ttl_seconds must be a positive number", "MCP launch profile unavailable",
     "[Errno 13] Permission denied: '/srv/team/secret.key'", "1 validation error for Rules\nvalue='/srv/x'"],
)
def test_an_older_releases_free_error_text_is_neither_counted_nor_shown(old_text) -> None:
    """Review finding GEN-2: the previous launcher stored str(exc); such a record never reaches people."""

    history = start_history([_record(5, AgentLaunchState.FAILED, reason=old_text)], TYPE)
    assert (history.failures_in_row, history.uncounted_ends, history.last_uncounted_reason) == (0, 1, None)
    shown = _observed([_record(5, AgentLaunchState.FAILED, reason=old_text)]).as_dict()
    assert old_text not in str(shown)


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


def test_a_worker_stopped_for_its_timeout_keeps_that_reason_when_the_runtime_then_stops(lead, monkeypatch) -> None:
    """The first stop reason stays: a timed-out worker that leaves slowly is not 'runtime stopped'."""

    from custom_agents import strands_team

    async def leaves_slowly(worker) -> None:
        await worker.get_exit_event().wait()
        await asyncio.sleep(1.5)  # longer than the launcher's 1 s join

    repository, record, _ = _spawn(lead, monkeypatch, leaves_slowly)
    _running_loop(record)
    assert not strands_team.stop_worker_launch(record, reason=STARTUP_TIMED_OUT)
    strands_team.shutdown_spawned_agents("team", "lead", 5.0)
    assert _ended(repository, record).reason == STARTUP_TIMED_OUT


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
async def test_a_timed_out_start_whose_thread_outlives_the_join_still_ends_startup_timed_out(lead, monkeypatch, caplog) -> None:
    """Slow join: unknown first, failed once the thread is gone, and the reason survives both writes and is logged."""

    _use_config(monkeypatch, {})
    repository, record = _starting_record(lead)
    reasons = _launcher_stops(lead, stopped=False)
    with caplog.at_level(logging.INFO):
        await _reconcile(lead, utc_now() + timedelta(seconds=150))  # the default startup timeout is 120 s
    assert reasons == [STARTUP_TIMED_OUT]
    unknown = repository.get(record.launch_id, "team")
    assert (unknown.state, unknown.reason) == (AgentLaunchState.UNKNOWN, STARTUP_TIMED_OUT)
    (pending,) = [r.getMessage() for r in caplog.records if "event=worker_launch_liveness_unknown" in r.getMessage()]
    assert "reason=startup_timed_out" in pending
    assert "event=worker_start_failed" not in caplog.text  # not failed yet
    caplog.clear()

    lead._local_team_runtime.inspect_launch = lambda _record: "absent"
    with caplog.at_level(logging.INFO):
        await _reconcile(lead, utc_now() + timedelta(minutes=10))  # after the lease ran out
    failed = repository.get(record.launch_id, "team")
    assert (failed.state, failed.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)
    assert start_history([failed], failed.named_agent_type).failures_in_row == 1
    (line,) = [r.getMessage() for r in caplog.records if "event=worker_start_failed" in r.getMessage()]
    assert "reason=startup_timed_out counted=True" in line and f"launch_id={record.launch_id}" in line
    (settled,) = [r.getMessage() for r in caplog.records if "event=worker_launch_unknown_settled" in r.getMessage()]
    assert "state=failed reason=startup_timed_out" in settled


@pytest.mark.asyncio
async def test_a_timed_out_start_keeps_its_reason_when_another_check_stops_it_again(lead, monkeypatch) -> None:
    """The first reason stays in the record too (critique C10): a later stop for another reason does not replace it."""

    _use_config(monkeypatch, {})
    repository, record = _starting_record(lead)
    _launcher_stops(lead, stopped=False)
    await _reconcile(lead, utc_now() + timedelta(seconds=150))
    assert repository.get(record.launch_id, "team").reason == STARTUP_TIMED_OUT

    _use_config(monkeypatch, {
        "agent": {"schedule_workers": {"max_startup_timeout_seconds": 300}},
        "agent_types": {"Worker": {"agent": {"schedule_worker_rules": {"startup_timeout_seconds": 600}}}},
    })
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead, utc_now() + timedelta(seconds=160))
    stored = repository.get(record.launch_id, "team")
    assert reasons == ["schedule_worker_rule_exceeds_operator_limit:startup_timeout_seconds"]
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)


@pytest.mark.asyncio
async def test_a_timed_out_start_whose_thread_is_already_gone_fails_startup_timed_out(lead, monkeypatch, caplog) -> None:
    _use_config(monkeypatch, {})
    repository, record = _starting_record(lead)
    _launcher_stops(lead, stopped=True)
    with caplog.at_level(logging.INFO):
        await _reconcile(lead, utc_now() + timedelta(seconds=150))
    stored = repository.get(record.launch_id, "team")
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)
    (line,) = [r.getMessage() for r in caplog.records if "event=worker_start_failed" in r.getMessage()]
    assert "reason=startup_timed_out counted=True" in line


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["pool", "schedule"])
async def test_a_start_whose_type_rules_are_refused_counts_as_a_failed_start(lead, monkeypatch, source) -> None:
    """A type rule above an operator maximum stops every start of the type until it is fixed (critique C01)."""

    _use_config(monkeypatch, {
        "agent": {"schedule_workers": {"max_startup_timeout_seconds": 300}},
        "agent_types": {"Worker": {"agent": {"schedule_worker_rules": {"startup_timeout_seconds": 600}}}},
    })
    refused = "schedule_worker_rule_exceeds_operator_limit:startup_timeout_seconds"
    repository, record = _starting_record(lead, source=source)
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


@pytest.mark.asyncio
async def test_a_schedules_own_rule_above_a_maximum_does_not_count_against_the_type(lead, monkeypatch) -> None:
    """The operator lowered a maximum under one schedule's own override (critique C12): about that schedule only."""

    repository, record = _configured_schedule_start(lead, monkeypatch, rules={"startup_timeout_seconds": 600}, config={
        "agent": {"schedule_workers": {"eligible_agent_types": ["Worker"], "max_startup_timeout_seconds": 300}},
    })
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    stored = repository.get(record.launch_id, "team")
    assert reasons == [LAUNCH_POLICY_REVOKED]
    assert start_history([stored], stored.named_agent_type).failures_in_row == 0


@pytest.mark.asyncio
async def test_a_scheduled_start_of_a_denied_type_counts_as_a_failed_start(lead, monkeypatch) -> None:
    _use_config(monkeypatch, {"agent": {"schedule_workers": {
        "eligible_agent_types": ["Worker"], "denied_agent_types": ["Worker"],
    }}})
    repository, record = _starting_record(lead, source="schedule", schedule_id="removed")
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    stored = repository.get(record.launch_id, "team")
    assert reasons == ["agent_type_disallowed"]
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, "agent_type_disallowed")
    assert start_history([stored], stored.named_agent_type).failures_in_row == 1


@pytest.mark.asyncio
async def test_a_refused_check_that_is_not_a_code_keeps_only_its_type_and_logs_the_error(lead, monkeypatch, caplog) -> None:
    _use_config(monkeypatch, {
        "agent_types": {"Worker": {"agent": {"schedule_worker_rules": {"startup_timeout_seconds": "/srv/team/x"}}}},
    })
    repository, record = _starting_record(lead)
    reasons = _launcher_stops(lead, stopped=True)
    with caplog.at_level(logging.WARNING):
        await _reconcile(lead)
    assert reasons == ["launcher_refused: ValidationError"]
    assert repository.get(record.launch_id, "team").reason == "launcher_refused: ValidationError"
    refused = [r.getMessage() for r in caplog.records if "event=worker_launch_refused" in r.getMessage()]
    assert refused and "/srv/team/x" in refused[0]


@pytest.mark.asyncio
async def test_the_real_launcher_stops_a_timed_out_worker_through_its_thread(lead, monkeypatch) -> None:
    """The reason's whole way: _why_stop_starting, the real stop_worker_launch, the real thread's own record."""

    from custom_agents import strands_team

    _use_config(monkeypatch, {})
    repository, record, _ = _spawn(lead, monkeypatch, _wait_for_exit)
    _running_loop(record)
    lead._local_team_runtime.inspect_launch = strands_team.inspect_worker_launch
    lead._local_team_runtime.stop_launch = strands_team.stop_worker_launch
    await _reconcile(lead, utc_now() + timedelta(seconds=150))
    stored = _ended(repository, record)
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, STARTUP_TIMED_OUT)


def _configured_schedule_start(lead, monkeypatch, *, rules: dict, config: dict):
    """A scheduled start of the test lead's schedule "job", saved with ``rules``, then ``config`` in force."""

    from custom_agents.schedule_definition_identity import launch_definition_digest
    from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerPolicySpecV1
    from tests.test_schedule_worker_service import configure

    _use_config(monkeypatch, {"agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}}})
    configure(lead, policy=ScheduleWorkerPolicySpecV1(rules=rules))
    binding = lead._schedule_store.worker_records.get_binding("job", team_id="team")
    _use_config(monkeypatch, config)
    return _starting_record(
        lead, source="schedule", schedule_id="job", policy_revision=binding.revision,
        definition_digest=launch_definition_digest(lead.definitions["Worker"]),
    )


@pytest.mark.asyncio
async def test_a_scheduled_start_the_launcher_accepted_is_left_alone(lead, monkeypatch) -> None:
    """The type's rule is above a lowered maximum, but the schedule's own override is within it (critique C13)."""

    repository, record = _configured_schedule_start(lead, monkeypatch, rules={"startup_timeout_seconds": 200}, config={
        "agent": {"schedule_workers": {"eligible_agent_types": ["Worker"], "max_startup_timeout_seconds": 300}},
        "agent_types": {"Worker": {"agent": {"schedule_worker_rules": {"startup_timeout_seconds": 600}}}},
    })
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    assert reasons == []
    assert repository.get(record.launch_id, "team").state == AgentLaunchState.STARTING


@pytest.mark.asyncio
async def test_a_scheduled_start_waits_its_schedules_own_startup_timeout(lead, monkeypatch) -> None:
    """600 s set by the schedule, checked at 150 s: not stopped, although the default would be 120 s."""

    repository, record = _configured_schedule_start(lead, monkeypatch, rules={"startup_timeout_seconds": 600}, config={
        "agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}},
    })
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead, utc_now() + timedelta(seconds=150))
    assert reasons == []
    assert repository.get(record.launch_id, "team").state == AgentLaunchState.STARTING


@pytest.mark.asyncio
async def test_switching_scheduled_starts_off_during_a_start_does_not_count(lead, monkeypatch) -> None:
    """An operator setting about scheduled starting, not a failure of the type (critique C15)."""

    _use_config(monkeypatch, {"agent": {"schedule_workers": {"enabled": False, "eligible_agent_types": ["Worker"]}}})
    repository, record = _starting_record(lead, source="schedule", schedule_id="removed")
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    stored = repository.get(record.launch_id, "team")
    assert reasons == ["agent_type_ineligible_for_automatic_launch"]
    assert start_history([stored], stored.named_agent_type).failures_in_row == 0


# -- how a worker thread's end is logged when its own record write fails -------------


def _thread_gone(record, timeout: float = 10.0) -> None:
    from custom_agents import strands_team

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with strands_team._SPAWNED_AGENT_LOCK:
            if record.agent_id not in strands_team._SPAWNED_AGENTS:
                return
        time.sleep(0.02)
    raise AssertionError("the worker thread did not end")


def _exit_lines(caplog) -> tuple[list[logging.LogRecord], list[logging.LogRecord]]:
    recorded = [r for r in caplog.records if "event=worker_exit_recorded_by_launcher" in r.getMessage()]
    errors = [r for r in caplog.records if r.getMessage() == "Could not record worker exit"]
    return recorded, errors


def _move_record_to_unknown(worker) -> None:
    """What the launcher does to a start whose lease ran out: a write that advances the fence."""

    repository = AgentLaunchRepository(worker.task_manager.storage_settings)
    current = repository.get(worker._worker_launch.launch_id, "team")
    repository.update(current, state=AgentLaunchState.UNKNOWN, reason=LAUNCHER_RECONCILIATION)


@pytest.mark.asyncio
async def test_a_timed_out_worker_that_outlives_the_join_logs_that_the_launcher_recorded_it(lead, monkeypatch, caplog) -> None:
    """The real launcher, the real stop and a real thread: one info line, no error with a traceback."""

    from custom_agents import strands_team

    async def leaves_slowly(worker) -> None:
        await worker.get_exit_event().wait()
        await asyncio.sleep(1.5)  # longer than the launcher's 1 s join

    _use_config(monkeypatch, {})
    repository, record, _ = _spawn(lead, monkeypatch, leaves_slowly)
    _running_loop(record)
    lead._local_team_runtime.inspect_launch = strands_team.inspect_worker_launch
    lead._local_team_runtime.stop_launch = strands_team.stop_worker_launch
    with caplog.at_level(logging.INFO):
        await _reconcile(lead, utc_now() + timedelta(seconds=150))
        _thread_gone(record)
    recorded, errors = _exit_lines(caplog)
    assert errors == []
    (line,) = recorded
    assert line.levelno == logging.INFO and "reason=startup_timed_out" in line.getMessage()
    stored = repository.get(record.launch_id, "team")
    assert (stored.state, stored.reason) == (AgentLaunchState.UNKNOWN, STARTUP_TIMED_OUT)


def test_a_ready_worker_whose_end_nothing_recorded_is_an_error(lead, monkeypatch, caplog) -> None:
    """The launcher moved a ready worker's record to unknown; the runtime then stops it (critique C14)."""

    from custom_agents import strands_team

    async def ready_then_lease_lost(worker) -> None:
        await worker.get_exit_event().wait()
        worker._worker_ready = True
        _move_record_to_unknown(worker)

    repository, record, _ = _spawn(lead, monkeypatch, ready_then_lease_lost)
    _running_loop(record)
    with caplog.at_level(logging.INFO):
        strands_team.shutdown_spawned_agents("team", "lead", 5.0)
        _thread_gone(record)
    recorded, errors = _exit_lines(caplog)
    assert recorded == [] and len(errors) == 1 and errors[0].levelno == logging.ERROR


def test_a_lost_exit_write_without_a_stop_is_an_error(lead, monkeypatch, caplog) -> None:
    async def record_changed_underneath(worker) -> None:
        _move_record_to_unknown(worker)
        raise RuntimeError("crashed")

    with caplog.at_level(logging.INFO):
        repository, record, _ = _spawn(lead, monkeypatch, record_changed_underneath)
        _thread_gone(record)
    recorded, errors = _exit_lines(caplog)
    assert recorded == [] and len(errors) == 1


def test_an_exit_write_that_fails_otherwise_is_an_error(lead, monkeypatch, caplog) -> None:
    def store_down(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("custom_agents.agent_instantiation_service.record_worker_exit", store_down)

    async def refused(_worker):
        raise BadRequestError(code=400, err_code=10026, description="maximum consumers limit reached")

    with caplog.at_level(logging.INFO):
        repository, record, _ = _spawn(lead, monkeypatch, refused)
        _thread_gone(record)
    recorded, errors = _exit_lines(caplog)
    assert recorded == [] and len(errors) == 1


@pytest.mark.asyncio
async def test_a_scheduled_start_whose_types_own_rule_is_refused_counts(lead, monkeypatch) -> None:
    """A saved schedule that overrides another rule: the type's own refused rule still counts (critique C16)."""

    refused = "schedule_worker_rule_exceeds_operator_limit:startup_timeout_seconds"
    repository, record = _configured_schedule_start(lead, monkeypatch, rules={"claim_wait_seconds": 30}, config={
        "agent": {"schedule_workers": {"eligible_agent_types": ["Worker"], "max_startup_timeout_seconds": 300}},
        "agent_types": {"Worker": {"agent": {"schedule_worker_rules": {"startup_timeout_seconds": 600}}}},
    })
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead)
    stored = repository.get(record.launch_id, "team")
    assert reasons == [refused]
    assert start_history([stored], stored.named_agent_type).failures_in_row == 1


@pytest.mark.asyncio
async def test_a_schedules_own_short_startup_timeout_does_not_count_against_the_type(lead, monkeypatch) -> None:
    """Review finding GEN-1 (probe test_probe_schedule_timeout): the schedule's 5 s ran out, not the type's 120 s."""

    repository, record = _configured_schedule_start(
        lead, monkeypatch, rules={"startup_timeout_seconds": 5},
        config={"agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}}},
    )
    reasons = _launcher_stops(lead, stopped=True)
    now = utc_now() + timedelta(seconds=10)
    await _reconcile(lead, now)
    stored = repository.get(record.launch_id, "team")
    assert reasons == [SCHEDULE_STARTUP_TIMED_OUT]
    assert (stored.state, stored.reason) == (AgentLaunchState.FAILED, SCHEDULE_STARTUP_TIMED_OUT)
    history = start_history([stored], "Worker", since=now - timedelta(hours=1))
    assert history.failures_in_row == 0
    observation = _observation(agent_type="Worker", waiting=3, oldest_wait_seconds=60.0,
                               failed_starts_in_row=history.failures_in_row,
                               start_retry_in_seconds=start_retry_in(history, now))
    assert decide(observation, team_active=0, team_limit_value=32, settings=WorkerPoolsConfig()).starts == 1


@pytest.mark.asyncio
async def test_a_scheduled_start_past_its_types_own_timeout_still_counts(lead, monkeypatch) -> None:
    repository, record = _configured_schedule_start(
        lead, monkeypatch, rules={"startup_timeout_seconds": 5},
        config={"agent": {"schedule_workers": {"eligible_agent_types": ["Worker"]}}},
    )
    reasons = _launcher_stops(lead, stopped=True)
    await _reconcile(lead, utc_now() + timedelta(seconds=130))  # past the type's own 120 s too
    stored = repository.get(record.launch_id, "team")
    assert reasons == [STARTUP_TIMED_OUT]
    assert start_history([stored], "Worker").failures_in_row == 1
