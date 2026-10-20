"""No Objective change is written back over a newer one (2026-10-20).

Real SQLite and PostgreSQL stores (``LIVE_TEST_POSTGRES_DSN``), a real
TaskManager and the team lead's real tools. Before this change the store
replaced the whole record with whatever copy a writer held: an older copy that
changed only the desired outcome put back the old title, and a task link saved
by one team lead process erased the success criterion another had just marked
met. Now every change is applied to the stored row in one write transaction,
and an agent's edit names the version it read and is refused with
``changed_since_read`` when what it decided on changed since.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    ChangeRefusal,
    MessageMetaType,
    ObjectiveCriterionStatus,
    ObjectiveEventType,
    ObjectiveHorizon,
    ObjectivePriority,
    ObjectiveStatus,
    ScheduleManagementClass,
)
from custom_messages.records.objectives import (
    OBJECTIVE_BOOKKEEPING_FIELDS,
    OBJECTIVE_VERSION_FIELDS,
    ObjectiveEventRecord,
    ObjectiveRecord,
    ObjectiveScheduleLink,
    ObjectiveSuccessCriterion,
    objective_version,
)
from custom_tools.providers.objectives import objectives, start_objective_task
from custom_tools.runtime_context import set_agent_ref
from storage.change_ledger import ChangeRefused
from storage.config import resolve_storage_settings
from storage.durable_owner_scope_repository import create_durable_owner_scope_repository
from storage.objective_repository import create_objective_repository
from storage.person_departure_repository import PersonDepartureRepository
from storage.plan_repository import create_plan_repository
from tests.change_approval_support import use_signed_contexts
from tests.test_objective_tools import _add_person_task, _FakeAgent
from tests.tool_result_support import tool_json

SCOPE = "manager_scope:team_1:TeamArchitect"


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def settings(tmp_path, request):
    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}})
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL Objective tests.")
    schema = "objective_versions_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    use_signed_contexts(monkeypatch)


def _repository(settings):
    return create_objective_repository(settings, logger=logging.getLogger("test_objective_versions"))


def _stored(repo, **fields) -> ObjectiveRecord:
    objective = ObjectiveRecord(
        objective_id="objective_1",
        title="Old title",
        desired_outcome="Old outcome",
        why="Asked.",
        horizon=ObjectiveHorizon.FINITE,
        owner_agent_id=SCOPE,
        team_id="team_1",
        success_criteria=[ObjectiveSuccessCriterion(criterion_id="crit_1", description="Tests pass.")],
        **fields,
    )
    repo.insert_objective(objective)
    return objective


def _setter(**fields):
    def change(objective: ObjectiveRecord) -> bool:
        for name, value in fields.items():
            setattr(objective, name, value)
        return True

    return change


def _get(repo) -> ObjectiveRecord:
    return repo.get_objective("objective_1", owner_agent_id=SCOPE)


# -- the store -------------------------------------------------------------------


def test_every_objective_field_is_either_versioned_or_bookkeeping() -> None:
    """A new field must be classified: covered fields refuse agents, bookkeeping ones merge."""

    assert OBJECTIVE_VERSION_FIELDS.isdisjoint(OBJECTIVE_BOOKKEEPING_FIELDS)
    assert OBJECTIVE_VERSION_FIELDS | OBJECTIVE_BOOKKEEPING_FIELDS == set(ObjectiveRecord.model_fields)


def test_an_edit_from_an_older_read_never_puts_back_a_newer_title(settings) -> None:
    """The reported case: one copy saves a title, then an older copy saves a desired outcome."""

    repo = _repository(settings)
    _stored(repo)
    read_by_both = objective_version(_get(repo))

    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(title="New title"), expected_version=read_by_both)
    with pytest.raises(ChangeRefused) as refused:
        repo.change_objective(
            "objective_1", owner_agent_id=SCOPE, change=_setter(desired_outcome="New outcome"), expected_version=read_by_both
        )

    assert refused.value.reason is ChangeRefusal.CHANGED_SINCE_READ
    assert refused.value.details == {"objective_id": "objective_1"}
    stored = _get(repo)
    assert (stored.title, stored.desired_outcome) == ("New title", "Old outcome")
    # Read again, the same edit goes through and keeps the other one.
    repo.change_objective(
        "objective_1", owner_agent_id=SCOPE, change=_setter(desired_outcome="New outcome"),
        expected_version=objective_version(stored),
    )
    assert (_get(repo).title, _get(repo).desired_outcome) == ("New title", "New outcome")


def test_two_changes_at_the_same_time_keep_both_fields(settings) -> None:
    """The first change stops inside its write transaction; the second waits for it and then sees its title."""

    repo = _repository(settings)
    _stored(repo)
    first_has_read, second_starts = threading.Event(), threading.Event()

    def title_after_a_pause(objective: ObjectiveRecord) -> bool:
        first_has_read.set()
        assert second_starts.wait(5)
        time.sleep(0.3)  # the second change now waits for this transaction
        objective.title = "New title"
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repo.change_objective, "objective_1", owner_agent_id=SCOPE, change=title_after_a_pause)
        assert first_has_read.wait(5)
        second_starts.set()
        second = pool.submit(
            repo.change_objective, "objective_1", owner_agent_id=SCOPE, change=_setter(desired_outcome="New outcome")
        )
        first.result(timeout=10)
        second.result(timeout=10)

    stored = _get(repo)
    assert (stored.title, stored.desired_outcome) == ("New title", "New outcome")


def test_changes_to_different_fields_without_a_version_both_stay(settings) -> None:
    """Trusted bookkeeping merges into the stored row: it never writes back an older copy."""

    repo = _repository(settings)
    _stored(repo)
    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(title="New title"))
    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(desired_outcome="New outcome"))

    stored = _get(repo)
    assert (stored.title, stored.desired_outcome) == ("New title", "New outcome")


def test_bookkeeping_leaves_the_version_an_agent_read_valid(settings) -> None:
    """Linked tasks, events, progress, status, plans, recovery and links do not refuse agent edits."""

    repo = _repository(settings)
    _stored(repo)
    read = objective_version(_get(repo))

    def bookkeeping(objective: ObjectiveRecord) -> bool:
        objective.related_task_ids.append("task_arrived")
        objective.recovery_task_ids.append("task_recovery")
        objective.plan_ids.append("plan_2")
        objective.current_plan_id = "plan_2"
        objective.schedule_links.append(ObjectiveScheduleLink(schedule_id="sched_1"))
        objective.event_log.append(ObjectiveEventRecord(event_type=ObjectiveEventType.TASK_LINKED, summary="Linked."))
        objective.progress_summary = "Half done."
        objective.current_focus = "The tests."
        objective.status = ObjectiveStatus.BLOCKED
        objective.updated_at = "2026-10-20T10:00:00+00:00"
        return True

    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=bookkeeping)
    assert objective_version(_get(repo)) == read

    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(priority=ObjectivePriority.HIGH), expected_version=read)
    stored = _get(repo)
    assert (stored.priority.value, stored.related_task_ids, stored.status) == ("high", ["task_arrived"], ObjectiveStatus.BLOCKED)


def test_a_criterion_change_changes_the_version_but_its_timestamp_alone_does_not(settings) -> None:
    repo = _repository(settings)
    _stored(repo)
    read = objective_version(_get(repo))

    def touch(objective: ObjectiveRecord) -> bool:
        objective.success_criteria[0].updated_at = "2026-10-20T11:00:00+00:00"
        return True

    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=touch)
    assert objective_version(_get(repo)) == read

    def met(objective: ObjectiveRecord) -> bool:
        objective.success_criteria[0].status = ObjectiveCriterionStatus.MET
        return True

    repo.change_objective("objective_1", owner_agent_id=SCOPE, change=met)
    assert objective_version(_get(repo)) != read


def test_a_refused_or_failed_change_leaves_the_record_as_it_was(settings) -> None:
    repo = _repository(settings)
    _stored(repo)
    before = _get(repo).model_dump(mode="json")

    with pytest.raises(ChangeRefused):
        repo.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(title="x"), expected_version="older")

    def fails_halfway(objective: ObjectiveRecord) -> bool:
        objective.title = "half"
        raise RuntimeError("the change failed")

    with pytest.raises(RuntimeError):
        repo.change_objective("objective_1", owner_agent_id=SCOPE, change=fails_halfway)
    assert _get(repo).model_dump(mode="json") == before


def test_a_change_to_a_deleted_objective_does_not_bring_it_back(settings) -> None:
    """A removed person's Objective stays deleted when a change for it arrives later."""

    repo = _repository(settings)
    _stored(repo, owner_canonical_user_id="person_gone")
    read = objective_version(_get(repo))
    assert PersonDepartureRepository(settings).delete_person_records("person_gone")["objectives"] == 1

    assert repo.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(title="Late")) is None
    assert repo.change_objective(
        "objective_1", owner_agent_id=SCOPE, change=_setter(title="Late"), expected_version=read
    ) is None
    assert _get(repo) is None


def test_an_insert_never_replaces_a_stored_objective(settings) -> None:
    repo = _repository(settings)
    _stored(repo)
    with pytest.raises(Exception):
        repo.insert_objective(
            ObjectiveRecord(
                objective_id="objective_1", title="Replacement", desired_outcome="x", why="y",
                horizon=ObjectiveHorizon.FINITE, owner_agent_id=SCOPE,
            )
        )
    assert _get(repo).title == "Old title"


def test_the_version_survives_a_restart_and_a_custody_recovery(settings) -> None:
    """A version read before the team lead restarts, or before a person's control adopts the record, still holds."""

    repo = _repository(settings)
    stale_owner = "TeamArchitect_old_process"
    repo.insert_objective(
        ObjectiveRecord(
            objective_id="objective_1", title="Mine", desired_outcome="Done.", why="Asked.",
            horizon=ObjectiveHorizon.FINITE, owner_agent_id=stale_owner,
            owner_canonical_user_id="person_a", team_id="team_1", owner_named_agent_type="TeamArchitect",
        )
    )
    read = objective_version(repo.get_objective("objective_1", owner_agent_id=stale_owner))

    restarted = _repository(settings)
    create_plan_repository(settings)  # adoption moves the Objective's plans too
    create_durable_owner_scope_repository(settings).adopt_user_owned_objective(
        objective_id="objective_1",
        owner_canonical_user_id="person_a",
        team_id="team_1",
        named_agent_type="TeamArchitect",
        durable_owner_agent_id=SCOPE,
        include_objective_work=False,
    )
    adopted = restarted.get_objective("objective_1", owner_agent_id=SCOPE)
    assert objective_version(adopted) == read
    restarted.change_objective("objective_1", owner_agent_id=SCOPE, change=_setter(title="Still mine"), expected_version=read)
    assert restarted.get_objective("objective_1", owner_agent_id=SCOPE).title == "Still mine"


def test_listing_returns_the_same_version_as_reading(settings) -> None:
    repo = _repository(settings)
    _stored(repo)
    [row] = repo.list_objectives(limit=10, owner_agent_id=SCOPE)
    assert row.version == objective_version(_get(repo))


# -- the team lead ---------------------------------------------------------------


def _manager(settings, agent_id: str) -> TaskManager:
    manager = TaskManager(db_path=settings.sqlite_path or "", logger=logging.getLogger(agent_id), storage_settings=settings)
    manager.set_owner_identity(agent_id=agent_id, agent_name="Lead", team_id="team_1", named_agent_type="TeamArchitect")
    return manager


def _objective_from_task(manager: TaskManager) -> ObjectiveRecord:
    task = Task(task_id="task_root", task_type=MessageMetaType.REQUEST, problem_statement="Track it.")
    assert manager.add_task(
        task,
        MessageHeader(from_agent_id="user", agent_signature=""),
        MessageMeta(from_agent_name="user", conversation_id="c1"),
        owner_person_id=None,
    )
    return manager.create_objective(
        title="Ship it", desired_outcome="It ships.", why="Asked.", task_id="task_root",
        success_criteria=[{"criterion_id": "crit_1", "description": "Tests pass."}],
    )


def _while_reading(monkeypatch, manager: TaskManager, other_write) -> None:
    """Run ``other_write`` (another team lead process) right after ``manager`` next reads the Objective."""

    repo = manager.objective_coordinator.objective_repository
    real_get = repo.get_objective

    def read_then_other_writes(objective_id, *, owner_agent_id):
        copy = real_get(objective_id, owner_agent_id=owner_agent_id)
        monkeypatch.setattr(repo, "get_objective", real_get)
        other_write(objective_id)
        return copy

    monkeypatch.setattr(repo, "get_objective", read_then_other_writes)


def test_a_task_link_during_another_processs_criterion_update_keeps_both(settings, monkeypatch) -> None:
    """Reproduced on the earlier store: the bookkeeping save erased the criterion marked met."""

    first = _manager(settings, "lead_old")
    objective = _objective_from_task(first)
    second = _manager(settings, "lead_new")
    _while_reading(
        monkeypatch,
        first,
        lambda objective_id: second.update_objective_progress(
            objective_id, criterion_updates=[{"criterion_id": "crit_1", "status": "met", "evidence_summary": "CI green."}]
        ),
    )

    first.objective_coordinator.link_incoming_task_to_objective("task_arrived", objective.objective_id)

    stored = second.get_objective(objective.objective_id)
    assert "task_arrived" in stored.related_task_ids
    assert stored.success_criteria[0].status.value == "met"


def test_a_task_end_review_during_an_agent_edit_keeps_both(settings, monkeypatch) -> None:
    first = _manager(settings, "lead_old")
    objective = _objective_from_task(first)
    read = objective_version(objective)
    second = _manager(settings, "lead_new")
    _while_reading(
        monkeypatch,
        first,
        lambda objective_id: second.objective_coordinator.change_objective(
            objective_id, _setter(priority=ObjectivePriority.HIGH), expected_version=read
        ),
    )

    first.fail_task("task_root", "The attempt failed.", "2026-10-20T10:00:00+00:00")

    stored = second.get_objective(objective.objective_id)
    assert stored.priority.value == "high"
    assert stored.event_log[-1].details["conclusion"] == "task_failed_plan_remains_active"
    assert stored.status == ObjectiveStatus.BLOCKED


def test_an_agent_edit_from_before_another_processs_criterion_change_is_refused(settings) -> None:
    first = _manager(settings, "lead_old")
    objective = _objective_from_task(first)
    read = objective_version(objective)
    second = _manager(settings, "lead_new")
    second.update_objective_progress(
        objective.objective_id, criterion_updates=[{"criterion_id": "crit_1", "status": "met"}], expected_version=read
    )

    with pytest.raises(ChangeRefused):
        first.update_objective_progress(
            objective.objective_id, criterion_updates=[{"criterion_id": "crit_1", "status": "blocked"}], expected_version=read
        )
    with pytest.raises(ChangeRefused):
        first.update_objective_status(objective.objective_id, "abandoned", expected_version=read)
    stored = first.get_objective(objective.objective_id)
    assert (stored.success_criteria[0].status.value, stored.status) == ("met", ObjectiveStatus.ACTIVE)


# -- the team lead's tools ---------------------------------------------------------


@pytest.fixture
def lead(settings):
    manager = _manager(settings, "manager_agent")
    task = _add_person_task(manager, "task_root", "objective-user", conversation_id="conversation_1")
    manager.task_state.current_in_progress_task_id = task.task_id
    agent = _FakeAgent(manager)
    set_agent_ref(agent)
    created = manager.create_objective(
        title="Ship it", desired_outcome="It ships.", why="Asked.", task_id=task.task_id,
        success_criteria=[{"criterion_id": "crit_1", "description": "Tests pass."}],
    )
    return manager, agent, created.objective_id


def _version(objective_id: str) -> str:
    return tool_json(objectives(action="get", objective_id=objective_id))["version"]


def test_the_tools_refuse_a_change_from_an_older_read(lead) -> None:
    """Two of a person's requests read the Objective; one marks the criterion met, the other is stale."""

    manager, _agent, objective_id = lead
    read_by_both = _version(objective_id)
    met = tool_json(
        objectives(
            action="update_progress", objective_id=objective_id, expected_version=read_by_both,
            criterion_updates_json=json.dumps([{"criterion_id": "crit_1", "status": "met", "evidence_summary": "CI."}]),
        )
    )
    assert met["version"] != read_by_both

    stale_progress = tool_json(
        objectives(
            action="update_progress", objective_id=objective_id, expected_version=read_by_both,
            criterion_updates_json=json.dumps([{"criterion_id": "crit_1", "status": "open"}]),
        )
    )
    stale_status = tool_json(
        objectives(action="update_status", objective_id=objective_id, expected_version=read_by_both, status="abandoned")
    )

    for refused in (stale_progress, stale_status):
        assert refused["error_code"] == "changed_since_read"
        assert refused["objective_id"] == objective_id
        assert "changed since you read it" in refused["error"]
    stored = manager.get_objective(objective_id)
    assert (stored.success_criteria[0].status.value, stored.status) == ("met", ObjectiveStatus.ACTIVE)
    # Read again, the change goes through.
    redone = tool_json(
        objectives(action="update_status", objective_id=objective_id, expected_version=_version(objective_id), status="paused")
    )
    assert redone["objective"]["status"] == "paused"


def test_a_refused_status_change_turns_nothing_off(lead) -> None:
    """The status write comes first, so a refused one leaves the linked schedule running."""

    manager, agent, objective_id = lead
    schedule = agent._schedule_store
    message = ScheduledMessage(
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check."),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="objective-user")),
        interval_ms=60_000,
        claim_team_id="team_1",
        claim_named_agent_type="TeamArchitect",
    )
    schedule.add(message)
    manager.link_schedule_to_objective(objective_id, message.schedule_id)
    read = _version(objective_id)
    manager.objective_coordinator.change_objective(objective_id, _setter(why="A new reason."), expected_version=read)

    refused = tool_json(objectives(action="update_status", objective_id=objective_id, expected_version=read, status="paused"))

    assert refused["error_code"] == "changed_since_read"
    assert schedule.get_scheduled_message(message.schedule_id).enabled is True
    assert manager.get_objective(objective_id).status == ObjectiveStatus.ACTIVE


@pytest.mark.parametrize("deletion_status", ["deleting", "deleted"])
def test_a_status_tool_cannot_reopen_an_objective_when_deletion_starts_before_its_write(
    lead, settings, monkeypatch, deletion_status
) -> None:
    manager, _agent, objective_id = lead
    second = _manager(settings, "manager_other")
    repository = manager.objective_coordinator.objective_repository
    change = repository.change_objective

    def deletion_before_write(*args, **kwargs):
        second.update_objective_status(objective_id, deletion_status, summary="The person requested deletion.")
        return change(*args, **kwargs)

    monkeypatch.setattr(repository, "change_objective", deletion_before_write)
    result = tool_json(
        objectives(action="update_status", objective_id=objective_id, expected_version=_version(objective_id), status="active")
    )

    assert "by its person, so it can no longer change" in result.get("error", "")
    assert manager.get_objective(objective_id).status.value == deletion_status


def test_work_on_the_objective_does_not_refuse_the_leads_next_change(lead) -> None:
    """A continuation task, a linked schedule and a task ending add bookkeeping; the version read before holds."""

    manager, _agent, objective_id = lead
    read = _version(objective_id)
    continued = tool_json(start_objective_task(work_brief="Keep going.", objective_id=objective_id))
    assert continued.get("task_id")
    manager.link_schedule_to_objective(objective_id, "sched_other")
    manager.fail_task(continued["task_id"], "That attempt failed.", "2026-10-20T10:00:00+00:00")

    updated = tool_json(
        objectives(action="update_progress", objective_id=objective_id, expected_version=read, progress_summary="Retrying.")
    )

    assert updated["objective"]["progress_summary"] == "Retrying."
    assert updated["version"] == read
    stored = manager.get_objective(objective_id)
    assert continued["task_id"] in stored.related_task_ids
    assert [link.schedule_id for link in stored.schedule_links] == ["sched_other"]


def test_the_change_tools_need_the_version(lead) -> None:
    _manager_, _agent, objective_id = lead
    missing = tool_json(objectives(action="update_progress", objective_id=objective_id, progress_summary="x"))
    assert missing == {
        "error": "objectives(action='update_progress') needs expected_version.",
        "error_code": "invalid_input",
    }
    listed = tool_json(objectives(action="list"))
    assert listed["objectives"][0]["version"] == _version(objective_id)
