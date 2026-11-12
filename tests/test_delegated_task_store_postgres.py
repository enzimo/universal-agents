"""Delegated-task writes take turns on PostgreSQL (independent review of PR 116, R1).

Every write of ``DelegatedTaskRepository`` reads a row, changes it and saves it
whole. Before the store took its own write lock in PostgreSQL, two connections
could both read before either saved: a check-back take cleared a later time
another connection had just saved, two takers both got the same due time, and
two setters creating one wait kept only one setter's field.

Each race runs against a real PostgreSQL server with nothing in SQL mocked. The
first caller pauses right after its real read of the row. The second caller runs
on its own connection, with its own ``application_name``, and the test waits
until PostgreSQL shows that connection waiting on an advisory lock, or until the
second call has finished, which is what happened without the lock. Then the
first caller goes on. Events and the server's own view decide the order, never a
fixed sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any
from uuid import uuid4

import pytest

from storage.config import StorageSettings, resolve_storage_settings
from storage.delegated_task_repository import DELEGATED_FINISHED, DELEGATED_RUNNING, DelegatedTaskRepository
from storage.postgres_connection import connect_postgres
from tests.delegated_task_support import delegated_task_storage

pytestmark = [pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]

TEAM = "team_a"
PARENT = "parent_a"
REQUESTER = "requester_a"
# How long the test waits for each step; the connections give up on a lock after 20 s.
STEP_SECONDS = 10


@pytest.fixture()
def settings(tmp_path) -> Iterator[StorageSettings]:
    with delegated_task_storage("postgres", tmp_path) as storage:
        yield storage


@dataclass(frozen=True)
class RaceOutcome:
    first: Any
    second: Any
    # PostgreSQL showed the second caller waiting on an advisory lock while the first was paused.
    second_waited: bool


def _repository(settings: StorageSettings, application_name: str) -> DelegatedTaskRepository:
    from psycopg.conninfo import make_conninfo

    dsn = make_conninfo(settings.postgres_dsn, application_name=application_name)
    return DelegatedTaskRepository(resolve_storage_settings({"backend": "postgres", "postgres": {"dsn": dsn}}))


def _waits_on_advisory_lock(monitor: Any, application_name: str) -> bool:
    waiting = monitor.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name=%s "
        "AND wait_event_type='Lock' AND wait_event='advisory')",
        (application_name,),
    ).fetchone()[0]
    # pg_stat_activity is read once per transaction; end it so the next poll sees the server now.
    monitor.commit()
    return bool(waiting)


def _race(
    settings: StorageSettings,
    *,
    pause_after: str,
    first: Callable[[DelegatedTaskRepository], Any],
    second: Callable[[DelegatedTaskRepository], Any],
) -> RaceOutcome:
    """Run ``first`` paused after its read (``pause_after``), and ``second`` meanwhile on its own connection."""

    first_repository = _repository(settings, "delegated_first_" + uuid4().hex)
    second_name = "delegated_second_" + uuid4().hex
    second_repository = _repository(settings, second_name)
    read, release = Event(), Event()
    original_read = getattr(first_repository, pause_after)

    def read_then_pause(*args: Any, **kwargs: Any) -> Any:
        result = original_read(*args, **kwargs)
        read.set()
        assert release.wait(STEP_SECONDS), "The test never let the paused first caller go on."
        return result

    # Only this instance pauses; the real SELECT still runs.
    setattr(first_repository, pause_after, read_then_pause)
    second_waited = False
    with ThreadPoolExecutor(max_workers=2) as pool, connect_postgres(settings.postgres_dsn) as monitor:
        first_call = pool.submit(first, first_repository)
        try:
            assert read.wait(STEP_SECONDS), "The first caller never read its row."
            second_call = pool.submit(second, second_repository)
            deadline = time.monotonic() + STEP_SECONDS
            while not second_call.done():
                if _waits_on_advisory_lock(monitor, second_name):
                    second_waited = True
                    break
                if time.monotonic() > deadline:
                    pytest.fail("The second caller neither finished nor waited on the store's lock.")
                time.sleep(0.01)
        finally:
            release.set()
        first_result = first_call.result(timeout=2 * STEP_SECONDS)
        second_result = second_call.result(timeout=2 * STEP_SECONDS)
    return RaceOutcome(first=first_result, second=second_result, second_waited=second_waited)


def _due_wait(repository: DelegatedTaskRepository, at: datetime) -> None:
    repository.set_check_back(team_id=TEAM, parent_task_id=PARENT, requester_agent_id=REQUESTER, at=at)


def _take(at: datetime) -> Callable[[DelegatedTaskRepository], bool]:
    return lambda repository: repository.take_due_check_back(TEAM, PARENT, now=at)


def _set_check_back(at: datetime, requester: str = REQUESTER) -> Callable[[DelegatedTaskRepository], Any]:
    return lambda repository: repository.set_check_back(
        team_id=TEAM, parent_task_id=PARENT, requester_agent_id=requester, at=at
    )


def _record_progress(counts: dict[str, int], at: datetime, requester: str = REQUESTER) -> Callable[[DelegatedTaskRepository], Any]:
    return lambda repository: repository.record_progress(
        team_id=TEAM, parent_task_id=PARENT, requester_agent_id=requester, counts=counts, at=at
    )


def test_a_take_keeps_a_later_check_back_saved_after_its_read(settings):
    """The review's first interleaving: the take read a due time, another connection saved a later one."""

    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    later = due + timedelta(minutes=30)
    repository = _repository(settings, "delegated_setup")
    _due_wait(repository, due)

    outcome = _race(settings, pause_after="_read_wait", first=_take(due), second=_set_check_back(later))

    assert outcome.second_waited
    assert outcome.first is True
    assert repository.get_wait(TEAM, PARENT).check_back_at_utc == later


def test_two_callers_taking_the_same_due_time_take_it_once(settings):
    """The review's second interleaving: two takers both read the due time before either saved."""

    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    repository = _repository(settings, "delegated_setup")
    _due_wait(repository, due)

    outcome = _race(settings, pause_after="_read_wait", first=_take(due), second=_take(due))

    assert outcome.second_waited
    assert (outcome.first, outcome.second) == (True, False)
    assert repository.get_wait(TEAM, PARENT).check_back_at_utc is None


def test_progress_recorded_during_a_take_is_kept(settings):
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    noted = due + timedelta(seconds=30)
    repository = _repository(settings, "delegated_setup")
    _due_wait(repository, due)
    _record_progress({"queued": 2}, due)(repository)

    outcome = _race(
        settings,
        pause_after="_read_wait",
        first=_take(due),
        second=_record_progress({"running": 1}, noted, requester="requester_after_recovery"),
    )

    assert outcome.second_waited
    assert outcome.first is True
    wait = repository.get_wait(TEAM, PARENT)
    assert (wait.check_back_at_utc, wait.last_progress_at_utc, wait.last_progress_counts) == (
        None, noted, {"running": 1}
    )
    # A setter never renames the wait's requester; the wait is found by parent.
    assert wait.requester_agent_id == REQUESTER


@pytest.mark.parametrize("progress_first", [True, False], ids=["progress_then_check_back", "check_back_then_progress"])
def test_two_setters_creating_one_wait_keep_both_fields(settings, progress_first):
    """No wait exists yet, so there is no row to lock: only the store's lock makes them take turns."""

    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    progress, check_back = _record_progress({"queued": 1}, at), _set_check_back(at)
    first, second = (progress, check_back) if progress_first else (check_back, progress)

    outcome = _race(settings, pause_after="_read_wait", first=first, second=second)

    assert outcome.second_waited
    wait = _repository(settings, "delegated_check").get_wait(TEAM, PARENT)
    assert (wait.check_back_at_utc, wait.last_progress_counts) == (at, {"queued": 1})


def test_a_claim_read_before_a_close_never_reopens_the_task(settings):
    """The lock covers the store's record writes too, not only the waits."""

    repository = _repository(settings, "delegated_setup")
    repository.record_published(
        team_id=TEAM,
        task_id="task_a",
        parent_task_id=PARENT,
        root_task_id="root_a",
        requester_agent_id=REQUESTER,
        named_agent_type="Researcher",
        target_agent_name=None,
        publish_sequence=3,
    )

    outcome = _race(
        settings,
        pause_after="_read",
        first=lambda store: store.record_claim(
            team_id=TEAM,
            task_id="task_a",
            worker_agent_id="worker_a",
            worker_agent_name="Worker A",
            claimed_at=datetime.now(timezone.utc),
        ),
        second=lambda store: store.mark_closed(team_id=TEAM, task_id="task_a", status=DELEGATED_FINISHED),
    )

    assert outcome.second_waited
    assert outcome.first.record.status == DELEGATED_RUNNING
    record = repository.get(TEAM, "task_a")
    assert (record.status, record.holder_agent_id) == (DELEGATED_FINISHED, "worker_a")
