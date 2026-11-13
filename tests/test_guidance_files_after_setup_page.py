"""Guidance with a file, sent after a delegated task opened a setup page, keeps its file on the request.

Review finding CRIT-1 (PR #138) described this path: a delegated task (a
specialist's task, or a worker's escalation the team lead holds) opens a
setup page; the gateway then points the person's running request at that
task, so the person's next message becomes guidance addressed and signed to
the delegated task, and the lead refuses its file (``reason=delegated_task``).

The gateway never makes that change. A running request keeps its own task:
``update_request_coordination`` changes a row with a run state only for the
task the row already names. So guidance, and the context the gateway signs
for it, stay on the request's own task, and the lead keeps the file there.
This test pins that, end to end, with real parts: the gateway (Web Chat, its
SQLite store, upload storage and setup pages), a team lead and a worker
(``UniversalAgent`` with their admission, question, claim and steering
handlers, turn-action outboxes and SQLite stores). Envelopes pass through
the NATS encoding between them; model turns are played through the agents'
real outboxes.

The scenario:

1. The person sends a request with a file. The lead delegates it to the
   worker, the worker escalates to the lead, and the lead asks the person a
   question in the escalation (a delegated task on the lead).
2. The person answers. While the chat waits for the escalation's reply, the
   escalation sends a setup page (``send_setup_request``, the message
   ``set_up_tools`` sends) with the correlation id delegation gave it. The
   page opens: the gateway saves its wait for the escalation.
3. The escalation sends a second page after the chat stopped waiting for it.
   The chat's credential handoff refuses it (it is not the request's task),
   and the gateway tells the escalation no chat is waiting for it.
4. The person sends a message with a file; the request is still running, so
   it is guidance.
5. The request ends; a restarted lead (its stores reopened) takes a
   follow-up request in the same chat.

The scenario is checked on its own (``test_the_scenario_reaches_each_step``),
so a page refused for another reason, or a correlation id other than the one
delegation gives, fails as a broken setup rather than passing unseen.

The test drives the escalation because it is the delegated task whose page
can open. A specialist's own page never does: only the team lead's questions
reach the person's chat (``message_routing.requester_bound_primary_route``),
so the chat never waits for a specialist's task, and its page goes to the
chat's credential handoff, which refuses any sender but the manager before
the request is touched
(``test_gateway_credential_handoff.py::test_delegated_setup_rejects_untrusted_lineage_even_on_redelivery``).

Run against a copy whose store drops the ``active_task_id`` condition, the
page moves the request to the escalation, the guidance is addressed and
signed to it, and the lead refuses its file as ``delegated_task``. The store
test
``test_gateway_question_store.py::test_a_sibling_task_cannot_change_a_running_request_or_its_conversation``
pins the same condition: its ``coordination`` cases fail without it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from access_control.ownership import steering_context_id
from assistant_gateway.application.tool_setup import REASON_CHAT_NOT_WAITING
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.domain.message_types import TOOL_SETUP_MESSAGE_TYPE
from assistant_gateway.domain.request_state import RUN_STATE_RUNNING, RequestRecord, ToolSetupRecord
from custom_agents import task_message_runtime, task_steering_runtime
from custom_agents.task_context_runtime import context_verifier
from custom_agents.tool_setup_run_runtime import send_setup_request
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import Task, TaskQuery, TaskSteering
from custom_messages.enums import MessageMetaType, RequesterType, TaskSteeringStatus
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_tools.user_attachment_tools import inspect_user_attachment
from runtime_settings.instance_layout import InstanceLayout
from tests.task_work_support import use_agent
from tests.test_conversation_attachments_regression import _answer, _lead
from tests.test_escalation_answer_files import (  # noqa: F401  (autouse fixtures)
    _keys,
    _last_sent,
    _no_background_work,
    _sent,
    _Team,
    _until,
    _worker,
    layout,
)
from tests.test_gateway_parallel_threads import _send, _web_chat_thread
from tests.test_gateway_tool_setup import _Guide
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp
from tests.test_question_answer_files import _prompt, _sections, _web_chat_gateway
from tests.test_turn_files_recording import _files_entries, _messages, _own, _restarted

pytestmark = pytest.mark.integration

PAGE, LATE_PAGE = "tool_setup_req_escalation", "tool_setup_req_escalation_late"
GUIDANCE_TEXT = "Use page two too."


@dataclass(frozen=True)
class _Kept:
    """Where the lead keeps the request's files: its conversation turn, its own files and their history entries."""

    turn_files: list[str]
    own_files: list[str]
    files_entry_messages: list[str]


def _kept(manager: Any, store: Any, task_id: str) -> _Kept:
    return _Kept(
        turn_files=[item["attachment_id"] for item in store.get_turn_by_task_id(task_id)["attachments"]],
        own_files=_own(manager, store, task_id),
        files_entry_messages=[entry.details["message_id"] for entry in _files_entries(manager, task_id)],
    )


@dataclass
class _Scenario:
    """What each step produced (module docstring)."""

    request: MessageEnvelope
    escalation_id: str
    escalation_parent_id: str
    escalation_requester: Any
    pages: list[MessageEnvelope]
    page_response: Any
    page_record: ToolSetupRecord | None
    rows: dict[str, RequestRecord]
    setup_results: list[ToolSetupResultV1]
    handoff_rejected: list[str]
    guidance: list[MessageEnvelope]
    guidance_statuses: list[TaskSteering]
    kept: _Kept
    restarted_kept: _Kept
    refused: list[str]
    follow_up_listed: str
    follow_up_opened: list[dict[str, Any]]
    uploads_dir: Path

    @property
    def root_id(self) -> str:
        return self.request.msg_content.task_id

    @property
    def guidance_file(self) -> str:
        """The file on the person's message (the first guidance)."""

        [attachment] = self.guidance[0].msg_content.attachments
        return attachment.attachment_id


class _SetupPageTeam(_Team):
    """``_Team`` (request, delegation, escalation and its question), then the escalation's setup pages.

    The person's answer is handled by the lead, and the escalation's turn
    sends a setup page while the chat waits for its reply. Guidance reaches
    the lead as NATS delivers it, and the lead's own status goes back to the
    gateway. Setup results the gateway sends are kept for the test.
    """

    def __init__(self, lead: Any, worker: Any) -> None:
        super().__init__(lead, worker)
        self.pages: list[MessageEnvelope] = []
        self.page_response: Any = None
        self.setup_results: list[ToolSetupResultV1] = []
        self.guidance: list[MessageEnvelope] = []
        self.guidance_statuses: list[TaskSteering] = []
        self.forwarded: list[MessageEnvelope] = []

    @property
    def escalation_id(self) -> str:
        assert self.question is not None
        return self.question.msg_content.parent_task_id

    def row(self) -> RequestRecord:
        assert self.request is not None and self.gateway is not None
        return self.gateway.state_store.get_request(self.request.msg_meta.msg_correlation_id)

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        content = envelope.msg_content
        if isinstance(content, ToolSetupResultV1):
            self.setup_results.append(content)
            return
        if isinstance(content, TaskQuery):
            # The person's answer to the escalation's question: the lead takes it and
            # resumes the escalation, whose turn opens a setup page (``escalation_turn``).
            self.answer = decode_envelope(encode_envelope(envelope))
            await task_message_runtime._handle_task_query_message(
                self.lead, self.answer.msg_content, self.answer.msg_header, self.answer.msg_meta
            )
            return
        await super().publish_durable(envelope, routing_key=routing_key)

    async def open_page(self, setup_request_id: str) -> None:
        """The escalation sends a setup page for its own task, as ``set_up_tools`` does."""

        escalation = self.lead.task_manager.get_task_info(self.escalation_id)
        request = ToolSetupRequestV1(
            setup_request_id=setup_request_id,
            parent_task_id=self.escalation_id,
            tool_id="http:gitea",
            kind="external_step",
            owner="person",
            step_id="create_token",
            reason="Create a Gitea token so I can read the recipe repository.",
        )
        await send_setup_request(self.lead, request, escalation)
        page = _last_sent(self.lead, ToolSetupRequestV1)
        self.pages.append(page)
        assert self.gateway is not None
        await self.gateway._manager_client.handle_response(page)

    async def forward(self, envelope: MessageEnvelope) -> None:
        """The lead's operational sends: it passes guidance on to the worker's waiting task."""

        self.forwarded.append(envelope)

    async def escalation_turn(self, *, msg_content: Any, msg_header: Any, msg_meta: Any) -> None:
        """The lead's model on the escalation after the answer: it needs a tool set up (``set_up_tools``)."""

        del msg_header, msg_meta
        assert msg_content.task_id == self.escalation_id, "only the escalation resumes after the answer"
        await self.open_page(PAGE)

    async def publish_operational(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        if not isinstance(envelope.msg_content, TaskSteering):
            await super().publish_operational(envelope, routing_key=routing_key)
            return
        delivered = decode_envelope(encode_envelope(envelope))
        self.guidance.append(delivered)
        sent_before = len(_sent(self.lead))
        await task_steering_runtime.handle_task_steering_message(
            self.lead, delivered.msg_content, delivered.msg_header, delivered.msg_meta
        )
        [status] = [item for _route, item in _sent(self.lead)[sent_before:] if isinstance(item.msg_content, TaskSteering)]
        self.guidance_statuses.append(status.msg_content)
        assert self.gateway is not None
        await self.gateway._manager_client.handle_response(status)


async def _settle(condition: Any) -> None:
    """Give the gateway up to two seconds to answer; the tests check what it did."""

    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.02)


def _follow_up(lead: Any, admitted: list[MessageEnvelope], files: list[str]) -> tuple[str, list[dict[str, Any]]]:
    """The follow-up's file list, and each file opened by id in its turn; empty if no follow-up was admitted."""

    if not admitted:
        return "", []
    info = lead.task_manager.get_task_info(admitted[-1].msg_content.task_id)
    use_agent(lead, info.task.task_id)
    listed = _sections(_prompt(lead, info.task, info.header, info.meta))["conversation_attachments"]
    return listed, [inspect_user_attachment._tool_func(attachment_id=item) for item in files]


@pytest_asyncio.fixture
async def scenario(
    tmp_path: Path, layout: InstanceLayout, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> _Scenario:
    lead, worker = _lead(tmp_path, layout), _worker(tmp_path, layout)
    team = _SetupPageTeam(lead, worker)
    gateway = _web_chat_gateway(tmp_path, layout, team, monkeypatch)
    gateway._credential_workflow.tool_setups.guide = _Guide()
    # The lead's question and the escalation's pages go to the person's chat through the team's gateway route.
    lead.instance_config_path = "instance.yaml"
    monkeypatch.setattr(GatewayInstanceConfig, "load_for_process", lambda _path: gateway.config)
    object.__setattr__(lead, "process_llm_interaction", team.escalation_turn)
    object.__setattr__(lead, "_send_operational_message", team.forward)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())

    with caplog.at_level(logging.INFO):
        async with TestClient(TestServer(app.build_app())) as client:
            thread = await _web_chat_thread(client)
            asked = asyncio.create_task(_send(client, thread, "Read this recipe.", request_id="request", with_file=True))
            await _until(lambda: team.question is not None)
            await asyncio.sleep(0.1)  # the request now waits for its result
            await gateway._manager_client.handle_response(team.question)
            # 2. The answer: the escalation's page opens while the chat waits for its reply.
            answered = await _send(client, thread, "Vegan.", request_id="answer")
            assert answered.status == 200, await answered.text()
            team.page_response = await answered.json()
            # The answer's wait has handled the page by now.
            rows = {PAGE: team.row()}
            # 3. A later page of the escalation, when no chat waits for it.
            await team.open_page(LATE_PAGE)
            await _settle(lambda: bool(team.setup_results))
            rows[LATE_PAGE] = team.row()
            # 4. The person's next message, with a file, while the request runs.
            guided = await _send(client, thread, GUIDANCE_TEXT, request_id="guidance", with_file=True)
            assert guided.status == 200, await guided.text()
            assert team.request is not None
            root_id = team.request.msg_content.task_id
            kept = _kept(lead.task_manager, lead._conversation_store, root_id)
            # The lead restarts while the request runs: its task records and conversation store reopened.
            restarted_kept = _kept(*_restarted(lead, tmp_path), root_id)
            # 5. The request ends; a restarted lead takes a follow-up in the same chat.
            await team.reply(team.request, Task(task_id=root_id, task_type=MessageMetaType.RESPONSE, answer="Done."))
            assert (await asked).status == 200
            await _answer(lead, root_id, "It is vegan.")
            follow_up_lead = _lead(tmp_path, layout)
            team.lead = follow_up_lead
            follow_up = await _send(client, thread, "What was on page two?", request_id="follow_up")
            assert follow_up.status == 200

    escalation = lead.task_manager.get_task_info(team.escalation_id)
    guidance_files = [item.attachment_id for envelope in team.guidance for item in envelope.msg_content.attachments]
    follow_up_listed, follow_up_opened = _follow_up(follow_up_lead, team.later, guidance_files)
    return _Scenario(
        request=team.request,
        escalation_id=team.escalation_id,
        escalation_parent_id=escalation.task.parent_task_id,
        escalation_requester=escalation.meta.requester_type,
        pages=team.pages,
        page_response=team.page_response,
        page_record=gateway.state_store.tool_setup_repository.get_tool_setup(PAGE),
        rows=rows,
        setup_results=team.setup_results,
        handoff_rejected=_messages(caplog, "gateway_credential_handoff_lineage_rejected"),
        guidance=team.guidance,
        guidance_statuses=team.guidance_statuses,
        kept=kept,
        restarted_kept=restarted_kept,
        refused=_messages(caplog, "sent_file_refused"),
        follow_up_listed=follow_up_listed,
        follow_up_opened=follow_up_opened,
        uploads_dir=layout.uploads_dir,
    )


@pytest.mark.asyncio
async def test_the_scenario_reaches_each_step(scenario: _Scenario) -> None:
    correlation_id = scenario.request.msg_meta.msg_correlation_id
    # The escalation is a delegated task on the lead: a worker's task asked for it.
    assert scenario.escalation_requester == RequesterType.AGENT_REQUESTER
    assert scenario.escalation_parent_id and scenario.escalation_parent_id != scenario.root_id
    # Its pages carry the request's correlation id, as delegation gives it.
    assert [page.msg_meta.msg_correlation_id for page in scenario.pages] == [correlation_id, correlation_id]
    assert [page.msg_content.parent_task_id for page in scenario.pages] == [scenario.escalation_id] * 2
    # The first page opened in the chat: the gateway waits on it for the escalation.
    shown = [message.get("message_type") for message in scenario.page_response["messages"]]
    assert shown[-1] == TOOL_SETUP_MESSAGE_TYPE, shown
    assert scenario.page_record is not None
    assert (scenario.page_record.parent_task_id, scenario.page_record.status) == (scenario.escalation_id, "waiting")
    assert scenario.page_record.correlation_id == correlation_id
    # The later page went to the chat's credential handoff, which refused it (it is not the
    # request's task), and the gateway told the escalation no chat waited for it.
    [rejected] = scenario.handoff_rejected
    assert f"request_id={LATE_PAGE} correlation_id={correlation_id}" in rejected
    [late] = scenario.setup_results
    assert (late.setup_request_id, late.status, late.reason_code) == (LATE_PAGE, "unavailable", REASON_CHAT_NOT_WAITING)
    # The person's message became guidance carrying its file.
    [guidance] = scenario.guidance
    assert guidance.msg_content.guidance_text == GUIDANCE_TEXT
    assert len(guidance.msg_content.attachments) == 1


@pytest.mark.asyncio
async def test_a_delegated_tasks_setup_pages_leave_the_running_request_on_its_own_task(scenario: _Scenario) -> None:
    for setup_request_id in (PAGE, LATE_PAGE):
        row = scenario.rows[setup_request_id]
        assert (row.active_task_id, row.run_state) == (scenario.root_id, RUN_STATE_RUNNING), setup_request_id


@pytest.mark.asyncio
async def test_guidance_after_the_page_is_addressed_and_signed_to_the_request_and_accepted(scenario: _Scenario) -> None:
    guidance = scenario.guidance[0]
    steering = guidance.msg_content
    assert steering.target_task_id == scenario.root_id
    assert steering.target_correlation_id == scenario.request.msg_meta.msg_correlation_id
    signed = context_verifier().verify(guidance.msg_meta.gateway_context.task_context)
    assert (signed.root_task_id, signed.context_id) == (scenario.root_id, steering_context_id(steering))
    status = scenario.guidance_statuses[0]
    assert (status.status, status.target_task_id) == (TaskSteeringStatus.ACCEPTED, scenario.root_id)


@pytest.mark.asyncio
async def test_the_guidance_file_stays_on_the_request_through_a_restart_and_a_follow_up(scenario: _Scenario) -> None:
    [request_file] = scenario.request.msg_content.attachments
    guidance_file = scenario.guidance_file
    guidance = scenario.guidance[0]
    expected = _Kept(
        turn_files=[request_file.attachment_id, guidance_file],
        own_files=[request_file.attachment_id, guidance_file],
        files_entry_messages=[guidance.msg_content.steering_id],
    )
    # On the request's turn, among its own files, recorded under the guidance; nothing refused.
    assert scenario.kept == expected
    assert scenario.refused == []
    # The lead restarted while the request ran: the same, from its reopened stores.
    assert scenario.restarted_kept == expected
    # A follow-up in a restarted lead lists it by id, without a path, and opens it.
    assert f"`{guidance_file}` notes.txt" in scenario.follow_up_listed
    assert str(scenario.uploads_dir) not in scenario.follow_up_listed
    [opened] = scenario.follow_up_opened
    assert opened["status"] == "success", opened
