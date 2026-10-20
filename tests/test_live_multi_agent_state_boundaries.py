"""
Live multi-agent state-boundary tests.

These tests exercise NATS routing, per-agent TaskManager ownership, durable
named-agent queues, and non-blocking AfterActionReport handling with
deterministic fake Strands model outputs. They intentionally avoid real LLM
providers so failures point at runtime boundaries rather than model behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from live_nats_client import nats_transport_config_from_env, resolve_live_transport_settings

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback for minimal live-test envs.
    load_dotenv = None

from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import load_config
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.projection import _project_transport
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_agents.pending_work_runtime import schedule_pending_work
from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin
from custom_agents.strands_agent import UniversalAgent
from custom_agents.tool_call_limits import ToolCallLimitCallbackHandler
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskQueryAction,
    TaskResponseAction,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.schedule_worker_v1 import AgentCreationResultV1, AgentLaunchResultV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    ManagerEscalationContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.task_v1 import (
    AfterActionReport,
    Task,
    TaskQuery,
    TaskQueryChoiceOption,
)
from custom_messages.enums import (
    MessageMetaType,
    PlanActionType,
    PlanStatus,
    ProgressState,
    RequesterType,
    TaskHistoryImportance,
    TaskHistoryType,
)
from custom_tools.providers.objectives import (
    activate_plan_execution,
    start_isolated_recovery,
    update_execution_plan_step,
)
from custom_tools.runtime_context import set_agent_ref
from storage import resolve_storage_settings
from custom_transport.base import TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id, new_id_suffix
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIVE_ENV_PATH = PROJECT_ROOT / "debug.env"
DEFAULT_LIVE_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
_LIVE_ENV_LOADED = False
_LIVE_CONFIG = None


class FakeAgentResult:
    def __init__(self, structured_output: StructuredAgentTurnDecision):
        self.structured_output = structured_output

    def __str__(self) -> str:
        return self.structured_output.model_dump_json()


class FakeStrandsAgent:
    def __init__(self, *responses: StructuredAgentTurnDecision):
        self._responses = list(responses)
        self.messages = []
        self.invocations: list[str] = []
        self.invoke_kwargs: list[dict] = []
        # Like a Strands agent: the turn makes its handler this agent's own for each call.
        self.callback_handler = None
        self.callback_handlers: list[object] = []

    async def invoke_async(self, prompt: str, **_: object) -> FakeAgentResult:
        self.invocations.append(prompt)
        self.invoke_kwargs.append(dict(_))
        self.callback_handlers.append(self.callback_handler)
        if not self._responses:
            raise AssertionError(
                "FakeStrandsAgent received more invocations than expected."
            )
        return FakeAgentResult(self._responses.pop(0))


class DelayedFakeStrandsAgent(FakeStrandsAgent):
    def __init__(self, delay_seconds: float, *responses: StructuredAgentTurnDecision):
        super().__init__(*responses)
        self.delay_seconds = delay_seconds

    async def invoke_async(self, prompt: str, **kwargs: object) -> FakeAgentResult:
        await asyncio.sleep(self.delay_seconds)
        return await super().invoke_async(prompt, **kwargs)


class ToolCallbackFakeStrandsAgent(FakeStrandsAgent):
    def __init__(self, tool_uses: list[dict], *responses: StructuredAgentTurnDecision):
        super().__init__(*responses)
        self.tool_uses = list(tool_uses)

    async def invoke_async(self, prompt: str, **kwargs: object) -> FakeAgentResult:
        callback_handler = self.callback_handler
        if callable(callback_handler):
            for tool_use in self.tool_uses:
                callback_handler(current_tool_use=tool_use)
        return await super().invoke_async(prompt, **kwargs)


class ActivatingPlanFakeStrandsAgent(FakeStrandsAgent):
    def __init__(
        self,
        activation_callback: Callable[[], dict],
        *responses: StructuredAgentTurnDecision,
    ):
        super().__init__(*responses)
        self._activation_callback = activation_callback
        self.activation_result: Optional[dict] = None

    async def invoke_async(self, prompt: str, **kwargs: object) -> FakeAgentResult:
        if self.activation_result is None:
            # Match Strands: synchronous tools execute on SDK worker threads.
            self.activation_result = await asyncio.to_thread(self._activation_callback)
        return await super().invoke_async(prompt, **kwargs)


def _load_live_env() -> None:
    global _LIVE_ENV_LOADED
    if _LIVE_ENV_LOADED:
        return
    env_path = Path(
        os.getenv("TEAM_NATS_LIVE_ENV", str(DEFAULT_LIVE_ENV_PATH))
    ).expanduser()
    if env_path.exists():
        if load_dotenv is not None:
            load_dotenv(env_path, override=True)
        else:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    _LIVE_ENV_LOADED = True


# The person live after-action report tests work for.
LIVE_AAR_PERSON = "usr_live_aar"


@pytest.fixture
def signed_person(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], GatewayContext]]:
    """A person's gateway context for a team, signed as the gateway signs requests.

    This process trusts the test keys and holds the scheduler key, as the
    manager process does, so after-action reports are recorded for the person
    and their review runs signed for them
    (``custom_agents.after_action_report_identity``). The live environment is
    loaded first so its values cannot replace the test keys.
    """

    _load_live_env()
    use_scheduler_key(monkeypatch)

    def for_team(team_id: str) -> GatewayContext:
        return GatewayContext(
            canonical_user_id=LIVE_AAR_PERSON,
            task_context=context_signer().issue(team_id=team_id, person_id=LIVE_AAR_PERSON, person_roles=("member",)),
        )

    yield for_team
    forget_task_context_keys()


def _live_config():
    global _LIVE_CONFIG
    if _LIVE_CONFIG is not None:
        return _LIVE_CONFIG
    _load_live_env()
    config_path = Path(
        os.getenv("TEAM_NATS_LIVE_CONFIG", str(DEFAULT_LIVE_CONFIG_PATH))
    ).expanduser()
    if config_path.exists():
        _LIVE_CONFIG = load_runtime_config_file(config_path)
    else:
        _LIVE_CONFIG = load_config(None, search_default_paths=False)
    return _LIVE_CONFIG


def _task_response(
    answer: str, *, aar: Optional[AfterActionReport] = None
) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.RESPONSE,
                    answer=answer,
                ),
                after_action_report=aar,
            )
        ],
    )


def _task_request(
    problem_statement: str,
    *,
    target_named_agent_type: str,
    target_agent_name: str | None = None,
) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=problem_statement,
                ),
                target_named_agent_type=target_named_agent_type,
                target_agent_name=target_agent_name,
            )
        ],
    )


def _task_query_request(
    message: str,
    *,
    choice_mode: str,
    choices: list[TaskQueryChoiceOption],
    allow_free_text: bool = True,
) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            TaskQueryAction(
                task_query=TaskQuery(
                    query_type=MessageMetaType.REQUEST,
                    message=message,
                    choice_mode=choice_mode,
                    choices=choices,
                    allow_free_text=allow_free_text,
                )
            )
        ]
    )


def _manager_escalation_request(
    problem_statement: str,
    *,
    escalation: ManagerEscalationContext,
) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=problem_statement,
                ),
                target_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                requester_type=RequesterType.AGENT_REQUESTER,
                manager_escalation=escalation,
            )
        ],
    )


def _unroutable_task_request(problem_statement: str) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            DelegateTaskAction(
                task=StructuredTaskPayload(
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=problem_statement,
                ),
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ],
    )


def _find_callback_instances(callback: object, cls: type) -> list[object]:
    found: list[object] = []
    seen: set[int] = set()

    def visit(value: object) -> None:
        value_id = id(value)
        if value_id in seen:
            return
        seen.add(value_id)
        if isinstance(value, cls):
            found.append(value)
            return
        if isinstance(value, (str, bytes, bytearray, int, float, bool, type(None))):
            return
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
            return
        if isinstance(value, (list, tuple, set)):
            for child in value:
                visit(child)
            return
        try:
            children = vars(value).values()
        except TypeError:
            return
        for child in children:
            visit(child)

    visit(callback)
    return found


def _agent(
    *,
    agent_id: str,
    name: str,
    team_id: str,
    named_agent_type: str,
    db_path: Path,
    workspace_dir: Path,
    is_lead: bool = False,
    debug: Optional[dict] = None,
    max_tool_calls_per_task: Optional[int] = None,
) -> UniversalAgent:
    storage_settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(db_path)}}
    )
    agent_kwargs = {
        "id": agent_id,
        "name": name,
        "named_agent_type": named_agent_type,
        "team_id": team_id,
        # Tests install the exact routing map explicitly below. Constructing
        # as a lead imports the full role registry and can trip unrelated tool
        # schema failures before the NATS boundary under test is reached. Tests
        # that need team-lead broker setup set `agent.is_team_lead` after
        # construction via the is_lead flag below.
        "is_team_lead": False,
        **_project_transport(transport_cfg=resolve_live_transport_settings().model_dump()),
        "task_manager": TaskManager(
            db_path=str(db_path),
            logger=logging.getLogger(f"TaskManager.{agent_id}"),
            storage_settings=storage_settings,
        ),
        "workspace_dir": str(workspace_dir),
        # Embedded agent: its working folder is its own instance root. Set it
        # explicitly so a TEAM_INSTANCE_ROOT left by an earlier test in this
        # process cannot move its runtime records.
        "instance_root": str(workspace_dir),
        "memory_graph_enabled": False,
        "activity_collector_base_url": None,
        "subtask_watchdog_interval_seconds": 3600,
        "model_provider": "openai",
        "model_id": "fake-live-test-model",
    }
    if debug is not None:
        agent_kwargs["debug"] = debug
    if max_tool_calls_per_task is not None:
        agent_kwargs["max_tool_calls_per_task"] = max_tool_calls_per_task

    agent = UniversalAgent(
        **agent_kwargs,
    )
    if is_lead:
        agent.is_team_lead = True
    return agent


_LIVE_NAMED_TYPES = (
    "TeamArchitect",
    "CodeWritingAgent",
    "WebResearchAgent",
    "PersonalAdminAgent",
    "ResearchComparisonAgent",
    "HomeErrandsAgent",
    "LearningCoachingAgent",
    "InternalImprovementAgent",
    "MemReviewAgent",
)


class LiveNatsQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self._messages: asyncio.Queue[MessageEnvelope] = asyncio.Queue()

    async def push(self, envelope: MessageEnvelope) -> None:
        await self._messages.put(envelope)

    async def get(
        self, timeout: float = 1.0, fail: bool = False
    ) -> MessageEnvelope | None:
        try:
            return await asyncio.wait_for(self._messages.get(), timeout=timeout)
        except asyncio.TimeoutError:
            if fail:
                raise
            return None


class LiveNatsChannel:
    def __init__(self, *, team_id: str) -> None:
        self.team_id = team_id
        config = nats_transport_config_from_env(name=f"live-boundary-{new_id_suffix()}")
        self.mapper = NatsSubjectMapper(
            team_id=team_id,
            subject_prefix=config.subject_prefix,
            named_agent_type_queue_map={
                named_type: f"{team_id}_{named_type}"
                for named_type in _LIVE_NAMED_TYPES
            },
        )
        self.adapter = NatsTransportAdapter(
            config=config,
            mapper=self.mapper,
            logger=logging.getLogger("LiveNatsChannel"),
        )
        self._connected = False

    async def connect(self) -> None:
        if not self._connected:
            await self.adapter.connect(reconcile_topology=True)
            self._connected = True

    async def close(self) -> None:
        await self.adapter.close()
        self._connected = False

    async def declare_queue(
        self,
        name: str,
        *,
        durable: bool = False,
        auto_delete: bool = False,
    ) -> LiveNatsQueue:
        await self.connect()
        queue = LiveNatsQueue(name)
        should_subscribe = (
            durable
            or auto_delete
            or "gateway" in name
            or name.endswith("_user_interact_queue")
        )
        if should_subscribe:

            async def handler(delivery) -> None:
                await queue.push(delivery.envelope)
                await delivery.ack()

            await self.adapter.subscribe_route(
                name=f"live-boundary:{name}",
                routing_key=name,
                handler=handler,
                durable_name=self.mapper.durable_name(new_id("live_boundary")),
            )
        return queue

    async def publish(self, routing_key: str, envelope: MessageEnvelope) -> None:
        await self.connect()
        await self.adapter.publish(
            envelope=envelope,
            routing_key=routing_key,
            options=TransportPublishOptions(durable=True),
        )


class LiveNatsConnection:
    def __init__(self, *, team_id: str) -> None:
        self._channel = LiveNatsChannel(team_id=team_id)

    async def channel(self) -> LiveNatsChannel:
        await self._channel.connect()
        return self._channel

    async def close(self) -> None:
        await self._channel.close()


async def _connect_nats(team_id: str) -> LiveNatsConnection:
    return LiveNatsConnection(team_id=team_id)


async def _start_agent(agent: UniversalAgent) -> asyncio.Task:
    task = asyncio.create_task(agent.connect())
    await asyncio.sleep(1.2)
    return task


async def _stop_agents(agent_tasks: list[tuple[UniversalAgent, asyncio.Task]]) -> None:
    for agent, _ in agent_tasks:
        with contextlib.suppress(Exception):
            await agent.exit_agent()
    for _, task in agent_tasks:
        if task.done():
            continue
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=6)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _publish_envelope(
    channel: LiveNatsChannel, routing_key: str, envelope: MessageEnvelope
) -> None:
    await channel.publish(routing_key, envelope)


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 12.0,
    interval: float = 0.1,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("Timed out waiting for live runtime condition.")


async def _wait_for_response(
    queue: LiveNatsQueue, *, timeout: float = 20.0
) -> MessageEnvelope:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await queue.get(timeout=1, fail=False)
        if message is not None:
            return message
        await asyncio.sleep(0.1)
    raise AssertionError(f"Timed out waiting for response on queue {queue.name}.")


async def test_live_manager_nats_request_uses_context_engine_accounting() -> None:
    team_id = new_id("team_live_ctx")
    manager_id = new_id("manager")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "context-engine.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(_task_response("context engine live ok"))

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("live_user"),
                    durable=False,
                    auto_delete=True,
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                task = Task(
                    task_id=new_id("context_task"),
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Reply with exactly context engine live ok.",
                )
                correlation_id = new_id("corr")
                envelope = MessageEnvelope(
                    msg_content=task,
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=manager.id,
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=correlation_id,
                        request_timezone="America/Chicago",
                    ),
                )

                await _publish_envelope(channel, manager.id, envelope)
                final_envelope = await _wait_for_response(user_queue)

                assert isinstance(final_envelope.msg_content, Task)
                assert final_envelope.msg_content.answer == "context engine live ok"
                assert manager_llm.invocations
                assert manager.turn_executor.last_context_assembly is not None

                assembly = manager.turn_executor.last_context_assembly
                section_names = [section.name for section in assembly.sections]
                assert "request_time_context" in section_names
                assert "request_conversation_context" in section_names
                assert "incoming_message" in section_names
                assert "provider_prompt_suffix" in section_names
                assert assembly.prompt == manager_llm.invocations[0]
                assert "Request Time Context:" in assembly.prompt
                assert "Request Conversation Context:" in assembly.prompt
                assert "Reply with exactly context engine live ok." in assembly.prompt
                assert (
                    assembly.max_context_tokens
                    == manager._resolved_context_window_tokens()
                )
                assert assembly.estimated_tokens > 0
                assert any(
                    item["name"] == "incoming_message"
                    for item in assembly.source_lengths
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_manager_wires_runtime_guardrails_and_context_debug_dump_plugins() -> (
    None
):
    team_id = new_id("team_plugins")
    manager_id = new_id("manager")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "plugins.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        tool_uses = [
            {
                "name": "searxng",
                "toolUseId": "tool_live_1",
                "input": {"operation": "search", "query": "strands plugins"},
            },
            {
                "name": "crawl4ai_md",
                "toolUseId": "tool_live_2",
                "input": {"url": "https://example.com"},
            },
        ]
        manager_llm = ToolCallbackFakeStrandsAgent(
            tool_uses,
            _task_response("runtime plugins live ok"),
        )
        context_dump_config = {
            "context_dump": {
                "enabled": True,
                "mode": "always",
                "output_dir": "artifacts/context_dumps",
                "include_strands_messages": True,
                "include_tool_results": False,
                "include_result": True,
                "include_reasoning_artifacts": True,
                "retention_days": 0,
            }
        }

        with (
            patch(
                "custom_agents.strands_agent.Agent", return_value=manager_llm
            ) as agent_factory,
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
                debug=context_dump_config,
                max_tool_calls_per_task=2,
            )

            agent_call_kwargs = agent_factory.call_args.kwargs
            guardrails_plugins = [
                hook
                for hook in agent_call_kwargs["hooks"]
                if isinstance(hook, RuntimeGuardrailsPlugin)
            ]
            assert guardrails_plugins == [manager._runtime_guardrails_plugin]
            assert guardrails_plugins[0].max_tool_calls == 2

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("live_user"),
                    durable=False,
                    auto_delete=True,
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                task = Task(
                    task_id=new_id("plugin_task"),
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Reply with exactly runtime plugins live ok.",
                )
                correlation_id = new_id("corr")
                envelope = MessageEnvelope(
                    msg_content=task,
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=manager.id,
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=correlation_id,
                        request_timezone="America/Chicago",
                    ),
                )

                await _publish_envelope(channel, manager.id, envelope)
                final_envelope = await _wait_for_response(user_queue)

                assert isinstance(final_envelope.msg_content, Task)
                assert final_envelope.msg_content.answer == "runtime plugins live ok"
                assert manager_llm.invoke_kwargs
                callback_handlers = _find_callback_instances(
                    manager_llm.callback_handlers[0],
                    ToolCallLimitCallbackHandler,
                )
                assert len(callback_handlers) == 1
                tool_limit_handler = callback_handlers[0]
                assert tool_limit_handler.max_tool_calls == 2
                assert tool_limit_handler.tool_sequence == ["searxng", "crawl4ai_md"]

                # An embedded agent's working folder is its own instance root.
                dump_root = (
                    InstanceLayout.at(workspace_dir).diagnostics_dir
                    / "context_dumps"
                    / task.task_id
                )
                await _wait_for(
                    lambda: (
                        dump_root.exists()
                        and any(
                            (path / "post_metadata.json").exists()
                            for path in dump_root.iterdir()
                        )
                    )
                )
                dump_dirs = [path for path in dump_root.iterdir() if path.is_dir()]
                assert len(dump_dirs) == 1
                dump_dir = dump_dirs[0]
                assert (dump_dir / "pre_metadata.json").exists()
                assert (dump_dir / "post_metadata.json").exists()
                assert (dump_dir / "prompt.md").exists()
                assert (dump_dir / "result.json").exists()
                assert (dump_dir / "parsed_content.json").exists()
                assert (dump_dir / "reasoning_artifacts.json").exists()

                pre_metadata = json.loads(
                    (dump_dir / "pre_metadata.json").read_text(encoding="utf-8")
                )
                post_metadata = json.loads(
                    (dump_dir / "post_metadata.json").read_text(encoding="utf-8")
                )
                parsed_content = json.loads(
                    (dump_dir / "parsed_content.json").read_text(encoding="utf-8")
                )
                prompt = (dump_dir / "prompt.md").read_text(encoding="utf-8")
                assert pre_metadata["phase"] == "pre"
                assert post_metadata["phase"] == "post"
                assert pre_metadata["task"]["task_id"] == task.task_id
                assert post_metadata["task"]["correlation_id"] == correlation_id
                assert pre_metadata["agent"]["agent_id"] == manager.id
                assert parsed_content["answer"] == "runtime plugins live ok"
                assert "Reply with exactly runtime plugins live ok." in prompt
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


def _archive_row(
    task_manager: TaskManager, task_id: str, owner_agent_id: str
) -> Optional[dict]:
    archived = task_manager.get_task_from_db(
        task_id,
        owner_agent_id=owner_agent_id,
    )
    if archived is None:
        return None
    archived["task"] = archived["main_task"]
    return archived


async def test_live_manager_task_query_choices_survive_nats_transport() -> None:
    team_id = new_id("team_q_choices")
    manager_id = new_id("manager")
    root_task_id = new_id("root_q_choices")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "query-choices.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(
            _task_query_request(
                "Which capability updates should be applied?",
                choice_mode="multiple",
                choices=[
                    TaskQueryChoiceOption(
                        option_id="image_true",
                        label="Set supports_image_input=true",
                        recommended=True,
                    ),
                    TaskQueryChoiceOption(
                        option_id="audio_false",
                        label="Set supports_audio_input=false",
                    ),
                ],
                allow_free_text=False,
            )
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_q_choices"),
                    durable=False,
                    auto_delete=True,
                )
                agent_tasks.append((manager, await _start_agent(manager)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Ask a structured capability approval question.",
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=root_task,
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{root_task_id}",
                            conversation_id=f"conversation_{root_task_id}",
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                query_envelope = await _wait_for_response(user_queue, timeout=20)

                assert isinstance(query_envelope.msg_content, TaskQuery)
                assert query_envelope.msg_content.query_type == MessageMetaType.REQUEST
                assert query_envelope.msg_content.parent_task_id == root_task_id
                assert query_envelope.msg_content.choice_mode == "multiple"
                assert query_envelope.msg_content.allow_free_text is False
                assert [
                    choice.option_id for choice in query_envelope.msg_content.choices
                ] == [
                    "image_true",
                    "audio_false",
                ]
                assert query_envelope.msg_content.choices[0].recommended is True
                assert (
                    query_envelope.msg_meta.msg_correlation_id == f"corr_{root_task_id}"
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_worker_manager_escalation_round_trips_through_team_architect(monkeypatch) -> (
    None
):
    team_id = new_id("team_mgr_esc")
    manager_id = new_id("manager")
    worker_id = new_id("internal")
    worker_task_id = new_id("worker_schedule")
    manager_route = f"{team_id}_{TEAM_ARCHITECT_AGENT_TYPE}"
    worker_route = f"{team_id}_InternalImprovementAgent"
    user_route = new_id("usr_mgr_esc")
    # Stand in for this team's configured gateway route. The worker's reply
    # route is deliberately different, so a question sent there cannot pass.
    monkeypatch.setattr("custom_agents.message_routing.person_chat_route", lambda *args, **kwargs: user_route)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "manager-escalation.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        escalation = ManagerEscalationContext(
            escalation_type="needs_user_input",
            source_task_id=worker_task_id,
            source_schedule_id="schedule_model_capability_probe",
            source_agent_id=worker_id,
            source_agent_name="internal",
            source_named_agent_type="InternalImprovementAgent",
            original_requester="worker_schedule",
            choice_mode="single",
            choices=[
                TaskQueryChoiceOption(
                    option_id="approve_image_true",
                    label="Approve supports_image_input=true",
                    recommended=True,
                )
            ],
            proposed_config_changes=[
                {
                    "path": "llm.overrides.fireworks/deepseek-v4-flash.supports_image_input",
                    "value": True,
                }
            ],
        )
        manager_llm = FakeStrandsAgent(
            _task_query_request(
                "Approve the capability update?", choice_mode="single", choices=escalation.choices,
            ),
            _task_response("MANAGER_GUIDANCE: user approved the capability update.")
        )
        worker_llm = FakeStrandsAgent(
            _manager_escalation_request(
                "ESCALATION_NEEDS_USER_APPROVAL: ask the user to approve the capability update.",
                escalation=escalation,
            ),
            _task_response(
                "WORKER_FINAL: applied manager-approved capability decision."
            ),
        )

        with (
            patch(
                "custom_agents.strands_agent.Agent",
                side_effect=[manager_llm, worker_llm],
            ),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {"InternalImprovementAgent": worker_route}
            )
            worker = _agent(
                agent_id=worker_id,
                name="internal",
                team_id=team_id,
                named_agent_type="InternalImprovementAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            worker.named_agent_type_queue_map.update(
                {TEAM_ARCHITECT_AGENT_TYPE: manager_route}
            )
            manager_received_envelopes: list[MessageEnvelope] = []
            original_manager_delivery = manager.on_nats_delivery

            async def recording_manager_delivery(delivery) -> None:
                manager_received_envelopes.append(delivery.envelope)
                await original_manager_delivery(delivery)

            object.__setattr__(manager, "on_nats_delivery", recording_manager_delivery)

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    user_route,
                    durable=False,
                    auto_delete=True,
                )

                for agent in (manager, worker):
                    agent_tasks.append((agent, await _start_agent(agent)))

                worker_task = Task(
                    task_id=worker_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Scheduled worker task: inspect unknown model capabilities.",
                )
                await _publish_envelope(
                    channel,
                    worker_route,
                    MessageEnvelope(
                        msg_content=worker_task,
                        msg_header=MessageHeader(
                            from_agent_id="live-test-scheduler",
                            to_routing_path=worker_route,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-scheduler",
                            requester_type=RequesterType.TIMER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{worker_task_id}",
                            conversation_id=f"conversation_{worker_task_id}",
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                await _wait_for(
                    lambda: any(
                        isinstance(envelope.msg_content, Task)
                        and envelope.msg_content.problem_statement
                        == "ESCALATION_NEEDS_USER_APPROVAL: ask the user to approve the capability update."
                        for envelope in manager_received_envelopes
                    ),
                    timeout=20,
                )
                escalation_envelope = next(
                    envelope
                    for envelope in manager_received_envelopes
                    if isinstance(envelope.msg_content, Task)
                    and envelope.msg_content.problem_statement
                    == "ESCALATION_NEEDS_USER_APPROVAL: ask the user to approve the capability update."
                )
                assert isinstance(escalation_envelope.msg_content, Task)
                assert escalation_envelope.msg_content.problem_statement == (
                    "ESCALATION_NEEDS_USER_APPROVAL: ask the user to approve the capability update."
                )
                assert escalation_envelope.msg_meta.manager_escalation is not None
                assert (
                    escalation_envelope.msg_meta.manager_escalation.escalation_type
                    == "needs_user_input"
                )
                assert (
                    escalation_envelope.msg_meta.manager_escalation.original_requester
                    == "worker_schedule"
                )
                assert (
                    escalation_envelope.msg_meta.manager_escalation.choices[0].option_id
                    == "approve_image_true"
                )

                question = await _wait_for_response(user_queue, timeout=20)
                assert isinstance(question.msg_content, TaskQuery)
                assert question.msg_content.message == "Approve the capability update?"
                assert question.msg_content.choices == escalation.choices
                for detail in ("needs_user_input", "approve_image_true", "supports_image_input"):
                    assert detail in manager_llm.invocations[0]
                await _publish_envelope(
                    channel, manager.id,
                    MessageEnvelope(
                        msg_content=TaskQuery(
                            task_query_id=question.msg_content.task_query_id,
                            parent_task_id=question.msg_content.parent_task_id,
                            query_type=MessageMetaType.RESPONSE, message="Approved.",
                            selected_choice_ids=["approve_image_true"],
                        ),
                        msg_header=MessageHeader(from_agent_id="live-test-user", to_routing_path=manager.id, response_routing_key=user_queue.name),
                        msg_meta=MessageMeta(requester_type=RequesterType.USER_REQUESTER, msg_correlation_id=question.msg_meta.msg_correlation_id, conversation_id=question.msg_meta.conversation_id),
                    ),
                )
                final_envelope = await _wait_for_response(user_queue, timeout=30)

                assert isinstance(final_envelope.msg_content, Task)
                assert final_envelope.msg_content.task_id == worker_task_id
                assert final_envelope.msg_content.task_type == MessageMetaType.RESPONSE
                assert (
                    final_envelope.msg_content.answer
                    == "WORKER_FINAL: applied manager-approved capability decision."
                )

                await _wait_for(
                    lambda: (
                        len(manager_llm.invocations) == 2
                        and len(worker_llm.invocations) == 2
                        and _archive_row(worker.task_manager, worker_task_id, worker_id)
                        is not None
                        # Guidance can reach the worker before the manager
                        # commits its own completed escalation task.
                        and _archive_row(
                            manager.task_manager,
                            escalation_envelope.msg_content.task_id,
                            manager_id,
                        ) is not None
                    ),
                    timeout=20,
                )
                escalation_archive = _archive_row(
                    manager.task_manager,
                    escalation_envelope.msg_content.task_id,
                    manager_id,
                )
                assert escalation_archive is not None
                assert escalation_archive["task"].problem_statement == (
                    "ESCALATION_NEEDS_USER_APPROVAL: ask the user to approve the capability update."
                )
                assert (
                    escalation_archive["task"].answer
                    == "MANAGER_GUIDANCE: user approved the capability update."
                )
                assert escalation_archive["task"].task_id != worker_task_id
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


class WorkingThenEscalatingFakeStrandsAgent(FakeStrandsAgent):
    """A worker model whose first turn edits a page (a tool call) before it escalates."""

    async def invoke_async(self, prompt: str, **kwargs: object) -> FakeAgentResult:
        if not self.invocations:
            self.messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": "edit_1",
                                    "name": "knowledge_write",
                                    "input": {"path": "team/home.md", "change": "link five personal pages"},
                                }
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"toolResult": {"toolUseId": "edit_1", "status": "success", "content": [{"text": "saved"}]}}],
                    },
                ]
            )
        return await super().invoke_async(prompt, **kwargs)


async def test_live_resumed_worker_turn_continues_after_the_lead_answers_its_escalation() -> None:
    """2026-10-19: a scheduled audit re-ran itself every time the lead answered its escalation.

    The worker's first turn edits a page and escalates; the lead answers. The
    worker's next turn must see that edit and the escalation it sent, and its
    request labelled as continuing, instead of a fresh "Current Request".
    """

    team_id = new_id("team_resume")
    manager_id = new_id("manager")
    worker_id = new_id("internal")
    worker_task_id = new_id("worker_schedule")
    manager_route = f"{team_id}_{TEAM_ARCHITECT_AGENT_TYPE}"
    worker_route = f"{team_id}_InternalImprovementAgent"
    request_text = "Scheduled worker task: audit the knowledge base and fix what is wrong."
    escalation_text = "AUDIT_FINDINGS: linked the home page to five pages; need the owner to confirm two IDs."

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "resume-work.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(_task_response("LEAD_REPLY: the owner confirmed both IDs."))
        worker_llm = WorkingThenEscalatingFakeStrandsAgent(
            _manager_escalation_request(
                escalation_text,
                escalation=ManagerEscalationContext(
                    escalation_type="needs_user_input",
                    source_task_id=worker_task_id,
                    original_requester="worker_schedule",
                ),
            ),
            _task_response("WORKER_FINAL: recorded the confirmed IDs."),
        )

        with (
            patch("custom_agents.strands_agent.Agent", side_effect=[manager_llm, worker_llm]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update({"InternalImprovementAgent": worker_route})
            worker = _agent(
                agent_id=worker_id,
                name="internal",
                team_id=team_id,
                named_agent_type="InternalImprovementAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            worker.named_agent_type_queue_map.update({TEAM_ARCHITECT_AGENT_TYPE: manager_route})

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                result_queue = await channel.declare_queue(new_id("usr_resume"), durable=False, auto_delete=True)
                for agent in (manager, worker):
                    agent_tasks.append((agent, await _start_agent(agent)))

                await _publish_envelope(
                    channel,
                    worker_route,
                    MessageEnvelope(
                        msg_content=Task(
                            task_id=worker_task_id,
                            task_type=MessageMetaType.REQUEST,
                            problem_statement=request_text,
                        ),
                        msg_header=MessageHeader(
                            from_agent_id="live-test-scheduler",
                            to_routing_path=worker_route,
                            response_routing_key=result_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-scheduler",
                            requester_type=RequesterType.TIMER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{worker_task_id}",
                            conversation_id=f"conversation_{worker_task_id}",
                        ),
                    ),
                )

                final_envelope = await _wait_for_response(result_queue, timeout=40)
                assert isinstance(final_envelope.msg_content, Task)
                assert final_envelope.msg_content.answer == "WORKER_FINAL: recorded the confirmed IDs."
                assert len(worker_llm.invocations) == 2

                first, resumed = worker_llm.invocations
                assert "Current Request:\n" + request_text in first
                assert "Work already done in this task" not in first
                assert "Work already done in this task" in resumed
                assert "called `knowledge_write` (success)" in resumed
                assert "team/home.md" in resumed
                assert escalation_text in resumed
                assert "Current Request (started earlier and continuing" in resumed
                assert "Current Request:\n" not in resumed
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_worker_report_finishes_the_workers_task_without_waiting_for_the_lead() -> None:
    """A worker_result report is its own task for the lead; the worker finishes in the same turn.

    Before 2026-10-19 the report was a subtask: the worker waited for the
    lead's acknowledgment, its answer was held back, and it took another turn.
    """

    team_id = new_id("team_report")
    manager_id = new_id("manager")
    worker_id = new_id("internal")
    worker_task_id = new_id("worker_schedule")
    manager_route = f"{team_id}_{TEAM_ARCHITECT_AGENT_TYPE}"
    worker_route = f"{team_id}_InternalImprovementAgent"
    report_text = "AUDIT_REPORT: linked the home page to five pages."

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "worker-report.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(_task_response("LEAD_NOTE: told the owner about the audit."))
        worker_llm = FakeStrandsAgent(
            StructuredAgentTurnDecision(
                actions=[
                    DelegateTaskAction(
                        task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement=report_text),
                        target_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                        requester_type=RequesterType.AGENT_REQUESTER,
                        manager_escalation=ManagerEscalationContext(escalation_type="worker_result"),
                    ),
                    TaskResponseAction(
                        task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer="WORKER_FINAL: audit done.")
                    ),
                ]
            )
        )

        with (
            patch("custom_agents.strands_agent.Agent", side_effect=[manager_llm, worker_llm]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update({"InternalImprovementAgent": worker_route})
            worker = _agent(
                agent_id=worker_id,
                name="internal",
                team_id=team_id,
                named_agent_type="InternalImprovementAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            worker.named_agent_type_queue_map.update({TEAM_ARCHITECT_AGENT_TYPE: manager_route})
            manager_received: list[MessageEnvelope] = []
            worker_received: list[MessageEnvelope] = []
            for agent, received in ((manager, manager_received), (worker, worker_received)):
                original_delivery = agent.on_nats_delivery

                async def recording_delivery(delivery, _original=original_delivery, _received=received) -> None:
                    _received.append(delivery.envelope)
                    await _original(delivery)

                object.__setattr__(agent, "on_nats_delivery", recording_delivery)

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                result_queue = await channel.declare_queue(new_id("usr_report"), durable=False, auto_delete=True)
                for agent in (manager, worker):
                    agent_tasks.append((agent, await _start_agent(agent)))

                await _publish_envelope(
                    channel,
                    worker_route,
                    MessageEnvelope(
                        msg_content=Task(
                            task_id=worker_task_id,
                            task_type=MessageMetaType.REQUEST,
                            problem_statement="Scheduled worker task: audit the knowledge base.",
                        ),
                        msg_header=MessageHeader(
                            from_agent_id="live-test-scheduler",
                            to_routing_path=worker_route,
                            response_routing_key=result_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-scheduler",
                            requester_type=RequesterType.TIMER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{worker_task_id}",
                            conversation_id=f"conversation_{worker_task_id}",
                        ),
                    ),
                )

                final_envelope = await _wait_for_response(result_queue, timeout=40)
                assert final_envelope.msg_content.answer == "WORKER_FINAL: audit done."
                await _wait_for(
                    lambda: len(manager_llm.invocations) == 1
                    and _archive_row(worker.task_manager, worker_task_id, worker_id) is not None,
                    timeout=30,
                )
                report = next(
                    envelope
                    for envelope in manager_received
                    if isinstance(envelope.msg_content, Task) and envelope.msg_content.problem_statement == report_text
                )
                assert report.msg_content.parent_task_id == worker_task_id
                assert report.msg_meta.is_response_requested is False
                assert report.msg_meta.manager_escalation.source_task_id == worker_task_id
                assert report_text in manager_llm.invocations[0]
                await _wait_for(
                    lambda: _archive_row(manager.task_manager, report.msg_content.task_id, manager_id) is not None,
                    timeout=20,
                )
                # One worker turn, and the lead's answer never went back to it.
                await asyncio.sleep(1.0)
                assert len(worker_llm.invocations) == 1
                assert not any(
                    isinstance(envelope.msg_content, Task)
                    and envelope.msg_content.task_id == report.msg_content.task_id
                    for envelope in worker_received
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_task_that_keeps_escalating_is_stopped_at_its_wait_limit(monkeypatch) -> None:
    """A worker that escalates on every resume is stopped before its limit + 1 wait goes out."""

    team_id = new_id("team_waitcap")
    manager_id = new_id("manager")
    worker_id = new_id("internal")
    worker_task_id = new_id("worker_schedule")
    manager_route = f"{team_id}_{TEAM_ARCHITECT_AGENT_TYPE}"
    worker_route = f"{team_id}_InternalImprovementAgent"
    limit = 2
    # A short limit keeps the test fast; the agent reads it at each turn.
    monkeypatch.setattr("custom_agents.strands_agent.MAX_WAITS_PER_TASK", limit)

    def escalate(pass_number: int) -> StructuredAgentTurnDecision:
        return _manager_escalation_request(
            f"AUDIT_PASS_{pass_number}: please confirm the findings.",
            escalation=ManagerEscalationContext(escalation_type="needs_user_input", source_task_id=worker_task_id),
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "wait-limit.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(*(_task_response(f"LEAD_REPLY_{index}") for index in range(limit)))
        worker_llm = FakeStrandsAgent(*(escalate(index) for index in range(limit + 1)))

        with (
            patch("custom_agents.strands_agent.Agent", side_effect=[manager_llm, worker_llm]),
            patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update({"InternalImprovementAgent": worker_route})
            worker = _agent(
                agent_id=worker_id,
                name="internal",
                team_id=team_id,
                named_agent_type="InternalImprovementAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            worker.named_agent_type_queue_map.update({TEAM_ARCHITECT_AGENT_TYPE: manager_route})

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                result_queue = await channel.declare_queue(new_id("usr_waitcap"), durable=False, auto_delete=True)
                for agent in (manager, worker):
                    agent_tasks.append((agent, await _start_agent(agent)))

                await _publish_envelope(
                    channel,
                    worker_route,
                    MessageEnvelope(
                        msg_content=Task(
                            task_id=worker_task_id,
                            task_type=MessageMetaType.REQUEST,
                            problem_statement="Scheduled worker task: audit the knowledge base.",
                        ),
                        msg_header=MessageHeader(
                            from_agent_id="live-test-scheduler",
                            to_routing_path=worker_route,
                            response_routing_key=result_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-scheduler",
                            requester_type=RequesterType.TIMER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{worker_task_id}",
                            conversation_id=f"conversation_{worker_task_id}",
                        ),
                    ),
                )

                failure = await _wait_for_response(result_queue, timeout=60)
                assert isinstance(failure.msg_content, Task)
                assert failure.msg_content.task_id == worker_task_id
                assert "was stopped because it had already waited 2 times" in (failure.msg_content.answer or "")
                # Two escalations reached the lead; the third was never sent.
                assert len(worker_llm.invocations) == limit + 1
                await asyncio.sleep(1.0)
                assert len(manager_llm.invocations) == limit
                assert worker.task_manager.get_task_info(worker_task_id) is None
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


def _register_manager_delegation(
    manager: UniversalAgent,
    *,
    parent_task_id: str,
    subtask_id: str,
    specialist_type: str,
    problem_statement: str,
    user_queue_name: str,
    gateway_context: Optional[GatewayContext] = None,
) -> MessageEnvelope:
    """Register a parent task and its delegated subtask; both carry ``gateway_context`` when given."""

    now = "2026-04-25T00:00:00+00:00"
    parent = Task(
        task_id=parent_task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement="Coordinate two specialists and synthesize the result.",
    )
    parent_header = MessageHeader(
        from_agent_id="live-test-user",
        to_routing_path=manager.id,
        response_routing_key=user_queue_name,
    )
    parent_meta = MessageMeta(
        from_agent_name="live-test-user",
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        msg_correlation_id=f"corr_{parent_task_id}",
        conversation_id=f"conversation_{parent_task_id}",
        request_timezone="America/Chicago",
        gateway_context=gateway_context,
    )
    if manager.task_manager.get_task_info(parent_task_id) is None:
        assert manager.task_manager.add_task(parent, parent_header, parent_meta, owner_person_id=None)

    subtask = Task(
        task_id=subtask_id,
        parent_task_id=parent_task_id,
        task_type=MessageMetaType.REQUEST,
        problem_statement=problem_statement,
    )
    subtask_header = MessageHeader(
        from_agent_id=manager.id,
        to_routing_path=f"{manager.team_id}_{specialist_type}",
        response_routing_key=manager.id,
    )
    subtask_meta = MessageMeta(
        from_agent_name=manager.name,
        to_named_agent_type=specialist_type,
        requester_type=RequesterType.AGENT_REQUESTER,
        is_response_requested=True,
        msg_correlation_id=f"corr_{parent_task_id}",
        conversation_id=f"conversation_{parent_task_id}",
        request_timezone="America/Chicago",
        # A delegated task carries its parent request's context unchanged.
        gateway_context=gateway_context.model_copy(deep=True) if gateway_context is not None else None,
    )
    assert manager.task_manager.add_subtask(
        parent_task_id, subtask, subtask_header, subtask_meta
    )
    action = manager.task_manager.record_plan_action(
        parent_task_id,
        PlanActionType.CHECKPOINT,
        f"Delegated {subtask_id} to {specialist_type}.",
        related_task_id=subtask_id,
        details={"specialist_type": specialist_type},
        agent_name=manager.name,
        agent_id=manager.id,
    )
    assert action is not None
    manager.task_manager.update_main_task_state(
        parent_task_id, ProgressState.WAITING, now
    )
    outbound_subtask = manager._task_request_transport_copy(subtask)
    assert "plan_id" not in outbound_subtask.model_dump()
    assert outbound_subtask.execution_history == []
    return MessageEnvelope(
        msg_content=outbound_subtask, msg_header=subtask_header, msg_meta=subtask_meta
    )


@pytest.mark.parametrize("lose_claim", [False, True])
async def test_live_manager_two_specialists_keep_state_separate_and_record_aar(
    signed_person: Callable[[str], GatewayContext],
    monkeypatch,
    lose_claim,
) -> None:
    if lose_claim:
        from custom_tools.comms_tools import TeamComms
        from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1

        publish = TeamComms.send_message

        async def publish_without_claim(self, message, *args, **kwargs):
            if isinstance(message.msg_content, DelegatedTaskClaimedV1):
                raise TimeoutError("Admission claim could not be published.")
            return await publish(self, message, *args, **kwargs)

        monkeypatch.setattr(TeamComms, "send_message", publish_without_claim)
    team_id = new_id("team_live_state")
    parent_task_id = new_id("parent")
    research_task_id = new_id("research")
    code_task_id = new_id("code")
    manager_id = new_id("manager")
    web_id = new_id("web")
    code_id = new_id("code_agent")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "state-boundaries.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        aar = AfterActionReport(
            report_id=new_id("aar"),
            summary="Research identified a reusable source-ranking constraint.",
            durable_context_candidates=[
                "Prefer official model/provider docs before benchmark roundups."
            ],
            risks_or_followups=["Benchmark pages can lag current model releases."],
            evidence_refs=[research_task_id],
            recommended_importance=TaskHistoryImportance.HIGH,
        )
        manager_llm = FakeStrandsAgent(
            _task_response("Final synthesis from both specialists.")
        )
        web_llm = FakeStrandsAgent(_task_response("Research complete.", aar=aar))
        code_llm = FakeStrandsAgent(_task_response("Implementation review complete."))

        with (
            patch(
                "custom_agents.strands_agent.Agent",
                side_effect=[manager_llm, web_llm, code_llm],
            ),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {
                    "WebResearchAgent": f"{team_id}_WebResearchAgent",
                    "CodeWritingAgent": f"{team_id}_CodeWritingAgent",
                }
            )
            web = _agent(
                agent_id=web_id,
                name="web",
                team_id=team_id,
                named_agent_type="WebResearchAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            code = _agent(
                agent_id=code_id,
                name="code",
                team_id=team_id,
                named_agent_type="CodeWritingAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("live_user"),
                    durable=False,
                    auto_delete=True,
                )

                for agent in (manager, web, code):
                    agent_tasks.append((agent, await _start_agent(agent)))

                person_context = signed_person(team_id)
                research_env = _register_manager_delegation(
                    manager,
                    parent_task_id=parent_task_id,
                    subtask_id=research_task_id,
                    specialist_type="WebResearchAgent",
                    problem_statement="Research current model context.",
                    user_queue_name=user_queue.name,
                    gateway_context=person_context,
                )
                code_env = _register_manager_delegation(
                    manager,
                    parent_task_id=parent_task_id,
                    subtask_id=code_task_id,
                    specialist_type="CodeWritingAgent",
                    problem_statement="Check implementation implications.",
                    user_queue_name=user_queue.name,
                    gateway_context=person_context,
                )

                await _publish_envelope(
                    channel, f"{team_id}_WebResearchAgent", research_env
                )
                await _publish_envelope(
                    channel, f"{team_id}_CodeWritingAgent", code_env
                )

                final_envelope = await _wait_for_response(user_queue)
                assert isinstance(final_envelope.msg_content, Task)
                assert final_envelope.msg_content.task_id == parent_task_id
                assert final_envelope.msg_content.task_type == MessageMetaType.RESPONSE
                assert (
                    final_envelope.msg_content.answer
                    == "Final synthesis from both specialists."
                )

                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, parent_task_id, manager_id)
                        is not None
                        and len(
                            manager.task_manager.list_after_action_report_reviews(
                                person_id=LIVE_AAR_PERSON
                            )
                        )
                        == 1
                    )
                )

                manager_parent = _archive_row(
                    manager.task_manager, parent_task_id, manager_id
                )
                web_archive = _archive_row(web.task_manager, research_task_id, web_id)
                code_archive = _archive_row(code.task_manager, code_task_id, code_id)

                assert manager_parent is not None
                assert web_archive is not None
                assert code_archive is not None
                manager_plan_id = manager.task_manager.get_task_plan_id(parent_task_id)
                assert manager_plan_id
                assert "plan_id" not in manager_parent["task"].model_dump()
                manager_plan = manager.task_manager.get_plan(manager_plan_id)
                assert manager_plan is not None
                assert manager_plan.status == PlanStatus.COMPLETED
                assert {parent_task_id, research_task_id, code_task_id}.issubset(
                    set(manager_plan.related_task_ids)
                )
                assert {
                    action.related_task_id
                    for action in manager_plan.plan_actions
                    if action.action_type == PlanActionType.CHECKPOINT
                }.issuperset({research_task_id, code_task_id})
                assert "plan_id" not in web_archive["task"].model_dump()
                assert "plan_id" not in code_archive["task"].model_dump()
                assert web.task_manager.get_task_plan_id(research_task_id) is None
                assert code.task_manager.get_task_plan_id(code_task_id) is None
                web_plan = web.task_manager.get_plan(manager_plan_id)
                code_plan = code.task_manager.get_plan(manager_plan_id)
                assert web_plan is None
                assert code_plan is None
                assert (
                    _archive_row(manager.task_manager, research_task_id, manager_id)
                    is None
                )
                assert (
                    _archive_row(manager.task_manager, code_task_id, manager_id) is None
                )
                assert _archive_row(web.task_manager, code_task_id, web_id) is None
                assert (
                    _archive_row(code.task_manager, research_task_id, code_id) is None
                )

                aar_entries = [
                    entry
                    for entry in manager_parent["task"].execution_history
                    if entry.entry_type == TaskHistoryType.AGENT_NOTE
                    and (entry.details or {}).get("report_id") == aar.report_id
                ]
                pending_aar_reviews = (
                    manager.task_manager.list_after_action_report_reviews(
                        person_id=LIVE_AAR_PERSON
                    )
                )
                assert len(pending_aar_reviews) == 1
                # The report was recorded for the person the parent request works for.
                assert pending_aar_reviews[0].person_id == LIVE_AAR_PERSON
                assert pending_aar_reviews[0].identity_verified
                assert pending_aar_reviews[0].report_id == aar.report_id
                assert pending_aar_reviews[0].source_task_id == research_task_id
                assert (
                    pending_aar_reviews[0].source_named_agent_type == "WebResearchAgent"
                )
                assert pending_aar_reviews[0].batch_id
                # Review admission and schedule installation are separate awaited
                # commits. Wait for the durable schedule, not just its review row.
                await _wait_for(
                    lambda: any(
                        row["schedule_name"]
                        == f"after_action_report_batch_review:{pending_aar_reviews[0].batch_id}"
                        for row in manager._schedule_store.get_all()
                    )
                )
                schedules = manager._schedule_store.get_all()
                assert any(
                    row["schedule_name"]
                    == f"after_action_report_batch_review:{pending_aar_reviews[0].batch_id}"
                    for row in schedules
                )
                if aar_entries:
                    assert len(aar_entries) == 1
                    assert aar_entries[0].details["source_task_id"] == research_task_id
                    assert (
                        aar_entries[0].details["source_named_agent_type"]
                        == "WebResearchAgent"
                    )
                    assert (
                        aar_entries[0].details["review_state"]
                        == "pending_manager_review"
                    )
                else:
                    assert pending_aar_reviews[0].batch_id
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_late_aar_schedules_hydrated_manager_review_task(
    signed_person: Callable[[str], GatewayContext],
) -> None:
    team_id = new_id("team_aar_rev")
    parent_task_id = new_id("parent_late_aar")
    manager_id = new_id("manager")
    report_id = new_id("aar_late")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "late-aar-review.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(_task_response("Reviewed late AAR packet."))

        with (
            patch("custom_agents.strands_agent.Agent", side_effect=[manager_llm]),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            parent = Task(
                task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Completed parent task that should receive late AAR review.",
            )
            person_context = signed_person(team_id)
            assert manager.task_manager.add_task(
                parent,
                MessageHeader(
                    from_agent_id="live-test-user",
                    response_routing_key="live-test-user",
                ),
                MessageMeta(
                    from_agent_name="live-test-user",
                    requester_type=RequesterType.USER_REQUESTER,
                    gateway_context=person_context,
                ),
                owner_person_id=LIVE_AAR_PERSON,
            )
            manager.task_manager.update_main_task_state(
                parent_task_id,
                ProgressState.IN_PROGRESS,
                "2026-04-25T00:00:00+00:00",
            )
            manager.task_manager.complete_task(
                parent_task_id, "2026-04-25T00:01:00+00:00"
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                agent_tasks.append((manager, await _start_agent(manager)))

                past_timestamp = (
                    datetime.now(timezone.utc) - timedelta(seconds=40)
                ).isoformat()
                report = AfterActionReport(
                    report_id=report_id,
                    source_task_id=new_id("late_child"),
                    parent_task_id=parent_task_id,
                    source_agent_id="late_specialist",
                    source_agent_name="late-specialist",
                    source_named_agent_type="WebResearchAgent",
                    summary="Late AAR should be injected into a scheduled review task.",
                    durable_context_candidates=[
                        "Use the injected report id to resolve the review."
                    ],
                    risks_or_followups=[
                        "Do not require a listing call before resolution."
                    ],
                    evidence_refs=["late-evidence-ref"],
                    recommended_importance=TaskHistoryImportance.HIGH,
                )
                await _publish_envelope(
                    channel,
                    manager_id,
                    MessageEnvelope(
                        msg_content=report,
                        msg_header=MessageHeader(
                            from_agent_id="late_specialist",
                            to_routing_path=manager_id,
                            timestamp=past_timestamp,
                            agent_signature="",
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="late-specialist",
                            requester_type=RequesterType.AGENT_REQUESTER,
                            is_response_requested=False,
                            # The specialist's task carried the parent request's context.
                            gateway_context=person_context.model_copy(deep=True),
                        ),
                    ),
                )

                await _wait_for(
                    lambda: any(
                        f"report_id: {report_id}" in prompt
                        and "Use resolve_after_action_report_review with the report_id shown above"
                        in prompt
                        for prompt in manager_llm.invocations
                    ),
                    timeout=16,
                )
                pending = manager.task_manager.list_after_action_report_reviews(
                    person_id=LIVE_AAR_PERSON
                )
                assert len(pending) == 1
                assert pending[0].report_id == report_id
                assert pending[0].batch_id
                batch = manager.task_manager.after_action_report_repository.get_batch(
                    pending[0].batch_id, owner_agent_id=manager_id
                )
                assert batch is not None and batch.scheduled_review_task_id
                # A model invocation is only the start of the turn. Keep the
                # database alive through result application and the final
                # admission drain, then verify the scheduled task completed.
                await _wait_for(
                    lambda: not manager.is_executing
                    and (manager._pending_work_task is None or manager._pending_work_task.done())
                )
                archived = _archive_row(
                    manager.task_manager, batch.scheduled_review_task_id, manager_id
                )
                assert archived is not None
                assert archived["status"] == ProgressState.COMPLETED
                # The review ran for the person whose work the report describes.
                assert batch.person_id == LIVE_AAR_PERSON
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_named_queue_task_survives_specialist_restart_gap_and_completes() -> (
    None
):
    team_id = new_id("team_restart")
    direct_task_id = new_id("restart_task")
    manager_id = new_id("manager")
    web_id = new_id("web")
    first_code_id = new_id("code_old")
    replacement_code_id = new_id("code_new")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "restart-gap.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        idle_manager_llm = FakeStrandsAgent()
        idle_web_llm = FakeStrandsAgent()
        idle_first_code_llm = FakeStrandsAgent()
        replacement_code_llm = FakeStrandsAgent(
            _task_response("Replacement specialist completed queued work.")
        )

        with (
            patch(
                "custom_agents.strands_agent.Agent",
                side_effect=[
                    idle_manager_llm,
                    idle_web_llm,
                    idle_first_code_llm,
                    replacement_code_llm,
                ],
            ),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {
                    "WebResearchAgent": f"{team_id}_WebResearchAgent",
                    "CodeWritingAgent": f"{team_id}_CodeWritingAgent",
                }
            )
            web = _agent(
                agent_id=web_id,
                name="web",
                team_id=team_id,
                named_agent_type="WebResearchAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )
            first_code = _agent(
                agent_id=first_code_id,
                name="code-old",
                team_id=team_id,
                named_agent_type="CodeWritingAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_restart"),
                    durable=False,
                    auto_delete=True,
                )

                for agent in (manager, web, first_code):
                    agent_tasks.append((agent, await _start_agent(agent)))

                await _stop_agents([(first_code, agent_tasks.pop()[1])])

                task = Task(
                    task_id=direct_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Complete this task after a same-role restart gap.",
                )
                envelope = MessageEnvelope(
                    msg_content=task,
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=f"{team_id}_CodeWritingAgent",
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        to_named_agent_type="CodeWritingAgent",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=f"corr_{direct_task_id}",
                        conversation_id=f"conversation_{direct_task_id}",
                        request_timezone="America/Chicago",
                    ),
                )
                await _publish_envelope(
                    channel, f"{team_id}_CodeWritingAgent", envelope
                )

                replacement_code = _agent(
                    agent_id=replacement_code_id,
                    name="code-new",
                    team_id=team_id,
                    named_agent_type="CodeWritingAgent",
                    db_path=db_path,
                    workspace_dir=workspace_dir,
                )
                agent_tasks.append(
                    (replacement_code, await _start_agent(replacement_code))
                )

                response = await _wait_for_response(user_queue)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == direct_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert (
                    response.msg_content.answer
                    == "Replacement specialist completed queued work."
                )

                await _wait_for(
                    lambda: (
                        _archive_row(
                            replacement_code.task_manager,
                            direct_task_id,
                            replacement_code_id,
                        )
                        is not None
                    )
                )
                assert (
                    _archive_row(first_code.task_manager, direct_task_id, first_code_id)
                    is None
                )
                replacement_archive = _archive_row(
                    replacement_code.task_manager,
                    direct_task_id,
                    replacement_code_id,
                )
                assert replacement_archive is not None
                assert (
                    replacement_archive["task"].answer
                    == "Replacement specialist completed queued work."
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_manager_isolated_recovery_uses_fresh_context_and_plan_link() -> (
    None
):
    team_id = new_id("team_recovery")
    original_task_id = new_id("original")
    manager_id = new_id("manager")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "isolated-recovery.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(
            _task_response(
                "PHASE3_RECOVERY_RESULT: split the retry into smaller verification tasks."
            ),
            _task_response(
                "Original task resumed and completed using the recovery result."
            ),
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_recovery"),
                    durable=False,
                    auto_delete=True,
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                original_problem = "Original long-running task with many prior details that should stay out of recovery."
                original_task = Task(
                    task_id=original_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=original_problem,
                )
                assert manager.task_manager.add_task(
                    original_task,
                    MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=manager.id,
                        response_routing_key=user_queue.name,
                    ),
                    MessageMeta(
                        from_agent_name="live-test-user",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=f"corr_{original_task_id}",
                        conversation_id=f"conversation_{original_task_id}",
                        request_timezone="America/Chicago",
                    ),
                    owner_person_id=None,
                )
                assert (
                    manager.task_manager.get_next_task_to_process() == original_task_id
                )
                assert manager.task_manager.update_main_task_state(
                    original_task_id,
                    ProgressState.IN_PROGRESS,
                    "2026-04-28T12:00:00+00:00",
                )
                manager.task_manager.set_task_plan(
                    original_task_id,
                    "Recover from context pressure and continue with a smaller verification path.",
                    goal_summary="Complete the long-running task after isolating recovery.",
                    current_focus="Recovery from critical context pressure.",
                )
                manager.task_manager.append_history_entry(
                    original_task_id,
                    "The manager detected critical_context_pressure while preparing recovery.",
                    entry_type=TaskHistoryType.ERROR_EVENT,
                    importance=TaskHistoryImportance.CRITICAL,
                    details={
                        "task_id": original_task_id,
                        "conversation_id": f"conversation_{original_task_id}",
                        "error": "critical_context_pressure",
                    },
                )

                plan_id = manager.task_manager.get_task_plan_id(original_task_id)
                assert plan_id is not None
                set_agent_ref(manager)
                tool_result = json.loads(
                    await asyncio.to_thread(
                        start_isolated_recovery,
                        failure_type="critical_context_pressure",
                        failure_summary="The original task context is too large for reliable troubleshooting.",
                        recovery_brief="PHASE3_RECOVERY_BRIEF: inspect the failure and return a compact recovery path.",
                        affected_step_id="plan_step_recover",
                        notify_user=True,
                    )
                )
                assert tool_result["recovery_started"] is True
                assert tool_result["metadata_hidden"] is True
                assert tool_result["recovery_task_payload_has_plan_id"] is False
                assert "recovery_task_id" not in tool_result
                recovery_links = [
                    link
                    for link in manager.task_manager.plan_state.recovery_links.values()
                    if link.original_task_id == original_task_id
                ]
                assert len(recovery_links) == 1
                recovery_task_id = recovery_links[0].recovery_task_id
                assert recovery_links[0].original_plan_id == plan_id

                assert manager.task_manager.update_main_task_state(
                    original_task_id,
                    ProgressState.WAITING,
                    "2026-04-28T12:00:05+00:00",
                )

                schedule_pending_work(manager)

                response = await _wait_for_response(user_queue)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == original_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert (
                    response.msg_content.answer
                    == "Original task resumed and completed using the recovery result."
                )

                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, recovery_task_id, manager_id)
                        is not None
                    )
                )
                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, original_task_id, manager_id)
                        is not None
                    )
                )
                recovery_archive = _archive_row(
                    manager.task_manager, recovery_task_id, manager_id
                )
                assert recovery_archive is not None
                assert "plan_id" not in recovery_archive["task"].model_dump()
                assert any(
                    entry.plan_id == plan_id
                    for entry in recovery_archive["task"].execution_history
                )
                original_archive = _archive_row(
                    manager.task_manager, original_task_id, manager_id
                )
                assert original_archive is not None
                assert any(
                    "PHASE3_RECOVERY_RESULT" in entry.summary
                    for entry in original_archive["task"].execution_history
                )

                manager_plan = manager.task_manager.get_plan(plan_id)
                assert manager_plan is not None
                assert original_task_id in manager_plan.related_task_ids
                assert recovery_task_id in manager_plan.related_task_ids
                assert recovery_task_id in manager_plan.recovery_task_ids
                assert any(
                    action.action_type == PlanActionType.START_ISOLATED_RECOVERY
                    and action.recovery_task_id == recovery_task_id
                    for action in manager_plan.plan_actions
                )
                assert any(
                    action.action_type == PlanActionType.RECOVERY_RESULT_READY
                    and action.recovery_task_id == recovery_task_id
                    for action in manager_plan.plan_actions
                )
                assert any(
                    action.action_type == PlanActionType.RESUME_TASK
                    and action.recovery_task_id == recovery_task_id
                    for action in manager_plan.plan_actions
                )

                assert len(manager_llm.invocations) == 2
                recovery_prompt = manager_llm.invocations[0]
                assert "PHASE3_RECOVERY_BRIEF" in recovery_prompt
                assert "Initial sanitized recovery context" in recovery_prompt
                assert "critical_context_pressure" in recovery_prompt
                assert (
                    "Complete the long-running task after isolating recovery"
                    in recovery_prompt
                )
                assert original_problem not in recovery_prompt
                assert original_task_id not in recovery_prompt
                assert recovery_task_id not in recovery_prompt
                assert f"conversation_{original_task_id}" not in recovery_prompt
                resumed_prompt = manager_llm.invocations[1]
                assert "PHASE3_RECOVERY_RESULT" in resumed_prompt
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_manager_plan_step_task_query_routes_to_root_requester() -> None:
    team_id = new_id("team_plan_query")
    root_task_id = new_id("root_plan_query")
    manager_id = new_id("manager")
    conversation_id = f"conversation_{root_task_id}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "plan-step-query.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_holder: dict[str, UniversalAgent] = {}
        query_text = "Refresh the project credential, then reply done."
        step_answer = "PLAN_QUERY_STEP_COMPLETE: credential confirmed."
        final_answer = "PLAN_QUERY_FINAL: project work resumed."

        def _activate_plan() -> dict:
            manager = manager_holder["manager"]
            set_agent_ref(manager)
            result = json.loads(
                activate_plan_execution(
                    json.dumps(
                        [
                            {
                                "title": "Create project",
                                "objective": "Create the requested project after resolving any blocker.",
                            }
                        ]
                    ),
                    final_review=False,
                )
            )
            assert result["plan_execution_started"] is True
            return result

        manager_llm = ActivatingPlanFakeStrandsAgent(
            _activate_plan,
            _task_response("PLAN_QUERY_EARLY_RESPONSE_SHOULD_BE_SUPPRESSED"),
            StructuredAgentTurnDecision(
                actions=[
                    TaskQueryAction(
                        task_query=TaskQuery(
                            query_type=MessageMetaType.REQUEST,
                            message=query_text,
                        )
                    )
                ]
            ),
            _task_response(step_answer),
            _task_response(final_answer),
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager_holder["manager"] = manager

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_plan_query"),
                    durable=False,
                    auto_delete=True,
                )
                agent_tasks.append((manager, await _start_agent(manager)))

                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=Task(
                            task_id=root_task_id,
                            task_type=MessageMetaType.REQUEST,
                            problem_statement=(
                                "Use runtime plan-step execution and ask for credential "
                                "confirmation before completing the step."
                            ),
                        ),
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{root_task_id}",
                            conversation_id=conversation_id,
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                query_envelope = await _wait_for_response(user_queue, timeout=20)
                query = query_envelope.msg_content
                assert isinstance(query, TaskQuery)
                assert query.query_type == MessageMetaType.REQUEST
                assert query.message == query_text
                assert query.parent_task_id is not None
                assert query.parent_task_id.startswith("plan_step_task_")
                assert query_envelope.msg_header.to_routing_path == user_queue.name
                assert query_envelope.msg_meta.conversation_id != conversation_id

                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=TaskQuery(
                            task_query_id=query.task_query_id,
                            query_type=MessageMetaType.RESPONSE,
                            parent_task_id=query.parent_task_id,
                            message="done",
                        ),
                        msg_header=MessageHeader(
                            from_agent_id=user_queue.name,
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=False,
                            msg_correlation_id=query_envelope.msg_meta.msg_correlation_id,
                            conversation_id=query_envelope.msg_meta.conversation_id,
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                final_envelope = await _wait_for_response(user_queue, timeout=24)
                assert isinstance(final_envelope.msg_content, Task)
                assert final_envelope.msg_content.task_id == root_task_id
                assert final_envelope.msg_content.task_type == MessageMetaType.RESPONSE
                assert final_envelope.msg_content.answer == final_answer

                await _wait_for(
                    lambda: (
                        _archive_row(
                            manager.task_manager,
                            root_task_id,
                            manager_id,
                        )
                        is not None
                    )
                )
                plan_id = manager.task_manager.get_task_plan_id(root_task_id)
                assert plan_id is not None
                plan = manager.task_manager.get_plan(plan_id)
                assert plan is not None
                assert plan.status == PlanStatus.COMPLETED
                assert plan.plan_steps[0].status.value == "completed"
                assert plan.plan_steps[0].result_summary == step_answer
                assert len(manager_llm.invocations) == 4
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_manager_plan_outer_loop_executes_internal_steps_and_resumes_root() -> (
    None
):
    team_id = new_id("team_plan_loop")
    root_task_id = new_id("root_plan")
    manager_id = new_id("manager")
    conversation_id = f"conversation_{root_task_id}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "plan-outer-loop.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_holder: dict[str, UniversalAgent] = {}

        def _activate_plan() -> dict:
            manager = manager_holder["manager"]
            set_agent_ref(manager)
            result = json.loads(
                activate_plan_execution(
                    json.dumps(
                        [
                            {
                                "title": "Inspect runtime path",
                                "objective": "Inspect the current plan execution path and summarize the relevant result.",
                                "step_id": "model_supplied_step_id_must_not_survive",
                            },
                            {
                                "title": "Verify bounded continuation",
                                "objective": "Verify the next step can run from compact plan state without replaying root internals.",
                            },
                        ]
                    ),
                    final_review=False,
                )
            )
            assert result["plan_execution_started"] is True
            assert result["metadata_hidden"] is True
            assert result["ids_available_to_model"] is False
            assert "plan_id" not in result
            assert "task_id" not in result
            return result

        manager_llm = ActivatingPlanFakeStrandsAgent(
            _activate_plan,
            _task_response("PLAN_LOOP_EARLY_RESPONSE_SHOULD_BE_SUPPRESSED"),
            _task_response("PLAN_STEP_ONE_RESULT: inspected runtime path."),
            _task_response("PLAN_STEP_TWO_RESULT: verified bounded continuation."),
            _task_response(
                "PLAN_OUTER_LOOP_FINAL: all plan steps completed and root response resumed."
            ),
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager_holder["manager"] = manager

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_plan_loop"),
                    durable=False,
                    auto_delete=True,
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=(
                        "Use runtime plan-step execution for this live boundary test. "
                        "The final answer must come only after both internal plan steps finish."
                    ),
                )
                envelope = MessageEnvelope(
                    msg_content=root_task,
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=manager.id,
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=f"corr_{root_task_id}",
                        conversation_id=conversation_id,
                        request_timezone="America/Chicago",
                    ),
                )
                await _publish_envelope(channel, manager.id, envelope)

                response = await _wait_for_response(user_queue, timeout=24)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == root_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert response.msg_content.answer == (
                    "PLAN_OUTER_LOOP_FINAL: all plan steps completed and root response resumed."
                )
                assert (
                    response.msg_content.answer
                    != "PLAN_LOOP_EARLY_RESPONSE_SHOULD_BE_SUPPRESSED"
                )

                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, root_task_id, manager_id)
                        is not None
                    )
                )

                root_archive = _archive_row(
                    manager.task_manager, root_task_id, manager_id
                )
                assert root_archive is not None
                assert "plan_id" not in root_archive["task"].model_dump()
                plan_id = manager.task_manager.get_task_plan_id(root_task_id)
                assert plan_id is not None
                plan = manager.task_manager.get_plan(plan_id)
                assert plan is not None
                assert plan.status == PlanStatus.COMPLETED
                assert [step.title for step in plan.plan_steps] == [
                    "Inspect runtime path",
                    "Verify bounded continuation",
                ]
                assert [step.status.value for step in plan.plan_steps] == [
                    "completed",
                    "completed",
                ]
                assert (
                    plan.plan_steps[0].result_summary
                    == "PLAN_STEP_ONE_RESULT: inspected runtime path."
                )
                assert (
                    plan.plan_steps[1].result_summary
                    == "PLAN_STEP_TWO_RESULT: verified bounded continuation."
                )
                assert all(
                    step.step_id.startswith("plan_step_") for step in plan.plan_steps
                )
                assert "model_supplied_step_id_must_not_survive" not in {
                    step.step_id for step in plan.plan_steps
                }

                step_task_ids = [
                    step_task_id
                    for step in plan.plan_steps
                    for step_task_id in step.step_task_ids
                ]
                assert len(step_task_ids) == 2
                assert {root_task_id, *step_task_ids}.issubset(
                    set(plan.related_task_ids)
                )
                for step_task_id in step_task_ids:
                    step_archive = _archive_row(
                        manager.task_manager, step_task_id, manager_id
                    )
                    assert step_archive is not None
                    assert "plan_id" not in step_archive["task"].model_dump()
                    assert any(
                        entry.plan_id == plan_id
                        for entry in step_archive["task"].execution_history
                    )

                action_types = [action.action_type for action in plan.plan_actions]
                assert PlanActionType.START_PLAN_EXECUTION in action_types
                assert action_types.count(PlanActionType.PLAN_STEP_STARTED) == 2
                assert action_types.count(PlanActionType.PLAN_STEP_COMPLETED) == 2
                assert PlanActionType.RESUME_TASK in action_types
                user_notice_summaries = [
                    action.summary
                    for action in plan.plan_actions
                    if action.action_type == PlanActionType.USER_NOTICE
                    and action.user_visible
                ]
                assert any(
                    "Created a runtime plan with 2 steps" in summary
                    for summary in user_notice_summaries
                )
                assert any(
                    "Plan milestone completed: Inspect runtime path" in summary
                    for summary in user_notice_summaries
                )
                assert any(
                    "All runtime plan milestones are complete" in summary
                    for summary in user_notice_summaries
                )

                history_summaries = [
                    entry.summary for entry in root_archive["task"].execution_history
                ]
                assert any(
                    "suppressing immediate root response" in summary
                    for summary in history_summaries
                )
                assert any(
                    "PLAN_STEP_ONE_RESULT" in summary for summary in history_summaries
                )
                assert any(
                    "PLAN_STEP_TWO_RESULT" in summary for summary in history_summaries
                )

                assert len(manager_llm.invocations) == 4
                first_step_prompt = manager_llm.invocations[1]
                second_step_prompt = manager_llm.invocations[2]
                final_root_prompt = manager_llm.invocations[3]
                assert "Internal manager plan-step task" in first_step_prompt
                assert "Internal manager plan-step task" in second_step_prompt
                assert "PLAN_STEP_ONE_RESULT" in second_step_prompt
                assert "PLAN_STEP_ONE_RESULT" in final_root_prompt
                assert "PLAN_STEP_TWO_RESULT" in final_root_prompt
                for prompt in (first_step_prompt, second_step_prompt):
                    assert root_task_id not in prompt
                    assert conversation_id not in prompt
                    assert "plan_step_task_" not in prompt
                    assert "model_supplied_step_id_must_not_survive" not in prompt
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_manager_final_review_loop_suppresses_unresolved_plan_answer_once() -> (
    None
):
    team_id = new_id("team_final_rev")
    root_task_id = new_id("root_final_rev")
    manager_id = new_id("manager")
    conversation_id = f"conversation_{root_task_id}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "final-review-loop.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_holder: dict[str, UniversalAgent] = {}

        def _record_unresolved_plan_item() -> dict:
            manager = manager_holder["manager"]
            set_agent_ref(manager)
            result = json.loads(
                update_execution_plan_step(
                    title="Validate unresolved evidence",
                    rationale="The manager must explicitly review this before completing.",
                    status="planned",
                    status_detail="Still open at the first attempted final answer.",
                )
            )
            assert "error" not in result
            return result

        manager_llm = ActivatingPlanFakeStrandsAgent(
            _record_unresolved_plan_item,
            _task_response("FINAL_REVIEW_EARLY_ANSWER_SHOULD_BE_SUPPRESSED"),
            _task_response(
                "FINAL_REVIEWED_WITH_CAVEATS: unresolved evidence was reviewed and caveated."
            ),
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager_holder["manager"] = manager

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_final_rev"),
                    durable=False,
                    auto_delete=True,
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=(
                        "Attempt to complete with an unresolved plan item so the runtime "
                        "must force one bounded final review turn."
                    ),
                )
                envelope = MessageEnvelope(
                    msg_content=root_task,
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=manager.id,
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=f"corr_{root_task_id}",
                        conversation_id=conversation_id,
                        request_timezone="America/Chicago",
                    ),
                )
                await _publish_envelope(channel, manager.id, envelope)

                response = await _wait_for_response(user_queue, timeout=24)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == root_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert response.msg_content.answer == (
                    "FINAL_REVIEWED_WITH_CAVEATS: unresolved evidence was reviewed and caveated."
                )
                assert (
                    response.msg_content.answer
                    != "FINAL_REVIEW_EARLY_ANSWER_SHOULD_BE_SUPPRESSED"
                )

                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, root_task_id, manager_id)
                        is not None
                    )
                )

                root_archive = _archive_row(
                    manager.task_manager, root_task_id, manager_id
                )
                assert root_archive is not None
                assert root_archive["status"] == ProgressState.COMPLETED
                assert root_archive["task"].answer == (
                    "FINAL_REVIEWED_WITH_CAVEATS: unresolved evidence was reviewed and caveated."
                )
                assert (
                    root_archive["task"].answer
                    != "FINAL_REVIEW_EARLY_ANSWER_SHOULD_BE_SUPPRESSED"
                )
                assert any(
                    entry.details.get("action") == "begin_final_review_loop"
                    for entry in root_archive["task"].execution_history
                )

                plan_id = manager.task_manager.get_task_plan_id(root_task_id)
                assert plan_id is not None
                plan = manager.task_manager.get_plan(plan_id)
                assert plan is not None
                assert plan.status == PlanStatus.COMPLETED
                assert any(
                    action.details.get("action") == "begin_final_review_loop"
                    for action in plan.plan_actions
                )
                assert len(manager_llm.invocations) == 2
                assert (
                    "Final review required before completion"
                    in manager_llm.invocations[1]
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_manager_plan_step_delegates_to_specialist_and_continues_plan() -> (
    None
):
    team_id = new_id("team_plan_del")
    root_task_id = new_id("root_plan_del")
    manager_id = new_id("manager")
    code_id = new_id("code")
    conversation_id = f"conversation_{root_task_id}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "plan-delegated-step.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_holder: dict[str, UniversalAgent] = {}

        def _activate_plan() -> dict:
            manager = manager_holder["manager"]
            set_agent_ref(manager)
            result = json.loads(
                activate_plan_execution(
                    json.dumps(
                        [
                            {
                                "title": "Delegate implementation check",
                                "objective": "Delegate a bounded implementation check to the code specialist and integrate the result.",
                            },
                            {
                                "title": "Summarize delegated result",
                                "objective": "Use the delegated result from the first step to prepare a compact continuation summary.",
                            },
                        ]
                    ),
                    final_review=False,
                )
            )
            assert result["plan_execution_started"] is True
            return result

        manager_llm = ActivatingPlanFakeStrandsAgent(
            _activate_plan,
            _task_response("PLAN_DELEGATE_EARLY_RESPONSE_SHOULD_BE_SUPPRESSED"),
            _task_request(
                "CODE_SPECIALIST_TASK: inspect the bounded implementation implication for this plan step.",
                target_named_agent_type="CodeWritingAgent",
            ),
            _task_response(
                "PLAN_STEP_DELEGATED_RESULT: integrated CODE_SPECIALIST_RESULT."
            ),
            _task_response("PLAN_STEP_SUMMARY_RESULT: delegated result summarized."),
            _task_response(
                "PLAN_DELEGATED_FINAL: completed delegated plan-step execution."
            ),
        )
        code_llm = FakeStrandsAgent(
            _task_response(
                "CODE_SPECIALIST_RESULT: implementation implication verified."
            )
        )

        with (
            patch(
                "custom_agents.strands_agent.Agent", side_effect=[manager_llm, code_llm]
            ),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {
                    "CodeWritingAgent": f"{team_id}_CodeWritingAgent",
                }
            )
            manager_holder["manager"] = manager
            code = _agent(
                agent_id=code_id,
                name="code",
                team_id=team_id,
                named_agent_type="CodeWritingAgent",
                db_path=db_path,
                workspace_dir=workspace_dir,
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_plan_del"),
                    durable=False,
                    auto_delete=True,
                )

                for agent in (manager, code):
                    agent_tasks.append((agent, await _start_agent(agent)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement=(
                        "Use runtime plan-step execution and delegate one internal step to the code specialist."
                    ),
                )
                envelope = MessageEnvelope(
                    msg_content=root_task,
                    msg_header=MessageHeader(
                        from_agent_id="live-test-user",
                        to_routing_path=manager.id,
                        response_routing_key=user_queue.name,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name="live-test-user",
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        msg_correlation_id=f"corr_{root_task_id}",
                        conversation_id=conversation_id,
                        request_timezone="America/Chicago",
                    ),
                )
                await _publish_envelope(channel, manager.id, envelope)

                response = await _wait_for_response(user_queue, timeout=30)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == root_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert (
                    response.msg_content.answer
                    == "PLAN_DELEGATED_FINAL: completed delegated plan-step execution."
                )

                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, root_task_id, manager_id)
                        is not None
                    ),
                    timeout=18,
                )

                plan_id = manager.task_manager.get_task_plan_id(root_task_id)
                assert plan_id is not None
                plan = manager.task_manager.get_plan(plan_id)
                assert plan is not None
                assert [step.status.value for step in plan.plan_steps] == [
                    "completed",
                    "completed",
                ]
                assert (
                    plan.plan_steps[0].result_summary
                    == "PLAN_STEP_DELEGATED_RESULT: integrated CODE_SPECIALIST_RESULT."
                )
                assert (
                    plan.plan_steps[1].result_summary
                    == "PLAN_STEP_SUMMARY_RESULT: delegated result summarized."
                )

                first_step_task_id = plan.plan_steps[0].step_task_ids[0]
                first_step_archive = _archive_row(
                    manager.task_manager, first_step_task_id, manager_id
                )
                assert first_step_archive is not None
                delegated_task_ids = [
                    task_id
                    for task_id in plan.related_task_ids
                    if task_id
                    not in {
                        root_task_id,
                        *plan.plan_steps[0].step_task_ids,
                        *plan.plan_steps[1].step_task_ids,
                    }
                ]
                assert len(delegated_task_ids) == 1
                delegated_task_id = delegated_task_ids[0]
                code_archive = _archive_row(
                    code.task_manager, delegated_task_id, code_id
                )
                assert code_archive is not None
                assert code_archive["task"].problem_statement == (
                    "CODE_SPECIALIST_TASK: inspect the bounded implementation implication for this plan step."
                )
                assert (
                    code_archive["task"].answer
                    == "CODE_SPECIALIST_RESULT: implementation implication verified."
                )
                assert "plan_id" not in code_archive["task"].model_dump()

                first_step_history = [
                    entry.summary
                    for entry in first_step_archive["task"].execution_history
                ]
                assert any(
                    delegated_task_id in summary for summary in first_step_history
                )

                assert len(manager_llm.invocations) == 5
                second_step_prompt = manager_llm.invocations[3]
                final_root_prompt = manager_llm.invocations[4]
                assert "PLAN_STEP_DELEGATED_RESULT" in second_step_prompt
                assert "PLAN_STEP_SUMMARY_RESULT" in final_root_prompt
                for prompt in manager_llm.invocations[1:4]:
                    assert root_task_id not in prompt
                    assert conversation_id not in prompt
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_turn_action_delegation_aborts_before_publish_when_target_unavailable() -> (
    None
):
    """An exact agent has no shared queue, so it must answer a live probe;
    a worker type's queue always takes the task (ADR-019)."""
    team_id = new_id("team_ob_advice")
    root_task_id = new_id("root_ob_advice")
    manager_id = new_id("manager")
    specialist_queue_name = f"{team_id}_WebResearchAgent"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "outbox-advisory.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(
            _task_request(
                "OUTBOX_ADVISORY_TASK: this must not be published to an exact agent that is not live.",
                target_named_agent_type="WebResearchAgent",
                target_agent_name="research-that-is-not-running",
            )
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {"WebResearchAgent": specialist_queue_name}
            )
            manager.subtask_status_probe_timeout_seconds = 0.15
            creation_mock = MagicMock(return_value=AgentCreationResultV1(request_id="test", results=(AgentLaunchResultV1(status="failed", reason="live test no specialist"),)))
            object.__setattr__(manager, "handle_agent_creation_request", creation_mock)

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_ob_advice"),
                    durable=False,
                    auto_delete=True,
                )
                specialist_queue = await channel.declare_queue(
                    specialist_queue_name, durable=True
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Delegate once and leave the parent waiting.",
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=root_task,
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{root_task_id}",
                            conversation_id=f"conversation_{root_task_id}",
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                await _wait_for(
                    lambda: any(
                        entry.details.get("final_failure_phase")
                        == "availability_verification_failed"
                        for entry in manager.task_manager.get_task_history_entries(
                            root_task_id, limit=None
                        )
                    ),
                    timeout=8,
                )

                assert await specialist_queue.get(timeout=1, fail=False) is None
                assert manager.task_manager.get_subtasks(root_task_id) == []
                # No other worker is substituted and none is started.
                assert not creation_mock.called
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_delegated_task_waits_in_its_queue_until_a_worker_starts() -> None:
    """ADR-019: the lead publishes to the type's shared queue without probing
    or starting a worker. The task waits there (recorded as queued); a worker
    that starts later (as the pool controller would start one) takes it,
    reports its claim, and the lead records which worker holds it."""
    from custom_agents.delegated_work_runtime import delegated_tasks

    team_id = new_id("team_pool_wait")
    root_task_id = new_id("root_pool_wait")
    manager_id = new_id("manager")
    worker_id = new_id("research")
    specialist_queue_name = f"{team_id}_ResearchComparisonAgent"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        manager_llm = FakeStrandsAgent(
            _task_request(
                "Compare the candidate vehicles with current evidence.",
                target_named_agent_type="ResearchComparisonAgent",
            ),
            _task_response("LIVE_POOL_FINAL: comparison completed."),
        )
        worker_llm = DelayedFakeStrandsAgent(
            2.0,
            _task_response("LIVE_POOL_WORKER: comparison evidence ready."),
        )

        with (
            patch(
                "custom_agents.strands_agent.Agent",
                side_effect=[manager_llm, worker_llm],
            ),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=tmp_path / "manager.db",
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {"ResearchComparisonAgent": specialist_queue_name}
            )
            creation_mock = MagicMock(
                side_effect=AssertionError("The lead must not start workers for delegated work")
            )
            object.__setattr__(manager, "handle_agent_creation_request", creation_mock)

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_pool_wait"),
                    durable=False,
                    auto_delete=True,
                )
                agent_tasks.append((manager, await _start_agent(manager)))

                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=Task(
                            task_id=root_task_id,
                            task_type=MessageMetaType.REQUEST,
                            problem_statement="Compare several cars and recommend the best fit.",
                        ),
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
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
                                session_key="session-live-comparison",
                                channel_name="cli",
                                canonical_user_id="usr_live_test_user",
                                chat_id="cli:live-test",
                                delivery_mode="interactive",
                            ),
                        ),
                    ),
                )

                notice = await _wait_for_response(user_queue, timeout=8)
                assert isinstance(notice.msg_content, UserNotification)

                def records():
                    return delegated_tasks(manager).list_for_root(team_id, root_task_id)

                # Recorded before publishing; the stream sequence follows the publish.
                await _wait_for(
                    lambda: bool(records()) and bool(records()[0].publish_sequence), timeout=5
                )
                (queued,) = records()
                assert (queued.status, queued.holder_agent_id) == ("queued", None)
                assert queued.publish_sequence

                # No worker yet: the task stays in the queue and nothing fails it.
                await asyncio.sleep(1.5)
                assert records()[0].status == "queued"
                assert manager.task_manager.get_task_info(root_task_id).state == ProgressState.WAITING

                worker = _agent(
                    agent_id=worker_id,
                    name="research-comparison",
                    team_id=team_id,
                    named_agent_type="ResearchComparisonAgent",
                    db_path=tmp_path / "worker.db",
                    workspace_dir=workspace_dir,
                )
                worker.named_agent_type_queue_map.update(
                    {"ResearchComparisonAgent": specialist_queue_name}
                )
                agent_tasks.append((worker, asyncio.create_task(worker.connect())))

                await _wait_for(
                    lambda: records()[0].holder_agent_id == worker_id, timeout=10
                )
                response = await _wait_for_response(user_queue, timeout=15)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.answer == "LIVE_POOL_FINAL: comparison completed."
                await _wait_for(lambda: records()[0].status == "finished", timeout=5)
                creation_mock.assert_not_called()
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_failed_predispatch_availability_reenters_manager_with_runtime_action_brief() -> (
    None
):
    team_id = new_id("team_assur_rec")
    root_task_id = new_id("root_assur_rec")
    manager_id = new_id("manager")
    specialist_queue_name = f"{team_id}_WebResearchAgent"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "exact-target-recovery.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(
            _task_request(
                "ASSURANCE_RECOVERY_TASK: publish, then recover if no worker capacity exists.",
                target_named_agent_type="WebResearchAgent",
                target_agent_name="research-that-is-not-running",
            ),
            _task_response(
                "ASSURANCE_RECOVERY_FINAL: manager recovered after no worker capacity."
            ),
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {"WebResearchAgent": specialist_queue_name}
            )
            manager.subtask_status_probe_timeout_seconds = 0.05
            object.__setattr__(
                manager,
                "handle_agent_creation_request",
                MagicMock(return_value=AgentCreationResultV1(request_id="test", results=(AgentLaunchResultV1(status="failed", reason="live test no worker capacity"),))),
            )

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_assur_rec"),
                    durable=False,
                    auto_delete=True,
                )
                await channel.declare_queue(specialist_queue_name, durable=True)

                agent_tasks.append((manager, await _start_agent(manager)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Delegate once to an exact agent; its absence should recover through the manager.",
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=root_task,
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{root_task_id}",
                            conversation_id=f"conversation_{root_task_id}",
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                response = await _wait_for_response(user_queue, timeout=20)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == root_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert response.msg_content.answer == (
                    "ASSURANCE_RECOVERY_FINAL: manager recovered after no worker capacity."
                )

                assert len(manager_llm.invocations) == 2
                assert "Runtime Action Recovery Brief" in manager_llm.invocations[1]
                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, root_task_id, manager_id)
                        is not None
                    )
                )
                archive = _archive_row(manager.task_manager, root_task_id, manager_id)
                assert archive is not None
                history_summaries = [
                    entry.summary for entry in archive["task"].execution_history
                ]
                assert any(
                    "needs model recovery" in summary for summary in history_summaries
                )
                assert any(
                    entry.details.get("final_failure_phase")
                    == "availability_verification_failed"
                    for entry in archive["task"].execution_history
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_runtime_mailbox_defers_response_while_batch_lease_active() -> None:
    team_id = new_id("team_mailbox")
    parent_task_id = new_id("parent_mailbox")
    subtask_id = new_id("subtask_mailbox")
    manager_id = new_id("manager")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "mailbox-deferral.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(_task_response("unused"))

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.runtime_coordinator = {"enabled": True, "mode": "enforced"}
            resume_mock = AsyncMock()
            object.__setattr__(manager, "_resume_parent_task_if_ready", resume_mock)

            parent = Task(
                task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Parent is held by an active runtime batch lease.",
            )
            assert manager.task_manager.add_task(
                parent,
                MessageHeader(
                    from_agent_id="live-test-user",
                    response_routing_key="unused_user_queue",
                ),
                MessageMeta(
                    from_agent_name="live-test-user",
                    requester_type=RequesterType.USER_REQUESTER,
                    is_response_requested=True,
                    msg_correlation_id=f"corr_{parent_task_id}",
                    conversation_id=f"conversation_{parent_task_id}",
                ),
                owner_person_id=None,
            )
            manager.task_manager.get_next_task_to_process()
            assert manager.task_manager.update_main_task_state(
                parent_task_id,
                ProgressState.IN_PROGRESS,
                datetime.now(timezone.utc).isoformat(),
            )
            child = Task(
                task_id=subtask_id,
                parent_task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Child response should be mailbox-deferred.",
            )
            assert manager.task_manager.add_subtask(
                parent_task_id,
                child,
                MessageHeader(
                    from_agent_id=manager.id,
                    to_routing_path=f"{team_id}_WebResearchAgent",
                ),
                MessageMeta(
                    to_named_agent_type="WebResearchAgent",
                    requester_type=RequesterType.AGENT_REQUESTER,
                ),
            )
            record_publication(manager, subtask_id=subtask_id, parent_task_id=parent_task_id, named_agent_type="WebResearchAgent", target_agent_name=None, publish_sequence=1)
            delegated_tasks(manager).record_claim(team_id=team_id, task_id=subtask_id, worker_agent_id="live-worker", worker_agent_name="live-worker", claimed_at=datetime.now(timezone.utc))
            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                agent_tasks.append((manager, await _start_agent(manager)))
                lease = manager.task_manager.begin_runtime_batch_lease(
                    root_task_id=parent_task_id,
                    source_task_id=parent_task_id,
                    batch_id="turn_batch_live_mailbox",
                    owner_agent_id=manager.id,
                )

                response = Task(
                    task_id=subtask_id,
                    parent_task_id=parent_task_id,
                    task_type=MessageMetaType.RESPONSE,
                    answer="live mailbox child result",
                    task_state=[
                        (
                            ProgressState.COMPLETED,
                            datetime.now(timezone.utc).isoformat(),
                        )
                    ],
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=response,
                        msg_header=MessageHeader(
                            from_agent_id="live-worker",
                            to_routing_path=manager.id,
                            message_id=f"msg_{subtask_id}",
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-worker",
                            requester_type=RequesterType.AGENT_REQUESTER,
                            msg_correlation_id=f"corr_{subtask_id}",
                            conversation_id=f"conversation_{parent_task_id}",
                        ),
                    ),
                )

                await _wait_for(
                    lambda: (
                        len(
                            manager.task_manager.list_deferred_runtime_events(
                                parent_task_id
                            )
                        )
                        == 1
                    ),
                    timeout=10,
                )
                assert (
                    manager.task_manager.get_task_info(subtask_id).state
                    == ProgressState.ACCEPTED
                )
                resume_mock.assert_not_awaited()

                manager.task_manager.complete_runtime_batch_lease(
                    lease.lease_id, status="completed"
                )
                schedule_pending_work(manager)

                await _wait_for(
                    lambda: (
                        manager.task_manager.get_task_info(subtask_id).state
                        == ProgressState.COMPLETED
                    ),
                    timeout=10,
                )
                assert (
                    manager.task_manager.get_task_info(subtask_id).task.answer
                    == "live mailbox child result"
                )
                assert (
                    manager.task_manager.list_deferred_runtime_events(parent_task_id)
                    == []
                )
                resume_mock.assert_awaited_once_with(parent_task_id)
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_watchdog_failure_replays_after_worker_response_when_batch_active() -> (
    None
):
    team_id = new_id("team_watch_race")
    parent_task_id = new_id("parent_watchdog")
    subtask_id = new_id("subtask_watch")
    manager_id = new_id("manager")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "watchdog-race.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(_task_response("unused"))

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.runtime_coordinator = {"enabled": True, "mode": "enforced"}
            resume_mock = AsyncMock()
            object.__setattr__(manager, "_resume_parent_task_if_ready", resume_mock)

            parent = Task(
                task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Parent has a watchdog failure racing a child response.",
            )
            assert manager.task_manager.add_task(
                parent,
                MessageHeader(
                    from_agent_id="live-test-user",
                    response_routing_key="unused_user_queue",
                ),
                MessageMeta(
                    from_agent_name="live-test-user",
                    requester_type=RequesterType.USER_REQUESTER,
                    is_response_requested=True,
                    msg_correlation_id=f"corr_{parent_task_id}",
                    conversation_id=f"conversation_{parent_task_id}",
                ),
                owner_person_id=None,
            )
            manager.task_manager.get_next_task_to_process()
            assert manager.task_manager.update_main_task_state(
                parent_task_id,
                ProgressState.IN_PROGRESS,
                datetime.now(timezone.utc).isoformat(),
            )
            child = Task(
                task_id=subtask_id,
                parent_task_id=parent_task_id,
                task_type=MessageMetaType.REQUEST,
                problem_statement="Child response should win over deferred watchdog failure.",
            )
            child_info = manager.task_manager.add_subtask(
                parent_task_id,
                child,
                MessageHeader(
                    from_agent_id=manager.id,
                    to_routing_path=f"{team_id}_WebResearchAgent",
                ),
                MessageMeta(
                    to_named_agent_type="WebResearchAgent",
                    requester_type=RequesterType.AGENT_REQUESTER,
                ),
            )
            assert child_info is not None

            record_publication(manager, subtask_id=subtask_id, parent_task_id=parent_task_id, named_agent_type="WebResearchAgent", target_agent_name=None, publish_sequence=1)
            delegated_tasks(manager).record_claim(team_id=team_id, task_id=subtask_id, worker_agent_id="live-worker", worker_agent_name="live-worker", claimed_at=datetime.now(timezone.utc))

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                agent_tasks.append((manager, await _start_agent(manager)))
                lease = manager.task_manager.begin_runtime_batch_lease(
                    root_task_id=parent_task_id,
                    source_task_id=parent_task_id,
                    batch_id="turn_batch_live_watchdog_race",
                    owner_agent_id=manager.id,
                )

                await manager._fail_subtask_and_reconcile_parent(
                    parent_task_id=parent_task_id,
                    subtask_info=child_info,
                    error_message="Delegated subtask timed out during active batch.",
                    resume_parent=True,
                    synthetic_context="Watchdog failure should replay behind any child response.",
                )
                await _wait_for(
                    lambda: (
                        len(
                            manager.task_manager.list_deferred_runtime_events(
                                parent_task_id
                            )
                        )
                        == 1
                    ),
                    timeout=10,
                )

                response = Task(
                    task_id=subtask_id,
                    parent_task_id=parent_task_id,
                    task_type=MessageMetaType.RESPONSE,
                    answer="live worker response wins",
                    task_state=[
                        (
                            ProgressState.COMPLETED,
                            datetime.now(timezone.utc).isoformat(),
                        )
                    ],
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=response,
                        msg_header=MessageHeader(
                            from_agent_id="live-worker",
                            to_routing_path=manager.id,
                            message_id=f"msg_{subtask_id}",
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-worker",
                            requester_type=RequesterType.AGENT_REQUESTER,
                            msg_correlation_id=f"corr_{subtask_id}",
                            conversation_id=f"conversation_{parent_task_id}",
                        ),
                    ),
                )

                await _wait_for(
                    lambda: (
                        len(
                            manager.task_manager.list_deferred_runtime_events(
                                parent_task_id
                            )
                        )
                        == 2
                    ),
                    timeout=10,
                )
                assert [
                    event.event_type
                    for event in manager.task_manager.list_deferred_runtime_events(
                        parent_task_id
                    )
                ] == ["task_response", "subtask_failure_reconciliation"]
                assert (
                    manager.task_manager.get_task_info(subtask_id).state
                    == ProgressState.ACCEPTED
                )
                resume_mock.assert_not_awaited()

                manager.task_manager.complete_runtime_batch_lease(
                    lease.lease_id, status="completed"
                )
                schedule_pending_work(manager)

                await _wait_for(
                    lambda: (
                        manager.task_manager.get_task_info(subtask_id).state
                        == ProgressState.COMPLETED
                    ),
                    timeout=10,
                )
                assert (
                    manager.task_manager.get_task_info(subtask_id).task.answer
                    == "live worker response wins"
                )
                assert (
                    manager.task_manager.list_deferred_runtime_events(parent_task_id)
                    == []
                )
                resume_mock.assert_awaited_once_with(parent_task_id)
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_turn_action_publish_retry_reuses_registered_subtask() -> None:
    team_id = new_id("team_ob_retry")
    root_task_id = new_id("root_ob_retry")
    manager_id = new_id("manager")
    specialist_queue_name = f"{team_id}_WebResearchAgent"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "outbox-retry.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(
            _task_request(
                "OUTBOX_RETRY_TASK: publish retry should not create a second subtask.",
                target_named_agent_type="WebResearchAgent",
            )
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.named_agent_type_queue_map.update(
                {"WebResearchAgent": specialist_queue_name}
            )
            manager.subtask_status_probe_timeout_seconds = 0.15
            manager.turn_action_outbox = {
                "max_auto_attempts": 2,
                "initial_retry_delay_seconds": 0.2,
                "max_retry_delay_seconds": 0.2,
                "retry_jitter_ratio": 0.0,
                "max_inline_recovery_attempts_per_batch": 2,
                "dispatching_stale_after_seconds": 0.2,
            }
            creation_mock = MagicMock(return_value=AgentCreationResultV1(request_id="test", results=(AgentLaunchResultV1(status="failed", reason="live test no specialist"),)))
            object.__setattr__(manager, "handle_agent_creation_request", creation_mock)

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_ob_retry"),
                    durable=False,
                    auto_delete=True,
                )
                specialist_queue = await channel.declare_queue(
                    specialist_queue_name, durable=True
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                original_send_message = manager.team_comms.send_message
                send_attempts: list[tuple[MessageEnvelope, str]] = []

                async def flaky_send_message(
                    *,
                    message: MessageEnvelope,
                    routing_key: str,
                    team_id: Optional[str] = None,
                ) -> None:
                    send_attempts.append((message, routing_key))
                    if len(send_attempts) == 1:
                        raise TimeoutError("live injected publish timeout")
                    await original_send_message(
                        message=message,
                        routing_key=routing_key,
                        team_id=team_id,
                    )

                object.__setattr__(
                    manager.team_comms, "send_message", flaky_send_message
                )

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="Delegate once; the first publish attempt should fail and retry.",
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=root_task,
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{root_task_id}",
                            conversation_id=f"conversation_{root_task_id}",
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                await _wait_for(
                    lambda: (
                        len(send_attempts) == 2
                        # Completing an action precedes the coordinator's
                        # awaited batch completion and parent-state projection.
                        and manager.task_manager.get_task_info(root_task_id).state
                        == ProgressState.WAITING
                        and bool(
                            manager.task_manager.execution_state.turn_action_outbox
                        )
                        and next(
                            iter(
                                manager.task_manager.execution_state.turn_action_outbox.values()
                            )
                        ).status
                        == "completed"
                    ),
                    timeout=14,
                )

                subtasks = manager.task_manager.get_subtasks(root_task_id)
                assert len(subtasks) == 1
                delegated_task_id = subtasks[0].task.task_id
                assert send_attempts[0][0].msg_content.task_id == delegated_task_id
                assert send_attempts[1][0].msg_content.task_id == delegated_task_id

                message = await specialist_queue.get(timeout=4, fail=False)
                assert message is not None
                delegated = message

                assert isinstance(delegated.msg_content, Task)
                assert delegated.msg_content.task_id == delegated_task_id
                assert delegated.msg_content.parent_task_id == root_task_id
                assert delegated.msg_content.problem_statement == (
                    "OUTBOX_RETRY_TASK: publish retry should not create a second subtask."
                )
                record = next(
                    iter(
                        manager.task_manager.execution_state.turn_action_outbox.values()
                    )
                )
                assert record.attempt_count == 2
                assert record.side_effects["delegated_task_id"] == delegated_task_id
                assert (
                    manager.task_manager.get_task_info(root_task_id).state
                    == ProgressState.WAITING
                )
                assert len(manager_llm.invocations) == 1
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()


async def test_live_turn_action_inline_recovery_reenters_model_with_action_ledger() -> (
    None
):
    team_id = new_id("team_ob_recov")
    root_task_id = new_id("root_ob_recov")
    manager_id = new_id("manager")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "outbox-inline-recovery.db"
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()

        manager_llm = FakeStrandsAgent(
            _unroutable_task_request(
                "OUTBOX_RECOVERY_BAD_TASK: missing target should trigger recovery."
            ),
            _task_response(
                "OUTBOX_RECOVERY_FINAL: recovered from the runtime action ledger."
            ),
        )

        with (
            patch("custom_agents.strands_agent.Agent", return_value=manager_llm),
            patch(
                "custom_agents.strands_agent.UniversalAgent._configure_model",
                return_value=MagicMock(),
            ),
        ):
            manager = _agent(
                agent_id=manager_id,
                name="manager",
                team_id=team_id,
                named_agent_type="TeamArchitect",
                db_path=db_path,
                workspace_dir=workspace_dir,
                is_lead=True,
            )
            manager.turn_action_outbox = {
                "max_auto_attempts": 0,
                "initial_retry_delay_seconds": 0.1,
                "max_retry_delay_seconds": 0.1,
                "retry_jitter_ratio": 0.0,
                "max_inline_recovery_attempts_per_batch": 2,
                "dispatching_stale_after_seconds": 0.2,
            }

            agent_tasks: list[tuple[UniversalAgent, asyncio.Task]] = []
            connection = await _connect_nats(team_id)
            try:
                channel = await connection.channel()
                user_queue = await channel.declare_queue(
                    new_id("usr_ob_recov"),
                    durable=False,
                    auto_delete=True,
                )

                agent_tasks.append((manager, await _start_agent(manager)))

                root_task = Task(
                    task_id=root_task_id,
                    task_type=MessageMetaType.REQUEST,
                    problem_statement="First produce an unroutable delegation, then recover from the ledger.",
                )
                await _publish_envelope(
                    channel,
                    manager.id,
                    MessageEnvelope(
                        msg_content=root_task,
                        msg_header=MessageHeader(
                            from_agent_id="live-test-user",
                            to_routing_path=manager.id,
                            response_routing_key=user_queue.name,
                        ),
                        msg_meta=MessageMeta(
                            from_agent_name="live-test-user",
                            requester_type=RequesterType.USER_REQUESTER,
                            is_response_requested=True,
                            msg_correlation_id=f"corr_{root_task_id}",
                            conversation_id=f"conversation_{root_task_id}",
                            request_timezone="America/Chicago",
                        ),
                    ),
                )

                response = await _wait_for_response(user_queue, timeout=20)
                assert isinstance(response.msg_content, Task)
                assert response.msg_content.task_id == root_task_id
                assert response.msg_content.task_type == MessageMetaType.RESPONSE
                assert (
                    response.msg_content.answer
                    == "OUTBOX_RECOVERY_FINAL: recovered from the runtime action ledger."
                )

                await _wait_for(
                    lambda: (
                        _archive_row(manager.task_manager, root_task_id, manager_id)
                        is not None
                    ),
                    timeout=12,
                )
                assert len(manager_llm.invocations) == 2
                recovery_prompt = manager_llm.invocations[1]
                assert "Runtime Action Recovery Brief" in recovery_prompt
                assert "Treat completed actions as facts" in recovery_prompt
                assert "Failed or retrying actions:" in recovery_prompt
                assert "Refusing to dispatch unroutable Task request" in recovery_prompt
                assert "Blocked actions not attempted:" in recovery_prompt

                archive = _archive_row(manager.task_manager, root_task_id, manager_id)
                assert archive is not None
                assert any(
                    "needs model recovery" in entry.summary
                    for entry in archive["task"].execution_history
                )
            finally:
                await _stop_agents(agent_tasks)
                await connection.close()
