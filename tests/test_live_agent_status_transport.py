"""Live NATS/JetStream checks for AgentStatus presence and operational routing."""

from __future__ import annotations

from tests.memory_support import SYSTEM_MEMORY_WRITER
from tests.team_services_support import InProcessTeamServices
import asyncio
import contextlib
import logging
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from live_nats_client import LiveNatsClient, resolve_live_transport_settings

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_agents.manager_control_runtime import (
    handle_task_progress_status,
)
from custom_agents.schedule_store import ScheduleStore
from custom_transport.protocol1.delivery_refs import ensure_delivery_ref_key
from custom_agents.scheduled_runtime import prepare_memory_review_schedule
from custom_agents.task_manager import TaskManager
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import (
    ScheduledMessage,
)
from custom_messages.contracts.task_v1 import Task, TaskProgressStatus
from custom_messages.enums import (
    ControlType,
    MessageMetaType,
    ProgressState,
    RoutingType,
    ScheduleManagementClass,
)
from custom_messages.system_schedules import (
    COREMEM_AUDIT_SCHEDULE_NAME_PREFIX,
)
from custom_transport.base import SubscriptionDelivery
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from runtime_settings.projection import _project_transport
from runtime_settings.transport import TransportSettings
from storage.pinned_memory_repository import create_pinned_memory_repository
from tests.broker_access_support import context_signer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]

@pytest.fixture
def live_transport() -> TransportSettings:
    # Resolve once per test. Every client, agent, and gateway receives this value.
    return resolve_live_transport_settings()


async def _wait_for_agent_connected(agent, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if getattr(agent, "_worker_ready", False):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("Agent did not finish NATS operational setup in time.")


@pytest.mark.asyncio
async def test_local_memory_schedule_does_not_publish_to_remote_memreview_queue(tmp_path, live_transport):
    from functools import partial

    from custom_agents.strands_agent import UniversalAgent
    from custom_messages.contracts.routing_v1 import GatewayContext
    from custom_messages.enums import ScheduleManagementClass

    team_id = new_id("live_memreview")
    memreview_route = f"{team_id}_MemReviewAgent"
    agent_task = None

    async with LiveNatsClient(team_id, transport=live_transport) as client:
        memreview_queue = asyncio.Queue()

        async def memreview_handler(delivery) -> None:
            await memreview_queue.put(delivery.envelope)
            await delivery.ack()

        mapper = NatsSubjectMapper(
            team_id=team_id,
            subject_prefix=live_transport.subject_prefix,
            named_agent_type_queue_map={"MemReviewAgent": memreview_route},
        )
        await client.adapter.subscribe_subject(
            name="memory-review-schedule-dispatch-probe",
            subject=mapper.named_type_work_subject("MemReviewAgent"),
            handler=memreview_handler,
            delivery=SubscriptionDelivery.CORE,
            logical_route=memreview_route,
        )
        try:
            with patch("custom_agents.strands_agent.Agent") as mock_agent_class:
                mock_agent_class.return_value = MagicMock()
                with patch(
                    "custom_agents.strands_agent.UniversalAgent._configure_model",
                    return_value=MagicMock(),
                ):
                    agent = UniversalAgent(
                        **_project_transport(transport_cfg=live_transport.model_dump()),
                        role="Live Memory Review Manager",
                        tools=[],
                        team_id=team_id,
                        named_agent_type="MemReviewAgent",
                        is_team_lead=False,
                        authorization_config={"enabled": False},
                        skills_enabled=False,
                        task_manager=TaskManager(
                            db_path=str(tmp_path / "memory_review_transport.db")
                        ),
                        memory_config={"review": {"enabled": True}},
                    )
                    agent_task = asyncio.create_task(agent.connect())
                    ready = asyncio.create_task(_wait_for_agent_connected(agent))
                    done, _ = await asyncio.wait((ready, agent_task), return_when=asyncio.FIRST_COMPLETED)
                    if agent_task in done:
                        ready.cancel()
                        await agent_task
                    await ready
                    agent._pinned_memory_repository.create_coremem(
                        writer=SYSTEM_MEMORY_WRITER,
                        team_id=team_id,
                        fact_text="Local memory review schedules run on MemReviewAgent.",
                        capacity=10,
                    )

                    accepted = await agent.task_manager.operations_runtime.run(
                        partial(prepare_memory_review_schedule, agent),
                        "mem_rev_core_live",
                        ScheduledMessage(
                            schedule_id="mem_rev_core_live",
                            schedule_name=f"{COREMEM_AUDIT_SCHEDULE_NAME_PREFIX}live",
                            management_class=ScheduleManagementClass.SYSTEM,
                            claim_named_agent_type="MemReviewAgent",
                            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="owner")),
                            payload=Task(
                                task_type=MessageMetaType.REQUEST,
                                problem_statement="maintenance",
                            ),
                            interval_ms=1000,
                        ),
                    )

                    assert "Review kind: coremem_audit" in accepted.payload.problem_statement
                    assert agent._memory_review_store.list_review_jobs(pending_only=True)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(memreview_queue.get(), timeout=0.25)
        finally:
            if agent_task is not None:
                agent.set_exit_flag()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(agent_task, timeout=10)


@pytest.mark.asyncio
async def test_live_gateway_manager_controls_use_nats_and_separate_storage(tmp_path, live_transport, monkeypatch):
    from tests.broker_access_support import forget_task_context_keys, use_gateway_key

    # The manager acts only for the person the gateway signed onto each command.
    use_gateway_key(monkeypatch)
    team_id = new_id("live_decoupled")
    try:
        await _gateway_manager_controls_over_nats(tmp_path, live_transport, team_id)
    finally:
        forget_task_context_keys()


async def _gateway_manager_controls_over_nats(tmp_path, live_transport, team_id: str) -> None:
    manager_id = new_id("manager")
    gateway_db = tmp_path / "gateway.db"
    manager_db = tmp_path / "manager.db"
    config = GatewayInstanceConfig.create(
        instance_name="live_decoupled",
        instance_root=str(tmp_path / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        display_name="Live Admin",
        manager_agent_id=manager_id,
        manager_storage_sqlite_path=str(manager_db),
        state_storage_sqlite_path=str(gateway_db),
        named_agent_type="TeamArchitect",
    )
    config.transport = live_transport.model_copy(deep=True)
    # Memory and schedule commands go to the team services (tests/test_live_team_memory.py covers
    # them over NATS); here they run in process on the manager's database, which the gateway never
    # opens. A chat's schedule list names the chat with the launcher's delivery reference key.
    ensure_delivery_ref_key(config.layout)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(gateway_db)),
        team_services=InProcessTeamServices(config),
    )
    session = gateway.application.resolve_session(
        session_key=config.user.default_session_key
    )
    ScheduleStore(storage_settings=config.manager_storage_settings).add(ScheduledMessage(
        schedule_id="sched_live_decoupled",
        management_class=ScheduleManagementClass.USER,
        claim_team_id=team_id,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Check the vehicle listings."),
        payload_meta=MessageMeta(gateway_context=GatewayContext(
            canonical_user_id=config.user.canonical_user_id, session_key=session.session_key)),
        interval_ms=3_600_000,
    ))
    root_task = Task(
        task_id="task_live_decoupled",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Compare several vehicles.",
        task_state=[
            (ProgressState.IN_PROGRESS, datetime.now(timezone.utc).isoformat())
        ],
    )
    root_info = SimpleNamespace(
        task=root_task,
        state=ProgressState.IN_PROGRESS,
        owner_person_id=config.user.canonical_user_id,
        meta=MessageMeta(
            conversation_id=session.conversation_id,
            gateway_context=GatewayContext(
                canonical_user_id=config.user.canonical_user_id,
                session_key=session.session_key,
                task_context=context_signer().issue(
                    team_id=team_id, person_id=config.user.canonical_user_id, person_roles=("admin",),
                ),
            ),
        ),
    )
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        # Delegated-task records shown in the progress view live in this store.
        storage_settings=config.manager_storage_settings,
        get_top_level_task_infos=lambda: [root_info],
        get_task_plan_id=lambda _task_id: None,
        get_plan=lambda _plan_id: None,
        get_subtasks=lambda _task_id: [],
    )

    async with LiveNatsClient(team_id, transport=live_transport) as manager_client:

        class _ManagerComms:
            async def send_message(
                self, *, message: MessageEnvelope, routing_key: str
            ) -> None:
                await manager_client.publish(routing_key, message, durable=True)

        async def send_operational(message: MessageEnvelope) -> None:
            await manager_client.publish(
                RoutingType.ALL_AGENTS_ROUTE.value,
                message,
                operational=True,
                durable=False,
            )

        manager = SimpleNamespace(
            id=manager_id,
            name="Live Decoupled Manager",
            role="manager",
            named_agent_type="TeamArchitect",
            team_id=team_id,
            is_team_lead=True,
            is_executing=True,
            paused=False,
            memory_config={},
            task_manager=task_manager,
            team_comms=_ManagerComms(),
            _logger=logging.getLogger("test.live.decoupled_manager"),
            _pinned_memory_repository=create_pinned_memory_repository(
                config.manager_storage_settings
            ),
            _scheduler_wake=asyncio.Event(),
            _send_operational_message=send_operational,
        )
        schedule_store = ScheduleStore(storage_settings=config.manager_storage_settings)
        manager._ensure_schedule_store = lambda: schedule_store
        work_started = asyncio.Event()
        release_work = asyncio.Event()

        async def operational_handler(delivery) -> None:
            envelope = delivery.envelope
            content = envelope.msg_content
            if (
                isinstance(content, AgentStatus)
                and content.status_type == MessageMetaType.REQUEST
            ):
                response = MessageEnvelope(
                    msg_content=AgentStatus(
                        self_id=manager_id,
                        team_id=team_id,
                        named_agent_type="TeamArchitect",
                        is_paused=False,
                        is_executing=True,
                        has_execution_capacity=False,
                        is_busy=True,
                        name=manager.name,
                        agent_role=manager.role,
                        status_type=MessageMetaType.RESPONSE,
                        info_type=ControlType.STATUS_REQUEST_BASIC,
                    ),
                    msg_header=MessageHeader(
                        from_agent_id=manager_id,
                        to_routing_path=envelope.msg_header.response_routing_key,
                        agent_signature="",
                    ),
                    msg_meta=MessageMeta(
                        msg_correlation_id=envelope.msg_meta.msg_correlation_id,
                    ),
                )
                await send_operational(response)
            elif isinstance(content, TaskProgressStatus):
                await handle_task_progress_status(
                    manager,
                    request=content,
                    msg_header=envelope.msg_header,
                    msg_meta=envelope.msg_meta,
                )
            await delivery.ack()

        async def manager_work_handler(delivery) -> None:
            if not isinstance(delivery.envelope.msg_content, Task):
                await delivery.ack()
                return
            work_started.set()
            try:
                await release_work.wait()
            finally:
                await delivery.ack()

        await manager_client.adapter.subscribe_subject(
            name="live-decoupled-manager-operational",
            subject=manager_client.mapper.operational_subject(),
            handler=operational_handler,
            delivery=SubscriptionDelivery.CORE,
            logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
        )
        await manager_client.adapter.subscribe_route(
            name="live-decoupled-manager-work",
            routing_key=manager_id,
            handler=manager_work_handler,
            durable_name=manager_client.mapper.durable_name(f"manager_{manager_id}"),
        )

        try:
            await gateway.connect(start_services=False)
            blocking_task = MessageEnvelope(
                msg_content=Task(
                    task_id="task_live_blocking",
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Hold the manager work consumer open.",
                ),
                msg_header=MessageHeader(
                    from_agent_id="live_test_gateway",
                    to_routing_path=manager_id,
                    response_routing_key=config.manager.response_queue_name,
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id="corr_live_blocking",
                    conversation_id=session.conversation_id,
                    gateway_context=GatewayContext(
                        canonical_user_id=config.user.canonical_user_id,
                        session_key=session.session_key,
                        task_context=context_signer().issue(
                            team_id=team_id, person_id=config.user.canonical_user_id, person_roles=("admin",),
                        ),
                    ),
                ),
            )
            await manager_client.publish(manager_id, blocking_task, durable=True)
            await asyncio.wait_for(work_started.wait(), timeout=5)

            status = await gateway.application.request_task_progress_status(
                session_key=session.session_key
            )
            assert status.message_type == "task_progress_status"
            assert status.task_id == root_task.task_id
            assert "Task status: in progress" in status.text

            saved = await gateway.application.execute_interactive_command(
                raw_text=(
                    '/usermem add "NATS keeps gateway and manager storage independent."'
                ),
                current_session_key=session.session_key,
                channel_name=None,
                user_id=None,
                chat_id=None,
            )
            assert saved.handled is True
            assert "User memory saved" in str(saved.text or "")
            listed = await gateway.application.execute_interactive_command(
                raw_text="/usermem list",
                current_session_key=session.session_key,
                channel_name=None,
                user_id=None,
                chat_id=None,
            )
            assert listed.handled is True
            assert "NATS keeps gateway and manager storage independent." in str(
                listed.text or ""
            )
            listed_jobs = await gateway.application.list_scheduled_jobs(session_key=session.session_key)
            assert [(job.scheduled_job_id, job.task_summary) for job in listed_jobs] == [
                ("sched_live_decoupled", "Check the vehicle listings.")
            ]
        finally:
            release_work.set()
            await gateway.close()

    with sqlite3.connect(manager_db) as connection:
        manager_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    with sqlite3.connect(gateway_db) as connection:
        gateway_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert "pinned_memory_facts_active" in manager_tables
    assert "scheduled_messages" in manager_tables
    assert "gateway_sessions" in gateway_tables
    assert "pinned_memory_facts_active" not in gateway_tables
    assert "scheduled_messages" not in gateway_tables
