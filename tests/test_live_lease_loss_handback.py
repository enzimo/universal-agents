"""End to end with real agents: a worker that loses its lease hands its task on.

A real TeamLead and two real workers of one type share a SQLite store and a
live NATS server; only the model is a stand-in. Worker A starts a user task
whose model call blocks, then loses its launch lease. A must interrupt the run,
hand the task to the lead and exit; the lead must send it out again; worker B
must run it with the note about the interrupted run and answer the user once.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import custom_agents.lease_loss_wind_down as wind_down
import custom_agents.task_message_runtime as task_message_runtime
import custom_agents.worker_handback_runtime as handback_runtime
from custom_agents.agent_instantiation_service import PROCESS_HOST_ID
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_worker_v1 import (
    AgentLaunchState,
    AgentLaunchV1,
    utc_now,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.contracts.worker_handback_v1 import handback_identity
from custom_messages.enums import MessageMetaType, RequesterType
from id_generation import new_id
from storage.agent_launch_repository import LaunchCapacity
from storage.agent_launch_repository import AgentLaunchRepository
from storage.worker_handback_repository import (
    HANDBACK_STATUS_REDISPATCHED,
    WorkerHandbackRepository,
)
from tests.test_live_multi_agent_state_boundaries import (
    FakeStrandsAgent,
    _agent,
    _connect_nats,
    _publish_envelope,
    _start_agent,
    _stop_agents,
    _task_response,
    _wait_for_response,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.live, pytest.mark.nats]

WORKER_TYPE = "WebResearchAgent"


class _BlockingModel(FakeStrandsAgent):
    """A model call that never returns until the run is interrupted."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def invoke_async(self, prompt: str, **_: object):
        self.invocations.append(prompt)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _ready_launch(worker) -> AgentLaunchV1:
    """Give a test worker the launch record the lead validates handbacks with."""

    launch, _created = AgentLaunchRepository(worker.task_manager.storage_settings).reserve(
        AgentLaunchV1(
            launch_id=new_id("launch"),
            agent_id=worker.id,
            team_id=worker.team_id,
            owner_canonical_user_id="live-test-user",
            named_agent_type=WORKER_TYPE,
            definition_digest="a" * 64,
            host_id=PROCESS_HOST_ID,
            source="delegation",
            state=AgentLaunchState.READY,
            lease_expires_at_utc=utc_now() + timedelta(minutes=2),
        ),
        LaunchCapacity.defaults(),
        join=False,
    )
    return launch


async def test_lost_lease_hands_the_running_task_to_another_worker(monkeypatch) -> None:
    monkeypatch.setattr(wind_down, "WIND_DOWN_POLL_SECONDS", 0.05)
    monkeypatch.setattr(wind_down, "HANDBACK_PUBLISH_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(handback_runtime, "interruption_limit", lambda agent: 3)
    admissions = []
    real_apply = task_message_runtime.apply_redispatch_notice

    async def record_admission(agent, task_id, meta):
        admissions.append((agent.id, task_id, getattr(meta, "redispatch_notice", None)))
        return await real_apply(agent, task_id, meta)

    monkeypatch.setattr(task_message_runtime, "apply_redispatch_notice", record_admission)

    team_id = new_id("team_handback")
    task_id = new_id("handback_task")
    lead_llm = FakeStrandsAgent()
    worker_a_llm = _BlockingModel()
    worker_b_llm = FakeStrandsAgent(_task_response("Worker B finished the report."))

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "handback.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        with (
            patch(
                "custom_agents.strands_agent.Agent",
                side_effect=[lead_llm, worker_a_llm, worker_b_llm],
            ),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            lead = _agent(
                agent_id=new_id("manager"),
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            lead.named_agent_type_queue_map[WORKER_TYPE] = f"{team_id}_{WORKER_TYPE}"
            lead.subtask_status_probe_timeout_seconds = 1.0
            worker_a = _agent(
                agent_id=new_id("worker_a"),
                name="worker-a",
                team_id=team_id,
                named_agent_type=WORKER_TYPE,
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            agent_tasks = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_handback"), durable=False, auto_delete=True
                )
                for agent in (lead, worker_a):
                    agent_tasks.append((agent, await _start_agent(agent)))

                request = MessageEnvelope(
                    msg_content=Task(
                        task_id=task_id,
                        task_type=MessageMetaType.REQUEST,
                        problem_statement="Compile the weekly report.",
                    ),
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=f"{team_id}_{WORKER_TYPE}",
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        to_named_agent_type=WORKER_TYPE,
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=f"corr_{task_id}",
                        conversation_id=f"conversation_{task_id}",
                        request_timezone="America/Chicago",
                    ),
                )
                await _publish_envelope(channel, f"{team_id}_{WORKER_TYPE}", request)
                await asyncio.wait_for(worker_a_llm.started.wait(), timeout=20)

                # Worker B joins while A is alive, so it does not adopt A's tasks.
                worker_b = _agent(
                    agent_id=new_id("worker_b"),
                    name="worker-b",
                    team_id=team_id,
                    named_agent_type=WORKER_TYPE,
                    db_path=db_path,
                    workspace_dir=workspace_dir,
                )
                agent_tasks.append((worker_b, await _start_agent(worker_b)))

                launch = _ready_launch(worker_a)
                worker_a._worker_launch = launch
                wind_down.start_lease_loss_wind_down(worker_a, reason="stale_launch_lease")

                await asyncio.wait_for(worker_a_llm.cancelled.wait(), timeout=10)
                response = await _wait_for_response(user_queue, timeout=40)
                worker_a_task = next(task for agent, task in agent_tasks if agent is worker_a)
                await asyncio.wait_for(asyncio.shield(worker_a_task), timeout=20)

                assert response.msg_content.task_id == task_id
                assert response.msg_content.answer == "Worker B finished the report."
                handback_id = handback_identity(
                    worker_agent_id=worker_a.id, launch_id=launch.launch_id, task_id=task_id
                )
                assert (
                    WorkerHandbackRepository(lead.task_manager.storage_settings).status(
                        handback_id, team_id
                    )
                    == HANDBACK_STATUS_REDISPATCHED
                )
                redispatched = [entry for entry in admissions if entry[2] is not None]
                assert [(agent_id, admitted) for agent_id, admitted, _ in redispatched] == [
                    (worker_b.id, task_id)
                ]
                notice = redispatched[0][2]
                assert notice.prior_disposition == "interrupted"
                assert notice.prior_worker_agent_id == worker_a.id
                assert any("already ran on another worker" in prompt for prompt in worker_b_llm.invocations)
                # The original delivery on A was settled; B ran the task only once.
                await asyncio.sleep(2)
                assert [admitted for agent_id, admitted, _ in admissions if agent_id == worker_b.id] == [
                    task_id
                ]
                assert await user_queue.get(timeout=1) is None
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()
