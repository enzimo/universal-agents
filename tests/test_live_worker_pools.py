"""Worker pools on a real NATS server with fake models (ADR-019).

Reproduces the 2026-10-04 findings and proves the fix: delegated tasks wait in
their worker type's shared queue, a worker takes one only when free, nothing
fails while it waits, tasks spread over free workers, the requester records
which worker holds each task, and the worker pool controller starts workers
from the queue's real state.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_agents.delegated_work_runtime import delegated_tasks
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchResultV1
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_transport.base import TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from live_nats_client import nats_transport_config_from_env
from runtime_settings.schedule_workers import ScheduleWorkersConfig
from runtime_settings.worker_pools import WorkerPoolsConfig
from storage import resolve_storage_settings
from test_live_multi_agent_state_boundaries import (
    DelayedFakeStrandsAgent,
    FakeStrandsAgent,
    _agent,
    _connect_nats,
    _publish_envelope,
    _start_agent,
    _stop_agents,
    _task_response,
    _wait_for,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_TYPE = "ResearchComparisonAgent"
WORKER_SECONDS = 2.0


def _fan_out(count: int, *, check_back: float | None = None) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=f"Research topic number {index}.",
                ),
                target_named_agent_type=WORKER_TYPE,
                check_back_after_minutes=check_back,
            )
            for index in range(1, count + 1)
        ]
    )


def _user_request(root_task_id: str, manager_id: str, reply_route: str) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=root_task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Research several topics in parallel.",
        ),
        msg_header=MessageHeader(
            from_agent_id="live-test-user",
            to_routing_path=manager_id,
            response_routing_key=reply_route,
        ),
        msg_meta=MessageMeta(
            from_agent_name="live-test-user",
            requester_type=RequesterType.USER_REQUESTER,
            is_response_requested=True,
            msg_correlation_id=f"corr_{root_task_id}",
            conversation_id=f"conversation_{root_task_id}",
            request_timezone="America/Chicago",
            gateway_context=GatewayContext(
                gateway_instance_name="live-test",
                session_key="session-pools",
                channel_name="cli",
                canonical_user_id="usr_live_test_user",
                chat_id="cli:live-test",
                delivery_mode="interactive",
            ),
        ),
    )


async def _run_fan_out(worker_count: int, task_count: int):
    """Delegate ``task_count`` tasks to ``worker_count`` workers; return what happened."""

    team_id = new_id("team_pools")
    root_task_id = new_id("root_pools")
    queue_name = f"{team_id}_{WORKER_TYPE}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        manager_llm = FakeStrandsAgent(_fan_out(task_count), _task_response("LEAD_FINAL"))
        worker_llms = [
            DelayedFakeStrandsAgent(
                WORKER_SECONDS, *[_task_response(f"WORKER{w}_RESULT_{i}") for i in range(task_count)]
            )
            for w in range(worker_count)
        ]
        with (
            patch("custom_agents.strands_agent.Agent", side_effect=[manager_llm, *worker_llms]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
        ):
            manager = _agent(
                agent_id=new_id("manager"),
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db",
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update({WORKER_TYPE: queue_name})
            manager.subtask_status_probe_timeout_seconds = 1.0

            def refuse_creation(*_args, **_kwargs):
                raise AssertionError("delegation must not start workers itself")

            object.__setattr__(manager, "handle_agent_creation_request", refuse_creation)
            workers = []
            for index in range(worker_count):
                worker = _agent(
                    agent_id=new_id(f"research{index}"),
                    name=f"research-{index}",
                    team_id=team_id,
                    named_agent_type=WORKER_TYPE,
                    db_path=tmp_path / f"worker{index}.db",
                    workspace_dir=workspace_dir,
                )
                worker.named_agent_type_queue_map.update({WORKER_TYPE: queue_name})
                workers.append(worker)
            agent_tasks = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(new_id("usr_pools"), durable=False, auto_delete=True)
                for worker in workers:
                    agent_tasks.append((worker, await _start_agent(worker)))
                agent_tasks.append((manager, await _start_agent(manager)))
                await _publish_envelope(channel, manager.id, _user_request(root_task_id, manager.id, user_queue.name))
                answer = None
                deadline = asyncio.get_running_loop().time() + WORKER_SECONDS * task_count + 30
                while asyncio.get_running_loop().time() < deadline:
                    message = await user_queue.get(timeout=0.5, fail=False)
                    if message is not None and isinstance(message.msg_content, Task):
                        answer = message.msg_content
                        break
                records = delegated_tasks(manager).list_for_root(team_id, root_task_id)
                history = manager.task_manager.get_task_from_db(root_task_id, owner_agent_id=manager.id)
                return SimpleNamespace(
                    answer=answer,
                    records=records,
                    manager_calls=len(manager_llm.invocations),
                    worker_calls=[len(llm.invocations) for llm in worker_llms],
                    history=history,
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_three_tasks_on_one_busy_worker_all_finish_without_false_failures() -> None:
    result = await _run_fan_out(worker_count=1, task_count=3)
    assert result.answer is not None and result.answer.answer == "LEAD_FINAL"
    assert not result.answer.errors
    assert [record.status for record in result.records] == ["finished"] * 3
    assert result.worker_calls == [3]
    # The lead's model ran twice: to delegate, then once with every result.
    assert result.manager_calls == 2


async def test_live_tasks_spread_over_free_workers() -> None:
    result = await _run_fan_out(worker_count=3, task_count=6)
    assert result.answer is not None and not result.answer.errors
    assert sorted(result.worker_calls) == [2, 2, 2]
    assert {record.status for record in result.records} == {"finished"}
    holders = {record.holder_agent_id for record in result.records}
    assert len(holders) == 3
    assert result.manager_calls == 2


# -- the controller against a real queue -------------------------------------------


class _Runtime:
    async def run(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _LocalRuntime:
    def __init__(self, agent_types):
        # A list, so a test can add a type while the team runs.
        self.types = list(agent_types)
        self.workers: list = []
        self.drained: list[str] = []

    def agent_types(self):
        return tuple(self.types)

    def local_workers(self, team_id):
        return tuple(self.workers)

    def drain_worker(self, agent_id):
        self.drained.append(agent_id)
        return True


@pytest.fixture()
def controller_world(tmp_path, monkeypatch):
    team_id = new_id("team_poolctl")
    mapper = NatsSubjectMapper(team_id=team_id, named_agent_type_queue_map={WORKER_TYPE: f"{team_id}_{WORKER_TYPE}"})
    adapter = NatsTransportAdapter(config=nats_transport_config_from_env(), mapper=mapper)
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "ctl.db")}})
    agent = SimpleNamespace(
        id="lead_ctl",
        name="lead",
        team_id=team_id,
        is_team_lead=True,
        task_manager=SimpleNamespace(storage_settings=storage, operations_runtime=_Runtime()),
        _local_team_runtime=_LocalRuntime([WORKER_TYPE, "TeamArchitect"]),
        _ensured_worker_queues=set(),
        _worker_pool_snapshot={},
        _logger=logging.getLogger("controller-test"),
        _ensure_nats_transport_adapter=lambda: adapter,
    )
    settings = WorkerPoolsConfig(scale_up_wait_seconds=0)
    monkeypatch.setattr(
        "custom_agents.worker_pool_settings.current_worker_pool_settings", lambda _agent: settings
    )
    monkeypatch.setattr(
        "custom_agents.agent_instantiation_service.current_worker_limits",
        lambda _agent: ScheduleWorkersConfig(),
    )
    reviews: list = []

    async def record_review(_agent, incident, **kwargs):
        reviews.append((incident.agent_type, incident.kind, kwargs.get("recurring")))
        return f"review_{len(reviews)}"

    monkeypatch.setattr("custom_agents.worker_pool_reviews.open_review", record_review)
    return SimpleNamespace(agent=agent, adapter=adapter, mapper=mapper, reviews=reviews, settings=settings)


async def _publish_tasks(adapter, mapper, count, agent_type=WORKER_TYPE):
    for index in range(count):
        await adapter.publish(
            envelope=MessageEnvelope(
                msg_content=Task(task_id=new_id("queued"), task_type=MessageMetaType.REQUEST, problem_statement=f"t{index}"),
                msg_header=MessageHeader(from_agent_id="lead_ctl", to_routing_path=f"{mapper.team_id}_{agent_type}"),
                msg_meta=MessageMeta(to_named_agent_type=agent_type),
            ),
            routing_key=f"{mapper.team_id}_{agent_type}",
            options=TransportPublishOptions(durable=True),
        )


async def test_live_controller_starts_workers_from_the_real_queue(controller_world) -> None:
    from custom_agents.worker_pool_controller import WorkerPoolController

    world = controller_world
    await world.adapter.connect(reconcile_topology=True)
    launches: list[str] = []

    def fake_launch(self, agent_type):
        launches.append(agent_type)
        return AgentLaunchResultV1(status="starting", agent_id=new_id("w"), launch_id=new_id("l"), created=True)

    try:
        with patch("custom_agents.worker_pool_controller.WorkerPoolController._launch", fake_launch):
            controller = WorkerPoolController(world.agent)
            # The queue exists before any worker runs, so waiting tasks are counted.
            await _publish_tasks(world.adapter, world.mapper, 3)
            await controller.reconcile()
            assert launches == [WORKER_TYPE]  # no worker yet: one at once
            pool = world.agent._worker_pool_snapshot["pools"][WORKER_TYPE]
            assert pool["tasks_waiting"] == 3

            # One worker runs and none asks: start up to the pool size of 4.
            world.agent._local_team_runtime.workers = [
                SimpleNamespace(self_id="w1", named_agent_type=WORKER_TYPE, draining=False, cooling_down=False, launched=True, idle=False)
            ]
            launches.clear()
            await controller.reconcile()
            assert launches == [WORKER_TYPE] * 3

            # At the pool size with tasks waiting: a capacity review for TeamArchitect.
            world.agent._local_team_runtime.workers = [
                SimpleNamespace(self_id=f"w{i}", named_agent_type=WORKER_TYPE, draining=False, cooling_down=False, launched=True, idle=False)
                for i in range(4)
            ]
            launches.clear()
            await controller.reconcile()
            assert launches == []
            assert (WORKER_TYPE, "capacity", False) in world.reviews
            # The same open problem is not reviewed again before the repeat time.
            await controller.reconcile()
            assert [review for review in world.reviews if review[1] == "capacity" and review[0] == WORKER_TYPE] == [
                (WORKER_TYPE, "capacity", False)
            ]
    finally:
        await world.adapter.close()


async def test_live_controller_drains_workers_above_a_lowered_size(controller_world) -> None:
    from custom_agents.worker_pool_controller import WorkerPoolController
    from custom_agents.worker_pool_limits import set_pool_limit

    world = controller_world
    await world.adapter.connect(reconcile_topology=True)
    try:
        set_pool_limit(
            world.agent,
            scope=WORKER_TYPE,
            max_workers=1,
            until_utc=None,
            reason="the provider allows one at a time",
            settings=world.settings,
            set_by="test",
        )
        world.agent._local_team_runtime.workers = [
            SimpleNamespace(self_id="busy", named_agent_type=WORKER_TYPE, draining=False, cooling_down=False, launched=True, idle=False),
            SimpleNamespace(self_id="idle", named_agent_type=WORKER_TYPE, draining=False, cooling_down=False, launched=True, idle=True),
            SimpleNamespace(self_id="roster", named_agent_type=WORKER_TYPE, draining=False, cooling_down=False, launched=False, idle=True),
        ]
        await WorkerPoolController(world.agent).reconcile()
        # Two above the size; idle first, and never a startup (roster) worker.
        assert world.agent._local_team_runtime.drained == ["idle", "busy"]
    finally:
        await world.adapter.close()


async def test_live_a_queued_task_is_withdrawn_from_the_stream(controller_world) -> None:
    world = controller_world
    await world.adapter.connect(reconcile_topology=True)
    try:
        await world.adapter.ensure_worker_queue(WORKER_TYPE)
        await _publish_tasks(world.adapter, world.mapper, 2)
        state = await world.adapter.worker_queue_state(WORKER_TYPE)
        assert state.waiting == 2
        stream = world.mapper.work_stream_name()
        first = await world.adapter._js.get_msg(stream, seq=1, subject=world.mapper.named_type_work_subject(WORKER_TYPE), next=True)
        assert await world.adapter.delete_work_message(first.seq)
        assert (await world.adapter.worker_queue_state(WORKER_TYPE)).waiting == 1
        assert not await world.adapter.delete_work_message(first.seq)
        assert await world.adapter.oldest_worker_queue_message_at(WORKER_TYPE) <= datetime.now(timezone.utc)
    finally:
        await world.adapter.close()


async def test_live_an_old_push_consumer_is_replaced_and_its_tasks_kept(controller_world) -> None:
    from nats.js.api import AckPolicy, ConsumerConfig

    world = controller_world
    await world.adapter.connect(reconcile_topology=True)
    try:
        stream = world.mapper.work_stream_name()
        durable = world.adapter.worker_queue_durable_name(WORKER_TYPE)
        await world.adapter._js.add_consumer(
            stream,
            ConsumerConfig(
                durable_name=durable,
                filter_subject=world.mapper.named_type_work_subject(WORKER_TYPE),
                deliver_subject=f"deliver.{new_id('old')}",
                deliver_group=durable,
                ack_policy=AckPolicy.EXPLICIT,
                metadata={"ua.managed": "true", "ua.schema": "consumer-lifecycle-v1"},
            ),
        )
        await _publish_tasks(world.adapter, world.mapper, 2)
        received: list[str] = []

        async def handler(delivery):
            received.append(delivery.envelope.msg_content.problem_statement)
            await delivery.ack()

        from custom_transport.base import FetchPolicy

        await world.adapter.subscribe_route(
            name="worker",
            routing_key=f"{world.mapper.team_id}_{WORKER_TYPE}",
            handler=handler,
            durable_name=durable,
            consumer_role="named_worker",
            fetch_policy=FetchPolicy.WHEN_READY,
            ready=lambda: True,
        )
        await _wait_for(lambda: len(received) == 2, timeout=10)
        info = await world.adapter._js.consumer_info(stream, durable)
        assert not info.config.deliver_subject
    finally:
        await world.adapter.close()


async def test_live_claim_records_the_holder_and_the_probe_reaches_only_it() -> None:
    result = await _run_fan_out(worker_count=2, task_count=2)
    assert result.answer is not None and not result.answer.errors
    for record in result.records:
        assert record.holder_agent_id
        assert record.holders == [record.holder_agent_id]
        assert record.interruptions == 0
        assert record.status == "finished"
    assert result.records[0].claimed_at_utc is not None
    assert ProgressState.COMPLETED.value


async def test_live_controller_starts_workers_for_a_type_created_while_the_team_runs(controller_world) -> None:
    """A specialist type TeamArchitect creates is in the catalog at once; the
    controller reads the catalog on every pass and starts its workers."""
    from custom_agents.worker_pool_controller import WorkerPoolController

    world = controller_world
    new_type = "MarketScanAgent"
    await world.adapter.connect(reconcile_topology=True)
    launches: list[str] = []

    def fake_launch(self, agent_type):
        launches.append(agent_type)
        return AgentLaunchResultV1(status="starting", agent_id=new_id("w"), launch_id=new_id("l"), created=True)

    try:
        with patch("custom_agents.worker_pool_controller.WorkerPoolController._launch", fake_launch):
            controller = WorkerPoolController(world.agent)
            await controller.reconcile()
            assert new_type not in world.agent._worker_pool_snapshot.get("pools", {})

            world.agent._local_team_runtime.types.append(new_type)
            await _publish_tasks(world.adapter, world.mapper, 2, agent_type=new_type)
            await controller.reconcile()
            assert launches == [new_type]
            assert world.agent._worker_pool_snapshot["pools"][new_type]["tasks_waiting"] == 2
    finally:
        await world.adapter.close()


async def test_live_a_named_worker_gets_the_task_while_another_worker_is_free() -> None:
    """A delegation naming one worker reaches that worker's own mailbox even
    though another worker of the same type is idle and asking the shared queue."""
    team_id = new_id("team_direct")
    root_task_id = new_id("root_direct")
    queue_name = f"{team_id}_{WORKER_TYPE}"
    decision = StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Continue your research."),
                target_named_agent_type=WORKER_TYPE,
                target_agent_name="research-1",
            )
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        manager_llm = FakeStrandsAgent(decision, _task_response("LEAD_FINAL"))
        idle_llm = FakeStrandsAgent(_task_response("WRONG_WORKER"))
        named_llm = FakeStrandsAgent(_task_response("NAMED_WORKER_RESULT"))
        with (
            patch("custom_agents.strands_agent.Agent", side_effect=[manager_llm, idle_llm, named_llm]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
        ):
            manager = _agent(
                agent_id=new_id("manager"),
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db",
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update({WORKER_TYPE: queue_name})
            manager.subtask_status_probe_timeout_seconds = 2.0
            workers = []
            for index in range(2):
                worker = _agent(
                    agent_id=new_id(f"research{index}"),
                    name=f"research-{index}",
                    team_id=team_id,
                    named_agent_type=WORKER_TYPE,
                    db_path=tmp_path / f"worker{index}.db",
                    workspace_dir=workspace_dir,
                )
                worker.named_agent_type_queue_map.update({WORKER_TYPE: queue_name})
                workers.append(worker)
            # Agent names get a unique suffix when created; the lead sees the full names.
            decision.actions[0].target_agent_name = workers[1].name
            agent_tasks = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(new_id("usr_direct"), durable=False, auto_delete=True)
                for worker in workers:
                    agent_tasks.append((worker, await _start_agent(worker)))
                agent_tasks.append((manager, await _start_agent(manager)))
                await _publish_envelope(channel, manager.id, _user_request(root_task_id, manager.id, user_queue.name))

                answer = None
                deadline = asyncio.get_running_loop().time() + 30
                while asyncio.get_running_loop().time() < deadline:
                    message = await user_queue.get(timeout=0.5, fail=False)
                    if message is not None and isinstance(message.msg_content, Task):
                        answer = message.msg_content
                        break
                assert answer is not None and answer.answer == "LEAD_FINAL"
                # Published to the named worker's own mailbox, not the shared queue.
                routes = [
                    action.side_effects.get("target_route")
                    for action in manager.task_manager.execution_state.turn_action_outbox.values()
                    if action.side_effects.get("delegated_task_id")
                ]
                assert routes == [workers[1].id]
                (record,) = delegated_tasks(manager).list_for_root(team_id, root_task_id)
                assert (record.target_agent_name, record.holder_agent_id) == (workers[1].name, workers[1].id)
                assert record.status == "finished"
                assert len(named_llm.invocations) == 1
                assert idle_llm.invocations == []
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()
