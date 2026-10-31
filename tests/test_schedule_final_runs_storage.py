"""A finishing schedule and a failing job's problem row, on SQLite and on PostgreSQL.

The SQL these paths use differs by database (row locks, ``ON CONFLICT ... DO
UPDATE ... WHERE``, the schedule-table check), so the store and snapshot paths
run on both. PostgreSQL needs ``LIVE_TEST_POSTGRES_DSN``; each run uses its own
schema.
"""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from access_control.identity import ENV_SCHEDULER_SIGNING_KEY, ISSUER_SCHEDULER, ContextSigner
from custom_agents.schedule_store import AdvanceOutcome, ScheduleStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.schedule_worker_v1 import ScheduleOccurrenceV1, occurrence_identity, utc_now
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType, ScheduleManagementClass
from storage.config import resolve_storage_settings
from storage.runtime_state_repository import create_runtime_state_repository
from storage.scheduled_run_problems import ScheduledRunProblems
from tests.broker_access_support import forget_task_context_keys, use_scheduler_key
from tests.runtime_snapshot_fixtures import canonical_json

TEAM = "team"
OWNER = dict(owner_agent_id="worker", owner_agent_name="worker", team_id=TEAM, owner_named_agent_type="Worker")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def settings(tmp_path, request, monkeypatch):
    use_scheduler_key(monkeypatch)
    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "rt.db")}})
        forget_task_context_keys()
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN required")
    schema = "final_runs_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        forget_task_context_keys()
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


def _meta(schedule_id: str) -> MessageMeta:
    signer = ContextSigner.from_value(ISSUER_SCHEDULER, os.environ[ENV_SCHEDULER_SIGNING_KEY])
    return MessageMeta(
        requester_type=RequesterType.TIMER_REQUESTER,
        gateway_context=GatewayContext(
            canonical_user_id="owner",
            task_context=signer.issue(team_id=TEAM, person_id="owner", schedule_id=schedule_id),
        ),
    )


def _snapshot(tasks: list[tuple[str, ProgressState]], meta: MessageMeta, *, note: str = "") -> str:
    records = [
        TaskSnapshotRecord(
            task_id=task_id, state=state,
            task=Task(
                task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Check launches",
                execution_history=[TaskHistoryEntry(summary=note)] if note else [],
            ),
            header=MessageHeader(message_id=f"message_{task_id}", timestamp="2026-09-07T00:00:00+00:00"),
            meta=meta,
            owner_person_id=None,
        )
        for task_id, state in tasks
    ]
    queued = [task_id for task_id, state in tasks if state == ProgressState.ACCEPTED]
    return canonical_json(RuntimeSnapshotV3(schema_version=3, tasks=records, task_queue=queued).model_dump(mode="json"))


def _start_final_run(settings, store, runtime, schedule_id: str = "schedule") -> ScheduleOccurrenceV1:
    """A one-time schedule claimed and admitted as a worker's task, then advanced."""

    schedule = ScheduledMessage(
        schedule_id=schedule_id, schedule_name="Launch check", management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check launches"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="owner")),
        claim_team_id=TEAM, claim_named_agent_type="Worker", interval_ms=None, repeat_count=1,
        next_fire_at=(utc_now() - timedelta(minutes=1)).isoformat(),
    )
    store.add(schedule)
    claim = store.claim_next_due(claimer_agent_id="worker", claimer_named_agent_type="Worker", claimer_team_id=TEAM)
    key = occurrence_identity(schedule_id, schedule.next_fire_at)
    occurrence = ScheduleOccurrenceV1(
        occurrence_id=key, schedule_id=schedule_id, task_id="task_" + key, due_at=schedule.next_fire_at,
        team_id=TEAM, owner_canonical_user_id="owner", named_agent_type="Worker", execution_digest="a" * 64,
        agent_id="worker", claim_token=claim["claim_token"],
    )
    runtime.save_task_runtime(
        expect_existing=False, schedule_admission=occurrence,
        runtime_state_json=_snapshot([(occurrence.task_id, ProgressState.ACCEPTED)], _meta(schedule_id)), **OWNER,
    )
    assert store.update_after_fire(
        schedule_id, claim_token=claim["claim_token"], claimer_agent_id="worker", due_at=schedule.next_fire_at,
    ) is AdvanceOutcome.FINISHING
    return occurrence


def test_a_finishing_schedule_waits_for_its_run_and_its_failure_is_recorded_once(settings):
    store, runtime = ScheduleStore(storage_settings=settings), create_runtime_state_repository(settings)
    run = _start_final_run(settings, store, runtime)
    meta = _meta("schedule")
    assert store.prune_expired() == 0
    assert store.get_row("schedule")["remaining_count"] == 0
    assert store.claim_next_due(claimer_agent_id="w2", claimer_named_agent_type="Worker", claimer_team_id=TEAM) is None

    runtime.save_task_runtime(
        expect_existing=True, runtime_state_json=_snapshot([(run.task_id, ProgressState.FAILED)], meta), **OWNER,
    )
    problems = ScheduledRunProblems(settings)
    [problem] = problems.problems(TEAM)
    assert (problem.schedule_id, problem.failed.task_id, problem.failed.schedule_name) == (
        "schedule", run.task_id, "Launch check",
    )
    assert problem.recovered is None
    assert store.prune_expired() == 1 and store.get_row("schedule") is None

    # A later save of the same failure, with its state unchanged, records nothing.
    runtime.save_task_runtime(
        expect_existing=True,
        runtime_state_json=_snapshot([(run.task_id, ProgressState.FAILED)], meta, note="late note"),
        **OWNER,
    )
    assert problems.problems(TEAM) == (problem,)


def test_a_problem_row_keeps_its_first_failure_until_a_run_finishes(settings):
    use = create_runtime_state_repository(settings)
    problems = ScheduledRunProblems(settings)
    meta = _meta("daily")
    saved: list[tuple[str, ProgressState]] = []

    def run(task_id: str, outcome: ProgressState) -> None:
        nonlocal saved
        use.save_task_runtime(
            expect_existing=bool(saved), runtime_state_json=_snapshot([*saved, (task_id, ProgressState.ACCEPTED)], meta),
            **OWNER,
        )
        saved = [*saved, (task_id, outcome)]
        use.save_task_runtime(expect_existing=True, runtime_state_json=_snapshot(saved, meta), **OWNER)

    run("first", ProgressState.FAILED)
    run("second", ProgressState.FAILED)
    [problem] = problems.problems(TEAM)
    assert (problem.failed.task_id, problem.failed.schedule_name, problem.recovered) == ("first", None, None)

    run("works", ProgressState.COMPLETED)
    [problem] = problems.problems(TEAM)
    assert (problem.failed.task_id, problem.recovered.task_id) == ("first", "works")

    run("new", ProgressState.FAILED)
    [problem] = problems.problems(TEAM)
    assert (problem.failed.task_id, problem.recovered) == ("new", None), "a failure after it worked is a new problem"

    problems.forget(problem.model_copy(update={"failed": problem.failed.model_copy(update={"task_id": "first"})}))
    assert problems.problems(TEAM) == (problem,), "forgetting an older problem leaves the newer one"
    problems.forget(problem)
    assert problems.problems(TEAM) == ()


def test_a_receipt_older_than_a_runs_identity_no_longer_keeps_the_schedule(settings):
    store, runtime = ScheduleStore(storage_settings=settings), create_runtime_state_repository(settings)
    _start_final_run(settings, store, runtime)
    assert store.prune_expired(now=datetime.now(timezone.utc) + timedelta(days=6)) == 0
    assert store.prune_expired(now=datetime.now(timezone.utc) + timedelta(days=8)) == 1
