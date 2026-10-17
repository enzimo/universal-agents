"""End to end with real agents: a worker that loses its lease hands its task on.

A real TeamLead and two real workers of one type share a SQLite store and a
live NATS server; only the model is a stand-in. Worker A starts a user task
whose model call blocks, then loses its launch lease. A must interrupt the run,
hand the task to the lead and exit; the lead must send it out again; worker B
must run it with the note about the interrupted run and answer the user once.

In the second run, worker A's model is a real Strands agent whose scripted
model asks for the real shell tool, so Strands runs the command in a worker
thread with the turn's real invocation keywords (the nested invocation_state
included). Worker A must end that command before it hands the task back, so
it is gone before worker B reruns the task and never finishes.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from strands import Agent

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
from custom_tools.non_interactive_shell import shell
from custom_tools.runtime_context import set_agent_ref
from custom_tools.workspace_scope import current_workspace_scope
from tests.turn_tool_stop_support import ScriptedShellModel, kill_if_alive, process_alive
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


class _ShellRun:
    """Worker A's model in the shell run: a real Strands agent with the real shell tool.

    Its scripted model asks for one shell command, which records its process
    id, sleeps two minutes and would then write a file. The shell ignores
    SIGTERM, but RTK's wrapper still ends it at once, so this run does not show
    the forced kill; ``test_tool_stop`` covers that at the process supervisor.
    """

    COMMAND = "trap '' TERM; echo $$ > worker-a.pid; sleep 120; touch worker-a-finished.txt"

    def __init__(self) -> None:
        self.model = ScriptedShellModel([self.COMMAND], Path("."))
        self.agent = Agent(model=self.model, tools=[shell], callback_handler=None)
        self.work_dir: Path | None = None

    def use_folder(self, work_dir: Path) -> None:
        self.work_dir = self.model.work_dir = work_dir

    @property
    def pid_file(self) -> Path:
        assert self.work_dir is not None
        return self.work_dir / "worker-a.pid"

    @property
    def finished_file(self) -> Path:
        assert self.work_dir is not None
        return self.work_dir / "worker-a-finished.txt"

    def command_alive(self) -> bool:
        text = self.pid_file.read_text().strip() if self.pid_file.exists() else ""
        return text.isdigit() and process_alive(int(text))

    async def wait_until(self, alive: bool, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.command_alive() != alive:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"The shell command never became alive={alive}")
            await asyncio.sleep(0.05)


class _RerunModel(FakeStrandsAgent):
    """Worker B's model; records whether worker A's command still ran when B started."""

    def __init__(self, command_alive, *responses) -> None:
        super().__init__(*responses)
        self.command_alive = command_alive
        self.alive_at_rerun: list[bool] = []

    async def invoke_async(self, prompt: str, **kwargs: object):
        self.alive_at_rerun.append(self.command_alive())
        return await super().invoke_async(prompt, **kwargs)


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


@pytest.mark.parametrize("runs_shell_command", [False, True], ids=["model_call", "shell_command"])
async def test_lost_lease_hands_the_running_task_to_another_worker(monkeypatch, runs_shell_command) -> None:
    monkeypatch.setattr(wind_down, "WIND_DOWN_POLL_SECONDS", 0.05)
    monkeypatch.setattr(wind_down, "HANDBACK_PUBLISH_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(handback_runtime, "interruption_limit", lambda agent: 3)
    admissions = []
    real_apply = task_message_runtime.apply_redispatch_notice

    async def record_admission(agent, task_id, meta):
        admissions.append((agent.id, task_id, getattr(meta, "redispatch_notice", None)))
        return await real_apply(agent, task_id, meta)

    monkeypatch.setattr(task_message_runtime, "apply_redispatch_notice", record_admission)
    alive_at_handback: list[bool] = []
    publish_handback = wind_down._publish_handback

    async def record_handback(agent, state, handed_back):
        if shell_run is not None:
            alive_at_handback.append(shell_run.command_alive())
        return await publish_handback(agent, state, handed_back)

    monkeypatch.setattr(wind_down, "_publish_handback", record_handback)

    team_id = new_id("team_handback")
    task_id = new_id("handback_task")
    lead_llm = FakeStrandsAgent()
    shell_run = _ShellRun() if runs_shell_command else None
    blocking_model = None if shell_run is not None else _BlockingModel()
    worker_a_llm = shell_run.agent if shell_run is not None else blocking_model
    finished = _task_response("Worker B finished the report.")
    if shell_run is not None:
        worker_b_llm = _RerunModel(shell_run.command_alive, finished)
    else:
        worker_b_llm = FakeStrandsAgent(finished)

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
            if shell_run is not None:
                # The live request carries no gateway-signed context, which code
                # needs with authorization on; with it off code runs in the
                # task's folders (custom_agents.code_access).
                worker_a.authorization_config = {"enabled": False}
                # The runtime puts its configured model on the Strands agent each
                # turn (person_model_runtime.apply_model); here that is the script.
                worker_a._strands_model = shell_run.model
                shell_run.use_folder(current_workspace_scope(worker_a).home)
                shell_run.work_dir.mkdir(parents=True, exist_ok=True)
            agent_tasks = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_handback"), durable=False, auto_delete=True
                )
                for agent in (lead, worker_a):
                    # Each agent's tasks copy this context when they start: the
                    # runtime gives each agent its own agent reference this way
                    # (custom_agents.strands_team), and the shell tool needs it.
                    set_agent_ref(agent)
                    agent_tasks.append((agent, await _start_agent(agent)))
                set_agent_ref(None)

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
                if shell_run is not None:
                    await shell_run.wait_until(alive=True, timeout=20)
                else:
                    await asyncio.wait_for(blocking_model.started.wait(), timeout=20)

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

                if shell_run is not None:
                    await shell_run.wait_until(alive=False, timeout=15)
                else:
                    await asyncio.wait_for(blocking_model.cancelled.wait(), timeout=10)
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
                if shell_run is not None:
                    # Worker A ended its command before handing the task back.
                    assert alive_at_handback == [False]
                    assert worker_b_llm.alive_at_rerun == [False]
                    assert not shell_run.command_alive()
                    assert not shell_run.finished_file.exists()
            finally:
                if shell_run is not None and shell_run.work_dir is not None:
                    kill_if_alive(shell_run.pid_file)
                set_agent_ref(None)
                await _stop_agents(agent_tasks)
                await connection.close()
