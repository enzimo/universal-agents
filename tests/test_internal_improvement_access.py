"""InternalImprovementAgent's maintenance runs are system work (access plan 7c).

Real Cedar, SQLite, TaskManager, instance folders and schedule store. The
scheduler signs each maintenance run for no person, and InternalImprovementAgent
is a built-in system agent, so the run may do its duties: files in ``agents/``
and ``team/``, sandboxed code, memory-review cleanup, model lookup, and
recording its own schedule's run. It reaches nothing of any person's: not their
folders and not their schedules. A worker given the same context is refused.

MemReviewAgent's runs work for the schedule's owner, so the person side
applies to them: a member's review may read that member's review jobs, and
only those.

When a maintenance run escalates to TeamArchitect, the team lead's task works
for the maintenance schedule's stored owner (the first user), with their
current roles from the gateway's copy of the team's people, so findings reach
a person; a person named in the escalation itself is ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.identity import ISSUER_SCHEDULER, ContextVerifier
from custom_agents import folder_access
from custom_agents.access_runtime import decide_call, verified_person_id
from custom_agents.code_access import CODE_SHELL_TOOL_ID, code_access
from custom_agents.internal_improvement_runtime import (
    build_internal_improvement_schedule,
    enqueue_internal_improvement_job,
    prepare_internal_improvement_schedule_for_manager,
)
from custom_agents.memory_review_store import MemoryReviewStore
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_context_runtime import sign_system_context, sign_system_escalation
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    ManagerEscalationContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, ScheduleManagementClass
from custom_tools.non_interactive_file_tools import file_write
from custom_tools.providers.memory import resolve_memory_review_job
from custom_tools.providers.memory_review_inspection import get_memory_review_job, list_pending_memory_review_jobs
from custom_tools.providers.schedules import (
    get_schedule_telemetry,
    list_scheduled_jobs,
    record_scheduled_run,
    update_scheduled_job,
)
from tests.task_work_support import acting_as, set_agent_ref
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import InstanceLayout
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from storage.access_repository import AccessRepository
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key
from tests.test_internal_improvement_runtime import _Manager, _Worker
from tests.tool_result_support import tool_json

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = _Manager.team_id
# The person who set up the instance: the maintenance schedule's stored owner,
# whose own schedule and folder the maintenance run must not reach.
FIRST_USER = "user_123"
MEMBER = "bob"
OTHER_MEMBER = "carol"
SYSTEM_TYPES = ("InternalImprovementAgent", "KnowledgeCuratorAgent", "MemReviewAgent")
MAINTENANCE_TASK = "maintenance_task"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
REVIEW_TASK = "member_review_task"

def verified_person_for(agent):
    """Whom ``agent`` verifiably works for in the task it works on (tests.task_work_support)."""

    with acting_as(agent):
        return verified_person_id(agent)


def decide_for(agent, function, arguments):
    """The access hook's decision for ``agent`` in the task it works on (tests.task_work_support)."""

    with acting_as(agent):
        return decide_call(agent, function, arguments)


pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


def _dispatched_maintenance_run() -> tuple[ScheduledMessage, object]:
    """The manager's dispatch of one maintenance run, as the worker receives it."""

    manager = _Manager()
    schedule = prepare_internal_improvement_schedule_for_manager(
        manager, build_internal_improvement_schedule(_Worker("worker_maintenance"))
    )
    assert schedule is not None
    schedule.next_fire_at = "2026-09-25T12:00:00+00:00"
    assert asyncio.run(enqueue_internal_improvement_job(manager, schedule.schedule_id, schedule))
    return schedule, manager.team_comms.sent[0][0]


def _person_schedule() -> ScheduledMessage:
    return ScheduledMessage(
        schedule_id="sched_first_user_reminder",
        schedule_name="Water the plants",
        # People's schedules say so explicitly; missing provenance counts as system-managed.
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Remind me to water the plants."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="manager_1"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=FIRST_USER)),
        interval_ms=86_400_000,
    )


def _member_review_meta() -> MessageMeta:
    signed = context_signer().issue(team_id=TEAM, person_id=MEMBER, person_roles=("member",))
    return MessageMeta(
        conversation_id="conversation_member",
        requester_type=RequesterType.TIMER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=MEMBER, task_context=signed),
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    use_scheduler_key(monkeypatch)
    folder_access.forget_kept_answers()

    schedule, envelope = _dispatched_maintenance_run()
    tasks = TaskManager(db_path=str(layout.root / "manager.db"))
    tasks.set_owner_identity("agent-InternalImprovementAgent", "InternalImprovementAgent", TEAM, "InternalImprovementAgent")
    envelope.msg_content.task_id = MAINTENANCE_TASK
    tasks.add_task(envelope.msg_content, envelope.msg_header, envelope.msg_meta, owner_person_id=None)
    tasks.add_task(Task(task_id=REVIEW_TASK, task_type=MessageMetaType.REQUEST), MessageHeader(), _member_review_meta(), owner_person_id=None)
    schedules = ScheduleStore(db_path=str(layout.root / "schedules.db"))
    schedules.add(schedule)
    schedules.add(_person_schedule())
    reviews = MemoryReviewStore(str(layout.root / "reviews.db"), logger=logging.getLogger("test.reviews"))

    def agent(
        agent_type: str, *, role: str = "system", task_id: str = MAINTENANCE_TASK, lead: bool = False
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"agent-{agent_type}",
            name=agent_type,
            named_agent_type=agent_type,
            team_id=TEAM,
            is_team_lead=lead,
            access_role=role,
            authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
            task_manager=tasks,
            instance_root=str(layout.root),
            workspace_dir=str(layout.workspaces_dir),
            test_task_id=task_id,
            _schedule_store=schedules,
            _memory_review_store=reviews,
            _conversation_store=None,
            _scheduler_wake=None,
            named_agent_type_queue_map={},
            team_comms=None,
            _logger=logging.getLogger(f"test.{agent_type}"),
        )

    yield SimpleNamespace(
        layout=layout, schedule=schedule, reviews=reviews, agent=agent, tasks=tasks, schedules=schedules
    )
    set_agent_ref(None)
    forget_task_context_keys()


def test_every_access_service_knows_the_built_in_system_agents() -> None:
    """The broker, gateway and operator commands read the same system types
    as the agent catalog, from the release definitions."""

    from access_control.entities import AGENT_ROLE_SYSTEM, agent_role_resolver
    from access_control.system_agents import builtin_system_agent_types
    from custom_agents.agent_catalog import load_agent_type_map

    catalog_system = {
        agent_type
        for agent_type, definition in load_agent_type_map(include_configured=False).items()
        if definition.get("access_role") == AGENT_ROLE_SYSTEM
    }
    assert builtin_system_agent_types() == frozenset(SYSTEM_TYPES) == catalog_system
    role_of = agent_role_resolver("TeamArchitect")
    assert all(role_of(agent_type) == AGENT_ROLE_SYSTEM for agent_type in SYSTEM_TYPES)


def test_a_maintenance_run_may_do_its_duties_with_no_person(world) -> None:
    maintenance = world.agent("InternalImprovementAgent")

    for function, arguments in (
        ("file_write", {"path": "report.md"}),
        ("shell", {"command": "cargo build --release"}),
        ("python_env", {"action": "run"}),
        ("inspect_memory_review_cleanup", {}),
        ("maintain_memory_review_jobs", {"operation": "archive"}),
        ("maintain_memory_review_jobs", {"operation": "purge"}),
        ("lookup_model_capability_candidates", {}),
        ("get_runtime_configuration_context", {}),
        ("record_scheduled_run", {}),
        ("request_access", {}),
    ):
        decision = decide_for(maintenance, function, arguments)
        assert decision is not None and decision.allowed, (function, decision and decision.reason)

    # Changing schedules or publishing is not a system duty.
    assert not decide_for(maintenance, "update_scheduled_job", {}).allowed
    assert not decide_for(maintenance, "publish_single_page_site", {}).allowed


def test_its_files_and_code_reach_only_the_shared_folders(world) -> None:
    maintenance = world.agent("InternalImprovementAgent")
    set_agent_ref(maintenance)
    person_folder = world.layout.person_dir(FIRST_USER)
    person_folder.mkdir(parents=True, exist_ok=True)

    written = file_write(path="maintenance-report.md", content="nothing stale")
    refused = file_write(path=str(person_folder / "report.md"), content="should not land")

    assert written["status"] == "success"
    assert (world.layout.agents_dir / "maintenance-report.md").read_text(encoding="utf-8") == "nothing stale"
    assert refused["status"] == "error" and not (person_folder / "report.md").exists()
    planned = code_access(maintenance, current_workspace_scope(maintenance), tool_id=CODE_SHELL_TOOL_ID)
    reachable = {Path(os.path.realpath(path)) for path in (*planned.read_write, *planned.read_only)}
    assert reachable == {
        Path(os.path.realpath(world.layout.agents_dir)),
        Path(os.path.realpath(world.layout.team_dir)),
    }


def test_it_records_and_sees_only_the_schedule_its_context_names(world) -> None:
    set_agent_ref(world.agent("InternalImprovementAgent"))
    schedule_id = world.schedule.schedule_id

    recorded = json.loads(record_scheduled_run(outcome="no_change", state_summary="Nothing stale."))
    listed = json.loads(list_scheduled_jobs())
    telemetry = json.loads(get_schedule_telemetry(include_schedules=True))

    assert recorded["recorded"] is True and recorded["schedule_id"] == schedule_id
    assert [item["schedule_id"] for item in listed["schedules"]] == [schedule_id]
    assert telemetry["summary"]["total"] == 1 and telemetry["schedules"][0]["schedule_id"] == schedule_id
    # Nothing of a person's schedules, even the first user's.
    person_schedule = _person_schedule().schedule_id
    assert json.loads(record_scheduled_run(schedule_id=person_schedule, outcome="success")).get("recorded") is not True
    assert json.loads(list_scheduled_jobs(schedule_id=person_schedule)).get("schedules") in (None, [])
    # A change to its own (system) schedule needs a person.
    changed = tool_json(
        update_scheduled_job(
            schedule_id=schedule_id,
            expected_record_digest=listed["schedules"][0]["record_digest"],
            interval_seconds=60,
        )
    )
    assert changed.get("updated") is not True and "approval_required" not in json.dumps(changed)


def test_a_worker_given_work_for_no_person_is_refused(world) -> None:
    worker = world.agent("WebResearchAgent", role="worker")

    for function in ("file_write", "shell", "record_scheduled_run", "request_access"):
        assert not decide_for(worker, function, {}).allowed, function
    set_agent_ref(worker)
    refused = json.loads(list_scheduled_jobs())
    assert refused["error"] == "system_work_own_schedule_only" and "schedules" not in refused


def test_a_members_memory_review_reads_only_that_members_jobs(world) -> None:
    review = world.agent("MemReviewAgent", task_id=REVIEW_TASK)

    # The person side applies: a member's review needs the member to hold the tool.
    assert decide_for(review, "get_memory_review_job", {}).allowed
    assert decide_for(review, "list_pending_memory_review_jobs", {}).allowed
    # Team-wide cleanup is not a member's to authorize.
    assert not decide_for(review, "maintain_memory_review_jobs", {"operation": "archive"}).allowed

    def job(person: str, index: int):
        return world.reviews.enqueue_review_job(
            owner_team_id=TEAM,
            owner_named_agent_type="MemReviewAgent",
            task_id=f"task_{person}_{index}",
            conversation_id=f"conversation_{person}",
            turn_index=index,
            source_payload={"review_kind": "conversation_window", "canonical_user_id": person, "turns": [person]},
        )

    own = job(MEMBER, 1)
    other = job(OTHER_MEMBER, 2)
    set_agent_ref(review)

    assert json.loads(get_memory_review_job(own.review_job_id))["review_job_id"] == own.review_job_id
    assert "error" in json.loads(get_memory_review_job(other.review_job_id))
    listed = json.loads(list_pending_memory_review_jobs())
    assert [item["review_job_id"] for item in listed["jobs"]] == [own.review_job_id]
    assert json.loads(resolve_memory_review_job(other.review_job_id))["error_code"] == "not_found"
    assert world.reviews.get_review_job(other.review_job_id).status == "queued"
    assert json.loads(resolve_memory_review_job(own.review_job_id))["status"] == "completed"


# -- Escalations from system work reach the maintenance schedule's owner ------

ESCALATION_TASK = "escalation_task"


def _escalation(world, **gateway_fields) -> MessageMeta:
    """TeamArchitect's copy of a maintenance run's escalation, as the worker's delegation builds it."""

    meta = world.tasks.get_task_info(MAINTENANCE_TASK).meta.model_copy(deep=True)
    for name, value in gateway_fields.items():
        setattr(meta.gateway_context, name, value)
    meta.requester_type = RequesterType.AGENT_REQUESTER
    meta.manager_escalation = ManagerEscalationContext(
        escalation_type="worker_result",
        source_task_id=MAINTENANCE_TASK,
        source_schedule_id=world.schedule.schedule_id,
        source_named_agent_type="InternalImprovementAgent",
    )
    return meta


def _store_with(world, name: str, schedule: ScheduledMessage) -> ScheduleStore:
    store = ScheduleStore(db_path=str(world.layout.root / f"{name}.db"))
    store.add(schedule)
    return store


def _record_first_user(world, standing: PersonStanding = PersonStanding.ACTIVE) -> None:
    """What the gateway keeps in the access store: the first user's standing and current roles."""

    roles = ("admin", "owner") if standing is PersonStanding.ACTIVE else ()
    AccessRepository(world.tasks.storage_settings).replace_people(
        TEAM,
        [TeamPersonV1(team_id=TEAM, person_id=FIRST_USER, standing=standing, roles=roles, changed_at_utc=NOW)],
        at=NOW,
    )


def _team_lead(world, store: ScheduleStore) -> SimpleNamespace:
    lead = world.agent("TeamArchitect", role="worker", task_id=ESCALATION_TASK, lead=True)
    lead._schedule_store = store
    return lead


def _admit(world, lead, meta: MessageMeta) -> MessageMeta:
    """What the team lead's admission does (task_message_runtime._handle_task_request)."""

    admitted = sign_system_escalation(lead, meta, task_id=ESCALATION_TASK)
    world.tasks.add_task(Task(task_id=ESCALATION_TASK, task_type=MessageMetaType.REQUEST), MessageHeader(), admitted, owner_person_id=None)
    return admitted


def _verified(meta: MessageMeta):
    return ContextVerifier.from_env().verify(meta.gateway_context.task_context, team_id=TEAM)


def test_a_maintenance_escalation_works_for_the_schedules_owner(world, caplog) -> None:
    # The manager created the schedule itself: nothing the gateway signed is on
    # it, and the owner's roles still come through (the gateway's people record).
    assert world.schedule.payload_meta.gateway_context.task_context is None
    _record_first_user(world)
    lead = _team_lead(world, world.schedules)

    with caplog.at_level(logging.INFO, logger="custom_agents.task_context_runtime"):
        admitted = _admit(world, lead, _escalation(world))

    context = _verified(admitted)
    assert (context.issuer, context.person_id, context.person_roles) == (ISSUER_SCHEDULER, FIRST_USER, ("admin", "owner"))
    assert context.schedule_id == world.schedule.schedule_id
    assert admitted.gateway_context.canonical_user_id == FIRST_USER
    assert "event=system_escalation_signed_for_owner" in caplog.text
    assert world.schedule.schedule_id in caplog.text and FIRST_USER in caplog.text and ESCALATION_TASK in caplog.text
    # TeamArchitect acts for the owner, within the owner's access: it can tell them.
    assert decide_for(lead, "notify_user", {}).allowed
    assert verified_person_for(lead) == FIRST_USER


def test_a_person_named_in_the_escalation_is_ignored(world) -> None:
    _record_first_user(world)
    lead = _team_lead(world, world.schedules)
    forged = _escalation(world, canonical_user_id="mallory")
    forged.manager_escalation.source_schedule_id = _person_schedule().schedule_id

    admitted = _admit(world, lead, forged)

    context = _verified(admitted)
    assert (context.person_id, context.schedule_id) == (FIRST_USER, world.schedule.schedule_id)
    assert admitted.gateway_context.canonical_user_id == FIRST_USER


def test_an_escalation_from_a_persons_task_keeps_that_person(world) -> None:
    lead = _team_lead(world, world.schedules)
    meta = _member_review_meta()
    meta.manager_escalation = ManagerEscalationContext(escalation_type="needs_user_input", source_task_id=REVIEW_TASK)

    admitted = _admit(world, lead, meta)

    assert admitted is meta
    assert verified_person_for(lead) == MEMBER


def _ownerless_maintenance_schedule() -> ScheduledMessage:
    manager = _Manager()
    manager.bootstrap_config = {"instance_name": "nova", "timezone": "America/Chicago"}
    schedule = prepare_internal_improvement_schedule_for_manager(
        manager, build_internal_improvement_schedule(_Worker("worker_ownerless"))
    )
    assert schedule is not None
    return schedule


@pytest.mark.parametrize("reason", ["schedule_not_system_managed", "schedule_owner_missing"])
def test_an_escalation_is_not_signed_for_anyone_without_a_system_schedule_owner(world, caplog, reason) -> None:
    if reason == "schedule_not_system_managed":
        # System work naming a person's own schedule.
        store, escalation = world.schedules, MessageMeta(gateway_context=GatewayContext())
        sign_system_context(escalation, team_id=TEAM, schedule_id=_person_schedule().schedule_id)
        escalation.manager_escalation = ManagerEscalationContext(escalation_type="worker_result")
    else:
        store, escalation = _store_with(world, "ownerless", _ownerless_maintenance_schedule()), _escalation(world)
    lead = _team_lead(world, store)

    with caplog.at_level(logging.WARNING, logger="custom_agents.task_context_runtime"):
        admitted = _admit(world, lead, escalation)

    assert admitted is escalation and _verified(admitted).person_id is None
    assert f"event=system_escalation_refused reason={reason}" in caplog.text
    assert not decide_for(lead, "notify_user", {}).allowed


@pytest.mark.parametrize("standing", [None, PersonStanding.DISABLED, PersonStanding.REMOVED])
def test_the_owner_has_no_roles_unless_the_gateway_recorded_them_as_active(world, caplog, standing) -> None:
    """None: the gateway has not recorded the team's people yet (fails closed)."""

    if standing is not None:
        _record_first_user(world, standing)
    lead = _team_lead(world, world.schedules)

    with caplog.at_level(logging.WARNING, logger="custom_agents.task_context_runtime"):
        admitted = _admit(world, lead, _escalation(world))

    assert (_verified(admitted).person_id, _verified(admitted).person_roles) == (FIRST_USER, ())
    assert "event=scheduled_owner_roles_unavailable" in caplog.text
    assert not decide_for(lead, "notify_user", {}).allowed


@pytest.mark.asyncio
# A delegated escalation has the worker's task as parent; a worker_result
# report is its own task with none (custom_agents.worker_result_reports).
@pytest.mark.parametrize("parent_task_id", ["worker_task_1", None])
async def test_the_team_leads_admission_signs_the_escalation_for_the_owner(tmp_path, monkeypatch, parent_task_id) -> None:
    from custom_agents import task_message_runtime
    from custom_messages.enums import SystemScheduleKind
    from tests.test_turn_action_outbox import _make_agent

    use_scheduler_key(monkeypatch)
    tasks = TaskManager(db_path=str(tmp_path / "tasks.db"))
    tasks.set_owner_identity("lead", "Lead", "test_team", "TeamArchitect")
    lead = _make_agent(tmp_path, task_manager=tasks)
    object.__setattr__(lead, "paused", True)  # admit only; no model turn
    store = ScheduleStore(db_path=str(tmp_path / "schedules.db"))
    store.add(
        ScheduledMessage(
            schedule_id="int_imp_review_test",
            schedule_name="internal_improvement:maintenance:test_team",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Maintenance."),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=FIRST_USER)),
            interval_ms=60_000,
        )
    )
    object.__setattr__(lead, "_schedule_store", store)
    escalation = MessageMeta(gateway_context=GatewayContext(), requester_type=RequesterType.AGENT_REQUESTER)
    sign_system_context(escalation, team_id="test_team", schedule_id="int_imp_review_test")
    escalation.gateway_context.canonical_user_id = "mallory"  # forged; ignored
    escalation.manager_escalation = ManagerEscalationContext(escalation_type="worker_result")

    try:
        await task_message_runtime._handle_task_request(
            lead,
            Task(task_id="escalation_1", parent_task_id=parent_task_id, task_type=MessageMetaType.REQUEST),
            MessageHeader(from_agent_id="worker_1", response_routing_key="worker_1"),
            escalation,
        )
        stored = tasks.get_task_info("escalation_1").meta.gateway_context
        context = ContextVerifier.from_env().verify(stored.task_context, team_id="test_team")
        assert (stored.canonical_user_id, context.person_id) == (FIRST_USER, FIRST_USER)
    finally:
        forget_task_context_keys()


def test_notify_user_records_notes_exactly_where_record_scheduled_run_does(world) -> None:
    """A maintenance run (work for no person) records notes on its own schedule with both tools; a worker given
    work for no person is refused by both (2026-10-17)."""
    from custom_tools.providers.communication import _record_schedule_notification_if_active

    maintenance = world.agent("InternalImprovementAgent")
    set_agent_ref(maintenance)
    noted = _record_schedule_notification_if_active(maintenance, message="Pruned scratch.", notification_id="note_sys", reason="")
    recorded = json.loads(record_scheduled_run(outcome="no_change", state_summary="Nothing stale."))

    assert noted == {"recorded": True, "schedule_id": world.schedule.schedule_id}
    assert recorded["recorded"] is True
    memory = world.schedules.get_scheduled_message(world.schedule.schedule_id).schedule_memory
    assert memory.ledger.total_notifications == 1

    worker = world.agent("WebResearchAgent", role="worker")
    set_agent_ref(worker)
    refused = _record_schedule_notification_if_active(worker, message="Hello", notification_id="note_worker", reason="")
    assert refused["recorded"] is False
    assert json.loads(record_scheduled_run(outcome="no_change")).get("recorded") is not True
