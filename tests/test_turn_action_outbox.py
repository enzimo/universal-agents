import asyncio
import itertools
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant_gateway.config import _migrate_instance_config_dict
from config_loader import _migrate_config_dict
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    ModelAgentTurnDecision,
    ModelDelegateTaskAction,
    ModelTaskRequestPayload,
    NoOpAction,
    SecureCredentialCollectionAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_agents.conversation_delivery_states import (
    DELIVERY_STATE_NOTIFICATION_QUEUED,
)
from custom_agents.provider_error_runtime import (
    PROVIDER_TIMEOUT_USER_MESSAGE,
    ProviderTimeoutError,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.pending_work_runtime import close_pending_work
from custom_tools.tool_stop import ToolStop
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    SecureCredentialFieldSpec,
    ToolSetupRequestV1,
    ToolSetupResultV1,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import (
    Task,
    TaskQuery,
)
from custom_messages.enums import (
    MessageMetaType,
    ProgressState,
    RequesterType,
    TaskQueryDecision,
    TaskQueryReason,
    RecoverablePauseTrigger,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_task_manager(tmp_path, *, owner_id: str = "agent_id_manager") -> TaskManager:
    manager = TaskManager(
        db_path=str(tmp_path / f"{owner_id}.db"),
        logger=logging.getLogger(f"TaskManager.{owner_id}"),
    )
    manager.set_owner_identity(
        owner_id,
        agent_name="manager",
        team_id="test_team",
        named_agent_type="TeamArchitect",
    )
    return manager


def _add_active_parent_task(
    manager: TaskManager,
    *,
    task_id: str = "task_parent",
    requester_type: RequesterType = RequesterType.USER_REQUESTER,
) -> Task:
    task = Task(
        task_id=task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Do the parent work.",
    )
    header = MessageHeader(
        from_agent_id="user_agent", response_routing_key="user_reply"
    )
    meta = MessageMeta(
        from_agent_name="user",
        requester_type=requester_type,
        is_response_requested=True,
        msg_correlation_id="corr_parent",
        conversation_id="conv_parent",
    )
    assert manager.add_task(task, header, meta, owner_person_id=None)
    assert manager.get_next_task_to_process() == task_id
    assert manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, _now())
    return task


def _make_agent(tmp_path, *, task_manager: TaskManager | None = None, **kwargs):
    from custom_agents.strands_agent import UniversalAgent

    task_manager = task_manager or _make_task_manager(tmp_path)
    kwargs.setdefault("id", task_manager.identity.owner_agent_id)
    with (
        patch("custom_agents.strands_agent.Agent"),
        patch(
            "custom_agents.strands_agent.UniversalAgent._configure_model",
            return_value=MagicMock(),
        ),
    ):
        agent = UniversalAgent(
            role="Manager",
            tools=[],
            team_id="test_team",
            named_agent_type="TeamArchitect",
            is_team_lead=True,
            task_manager=task_manager,
            **kwargs,
        )
    agent.named_agent_type_queue_map["WebResearchAgent"] = "test_team_WebResearchAgent"
    sequences = itertools.count(1)
    # Publishing returns the stream sequence of the stored message.
    agent.team_comms = SimpleNamespace(
        send_message=AsyncMock(side_effect=lambda **_kwargs: next(sequences)),
    )
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_team", ["test_team", "other_team", None])
async def test_setup_pages_for_delegated_work_route_to_the_configured_gateway(tmp_path, gateway_team):
    """set_up_tools sends its pages to the person's chat even when a worker needed the account."""

    from custom_agents.tool_setup_run_runtime import send_setup_request
    from custom_agents.message_routing import OutgoingRoutingError

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager, requester_type=RequesterType.AGENT_REQUESTER)
    info = manager.get_task_info(parent.task_id)
    info.header.from_agent_id = "background_worker"
    info.header.response_routing_key = "background_worker"
    info.meta.gateway_context = GatewayContext(session_key="session", canonical_user_id="owner")
    agent = _make_agent(tmp_path, task_manager=manager, instance_config_path="instance.yaml" if gateway_team else None)
    config = SimpleNamespace(manager=SimpleNamespace(team_id=gateway_team, response_queue_name="configured_gateway"))
    request = ToolSetupRequestV1(
        parent_task_id=parent.task_id, tool_id="http:portainer", kind="account_link", owner="team",
        reason="Read the Portainer stacks for the weekly report.",
    )
    from contextlib import nullcontext

    expected = nullcontext() if gateway_team == "test_team" else pytest.raises(
        OutgoingRoutingError, match="gateway response route"
    )
    with patch("assistant_gateway.config.GatewayInstanceConfig.load_for_process", return_value=config), expected:
        await send_setup_request(agent, request, info)
    if gateway_team != "test_team":
        agent.team_comms.send_message.assert_not_awaited()
        return
    agent.team_comms.send_message.assert_awaited_once()
    sent = agent.team_comms.send_message.call_args.kwargs
    assert sent["routing_key"] == "configured_gateway"
    message = sent["message"]
    assert message.msg_content == request
    assert message.msg_header.idempotency_key == request.setup_request_id
    assert message.msg_meta.gateway_context.session_key == "session"


def test_raw_messages_cannot_carry_tool_setup_requests() -> None:
    from pydantic import ValidationError

    from custom_agents.structured_llm_messages import StructuredLLMResponse

    # Setup requests and results are runtime-owned: only set_up_tools (trusted
    # code) sends a request, and only the gateway sends results.
    with pytest.raises(ValidationError):
        StructuredLLMResponse.model_validate(
            {"selected_message_type": "ToolSetupRequestV1", "tool_setup_request": {}}
        )


def test_secure_credential_collection_has_first_class_turn_action() -> None:
    request = SecureCredentialCollectionRequest(
        parent_task_id="task_parent",
        service_name="Home Assistant",
        signup_url="https://homeassistant.local",
        credential_key="home_assistant_login",
        display_name="Home Assistant Login",
        purpose="Login for approved Home Assistant automation.",
        fields=[
            SecureCredentialFieldSpec(
                name="username",
                kind="username",
                visibility="metadata_visible",
                value_source="user_input",
            ),
            SecureCredentialFieldSpec(
                name="service_password",
                kind="password",
                visibility="secret",
                value_source="user_input",
            ),
        ],
        user_approved=True,
    )

    decision = StructuredAgentTurnDecision(
        actions=[
            {
                "action_type": "secure_credential_collection_request",
                "secure_credential_collection_request": request.model_dump(mode="json"),
            }
        ]
    )

    assert isinstance(decision.actions[0], SecureCredentialCollectionAction)
    action = decision.actions[0]
    assert isinstance(action, SecureCredentialCollectionAction)
    assert (
        action.secure_credential_collection_request.credential_key
        == "home_assistant_login"
    )


def test_secure_credential_collection_blocks_later_same_turn_response(tmp_path, caplog) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)
    credential_request = SecureCredentialCollectionRequest(
        parent_task_id=parent.task_id,
        service_name="Example",
        credential_key="test_credential",
        purpose="Edit test credential.",
    )
    decision = StructuredAgentTurnDecision(
        actions=[
            SecureCredentialCollectionAction(
                secure_credential_collection_request=credential_request
            ),
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.RESPONSE,
                    answer="I can create an edit form for you.",
                )
            ),
        ]
    )

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=task_info.header,
            incoming_meta=task_info.meta,
        )
    )

    assert isinstance(result, SecureCredentialCollectionRequest)
    assert result.credential_key == "test_credential"
    agent.team_comms.send_message.assert_awaited_once()
    sent_message = agent.team_comms.send_message.await_args.kwargs["message"]
    assert isinstance(sent_message.msg_content, SecureCredentialCollectionRequest)
    assert sent_message.msg_header.to_routing_path == "user_reply"
    assert "requester_bound_request_route_repaired" not in caplog.text
    parent_info = manager.get_task_info(parent.task_id)
    assert parent_info is not None
    assert parent_info.state == ProgressState.WAITING
    records = sorted(
        manager.execution_state.turn_action_outbox.values(),
        key=lambda item: item.action_index,
    )
    assert [record.action_type for record in records] == [
        "secure_credential_collection_request",
        "task_response",
    ]
    assert records[0].status == "completed"
    assert records[1].status == "blocked"
    assert "Secure credential collection request dispatched" in (
        records[1].last_error or ""
    )
    batch = manager.get_turn_action_batch(records[0].batch_id)
    assert batch is not None
    assert batch.status == "waiting_external_result"


@pytest.mark.asyncio
async def test_secure_credential_collection_request_releases_execution_slot(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    task = Task(
        task_id="task_credential",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Collect a Home Assistant token.",
    )
    header = MessageHeader(
        from_agent_id="user_agent", response_routing_key="user_reply"
    )
    meta = MessageMeta(
        from_agent_name="user",
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        msg_correlation_id="corr_credential",
        conversation_id="conv_credential",
    )
    assert manager.add_task(task, header, meta, owner_person_id=None)
    agent = _make_agent(tmp_path, task_manager=manager)
    credential_request = SecureCredentialCollectionRequest(
        parent_task_id="task_credential",
        service_name="Home Assistant",
        credential_key="home_assistant_token",
        purpose="Home Assistant API token for approved workflows.",
    )
    object.__setattr__(
        agent, "process_llm_interaction", AsyncMock(return_value=credential_request)
    )

    started = await agent._check_and_process_next_task()

    assert started is True
    task_info = manager.get_task_info("task_credential")
    assert task_info is not None
    assert task_info.state == ProgressState.WAITING
    assert manager.current_task_id is None
    assert agent.is_executing is False
    assert manager.can_start_new_task() is True


@pytest.mark.asyncio
async def test_provider_timeout_returns_one_clean_terminal_error(tmp_path, caplog) -> None:
    manager = _make_task_manager(tmp_path)
    task = Task(
        task_id="task_provider_timeout",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Finish the request.",
    )
    header = MessageHeader(
        from_agent_id="user_agent", response_routing_key="user_reply"
    )
    meta = MessageMeta(
        from_agent_name="user",
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        msg_correlation_id="corr_provider_timeout",
        conversation_id="conv_provider_timeout",
    )
    assert manager.add_task(task, header, meta, owner_person_id=None)
    agent = _make_agent(tmp_path, task_manager=manager)
    object.__setattr__(
        agent,
        "process_llm_interaction",
        AsyncMock(side_effect=ProviderTimeoutError()),
    )

    started = await agent._check_and_process_next_task()

    assert started is True
    sent = agent.team_comms.send_message.await_args.kwargs["message"]
    assert isinstance(sent.msg_content, Task)
    assert sent.msg_content.task_state[-1][0] == ProgressState.FAILED
    assert sent.msg_content.errors == [PROVIDER_TIMEOUT_USER_MESSAGE]
    assert agent.is_executing is False
    assert manager.get_task_info(task.task_id) is None
    assert manager.can_start_new_task()
    timeout_logs = [record for record in caplog.records if "model_provider_timeout" in record.message]
    assert len(timeout_logs) == 1
    assert timeout_logs[0].levelno == logging.WARNING
    assert not any(record.exc_info for record in caplog.records)


def test_task_manager_marks_live_stale_dispatching_action_due_for_retry(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    _add_active_parent_task(manager)
    batch = manager.create_turn_action_batch(
        "task_parent",
        [
            {
                "action_type": "send_message",
                "action": {
                    "action_type": "send_message",
                    "message": {
                        "selected_message_type": "NoOpMessage",
                        "no_op_message": {"info_message": "noop"},
                    },
                },
                "recoverability": "automatic_retry",
            }
        ],
    )
    assert batch is not None
    action_id = f"{batch.batch_id}_action_0"
    record = manager.mark_turn_action_record(
        action_id, status="dispatching", phase="publish"
    )
    assert record is not None
    record.updated_at = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    ).isoformat()

    marked = manager.mark_stale_dispatching_turn_actions(stale_after_seconds=30.0)

    assert [item.action_id for item in marked] == [action_id]
    restored_record = manager.get_turn_action_record(action_id)
    assert restored_record is not None
    assert restored_record.status == "retry_scheduled"
    assert restored_record.next_retry_at


def test_delegation_outbox_publishes_to_the_type_queue_and_records_it_as_waiting(
    tmp_path,
) -> None:
    """A task for a worker type is published without probing or starting a
    worker: it waits in the type's shared queue until a free worker takes it,
    and its record keeps the stream sequence so a stop can withdraw it."""
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.team_comms.send_message = AsyncMock(return_value=42)
    agent._probe_live_named_agent_type = AsyncMock()

    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=manager.get_task_info(parent.task_id).header,
            incoming_meta=manager.get_task_info(parent.task_id).meta,
        )
    )

    assert isinstance(result, Task)
    assert result.parent_task_id == parent.task_id
    agent.team_comms.send_message.assert_awaited_once()
    agent._probe_live_named_agent_type.assert_not_awaited()
    from custom_agents.delegated_work_runtime import delegated_tasks

    record = delegated_tasks(agent).get(str(agent.team_id), result.task_id)
    assert (record.status, record.publish_sequence, record.named_agent_type) == (
        "queued",
        42,
        "WebResearchAgent",
    )
    parent_info = manager.get_task_info(parent.task_id)
    assert parent_info is not None
    assert parent_info.state == ProgressState.WAITING
    records = list(manager.execution_state.turn_action_outbox.values())
    assert len(records) == 1
    assert records[0].status == "completed"
    assert records[0].phase == "complete"


def _delegate(agent, manager, parent, *, named_type, agent_name=None):
    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type=named_type,
                target_agent_name=agent_name,
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )
    return asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=manager.get_task_info(parent.task_id).header,
            incoming_meta=manager.get_task_info(parent.task_id).meta,
        )
    )


def test_a_named_worker_gets_the_task_in_its_own_mailbox(tmp_path) -> None:
    """With a type and a worker name, the task goes to that worker's mailbox
    (the id it answers the live probe with), not the type's shared queue.
    Before 2026-10-05 the type won and any free worker could take it."""
    from custom_agents.delegated_work_runtime import delegated_tasks

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    worker = AgentStatus(
        team_id="test_team",
        named_agent_type="WebResearchAgent",
        self_id="worker_7",
        is_paused=False,
        is_executing=True,
        has_execution_capacity=False,
        name="research-7",
        agent_role="research",
        status_type=MessageMetaType.RESPONSE,
    )
    probe = AsyncMock(return_value=worker)
    object.__setattr__(agent, "_probe_live_named_agent_type", probe)

    result = _delegate(agent, manager, parent, named_type="WebResearchAgent", agent_name="research-7")

    probe.assert_awaited_once_with("WebResearchAgent", "research-7")
    sent = agent.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == "worker_7"
    assert sent["message"].msg_header.to_routing_path == "worker_7"
    record = delegated_tasks(agent).get(str(agent.team_id), result.task_id)
    assert (record.status, record.target_agent_name) == ("queued", "research-7")


def test_a_type_created_after_startup_is_delegated_to_at_once(tmp_path) -> None:
    """TeamArchitect may create a specialist type while the team runs; the
    catalog is read live, so the type is listed and its queue used at once."""
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    catalog = ["TeamArchitect", "WebResearchAgent"]
    object.__setattr__(agent, "_local_team_runtime", SimpleNamespace(agent_types=lambda: tuple(catalog)))
    assert "MarketScanAgent" not in agent._build_team_routing_prompt_context()

    catalog.append("MarketScanAgent")
    assert "- `MarketScanAgent`" in agent._build_team_routing_prompt_context()
    assert "- `TeamArchitect`" not in agent._build_team_routing_prompt_context()
    _delegate(agent, manager, parent, named_type="MarketScanAgent")

    sent = agent.team_comms.send_message.await_args.kwargs
    assert sent["routing_key"] == "test_team_MarketScanAgent"
    assert agent.named_agent_type_queue_map["MarketScanAgent"] == "test_team_MarketScanAgent"


def test_a_type_missing_from_the_catalog_is_refused(tmp_path) -> None:
    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    object.__setattr__(agent, "_local_team_runtime", SimpleNamespace(agent_types=lambda: ("TeamArchitect",)))
    with pytest.raises(TurnActionRecoveryScheduled):
        _delegate(agent, manager, parent, named_type="NoSuchAgent")
    agent.team_comms.send_message.assert_not_awaited()


def test_delegation_outbox_does_not_register_or_publish_to_unavailable_worker(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    # An exact agent has no shared queue: it must answer a live status probe.
    availability = AsyncMock(return_value=None)
    object.__setattr__(agent, "_probe_live_named_agent_type", availability)
    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research an unavailable target.",
                ),
                target_agent_name="worker_that_does_not_exist",
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )
    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    with pytest.raises(TurnActionRecoveryScheduled):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=manager.get_task_info(parent.task_id).header,
                incoming_meta=manager.get_task_info(parent.task_id).meta,
            )
        )

    availability.assert_awaited_once_with(
        "WebResearchAgent",
        "worker_that_does_not_exist",
    )
    agent.team_comms.send_message.assert_not_awaited()
    assert manager.get_subtasks(parent.task_id) == []
    aborts = [
        entry
        for entry in manager.get_task_history_entries(parent.task_id, limit=None)
        if entry.details.get("event") == "delegation_subtask_registration_aborted"
    ]
    assert len(aborts) == 1
    assert aborts[0].details["final_failure_phase"] == (
        "availability_verification_failed"
    )


@pytest.mark.parametrize(
    ("decision_text", "expected_action"),
    [
        ("2", "partial_after_user_guided_subtask_timeout"),
    ],
)
def test_user_guided_timeout_resolution_terminalizes_affected_child(
    tmp_path,
    decision_text,
    expected_action,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    child = Task(
        task_id="task_timed_out_child",
        parent_task_id=parent.task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Worker request that timed out.",
    )
    child_info = manager.add_subtask(
        parent.task_id,
        child,
        MessageHeader(to_routing_path="missing_worker"),
        MessageMeta(
            to_agent_name="missing_worker",
            to_named_agent_type="WebResearchAgent",
        ),
    )
    assert child_info is not None
    manager.update_main_task_state(
        parent.task_id,
        ProgressState.PAUSED,
        _now(),
    )
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id=parent.task_id,
        message="Choose how to recover.",
        query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
        recoverable_pause_trigger=(RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT),
        allowed_decisions=[
            TaskQueryDecision.RESUME,
            TaskQueryDecision.PARTIAL,
            TaskQueryDecision.CANCEL,
        ],
    )
    manager.add_task_query(parent.task_id, query)
    manager.create_recoverable_pause(
        parent.task_id,
        trigger=RecoverablePauseTrigger.DELEGATED_SUBTASK_TIMEOUT,
        friendly_reason="The worker timed out.",
        task_query_id=query.task_query_id,
        affected_subtask_id=child.task_id,
    )
    agent = _make_agent(tmp_path, task_manager=manager)
    object.__setattr__(
        agent,
        "process_llm_interaction",
        AsyncMock(return_value=None),
    )
    response = TaskQuery(
        task_query_id=query.task_query_id,
        query_type=MessageMetaType.RESPONSE,
        parent_task_id=parent.task_id,
        message=decision_text,
    )

    asyncio.run(
        agent._handle_task_query_message(
            task_query_msg=response,
            incoming_msg_header=MessageHeader(from_agent_id="gateway"),
            incoming_msg_meta=MessageMeta(),
        )
    )

    assert manager.get_task_info(child.task_id).state == ProgressState.FAILED
    assert manager.get_task_info(parent.task_id).state == ProgressState.IN_PROGRESS
    assert manager.get_pending_recoverable_pause(parent.task_id) is None
    actions = [
        entry.details.get("action")
        for entry in manager.get_task_history_entries(parent.task_id, limit=None)
    ]
    assert expected_action in actions


def test_delegation_outbox_defers_same_turn_terminal_response_until_child_returns(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)

    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Find good duck recipes.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            ),
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Find weeknight-friendly duck recipes.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            ),
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.RESPONSE,
                    answer="I delegated the recipe search; results are coming right up.",
                )
            ),
        ]
    )

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=task_info.header,
            incoming_meta=task_info.meta,
        )
    )

    assert isinstance(result, Task)
    assert result.task_id == parent.task_id
    assert result.task_type == MessageMetaType.REQUEST
    assert agent.team_comms.send_message.await_count == 2
    sent_messages = [
        call.kwargs["message"] for call in agent.team_comms.send_message.await_args_list
    ]
    assert all(isinstance(message.msg_content, Task) for message in sent_messages)
    assert [message.msg_content.parent_task_id for message in sent_messages] == [
        parent.task_id,
        parent.task_id,
    ]
    parent_info = manager.get_task_info(parent.task_id)
    assert parent_info is not None
    assert parent_info.state == ProgressState.WAITING
    assert manager.current_task_id is None
    assert manager.has_pending_subtasks(parent.task_id)
    records = sorted(
        manager.execution_state.turn_action_outbox.values(),
        key=lambda item: item.action_index,
    )
    assert [record.action_type for record in records] == [
        "delegate_task",
        "delegate_task",
        "task_response",
    ]
    assert [record.status for record in records] == [
        "completed",
        "completed",
        "completed",
    ]
    history_events = [
        entry
        for entry in parent_info.task.execution_history
        if entry.details.get("event") == "terminal_task_response_deferred"
    ]
    assert len(history_events) == 1
    assert history_events[0].details["pending_subtask_ids"] == [
        message.msg_content.task_id for message in sent_messages
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "*",
        "Mark replied! He said: *",
        "He replied! Here's what Mark said:\n\n> **",
    ],
)
def test_terminal_task_response_rejects_malformed_markdown_answer(
    tmp_path, answer: str
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)

    decision = StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.RESPONSE,
                    answer=answer,
                )
            )
        ]
    )

    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    with pytest.raises(TurnActionRecoveryScheduled, match="semantic validation"):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=task_info.header,
                incoming_meta=task_info.meta,
            )
        )

    agent.team_comms.send_message.assert_not_awaited()
    records = sorted(
        manager.execution_state.turn_action_outbox.values(),
        key=lambda item: item.action_index,
    )
    assert len(records) == 1
    assert records[0].status == "failed_recoverable"
    assert records[0].recoverability == "model_recovery"
    assert "Terminal Task response answer failed semantic validation" in (
        records[0].last_error or ""
    )
    assert manager.get_task_info(parent.task_id).state == ProgressState.PARTIAL


def test_suppressed_timer_task_response_completes_without_dispatch_and_marks_delivery(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(
        manager,
        task_id="task_timer",
        requester_type=RequesterType.TIMER_REQUESTER,
    )
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None

    conversation_store = ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    conversation_store.ensure_thread(
        conversation_id=task_info.meta.conversation_id,
        team_id="test_team",
        owner_agent_id="agent_id_manager",
        owner_agent_name="manager",
        requester_name="user",
        requester_id="user_agent",
        root_task_id=parent.task_id,
        subject_preview="Timer request",
    )
    conversation_store.append_turn(
        conversation_id=task_info.meta.conversation_id,
        task_id=parent.task_id,
        followup_to_task_id=None,
        owner_agent_id="agent_id_manager",
        requester_name="user",
        requester_id="user_agent",
        correlation_id=task_info.meta.msg_correlation_id,
        problem_text=parent.problem_statement or "",
        requester_type=RequesterType.TIMER_REQUESTER.value,
        delivery_state="pending_gateway",
        task_status="accepted",
    )

    agent = _make_agent(tmp_path, task_manager=manager)
    agent._conversation_store = conversation_store
    internal_answer = (
        "Checked scheduled state; no change found, so no user message is required."
    )
    decision = ModelAgentTurnDecision.model_validate(
        {
            "actions": [
                {
                    "action_type": "task_response",
                    "task": {"answer": internal_answer},
                    "suppress_outbound_dispatch": True,
                }
            ]
        }
    ).to_runtime_decision()

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=task_info.header,
            incoming_meta=task_info.meta,
        )
    )

    assert isinstance(result, Task)
    assert result.answer == internal_answer
    agent.team_comms.send_message.assert_not_awaited()
    assert manager.get_task_info(parent.task_id) is None
    archived = manager.list_archived_tasks(limit=5)
    archived_task = next(item for item in archived if item["task_id"] == parent.task_id)
    assert archived_task["status"] == ProgressState.COMPLETED.value

    turn = conversation_store.get_turn_by_task_id(parent.task_id)
    assert turn is not None
    assert turn["delivery_state"] == "suppressed"
    assert turn["delivery_via"] == "runtime_noop"
    assert turn["final_answer_text"] == internal_answer
    assert "without user-visible delivery" in turn["execution_history_text"]
    assert (
        conversation_store.get_recent_turns(
            task_info.meta.conversation_id,
            include_pending_delivery=False,
        )
        == []
    )


def test_timer_response_after_notify_user_is_auto_suppressed(tmp_path) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(
        manager,
        task_id="task_timer_notify",
        requester_type=RequesterType.TIMER_REQUESTER,
    )
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    manager.append_history_entry(
        parent.task_id,
        "User-visible notification sent through notify_user.",
        details={"event": "notify_user_sent", "notification_id": "notif_1"},
    )
    agent = _make_agent(tmp_path, task_manager=manager)
    conversation_store = ConversationStore(db_path=str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    conversation_store.ensure_thread(
        conversation_id=task_info.meta.conversation_id,
        team_id="test_team",
        owner_agent_id="agent_id_manager",
        owner_agent_name="manager",
        requester_name="user",
        requester_id="user_agent",
        root_task_id=parent.task_id,
        subject_preview="Timer request",
    )
    conversation_store.append_turn(
        conversation_id=task_info.meta.conversation_id,
        task_id=parent.task_id,
        followup_to_task_id=None,
        owner_agent_id="agent_id_manager",
        requester_name="user",
        requester_id="user_agent",
        correlation_id=task_info.meta.msg_correlation_id,
        problem_text=parent.problem_statement or "",
        requester_type=RequesterType.TIMER_REQUESTER.value,
        delivery_state="pending_gateway",
        task_status="accepted",
    )
    agent._conversation_store = conversation_store

    decision = StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.RESPONSE,
                    answer="Kickoff notification delivered to Discord.",
                ),
                suppress_outbound_dispatch=False,
            )
        ]
    )

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=task_info.header,
            incoming_meta=task_info.meta,
        )
    )

    assert isinstance(result, Task)
    assert result.answer == "Kickoff notification delivered to Discord."
    agent.team_comms.send_message.assert_not_awaited()
    assert manager.get_task_info(parent.task_id) is None
    archived = manager.list_archived_tasks(limit=5)
    archived_task = next(item for item in archived if item["task_id"] == parent.task_id)
    assert archived_task["status"] == ProgressState.COMPLETED.value
    archived_record = manager.task_archive_repository.get_archived_task(
        parent.task_id,
        owner_agent_id=None,
        include_all_agents=True,
    )
    assert archived_record is not None
    history = archived_record.task_json
    assert "notify_user_already_sent" in history
    assert "task_response_task_id_corrected" not in agent._runtime_repair_anomaly_counts
    turn = conversation_store.get_turn_by_task_id(parent.task_id)
    assert turn is not None
    assert turn["delivery_state"] == DELIVERY_STATE_NOTIFICATION_QUEUED
    assert turn["delivery_via"] == "runtime_notify_user"
    visible_turns = conversation_store.get_recent_turns(
        task_info.meta.conversation_id,
        include_pending_delivery=False,
    )
    assert [item["task_id"] for item in visible_turns] == [parent.task_id]


def test_model_facing_task_response_preserves_gateway_metadata_and_route(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    task_info.meta.gateway_context = GatewayContext(
        gateway_instance_name="gateway_test",
        session_key="web_chat:default:browser_1",
        channel_name="web_chat",
        user_id="browser_1",
        chat_id="default",
        thread_id="default",
        delivery_mode="interactive",
        original_message_id="browser_msg_1",
    )
    agent = _make_agent(
        tmp_path,
        task_manager=manager,
        debug={"reply_routing_trace": {"enabled": True}},
    )

    model_decision = ModelAgentTurnDecision.model_validate(
        {
            "actions": [
                {
                    "action_type": "task_response",
                    "answer": "Credential status checked.",
                }
            ]
        }
    )
    runtime_decision = agent._turn_decision_from_structured_output(model_decision)
    assert runtime_decision is not None

    result = asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=runtime_decision,
            incoming_content=parent,
            incoming_header=task_info.header,
            incoming_meta=task_info.meta,
        )
    )

    assert isinstance(result, Task)
    assert result.task_id == parent.task_id
    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert sent_message.msg_header.to_routing_path == "user_reply"
    assert sent_message.msg_meta.msg_correlation_id == "corr_parent"
    assert sent_message.msg_meta.conversation_id == "conv_parent"
    assert sent_message.msg_meta.gateway_context is not None
    assert (
        sent_message.msg_meta.gateway_context.session_key
        == "web_chat:default:browser_1"
    )
    assert sent_message.msg_meta.gateway_context.channel_name == "web_chat"
    assert sent_message.msg_meta.gateway_context.chat_id == "default"


def test_failed_credential_deletion_result_sends_terminal_response_without_retry(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)
    llm_mock = AsyncMock(side_effect=AssertionError("LLM should not retry deletion"))
    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_failed_delete",
        status="credential_unavailable",
        credential_key="test_credential",
        service_name="Example Service",
        parent_task_id=parent.task_id,
        metadata={"flow": "credential_deletion"},
        message="Secure credential collection finished with status credential_unavailable.",
        error="sealbox-cli credential delete exited with code 2",
    )

    with patch.object(type(agent), "process_llm_interaction", llm_mock):
        asyncio.run(
            agent._handle_secure_credential_collection_result(
                result,
                incoming_msg_header=MessageHeader(from_agent_id="gateway"),
                incoming_msg_meta=MessageMeta(
                    msg_correlation_id="corr_parent", conversation_id="conv_parent"
                ),
            )
        )

    llm_mock.assert_not_awaited()
    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert isinstance(sent_message.msg_content, Task)
    assert sent_message.msg_content.task_type == MessageMetaType.RESPONSE
    assert sent_message.msg_content.task_id == parent.task_id
    assert (
        sent_message.msg_content.answer
        == "I couldn't delete your saved credentials for Example Service. Nothing was changed."
    )
    assert sent_message.msg_content.errors == [
        "sealbox-cli credential delete exited with code 2"
    ]
    assert "test_credential" not in sent_message.msg_content.answer
    assert manager.get_task_info(parent.task_id) is None


def test_successful_credential_collection_result_sends_terminal_response_without_retry(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    task_info.meta.gateway_context = GatewayContext(
        gateway_instance_name="test_gateway",
        session_key="website:web-chat:credential-result",
        channel_name="website",
        user_id="web_user_1",
        canonical_user_id="canonical_test_user",
        chat_id="web-chat",
        delivery_mode="interactive",
    )
    agent = _make_agent(tmp_path, task_manager=manager)
    llm_mock = AsyncMock(
        side_effect=AssertionError("LLM should not request another credential form")
    )
    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_stored",
        status="credential_stored",
        credential_key="some_test_token",
        service_name="GitHub",
        parent_task_id=parent.task_id,
        metadata={"flow": "credential_collection"},
        message="Secure credential collection finished with status credential_stored.",
    )

    with patch.object(type(agent), "process_llm_interaction", llm_mock):
        asyncio.run(
            agent._handle_secure_credential_collection_result(
                result,
                incoming_msg_header=MessageHeader(from_agent_id="gateway"),
                incoming_msg_meta=MessageMeta(
                    msg_correlation_id="corr_parent", conversation_id="conv_parent"
                ),
            )
        )

    llm_mock.assert_not_awaited()
    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert sent_message.msg_content.task_type == MessageMetaType.RESPONSE
    assert sent_message.msg_content.task_id == parent.task_id
    assert sent_message.msg_meta.requester_type == RequesterType.USER_REQUESTER
    assert sent_message.msg_meta.gateway_context is not None
    assert sent_message.msg_meta.gateway_context.delivery_mode == "interactive"
    assert (
        sent_message.msg_content.answer
        == "Your credentials for GitHub were stored successfully."
    )
    assert "some_test_token" not in sent_message.msg_content.answer
    assert "broker" not in sent_message.msg_content.answer.lower()
    assert manager.get_task_info(parent.task_id) is None


@pytest.mark.parametrize(
    ("failure_error", "expected_answer"),
    [
        (
            "sealbox store failed",
            "I couldn't store your credentials for GitHub. Please try again.",
        ),
        (
            "Credential collection session expired.",
            "I couldn't store your credentials for GitHub because the secure form "
            "expired before it was submitted. Please start the request again.",
        ),
    ],
)
def test_failed_credential_collection_result_sends_terminal_response_without_retry(
    tmp_path,
    failure_error: str,
    expected_answer: str,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)
    llm_mock = AsyncMock(
        side_effect=AssertionError("LLM should not request another credential form")
    )
    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_failed_collection",
        status="credential_unavailable",
        credential_key="some_test_token",
        service_name="GitHub",
        parent_task_id=parent.task_id,
        metadata={"flow": "credential_collection"},
        message="Secure credential collection finished with status credential_unavailable.",
        error=failure_error,
    )

    with patch.object(type(agent), "process_llm_interaction", llm_mock):
        asyncio.run(
            agent._handle_secure_credential_collection_result(
                result,
                incoming_msg_header=MessageHeader(from_agent_id="gateway"),
                incoming_msg_meta=MessageMeta(
                    msg_correlation_id="corr_parent", conversation_id="conv_parent"
                ),
            )
        )

    llm_mock.assert_not_awaited()
    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert sent_message.msg_content.task_type == MessageMetaType.RESPONSE
    assert sent_message.msg_content.task_id == parent.task_id
    assert sent_message.msg_content.answer == expected_answer
    assert sent_message.msg_content.errors == [failure_error]
    assert "credential_unavailable" not in sent_message.msg_content.answer
    assert "some_test_token" not in sent_message.msg_content.answer
    assert manager.get_task_info(parent.task_id) is None


def test_orphaned_credential_collection_result_closes_gateway_request(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    agent = _make_agent(tmp_path, task_manager=manager)
    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_orphaned",
        status="credential_unavailable",
        credential_key="some_test_token",
        service_name="GitHub",
        parent_task_id="task_already_archived",
        metadata={"flow": "credential_collection"},
        error="Credential collection session expired.",
    )
    incoming_meta = MessageMeta(
        msg_correlation_id="corr_orphaned_credential_result",
        conversation_id="conv_parent",
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        gateway_context=GatewayContext(
            gateway_instance_name="test_gateway",
            session_key="website:web-chat:credential-result",
            channel_name="website",
            user_id="web_user_1",
            canonical_user_id="canonical_test_user",
            chat_id="web-chat",
            delivery_mode="interactive",
        ),
    )

    asyncio.run(
        agent._handle_secure_credential_collection_result(
            result,
            incoming_msg_header=MessageHeader(
                from_agent_id="gateway",
                response_routing_key="user_reply",
            ),
            incoming_msg_meta=incoming_meta,
        )
    )

    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert isinstance(sent_message.msg_content, Task)
    assert sent_message.msg_content.task_type == MessageMetaType.RESPONSE
    assert sent_message.msg_content.task_id == "task_already_archived"
    assert (
        sent_message.msg_content.answer
        == "I couldn't store your credentials for GitHub because the secure form "
        "expired before it was submitted. Please start the request again."
    )
    assert sent_message.msg_meta.msg_correlation_id == (
        "corr_orphaned_credential_result"
    )
    assert sent_message.msg_meta.gateway_context == incoming_meta.gateway_context


def test_orphaned_tool_setup_result_closes_gateway_request(tmp_path) -> None:
    manager = _make_task_manager(tmp_path)
    agent = _make_agent(tmp_path, task_manager=manager)
    result = ToolSetupResultV1(
        setup_request_id="tool_setup_req_orphaned",
        status="declined",
        parent_task_id="task_already_archived",
        tool_id="http:portainer",
        kind="account_link",
        owner="person",
        message="The form for the account link for Portainer was closed without linking anything.",
    )

    asyncio.run(
        agent._handle_tool_setup_result(
            result,
            incoming_msg_header=MessageHeader(
                from_agent_id="gateway",
                response_routing_key="user_reply",
            ),
            incoming_msg_meta=MessageMeta(
                msg_correlation_id="corr_orphaned_link_result",
                conversation_id="conv_parent",
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
            ),
        )
    )

    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert isinstance(sent_message.msg_content, Task)
    assert sent_message.msg_content.task_type == MessageMetaType.RESPONSE
    assert sent_message.msg_content.task_id == "task_already_archived"
    assert (
        sent_message.msg_content.answer
        == "The account link for http:portainer was not finished. Nothing was changed."
    )
    assert sent_message.msg_meta.msg_correlation_id == "corr_orphaned_link_result"


def test_successful_credential_deletion_result_sends_terminal_response_without_retry(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)
    llm_mock = AsyncMock(
        side_effect=AssertionError("LLM should not summarize deletion")
    )
    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_deleted",
        status="credential_deleted",
        credential_key="test_credential",
        service_name="Example Service",
        parent_task_id=parent.task_id,
        metadata={"flow": "credential_deletion"},
        message="Credential test_credential was deleted.",
    )

    with patch.object(type(agent), "process_llm_interaction", llm_mock):
        asyncio.run(
            agent._handle_secure_credential_collection_result(
                result,
                incoming_msg_header=MessageHeader(from_agent_id="gateway"),
                incoming_msg_meta=MessageMeta(
                    msg_correlation_id="corr_parent", conversation_id="conv_parent"
                ),
            )
        )

    llm_mock.assert_not_awaited()
    send_kwargs = agent.team_comms.send_message.await_args.kwargs
    assert send_kwargs["routing_key"] == "user_reply"
    sent_message = send_kwargs["message"]
    assert sent_message.msg_content.task_type == MessageMetaType.RESPONSE
    assert sent_message.msg_content.task_id == parent.task_id
    assert (
        sent_message.msg_content.answer
        == "Your saved credentials for Example Service were deleted successfully."
    )
    assert "test_credential" not in sent_message.msg_content.answer
    assert manager.get_task_info(parent.task_id) is None


def test_manual_signup_result_resumes_with_friendly_output_directive(tmp_path) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    captured: dict[str, str] = {}

    async def fake_process_llm_interaction(**_kwargs):
        captured["directive"] = (
            manager.read_runtime_resume_directive(parent.task_id) or ""
        )
        return Task(
            task_id=parent.task_id,
            task_type=MessageMetaType.RESPONSE,
            answer="Your credentials for Ally Bank were stored successfully.",
        )

    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_manual_signup",
        status="manual_signup_collected",
        credential_key="ally_bank_login",
        service_name="Ally Bank",
        display_name="Ally Bank login",
        parent_task_id=parent.task_id,
        metadata={"flow": "manual_signup"},
    )

    with patch.object(
        type(agent),
        "process_llm_interaction",
        AsyncMock(side_effect=fake_process_llm_interaction),
    ) as llm_mock:
        asyncio.run(
            agent._handle_secure_credential_collection_result(
                result,
                incoming_msg_header=MessageHeader(from_agent_id="gateway"),
                incoming_msg_meta=MessageMeta(
                    msg_correlation_id="corr_parent",
                    conversation_id="conv_parent",
                ),
            )
        )

    llm_mock.assert_awaited_once()
    assert "Ally Bank" in captured["directive"]
    assert "short, natural language" in captured["directive"]
    assert "do not repeat a completed collection" in captured["directive"].lower()
    assert "ally_bank_login" not in captured["directive"]
    assert manager.get_task_info(parent.task_id) is None


def test_credential_collection_that_continues_the_task_resumes_through_the_model(tmp_path) -> None:
    # A form that is one step of a larger request (collect, then link, then use)
    # resumes the task instead of ending it with the canned answer.
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    assert manager.update_main_task_state(parent.task_id, ProgressState.WAITING, _now())
    agent = _make_agent(tmp_path, task_manager=manager)
    captured: dict[str, object] = {}

    async def fake_process_llm_interaction(**kwargs):
        captured["directive"] = manager.read_runtime_resume_directive(parent.task_id) or ""
        return Task(task_id=parent.task_id, task_type=MessageMetaType.RESPONSE, answer="Linked and posted.")

    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_continue",
        status="credential_stored",
        credential_key="team_x_oauth",
        service_name="X",
        parent_task_id=parent.task_id,
        metadata={"flow": "credential_collection"},
        continue_task=True,
    )

    with patch.object(
        type(agent), "process_llm_interaction", AsyncMock(side_effect=fake_process_llm_interaction)
    ) as llm_mock:
        asyncio.run(
            agent._handle_secure_credential_collection_result(
                result,
                incoming_msg_header=MessageHeader(from_agent_id="gateway"),
                incoming_msg_meta=MessageMeta(msg_correlation_id="corr_parent", conversation_id="conv_parent"),
            )
        )

    llm_mock.assert_awaited_once()
    assert "set_up_tools for the tool" in str(captured["directive"])
    assert "team_x_oauth" not in str(captured["directive"])
    assert manager.get_task_info(parent.task_id) is None


def test_worker_direct_delegation_to_specialist_is_rejected(tmp_path) -> None:
    manager = _make_task_manager(tmp_path)
    manager.set_owner_identity(
        "agent_id_worker",
        agent_name="worker",
        team_id="test_team",
        named_agent_type="InternalImprovementAgent",
    )
    parent = _add_active_parent_task(
        manager, requester_type=RequesterType.TIMER_REQUESTER
    )
    task_info = manager.get_task_info(parent.task_id)
    assert task_info is not None
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.id = "agent_id_worker"
    agent.name = "worker"
    agent.named_agent_type = "InternalImprovementAgent"
    agent.is_team_lead = False

    model_decision = ModelAgentTurnDecision(
        actions=[
            ModelDelegateTaskAction(
                task=ModelTaskRequestPayload(
                    problem_statement="Research this directly."
                ),
                target_named_agent_type="WebResearchAgent",
            )
        ]
    )
    runtime_decision = agent._turn_decision_from_structured_output(model_decision)
    assert runtime_decision is not None

    with pytest.raises(RuntimeError, match="manager_escalation task to TeamArchitect"):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=runtime_decision,
                incoming_content=parent,
                incoming_header=task_info.header,
                incoming_meta=task_info.meta,
            )
        )


def test_publish_timeout_schedules_automatic_retry_without_model_reentry(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(tmp_path, task_manager=manager)
    agent.team_comms.send_message = AsyncMock(
        side_effect=TimeoutError("broker publish timed out")
    )

    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )

    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    with pytest.raises(TurnActionRecoveryScheduled):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=manager.get_task_info(parent.task_id).header,
                incoming_meta=manager.get_task_info(parent.task_id).meta,
            )
        )

    record = next(iter(manager.execution_state.turn_action_outbox.values()))
    assert record.status == "retry_scheduled"
    assert record.phase == "publish"
    assert record.attempt_count == 1
    assert record.next_retry_at
    assert manager.get_task_info(parent.task_id).state == ProgressState.RETRYING
    agent._strands_agent.assert_not_called()


def test_a_failed_publish_wakes_the_retained_drain_at_its_retry_time(tmp_path) -> None:
    """The retry wake-up is a timer the agent owns, not a task shutdown cannot see."""
    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(
        tmp_path,
        task_manager=manager,
        turn_action_outbox={
            "max_auto_attempts": 3,
            "initial_retry_delay_seconds": 0.05,
            "max_retry_delay_seconds": 0.05,
            "retry_jitter_ratio": 0.0,
        },
    )
    agent.team_comms.send_message = AsyncMock(side_effect=TimeoutError("broker publish timed out"))
    object.__setattr__(agent, "_continue_pending_work", AsyncMock())
    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )

    async def scenario() -> None:
        with pytest.raises(TurnActionRecoveryScheduled):
            await agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=manager.get_task_info(parent.task_id).header,
                incoming_meta=manager.get_task_info(parent.task_id).meta,
            )
        assert len(agent._pending_work_timers) == 1
        await asyncio.sleep(0.2)
        await asyncio.wait_for(agent._pending_work_task, 5)
        agent._continue_pending_work.assert_awaited()
        assert agent._pending_work_timers == set()

    asyncio.run(scenario())


@pytest.mark.parametrize("publish_failures", [1, 2])
def test_publish_retry_reuses_delegated_subtask_side_effect(tmp_path, publish_failures) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(
        tmp_path,
        task_manager=manager,
        turn_action_outbox={
            "max_auto_attempts": 3,
            "initial_retry_delay_seconds": 0.0,
            "max_retry_delay_seconds": 0.0,
            "retry_jitter_ratio": 0.0,
        },
    )
    published_messages = []

    async def flaky_send_message(*, message, routing_key):
        published_messages.append((message, routing_key))
        if len(published_messages) <= publish_failures:
            raise TimeoutError(
                "broker publish timed out after local subtask registration"
            )

    agent.team_comms.send_message = AsyncMock(side_effect=flaky_send_message)

    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )

    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    with pytest.raises(TurnActionRecoveryScheduled):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=manager.get_task_info(parent.task_id).header,
                incoming_meta=manager.get_task_info(parent.task_id).meta,
            )
        )

    record = next(iter(manager.execution_state.turn_action_outbox.values()))
    delegated_task_id = record.side_effects["delegated_task_id"]
    assert [
        subtask.task.task_id for subtask in manager.get_subtasks(parent.task_id)
    ] == [delegated_task_id]
    manager.mark_turn_action_record(
        record.action_id,
        next_retry_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )

    retried = asyncio.run(agent._drain_due_turn_action_outbox())

    if publish_failures == 2:
        assert retried is True
        assert any(lease.status == "failed" for lease in manager.execution_state.runtime_leases.values())
        manager.mark_turn_action_record(
            record.action_id,
            next_retry_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
        retried = asyncio.run(agent._drain_due_turn_action_outbox())

    assert retried is True
    assert len(manager.get_subtasks(parent.task_id)) == 1
    assert [
        subtask.task.task_id for subtask in manager.get_subtasks(parent.task_id)
    ] == [delegated_task_id]
    assert len(published_messages) == publish_failures + 1
    retry_envelope, retry_route = published_messages[1]
    assert retry_route == "test_team_WebResearchAgent"
    assert retry_envelope.msg_content.task_id == delegated_task_id
    assert manager.get_turn_action_record(record.action_id).status == "completed"


def test_live_agent_probe_does_not_accept_stale_cached_status_when_operational_transport_is_unavailable(
    tmp_path,
) -> None:
    agent = _make_agent(tmp_path)
    stale = AgentStatus(
        team_id="test_team",
        named_agent_type="WebResearchAgent",
        self_id="stale_worker",
        is_paused=False,
        is_executing=False,
        has_execution_capacity=True,
        name="stale-worker",
        agent_role="research",
        status_type=MessageMetaType.RESPONSE,
        last_seen_at="2026-01-01T00:00:00+00:00",
    )
    agent.known_agents.agent_map[stale.self_id] = stale
    object.__setattr__(agent, "_operational_exchange_name", None)

    result = asyncio.run(agent._probe_live_named_agent_type("WebResearchAgent"))

    assert result is None


def test_live_agent_probe_timeout_does_not_fall_back_to_durable_status(
    tmp_path,
) -> None:
    agent = _make_agent(tmp_path)
    stale = AgentStatus(
        team_id="test_team",
        named_agent_type="WebResearchAgent",
        self_id="stale_worker",
        is_paused=False,
        is_executing=False,
        has_execution_capacity=True,
        name="stale-worker",
        agent_role="research",
        status_type=MessageMetaType.RESPONSE,
        last_seen_at="2026-01-01T00:00:00+00:00",
    )
    agent.known_agents.agent_map[stale.self_id] = stale
    object.__setattr__(agent, "_operational_exchange_name", "ua.test_team.operational")
    agent.subtask_status_probe_timeout_seconds = 0.01
    object.__setattr__(agent, "_send_operational_message", AsyncMock())

    result = asyncio.run(agent._probe_live_named_agent_type("WebResearchAgent"))

    assert result is None


def test_delegation_preserves_user_scope_and_sends_one_interactive_wait_notification(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    parent_info = manager.get_task_info(parent.task_id)
    parent_info.meta.objective_id = "objective_compare_cars"
    parent_info.meta.request_timezone = "America/Chicago"
    parent_info.meta.gateway_context = GatewayContext(
        gateway_instance_name="instance_test_user",
        session_key="session_compare",
        channel_name="telegram",
        canonical_user_id="usr_test_user",
        chat_id="100200300",
        delivery_mode="interactive",
        original_message_id="2110",
    )
    agent = _make_agent(tmp_path, task_manager=manager)
    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Compare the candidate vehicles.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ]
    )

    asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision,
            incoming_content=parent,
            incoming_header=parent_info.header,
            incoming_meta=parent_info.meta,
        )
    )

    sent = [
        call.kwargs["message"] for call in agent.team_comms.send_message.await_args_list
    ]
    delegated = next(
        envelope for envelope in sent if isinstance(envelope.msg_content, Task)
    )
    notification = next(
        envelope
        for envelope in sent
        if isinstance(envelope.msg_content, UserNotification)
    )
    assert delegated.msg_meta.gateway_context.canonical_user_id == "usr_test_user"
    assert delegated.msg_meta.gateway_context is not parent_info.meta.gateway_context
    assert delegated.msg_meta.objective_id == "objective_compare_cars"
    assert delegated.msg_meta.request_timezone == "America/Chicago"
    assert notification.msg_meta.gateway_context.canonical_user_id == "usr_test_user"
    assert notification.msg_header.to_routing_path == "user_reply"
    assert notification.msg_header.idempotency_key == f"notif_del_wait_{parent.task_id}"

    sent_again = asyncio.run(
        agent._notify_user_delegated_work_started(
            parent_task_id=parent.task_id,
            delegated_task_id=delegated.msg_content.task_id,
            target_named_agent_type="WebResearchAgent",
        )
    )
    assert sent_again is False
    assert len(agent.team_comms.send_message.await_args_list) == 2


def test_partial_failure_blocks_later_actions_and_builds_inline_recovery_brief(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    agent = _make_agent(
        tmp_path,
        task_manager=manager,
        turn_action_outbox={
            "max_auto_attempts": 0,
            "max_inline_recovery_attempts_per_batch": 2,
        },
    )
    agent.team_comms.send_message = AsyncMock(
        side_effect=RuntimeError("route disappeared")
    )

    decision = StructuredAgentTurnDecision(
        actions=[
            NoOpAction(reason="First action already done."),
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            ),
            NoOpAction(reason="Later action must wait."),
        ]
    )

    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    with pytest.raises(TurnActionRecoveryScheduled):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=manager.get_task_info(parent.task_id).header,
                incoming_meta=manager.get_task_info(parent.task_id).meta,
            )
        )

    records = sorted(
        manager.execution_state.turn_action_outbox.values(),
        key=lambda item: item.action_index,
    )
    assert [record.status for record in records] == [
        "completed",
        "failed_recoverable",
        "blocked",
    ]
    assert records[1].recoverability == "model_recovery"
    assert manager.get_task_info(parent.task_id).state == ProgressState.PARTIAL
    brief = manager.build_turn_action_recovery_brief(records[1].batch_id)
    assert "Runtime Action Recovery Brief" in brief
    assert "Completed actions:" in brief
    assert "Blocked actions not attempted:" in brief
    assert records[0].action_id in brief
    assert records[2].action_id in brief


def test_inline_recovery_cap_starts_isolated_recovery_when_plan_exists(
    tmp_path,
) -> None:
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    assert manager.set_task_plan(
        parent.task_id,
        "1. Delegate research. 2. Summarize result.",
        goal_summary="Recover a failed runtime action safely.",
        agent_name="manager",
    )
    agent = _make_agent(
        tmp_path,
        task_manager=manager,
        turn_action_outbox={
            "max_auto_attempts": 0,
            "max_inline_recovery_attempts_per_batch": 0,
        },
    )
    agent.team_comms.send_message = AsyncMock(
        side_effect=RuntimeError("route disappeared")
    )

    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Research flights.",
                ),
                target_named_agent_type="WebResearchAgent",
                requester_type=RequesterType.AGENT_REQUESTER,
            ),
            NoOpAction(reason="Later action must wait."),
        ]
    )

    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    with pytest.raises(TurnActionRecoveryScheduled):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision,
                incoming_content=parent,
                incoming_header=manager.get_task_info(parent.task_id).header,
                incoming_meta=manager.get_task_info(parent.task_id).meta,
            )
        )

    records = sorted(
        manager.execution_state.turn_action_outbox.values(),
        key=lambda item: item.action_index,
    )
    batch = manager.get_turn_action_batch(records[0].batch_id)
    assert batch is not None
    assert batch.status == "isolated_recovery_started"
    assert records[0].status == "failed_recoverable"
    assert records[0].recoverability == "isolated_recovery"
    assert records[1].status == "blocked"
    assert manager.get_task_info(parent.task_id).state == ProgressState.WAITING
    recovery_task_ids = [
        task_id
        for task_id in manager.task_state.tasks
        if task_id.startswith("recovery_task_")
    ]
    assert len(recovery_task_ids) == 1
    recovery_info = manager.get_task_info(recovery_task_ids[0])
    assert recovery_info is not None
    assert "Runtime Action Recovery Brief" in recovery_info.task.problem_statement


def test_runtime_action_outbox_config_loaders_reject_invalid_schema(tmp_path) -> None:
    with pytest.raises(ValueError, match="Invalid config.yaml schema_version"):
        _migrate_config_dict(
            {"schema_version": 0}, source_path=tmp_path / "config.yaml"
        )
    with pytest.raises(ValueError, match="Invalid instance_config schema_version"):
        _migrate_instance_config_dict(
            {"schema_version": 0, "agent": {}},
            source_path=tmp_path / "instance_config.yaml",
        )


def test_workflow_public_config_keeps_agent_type_overrides_sparse(tmp_path) -> None:
    generic, _ = _migrate_config_dict(
        {
            "schema_version": 1,
            "agent_types": {
                "WebResearchAgent": {
                    "agent": {
                        "max_tool_calls_per_task": 50,
                    }
                },
                "TeamArchitect": {
                    "agent": {
                        "turn_action_outbox": {"max_auto_attempts": 7},
                        "delegation_assurance": {"enabled": False},
                        "runtime_coordinator": {"mode": "observe"},
                    },
                    "llm": {
                        "failover": {
                            "enabled": True,
                            "fallbacks": [
                                {"provider": "litellm", "model_id": "fallback/model"}
                            ],
                        }
                    },
                },
            },
        },
        source_path=tmp_path / "config.yaml",
    )
    instance, _ = _migrate_instance_config_dict(
        {
            "schema_version": 1,
            "agent": {},
            "agent_types": {
                "WebResearchAgent": {
                    "agent": {
                        "max_tool_calls_per_task": 50,
                    }
                },
                "TeamArchitect": {
                    "agent": {
                        "turn_action_outbox": {"max_auto_attempts": 7},
                        "delegation_assurance": {"enabled": False},
                        "runtime_coordinator": {"mode": "observe"},
                    },
                    "llm": {
                        "failover": {
                            "enabled": True,
                            "fallbacks": [
                                {"provider": "litellm", "model_id": "fallback/model"}
                            ],
                        }
                    },
                },
            },
        },
        source_path=tmp_path / "instance_config.yaml",
    )

    for migrated in (generic, instance):
        web_agent = migrated["agent_types"]["WebResearchAgent"]["agent"]
        assert web_agent == {"max_tool_calls_per_task": 50}
        assert "llm" not in migrated["agent_types"]["WebResearchAgent"]

        manager_agent = migrated["agent_types"]["TeamArchitect"]["agent"]
        assert manager_agent["turn_action_outbox"]["max_auto_attempts"] == 7
        assert "initial_retry_delay_seconds" not in manager_agent["turn_action_outbox"]
        # Delegation assurance was removed (2026-10-04); the upgrade drops it.
        assert "delegation_assurance" not in manager_agent
        assert manager_agent["runtime_coordinator"]["mode"] == "observe"
        assert (
            "max_deferred_events_per_task" not in manager_agent["runtime_coordinator"]
        )
        manager_llm = migrated["agent_types"]["TeamArchitect"]["llm"]
        assert manager_llm["failover"]["enabled"] is True
        assert manager_llm["failover"]["fallbacks"] == [
            {"provider": "litellm", "model_id": "fallback/model"}
        ]
        assert "same_provider_retry" not in manager_llm["failover"]


# -- what a turn read is released when its decision is recorded (review 2026-10-18) --

ACCESS_NOTE = (
    "Your access request was decided while this task was paused. Continue the task using this decision; "
    "call the tool you needed again only if it was granted: {\"status\": \"granted\"}"
)


def _delegation(problem: str) -> DelegateTaskAction:
    return DelegateTaskAction(
        task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement=problem),
        target_named_agent_type="WebResearchAgent",
        requester_type=RequesterType.AGENT_REQUESTER,
    )


def _turn_on_the_real_outbox(agent, manager, model_call):
    """The real turn owner: it reads notes from ``manager`` and records its decision in the agent's outbox."""
    import dataclasses
    from collections import deque

    from tests.test_runtime_owner_boundaries import build_turn_owner

    executor, _ = build_turn_owner(
        invoke=model_call,
        guidance=deque(["", ""]),
        committed=[],
        cleared=[],
        task_operations={
            "read_runtime_resume_directive": manager.read_runtime_resume_directive,
            "read_partial_tool_call_limit": manager.read_partial_tool_call_limit,
            "release_turn_start_notes": manager.release_turn_start_notes,
            "get_task_info": manager.get_task_info,
            "count_recoverable_pauses": manager.count_recoverable_pauses,
        },
    )
    executor.effects = dataclasses.replace(
        executor.effects, execute_turn_decision_outbox=agent._execute_turn_decision_outbox
    )
    return executor


def _saved_note(tmp_path, task_id: str) -> str:
    reloaded = _make_task_manager(tmp_path)
    reloaded.restore_runtime_state()
    return reloaded.read_runtime_resume_directive(task_id) or ""


def test_an_outbox_retry_leaves_the_decided_turns_note_released(tmp_path) -> None:
    """Before, a later outbox action failing with an automatic retry kept the
    note, so a later turn read "call the tool again only if granted" again."""
    from custom_agents.strands_agent import TurnActionRecoveryScheduled

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    manager.append_runtime_resume_directive(parent.task_id, ACCESS_NOTE)
    manager.record_partial_tool_call_limit(parent.task_id, 3)
    agent = _make_agent(
        tmp_path,
        task_manager=manager,
        turn_action_outbox={
            "max_auto_attempts": 3,
            "initial_retry_delay_seconds": 60.0,
            "max_retry_delay_seconds": 60.0,
            "retry_jitter_ratio": 0.0,
        },
    )
    agent.team_comms.send_message = AsyncMock(side_effect=TimeoutError("broker publish timed out"))
    prompts: list[str] = []

    async def model_call(prompt, **_kwargs):
        prompts.append(prompt)
        return SimpleNamespace(structured_output=StructuredAgentTurnDecision(actions=[_delegation("Research flights.")]))

    executor = _turn_on_the_real_outbox(agent, manager, model_call)
    info = manager.get_task_info(parent.task_id)

    async def scenario() -> None:
        with pytest.raises(TurnActionRecoveryScheduled):
            await executor.process_llm_interaction(parent, info.header, info.meta)
        await close_pending_work(agent)  # the retry timer is not part of this test

    asyncio.run(scenario())
    assert ACCESS_NOTE in prompts[0]  # the turn read the note and decided
    assert ACCESS_NOTE not in (manager.read_runtime_resume_directive(parent.task_id) or "")
    assert manager.read_partial_tool_call_limit(parent.task_id) is None
    assert ACCESS_NOTE not in _saved_note(tmp_path, parent.task_id)  # in the batch's own save


def test_a_cancel_after_the_outbox_dispatched_leaves_the_note_released(tmp_path) -> None:
    """Before, SIGTERM after the outbox had sent a re-delegation kept the
    delegation-decision note, so the turn after the restart delegated again."""
    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    decision_note = (
        "Runtime instruction: a delegated task failed. Inspect it, then decide: keep waiting, "
        "delegate a task again differently, or answer with what you have."
    )
    manager.append_runtime_resume_directive(parent.task_id, decision_note)
    agent = _make_agent(tmp_path, task_manager=manager)
    sent: list[str] = []

    async def scenario() -> None:
        second_sending = asyncio.Event()

        async def publish(*, message, routing_key):
            sent.append(routing_key)
            if len(sent) == 1:
                return 1  # the re-delegation went out
            second_sending.set()
            await asyncio.Event().wait()  # the next action is still sending when SIGTERM arrives

        agent.team_comms.send_message = AsyncMock(side_effect=publish)

        async def model_call(_prompt, **_kwargs):
            return SimpleNamespace(
                structured_output=StructuredAgentTurnDecision(
                    actions=[_delegation("Research it again, differently."), _delegation("Check the supplier list.")]
                )
            )

        executor = _turn_on_the_real_outbox(agent, manager, model_call)
        info = manager.get_task_info(parent.task_id)
        turn = asyncio.create_task(executor.process_llm_interaction(parent, info.header, info.meta))
        await asyncio.wait_for(second_sending.wait(), 5)
        turn.cancel()  # shutdown cancels the turn mid-outbox
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(scenario())
    assert len(sent) == 2
    assert decision_note not in _saved_note(tmp_path, parent.task_id)


def test_an_approval_pause_keeps_the_note_until_the_resumed_turns_decision(tmp_path) -> None:
    """A paused turn is mid-work: its note stays saved (a restart that loses
    the live session gives the fresh turn the note again), and the resumed
    turn's recorded decision releases it."""
    import dataclasses

    from custom_agents import task_steering_runtime
    from custom_messages.contracts.task_v1 import TaskSteering
    from custom_messages.enums import TaskSteeringType

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    setup_note = "The tool setup this task paused for ended while the task was paused. Continue the task."
    manager.append_runtime_resume_directive(parent.task_id, setup_note)
    agent = _make_agent(tmp_path, task_manager=manager)
    handed: dict[str, object] = {}

    async def model_call(_prompt, **_kwargs):
        return SimpleNamespace(stop_reason="interrupt", structured_output=None)

    async def pause_for_approval(**kwargs):
        handed.update(kwargs)
        return TaskSteering(steering_type=TaskSteeringType.INTERRUPT_REQUEST, target_task_id=parent.task_id)

    executor = _turn_on_the_real_outbox(agent, manager, model_call)
    executor.effects = dataclasses.replace(executor.effects, send_interrupt_request_for_result=pause_for_approval)
    info = manager.get_task_info(parent.task_id)
    asyncio.run(executor.process_llm_interaction(parent, info.header, info.meta))
    assert setup_note in _saved_note(tmp_path, parent.task_id)  # still saved while the task waits
    start_notes = handed["start_notes"]
    assert start_notes is not None and start_notes.resume_directive == setup_note

    # The decision arrives: the live session resumes and its decision is recorded.
    resumed_strands_agent = SimpleNamespace(
        invoke_async=AsyncMock(
            return_value=SimpleNamespace(
                stop_reason="end_turn",
                structured_output=StructuredAgentTurnDecision(
                    actions=[
                        TaskResponseAction(
                            task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer="Set up and done.")
                        )
                    ]
                ),
            )
        )
    )
    session = task_steering_runtime.StrandsInterruptedTurnSession(
        task_id=parent.task_id,
        strands_agent=resumed_strands_agent,
        invoke_kwargs={},
        full_prompt="",
        msg_content=parent,
        msg_header=info.header,
        msg_meta=info.meta,
        created_at=_now(),
        interrupts=[],
        start_notes=start_notes,
    )
    object.__setattr__(agent, "_apply_task_processing_result", AsyncMock())
    steering = TaskSteering(steering_type=TaskSteeringType.INTERRUPT_RESPONSE, target_task_id=parent.task_id)
    tool_stop = ToolStop(agent_id=agent.id, task_id=parent.task_id)
    asyncio.run(
        task_steering_runtime._run_resumed_turn(
            agent, session, steering, info, [{"interruptResponse": {}}], tool_stop
        )
    )
    assert setup_note not in _saved_note(tmp_path, parent.task_id)


def test_a_resumed_approval_turn_cancelled_at_shutdown_ends_its_tool_work_and_keeps_its_note(tmp_path) -> None:
    """Both rules meet here: a cancelled turn ends its commands (task stop), and a
    turn cancelled before its decision is recorded keeps the note it read."""
    from custom_agents import task_steering_runtime
    from custom_agents.task_runtime_state import TurnStartNotes
    from custom_messages.contracts.task_v1 import TaskSteering, TaskSteeringInterruptRecord
    from custom_messages.enums import TaskSteeringType

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    manager.append_runtime_resume_directive(parent.task_id, ACCESS_NOTE)
    info = manager.get_task_info(parent.task_id)
    stops: list[ToolStop] = []

    async def scenario() -> None:
        model_called = asyncio.Event()

        async def resumed_model_call(_prompt, **_kwargs):
            model_called.set()
            await asyncio.Event().wait()

        session = task_steering_runtime.StrandsInterruptedTurnSession(
            task_id=parent.task_id,
            strands_agent=SimpleNamespace(invoke_async=resumed_model_call),
            invoke_kwargs={},
            full_prompt="",
            msg_content=parent,
            msg_header=info.header,
            msg_meta=info.meta,
            created_at=_now(),
            interrupts=[TaskSteeringInterruptRecord(interrupt_id="interrupt_1", reason={"access_request_id": "areq_1"})],
            start_notes=TurnStartNotes(parent.task_id, resume_directive=ACCESS_NOTE),
        )
        steering = TaskSteering(
            steering_type=TaskSteeringType.INTERRUPT_RESPONSE,
            target_task_id=parent.task_id,
            interrupt_responses=[
                {"interrupt_id": "interrupt_1", "response": {"access_request_id": "areq_1", "status": "granted"}}
            ],
        )
        agent = SimpleNamespace(
            id="agent_id_manager",
            name="manager",
            task_manager=manager,
            _interrupted_turn_sessions={parent.task_id: session},
            _pending_interrupt_responses={parent.task_id: steering},
            _strands_agent=None,
            _strands_agent_replacement_required=False,
            _set_active_execution_context=lambda task_id: stops.append(
                ToolStop(agent_id="agent_id_manager", task_id=task_id)
            )
            or stops[-1],
            _clear_active_execution_context=lambda: None,
            _refresh_self_agent_state=AsyncMock(),
            _logger=logging.getLogger("test_turn_action_outbox"),
        )
        resumed = asyncio.create_task(task_steering_runtime.resume_interrupted_task_turn(agent, parent.task_id))
        await asyncio.wait_for(model_called.wait(), 5)
        resumed.cancel()  # shutdown cancels the resumed turn before its decision
        with pytest.raises(asyncio.CancelledError):
            await resumed

    asyncio.run(scenario())
    assert stops and stops[0].reason == "turn_cancelled"  # its tool work was ended
    assert ACCESS_NOTE in _saved_note(tmp_path, parent.task_id)  # and its note kept


def test_a_recorded_batch_and_its_released_note_are_saved_together(tmp_path) -> None:
    """A crash never keeps a note for a recorded decision, nor drops one for an unrecorded decision."""
    from custom_agents.task_runtime_state import TurnStartNotes

    manager = _make_task_manager(tmp_path)
    parent = _add_active_parent_task(manager)
    manager.append_runtime_resume_directive(parent.task_id, ACCESS_NOTE)
    read = TurnStartNotes(parent.task_id, resume_directive=ACCESS_NOTE)

    assert manager.create_turn_action_batch("task_unknown", [], release_notes=read) is None
    assert ACCESS_NOTE in _saved_note(tmp_path, parent.task_id)  # no decision recorded, note kept

    batch = manager.create_turn_action_batch(parent.task_id, [], release_notes=read)
    reloaded = _make_task_manager(tmp_path)
    reloaded.restore_runtime_state()
    assert reloaded.get_turn_action_batch(batch.batch_id) is not None
    assert reloaded.read_runtime_resume_directive(parent.task_id) is None
