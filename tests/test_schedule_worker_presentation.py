"""Real saved assignment state crosses the manager and website boundaries."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_services import WebsiteOperationsApplication
from custom_agents.manager_control_runtime import (
    handle_manager_schedule_command,
)
from custom_agents.schedule_management import schedule_control_state_digest
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    schedule_listing_digest,
    ManagerScheduleCommand,
    ScheduledMessage,
    ScheduleWorkerAssignmentV1,
)
from custom_messages.contracts.schedule_worker_v1 import (
    ScheduleWorkerBindingV1,
    ScheduleWorkerPolicySpecV1,
    utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass
from custom_messages.transport_codec import decode_envelope, encode_envelope
from tests.test_manager_control_runtime import (  # noqa: F401
    _bootstrap_agent,
    _CaptureComms,
    _config,
    _gateway_signing_keys,
    _signed,
)


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
    config = _config(tmp_path)
    agent = _bootstrap_agent(config)
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    owner, team = config.user.canonical_user_id, config.gateway.team_id
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
            actor_principal_id=agent.id,
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
    comms = _CaptureComms()
    agent.team_comms = comms
    agent._ensure_schedule_store = lambda: store
    command = ManagerScheduleCommand(
        action="list",
        worker_assignments={
            "forged": ScheduleWorkerAssignmentV1(
                agent_type="Injected", agent_id=None, automatic_creation="enabled"
            )
        },
    )
    await handle_manager_schedule_command(
        agent,
        command=command,
        msg_header=MessageHeader(
            from_agent_id="gateway", response_routing_key="gateway"
        ),
        msg_meta=MessageMeta(
            gateway_context=_signed(
                GatewayContext(canonical_user_id=owner, session_key="session_owner"),
                team_id=config.gateway.team_id,
                roles=["admin"],
            )
        ),
    )
    # Exercise the actual typed transport encoding, not just a Python object.
    response = decode_envelope(encode_envelope(comms.messages[-1])).msg_content
    assert isinstance(response, ManagerScheduleCommand)
    assert response.success
    assert set(response.worker_assignments) == {"job"}
    assert [item.schedule_id for item in response.schedules] == ["job"]
    assignment = response.worker_assignments["job"]
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
    )
    result = await website.list_web_chat_schedules(
        GatewayRouteContextV1(session_key="session_owner", canonical_user_id=owner)
    )
    projected = result.schedules[0]
    assert (
        projected.worker_assignment.model_dump(exclude={"schema_version"})
        == assignment.model_dump()
    )
    assert projected.record_digest == schedule_listing_digest(store.get_scheduled_message("job"))
    assert schedules.list_with_worker_assignments.call_args.kwargs["owner_wide"] is True
    # Memory review timers are managed by the runtime: no controls for this person.
    assert bool(projected.available_actions) is (memory_kind is None)


@pytest.mark.asyncio
async def test_website_does_not_present_failed_assignment_read_as_empty_inventory():
    response = ManagerScheduleCommand(
        action="list",
        success=False,
        message="manager_required",
    )
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
    )
    with pytest.raises(RuntimeError, match="manager_required"):
        await website.list_web_chat_schedules(
            GatewayRouteContextV1(session_key="session")
        )
