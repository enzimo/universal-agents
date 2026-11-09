"""Real saved assignment state crosses the team services and website boundaries.

The team services list a person's schedules with their worker assignments
(``team_services.schedules``); the gateway reads the contract's rows
(``GatewayScheduleView``) and the website projects them. Frames are stand-ins with
the contract's checks (tests/team_services_support.py).
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.people_directory import PeopleDirectory
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.schedule_management import GatewayScheduleListing, GatewayScheduleView
from assistant_gateway.application.website_services import WebsiteOperationsApplication
from custom_agents.schedule_management import schedule_control_state_digest
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    schedule_listing_digest,
    ScheduledMessage,
)
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
    utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from storage.config import resolve_storage_settings
from team_services.schedules import ScheduleOperations
from tests.people_directory_support import access_store_at
from tests.team_services_support import checked_reply, stand_in_frame

TEAM = "team_presentation"
OWNER, OWNER_REF = "usr_owner", "p-OwnerOwnerOwnerOwnerOw"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy_enabled,pending,other_team,memory_kind",
    [
        (None, False, False, None),
        (True, False, False, None),
        (False, False, False, None),
        (True, True, False, None),
        (True, False, True, None),
        (None, False, False, "stale_conversations"),
        (None, False, False, "coremem"),
        (None, False, True, "usermem"),
    ],
)
async def test_owner_scoped_assignment_reaches_website_without_changing_control_digest(
    tmp_path, policy_enabled, pending, other_team, memory_kind
):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    store = ScheduleStore(storage_settings=storage)
    access = access_store_at(tmp_path)
    access.record_person(TeamPersonV1(team_id=TEAM, person_id=OWNER, person_ref=OWNER_REF,
                                      standing=PersonStanding.ACTIVE, roles=("member",),
                                      changed_at_utc=datetime.now(timezone.utc)))
    owner, team = OWNER, TEAM
    if other_team:
        team = "other_stored_team"
    for schedule_id, user in (("job", owner), ("private_job", "another_owner")):
        store.add(
            ScheduledMessage(
                schedule_id=schedule_id,
                management_class=ScheduleManagementClass.SYSTEM if memory_kind else ScheduleManagementClass.USER,
                schedule_name=f"memory_review:{memory_kind}:instance" if memory_kind else "ordinary",
                claim_team_id=team,
                claim_named_agent_type="TeamArchitect" if memory_kind else "Worker",
                claim_agent_id="specific_worker" if pending else None,
                next_fire_at=(utc_now() - timedelta(minutes=1)).isoformat(),
                interval_ms=60_000,
                payload=Task(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Do scheduled work",
                ),
                payload_meta=MessageMeta(
                    gateway_context=GatewayContext(
                        canonical_user_id=user, session_key="session_owner"
                    )
                ),
            )
        )
    schedule = store.get_scheduled_message("job")
    if pending:
        assert store.claim_next_due(
            claimer_agent_id="specific_worker",
            claimer_named_agent_type="Worker",
            claimer_team_id=team,
        )
    if policy_enabled is not None:
        target = "Replacement" if pending else "Worker"
        binding = ScheduleWorkerBindingV1(
            schedule_id="job",
            team_id=team,
            owner_canonical_user_id=owner,
            target_agent_type=target,
            definition_digest="a" * 64,
            execution_digest="b" * 64,
            revision=1,
            policy=ScheduleWorkerPolicySpecV1(enabled=policy_enabled),
            actor_principal_id="lead_1",
            authority_mode="agreement",
        )
        assert (
            store.worker_records.configure(
                binding,
                schedule.model_copy(
                    update={"claim_named_agent_type": target, "claim_agent_id": None}
                ),
                expected_revision=0,
                expected_control_digest=schedule_control_state_digest(schedule),
            )
            is pending
        )
    operations = ScheduleOperations(store, PeopleDirectory(access, TEAM), None, lambda person: False,
                                    lambda: datetime.now(timezone.utc), logging.getLogger("test.presentation"))
    body = {"request_id": "cmd-list", "offset": 0, "limit": 50}
    reply = checked_reply("schedules.list", operations.answer(
        stand_in_frame(TEAM, "schedules.list", body, person_ref=OWNER_REF, request_id="cmd-list")))

    # The team address never lists another team's schedules, even for the same person.
    if other_team:
        assert reply == {"outcome": "ok", "schedules": [], "total": 0}
        assert schedule_listing_digest(store.get_scheduled_message("job")) == schedule_listing_digest(schedule)
        return
    # Only the person's own schedule is listed, with its worker assignment.
    assert [row["schedule_id"] for row in reply["schedules"]] == ["job"]
    response = GatewayScheduleListing(success=True, schedules=tuple(
        GatewayScheduleView.from_wire(row) for row in reply["schedules"]))
    assignment = SimpleNamespace(**{
        "agent_id": None, "review_agent_type": None, "desired_agent_type": None, "reassignment_pending": False,
        **reply["schedules"][0]["worker"],
    })
    assert assignment.agent_type == ("TeamArchitect" if memory_kind else "Worker")
    assert assignment.review_agent_type == ("MemReviewAgent" if memory_kind else None)
    assert assignment.agent_id == ("specific_worker" if pending else None)
    assert assignment.automatic_creation == (
        "not_configured"
        if policy_enabled is None
        else "enabled"
        if policy_enabled
        else "disabled"
    )
    assert assignment.reassignment_pending is pending
    assert assignment.desired_agent_type == (
        "Replacement" if pending else "Worker" if policy_enabled is not None else None
    )

    schedules = SimpleNamespace(
        list_with_worker_assignments=AsyncMock(return_value=response),
        # A member who may not change schedules the runtime manages.
        may_change_system_schedules=lambda route: False,
    )
    website = WebsiteOperationsApplication(
        team=None,
        mcp=None,
        event_output=None,
        operational_controls=None,
        schedules=schedules,
        memory_operations=None,
        workspace_browser=None,
        team_commands=None,
    )
    result = await website.list_web_chat_schedules(
        GatewayRouteContextV1(session_key="session_owner", canonical_user_id=owner)
    )
    projected = result.schedules[0]
    assert projected.worker_assignment.model_dump(exclude={"schema_version"}) == {
        "agent_type": None, "agent_id": None, "review_agent_type": None, "upgrade_required": False,
        "desired_agent_type": None, "reassignment_pending": False, "not_running_since": None,
        "not_running_explanation": None, **reply["schedules"][0]["worker"],
    }
    assert projected.record_digest == schedule_listing_digest(store.get_scheduled_message("job"))
    assert schedules.list_with_worker_assignments.call_args.kwargs["owner_wide"] is True
    # Memory review timers are managed by the runtime: no controls for this person.
    assert bool(projected.available_actions) is (memory_kind is None)


@pytest.mark.asyncio
async def test_website_does_not_present_failed_assignment_read_as_empty_inventory():
    response = GatewayScheduleListing(success=False, message="The schedules could not be listed.")
    website = WebsiteOperationsApplication(
        team=None,
        mcp=None,
        event_output=None,
        operational_controls=None,
        schedules=SimpleNamespace(
            list_with_worker_assignments=AsyncMock(return_value=response)
        ),
        memory_operations=None,
        workspace_browser=None,
        team_commands=None,
    )
    with pytest.raises(RuntimeError, match="could not be listed"):
        await website.list_web_chat_schedules(
            GatewayRouteContextV1(session_key="session")
        )
