"""A lead passes a person's guidance on to the workers holding its delegated subtasks (plan decision 59).

Each forwarded copy goes to the holder's own control mailbox. Who holds a subtask comes
from the real stores: the lead's task records, team-wide task custody and the delegated-task
records (SQLite, and PostgreSQL when ``LIVE_TEST_POSTGRES_DSN`` is set). Only the transport
is a recorder. The holder used to come from the lead's claim records keyed by the lead's
current id, which missed a lead restarted with a new id, a claim not recorded yet and a
takeover whose claim was not recorded yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from custom_agents.delegated_work_runtime import delegated_tasks, record_publication
from custom_agents.task_manager import TaskManager
from custom_agents.task_steering_runtime import handle_task_steering_message
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    RoutingType,
    TaskSteeringStatus,
    TaskSteeringType,
)
from custom_transport.subjects import NatsSubjectMapper
from tests.knowledge_support import BACKENDS, knowledge_storage

TEAM = "team_guidance"
GATEWAY = "team_guidance_gateway_response"
LEAD = "lead_now"
PARENT, CHILD = "task_parent", "task_child"
WORKER_TYPE = "WebResearchAgent"
MAPPER = NatsSubjectMapper(team_id=TEAM)


@dataclass
class Sent:
    routing_key: str
    envelope: MessageEnvelope


class RecordingComms:
    """The lead's transport: records each durable send."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []

    async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
        self.sent.append(Sent(routing_key, message))

    def forwarded(self) -> list[Sent]:
        return [item for item in self.sent if isinstance(item.envelope.msg_content, TaskSteering)
                and item.envelope.msg_content.steering_type == TaskSteeringType.PROACTIVE_GUIDANCE]

    def answers(self) -> list[TaskSteering]:
        return [item.envelope.msg_content for item in self.sent if item.routing_key == GATEWAY]


@pytest.fixture(params=BACKENDS)
def storage(request, tmp_path: Path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield settings


def _lead(storage, *, directory: Optional[dict[str, str]] = None, asked_by_name: Optional[str] = None) -> Any:
    """The lead holding a request whose subtask waits on a worker, as after it delegated it."""

    manager = TaskManager(storage_settings=storage)
    manager.set_owner_identity(LEAD, "Lead", TEAM, "TeamArchitect")
    manager.add_task(Task(task_id=PARENT, task_type=MessageMetaType.REQUEST, problem_statement="Find soup recipes."),
                     MessageHeader(from_agent_id=GATEWAY), MessageMeta(msg_correlation_id="corr_parent"),
                     owner_person_id=None)
    manager.add_subtask(
        PARENT,
        Task(task_id=CHILD, parent_task_id=PARENT, task_type=MessageMetaType.REQUEST, problem_statement="Research."),
        MessageHeader(from_agent_id=LEAD, to_routing_path=f"{TEAM}_{WORKER_TYPE}"),
        MessageMeta(msg_correlation_id="corr_parent", to_named_agent_type=WORKER_TYPE, to_agent_name=asked_by_name),
    )
    return SimpleNamespace(
        id=LEAD, name="Lead", named_agent_type="TeamArchitect", team_id=TEAM,
        mid_turn_steering={"enabled": True}, task_manager=manager,
        _pending_proactive_task_steering={}, _processed_task_steering_ids=set(),
        _agent_id_by_name=dict(directory or {}), named_agent_type_queue_map={WORKER_TYPE: f"{TEAM}_{WORKER_TYPE}"},
        team_comms=RecordingComms(), _logger=logging.getLogger("test.guidance.lead"),
    )


def _published_by(lead: Any, requester_id: str, *, asked_by_name: Optional[str] = None) -> Any:
    """The delegated-task record, written by the lead under ``requester_id`` (an earlier id after a restart)."""

    publisher = SimpleNamespace(id=requester_id, team_id=TEAM, task_manager=lead.task_manager)
    record_publication(publisher, subtask_id=CHILD, parent_task_id=PARENT, named_agent_type=WORKER_TYPE,
                       target_agent_name=asked_by_name, publish_sequence=7)
    return publisher


def _claim_recorded(publisher: Any, worker_id: str) -> None:
    delegated_tasks(publisher).record_claim(team_id=TEAM, task_id=CHILD, worker_agent_id=worker_id,
                                            worker_agent_name=worker_id, claimed_at=datetime.now(timezone.utc))


def _admitted_by(storage, worker_id: str) -> None:
    """The worker's own saved work holds the subtask: its team-wide custody row."""

    worker = TaskManager(storage_settings=storage)
    worker.set_owner_identity(worker_id, worker_id, TEAM, WORKER_TYPE)
    assert worker.add_task(Task(task_id=CHILD, parent_task_id=PARENT, task_type=MessageMetaType.REQUEST),
                           MessageHeader(from_agent_id=LEAD), MessageMeta(), owner_person_id=None)


def _guidance(key: str = "corr_guidance") -> tuple[TaskSteering, MessageHeader, MessageMeta]:
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE, target_task_id=PARENT, target_agent_id=LEAD,
        guidance_text="Only vegetarian soups.", idempotency_key=key,
        replied_message=RepliedMessage(author="assistant", text="Soup recipes found: 3."),
    )
    header = MessageHeader(from_agent_id=GATEWAY, to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                           response_routing_key=GATEWAY)
    meta = MessageMeta(msg_correlation_id=key, conversation_id="conversation_1",
                       requester_type=RequesterType.USER_REQUESTER, is_response_requested=True)
    return steering, header, meta


def _forwarded_to(lead: Any, holder: str) -> TaskSteering:
    [sent] = lead.team_comms.forwarded()
    assert sent.routing_key == holder
    assert MAPPER.route_subject(sent.routing_key, envelope=sent.envelope) == MAPPER.agent_control_subject(holder)
    forwarded = sent.envelope.msg_content
    assert (forwarded.target_task_id, forwarded.idempotency_key) == (CHILD, f"corr_guidance:{CHILD}")
    assert sent.envelope.msg_header.idempotency_key == f"corr_guidance:{CHILD}"
    assert forwarded.guidance_text == "Only vegetarian soups."
    assert forwarded.replied_message == RepliedMessage(author="assistant", text="Soup recipes found: 3.")
    assert sent.envelope.msg_meta.to_named_agent_type == WORKER_TYPE
    assert sent.envelope.msg_meta.is_response_requested is False
    return forwarded


@pytest.mark.asyncio
async def test_a_lead_restarted_with_a_new_id_finds_the_worker_holding_its_subtask(storage) -> None:
    """Its records name its earlier id (``requester_agent_id``); the worker's custody names the holder."""

    lead = _lead(storage)
    _claim_recorded(_published_by(lead, "lead_before_restart"), "worker_1")
    _admitted_by(storage, "worker_1")

    await handle_task_steering_message(lead, *_guidance())

    _forwarded_to(lead, "worker_1")
    [answer] = lead.team_comms.answers()
    assert answer.status == TaskSteeringStatus.ACCEPTED
    assert answer.status_reason == "Proactive steering accepted and forwarded to 1 active delegated subtask(s)."


@pytest.mark.asyncio
async def test_records_written_under_an_earlier_lead_id_name_the_holder_without_custody(storage) -> None:
    """Looked up by team and parent, whoever wrote them."""

    lead = _lead(storage)
    _claim_recorded(_published_by(lead, "lead_before_restart"), "worker_1")

    await handle_task_steering_message(lead, *_guidance())

    _forwarded_to(lead, "worker_1")


@pytest.mark.asyncio
async def test_a_worker_that_admitted_the_subtask_gets_it_before_its_claim_is_recorded(storage) -> None:
    lead = _lead(storage)
    _published_by(lead, LEAD)
    _admitted_by(storage, "worker_1")

    await handle_task_steering_message(lead, *_guidance())

    _forwarded_to(lead, "worker_1")


@pytest.mark.asyncio
async def test_after_a_takeover_the_worker_holding_the_subtask_gets_it_not_the_one_recorded(storage) -> None:
    """The record still names the worker that was judged gone; custody moved to the one that took over."""

    lead = _lead(storage)
    _claim_recorded(_published_by(lead, LEAD), "worker_gone")
    _admitted_by(storage, "worker_took_over")

    await handle_task_steering_message(lead, *_guidance())

    _forwarded_to(lead, "worker_took_over")


@pytest.mark.asyncio
async def test_a_subtask_asked_of_an_agent_by_name_goes_to_that_agent(storage) -> None:
    lead = _lead(storage, directory={"Soup Researcher": "worker_named"}, asked_by_name="Soup Researcher")
    _published_by(lead, LEAD, asked_by_name="Soup Researcher")

    await handle_task_steering_message(lead, *_guidance())

    _forwarded_to(lead, "worker_named")


@pytest.mark.asyncio
async def test_a_subtask_no_worker_has_taken_gets_nothing_and_that_is_logged(storage, caplog) -> None:
    """Nothing would read it until a worker takes the subtask; the lead applies the guidance itself."""

    lead = _lead(storage)
    _published_by(lead, LEAD)

    with caplog.at_level(logging.INFO):
        await handle_task_steering_message(lead, *_guidance())

    assert lead.team_comms.forwarded() == []
    assert f"event=task_steering_forward_no_holder task_id={PARENT} subtask_id={CHILD}" in caplog.text
    [answer] = lead.team_comms.answers()
    assert answer.status == TaskSteeringStatus.ACCEPTED
    assert answer.status_reason == "Proactive steering accepted and will apply at the next safe checkpoint."
    assert [item.guidance_text for item in lead._pending_proactive_task_steering[PARENT]] == ["Only vegetarian soups."]
