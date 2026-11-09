"""A memory review sent right after a turn works for the person's conversation review schedule.

The team lead sends MemReviewAgent a review of each finished turn
(``strands_agent._publish_memory_review_task``). The turn's signed context
names the person's request, which has ended by then, so the change ledger
would refuse the review's memory changes as ``request_closed``. The review is
signed instead as work of the person's stale-conversation review schedule
(``task_context_runtime.sign_memory_review``), or waits for that schedule's
next run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from access_control.identity import ISSUER_SCHEDULER, ContextVerifier
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1, new_person_ref
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_messages.system_schedules import STALE_CONVERSATION_SCHEDULE_NAME_PREFIX, person_system_schedule_name
from storage.access_repository import AccessRepository
from storage.change_ledger import ChangeRequestStore, ChangeWriterKind, RequestState, work_writer
from storage.pinned_memory_changes import MemoryWriter
from storage.pinned_memory_repository import create_pinned_memory_repository
from tests.broker_access_support import context_signer, forget_task_context_keys, use_scheduler_key

TEAM, PERSON = "team_test", "alice"
NOW = datetime.now(timezone.utc)
SCHEDULE_ID = "mem_rev_conv_default_alice"


def _conversation_review(owner: str = PERSON, schedule_id: str = SCHEDULE_ID) -> ScheduledMessage:
    """The person's stale-conversation review schedule, as the gateway installs it (system-managed)."""

    return ScheduledMessage(
        schedule_id=schedule_id,
        schedule_name=person_system_schedule_name(STALE_CONVERSATION_SCHEDULE_NAME_PREFIX, "default", owner),
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Review stale conversations."),
        payload_header=MessageHeader(from_agent_id="gateway", to_routing_path="team_test_MemReviewAgent"),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner, channel_name="telegram", chat_id="chat_schedule")),
        interval_ms=86_400_000,
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    use_scheduler_key(monkeypatch)
    tasks = TaskManager(db_path=str(tmp_path / "manager.db"))
    schedules = ScheduleStore(db_path=str(tmp_path / "schedules.db"))
    lead = SimpleNamespace(
        id="agent_lead",
        name="TeamArchitect",
        team_id=TEAM,
        is_team_lead=True,
        task_manager=tasks,
        _schedule_store=schedules,
        team_comms=SimpleNamespace(send_message=AsyncMock()),
        memory_config={},
        _logger=logging.getLogger("test.memory_review_signing"),
        _verify_delegation_target=AsyncMock(return_value=True),
        _memory_review_task_ttl_seconds=lambda: 600.0,
    )
    yield SimpleNamespace(tasks=tasks, schedules=schedules, lead=lead, tmp_path=tmp_path)
    forget_task_context_keys()


def _record_person(world, standing: PersonStanding = PersonStanding.ACTIVE) -> None:
    roles = ("member",) if standing is PersonStanding.ACTIVE else ()
    AccessRepository(world.tasks.storage_settings).replace_people(
        TEAM, [TeamPersonV1(person_ref=new_person_ref(), team_id=TEAM, person_id=PERSON, standing=standing, roles=roles, changed_at_utc=NOW)], at=NOW
    )


def _turn_signed_context():
    """The gateway's signed context of the person's turn, which the review job keeps."""

    return context_signer().issue(team_id=TEAM, person_id=PERSON, person_roles=("member",))


async def _send_review(world, turn_context) -> list:
    from custom_agents.strands_agent import UniversalAgent

    job = SimpleNamespace(job_id="memory_review_job_1", plugin_id="pinned_memory_review")
    payload = {
        "task_id": "task_turn",
        "conversation_id": "conversation_trip",
        "canonical_user_id": PERSON,
        "gateway_context": GatewayContext(
            canonical_user_id=PERSON, channel_name="telegram", chat_id="chat_turn", task_context=turn_context
        ).model_dump(mode="json"),
    }
    await UniversalAgent._publish_memory_review_task(world.lead, job, payload, "team_test_MemReviewAgent")
    return [call.kwargs["message"] for call in world.lead.team_comms.send_message.await_args_list]


@pytest.mark.asyncio
async def test_a_review_after_a_turn_works_for_the_persons_review_schedule_so_its_memory_changes_go_through(world, caplog):
    world.schedules.add(_conversation_review())
    _record_person(world)
    turn = _turn_signed_context()
    # The person's request was admitted and has ended: the turn is over.
    requests = ChangeRequestStore(world.tasks.storage_settings)
    requests.open(team_id=TEAM, request_id=turn.context.context_id, person_id=PERSON, now=NOW)
    requests.close(team_id=TEAM, request_id=turn.context.context_id, person_id=PERSON, state=RequestState.ENDED, now=NOW)

    with caplog.at_level(logging.INFO, logger="custom_agents.task_context_runtime"):
        [envelope] = await _send_review(world, turn)

    context = ContextVerifier.from_env().verify(envelope.msg_meta.gateway_context.task_context, team_id=TEAM)
    assert (context.issuer, context.person_id, context.person_roles, context.schedule_id) == (
        ISSUER_SCHEDULER, PERSON, ("member",), SCHEDULE_ID,
    )
    # The chat route comes from the schedule, with the identity.
    assert envelope.msg_meta.gateway_context.chat_id == "chat_schedule"
    assert envelope.msg_meta.conversation_id == "conversation_trip"
    assert "event=memory_review_signed_for_schedule" in caplog.text

    # As one of the person's scheduled jobs, its memory changes are not refused for the ended request.
    writer = work_writer(person_id=context.person_id, request_id="", checked=True)
    assert writer.kind is ChangeWriterKind.SCHEDULED
    store = create_pinned_memory_repository(world.tasks.storage_settings)
    store.create_usermem(canonical_user_id=PERSON, fact_text="Prefers trains.", capacity=10, writer=MemoryWriter(TEAM, writer))
    assert [row.fact_text for row in store.list_usermem(canonical_user_id=PERSON)] == ["Prefers trains."]


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", [None, "another person's"], ids=["no_schedule", "another_persons_schedule"])
async def test_without_the_persons_own_review_schedule_the_job_waits_for_its_next_run(world, caplog, schedule):
    if schedule is not None:
        world.schedules.add(_conversation_review(owner="bob", schedule_id="mem_rev_conv_default_bob"))
    _record_person(world)

    with caplog.at_level(logging.INFO, logger="custom_agents.task_context_runtime"):
        sent = await _send_review(world, _turn_signed_context())

    assert sent == []
    assert "event=memory_review_not_signed reason=schedule_not_found" in caplog.text
    assert "memory_review_job_1" in caplog.text


@pytest.mark.asyncio
async def test_a_disabled_persons_review_is_not_sent(world):
    world.schedules.add(_conversation_review())
    _record_person(world, PersonStanding.DISABLED)

    assert await _send_review(world, _turn_signed_context()) == []
