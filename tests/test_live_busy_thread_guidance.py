"""A message sent into a busy thread is guidance for that thread's request.

A thread runs one request at a time. These tests run the real gateway (its
SQLite store and request handling) and a real team lead (``UniversalAgent``
with its own task store) over a live NATS server, and check where each later
message in the thread goes. Only the lead's model is scripted, so a test
controls when a model turn starts and ends; the secure tool broker is stubbed
in the credential-form case because the broker is a separate service.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_agents.structured_llm_messages import (
    SecureCredentialCollectionAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskResponseAction,
)
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.enums import MessageMetaType, TaskSteeringStatus, TaskSteeringType
from id_generation import new_id
from live_nats_client import LiveNatsClient, resolve_live_transport_settings
from tests.test_live_multi_agent_state_boundaries import _agent, _start_agent, _stop_agents


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
]

logger = logging.getLogger(__name__)
# Generous: the lead's SQLite store can be slow on shared test storage.
WAIT_SECONDS = 120.0


def _answer(text: str) -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            TaskResponseAction(
                task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer=text)
            )
        ]
    )


def _credential_form() -> StructuredAgentTurnDecision:
    return StructuredAgentTurnDecision(
        actions=[
            SecureCredentialCollectionAction(
                secure_credential_collection_request=SecureCredentialCollectionRequest(
                    service_name="Example",
                    credential_key="example_login",
                    purpose="Sign in to Example for the report.",
                )
            )
        ]
    )


@dataclass
class _Turn:
    """One scripted lead model turn, chosen by ``marker`` appearing in its prompt."""

    marker: str
    decision: StructuredAgentTurnDecision
    release: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    prompt: str | None = None


class _ScriptedLead:
    """Stands in for the lead's Strands agent.

    Each invocation takes the first unused turn whose marker is in the prompt,
    so an unexpected turn (such as guidance turned into a separate request)
    shows up in ``unexpected`` instead of shifting the script.
    """

    def __init__(self, *turns: _Turn) -> None:
        self.turns = list(turns)
        self.messages: list[object] = []
        self.unexpected: list[str] = []

    async def invoke_async(self, prompt: str, **_: object) -> object:
        turn = next(
            (item for item in self.turns if item.prompt is None and item.marker in prompt),
            None,
        )
        if turn is None:
            self.unexpected.append(prompt)
            decision = _answer("Unexpected turn.")
        else:
            turn.prompt = prompt
            turn.started.set()
            if turn.release is not None:
                await turn.release.wait()
            decision = turn.decision
        return type("Result", (), {"structured_output": decision, "__str__": lambda self: decision.model_dump_json()})()


@dataclass
class _Gateway:
    """One running gateway process over the shared store, with its outbound traffic recorded."""

    gateway: UserGateway
    held_statuses: list[MessageEnvelope]
    deliver_inbound: Callable[[MessageEnvelope], object]


@dataclass
class _Team:
    tmp_path: Path
    config: GatewayInstanceConfig
    lead: object
    lead_model: _ScriptedLead
    published_tasks: list[MessageEnvelope] = field(default_factory=list)
    published_steering: list[MessageEnvelope] = field(default_factory=list)
    steering_statuses: list[MessageEnvelope] = field(default_factory=list)
    hold_steering_status: bool = False
    current: _Gateway | None = None

    @property
    def gateway(self) -> UserGateway:
        assert self.current is not None
        return self.current.gateway

    async def start_gateway(self) -> UserGateway:
        gateway = UserGateway(
            self.config,
            state_store=GatewayStateStore(str(self.tmp_path / "gateway.db")),
        )
        client = gateway._manager_client
        publish_durable = client.publish_durable
        publish_operational = client.publish_operational
        handle_response = client.handle_response
        held: list[MessageEnvelope] = []

        async def record_durable(envelope: MessageEnvelope, **kwargs: object) -> None:
            self.published_tasks.append(envelope)
            await publish_durable(envelope, **kwargs)

        async def record_operational(envelope: MessageEnvelope) -> None:
            if isinstance(envelope.msg_content, TaskSteering):
                self.published_steering.append(envelope)
            await publish_operational(envelope)

        async def hold_statuses(envelope: MessageEnvelope) -> None:
            content = envelope.msg_content
            if isinstance(content, TaskSteering) and content.steering_type == TaskSteeringType.STATUS:
                self.steering_statuses.append(envelope)
            if (
                self.hold_steering_status
                and isinstance(content, TaskSteering)
                and content.steering_type == TaskSteeringType.STATUS
            ):
                held.append(envelope)
                return
            await handle_response(envelope)

        client.publish_durable = record_durable
        client.publish_operational = record_operational
        client.handle_response = hold_statuses
        await gateway.connect(start_services=False)
        self.current = _Gateway(gateway, held, handle_response)
        return gateway

    async def stop_gateway(self) -> None:
        if self.current is not None:
            await self.current.gateway.close()
            self.current = None

    async def release_held_statuses(self) -> None:
        assert self.current is not None
        self.hold_steering_status = False
        held = list(self.current.held_statuses)
        self.current.held_statuses.clear()
        for envelope in held:
            await self.current.deliver_inbound(envelope)

    def send(self, text: str, *, session_key: str) -> asyncio.Task:
        return asyncio.create_task(
            self.gateway.application.send_user_message(text, session_key=session_key)
        )

    def requests(self, session_key: str):
        return self.gateway.state_store.list_requests_for_session(session_key, limit=50)

    def request_for(self, session_key: str, text: str):
        return next(record for record in self.requests(session_key) if record.request_text == text)

    def root_tasks_named(self, text: str) -> list[Task]:
        return [
            envelope.msg_content
            for envelope in self.published_tasks
            if isinstance(envelope.msg_content, Task)
            and envelope.msg_content.task_type == MessageMetaType.REQUEST
            and text in (envelope.msg_content.problem_statement or "")
        ]

    def steering_with(self, text: str) -> list[tuple[str | None, str | None]]:
        return [
            (envelope.msg_content.target_task_id, envelope.msg_content.target_correlation_id)
            for envelope in self.published_steering
            if envelope.msg_content.guidance_text == text
        ]


@contextlib.asynccontextmanager
async def _team(
    tmp_path: Path,
    *turns: _Turn,
    request_timeout_seconds: float | None = None,
    configure: Callable[[GatewayInstanceConfig], None] | None = None,
) -> AsyncIterator[_Team]:
    team_id = new_id("team_busy")
    manager_id = new_id("manager")
    lead_model = _ScriptedLead(*turns)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = GatewayInstanceConfig.create(
        instance_name=team_id,
        instance_root=str(tmp_path / "instance"),
        team_id=team_id,
        timezone="UTC",
        manager_agent_id=manager_id,
    )
    config.transport = resolve_live_transport_settings()
    config.delivery.quiet_hours.enabled = False
    if request_timeout_seconds is not None:
        config.manager.request_timeout_seconds = request_timeout_seconds
    if configure is not None:
        configure(config)
    with (
        patch("custom_agents.strands_agent.Agent", return_value=lead_model),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        lead = _agent(
            agent_id=manager_id,
            name="manager",
            team_id=team_id,
            named_agent_type="TeamArchitect",
            db_path=tmp_path / "manager.db",
            workspace_dir=workspace,
            is_lead=True,
        )
        team = _Team(tmp_path=tmp_path, config=config, lead=lead, lead_model=lead_model)
        agents = [(lead, await _start_agent(lead))]
        await team.start_gateway()
        try:
            yield team
        finally:
            for turn in turns:
                if turn.release is not None:
                    turn.release.set()
            await team.stop_gateway()
            await _stop_agents(agents)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = WAIT_SECONDS) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Timed out waiting for the condition.")
        await asyncio.sleep(0.05)


async def _settle(*tasks: asyncio.Task) -> None:
    """Let in-flight sends finish; a send whose wait timed out is not a test failure."""

    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), WAIT_SECONDS)


async def test_guidance_before_the_first_reply_reaches_the_new_request(tmp_path: Path) -> None:
    """Finish A, start B in the same thread, send guidance before B's first reply."""

    hold_b = asyncio.Event()
    turns = (
        _Turn("ALPHA-REQUEST", _answer("Alpha finished.")),
        _Turn("BRAVO-REQUEST", _answer("Bravo draft."), release=hold_b),
        _Turn("BRAVO-GUIDANCE", _answer("Bravo finished with the guidance.")),
    )
    async with _team(tmp_path, *turns) as team:
        session = team.gateway.application.start_new_session().session_key
        first = await asyncio.wait_for(team.send("ALPHA-REQUEST", session_key=session), WAIT_SECONDS)
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await asyncio.wait_for(turns[1].started.wait(), WAIT_SECONDS)
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        correlation_b = team.request_for(session, "BRAVO-REQUEST").correlation_id

        guidance = team.send("BRAVO-GUIDANCE", session_key=session)
        await _wait_until(lambda: bool(team.steering_with("BRAVO-GUIDANCE")) or guidance.done())
        await asyncio.sleep(0.5)
        logger.warning(
            "early guidance: task_a=%s task_b=%s correlation_b=%s steering=%s new_requests=%s",
            first.task_id,
            task_b,
            correlation_b,
            team.steering_with("BRAVO-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-GUIDANCE")],
        )
        hold_b.set()
        await _settle(bravo, guidance)

        assert team.steering_with("BRAVO-GUIDANCE") == [(task_b, correlation_b)]
        assert team.root_tasks_named("BRAVO-GUIDANCE") == []
        assert team.gateway.state_store.get_request(correlation_b).status != "stale"
        await _wait_until(lambda: turns[2].prompt is not None)
        assert team.lead_model.unexpected == []


async def test_guidance_for_a_request_waiting_behind_other_work_is_held_until_it_starts(
    tmp_path: Path,
) -> None:
    """The lead is busy with another thread, so B waits in its queue; so does B's guidance."""

    hold_other = asyncio.Event()
    hold_bravo = asyncio.Event()
    turns = (
        _Turn("ALPHA-REQUEST", _answer("Alpha finished.")),
        _Turn("XRAY-REQUEST", _answer("Xray finished."), release=hold_other),
        _Turn("BRAVO-REQUEST", _answer("Bravo draft."), release=hold_bravo),
        _Turn("BRAVO-GUIDANCE", _answer("Bravo finished with guidance.")),
    )
    async with _team(tmp_path, *turns) as team:
        _short_steering_status_wait(team.gateway)
        session = team.gateway.application.start_new_session().session_key
        other = team.gateway.application.start_new_session().session_key
        await asyncio.wait_for(team.send("ALPHA-REQUEST", session_key=session), WAIT_SECONDS)
        xray = team.send("XRAY-REQUEST", session_key=other)
        await asyncio.wait_for(turns[1].started.wait(), WAIT_SECONDS)
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await _wait_until(lambda: bool(team.root_tasks_named("BRAVO-REQUEST")))
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id

        guidance = await asyncio.wait_for(team.send("BRAVO-GUIDANCE", session_key=session), WAIT_SECONDS)
        logger.warning(
            "guidance behind other work: task_b=%s reply=%r steering=%s new_requests=%s",
            task_b,
            guidance.text,
            team.steering_with("BRAVO-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-GUIDANCE")],
        )
        guidance_correlation = team.request_for(session, "BRAVO-GUIDANCE").correlation_id
        # A duplicate NOT_FOUND response is still deduplicated by JetStream.
        # It must not prevent a later ACCEPTED response from reaching us.
        await team.gateway._thread_guidance.retry_held()
        def statuses():
            return [
                envelope.msg_content.status
                for envelope in team.steering_statuses
                if envelope.msg_meta.msg_correlation_id == guidance_correlation
            ]
        assert statuses() == [TaskSteeringStatus.STALE]
        hold_other.set()
        await asyncio.wait_for(turns[2].started.wait(), WAIT_SECONDS)
        # The lead pulls the queued request only when it can run it. Its
        # startup report wakes held guidance, which applies at its next safe
        # point, including deferring the first turn's draft answer.
        await _wait_until(
            lambda: team.request_for(session, "BRAVO-GUIDANCE").status == "accepted"
        )
        hold_bravo.set()
        await _settle(xray, bravo)
        await _wait_until(lambda: turns[3].prompt is not None)

        assert team.root_tasks_named("BRAVO-GUIDANCE") == []
        assert {target for target, _correlation in team.steering_with("BRAVO-GUIDANCE")} == {task_b}
        assert "BRAVO-GUIDANCE" in turns[3].prompt
        assert statuses() == [TaskSteeringStatus.STALE, TaskSteeringStatus.ACCEPTED]
        assert team.lead_model.unexpected == []


async def test_message_after_the_reply_wait_timed_out_is_guidance(tmp_path: Path) -> None:
    hold_b = asyncio.Event()
    turns = (
        _Turn("BRAVO-REQUEST", _answer("Bravo draft."), release=hold_b),
        _Turn("BRAVO-GUIDANCE", _answer("Bravo finished with the guidance.")),
    )
    async with _team(tmp_path, *turns, request_timeout_seconds=1.0) as team:
        session = team.gateway.application.start_new_session().session_key
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(bravo, WAIT_SECONDS)
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        correlation_b = team.request_for(session, "BRAVO-REQUEST").correlation_id

        guidance = team.send("BRAVO-GUIDANCE", session_key=session)
        await _wait_until(lambda: bool(team.steering_with("BRAVO-GUIDANCE")) or bool(team.root_tasks_named("BRAVO-GUIDANCE")))
        logger.warning(
            "after reply timeout: task_b=%s correlation_b=%s steering=%s new_requests=%s",
            task_b,
            correlation_b,
            team.steering_with("BRAVO-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-GUIDANCE")],
        )
        hold_b.set()
        await _settle(guidance)

        assert team.steering_with("BRAVO-GUIDANCE") == [(task_b, correlation_b)]
        assert team.root_tasks_named("BRAVO-GUIDANCE") == []
        await _wait_until(lambda: turns[1].prompt is not None)
        assert team.lead_model.unexpected == []


async def test_message_after_a_guidance_status_timeout_is_guidance(tmp_path: Path) -> None:
    hold_b = asyncio.Event()
    turns = (
        _Turn("BRAVO-REQUEST", _answer("Bravo draft."), release=hold_b),
        _Turn("BRAVO-GUIDANCE", _answer("Bravo finished with the guidance.")),
    )
    async with _team(tmp_path, *turns) as team:
        _short_steering_status_wait(team.gateway)
        session = team.gateway.application.start_new_session().session_key
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        correlation_b = team.request_for(session, "BRAVO-REQUEST").correlation_id

        team.hold_steering_status = True
        first = await asyncio.wait_for(team.send("BRAVO-GUIDANCE", session_key=session), WAIT_SECONDS)
        second = await asyncio.wait_for(team.send("BRAVO-MORE-GUIDANCE", session_key=session), WAIT_SECONDS)
        assert team.steering_with("BRAVO-MORE-GUIDANCE") == []
        assert team.root_tasks_named("BRAVO-MORE-GUIDANCE") == []
        held = team.gateway.request_repository.get_held_guidance(second.correlation_id)
        assert held is not None and held.target_correlation_id == correlation_b
        logger.warning(
            "after guidance status timeout: task_b=%s correlation_b=%s first_reply=%r steering=%s new_requests=%s",
            task_b,
            correlation_b,
            first.text,
            team.steering_with("BRAVO-MORE-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-MORE-GUIDANCE")],
        )
        await team.release_held_statuses()
        await team.gateway._thread_guidance.retry_held()
        await _wait_until(lambda: bool(team.steering_with("BRAVO-MORE-GUIDANCE")))
        hold_b.set()
        await _settle(bravo)

        assert team.root_tasks_named("BRAVO-MORE-GUIDANCE") == []
        assert set(team.steering_with("BRAVO-MORE-GUIDANCE")) == {(task_b, correlation_b)}
        assert team.lead_model.unexpected == []


async def test_a_timer_result_with_the_same_correlation_keeps_the_original_request_running(tmp_path: Path) -> None:
    hold = asyncio.Event()
    turn = _Turn("ORIGINAL-REQUEST", _answer("Original request finished"), release=hold)
    async with _team(tmp_path, turn) as team:
        delivered = []
        team.gateway._event_output.emit = _recording_emit(team.gateway._event_output.emit, delivered)
        session = team.gateway.application.start_new_session().session_key
        original = team.send("ORIGINAL-REQUEST", session_key=session)
        try:
            await asyncio.wait_for(turn.started.wait(), WAIT_SECONDS)
            root = team.published_tasks[0]
            correlation = root.msg_meta.msg_correlation_id
            before = team.gateway.state_store.get_request(correlation)
            async with LiveNatsClient(team.config.gateway.team_id, transport=team.config.transport) as timer:
                await timer.publish(
                    team.config.manager.response_queue_name,
                    MessageEnvelope(
                        msg_content=Task(task_id=new_id("timer_task"), task_type=MessageMetaType.RESPONSE, answer="Timer finished"),
                        msg_header=MessageHeader(from_agent_id=team.config.manager.manager_agent_id),
                        msg_meta=root.msg_meta,
                    ),
                )
                await _wait_until(lambda: "Timer finished" in delivered)
            assert not original.done()
            assert team.gateway.state_store.get_request(correlation) == before
            hold.set()
            response = await asyncio.wait_for(original, WAIT_SECONDS)
            assert response.task_id == root.msg_content.task_id
            assert response.text == "Original request finished"
            assert team.gateway.state_store.get_request(correlation).run_state == "finished"
        finally:
            hold.set()
            original.cancel()
            await asyncio.gather(original, return_exceptions=True)


async def test_late_guidance_acknowledgment_is_recorded_and_next_message_is_guidance(
    tmp_path: Path,
) -> None:
    hold_b = asyncio.Event()
    turns = (
        _Turn("BRAVO-REQUEST", _answer("Bravo draft."), release=hold_b),
        _Turn("BRAVO-GUIDANCE", _answer("Bravo finished with the guidance.")),
    )
    async with _team(tmp_path, *turns) as team:
        _short_steering_status_wait(team.gateway)
        delivered: list[str] = []
        team.gateway._event_output.emit = _recording_emit(team.gateway._event_output.emit, delivered)
        session = team.gateway.application.start_new_session().session_key
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        correlation_b = team.request_for(session, "BRAVO-REQUEST").correlation_id

        team.hold_steering_status = True
        await asyncio.wait_for(team.send("BRAVO-GUIDANCE", session_key=session), WAIT_SECONDS)
        guidance_correlation = team.request_for(session, "BRAVO-GUIDANCE").correlation_id
        await team.release_held_statuses()
        await _wait_until(
            lambda: team.gateway.state_store.get_request(guidance_correlation).status != "submitted"
        )
        second = team.send("BRAVO-MORE-GUIDANCE", session_key=session)
        await _wait_until(
            lambda: bool(team.steering_with("BRAVO-MORE-GUIDANCE"))
            or bool(team.root_tasks_named("BRAVO-MORE-GUIDANCE"))
        )
        logger.warning(
            "late acknowledgment: guidance_status=%s delivered=%r steering=%s new_requests=%s",
            team.gateway.state_store.get_request(guidance_correlation).status,
            delivered,
            team.steering_with("BRAVO-MORE-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-MORE-GUIDANCE")],
        )
        hold_b.set()
        await _settle(bravo, second)

        assert team.gateway.state_store.get_request(guidance_correlation).status == "accepted"
        assert team.root_tasks_named("BRAVO-MORE-GUIDANCE") == []
        assert set(team.steering_with("BRAVO-MORE-GUIDANCE")) == {(task_b, correlation_b)}
        assert team.lead_model.unexpected == []


async def test_message_after_a_gateway_restart_is_guidance_for_the_running_request(
    tmp_path: Path,
) -> None:
    hold_b = asyncio.Event()
    turns = (
        _Turn("BRAVO-REQUEST", _answer("Bravo draft."), release=hold_b),
        _Turn("BRAVO-GUIDANCE", _answer("Bravo finished with the guidance.")),
    )
    async with _team(tmp_path, *turns) as team:
        session = team.gateway.application.start_new_session().session_key
        bravo = team.send("BRAVO-REQUEST", session_key=session)
        await asyncio.wait_for(turns[0].started.wait(), WAIT_SECONDS)
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        correlation_b = team.request_for(session, "BRAVO-REQUEST").correlation_id

        # The gateway process stops: its in-memory reply wait ends with it.
        bravo.cancel()
        await asyncio.gather(bravo, return_exceptions=True)
        await team.stop_gateway()
        await team.start_gateway()

        guidance = team.send("BRAVO-GUIDANCE", session_key=session)
        await _wait_until(lambda: bool(team.steering_with("BRAVO-GUIDANCE")) or bool(team.root_tasks_named("BRAVO-GUIDANCE")))
        logger.warning(
            "after gateway restart: task_b=%s correlation_b=%s steering=%s new_requests=%s",
            task_b,
            correlation_b,
            team.steering_with("BRAVO-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-GUIDANCE")],
        )
        hold_b.set()
        await _settle(guidance)

        assert team.steering_with("BRAVO-GUIDANCE") == [(task_b, correlation_b)]
        assert team.root_tasks_named("BRAVO-GUIDANCE") == []
        await _wait_until(lambda: turns[1].prompt is not None)
        assert team.lead_model.unexpected == []


async def test_message_while_a_credential_form_waits_is_guidance_for_that_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's rule: a message sent during a credential form wait is guidance."""

    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    def use_broker(config: GatewayInstanceConfig) -> None:
        config.security_layer.mode = "broker"
        config.security_layer.broker_internal_base_url = "http://127.0.0.1:9"

    turns = (_Turn("BRAVO-REQUEST", _credential_form()),)
    async with _team(tmp_path, *turns, configure=use_broker) as team:
        _stub_open_credential_form(team.gateway, monkeypatch)
        session = team.gateway.application.start_new_session().session_key
        form = await asyncio.wait_for(team.send("BRAVO-REQUEST", session_key=session), WAIT_SECONDS)
        task_b = team.root_tasks_named("BRAVO-REQUEST")[0].task_id
        correlation_b = team.request_for(session, "BRAVO-REQUEST").correlation_id
        assert team.gateway.state_store.get_request(correlation_b).status == "waiting_for_credential"

        guidance = team.send("BRAVO-GUIDANCE", session_key=session)
        await _wait_until(lambda: bool(team.steering_with("BRAVO-GUIDANCE")) or bool(team.root_tasks_named("BRAVO-GUIDANCE")))
        logger.warning(
            "during credential form: form_reply_type=%s task_b=%s correlation_b=%s steering=%s new_requests=%s",
            form.message_type,
            task_b,
            correlation_b,
            team.steering_with("BRAVO-GUIDANCE"),
            [task.task_id for task in team.root_tasks_named("BRAVO-GUIDANCE")],
        )
        await _settle(guidance)

        assert team.steering_with("BRAVO-GUIDANCE") == [(task_b, correlation_b)]
        assert team.root_tasks_named("BRAVO-GUIDANCE") == []
        history = team.lead.task_manager.get_task_info(task_b).task.execution_history
        assert any("BRAVO-GUIDANCE" in (entry.summary or "") for entry in history)


def _short_steering_status_wait(gateway: UserGateway) -> None:
    guidance = gateway._thread_guidance
    guidance._settings = dataclasses.replace(guidance._settings, status_timeout_seconds=0.5)


def _recording_emit(emit, delivered: list[str]):
    async def record(event):
        delivered.append(event.text)
        return await emit(event)

    return record


def _stub_open_credential_form(gateway: UserGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker opens the form and the person has not finished it yet."""

    monkeypatch.setattr(
        gateway._secure_broker,
        "create_collection_session",
        AsyncMock(
            return_value={
                "session_id": "broker_session_busy_thread",
                "form_url": "https://broker.example.test/credential-collection/broker_session_busy_thread",
                "status_url": "https://broker.example.test/v1/credential-collection/sessions/broker_session_busy_thread",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        ),
    )
    monkeypatch.setattr(gateway._secure_broker, "user_form_url", lambda value: value)

    async def form_still_open(**_: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway._credential_workflow, "poll_session", form_still_open)
