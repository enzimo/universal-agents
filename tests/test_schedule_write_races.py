"""Schedule changes never overwrite newer state, and runs never repeat (2026-10-17).

Real SQLite and PostgreSQL schedule stores. Each race is driven deterministically:
the concurrent change happens between a caller's read and its write. Before this
change, a save replaced the whole row with the copy read earlier, so a run
recording its notes brought back a deleted schedule, undid a pause or a
turn-off and cleared another worker's claim; an edit racing a reminder's last
run recreated the reminder so it could never finish; a pause during a run left
the run's occurrence due; and a daily schedule in the hour that repeats when
clocks go back ran twice.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from custom_agents import schedule_store as schedule_store_module
from custom_agents.schedule_changes import (
    ScheduleChangeRequestV1,
    ScheduleUpdateV1,
    _updated_candidate,
    prepare_schedule_change,
)
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    SystemScheduleMutationDenied,
    build_schedule_mutation_permit,
)
from custom_agents.schedule_store import (
    SCHEDULE_CHANGED,
    SCHEDULE_GONE,
    SCHEDULE_OWNER_CHANGED,
    AdvanceOutcome,
    OwnerGuard,
    ScheduleStore,
    ScheduleWriteConflict,
    compute_next_fire_at,
    schedule_record_digest,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from storage.config import resolve_storage_settings
from storage.schedule_repository import RowWrite

OWNER = "person_a"
TEAM = "team"
WORKER_TYPE = "Researcher"
HOUR = timedelta(hours=1)


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def store(tmp_path, request):
    if request.param == "sqlite":
        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "runtime.db")}})
        yield ScheduleStore(storage_settings=settings, default_cron_timezone="UTC")
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL schedule races.")
    schema = "schedule_races_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    settings = resolve_storage_settings(
        {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
    )
    try:
        yield ScheduleStore(storage_settings=settings, default_cron_timezone="UTC")
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _schedule(**changes) -> ScheduledMessage:
    fields = dict(
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check the feed"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=OWNER)),
        interval_ms=3_600_000,
        repeat_count=None,
        next_fire_at=(_now() + HOUR).isoformat(),
        claim_team_id=TEAM,
        claim_named_agent_type=WORKER_TYPE,
    )
    fields.update(changes)
    return ScheduledMessage(**fields)


def _due(store: ScheduleStore, **changes) -> str:
    """Add a schedule that is due now; return its id."""

    schedule = _schedule(next_fire_at=(_now() - timedelta(seconds=5)).isoformat(), **changes)
    store.add(schedule)
    return schedule.schedule_id


def _claim(store: ScheduleStore, agent_id: str = "worker_b", agent_type: str = WORKER_TYPE) -> dict:
    claimed = store.claim_next_due(claimer_agent_id=agent_id, claimer_named_agent_type=agent_type, claimer_team_id=TEAM)
    assert claimed is not None
    return claimed


def _add_note(memory: ScheduleTaskMemory) -> bool:
    memory.ledger.total_notifications += 1
    memory.state.summary = "run notes"
    return True


# -- finding 1: run notes and edits never overwrite newer state ---------------------------


def test_run_notes_never_recreate_a_schedule_deleted_after_it_was_read(store):
    schedule = _schedule()
    store.add(schedule)
    read = store.get_scheduled_message(schedule.schedule_id)  # the tool's ownership read
    assert store.remove(schedule.schedule_id)

    result = store.edit_schedule_memory(
        schedule.schedule_id, _add_note, owner_guard=OwnerGuard(read.payload_meta.gateway_context.canonical_user_id)
    )

    assert result is None
    assert store.get_row(schedule.schedule_id) is None


@pytest.mark.parametrize("control", ["disable", "pause"])
def test_run_notes_keep_a_turn_off_or_pause_made_after_the_read(store, control):
    schedule = _schedule()
    store.add(schedule)
    store.get_scheduled_message(schedule.schedule_id)  # the tool's read
    until = (_now() + timedelta(days=1)).isoformat()
    if control == "disable":
        assert store.set_enabled(schedule.schedule_id, False)
    else:
        assert store.set_paused_until(schedule.schedule_id, until)

    assert store.edit_schedule_memory(schedule.schedule_id, _add_note) is True

    saved = store.get_scheduled_message(schedule.schedule_id)
    assert saved.schedule_memory.state.summary == "run notes"
    if control == "disable":
        assert saved.enabled is False
    else:
        assert saved.paused_until == until


def test_run_notes_keep_another_workers_claim_and_its_advance_applies(store):
    schedule_id = _due(store)
    claimed = _claim(store)

    store.edit_schedule_memory(schedule_id, _add_note)

    row = store.get_row(schedule_id)
    assert (row["claimed_by_agent_id"], row["claim_token"]) == ("worker_b", claimed["claim_token"])
    outcome = store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )
    assert outcome is AdvanceOutcome.ADVANCED
    assert store.get_row(schedule_id)["next_fire_at"] > claimed["next_fire_at"]
    assert store.claim_next_due(claimer_agent_id="worker_c", claimer_named_agent_type=WORKER_TYPE, claimer_team_id=TEAM) is None


def test_run_notes_are_refused_when_the_schedule_changed_hands(store):
    schedule = _schedule()
    store.add(schedule)

    with pytest.raises(ScheduleWriteConflict) as refused:
        store.edit_schedule_memory(schedule.schedule_id, _add_note, owner_guard=OwnerGuard("someone_else"))

    assert refused.value.reason_code == SCHEDULE_OWNER_CHANGED
    assert store.get_scheduled_message(schedule.schedule_id).schedule_memory is None


def test_concurrent_run_notes_lose_no_update(store):
    schedule = _schedule()
    store.add(schedule)
    writers = 6
    barrier = Barrier(writers)

    def note(_index: int) -> None:
        barrier.wait(timeout=10)
        store.edit_schedule_memory(schedule.schedule_id, _add_note)

    with ThreadPoolExecutor(max_workers=writers) as pool:
        list(pool.map(note, range(writers)))

    assert store.get_scheduled_message(schedule.schedule_id).schedule_memory.ledger.total_notifications == writers


def test_an_edit_racing_a_reminders_last_run_never_recreates_it(store):
    reminder_id = _due(store, interval_ms=None, repeat_count=1, claim_named_agent_type="TeamArchitect")
    request = ScheduleChangeRequestV1(
        schedule_id=reminder_id,
        action="update",
        update=ScheduleUpdateV1(instruction="Remind me to call the bank about the card"),
    )
    prepared = prepare_schedule_change(store, request, person_id=OWNER)
    claimed = _claim(store, "lead", "TeamArchitect")
    assert store.update_after_fire(
        reminder_id, claim_token=claimed["claim_token"], claimer_agent_id="lead", due_at=claimed["next_fire_at"]
    ) is AdvanceOutcome.FINISHED

    with pytest.raises(ScheduleWriteConflict) as refused:
        prepared.commit(None)

    assert refused.value.reason_code == SCHEDULE_GONE
    assert store.get_row(reminder_id) is None


def test_an_edit_is_refused_when_the_person_paused_the_schedule_after_it_was_read(store):
    schedule = _schedule()
    store.add(schedule)
    request = ScheduleChangeRequestV1(
        schedule_id=schedule.schedule_id, action="update", update=ScheduleUpdateV1(instruction="New wording")
    )
    prepared = prepare_schedule_change(store, request, person_id=OWNER)
    until = (_now() + timedelta(days=1)).isoformat()
    assert store.set_paused_until(schedule.schedule_id, until)

    with pytest.raises(ScheduleWriteConflict) as refused:
        prepared.commit(None)

    assert refused.value.reason_code == SCHEDULE_CHANGED
    saved = store.get_scheduled_message(schedule.schedule_id)
    assert (saved.paused_until, saved.payload.problem_statement) == (until, "Check the feed")


def test_an_edit_keeps_the_run_time_count_and_notes_a_run_wrote_after_the_read(store):
    schedule_id = _due(store, repeat_count=5)
    request = ScheduleChangeRequestV1(
        schedule_id=schedule_id, action="update", update=ScheduleUpdateV1(instruction="New wording")
    )
    prepared = prepare_schedule_change(store, request, person_id=OWNER)
    claimed = _claim(store)
    store.edit_schedule_memory(schedule_id, _add_note)
    store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )
    advanced = store.get_row(schedule_id)

    prepared.commit(None)

    saved = store.get_scheduled_message(schedule_id)
    row = store.get_row(schedule_id)
    assert saved.payload.problem_statement == "New wording"
    assert row["next_fire_at"] == advanced["next_fire_at"] != claimed["next_fire_at"]
    assert row["remaining_count"] == 4
    assert saved.schedule_memory.state.summary == "run notes"


def test_a_notify_policy_change_keeps_run_notes_written_after_the_read(store):
    schedule = _schedule(schedule_memory=ScheduleTaskMemory())
    store.add(schedule)
    request = ScheduleChangeRequestV1(
        schedule_id=schedule.schedule_id, action="update", update=ScheduleUpdateV1(notify="always")
    )
    prepared = prepare_schedule_change(store, request, person_id=OWNER)
    store.edit_schedule_memory(schedule.schedule_id, _add_note)  # a run writes its notes

    prepared.commit(None)

    memory = store.get_scheduled_message(schedule.schedule_id).schedule_memory
    assert memory.policy.notify == "always"
    assert (memory.state.summary, memory.ledger.total_notifications) == ("run notes", 1)


def test_an_edit_that_sets_timing_or_the_count_writes_them(store):
    schedule_id = _due(store, repeat_count=5)
    claimed = _claim(store)
    store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )
    request = ScheduleChangeRequestV1(
        schedule_id=schedule_id,
        action="update",
        update=ScheduleUpdateV1(interval_seconds=600, repeat_count=5),
    )
    prepared = prepare_schedule_change(store, request, person_id=OWNER)

    prepared.commit(None)

    row = store.get_row(schedule_id)
    assert row["interval_ms"] == 600_000
    assert row["next_fire_at"] == prepared.candidate.next_fire_at
    assert row["remaining_count"] == 5  # setting the count again starts it again


def test_installing_over_a_schedule_keeps_its_claim_and_count(store):
    schedule_id = _due(store, repeat_count=5)
    claimed = _claim(store)
    store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )
    install = store.get_scheduled_message(schedule_id).model_copy(deep=True)
    install.payload.problem_statement = "Installed again"
    install.next_fire_at = (_now() - timedelta(seconds=1)).isoformat()
    store.add(install)
    claim = _claim(store)

    store.add(install.model_copy(update={"schedule_name": "renamed"}))

    row = store.get_row(schedule_id)
    assert row["claim_token"] == claim["claim_token"]
    assert row["remaining_count"] == 4
    assert row["schedule_name"] == "renamed"


def test_a_system_schedule_change_is_refused_when_a_run_moved_it_after_the_permit(store):
    schedule = _schedule(
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        schedule_name="heartbeat:team",
        claim_named_agent_type="TeamArchitect",
        next_fire_at=(_now() - timedelta(seconds=5)).isoformat(),
    )
    store.add(schedule)
    base = store.get_scheduled_message(schedule.schedule_id)
    candidate = base.model_copy(deep=True)
    candidate.payload.problem_statement = "Changed by an approved change"
    permit = build_schedule_mutation_permit(
        schedule=base,
        operation=ScheduleMutationOperation.UPDATE,
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id="runtime",
        owner_canonical_user_id=OWNER,
        team_id=TEAM,
        candidate=candidate,
    )
    claimed = _claim(store, "lead", "TeamArchitect")
    store.update_after_fire(
        schedule.schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="lead", due_at=claimed["next_fire_at"]
    )

    with pytest.raises(SystemScheduleMutationDenied):
        store.update(candidate, base=base, system_mutation_permit=permit)

    assert store.get_scheduled_message(schedule.schedule_id).payload.problem_statement == "Check the feed"


@pytest.mark.parametrize("action", ["disable", "pause", "resume", "remove"])
def test_a_person_acts_only_on_the_schedule_they_saw_listed(store, action):
    schedule = _schedule()
    store.add(schedule)
    listed = schedule_record_digest(store.get_scheduled_message(schedule.schedule_id))
    base = store.get_scheduled_message(schedule.schedule_id)
    edited = base.model_copy(deep=True)
    edited.payload.problem_statement = "Check another feed"
    store.update(edited, base=base)  # someone changed what the person saw
    calls = {
        "disable": lambda digest: store.set_enabled(schedule.schedule_id, False, expected_record_digest=digest),
        "pause": lambda digest: store.set_paused_until(
            schedule.schedule_id, (_now() + HOUR).isoformat(), expected_record_digest=digest
        ),
        "resume": lambda digest: store.resume(schedule.schedule_id, expected_record_digest=digest),
        "remove": lambda digest: store.remove(schedule.schedule_id, expected_record_digest=digest),
    }

    with pytest.raises(ScheduleWriteConflict) as refused:
        calls[action](listed)
    assert refused.value.reason_code == SCHEDULE_CHANGED
    assert store.get_row(schedule.schedule_id)["enabled"]

    current = schedule_record_digest(store.get_scheduled_message(schedule.schedule_id))
    assert calls[action](current) is True


def test_a_schedule_that_ran_after_it_was_listed_can_still_be_paused(store):
    """The listed digest leaves out the next run time and run notes, which runs change by themselves."""
    schedule_id = _due(store, repeat_count=5)
    listed = schedule_record_digest(store.get_scheduled_message(schedule_id))
    claimed = _claim(store)
    store.edit_schedule_memory(schedule_id, _add_note)
    store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )

    assert store.set_paused_until(schedule_id, (_now() + HOUR).isoformat(), expected_record_digest=listed) is True


def test_the_listed_digest_covers_a_one_time_run_time_and_the_notes_settings_but_not_the_notes(store):
    one_time = _schedule(interval_ms=None, repeat_count=1, schedule_memory=ScheduleTaskMemory())
    store.add(one_time)
    listed = schedule_record_digest(store.get_scheduled_message(one_time.schedule_id))

    store.edit_schedule_memory(one_time.schedule_id, _add_note)  # notes content: not covered
    assert schedule_record_digest(store.get_scheduled_message(one_time.schedule_id)) == listed

    def notify_always(memory: ScheduleTaskMemory) -> bool:
        memory.policy.notify = "always"
        return True

    store.edit_schedule_memory(one_time.schedule_id, notify_always)  # a notes setting: covered
    after_policy = schedule_record_digest(store.get_scheduled_message(one_time.schedule_id))
    assert after_policy != listed

    base = store.get_scheduled_message(one_time.schedule_id)
    moved = base.model_copy(deep=True)
    moved.next_fire_at = (_now() + timedelta(days=3)).isoformat()
    store.update(moved, base=base)  # a one-time run time: covered, runs never move it
    assert schedule_record_digest(store.get_scheduled_message(one_time.schedule_id)) != after_policy
    with pytest.raises(ScheduleWriteConflict):
        store.set_enabled(one_time.schedule_id, False, expected_record_digest=after_policy)


def test_two_first_saves_of_one_schedule_never_collide(store):
    schedule = _schedule()
    barrier = Barrier(2)

    def save(name: str) -> None:
        barrier.wait(timeout=10)
        store.add(schedule.model_copy(deep=True, update={"schedule_name": name}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, ["first", "second"]))

    assert store.get_row(schedule.schedule_id)["schedule_name"] in {"first", "second"}


# -- finding 3: control changes keep the claim; the advance writes only its own fields ----


@pytest.mark.parametrize("control", ["disable", "pause", "resume"])
def test_control_changes_keep_a_live_claim(store, control):
    schedule_id = _due(store)
    claimed = _claim(store)
    {
        "disable": lambda: store.set_enabled(schedule_id, False),
        "pause": lambda: store.set_paused_until(schedule_id, (_now() + HOUR).isoformat()),
        "resume": lambda: store.resume(schedule_id),
    }[control]()

    row = store.get_row(schedule_id)
    assert (row["claimed_by_agent_id"], row["claim_token"]) == ("worker_b", claimed["claim_token"])


def test_a_pause_during_a_run_stays_and_the_run_is_not_repeated_on_resume(store):
    schedule_id = _due(store)
    claimed = _claim(store, "lead")
    until = (_now() + timedelta(minutes=10)).isoformat()
    assert store.set_paused_until(schedule_id, until)  # after admission, before the advance

    outcome = store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="lead", due_at=claimed["next_fire_at"]
    )

    row = store.get_row(schedule_id)
    assert outcome is AdvanceOutcome.ADVANCED
    assert row["paused_until"] == until
    assert row["next_fire_at"] > claimed["next_fire_at"]
    assert store.resume(schedule_id)
    assert store.claim_next_due(claimer_agent_id="lead", claimer_named_agent_type=WORKER_TYPE, claimer_team_id=TEAM) is None


def test_a_new_run_time_set_during_a_run_wins_and_the_claim_is_released(store):
    schedule_id = _due(store)
    claimed = _claim(store)
    base = store.get_scheduled_message(schedule_id)
    candidate = base.model_copy(deep=True)
    candidate.next_fire_at = (_now() + timedelta(days=2)).isoformat()
    store.update(candidate, base=base)

    outcome = store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )

    row = store.get_row(schedule_id)
    assert outcome is AdvanceOutcome.DUE_CHANGED
    assert row["next_fire_at"] == candidate.next_fire_at
    assert row["claimed_by_agent_id"] is None


def _retime_during_run(store: ScheduleStore, schedule_id: str) -> tuple[dict, str]:
    """Claim the due run, then let the person move the next run time while it runs; (claim, new time)."""

    claimed = _claim(store)
    base = store.get_scheduled_message(schedule_id)
    candidate = base.model_copy(deep=True)
    candidate.next_fire_at = (_now() + timedelta(days=1)).isoformat()
    store.update(candidate, base=base)
    return claimed, candidate.next_fire_at


def test_a_run_re_timed_during_its_run_still_counts_while_its_series_has_runs_left(store):
    schedule_id = _due(store, repeat_count=3)
    claimed, new_time = _retime_during_run(store, schedule_id)

    outcome = store.update_after_fire(
        schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )

    row = store.get_row(schedule_id)
    assert outcome is AdvanceOutcome.DUE_CHANGED
    assert (row["remaining_count"], row["next_fire_at"], row["claimed_by_agent_id"]) == (2, new_time, None)


@pytest.mark.parametrize("kind", ["one_time_reminder", "series_on_its_last_run"])
def test_a_schedule_re_timed_during_its_last_run_stays_at_the_new_time(store, caplog, kind):
    """The person's latest instruction wins (owner's decision 2026-10-17): a reminder moved to tomorrow stays."""
    if kind == "one_time_reminder":
        schedule_id = _due(store, interval_ms=None, repeat_count=1)
    else:
        schedule_id = _due(store, repeat_count=3)
        store._repository.write_locked(schedule_id, lambda _row: RowWrite(columns={"remaining_count": 1}))
    claimed, new_time = _retime_during_run(store, schedule_id)

    with caplog.at_level(logging.INFO, logger=schedule_store_module.__name__):
        outcome = store.update_after_fire(
            schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
        )

    row = store.get_row(schedule_id)
    assert outcome is AdvanceOutcome.DUE_CHANGED
    assert (row["remaining_count"], row["next_fire_at"], row["claimed_by_agent_id"]) == (1, new_time, None)
    assert "event=schedule_retimed_during_final_run_kept" in caplog.text
    assert f"schedule_id={schedule_id}" in caplog.text


def test_an_advance_by_an_agent_that_lost_its_claim_is_refused(store):
    schedule_id = _due(store)
    claimed = _claim(store)

    outcome = store.update_after_fire(
        schedule_id, claim_token="someone_elses_token", claimer_agent_id="worker_x", due_at=claimed["next_fire_at"]
    )

    assert outcome is AdvanceOutcome.CLAIM_LOST
    assert store.get_row(schedule_id)["next_fire_at"] == claimed["next_fire_at"]


@pytest.mark.parametrize(("repeat_count", "warned"), [(1, False), (None, True)])
def test_a_one_time_schedule_finishes_after_its_run_and_a_count_left_is_logged(store, caplog, repeat_count, warned):
    """repeat_count=1 is a normal one-time run; a stored one with runs left (an older update made them) is logged."""
    schedule_id = _due(store, interval_ms=None, repeat_count=repeat_count)
    claimed = _claim(store)

    with caplog.at_level(logging.WARNING, logger=schedule_store_module.__name__):
        outcome = store.update_after_fire(
            schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
        )

    assert outcome is AdvanceOutcome.FINISHED
    assert store.get_row(schedule_id) is None
    assert ("event=schedule_one_time_count_ignored" in caplog.text) is warned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "level"),
    [
        (AdvanceOutcome.DUE_CHANGED, logging.INFO),
        (AdvanceOutcome.NOT_FOUND, logging.INFO),
        (AdvanceOutcome.CLAIM_LOST, logging.WARNING),
        (AdvanceOutcome.FAILED, None),  # the store logged it with its traceback
        (AdvanceOutcome.ADVANCED, None),
    ],
)
async def test_the_scheduler_logs_an_advance_that_did_not_apply_once(caplog, outcome, level):
    from custom_agents.scheduled_runtime import _advance_after_admission

    calls = []

    async def run(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    def update_after_fire(schedule_id, **kwargs):
        calls.append((schedule_id, kwargs))
        return outcome

    agent = SimpleNamespace(
        id="worker_b",
        task_manager=SimpleNamespace(operations_runtime=SimpleNamespace(run=run)),
        _schedule_store=SimpleNamespace(update_after_fire=update_after_fire),
        _logger=logging.getLogger("schedule-advance-test"),
    )
    claimed = {"schedule_id": "sched_1", "claim_token": "token_1", "next_fire_at": "2026-10-17T09:00:00+00:00"}

    with caplog.at_level(logging.DEBUG, logger="schedule-advance-test"):
        await _advance_after_admission(agent, claimed)

    assert calls == [
        ("sched_1", {"claim_token": "token_1", "claimer_agent_id": "worker_b", "due_at": "2026-10-17T09:00:00+00:00"})
    ]
    logged = [record for record in caplog.records if "event=schedule_advance_not_applied" in record.getMessage()]
    assert [record.levelno for record in logged] == ([level] if level is not None else [])
    if logged:
        assert "schedule_id=sched_1" in logged[0].getMessage()
        assert f"outcome={outcome.value}" in logged[0].getMessage()


def test_every_deletion_by_the_store_ends_the_schedules_access(store, monkeypatch):
    """Removing a finished schedule after its run, and pruning an ended one, end its access like remove does."""
    ended = []
    monkeypatch.setattr(store, "_end_schedule_access", ended.append)
    finished_id = _due(store, interval_ms=None, repeat_count=1)
    claimed = _claim(store)
    store.update_after_fire(
        finished_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b", due_at=claimed["next_fire_at"]
    )
    ended_schedule = _schedule(expires_at_utc=(_now() + timedelta(seconds=1)).isoformat())
    store.add(ended_schedule)
    removed = _schedule()
    store.add(removed)

    assert store.prune_expired(now=_now() + timedelta(minutes=1)) == 1
    assert store.remove(removed.schedule_id)

    assert ended == [finished_id, ended_schedule.schedule_id, removed.schedule_id]


# -- finding 4: a missed one-time run is turned off only while it still waits -------------


def test_a_missed_run_is_turned_off_only_while_it_is_still_waiting_unclaimed(store):
    schedule_id = _due(store, interval_ms=None, repeat_count=1)
    due = store.get_row(schedule_id)["next_fire_at"]
    now = _now()

    assert store.turn_off_missed_run(schedule_id, due_at="2020-01-01T00:00:00+00:00", now=now) is False
    claimed = _claim(store)
    assert store.turn_off_missed_run(schedule_id, due_at=due, now=now) is False
    assert store.get_row(schedule_id)["enabled"]

    assert store.release_claim(schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="worker_b")
    assert store.turn_off_missed_run(schedule_id, due_at=due, now=now) is True
    assert not store.get_row(schedule_id)["enabled"]


@pytest.mark.parametrize("change", ["count_raised", "paused"])
def test_a_missed_run_is_not_turned_off_when_its_count_was_raised_or_it_was_paused(store, change):
    schedule_id = _due(store, interval_ms=None, repeat_count=1, claim_named_agent_type="TeamArchitect")
    due = store.get_row(schedule_id)["next_fire_at"]
    if change == "count_raised":
        store._repository.write_locked(schedule_id, lambda _row: RowWrite(columns={"remaining_count": 2}))
    else:
        assert store.set_paused_until(schedule_id, (_now() + HOUR).isoformat())

    assert store.turn_off_missed_run(schedule_id, due_at=due, now=_now()) is False
    assert store.get_row(schedule_id)["enabled"]


def test_a_missed_run_whose_claim_expired_is_turned_off(store):
    schedule_id = _due(store, interval_ms=None, repeat_count=1)
    due = store.get_row(schedule_id)["next_fire_at"]
    _claim(store)

    assert store.turn_off_missed_run(schedule_id, due_at=due, now=_now() + timedelta(minutes=5)) is True


def test_a_system_schedule_is_never_turned_off_as_missed(store):
    schedule = _schedule(
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.OBJECTIVE_WORK_SESSION,
        schedule_name="objective_work_session:o1",
        interval_ms=None,
        repeat_count=1,
        next_fire_at=(_now() - HOUR).isoformat(),
    )
    store.add(schedule)

    with pytest.raises(SystemScheduleMutationDenied):
        store.turn_off_missed_run(schedule.schedule_id, due_at=schedule.next_fire_at, now=_now())
    assert store.get_row(schedule.schedule_id)["enabled"]


# -- finding 2: daylight saving changes ------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "cron", "first_run", "next_run"),
    [
        # New York goes back from 02:00 EDT to 01:00 EST on 2026-11-01.
        ("America/New_York", "30 1 * * *", "2026-11-01T05:30:00+00:00", "2026-11-02T06:30:00+00:00"),
        # Berlin goes back from 03:00 CEST to 02:00 CET on 2026-10-25.
        ("Europe/Berlin", "30 2 * * *", "2026-10-25T00:30:00+00:00", "2026-10-26T01:30:00+00:00"),
    ],
)
def test_a_daily_run_in_the_repeated_hour_runs_once_when_clocks_go_back(zone, cron, first_run, next_run):
    previous = datetime.fromisoformat(first_run)

    computed = compute_next_fire_at(
        cron_expression=cron, cron_timezone=zone, from_time=previous + timedelta(seconds=5), previous_fire_at=previous
    )

    assert computed == next_run


def _runs_from(cron: str, start: str, count: int, zone: str = "America/New_York") -> list[str]:
    """The next ``count`` run times as the scheduler chains them, each computed after the previous run."""

    previous = datetime.fromisoformat(start)
    found = []
    for _ in range(count):
        run = compute_next_fire_at(
            cron_expression=cron,
            cron_timezone=zone,
            from_time=previous + timedelta(seconds=5),
            previous_fire_at=previous,
        )
        found.append(run)
        previous = datetime.fromisoformat(run)
    return found


@pytest.mark.parametrize(
    ("cron", "runs"),
    [
        # Wildcard hours run every real hour, the repeated 01:xx (05:xx and 06:xx UTC) included.
        ("0 * * * *", ["04:00", "05:00", "06:00", "07:00"]),
        ("15 * * * *", ["04:15", "05:15", "06:15", "07:15"]),
        ("*/30 * * * *", ["04:00", "04:30", "05:00", "05:30", "06:00", "06:30"]),
    ],
)
def test_wildcard_and_step_schedules_run_every_real_hour_when_clocks_go_back(cron, runs):
    # New York goes back from 02:00 EDT to 01:00 EST at 06:00 UTC on 2026-11-01.
    found = _runs_from(cron, "2026-11-01T03:59:00+00:00", len(runs))

    assert [run[11:16] for run in found] == runs
    assert all(run.startswith("2026-11-01") for run in found)


@pytest.mark.parametrize(
    ("cron", "runs"),
    [
        # Two fixed times in the repeated hour: each once that night, then the next day.
        (
            "0,30 1 * * *",
            ["2026-11-01T05:00:00+00:00", "2026-11-01T05:30:00+00:00", "2026-11-02T06:00:00+00:00"],
        ),
        # Weekly on Sunday 01:30: 2026-11-01 is a Sunday.
        ("30 1 * * 0", ["2026-11-01T05:30:00+00:00", "2026-11-08T06:30:00+00:00"]),
        # An hour range is fixed, as in classic cron: 01:00 once, then 02:00.
        (
            "0 1-2 * * *",
            ["2026-11-01T05:00:00+00:00", "2026-11-01T07:00:00+00:00", "2026-11-02T06:00:00+00:00"],
        ),
    ],
)
def test_fixed_time_schedules_run_once_per_time_when_clocks_go_back(cron, runs):
    assert _runs_from(cron, "2026-11-01T04:00:00+00:00", len(runs)) == runs


def test_a_daily_run_in_the_skipped_hour_runs_at_the_first_time_after_it_when_clocks_go_forward():
    # New York goes forward from 02:00 EST to 03:00 EDT on 2026-03-08.
    day_before = datetime.fromisoformat("2026-03-07T07:30:00+00:00")  # 02:30 EST

    change_day = compute_next_fire_at(
        cron_expression="30 2 * * *", cron_timezone="America/New_York", from_time=day_before, previous_fire_at=day_before
    )
    after = datetime.fromisoformat(change_day)
    day_after = compute_next_fire_at(
        cron_expression="30 2 * * *", cron_timezone="America/New_York", from_time=after, previous_fire_at=after
    )

    assert change_day == "2026-03-08T07:00:00+00:00"  # 03:00 EDT: it runs, not skipped
    assert day_after == "2026-03-09T06:30:00+00:00"


def test_the_store_moves_a_fall_back_run_to_the_next_day(store, monkeypatch):
    first_run = "2026-11-01T05:30:00+00:00"
    schedule = _schedule(
        interval_ms=None, cron_expression="30 1 * * *", cron_timezone="America/New_York", next_fire_at=first_run
    )
    store.add(schedule)

    class _AtTheRun(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-11-01T05:30:07+00:00").astimezone(tz)

    monkeypatch.setattr(schedule_store_module, "datetime", _AtTheRun)
    outcome = store.update_after_fire(schedule.schedule_id, due_at=first_run)

    assert outcome is AdvanceOutcome.ADVANCED
    assert store.get_row(schedule.schedule_id)["next_fire_at"] == "2026-11-02T06:30:00+00:00"


# -- related: repeat_count=0 on a one-time schedule -----------------------------------------


@pytest.mark.parametrize(
    ("timing", "requested", "stored"),
    [
        ({"interval_ms": None, "next_fire_at": "2026-10-18T09:00:00+00:00"}, 0, 1),
        ({"interval_ms": None, "next_fire_at": "2026-10-18T09:00:00+00:00"}, 1, 1),
        ({"interval_ms": 60_000}, 0, None),
        ({"interval_ms": 60_000}, 3, 3),
    ],
)
def test_the_repeat_count_of_an_update_follows_the_schedules_timing(timing, requested, stored):
    candidate, changes = _updated_candidate(_schedule(**timing), ScheduleUpdateV1(repeat_count=requested), "UTC")

    assert candidate.repeat_count == changes["repeat_count"] == stored


def test_more_than_one_run_of_a_one_time_schedule_is_refused():
    with pytest.raises(ValueError, match="runs once"):
        _updated_candidate(_schedule(interval_ms=None), ScheduleUpdateV1(repeat_count=3), "UTC")
