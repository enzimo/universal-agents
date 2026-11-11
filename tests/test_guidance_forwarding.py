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
from nats.errors import TimeoutError as NatsTimeoutError

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
    """The lead's transport: records each durable send; ``failing`` holders' publishes time out."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.failing: dict[str, int] = {}

    async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
        if self.failing.get(routing_key, 0) > 0:
            self.failing[routing_key] -= 1
            raise NatsTimeoutError()  # what a JetStream publish raises when its acknowledgement does not come
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


def _published_by(lead: Any, requester_id: str, *, asked_by_name: Optional[str] = None,
                  subtask_id: str = CHILD) -> Any:
    """The delegated-task record, written by the lead under ``requester_id`` (an earlier id after a restart)."""

    publisher = SimpleNamespace(id=requester_id, team_id=TEAM, task_manager=lead.task_manager)
    record_publication(publisher, subtask_id=subtask_id, parent_task_id=PARENT, named_agent_type=WORKER_TYPE,
                       target_agent_name=asked_by_name, publish_sequence=7)
    return publisher


def _claim_recorded(publisher: Any, worker_id: str) -> None:
    delegated_tasks(publisher).record_claim(team_id=TEAM, task_id=CHILD, worker_agent_id=worker_id,
                                            worker_agent_name=worker_id, claimed_at=datetime.now(timezone.utc))


def _admitted_by(storage, worker_id: str, subtask_id: str = CHILD) -> None:
    """The worker's own saved work holds the subtask: its team-wide custody row."""

    worker = TaskManager(storage_settings=storage)
    worker.set_owner_identity(worker_id, worker_id, TEAM, WORKER_TYPE)
    assert worker.add_task(Task(task_id=subtask_id, parent_task_id=PARENT, task_type=MessageMetaType.REQUEST),
                           MessageHeader(from_agent_id=LEAD), MessageMeta(), owner_person_id=None)


def _held_by(storage, lead: Any, worker_id: str) -> None:
    """The usual case: the lead published the subtask and the worker admitted it."""

    _published_by(lead, LEAD)
    _admitted_by(storage, worker_id)


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


# -- a copy that did not go out (F4) -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forward_that_failed_reaches_the_worker_when_the_gateway_sends_the_guidance_again(
    storage, caplog
) -> None:
    """The gateway keeps guidance it got no accepted answer for and sends it again with the same key.

    It used to be answered as a duplicate and never passed on again, so the worker never got it.
    """

    lead = _lead(storage)
    _held_by(storage, lead, "worker_1")
    lead.team_comms.failing["worker_1"] = 1

    with caplog.at_level(logging.INFO):
        await handle_task_steering_message(lead, *_guidance())

    assert lead.team_comms.forwarded() == []
    [pending] = lead.team_comms.answers()
    assert pending.status == TaskSteeringStatus.PENDING
    assert (f"event=task_steering_forward_failed task_id={PARENT} subtask_id={CHILD} holder_id=worker_1 "
            "error_type=TimeoutError") in caplog.text
    # The lead keeps the guidance for its own task whatever happened to the copies.
    assert [item.guidance_text for item in lead._pending_proactive_task_steering[PARENT]] == ["Only vegetarian soups."]

    await handle_task_steering_message(lead, *_guidance())

    _forwarded_to(lead, "worker_1")
    accepted = lead.team_comms.answers()[-1]
    assert accepted.status == TaskSteeringStatus.ACCEPTED
    assert accepted.status_reason == "Duplicate steering event; passed on again to 1 active delegated subtask(s)."
    assert len(lead._pending_proactive_task_steering[PARENT]) == 1, "saved once, not again"


@pytest.mark.asyncio
async def test_one_holder_that_cannot_be_reached_does_not_stop_the_others(storage, caplog) -> None:
    second = "task_child_two"
    lead = _lead(storage)
    lead.task_manager.add_subtask(
        PARENT,
        Task(task_id=second, parent_task_id=PARENT, task_type=MessageMetaType.REQUEST, problem_statement="More."),
        MessageHeader(from_agent_id=LEAD), MessageMeta(to_named_agent_type=WORKER_TYPE),
    )
    _held_by(storage, lead, "worker_1")
    _published_by(lead, LEAD, subtask_id=second)
    _admitted_by(storage, "worker_2", second)
    lead.team_comms.failing["worker_1"] = 1

    with caplog.at_level(logging.INFO):
        await handle_task_steering_message(lead, *_guidance())

    assert [(sent.routing_key, sent.envelope.msg_content.target_task_id) for sent in lead.team_comms.forwarded()] == [
        ("worker_2", second)
    ]
    assert f"subtask_id={CHILD} holder_id=worker_1 error_type=TimeoutError" in caplog.text
    [answer] = lead.team_comms.answers()
    assert answer.status == TaskSteeringStatus.PENDING


@pytest.mark.asyncio
async def test_holders_that_cannot_be_read_are_a_warning_and_a_pending_answer(storage, caplog, monkeypatch) -> None:
    """Not "no worker holds it": the gateway's next send tries again."""

    lead = _lead(storage)
    _held_by(storage, lead, "worker_1")

    def unreadable(*_args, **_kwargs):
        raise OSError("database is locked")

    monkeypatch.setattr(lead.task_manager.runtime_state_store, "get_task_custody", unreadable)
    with caplog.at_level(logging.INFO):
        await handle_task_steering_message(lead, *_guidance())

    assert lead.team_comms.forwarded() == []
    assert (f"event=delegated_task_holders_unreadable task_id={PARENT} subtask_ids=['{CHILD}'] "
            "error_type=OSError") in caplog.text
    assert "task_steering_forward_no_holder" not in caplog.text
    [answer] = lead.team_comms.answers()
    assert answer.status == TaskSteeringStatus.PENDING


@pytest.mark.asyncio
async def test_a_forward_that_keeps_failing_is_given_up_so_later_guidance_is_not_held_back(storage, caplog) -> None:
    """The gateway holds a thread's later messages behind a held one, so pending answers are limited."""

    from custom_agents.task_steering_runtime import GUIDANCE_PASS_ON_ATTEMPTS

    lead = _lead(storage)
    _held_by(storage, lead, "worker_1")
    lead.team_comms.failing["worker_1"] = 100

    with caplog.at_level(logging.INFO):
        for _attempt in range(GUIDANCE_PASS_ON_ATTEMPTS):
            await handle_task_steering_message(lead, *_guidance())

    statuses = [answer.status for answer in lead.team_comms.answers()]
    assert statuses == [TaskSteeringStatus.PENDING] * (GUIDANCE_PASS_ON_ATTEMPTS - 1) + [TaskSteeringStatus.ACCEPTED]
    assert lead.team_comms.answers()[-1].status_reason == (
        f"Proactive steering accepted, but after {GUIDANCE_PASS_ON_ATTEMPTS} attempts it could not be passed on to "
        f"delegated subtask(s) {CHILD}; the task applies it at its next safe checkpoint."
    )
    assert (f"event=task_steering_forward_abandoned task_id={PARENT} steering_key=corr_guidance "
            f"attempts={GUIDANCE_PASS_ON_ATTEMPTS} subtask_ids=['{CHILD}'] holder_ids=['worker_1']") in caplog.text
    assert lead._guidance_pass_on_failures == {}

    # The next message for the request is the gateway's to send now; the lead takes it as new guidance.
    lead.team_comms.failing.clear()
    await handle_task_steering_message(lead, *_guidance("corr_guidance_next"))
    assert lead.team_comms.answers()[-1].status == TaskSteeringStatus.ACCEPTED
    assert [sent.envelope.msg_content.idempotency_key for sent in lead.team_comms.forwarded()] == [
        f"corr_guidance_next:{CHILD}"
    ]


@pytest.mark.asyncio
async def test_a_repeat_after_a_restart_is_passed_on_again_under_the_same_ids(storage) -> None:
    """The saved history marks it a repeat after a restart too; the worker and NATS drop the same id."""

    lead = _lead(storage)
    _held_by(storage, lead, "worker_1")
    await handle_task_steering_message(lead, *_guidance())
    lead._processed_task_steering_ids.clear()  # what a restarted lead remembers in memory

    await handle_task_steering_message(lead, *_guidance())

    first, again = lead.team_comms.forwarded()
    assert first.envelope.msg_header.idempotency_key == again.envelope.msg_header.idempotency_key == (
        f"corr_guidance:{CHILD}"
    )
    assert [answer.status_reason for answer in lead.team_comms.answers()] == [
        "Proactive steering accepted and forwarded to 1 active delegated subtask(s).",
        "Duplicate steering event; passed on again to 1 active delegated subtask(s).",
    ]
    assert len(lead._pending_proactive_task_steering[PARENT]) == 1
