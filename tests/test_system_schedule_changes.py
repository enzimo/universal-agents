"""Agent schedule changes under access model 2, with real Cedar and SQLite.

- A person's own schedule: the tools are decided per call (fn:schedules) and
  check the owner; the change applies at once and the store needs no permit.
  Another person's schedule is "not found".
- A schedule the runtime manages: the tool stages an exact change
  (``system_schedule``); ``apply_change`` pauses the task until a person
  approves, then applies it once with a ``person_approval`` permit. A replay
  and a schedule that changed after staging are refused. With authorization
  off the person's agreement applies it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import InterruptException

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    build_schedule_mutation_permit,
)
from custom_agents.schedule_store import AdvanceOutcome, ScheduleStore
from custom_messages.contracts.access_v2 import AccessRequestKind, AccessRequestStatus, AccessReviewChoice
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_tools.providers.schedules import (
    disable_scheduled_job,
    pause_scheduled_job,
    remove_scheduled_job,
    resume_scheduled_job,
    update_scheduled_job,
)
from custom_tools.runtime_context import set_agent_ref
from tests.change_approval_support import (
    CEDAR_BINARY,
    ToolCall,
    change_agent,
    decide,
    needs_cedar,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)

TEAM = "team_sched"
PERSON = "user_alice"
OTHER_PERSON = "user_bob"
PERMITTED = "event=system_schedule_change_permitted"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _lead(tmp_path: Path, *, authorization_enabled: bool) -> SimpleNamespace:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="schedule-test",
        instance_root=str(root),
        team_id=TEAM,
        timezone="UTC",
        manager_storage_sqlite_path="runtime/task_history.db",
    )
    config.user.canonical_user_id = PERSON
    config.authorization.enabled = authorization_enabled
    config.authorization.cedar_binary_path = str(CEDAR_BINARY)
    config_path = config.save(root / "instance_config.yaml")
    return change_agent(
        config,
        config_path,
        authorization_enabled=authorization_enabled,
        _schedule_store=ScheduleStore(storage_settings=config.manager_storage_settings),
        _scheduler_wake=None,
        _logger=logging.getLogger("test.system_schedule_changes"),
    )


def _schedule(
    agent: Any,
    schedule_id: str,
    *,
    owner: str = PERSON,
    system: bool = False,
) -> ScheduledMessage:
    schedule = ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=f"heartbeat:{schedule_id}" if system else f"reminder {schedule_id}",
        management_class=ScheduleManagementClass.SYSTEM if system else ScheduleManagementClass.USER,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER if system else None,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check the garden."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="agent_team_lead", agent_signature=""),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner)),
        interval_ms=3_600_000,
        repeat_count=None,
        claim_team_id=TEAM,
    )
    agent._schedule_store.add(schedule)
    return schedule


def _call(agent: Any, tool: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    set_agent_ref(agent)
    return json.loads(tool(*args, **kwargs))


def _stored(agent: Any, schedule_id: str) -> ScheduledMessage | None:
    return agent._schedule_store.get_scheduled_message(schedule_id)


@needs_cedar
def test_owner_pauses_and_resumes_own_schedule_without_a_permit(tmp_path: Path, caplog) -> None:
    lead = _lead(tmp_path, authorization_enabled=True)
    _schedule(lead, "sched_own")
    _schedule(lead, "sched_bob", owner=OTHER_PERSON)

    with caplog.at_level(logging.INFO):
        paused = _call(lead, pause_scheduled_job, "sched_own", duration_minutes=30)
        resumed = _call(lead, resume_scheduled_job, "sched_own")
        foreign = _call(lead, pause_scheduled_job, "sched_bob", duration_minutes=30)
        foreign_removal = _call(lead, remove_scheduled_job, "sched_bob")

    assert paused["paused"] is True and "status" not in paused
    assert resumed["resumed"] is True
    stored = _stored(lead, "sched_own")
    assert stored is not None and stored.paused_until is None and stored.enabled is True
    assert foreign == {"schedule_id": "sched_bob", "paused": False, "error": "schedule not found"}
    assert foreign_removal == {"schedule_id": "sched_bob", "removed": False, "error": "schedule not found"}
    assert _stored(lead, "sched_bob").paused_until is None
    # A person's own schedule needs no permit, so no protected change was permitted.
    assert PERMITTED not in caplog.text
    assert "authority_mode=owner" in caplog.text


@needs_cedar
def test_system_schedule_change_waits_for_a_person_then_applies_once(tmp_path: Path, caplog) -> None:
    lead = _lead(tmp_path, authorization_enabled=True)
    _schedule(lead, "hb_sched_team", system=True)
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat()

    staged = _call(lead, pause_scheduled_job, "hb_sched_team", until_iso=until)

    assert staged["status"] == "approval_required"
    assert staged["paused"] is False
    assert "a person approves this exact change in the Inbox" in staged["next_step"]
    assert _stored(lead, "hb_sched_team").paused_until is None

    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(lead, staged["change_id"], call, reason="Pause the heartbeat for maintenance.")
    request = stored_request(lead, call)
    assert request.kind is AccessRequestKind.CHANGE
    assert request.status is AccessRequestStatus.WAITING
    assert request.change is not None and request.change.kind == "system_schedule"
    assert request.target.id == "fn:system_schedules#change:system_schedule"
    assert any(line.startswith("Pauses it until ") for line in request.change.effect)
    assert request.change.summary == f"{request.change.title}. Takes effect when approved."
    assert _stored(lead, "hb_sched_team").paused_until is None

    decide(lead, call)
    with caplog.at_level(logging.INFO):
        applied = run_apply_change(lead, staged["change_id"], call, reason="Pause the heartbeat for maintenance.")

    assert applied["status"] == "applied"
    assert applied["approval"]["mode"] == "person_approval"
    assert applied["approval"]["access_request_id"] == request.request_id
    assert applied["result"]["paused"] is True
    assert applied["result"]["authority"]["mode"] == "person_approval"
    assert "_report" not in applied["result"]
    stored = _stored(lead, "hb_sched_team")
    assert stored is not None and stored.paused_until == until
    assert PERMITTED in caplog.text and "authority_mode=person_approval" in caplog.text

    replay = run_apply_change(lead, staged["change_id"], ToolCall("tooluse_replay"))
    assert replay["status"] == "refused"
    assert "already applied" in replay["reason"]


@needs_cedar
def test_a_run_between_staging_and_approval_does_not_make_the_change_stale(tmp_path: Path) -> None:
    """The approval binds what the person reviewed; a regular run only moves the next run time."""

    lead = _lead(tmp_path, authorization_enabled=True)
    _schedule(lead, "hb_sched_ran", system=True)
    staged = _call(lead, disable_scheduled_job, "hb_sched_ran")
    assert staged["status"] == "approval_required"

    # The scheduler runs the schedule, which moves its next run time.
    assert lead._schedule_store.update_after_fire("hb_sched_ran") == AdvanceOutcome.ADVANCED
    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(lead, staged["change_id"], call)
    decide(lead, call)
    assert lead._schedule_store.update_after_fire("hb_sched_ran") == AdvanceOutcome.ADVANCED  # and again while it waited
    applied = run_apply_change(lead, staged["change_id"], call)

    assert applied["status"] == "applied", applied
    assert _stored(lead, "hb_sched_ran").enabled is False


@needs_cedar
def test_system_schedule_whose_settings_changed_after_staging_is_refused_before_review(tmp_path: Path) -> None:
    lead = _lead(tmp_path, authorization_enabled=True)
    schedule = _schedule(lead, "hb_sched_moved", system=True)
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat()
    staged = _call(lead, pause_scheduled_job, "hb_sched_moved", until_iso=until)
    assert staged["status"] == "approval_required"

    # Trusted runtime code disables the schedule meanwhile: a real settings change.
    disabled = schedule.model_copy(deep=True)
    disabled.enabled = False
    permit = build_schedule_mutation_permit(
        schedule=_stored(lead, "hb_sched_moved"),
        operation=ScheduleMutationOperation.DISABLE,
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id="runtime",
        owner_canonical_user_id=PERSON,
        team_id=TEAM,
        candidate=disabled,
    )
    assert lead._schedule_store.set_enabled("hb_sched_moved", False, system_mutation_permit=permit)
    call = ToolCall()
    refused = run_apply_change(lead, staged["change_id"], call)

    assert refused["status"] == "refused"
    assert "changed after this change was staged" in refused["reason"]
    assert call.reasons == []  # nobody was asked to approve a stale change
    assert _stored(lead, "hb_sched_moved").paused_until is None



@needs_cedar
def test_denied_system_schedule_change_is_not_applied(tmp_path: Path) -> None:
    lead = _lead(tmp_path, authorization_enabled=True)
    _schedule(lead, "hb_sched_keep", system=True)
    staged = _call(lead, remove_scheduled_job, "hb_sched_keep")

    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(lead, staged["change_id"], call)
    decide(lead, call, AccessReviewChoice.DENY)
    denied = run_apply_change(lead, staged["change_id"], call)

    assert denied["status"] == "denied"
    assert _stored(lead, "hb_sched_keep") is not None


def test_authorization_off_applies_system_schedule_change_with_agreement(tmp_path: Path, caplog) -> None:
    lead = _lead(tmp_path, authorization_enabled=False)
    _schedule(lead, "hb_sched_off", system=True)

    staged = _call(lead, update_scheduled_job, "hb_sched_off", interval_seconds=120)
    assert staged["status"] == "approval_required"
    assert "confirmed=true" in staged["next_step"]

    unconfirmed = run_apply_change(lead, staged["change_id"])
    assert unconfirmed["status"] == "agreement_required"
    assert _stored(lead, "hb_sched_off").interval_ms == 3_600_000

    with caplog.at_level(logging.INFO):
        applied = run_apply_change(lead, staged["change_id"], confirmed=True)

    assert applied["status"] == "applied"
    assert applied["approval"]["mode"] == "agreement"
    assert applied["result"]["updated"] is True
    assert _stored(lead, "hb_sched_off").interval_ms == 120_000
    assert "authority_mode=agreement" in caplog.text


def test_system_schedule_plan_belongs_to_the_task_that_staged_it(tmp_path: Path) -> None:
    lead = _lead(tmp_path, authorization_enabled=False)
    _schedule(lead, "hb_sched_task", system=True)
    staged = _call(lead, resume_scheduled_job, "hb_sched_task")

    lead.task_manager.current_task_id = "another_task"
    refused = run_apply_change(lead, staged["change_id"], confirmed=True)

    assert refused["status"] == "refused"
    assert "another agent, person or task" in refused["reason"]
