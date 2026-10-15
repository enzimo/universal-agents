from __future__ import annotations

from pathlib import Path

import pytest

from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    SystemScheduleMutationDenied,
    build_schedule_mutation_permit,
    schedule_control_state_digest,
)
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind


def _system_schedule() -> ScheduledMessage:
    return ScheduledMessage(
        schedule_id="hb_sched_test",
        schedule_name="heartbeat:test",
        management_class=ScheduleManagementClass.SYSTEM,
        system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
        payload=Task(
            task_type=MessageMetaType.REQUEST,
            problem_statement="Launch heartbeat items.",
        ),
        payload_meta=MessageMeta(
            gateway_context=GatewayContext(
                canonical_user_id="usr_owner",
                heartbeat_source_id="hb_source_test",
            )
        ),
        interval_ms=60_000,
        repeat_count=None,
        claim_team_id="team_1",
    )


def _store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(db_path=str(tmp_path / "schedules.db"))


def test_missing_schedule_provenance_fails_safe_as_system() -> None:
    schedule = ScheduledMessage(
        payload=Task(task_type=MessageMetaType.REQUEST),
        interval_ms=1_000,
    )

    assert schedule.management_class == ScheduleManagementClass.SYSTEM
    assert schedule.effective_system_schedule_kind == SystemScheduleKind.UNCLASSIFIED


def test_store_rejects_unpermitted_system_delete_disable_and_downgrade(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    schedule = _system_schedule()
    store.add(schedule)

    with pytest.raises(SystemScheduleMutationDenied, match="authenticated authority"):
        store.remove(schedule.schedule_id)
    with pytest.raises(SystemScheduleMutationDenied, match="authenticated authority"):
        store.set_enabled(schedule.schedule_id, False)

    downgraded = schedule.model_copy(deep=True)
    downgraded.management_class = ScheduleManagementClass.USER
    downgraded.system_schedule_kind = None
    with pytest.raises(
        SystemScheduleMutationDenied,
        match="cannot be replaced as user-managed",
    ):
        store.add(downgraded)

    restored = store.get_scheduled_message(schedule.schedule_id)
    assert restored is not None
    assert restored.enabled is True
    assert restored.management_class == ScheduleManagementClass.SYSTEM


def test_exact_authenticated_permit_allows_only_its_bound_transition(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    schedule = _system_schedule()
    store.add(schedule)
    candidate = schedule.model_copy(deep=True)
    candidate.enabled = False
    permit = build_schedule_mutation_permit(
        schedule=schedule,
        operation=ScheduleMutationOperation.DISABLE,
        authority_mode=ScheduleMutationAuthorityMode.AUTHENTICATED_GATEWAY_USER,
        actor_principal_id="usr_owner",
        owner_canonical_user_id="usr_owner",
        team_id="team_1",
        candidate=candidate,
    )

    with pytest.raises(SystemScheduleMutationDenied, match="does not match"):
        store.remove(schedule.schedule_id, system_mutation_permit=permit)

    assert store.set_enabled(
        schedule.schedule_id,
        False,
        operation=ScheduleMutationOperation.DISABLE,
        system_mutation_permit=permit,
    )
    restored = store.get_scheduled_message(schedule.schedule_id)
    assert restored is not None
    assert restored.enabled is False
    assert schedule_control_state_digest(restored) != permit.before_state_digest

    with pytest.raises(SystemScheduleMutationDenied, match="does not match"):
        store.set_enabled(
            schedule.schedule_id,
            False,
            operation=ScheduleMutationOperation.DISABLE,
            system_mutation_permit=permit,
        )


def test_user_schedule_mutations_need_no_system_permit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    schedule = _system_schedule().model_copy(deep=True)
    schedule.schedule_id = "sched_user"
    schedule.management_class = ScheduleManagementClass.USER
    schedule.system_schedule_kind = None
    store.add(schedule)

    assert store.set_enabled(schedule.schedule_id, False)
    assert store.remove(schedule.schedule_id)
