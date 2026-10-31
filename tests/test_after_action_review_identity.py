"""After-action report reviews work for the person whose work produced the reports.

A specialist's after-action report (AAR) comes back to the agent that delegated
the work. The runtime collects the reports for one parent task in a batch and
later starts a scheduled review task on that agent
(``custom_agents.after_action_report_identity``). These tests drive that path
with real SQLite stores, real Ed25519 task contexts, the agent's own methods,
the real fire path (``scheduled_runtime.enqueue_fired_scheduled_task``), the
real review tools and real Cedar for tool decisions. Covers:

- the bug (2026-10-10): the review ran with no identity, so with authorization
  on ``resolve_after_action_report_review`` was refused; now the review verifies
  as the person and the call is allowed;
- the review tools see and resolve only that person's reports;
- two people's reports never share a batch or a review;
- a report about other work than its parent task's is not recorded;
- reports of work for nobody are reviewed as work for nobody;
- unproven reports: not recorded with authorization on, work for nobody with it off;
- a disabled person's review does not run;
- batches and reviews saved before the person was recorded are closed unreviewed
  and never listed;
- the specialist's runtime sets the report's task links from its own task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_agents import access_runtime
from custom_agents.schedule_store import ScheduleStore
from custom_agents.scheduled_runtime import enqueue_fired_scheduled_task
from custom_agents.strands_agent import UniversalAgent
from custom_agents.task_context_runtime import scheduler_signer
from custom_agents.task_manager import TaskManager
from custom_agents.task_manager_after_action_reports import UNVERIFIED_IDENTITY_STALE_REASON
from custom_agents.task_records import ReportedWorkIdentity
from custom_messages.contracts.access_v2 import PersonStanding, ScheduleRunIdentityV1, TeamPersonV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import AfterActionReport, Task
from custom_messages.enums import (
    AfterActionReportReviewBatchStatus,
    AfterActionReportReviewStatus,
    MessageMetaType,
    ProgressState,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.records.reviews import AfterActionReportReviewBatchRecord, AfterActionReportReviewRecord
from custom_tools.providers.context_memory import (
    list_pending_after_action_reports,
    resolve_after_action_report_review,
)
from tests.task_work_support import bind_task, set_agent_ref
from storage.access_repository import AccessRepository
from storage.knowledge_repository import create_knowledge_repository
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key
from tests.knowledge_support import BACKENDS, knowledge_storage
from tests.scheduled_run_support import NOW

TEAM = "team_1"
ANN = "usr_ann"
BOB = "usr_bob"
MANAGER_ID = "manager_1"
LEAD_TYPE = "TeamArchitect"
COMPLETED_AT = "2026-10-10T00:05:00+00:00"
ROOT = Path(__file__).resolve().parents[1]
CEDAR = ROOT / "target" / "debug" / "ua-cedar-authorizer"
needs_cedar = pytest.mark.skipif(not CEDAR.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


def _build_world(db_path: str) -> SimpleNamespace:
    """The team lead in the manager process, over one SQLite database."""

    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity(agent_id=MANAGER_ID, agent_name="lead", team_id=TEAM, named_agent_type=LEAD_TYPE)
    store = ScheduleStore(db_path=db_path, storage_settings=tasks.storage_settings)
    manager = SimpleNamespace(
        id=MANAGER_ID,
        name="lead",
        team_id=TEAM,
        named_agent_type=LEAD_TYPE,
        is_team_lead=True,
        access_role="team_lead",
        authorization_config={"enabled": True, "cedar_binary_path": str(CEDAR)},
        instance_root=None,
        instance_timezone="UTC",
        task_manager=tasks,
        _schedule_store=store,
        _scheduler_wake=None,
        paused=False,
        is_executing=True,  # queue the review instead of starting a model turn
        main_lane_busy=True,  # the main lane (runtime_hosting.agent_execution)
        # The review's wake-up is recorded; with is_executing=True the drain finds the slot busy, so the review stays queued.
        _should_exit=asyncio.Event(),
        _pending_work_task=None,
        _continue_pending_work=AsyncMock(),
        _conversation_store=None,
        _ensure_request_timezone=lambda meta, top_level_request: meta,
        _logger=logging.getLogger("test.lead"),
    )
    manager._schedule_after_action_report_batch_review = lambda batch: UniversalAgent._schedule_after_action_report_batch_review(
        manager, batch
    )
    _record_people(tasks, {ANN: PersonStanding.ACTIVE, BOB: PersonStanding.ACTIVE})
    return SimpleNamespace(tasks=tasks, store=store, manager=manager, db_path=db_path)


def _record_people(tasks: TaskManager, standings: dict[str, PersonStanding]) -> None:
    """What the gateway keeps in the access store: each person's standing and roles."""

    AccessRepository(tasks.storage_settings).replace_people(
        TEAM,
        [
            TeamPersonV1(
                team_id=TEAM,
                person_id=person_id,
                standing=standing,
                roles=("admin",) if standing is PersonStanding.ACTIVE else (),
                changed_at_utc=NOW,
            )
            for person_id, standing in standings.items()
        ],
        at=NOW,
    )


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch):
    """This process holds the scheduler key and trusts the gateway's, as the manager process does."""

    use_scheduler_key(monkeypatch)
    yield
    forget_task_context_keys()
    set_agent_ref(None)


@pytest.fixture
def world(tmp_path: Path, keys):
    return _build_world(str(tmp_path / "manager.db"))


# -- helpers ------------------------------------------------------------------------------


def _person_meta(person_id: str) -> MessageMeta:
    """A request's meta as the gateway sends it: the person and their signed context."""

    return MessageMeta(
        from_agent_name="gateway",
        gateway_context=GatewayContext(
            canonical_user_id=person_id,
            task_context=context_signer().issue(team_id=TEAM, person_id=person_id, person_roles=("admin",)),
        ),
    )


def _system_meta() -> MessageMeta:
    """A maintenance run's meta: signed by the scheduler for no person."""

    signer = scheduler_signer()
    assert signer is not None
    return MessageMeta(
        from_agent_name="scheduler",
        gateway_context=GatewayContext(
            task_context=signer.issue(team_id=TEAM, person_id=None, schedule_id="sched_maintenance")
        ),
    )


def _parent_task(world, task_id: str, meta: MessageMeta) -> Task:
    parent = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=f"Work of {task_id}.")
    world.tasks.add_task(parent, MessageHeader(from_agent_id="gateway", agent_signature=""), meta, owner_person_id=None)
    world.tasks.update_main_task_state(task_id, ProgressState.IN_PROGRESS, "2026-10-10T00:00:00+00:00")
    return parent


def _report(report_id: str, parent_task_id: str) -> AfterActionReport:
    return AfterActionReport(
        report_id=report_id,
        source_task_id=f"task_child_{report_id}",
        parent_task_id=parent_task_id,
        source_agent_id="worker_1",
        source_agent_name="researcher",
        source_named_agent_type="WebResearchAgent",
        summary=f"Lesson from {report_id}.",
    )


def _receive(world, report: AfterActionReport, meta: MessageMeta) -> None:
    """The report arrives at the delegating agent, carrying the specialist task's meta."""

    asyncio.run(
        UniversalAgent._handle_after_action_report(
            world.manager,
            report=report,
            msg_header=MessageHeader(from_agent_id="worker_1", agent_signature=""),
            msg_meta=meta,
        )
    )


def _complete(world, task_id: str) -> None:
    """The parent task ends; the agent schedules the review of its reports."""

    asyncio.run(UniversalAgent._complete_task_and_schedule_aar_review(world.manager, task_id, COMPLETED_AT))


def _review_schedules(world) -> list[ScheduledMessage]:
    schedules = [world.store.get_scheduled_message(str(row["schedule_id"])) for row in world.store.all_rows()]
    return [
        schedule
        for schedule in schedules
        if schedule is not None and str(schedule.schedule_name).startswith("after_action_report_batch_review:")
    ]


def _owner(schedule: ScheduledMessage) -> str | None:
    return schedule.payload_meta.gateway_context.canonical_user_id


def _fire(world, schedule: ScheduledMessage) -> None:
    assert asyncio.run(enqueue_fired_scheduled_task(world.manager, schedule.schedule_id, schedule)) is True


def _access_agent(world, task_id: str) -> SimpleNamespace:
    """The team lead as the access hook and the tools see it while it runs ``task_id``."""

    access_runtime._runtime_access.cache_clear()
    bind_task(task_id)  # the test's direct calls work for this task (tests.task_work_support)
    return SimpleNamespace(
        id=MANAGER_ID,
        name="lead",
        named_agent_type=LEAD_TYPE,
        team_id=TEAM,
        is_team_lead=True,
        access_role="team_lead",
        authorization_config=world.manager.authorization_config,
        task_manager=world.tasks,
        instance_root=None,
        test_task_id=task_id,
    )


def _review_ids(world, person_id: str | None) -> list[str]:
    return [review.report_id for review in world.tasks.list_after_action_report_reviews(person_id=person_id)]


# -- the bug ------------------------------------------------------------------------------


@needs_cedar
def test_a_review_of_a_persons_reports_works_for_that_person(world) -> None:
    meta = _person_meta(ANN)
    parent = _parent_task(world, "task_parent_ann", meta)
    _receive(world, _report("aar_ann_1", parent.task_id), meta.model_copy(deep=True))
    _complete(world, parent.task_id)

    [schedule] = _review_schedules(world)
    assert schedule.management_class is ScheduleManagementClass.SYSTEM
    assert schedule.system_schedule_kind is SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW
    assert _owner(schedule) == ANN, "the review schedule belongs to the person the reports are about"
    _fire(world, schedule)

    review = world.tasks.get_task_info(schedule.payload.task_id)
    assert review is not None, "the review task was queued"
    agent = _access_agent(world, review.task.task_id)
    context = access_runtime.verified_task_context(agent)
    assert context is not None and (context.person_id, context.schedule_id) == (ANN, schedule.schedule_id)
    assert access_runtime.verified_person_id(agent) == ANN
    # Before the fix this was refused: "Nobody verifiably asked for this task".
    decision = access_runtime.decide_call(
        agent, "resolve_after_action_report_review", {"report_id": "aar_ann_1", "status": "ignored"}
    )
    assert decision is not None and decision.allowed, decision


# -- one person per review ------------------------------------------------------------------


def test_review_tools_see_and_resolve_only_the_reviewed_persons_reports(world) -> None:
    ann_meta, bob_meta = _person_meta(ANN), _person_meta(BOB)
    ann_parent = _parent_task(world, "task_parent_ann", ann_meta)
    bob_parent = _parent_task(world, "task_parent_bob", bob_meta)
    _receive(world, _report("aar_ann", ann_parent.task_id), ann_meta.model_copy(deep=True))
    _receive(world, _report("aar_bob", bob_parent.task_id), bob_meta.model_copy(deep=True))
    _complete(world, ann_parent.task_id)
    _complete(world, bob_parent.task_id)

    schedules = {_owner(schedule): schedule for schedule in _review_schedules(world)}
    assert set(schedules) == {ANN, BOB}, "one review per person"
    _fire(world, schedules[ANN])
    review = world.tasks.get_task_info(schedules[ANN].payload.task_id)
    assert "report_id: aar_ann" in review.task.problem_statement
    assert "aar_bob" not in review.task.problem_statement

    set_agent_ref(_access_agent(world, review.task.task_id))
    listed = json.loads(list_pending_after_action_reports(limit=10))
    assert [item["report_id"] for item in listed["reviews"]] == ["aar_ann"]
    not_hers = json.loads(
        resolve_after_action_report_review(status="ignored", decision_summary="Not hers.", report_id="aar_bob")
    )
    assert not_hers["error"] == "AfterActionReport review not found"
    resolved = json.loads(
        resolve_after_action_report_review(status="ignored", decision_summary="Nothing durable.", report_id="aar_ann")
    )
    assert resolved["review"]["status"] == "ignored"
    assert _review_ids(world, BOB) == ["aar_bob"], "Bob's report waits for Bob's review"


def test_reports_for_one_archived_parent_are_batched_per_person(world) -> None:
    parent = _parent_task(world, "task_parent_shared", _person_meta(ANN))
    world.tasks.complete_task(parent.task_id, COMPLETED_AT)

    recorded = {
        person_id: world.tasks.record_after_action_report(
            _report(f"aar_late_{person_id}", parent.task_id),
            identity=ReportedWorkIdentity(person_id=person_id),
            timestamp="2026-10-10T00:05:05+00:00",
        )
        for person_id in (ANN, BOB)
    }

    batches = {person_id: result.batch for person_id, result in recorded.items()}
    assert batches[ANN].batch_id != batches[BOB].batch_id
    assert (batches[ANN].person_id, batches[BOB].person_id) == (ANN, BOB)
    assert all(result.schedule_required for result in recorded.values()), "each person's batch gets its own review"
    assert _review_ids(world, ANN) == [f"aar_late_{ANN}"] and _review_ids(world, BOB) == [f"aar_late_{BOB}"]


@pytest.mark.parametrize("resolve_first", [False, True])
def test_reused_report_ids_keep_each_persons_review_and_repeated_delivery(world, resolve_first) -> None:
    review_ids = {}
    for person in (ANN, BOB, None):
        meta = _person_meta(person) if person is not None else _system_meta()
        parent = _parent_task(world, f"task_parent_{person}", meta)
        report = _report("aar_reused_id", parent.task_id)
        _receive(world, report, meta)
        review = world.tasks.get_after_action_report_review_by_report_id(report.report_id, person_id=person)
        review_ids[person] = review.review_id
        _receive(world, report.model_copy(deep=True), meta)
        [repeated] = world.tasks.list_after_action_report_reviews(person_id=person)
        assert repeated.review_id == review.review_id
        assert repeated.parent_task_id == parent.task_id
        if resolve_first and person == ANN:
            world.tasks.resolve_after_action_report_review(
                review.review_id, person_id=ANN, status=AfterActionReportReviewStatus.IGNORED,
                decision_summary="Already reviewed.",
            )
        _complete(world, parent.task_id)

    assert len(set(review_ids.values())) == 3
    for schedule in _review_schedules(world):
        person = _owner(schedule)
        _fire(world, schedule)
        task = world.tasks.get_task_info(schedule.payload.task_id)
        if resolve_first and person == ANN:
            assert task is None
        else:
            assert task is not None
            assert "report_id: aar_reused_id" in task.task.problem_statement
            assert f"Work of task_parent_{person}." in task.task.problem_statement


def test_an_existing_review_cannot_be_reassigned_to_another_person(world) -> None:
    parent = _parent_task(world, "task_parent_ann", _person_meta(ANN))
    _receive(world, _report("aar_ann", parent.task_id), _person_meta(ANN))
    original = world.tasks.get_after_action_report_review_by_report_id("aar_ann", person_id=ANN)
    other = original.model_copy(update={"person_id": BOB, "report_id": "aar_bob", "summary": "Bob's report."})

    with pytest.raises(ValueError, match="cannot change whose work"):
        world.tasks.after_action_report_repository.upsert_review(other)

    assert world.tasks.get_after_action_report_review(original.review_id, person_id=ANN) == original
    assert _review_ids(world, BOB) == []


def _historical_meta(world, source: str) -> MessageMeta:
    started = datetime.now(timezone.utc) - timedelta(days=8)
    if source == "expired_context":
        signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("admin",), now=started)
        return MessageMeta(gateway_context=GatewayContext(canonical_user_id=ANN, task_context=signed))
    signer = scheduler_signer()
    assert signer is not None
    schedule = ScheduledMessage(
        schedule_id="sched_finished_source", repeat_count=1,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="One-shot delegated work."),
        payload_meta=_person_meta(ANN), next_fire_at=started.isoformat(),
    )
    world.store.add(schedule, claim_team_id=TEAM, claim_named_agent_type="WebResearchAgent")
    permit = signer.issue_permit(
        team_id=TEAM, schedule_id=schedule.schedule_id, person_id=ANN, now=started, lifetime=timedelta(days=7),
    )
    # A finished one-shot schedule no longer exists when the report arrives.
    world.store.update_after_fire(schedule.schedule_id)
    assert world.store.get_scheduled_message(schedule.schedule_id) is None
    return MessageMeta(gateway_context=GatewayContext(
        canonical_user_id=ANN,
        schedule_run=ScheduleRunIdentityV1(permit=permit, context_id="schedule_run_finished", started_at_utc=started),
    ))


@pytest.mark.parametrize("source", ["expired_context", "expired_permit"])
# Only authorization on asks the evaluator for a decision; the off cases run without it.
@pytest.mark.parametrize("enabled", [pytest.param(True, marks=needs_cedar), False])
@pytest.mark.parametrize("archived", [True, False])
def test_historical_reports_keep_the_person_and_reviews_receive_fresh_identity(world, source, enabled, archived) -> None:
    world.manager.authorization_config["enabled"] = enabled
    meta = _historical_meta(world, source)
    parent = _parent_task(world, "task_historical", meta)
    if archived:
        _complete(world, parent.task_id)
    _receive(world, _report("aar_historical_ann", parent.task_id), meta)
    if not archived:
        _complete(world, parent.task_id)

    assert _review_ids(world, ANN) == ["aar_historical_ann"]
    assert _review_ids(world, None) == []
    [schedule] = _review_schedules(world)
    assert _owner(schedule) == ANN
    _fire(world, schedule)
    agent = _access_agent(world, schedule.payload.task_id)
    context = access_runtime.verified_task_context(agent)
    assert context is not None and context.person_id == ANN
    assert context.expires_at_utc > datetime.now(timezone.utc)
    assert context.schedule_id == schedule.schedule_id
    if enabled:
        assert access_runtime.resolve_meta_identity(world.manager, meta) is None, "old authority stays invalid"
        decision = access_runtime.decide_call(agent, "resolve_after_action_report_review", {"report_id": "aar_historical_ann"})
        assert decision is not None and decision.allowed


@pytest.mark.parametrize("source", ["expired_context", "expired_permit"])
@pytest.mark.parametrize("enabled", [True, False])
def test_changed_historical_signatures_never_attribute_reports_to_a_person(world, source, enabled) -> None:
    world.manager.authorization_config["enabled"] = enabled
    meta = _historical_meta(world, source)
    context = meta.gateway_context
    if context.task_context is not None:
        signed = context.task_context
        context.task_context = signed.model_copy(update={"context": signed.context.model_copy(update={"person_id": BOB})})
    else:
        run = context.schedule_run
        signed = run.permit
        changed = signed.model_copy(update={"permit": signed.permit.model_copy(update={"person_id": BOB})})
        context.schedule_run = run.model_copy(update={"permit": changed})
    parent = _parent_task(world, "task_forged", meta)
    _receive(world, _report("aar_forged", parent.task_id), meta)

    assert _review_ids(world, ANN) == [] and _review_ids(world, BOB) == []
    assert _review_ids(world, None) == (["aar_forged"] if not enabled else [])


def test_a_report_about_other_work_than_its_parent_tasks_is_not_recorded(world, caplog) -> None:
    parent = _parent_task(world, "task_parent_ann", _person_meta(ANN))

    with caplog.at_level(logging.WARNING, logger="custom_agents.after_action_report_identity"):
        _receive(world, _report("aar_bob_work", parent.task_id), _person_meta(BOB))

    assert "event=after_action_report_person_mismatch" in caplog.text
    assert _review_ids(world, ANN) == [] and _review_ids(world, BOB) == []
    history = world.tasks.get_task_info(parent.task_id).task.execution_history
    assert not [entry for entry in history if (entry.details or {}).get("report_id") == "aar_bob_work"]


# -- work for nobody and unproven work --------------------------------------------------------


def test_reports_of_work_for_nobody_are_reviewed_as_work_for_nobody(world) -> None:
    meta = _system_meta()
    parent = _parent_task(world, "task_parent_system", meta)
    _receive(world, _report("aar_system", parent.task_id), meta.model_copy(deep=True))
    _complete(world, parent.task_id)

    assert _review_ids(world, None) == ["aar_system"] and _review_ids(world, ANN) == []
    [schedule] = _review_schedules(world)
    assert _owner(schedule) is None
    _fire(world, schedule)

    review = world.tasks.get_task_info(schedule.payload.task_id)
    context = access_runtime.verified_task_context(_access_agent(world, review.task.task_id))
    assert context is not None, "the scheduler signed the run"
    assert (context.issuer, context.person_id, context.schedule_id) == ("scheduler", None, schedule.schedule_id)


def test_unproven_reports_are_not_recorded_with_authorization_on(world, caplog) -> None:
    plain = MessageMeta(from_agent_name="gateway")
    parent = _parent_task(world, "task_parent_plain", plain)

    with caplog.at_level(logging.WARNING, logger="custom_agents.after_action_report_identity"):
        _receive(world, _report("aar_plain", parent.task_id), plain.model_copy(deep=True))
    _complete(world, parent.task_id)

    assert "event=after_action_report_unverified " in caplog.text
    assert _review_ids(world, None) == [] and _review_schedules(world) == []


def test_unproven_reports_are_work_for_nobody_with_authorization_off(world) -> None:
    world.manager.authorization_config = {"enabled": False}
    plain = MessageMeta(from_agent_name="gateway")
    parent = _parent_task(world, "task_parent_plain", plain)
    _receive(world, _report("aar_plain", parent.task_id), plain.model_copy(deep=True))
    _complete(world, parent.task_id)

    assert _review_ids(world, None) == ["aar_plain"]
    [schedule] = _review_schedules(world)
    assert _owner(schedule) is None
    _fire(world, schedule)
    review = world.tasks.get_task_info(schedule.payload.task_id)
    set_agent_ref(_access_agent(world, review.task.task_id))
    assert json.loads(list_pending_after_action_reports(limit=5))["returned"] == 1


# -- departures -------------------------------------------------------------------------------


def test_a_disabled_persons_review_does_not_run(world, caplog) -> None:
    meta = _person_meta(ANN)
    parent = _parent_task(world, "task_parent_ann", meta)
    _receive(world, _report("aar_ann", parent.task_id), meta.model_copy(deep=True))
    _complete(world, parent.task_id)
    [schedule] = _review_schedules(world)
    _record_people(world.tasks, {ANN: PersonStanding.DISABLED, BOB: PersonStanding.ACTIVE})

    with caplog.at_level(logging.INFO, logger="custom_agents.task_context_runtime"):
        _fire(world, schedule)

    assert "event=scheduled_run_skipped_owner_inactive" in caplog.text
    assert world.tasks.get_task_info(schedule.payload.task_id) is None, "nothing ran for her, or for nobody"


# -- records saved before the person was recorded --------------------------------------------------


_OLD_TABLES = (
    """
    CREATE TABLE after_action_report_reviews (
        review_record_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, report_id TEXT NOT NULL, batch_id TEXT,
        owner_agent_id TEXT, owner_agent_name TEXT, team_id TEXT, owner_named_agent_type TEXT,
        source_task_id TEXT, parent_task_id TEXT, source_agent_id TEXT, source_agent_name TEXT,
        source_named_agent_type TEXT, status TEXT NOT NULL, created_at TEXT, updated_at TEXT,
        reviewed_at TEXT, stale_reason TEXT, review_data TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE after_action_report_review_batches (
        batch_record_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, owner_agent_id TEXT, owner_agent_name TEXT,
        team_id TEXT, owner_named_agent_type TEXT, parent_task_id TEXT NOT NULL, status TEXT NOT NULL,
        opened_at TEXT, accept_until TEXT, scheduled_review_at TEXT, scheduled_message_id TEXT,
        scheduled_review_task_id TEXT, final_history_entry_id TEXT, report_count INTEGER,
        stale_report_count INTEGER, created_at TEXT, updated_at TEXT, closed_at TEXT, batch_data TEXT NOT NULL
    )
    """,
)
_NEW_FIELDS = {"person_id", "identity_verified"}


def _save_old_rows(db_path: str, *, batch_id: str, parent_task_id: str, report_id: str, schedule_id: str | None) -> None:
    """A batch and a pending review as the previous release stored them: no person columns or fields."""

    batch = AfterActionReportReviewBatchRecord(
        batch_id=batch_id,
        owner_agent_id=MANAGER_ID,
        team_id=TEAM,
        parent_task_id=parent_task_id,
        accept_until=COMPLETED_AT,
        scheduled_review_at=COMPLETED_AT,
        scheduled_message_id=schedule_id,
        scheduled_review_task_id=f"task_review_{batch_id}" if schedule_id else None,
    )
    review = AfterActionReportReviewRecord(
        report_id=report_id,
        batch_id=batch_id,
        owner_agent_id=MANAGER_ID,
        team_id=TEAM,
        parent_task_id=parent_task_id,
        summary="A lesson from before the upgrade.",
    )
    with sqlite3.connect(db_path) as conn:
        for statement in _OLD_TABLES:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO after_action_report_review_batches (batch_record_id, batch_id, owner_agent_id, team_id, "
            "parent_task_id, status, opened_at, accept_until, scheduled_review_at, scheduled_message_id, "
            "scheduled_review_task_id, batch_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{MANAGER_ID}::{batch_id}",
                batch_id,
                MANAGER_ID,
                TEAM,
                parent_task_id,
                batch.status.value,
                batch.opened_at,
                batch.accept_until,
                batch.scheduled_review_at,
                schedule_id,
                batch.scheduled_review_task_id,
                batch.model_dump_json(exclude=_NEW_FIELDS),
            ),
        )
        conn.execute(
            "INSERT INTO after_action_report_reviews (review_record_id, review_id, report_id, batch_id, "
            "owner_agent_id, team_id, parent_task_id, status, created_at, updated_at, review_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{MANAGER_ID}::{review.review_id}",
                review.review_id,
                report_id,
                batch_id,
                MANAGER_ID,
                TEAM,
                parent_task_id,
                review.status.value,
                review.created_at,
                review.updated_at,
                review.model_dump_json(exclude=_NEW_FIELDS),
            ),
        )


def _old_review_schedule(batch_id: str, schedule_id: str) -> ScheduledMessage:
    """The review schedule as the previous release saved it: no gateway context."""

    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=f"after_action_report_batch_review:{batch_id}",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.AFTER_ACTION_REPORT_REVIEW,
        payload=Task(task_id=f"task_review_{batch_id}", task_type=MessageMetaType.REQUEST, problem_statement="Review."),
        payload_header=MessageHeader(from_agent_id=MANAGER_ID, to_routing_path="self", agent_signature=""),
        payload_meta=MessageMeta(
            from_agent_name="lead", requester_type=RequesterType.TIMER_REQUESTER, is_response_requested=False
        ),
        repeat_count=1,
        next_fire_at=COMPLETED_AT,
    )


def test_old_scheduled_batches_are_closed_unreviewed(tmp_path: Path, keys, caplog) -> None:
    db_path = str(tmp_path / "manager.db")
    _save_old_rows(db_path, batch_id="aar_batch_old", parent_task_id="task_old", report_id="aar_old", schedule_id="sched_old")
    world = _build_world(db_path)  # opening the stores adds the new columns
    world.store.add(_old_review_schedule("aar_batch_old", "sched_old"), claim_team_id=TEAM, claim_named_agent_type=LEAD_TYPE)

    with caplog.at_level(logging.WARNING, logger="custom_agents.task_manager_after_action_reports"):
        _fire(world, world.store.get_scheduled_message("sched_old"))

    assert "event=after_action_report_batch_unverified_closed" in caplog.text
    assert world.tasks.get_task_info("task_review_aar_batch_old") is None, "no review ran, for anyone"
    repository = world.tasks.after_action_report_repository
    batch = repository.get_batch("aar_batch_old", owner_agent_id=MANAGER_ID)
    assert batch.status is AfterActionReportReviewBatchStatus.CLOSED and not batch.identity_verified
    [review] = repository.list_reviews_for_batch("aar_batch_old", owner_agent_id=MANAGER_ID)
    assert (review.status, review.stale_reason) == (AfterActionReportReviewStatus.IGNORED, UNVERIFIED_IDENTITY_STALE_REASON)


def test_old_pending_reviews_are_never_listed_and_old_open_batches_close_with_their_task(tmp_path: Path, keys) -> None:
    db_path = str(tmp_path / "manager.db")
    _save_old_rows(db_path, batch_id="aar_batch_open", parent_task_id="task_parent_ann", report_id="aar_old", schedule_id=None)
    world = _build_world(db_path)
    meta = _person_meta(ANN)
    parent = _parent_task(world, "task_parent_ann", meta)
    _receive(world, _report("aar_new", parent.task_id), meta.model_copy(deep=True))

    assert _review_ids(world, ANN) == ["aar_new"] and _review_ids(world, None) == []
    _complete(world, parent.task_id)

    old = world.tasks.after_action_report_repository.get_batch("aar_batch_open", owner_agent_id=MANAGER_ID)
    assert old.status is AfterActionReportReviewBatchStatus.CLOSED, "closed with its task, without a review"
    [schedule] = _review_schedules(world)
    assert _owner(schedule) == ANN, "only the new, verified batch is reviewed"


# -- the specialist's side ------------------------------------------------------------------------


def test_the_specialists_runtime_sets_the_reports_task_links(keys, caplog) -> None:
    worker = SimpleNamespace(
        id="worker_1",
        name="researcher",
        team_id=TEAM,
        named_agent_type="WebResearchAgent",
        named_agent_type_queue_map={},
        _agent_id_by_name={},
        _logger=logging.getLogger("test.worker"),
        _record_runtime_repair_anomaly=AsyncMock(),
        team_comms=SimpleNamespace(send_message=AsyncMock()),
    )
    source = Task(
        task_id="task_child", parent_task_id="task_parent_ann", task_type=MessageMetaType.RESPONSE, problem_statement="x"
    )
    # The model named another person's task.
    report = AfterActionReport(report_id="aar_1", parent_task_id="task_parent_bob", source_task_id="task_x", summary="s")

    with caplog.at_level(logging.WARNING, logger="test.worker"):
        sent = asyncio.run(
            UniversalAgent._send_after_action_report_followup(
                worker,
                report=report,
                source_task=source,
                incoming_header=MessageHeader(from_agent_id=MANAGER_ID, response_routing_key=MANAGER_ID, agent_signature=""),
                incoming_meta=_person_meta(ANN),
            )
        )

    assert sent and "event=after_action_report_links_replaced" in caplog.text
    envelope = worker.team_comms.send_message.await_args.kwargs["message"]
    assert (envelope.msg_content.source_task_id, envelope.msg_content.parent_task_id) == ("task_child", "task_parent_ann")
    assert envelope.msg_meta.gateway_context.canonical_user_id == ANN, "the report carries its task's identity"


# -- saving a lesson to the knowledge base ---------------------------------------------------


def _knowledge_agent(world, task_id: str) -> SimpleNamespace:
    agent = _access_agent(world, task_id)
    agent._knowledge_repository = create_knowledge_repository(world.tasks.storage_settings)
    agent.knowledge_config = {}
    return agent


@pytest.fixture(params=BACKENDS)
def lesson_store(request, tmp_path):
    """The lesson write uses real SQLite or PostgreSQL; the review remains in its own store."""

    with knowledge_storage(request.param, tmp_path) as settings:
        yield create_knowledge_repository(settings)


def _review_task(world, report_id: str, meta: MessageMeta) -> str:
    parent = _parent_task(world, f"task_parent_{report_id}", meta)
    _receive(world, _report(report_id, parent.task_id), meta.model_copy(deep=True))
    _complete(world, parent.task_id)
    [schedule] = [s for s in _review_schedules(world) if s.schedule_id not in world.fired]
    world.fired.add(schedule.schedule_id)
    _fire(world, schedule)
    return schedule.payload.task_id


def _save(**fields) -> dict:
    return json.loads(
        resolve_after_action_report_review(status="saved_to_knowledge", decision_summary="Worth keeping.", **fields)
    )


@needs_cedar
def test_a_lesson_from_a_persons_review_is_proposed_for_the_curator(world) -> None:
    from knowledge_base.vocabulary import ContributionStatus

    world.fired = set()
    agent = _knowledge_agent(world, _review_task(world, "aar_ann", _person_meta(ANN)))
    set_agent_ref(agent)

    missing = _save(report_id="aar_ann", knowledge_page="team/release-lessons")
    assert "needs knowledge_page" in missing["error"]
    wrong_status = json.loads(
        resolve_after_action_report_review(
            status="ignored", decision_summary="No.", report_id="aar_ann", knowledge_body="Text."
        )
    )
    assert "only for saved_to_knowledge" in wrong_status["error"]
    secret = _save(report_id="aar_ann", knowledge_page="team/x", knowledge_title="X", knowledge_body="token: ghp_" + "a" * 36)
    assert secret["error_code"] == "sensitive_content"
    assert _review_ids(world, ANN) == ["aar_ann"], "a refused lesson leaves the review waiting"

    saved = _save(
        report_id="aar_ann", knowledge_page="team/release-lessons", knowledge_title="Release lessons",
        knowledge_summary="What releases taught us.", knowledge_body="Run CI before tagging.",
    )
    review = saved["review"]
    assert review["status"] == "saved_to_knowledge"
    repository = agent._knowledge_repository
    [contribution] = repository.list_contributions(
        team_id=TEAM, owner_person_id=ANN, status=ContributionStatus.PENDING, limit=5
    )
    assert contribution.slug == "release-lessons" and contribution.body == "Run CI before tagging."
    assert contribution.contribution_id in review["decision_summary"]
    assert repository.export_rows("knowledge_pages") == [], "the team sees it only after the curator's review"
    again = _save(report_id="aar_ann", knowledge_page="team/other", knowledge_title="Other", knowledge_body="Again.")
    assert "already resolved" in again["error"]


def test_a_lesson_from_a_review_of_work_for_nobody_is_published(world) -> None:
    world.fired = set()
    agent = _knowledge_agent(world, _review_task(world, "aar_system", _system_meta()))
    # With authorization on, the team lead holds no access in work for nobody
    # (no person to check it against), so it cannot resolve this review at all;
    # with it off, the review's lesson is system work and is published.
    agent.authorization_config = {"enabled": False}
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(agent)

    saved = _save(
        report_id="aar_system", knowledge_page="team/maintenance-lessons", knowledge_title="Maintenance lessons",
        knowledge_body="Restart the worker after a model change.",
    )
    assert saved["review"]["status"] == "saved_to_knowledge", saved
    assert "team/maintenance-lessons (revision 1)" in saved["review"]["decision_summary"]
    [page] = agent._knowledge_repository.export_rows("knowledge_pages")
    assert page["slug"] == "maintenance-lessons" and page["body"] == "Restart the worker after a model change."


@pytest.mark.parametrize("summary_fields,expected", [({}, "Useful overview"), ({"knowledge_summary": ""}, "")])
def test_a_lesson_edit_preserves_an_omitted_summary_and_clears_an_explicit_empty_summary(world, lesson_store, summary_fields, expected) -> None:
    from custom_tools.providers.knowledge import knowledge_actor, knowledge_service
    from knowledge_base.service import DraftInput

    world.fired = set()
    agent = _knowledge_agent(world, _review_task(world, "aar_system", _system_meta()))
    agent._knowledge_repository = lesson_store
    agent.authorization_config = {"enabled": False}
    access_runtime._runtime_access.cache_clear()
    set_agent_ref(agent)
    service, actor = knowledge_service(agent), knowledge_actor(agent)
    page = service.write(
        actor, "team/maintenance-lessons",
        DraftInput(title="Maintenance lessons", body="Initial worker maintenance lesson.", summary="Useful overview"),
    ).page

    saved = _save(
        report_id="aar_system", knowledge_page="team/maintenance-lessons",
        knowledge_body="Restart the worker after a model change.", knowledge_base_revision=page.revision,
        **summary_fields,
    )

    assert saved["review"]["status"] == "saved_to_knowledge", saved
    edited = service.read(actor, page.reference()).page
    assert edited.summary == expected and edited.revision == 2
    assert edited.title == page.title and edited.body == "Restart the worker after a model change."


def test_a_lesson_is_refused_for_another_persons_review(world, caplog) -> None:
    from custom_agents.after_action_report_knowledge import (
        CODE_PERSON_MISMATCH,
        KnowledgeLesson,
        LessonRefused,
        save_review_lesson,
    )
    from custom_messages.records.reviews import AfterActionReportReviewRecord

    world.fired = set()
    agent = _knowledge_agent(world, _review_task(world, "aar_ann", _person_meta(ANN)))
    [ann_review] = world.tasks.list_after_action_report_reviews(person_id=ANN)
    bobs = AfterActionReportReviewRecord.model_validate({**ann_review.model_dump(), "person_id": BOB})
    with caplog.at_level(logging.WARNING), pytest.raises(LessonRefused) as refused:
        save_review_lesson(agent, bobs, KnowledgeLesson(page="team/x", title="X", body="Text."))
    assert refused.value.payload["error_code"] == CODE_PERSON_MISMATCH
    assert "event=aar_knowledge_person_mismatch" in caplog.text and BOB not in caplog.text
    assert agent._knowledge_repository.export_rows("knowledge_contributions") == []


@needs_cedar
@pytest.mark.parametrize("space", ["team", "personal", "system"])
def test_a_saved_lesson_resumes_after_resolution_failure_and_restart(world, monkeypatch, space) -> None:
    from storage.knowledge_repository import create_knowledge_repository

    world.fired = set()
    meta = _system_meta() if space == "system" else _person_meta(ANN)
    task_id = _review_task(world, "aar_retry", meta)
    agent = _knowledge_agent(world, task_id)
    if space == "system":
        world.manager.authorization_config["enabled"] = False
        access_runtime._runtime_access.cache_clear()
    set_agent_ref(agent)
    fields = {
        "report_id": "aar_retry", "knowledge_page": f"{'personal' if space == 'personal' else 'team'}/lesson-retry",
        "knowledge_title": "Release lessons", "knowledge_summary": "Lessons for releases.",
        "knowledge_body": "Run CI before tagging.",
    }

    def resolution_unavailable(*args, **kwargs):
        raise RuntimeError("Review storage temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(world.tasks, "resolve_after_action_report_review", resolution_unavailable)
        failed = _save(**fields)
    assert "knowledge_saved" in failed
    assert _review_ids(world, None if space == "system" else ANN) == ["aar_retry"]

    # Recreate both storage owners, like a process restart. Recovery must use
    # persisted state, not a cached result from the first tool call.
    restarted = _build_world(world.db_path)
    restarted.tasks.restore_runtime_state()
    restarted.manager.authorization_config["enabled"] = space != "system"
    agent = _knowledge_agent(restarted, task_id)
    set_agent_ref(agent)
    changed = _save(**{**fields, "knowledge_body": "A different lesson."})
    assert changed.get("error_code") == "saved_write_mismatch", changed
    saved = _save(**fields)
    assert saved["review"]["status"] == "saved_to_knowledge", saved
    assert failed["knowledge_saved"] in saved["review"]["decision_summary"]
    repository = create_knowledge_repository(world.tasks.storage_settings)
    assert len(repository.export_rows("knowledge_contributions")) == int(space == "team")
    assert len(repository.export_rows("knowledge_pages")) == int(space != "team")
    assert len(repository.export_rows("knowledge_revisions")) == int(space != "team")
