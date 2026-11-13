"""Files a person sends with the answer to a worker's escalation reach the request and that worker.

Only the team lead may ask the person anything. A worker that needs the
person escalates (``manager_escalation``): it delegates a task to the team
lead, which holds it as a delegated task of its own and asks the person in
it. The person's answer names that escalation. Before this change the lead
refused its files (``reason=delegated_task``) and the result it sent the
worker carried none, so neither the request nor the worker could open them.

The end-to-end test runs the real gateway (Web Chat, its store and upload
storage) with a real team lead and a real worker (``UniversalAgent`` with
their admission, question, result and claim handlers, turn-action outboxes,
SQLite stores, context engines and the attachment tools). Envelopes pass
through the NATS encoding between them; each model turn is played by the
test through the agent's real outbox. Covers:

- the lead keeps the answer's file on the escalation and on the request (its
  conversation turn and its own files), and its turn names it under the
  answer;
- the lead's result for the escalation carries that file and nothing else
  (not the request's own file, which the worker already has);
- the worker keeps it as its task's own: listed in its prompt without a
  path, opened by id, and still its own after a restart;
- a follow-up request in the same chat, in a restarted lead, lists and opens it.

An escalation from work the lead never held (a worker's scheduled job run or
maintenance run, a top-level task in the worker) keeps the answer's file on
the escalation; the result carries it, and the worker keeps it as the run's
own and on the run's conversation turn, where the next run lists and opens it.
When the lead delegates work from such an escalation and the specialist doing
it needs the person, the answer's file is kept on the specialist's escalation
and on the run's: the specialist and the run both get it.

The other tests cover the refusals: an escalation whose request, or whose
run's escalation, works for someone else, a run's escalation held by an
agent that is not the team lead, a result that is not for an escalation,
records outside the upload folders, and which results carry files at all.

A worker's own question (a ``TaskQuery`` to the lead) never reaches the
person: the lead's turn on it must answer it (``_validate_task_turn_llm_output``),
so that path has no answer files.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_control.ownership import answer_context_id
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.conversation_attachments import (
    ESCALATION_RESULT_MESSAGE_TYPE,
    QUESTION_ANSWER_MESSAGE_TYPE,
    TaskAttachments,
    task_files_details,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.delegated_work_runtime import handle_delegated_task_claimed
from custom_agents.sent_files_runtime import keep_escalation_result_files, result_with_answer_files
from custom_agents.structured_llm_messages import (
    DelegateTaskAction,
    ModelManagerEscalationAction,
    StructuredAgentTurnDecision,
    StructuredTaskPayload,
    TaskQueryAction,
    TaskResponseAction,
)
from custom_agents.task_context_runtime import context_verifier, sign_system_context
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.delegated_work_v1 import DelegatedTaskClaimedV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, ManagerEscalationContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskStatus
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType, TaskHistoryType
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import forget_task_context_keys, use_gateway_key, use_scheduler_key
from tests.task_work_support import use_agent
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, TEAM, _admit, _answer, _lead
from tests.test_gateway_parallel_threads import _send, _web_chat_thread
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp
from tests.test_question_answer_files import (
    _answer_meta,
    _answer_to,
    _deliver,
    _ModelTurns,
    _point_root_at,
    _prompt,
    _root_working_for_bob,
    _sections,
    _web_chat_gateway,
)
from tests.test_turn_files_recording import _files_entries, _messages, _outside, _own, _upload

pytestmark = pytest.mark.integration

WORKER_ID = "worker_1"
WORKER_TYPE = "WebResearchAgent"
QUESTION = "Which diet must the recipe suit?"
NOW = "2026-11-12T00:00:00+00:00"
MAINTENANCE_CONVERSATION = "conv_maintenance"


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch):
    # The gateway signs with the test gateway key; agents trust it.
    use_gateway_key(monkeypatch)
    yield
    forget_task_context_keys()


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch: pytest.MonkeyPatch):
    # Each test plays the model turns itself.
    monkeypatch.setattr(task_message_runtime, "schedule_pending_work", lambda _agent: None)
    monkeypatch.setattr(task_steering_runtime, "schedule_pending_work", lambda _agent: None)


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


def _worker(tmp_path: Path, layout: InstanceLayout, *, agent_id: str = WORKER_ID):
    """A worker of type ``WORKER_TYPE`` on the same host, with its own stores."""

    from custom_agents.strands_agent import UniversalAgent

    db_path = str(tmp_path / f"{agent_id}.db")
    tasks = TaskManager(db_path=db_path)
    tasks.set_owner_identity(agent_id, "Researcher", TEAM, WORKER_TYPE)
    with (
        patch("custom_agents.strands_agent.Agent"),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        agent = UniversalAgent(
            role="Researcher",
            tools=[],
            team_id=TEAM,
            named_agent_type=WORKER_TYPE,
            is_team_lead=False,
            task_manager=tasks,
            id=agent_id,
            instance_root=str(layout.root),
        )
    sequences = itertools.count(1)
    agent.team_comms = SimpleNamespace(send_message=AsyncMock(side_effect=lambda **_kwargs: next(sequences)))
    agent._conversation_store = ConversationStore(db_path, artifacts_dir=tmp_path / f"{agent_id}_artifacts")
    agent._create_context_engine()
    return agent


def _sent(agent: Any) -> list[tuple[str, MessageEnvelope]]:
    """Every envelope the agent published, as NATS delivers it, with its routing key."""

    return [
        (call.kwargs["routing_key"], decode_envelope(encode_envelope(call.kwargs["message"])))
        for call in agent.team_comms.send_message.await_args_list
    ]


def _last_sent(agent: Any, content_type: type) -> MessageEnvelope:
    return [envelope for _route, envelope in _sent(agent) if isinstance(envelope.msg_content, content_type)][-1]


async def _deliver_claims(sender: Any, receiver: Any) -> None:
    """Hand the receiver the claims the sender sent it (``delegated_work_runtime``)."""

    for route, envelope in _sent(sender):
        if isinstance(envelope.msg_content, DelegatedTaskClaimedV1) and route == receiver.id:
            await handle_delegated_task_claimed(
                receiver, claim=envelope.msg_content, msg_header=envelope.msg_header, msg_meta=envelope.msg_meta
            )


async def _turn(agent: Any, task_id: str, actions: list[Any]) -> Any:
    """Start ``task_id``'s turn and carry out the model's ``actions`` through the agent's outbox."""

    manager = agent.task_manager
    assert manager.get_next_task_to_process() == task_id
    manager.update_main_task_state(task_id, ProgressState.IN_PROGRESS, NOW)
    use_agent(agent, task_id)
    info = manager.get_task_info(task_id)
    return await agent._execute_turn_decision_outbox(
        decision=StructuredAgentTurnDecision(actions=actions),
        incoming_content=info.task,
        incoming_header=info.header,
        incoming_meta=info.meta,
    )


def _escalate(escalation_type: str = "needs_user_input") -> Any:
    return ModelManagerEscalationAction.model_validate(
        {
            "action_type": "manager_escalation",
            "task": {"problem_statement": "Ask the person which diet the recipe must suit."},
            "escalation": {"escalation_type": escalation_type, "reason": QUESTION},
        }
    ).to_runtime_action()


class _Team:
    """The gateway's manager transport: the team lead, and the worker it delegates to.

    The request: the lead delegates it to the worker, the worker escalates
    to the lead, and the lead asks the person in the escalation (the
    question waits in ``question`` for the test to show). The answer: the
    lead handles it and plays the escalation's turn (``_LeadTurns``), which
    sends the worker its result. A later request is admitted and finished.
    """

    connected = True

    def __init__(self, lead: Any, worker: Any) -> None:
        self.lead = lead
        self.worker = worker
        self.gateway: Any = None
        self.request: MessageEnvelope | None = None
        self.question: MessageEnvelope | None = None
        self.answer: MessageEnvelope | None = None
        self.work_id = ""
        self.later: list[MessageEnvelope] = []

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        del routing_key
        envelope = decode_envelope(encode_envelope(envelope))
        content = envelope.msg_content
        if isinstance(content, TaskQuery):
            self.answer = envelope
            await task_message_runtime._handle_task_query_message(
                self.lead, content, envelope.msg_header, envelope.msg_meta
            )
            await self.reply(envelope, Task(task_id=content.parent_task_id, task_type=MessageMetaType.RESPONSE, answer="Done."))
            return
        if self.request is not None:
            self.later.append(envelope)
            await task_message_runtime._handle_task_request(self.lead, content, envelope.msg_header, envelope.msg_meta)
            await self.reply(envelope, Task(task_id=content.task_id, task_type=MessageMetaType.RESPONSE, answer="Done."))
            return
        self.request = envelope
        await self._delegate_and_escalate(envelope)

    async def _delegate_and_escalate(self, envelope: MessageEnvelope) -> None:
        lead, worker = self.lead, self.worker
        await task_message_runtime._handle_task_request(lead, envelope.msg_content, envelope.msg_header, envelope.msg_meta)
        await _turn(
            lead,
            envelope.msg_content.task_id,
            [
                DelegateTaskAction(
                    task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Find a recipe."),
                    target_named_agent_type=WORKER_TYPE,
                    requester_type=RequesterType.AGENT_REQUESTER,
                )
            ],
        )
        delegated = _last_sent(lead, Task)
        self.work_id = delegated.msg_content.task_id
        await task_message_runtime._handle_task_request(worker, delegated.msg_content, delegated.msg_header, delegated.msg_meta)
        await _deliver_claims(worker, lead)
        await _turn(worker, delegated.msg_content.task_id, [_escalate()])
        escalation = _last_sent(worker, Task)
        await task_message_runtime._handle_task_request(lead, escalation.msg_content, escalation.msg_header, escalation.msg_meta)
        await _deliver_claims(lead, worker)
        await _turn(
            lead,
            escalation.msg_content.task_id,
            [TaskQueryAction(task_query=TaskQuery(query_type=MessageMetaType.REQUEST, message=QUESTION))],
        )
        self.question = _last_sent(lead, TaskQuery)

    async def publish_operational(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        # The gateway checks that a question's task is still known.
        request = envelope.msg_content
        if not isinstance(request, TaskStatus) or self.gateway is None:
            return
        known = self.lead.task_manager.get_task_info(request.task_id) is not None
        await self.gateway._manager_client.get_control_channel(envelope.msg_meta.msg_correlation_id).put(
            MessageEnvelope(
                msg_content=request.model_copy(
                    update={"status_type": MessageMetaType.RESPONSE, "is_known_task": known, "responder_agent_id": self.lead.id}
                ),
                msg_header=MessageHeader(from_agent_id=self.lead.id),
                msg_meta=envelope.msg_meta,
            )
        )

    async def reply(self, envelope: MessageEnvelope, content: Any) -> None:
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=content,
                msg_header=MessageHeader(from_agent_id=self.lead.id, agent_signature=""),
                msg_meta=envelope.msg_meta.model_copy(
                    update={"requester_type": RequesterType.USER_REQUESTER, "is_response_requested": False}
                ),
            )
        )


class _LeadTurns:
    """The lead's model after the answer: records the escalation's prompt, then sends the worker its result."""

    def __init__(self, lead: Any, worker: Any) -> None:
        self.lead = lead
        self.worker = worker
        self.prompts: list[Any] = []
        self.result: MessageEnvelope | None = None

    async def __call__(self, *, msg_content: Any, msg_header: MessageHeader, msg_meta: MessageMeta) -> Any:
        use_agent(self.lead, msg_content.task_id)
        self.prompts.append(_prompt(self.lead, msg_content, msg_header, msg_meta))
        sent_before = len(_sent(self.lead))
        result = await self.lead._execute_turn_decision_outbox(
            decision=StructuredAgentTurnDecision(
                actions=[
                    TaskResponseAction(
                        task=StructuredTaskPayload(task_type=MessageMetaType.RESPONSE, answer="Vegetarian; see the list.")
                    )
                ]
            ),
            incoming_content=msg_content,
            incoming_header=msg_header,
            incoming_meta=msg_meta,
        )
        [(route, self.result)] = [item for item in _sent(self.lead)[sent_before:] if isinstance(item[1].msg_content, Task)]
        assert route == self.worker.id
        await task_message_runtime._handle_task_response(
            self.worker, self.result.msg_content, self.result.msg_header, self.result.msg_meta
        )
        return result


class _WorkerTurns:
    """The worker's model when its task resumes: records the prompt and opens each of the task's files by id."""

    def __init__(self, worker: Any) -> None:
        self.worker = worker
        self.prompts: list[Any] = []
        self.opened: dict[str, dict[str, Any]] = {}

    async def __call__(self, *, msg_content: Any, msg_header: MessageHeader, msg_meta: MessageMeta) -> Any:
        use_agent(self.worker, msg_content.task_id)
        self.prompts.append(_prompt(self.worker, msg_content, msg_header, msg_meta))
        info = self.worker.task_manager.get_task_info(msg_content.task_id)
        for attachment in TaskAttachments.load(info, None).own:
            self.opened[attachment.attachment_id] = inspect_user_attachment._tool_func(attachment_id=attachment.attachment_id)
        return None


def _restarted_worker(worker: Any, tmp_path: Path) -> TaskManager:
    manager = TaskManager(db_path=worker.task_manager._db_path)
    manager.set_owner_identity(worker.id, "Researcher", TEAM, WORKER_TYPE)
    assert manager.restore_runtime_state() is not None
    return manager


async def _until(condition: Any) -> None:
    for _ in range(250):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the team never got there")


@pytest.mark.asyncio
async def test_an_answer_to_a_workers_escalation_keeps_its_file_on_the_request_and_sends_it_to_the_worker(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lead, worker = _lead(tmp_path, layout), _worker(tmp_path, layout)
    team = _Team(lead, worker)
    gateway = _web_chat_gateway(tmp_path, layout, team, monkeypatch)
    team.gateway = gateway
    # The lead's question goes to the person's chat through the team's gateway route.
    lead.instance_config_path = "instance.yaml"
    monkeypatch.setattr(GatewayInstanceConfig, "load_for_process", lambda _path: gateway.config)
    lead_turns, worker_turns = _LeadTurns(lead, worker), _WorkerTurns(worker)
    object.__setattr__(lead, "process_llm_interaction", lead_turns)
    object.__setattr__(worker, "process_llm_interaction", worker_turns)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())

    with caplog.at_level(logging.INFO):
        async with TestClient(TestServer(app.build_app())) as client:
            thread = await _web_chat_thread(client)
            # The request has a file of its own; the worker gets it with its task.
            asked = asyncio.create_task(_send(client, thread, "Find a dinner recipe.", request_id="request", with_file=True))
            await _until(lambda: team.question is not None)
            await asyncio.sleep(0.1)  # the request now waits for its result
            await gateway._manager_client.handle_response(team.question)
            answered = await _send(client, thread, "Vegetarian. Use this list.", request_id="answer", with_file=True)
            assert answered.status == 200, await answered.text()
            request_id = team.request.msg_content.task_id
            await team.reply(team.request, Task(task_id=request_id, task_type=MessageMetaType.RESPONSE, answer="Here is one."))
            assert (await asked).status == 200

            [request_file] = team.request.msg_content.attachments
            answer = team.answer.msg_content
            [notes] = answer.attachments
            escalation_id = team.question.msg_content.parent_task_id
            work_id = team.work_id
            # The person answered the lead's question in the escalation, under a context bound to it and the file.
            assert answer.parent_task_id == escalation_id
            signed = context_verifier().verify(team.answer.msg_meta.gateway_context.task_context)
            assert (signed.root_task_id, signed.context_id) == (escalation_id, answer_context_id(answer))

            # The lead keeps the file on the request: its turn and its own files (the
            # escalation keeps it too; it has ended, and its result below carries it).
            manager, store = lead.task_manager, lead._conversation_store
            assert [item["attachment_id"] for item in store.get_turn_by_task_id(request_id)["attachments"]] == [
                request_file.attachment_id,
                notes.attachment_id,
            ]
            [entry] = _files_entries(manager, request_id)
            assert (entry.details["message_id"], entry.details["message_type"]) == (answer.task_query_id, QUESTION_ANSWER_MESSAGE_TYPE)
            assert escalation_id in entry.summary
            assert notes.attachment_id in _own(manager, store, request_id)
            assert not _messages(caplog, "sent_file_refused")
            # Its turn on the escalation names the file under the answer, never by path.
            [escalation_prompt] = lead_turns.prompts
            assert f"`{notes.attachment_id}` notes.txt (text/plain, document, 5 bytes)" in _sections(escalation_prompt)["task_history"]
            assert str(layout.uploads_dir) not in escalation_prompt.prompt

            # The result carries the answer's file and nothing else.
            result = lead_turns.result.msg_content
            assert (result.task_id, result.parent_task_id, result.attachments) == (escalation_id, work_id, [notes])

            # The worker keeps it as its task's own: listed without a path, opened by id.
            [files_entry] = _files_entries(worker.task_manager, work_id)
            assert (files_entry.details["message_id"], files_entry.details["message_type"]) == (
                escalation_id,
                ESCALATION_RESULT_MESSAGE_TYPE,
            )
            assert _own(worker.task_manager, None, work_id) == [request_file.attachment_id, notes.attachment_id]
            [worker_prompt] = worker_turns.prompts
            assert f"`{notes.attachment_id}` notes.txt" in _sections(worker_prompt)["conversation_attachments"]
            assert str(layout.uploads_dir) not in worker_prompt.prompt
            opened = worker_turns.opened[notes.attachment_id]
            assert opened["status"] == "success", opened
            assert str(layout.uploads_dir) not in opened["content"][0]["text"]
            # After a restart it is still the worker task's own.
            assert notes.attachment_id in _own(_restarted_worker(worker, tmp_path), None, work_id)

            # A follow-up request in the same chat, in a restarted lead, lists it and opens it by id.
            await _answer(lead, request_id, "Here is one.")
            follow_up_lead = _lead(tmp_path, layout)
            team.lead = follow_up_lead
            follow_up = await _send(client, thread, "What was on that list?", request_id="follow_up")
            assert follow_up.status == 200
    follow_up_id = team.later[-1].msg_content.task_id
    info = follow_up_lead.task_manager.get_task_info(follow_up_id)
    use_agent(follow_up_lead, follow_up_id)
    listed = _sections(_prompt(follow_up_lead, info.task, info.header, info.meta))["conversation_attachments"]
    assert f"`{notes.attachment_id}` notes.txt" in listed
    assert inspect_user_attachment._tool_func(attachment_id=notes.attachment_id)["status"] == "success"


# --- An escalation from work the lead never held ------------------------------------------------


@pytest.mark.asyncio
async def test_an_answer_to_a_job_runs_escalation_keeps_its_file_on_the_escalation_and_on_the_runs_turn(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker's own top-level task escalates, so the lead holds no task the escalation runs for.

    A scheduled job run or maintenance run is such a task: the worker's
    process admits it (``scheduled_runtime``) and records it as a turn of its
    own conversation, and the lead never holds it. Here the run is Ann's
    top-level task admitted through the worker's real admission, which
    records the same turn. Before this change the lead refused the answer's
    file (``reason=delegated_task``) and its result carried none. The answer
    reaches the lead as the gateway signs it; the end-to-end test above
    covers the gateway's side.
    """

    lead, worker = _lead(tmp_path, layout), _worker(tmp_path, layout)
    lead_turns, worker_turns = _LeadTurns(lead, worker), _WorkerTurns(worker)
    object.__setattr__(lead, "process_llm_interaction", lead_turns)
    object.__setattr__(worker, "process_llm_interaction", worker_turns)
    await _admit(worker, task_id="task_run", text="Plan this week's dinners.", attachments=[])
    await _turn(worker, "task_run", [_escalate()])
    escalation = _last_sent(worker, Task)
    escalation_id = escalation.msg_content.task_id
    await task_message_runtime._handle_task_request(lead, escalation.msg_content, escalation.msg_header, escalation.msg_meta)
    await _deliver_claims(lead, worker)
    manager = lead.task_manager
    # The lead holds the escalation, for Ann, and no task it runs for.
    held = manager.get_task_info(escalation_id)
    assert (held.task.parent_task_id, held.owner_person_id) == ("task_run", ANN)
    assert held.meta.manager_escalation is not None
    assert manager.resolve_root_task_id(escalation_id) == escalation_id
    # The lead asks Ann in the escalation, and she answers with a file.
    assert manager.get_next_task_to_process() == escalation_id
    manager.update_main_task_state(escalation_id, ProgressState.IN_PROGRESS, NOW)
    asked = TaskQuery(task_query_id="task_query_list", query_type=MessageMetaType.REQUEST, parent_task_id=escalation_id, message=QUESTION)
    manager.add_task_query(escalation_id, asked, agent_name="Lead", agent_id=lead.id)
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes], task_id=escalation_id)

    with caplog.at_level(logging.INFO):
        await _deliver(lead, answer, _answer_meta(answer, task_id=escalation_id))

    # The lead keeps it on the escalation (which ends with the result it sends): its turn names
    # the file under the stored answer, never by path, and its result carries the file of its
    # answer files entry (``answer_files``) to the worker.
    assert not _messages(caplog, "sent_file_refused")
    [escalation_prompt] = lead_turns.prompts
    assert f"`{notes.attachment_id}` list.png" in _sections(escalation_prompt)["task_history"]
    assert str(layout.uploads_dir) not in escalation_prompt.prompt
    result = lead_turns.result.msg_content
    assert (result.task_id, result.parent_task_id, result.attachments) == (escalation_id, "task_run", [notes])

    # The worker keeps it as the run's own and on the run's conversation turn.
    [files_entry] = _files_entries(worker.task_manager, "task_run")
    assert (files_entry.details["message_id"], files_entry.details["message_type"]) == (
        escalation_id,
        ESCALATION_RESULT_MESSAGE_TYPE,
    )
    store = worker._conversation_store
    assert [item["attachment_id"] for item in store.get_turn_by_task_id("task_run")["attachments"]] == [notes.attachment_id]
    assert any("task_id=task_run" in message for message in _messages(caplog, "conversation_turn_attachments_merged"))
    assert notes.attachment_id in _own(worker.task_manager, store, "task_run")
    [worker_prompt] = worker_turns.prompts
    assert f"`{notes.attachment_id}` list.png" in _sections(worker_prompt)["conversation_attachments"]
    assert str(layout.uploads_dir) not in worker_prompt.prompt
    assert worker_turns.opened[notes.attachment_id]["status"] == "success"
    # After a restart it is still the run's.
    assert notes.attachment_id in _own(_restarted_worker(worker, tmp_path), None, "task_run")
    # The next turn of the run's conversation (as the schedule's next run is) lists it and opens it by id.
    await _answer(worker, "task_run", "Planned.")
    await _admit(worker, task_id="task_next_run", text="Plan next week's dinners.", attachments=[])
    info = worker.task_manager.get_task_info("task_next_run")
    use_agent(worker, "task_next_run")
    listed = _sections(_prompt(worker, info.task, info.header, info.meta))["conversation_attachments"]
    assert f"`{notes.attachment_id}` list.png" in listed
    assert inspect_user_attachment._tool_func(attachment_id=notes.attachment_id)["status"] == "success"


@pytest.mark.asyncio
async def test_an_answer_to_a_specialists_escalation_for_a_job_runs_escalation_reaches_the_specialist_and_the_run(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """The lead delegates work from a job run's escalation, and the specialist doing it needs the person.

    The specialist's escalation is the lead's delegated task under the work it
    delegated, under the run's escalation, whose parent (the run) the lead
    never held: the task manager's links end at the run's escalation. That
    escalation is the work's request, as the lead's own request is for work
    delegated from it. Before this change the lead refused the answer's file
    (``reason=delegated_task``, ``sent_files_root_not_usable reason=delegated``).
    """

    lead, runner = _lead(tmp_path, layout), _worker(tmp_path, layout)
    specialist = _worker(tmp_path, layout, agent_id="worker_2")
    lead_turns, specialist_turns = _LeadTurns(lead, specialist), _WorkerTurns(specialist)
    object.__setattr__(lead, "process_llm_interaction", lead_turns)
    object.__setattr__(specialist, "process_llm_interaction", specialist_turns)
    manager = lead.task_manager
    # Ann's job run asks the lead for a specialist.
    await _admit(runner, task_id="task_run", text="Plan this week's dinners.", attachments=[])
    await _turn(runner, "task_run", [_escalate("needs_specialist_support")])
    sent = _last_sent(runner, Task)
    run_escalation_id = sent.msg_content.task_id
    await task_message_runtime._handle_task_request(lead, sent.msg_content, sent.msg_header, sent.msg_meta)
    await _deliver_claims(lead, runner)
    # The lead delegates the work to the specialist.
    await _turn(
        lead,
        run_escalation_id,
        [
            DelegateTaskAction(
                task=StructuredTaskPayload(task_type=MessageMetaType.REQUEST, problem_statement="Plan the dinners."),
                target_named_agent_type=WORKER_TYPE,
                requester_type=RequesterType.AGENT_REQUESTER,
            )
        ],
    )
    sent = _last_sent(lead, Task)
    work_id = sent.msg_content.task_id
    await task_message_runtime._handle_task_request(specialist, sent.msg_content, sent.msg_header, sent.msg_meta)
    await _deliver_claims(specialist, lead)
    # The specialist needs Ann.
    await _turn(specialist, work_id, [_escalate()])
    sent = _last_sent(specialist, Task)
    escalation_id = sent.msg_content.task_id
    await task_message_runtime._handle_task_request(lead, sent.msg_content, sent.msg_header, sent.msg_meta)
    await _deliver_claims(lead, specialist)
    held = manager.get_task_info(escalation_id)
    assert (held.task.parent_task_id, held.owner_person_id) == (work_id, ANN)
    assert held.meta.manager_escalation is not None
    assert manager.resolve_root_task_id(escalation_id) == run_escalation_id
    # The lead asks Ann in the specialist's escalation, and she answers with a file.
    assert manager.get_next_task_to_process() == escalation_id
    manager.update_main_task_state(escalation_id, ProgressState.IN_PROGRESS, NOW)
    asked = TaskQuery(task_query_id="task_query_list", query_type=MessageMetaType.REQUEST, parent_task_id=escalation_id, message=QUESTION)
    manager.add_task_query(escalation_id, asked, agent_name="Lead", agent_id=lead.id)
    data = _upload(layout, "data.csv")
    answer = _answer_to([data], task_id=escalation_id)

    with caplog.at_level(logging.INFO):
        await _deliver(lead, answer, _answer_meta(answer, task_id=escalation_id))

    assert not _messages(caplog, "sent_file_refused")
    assert not _messages(caplog, "sent_files_root_not_usable")
    # Kept on the run's escalation, which still waits for the work, as an answer's files.
    [run_entry] = _files_entries(manager, run_escalation_id)
    assert (run_entry.details["message_id"], run_entry.details["message_type"]) == (
        answer.task_query_id,
        QUESTION_ANSWER_MESSAGE_TYPE,
    )
    assert escalation_id in run_entry.summary
    # And on the specialist's escalation (which ends with the result it sends): its result carries
    # the file of its answer files entry to the specialist, which keeps it as the work's own.
    result = lead_turns.result.msg_content
    assert (result.task_id, result.parent_task_id, result.attachments) == (escalation_id, work_id, [data])
    assert data.attachment_id in _own(specialist.task_manager, None, work_id)
    assert specialist_turns.opened[data.attachment_id]["status"] == "success"
    # The run's escalation's result carries it to the run, which keeps it as its own and on its turn.
    reply = await result_with_answer_files(
        lead,
        Task(task_id=run_escalation_id, parent_task_id="task_run", task_type=MessageMetaType.RESPONSE, answer="Planned."),
        manager.get_task_info(run_escalation_id).task,
    )
    assert reply.attachments == [data]
    await keep_escalation_result_files(runner, runner.task_manager.get_task_info(run_escalation_id), reply)
    store = runner._conversation_store
    assert data.attachment_id in _own(runner.task_manager, store, "task_run")
    assert [item["attachment_id"] for item in store.get_turn_by_task_id("task_run")["attachments"]] == [data.attachment_id]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        # Bob's run's escalation: the work under it is Bob's.
        ("other_person", "other_person"),
        # Ann's, but held by an agent that is not the team lead: only the lead takes escalations.
        ("not_the_team_lead", "delegated"),
    ],
)
@pytest.mark.asyncio
async def test_an_escalation_under_a_run_escalation_that_cannot_take_them_keeps_no_files_from_the_answer(
    case: str, reason: str, tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    lead = _lead(tmp_path, layout)
    manager = lead.task_manager
    if case == "not_the_team_lead":
        lead.is_team_lead = False
    # A run's escalation, work delegated from it, and Ann's escalation under that work.
    assert manager.add_task(
        Task(task_id="task_run_escalation", parent_task_id="task_a_run", task_type=MessageMetaType.REQUEST, problem_statement="Need a researcher."),
        MessageHeader(from_agent_id=WORKER_ID, response_routing_key=WORKER_ID),
        MessageMeta(conversation_id="conv_run", manager_escalation=ManagerEscalationContext(escalation_type="needs_specialist_support")),
        owner_person_id="usr_bob" if case == "other_person" else ANN,
    )
    assert manager.add_subtask(
        "task_run_escalation",
        Task(task_id="task_work", task_type=MessageMetaType.REQUEST, problem_statement="Research."),
        MessageHeader(from_agent_id=lead.id, to_routing_path="worker_2"),
        MessageMeta(conversation_id="conv_run"),
    )
    escalation = _escalation_held_by_under(lead, "task_work")
    assert manager.resolve_root_task_id(escalation.task.task_id) == "task_run_escalation"
    object.__setattr__(lead, "process_llm_interaction", _ModelTurns(lead))
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes], task_id="task_escalation")

    with caplog.at_level(logging.WARNING):
        await _deliver(lead, answer, _answer_meta(answer, task_id="task_escalation"))

    assert lead.task_manager.get_task_queries("task_escalation")[-1].attachments == []
    assert all(_files_entries(manager, task_id) == [] for task_id in ("task_escalation", "task_run_escalation"))
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=delegated_task attachment_id={notes.attachment_id} task_id=task_escalation" in refused
    [warning] = _messages(caplog, "sent_files_root_not_usable")
    assert f"task_id=task_escalation root_task_id=task_run_escalation reason={reason}" in warning


# --- Refusals -------------------------------------------------------------------------------------


def _escalation_held_by(agent: Any, *, escalation_id: str = "task_escalation") -> Any:
    """The lead's record of a worker's escalation about Ann's request (TASK_ADMITTED), as admission keeps it."""

    manager = agent.task_manager
    work = manager.add_subtask(
        "task_recipe",
        Task(task_id="task_work", task_type=MessageMetaType.REQUEST, problem_statement="Find a recipe."),
        MessageHeader(from_agent_id=agent.id, to_routing_path=WORKER_ID),
        MessageMeta(conversation_id=CONVERSATION),
    )
    assert work is not None
    return _escalation_held_by_under(agent, "task_work", escalation_id=escalation_id)


def _escalation_held_by_under(agent: Any, work_id: str, *, escalation_id: str = "task_escalation") -> Any:
    """The lead's record of Ann's escalation from the worker doing ``work_id``, as admission keeps it."""

    manager = agent.task_manager
    assert manager.add_task(
        Task(
            task_id=escalation_id,
            parent_task_id=work_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="Ask which diet.",
        ),
        MessageHeader(from_agent_id=WORKER_ID, response_routing_key=WORKER_ID),
        MessageMeta(conversation_id=CONVERSATION, manager_escalation=ManagerEscalationContext(escalation_type="needs_user_input")),
        owner_person_id=ANN,
    )
    return manager.get_task_info(escalation_id)


@pytest.mark.asyncio
async def test_an_escalation_about_someone_elses_request_keeps_no_files_from_the_answer(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lead = _lead(tmp_path, layout)
    await _admit(lead, task_id="task_recipe", text="Find a recipe.", attachments=[])
    escalation = _escalation_held_by(lead)
    assert lead.task_manager.resolve_root_task_id("task_escalation") == "task_recipe"
    _point_root_at(lead.task_manager, escalation, _root_working_for_bob(lead.task_manager, escalation, monkeypatch), monkeypatch)
    object.__setattr__(lead, "process_llm_interaction", _ModelTurns(lead))
    notes = _upload(layout, "list.png")
    answer = _answer_to([notes], task_id="task_escalation")

    with caplog.at_level(logging.WARNING):
        await _deliver(lead, answer, _answer_meta(answer, task_id="task_escalation"))

    # The answer's text is kept without its file, and no task holds the file.
    assert lead.task_manager.get_task_queries("task_escalation")[-1].attachments == []
    assert all(_files_entries(lead.task_manager, task_id) == [] for task_id in ("task_escalation", "task_bob", "task_recipe"))
    # An escalation linked to a request here keeps none when that request cannot take them.
    [refused] = _messages(caplog, "sent_file_refused")
    assert f"reason=delegated_task attachment_id={notes.attachment_id} task_id=task_escalation" in refused
    [warning] = _messages(caplog, "sent_files_root_not_usable")
    assert "task_id=task_escalation root_task_id=task_bob reason=other_person" in warning


@pytest.mark.asyncio
async def test_only_a_delegated_tasks_result_carries_the_files_of_its_answers(
    tmp_path: Path, layout: InstanceLayout
) -> None:
    lead = _lead(tmp_path, layout)
    await _admit(lead, task_id="task_recipe", text="Find a recipe.", attachments=[])
    escalation = _escalation_held_by(lead)
    manager = lead.task_manager
    notes, page = _upload(layout, "list.png"), _upload(layout, "page.png")
    for task_id in ("task_escalation", "task_recipe"):
        manager.append_history_entry(
            task_id,
            "Files sent with an answer to a question accepted: list.png",
            entry_type=TaskHistoryType.TASK_EVENT,
            details=task_files_details([notes], message_id="task_query_list", message_type=QUESTION_ANSWER_MESSAGE_TYPE),
        )
    # Files of another kind of message are not an answer's.
    manager.append_history_entry(
        "task_escalation",
        "Files sent with a guidance accepted: page.png",
        entry_type=TaskHistoryType.TASK_EVENT,
        details=task_files_details([page], message_id="steer_1", message_type="proactive_guidance"),
    )
    escalation_task = manager.get_task_info("task_escalation").task
    request_task = manager.get_task_info("task_recipe").task

    def result(task: Task) -> Task:
        return Task(task_id=task.task_id, parent_task_id=task.parent_task_id, task_type=MessageMetaType.RESPONSE, answer="Done.")

    carried = await result_with_answer_files(lead, result(escalation_task), escalation_task)
    assert carried.attachments == [notes]
    # The request's own result goes to the gateway without files.
    assert (await result_with_answer_files(lead, result(request_task), request_task)).attachments == []
    # A reply to a question carries none either.
    reply = TaskQuery(query_type=MessageMetaType.RESPONSE, parent_task_id="task_escalation", message="Vegetarian.")
    assert (await result_with_answer_files(lead, reply, escalation_task)).attachments == []
    assert escalation.task.task_id == "task_escalation"


@pytest.mark.asyncio
async def test_a_worker_keeps_files_only_from_its_escalations_results_and_only_from_the_upload_folders(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _worker(tmp_path, layout)
    manager = worker.task_manager
    assert manager.add_task(
        Task(task_id="task_work", parent_task_id="task_recipe", task_type=MessageMetaType.REQUEST, problem_statement="Find a recipe."),
        MessageHeader(from_agent_id="lead", response_routing_key="lead"),
        MessageMeta(conversation_id=CONVERSATION),
        owner_person_id=ANN,
    )

    def subtask(task_id: str, *, escalation: bool) -> Any:
        return manager.add_subtask(
            "task_work",
            # The delegation runtime links a subtask to its parent before recording it.
            Task(task_id=task_id, parent_task_id="task_work", task_type=MessageMetaType.REQUEST, problem_statement="Help."),
            MessageHeader(from_agent_id=WORKER_ID),
            MessageMeta(
                conversation_id=CONVERSATION,
                manager_escalation=ManagerEscalationContext(escalation_type="needs_user_input") if escalation else None,
            ),
        )

    helper, escalation = subtask("task_helper", escalation=False), subtask("task_escalation", escalation=True)
    notes, other = _upload(layout, "list.png"), _upload(layout, "other.png")

    def result(info: Any, files: list[Any]) -> Task:
        return Task(
            task_id=info.task.task_id, parent_task_id="task_work", task_type=MessageMetaType.RESPONSE, answer="Done.", attachments=files
        )

    with caplog.at_level(logging.WARNING):
        # Another worker's result brings no files: only the team lead's result for an escalation does.
        await keep_escalation_result_files(worker, helper, result(helper, [other]))
        await keep_escalation_result_files(worker, escalation, result(escalation, [notes, _outside(layout)]))

    assert _own(manager, None, "task_work") == [notes.attachment_id]
    [entry] = _files_entries(manager, "task_work")
    assert (entry.details["message_id"], entry.details["message_type"]) == ("task_escalation", ESCALATION_RESULT_MESSAGE_TYPE)
    [refused] = _messages(caplog, "result_file_refused")
    assert f"reason=not_an_escalation attachment_id={other.attachment_id} task_id=task_work subtask_id=task_helper" in refused
    [outside] = _messages(caplog, "attachment_outside_upload_folders")
    assert "task_id=task_work" in outside


async def _admit_maintenance_run(worker: Any, task_id: str) -> None:
    """Admit a maintenance run as the scheduler signs one: system work for no person, in its own conversation."""

    meta = MessageMeta(
        conversation_id=MAINTENANCE_CONVERSATION,
        msg_correlation_id=f"corr_{task_id}",
        requester_type=RequesterType.AGENT_REQUESTER,
        gateway_context=GatewayContext(),
    )
    sign_system_context(meta, team_id=TEAM, schedule_id="sched_maintenance")
    task = Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Weekly maintenance check.")
    await task_message_runtime._handle_task_request(
        worker, task, MessageHeader(from_agent_id="scheduler", response_routing_key="scheduler_response"), meta
    )


@pytest.mark.asyncio
async def test_a_maintenance_run_keeps_none_of_the_files_its_escalations_answer_brought(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SEC-1: work for no person must not keep an administrator's answer files.

    A maintenance run works for no person. The team lead answers its
    escalation for the maintenance schedule's owner, an administrator
    (``task_context_runtime.sign_system_escalation``, covered in
    ``test_internal_improvement_access``), and its result carries the files
    the administrator answered with. Before this change the run kept them,
    and so did its ownerless conversation: the next run for no person listed
    and opened them, and could copy them into ``workspaces/agents``, which
    every person's work reads. A run for a person keeps them (the job-run
    test above).
    """

    use_scheduler_key(monkeypatch)
    worker = _worker(tmp_path, layout)
    object.__setattr__(worker, "access_role", "system")  # a built-in system agent runs maintenance
    await _admit_maintenance_run(worker, "task_maintenance_1")
    assert worker.task_manager.get_task_info("task_maintenance_1").owner_person_id is None
    await _turn(worker, "task_maintenance_1", [_escalate()])
    [escalation] = [item for item in worker.task_manager.get_subtasks("task_maintenance_1") if item.meta.manager_escalation]
    # The administrator's upload, as the gateway stored it from their own chat.
    private = _upload(layout, "admin-private.png", session_key="web:admin-session", conversation_id="conv_admin")
    result = Task(
        task_id=escalation.task.task_id,
        parent_task_id="task_maintenance_1",
        task_type=MessageMetaType.RESPONSE,
        answer="Here is the file.",
        attachments=[private],
    )

    with caplog.at_level(logging.WARNING):
        await keep_escalation_result_files(worker, escalation, result)

    store = worker._conversation_store
    assert _files_entries(worker.task_manager, "task_maintenance_1") == []
    assert _own(worker.task_manager, store, "task_maintenance_1") == []
    assert store.get_turn_by_task_id("task_maintenance_1")["attachments"] == []
    [refused] = _messages(caplog, "result_file_refused")
    assert refused.endswith(
        f"reason=work_for_a_person_required attachment_id={private.attachment_id} task_id=task_maintenance_1 "
        f"subtask_id={escalation.task.task_id}"
    )
    # The next run for no person in the same conversation can neither list, open nor copy it.
    await _admit_maintenance_run(worker, "task_maintenance_2")
    later = worker.task_manager.get_task_info("task_maintenance_2")
    scope = TaskAttachments.for_task(worker.task_manager, later, store, is_team_lead=False)
    assert scope.conversation_id == MAINTENANCE_CONVERSATION
    assert scope.find([private.attachment_id]) == ([], [private.attachment_id])
    assert scope.choices() == []
    use_agent(worker, "task_maintenance_2")
    for copy in (False, True):
        opened = inspect_user_attachment._tool_func(attachment_id=private.attachment_id, copy_to_workspace=copy)
        assert opened["status"] == "error" and '"result": "unknown_attachment_id"' in opened["content"][0]["text"]
    assert not (layout.workspaces_dir / "agents" / "uploads").exists()
