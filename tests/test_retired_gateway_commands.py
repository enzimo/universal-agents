"""A gateway command queued before an in-place upgrade gets an answer (plan 13B.10 and 13B.12).

Phase 2 moved people's memory commands from a durable ``ManagerMemoryCommand``, and the
Web Chat Knowledge page's from a ``ManagerKnowledgeCommand``, in the team lead's control
queue to protocol 1 request/reply at the team services. Before this
change a command a 0.9 gateway queued just before the upgrade could not be read by the
0.10 lead: it was redelivered, set aside as a dead letter, and the person never heard
back. Now the lead reads it as a ``RetiredGatewayCommand`` and tells the person, in the
chat the command came from, that it was not carried out.

``tests/fixtures/upgrade/0.9-manager-memory-command.json`` is the envelope exactly as the
v0.9.11 gateway serialized a ``/coremem`` create (dumped with that release's own models),
so these tests do not depend on the deleted class. Only the signed task context is added
at test time, since a signature expires.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from access_control.identity import ContextSigner
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_agents.deferred_message_runtime import requires_execution_slot
from custom_agents.strands_agent import UniversalAgent
from custom_messages.contracts.control_v1 import (
    RETIRED_GATEWAY_COMMAND_NAMES,
    RetiredGatewayCommand,
    RetiredGatewayCommandClass,
)
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import GATEWAY_MANAGER_CONTROL_TYPES, MessageEnvelope
from custom_messages.enums import RequesterType
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_tools.providers.communication import send_message
from storage.pinned_memory_repository import create_pinned_memory_repository
from tests.broker_access_support import (
    context_signer,
    context_verify_keys,
    forget_task_context_keys,
    use_scheduler_key,
)
from tests.task_work_support import set_agent_ref
from tests.test_gateway_phase1 import _build_test_config, _CaptureGatewayManagerTransport
from tests.test_nats_pull_delivery import _adapter, _decode, _JsMessage

FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "0.9-manager-memory-command.json"
# A Web Chat Knowledge page note as the v0.9.11 gateway serialized it (dumped with that release's models).
KNOWLEDGE_FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "0.9-manager-knowledge-command.json"
# Topic command serialized with v0.9.11's own ManagerTopicCommand model.
TOPIC_FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "0.9-manager-topic-command.json"
PERSON = "Owner"
LEAD_ID = "manager_1234"
REFUSAL = "Your memory command was sent before the team was upgraded, so nothing was changed. Please send it again."


@pytest.fixture(autouse=True)
def _gateway_key(monkeypatch: pytest.MonkeyPatch):
    """The lead takes the command's person only from a gateway-signed context."""

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


class _Comms:
    """The lead's sends: (routing key, envelope), encoded and read back as the wire carries them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, MessageEnvelope]] = []

    async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
        self.sent.append((routing_key, decode_envelope(encode_envelope(message))))


@pytest.fixture
def world(tmp_path: Path) -> SimpleNamespace:
    config = _build_test_config(tmp_path)
    config.delivery.quiet_hours.enabled = False
    lead = SimpleNamespace(
        id=LEAD_ID,
        name="TeamLead",
        is_team_lead=True,
        team_id=config.gateway.team_id,
        team_comms=_Comms(),
        _pinned_memory_repository=create_pinned_memory_repository(config.manager_storage_settings),
        _logger=logging.getLogger("test.retired_gateway_commands"),
    )
    return SimpleNamespace(config=config, lead=lead, tmp_path=tmp_path)


def _queued_0_9_command(
    world: SimpleNamespace,
    *,
    signed: Any = "gateway",
    command_type: str = "request",
    claimed_person: str = PERSON,
    fixture: Path = FIXTURE,
) -> bytes:
    """The 0.9 gateway's bytes for this instance, as they wait in the lead's control queue."""

    document = json.loads(fixture.read_text())
    gateway_route = world.config.manager.response_queue_name
    document["msg_header"].update(
        from_agent_id=gateway_route, response_routing_key=gateway_route, to_routing_path=LEAD_ID
    )
    document["msg_content"]["command_type"] = command_type
    context = document["msg_meta"]["gateway_context"]
    context["canonical_user_id"] = claimed_person
    if signed == "gateway":
        signed = context_signer().issue(team_id=world.lead.team_id, person_id=PERSON, person_roles=("owner",))
    context["task_context"] = None if signed is None else signed.model_dump(mode="json")
    return json.dumps(document).encode()


async def _lead_handles(world: SimpleNamespace, payload: bytes) -> MessageEnvelope:
    """Decode on the lead's last allowed delivery, the way its transport does, then dispatch."""

    message = _JsMessage("queued_before_upgrade", data=payload, delivered=5, subject="team_test.agent.manager_1234.control")
    delivery = await _decode(_adapter(), message, max_deliver=5)
    assert delivery is not None, "the 0.10 lead must read a 0.9 gateway's memory command"
    assert message.nak_delays == [] and not message.terminated, "it is neither given back nor set aside"
    envelope = delivery.envelope
    await UniversalAgent._dispatch_message(
        world.lead,
        msg_header=envelope.msg_header,
        msg_content=envelope.msg_content,
        msg_meta=envelope.msg_meta,
    )
    return envelope


def _memory_unchanged(world: SimpleNamespace) -> bool:
    """No team or personal fact exists: the command (a ``/coremem`` create) was not carried out."""

    return world.lead._pinned_memory_repository.list_unexpired_facts() == []


@pytest.mark.asyncio
async def test_a_memory_command_queued_before_the_upgrade_is_answered_in_the_persons_chat(world, caplog) -> None:
    delivered: list[Any] = []
    gateway = UserGateway(
        world.config,
        output_sink=None,
        event_sink=delivered.append,
        state_store=GatewayStateStore(str(world.tmp_path / "gateway.db")),
        manager_transport=_CaptureGatewayManagerTransport(),
    )

    with caplog.at_level(logging.INFO, logger="test.retired_gateway_commands"):
        envelope = await _lead_handles(world, _queued_0_9_command(world))

    command = envelope.msg_content
    assert isinstance(command, RetiredGatewayCommand)
    assert (command.message_class, command.command_id) == ("ManagerMemoryCommand", "manager_memory_0f3c2a9b")
    # Answered at once, also while the lead is busy or paused, like other gateway controls.
    assert requires_execution_slot(envelope) is False
    assert RetiredGatewayCommand in GATEWAY_MANAGER_CONTROL_TYPES

    (routing_key, notice), = world.lead.team_comms.sent
    assert routing_key == world.config.manager.response_queue_name
    assert isinstance(notice.msg_content, UserNotification)
    assert notice.msg_content.text == f"Your memory command was not carried out\n\n{REFUSAL}"
    assert notice.msg_content.reason == "retired_gateway_command"
    assert notice.msg_header.idempotency_key == "retired_gateway_command:manager_memory_0f3c2a9b"
    assert notice.msg_meta.requester_type == RequesterType.AGENT_REQUESTER
    context = notice.msg_meta.gateway_context
    assert (context.session_key, context.channel_name, context.chat_id, context.canonical_user_id) == (
        "cli:owner", "cli", "owner-chat", PERSON,
    )
    assert context.delivery_mode == "autonomous"
    assert _memory_unchanged(world)
    answered = next(r.getMessage() for r in caplog.records if "event=retired_gateway_command_answered" in r.getMessage())
    assert "command_class=ManagerMemoryCommand" in answered
    assert "command_id=manager_memory_0f3c2a9b" in answered and f"person_id={PERSON}" in answered

    # The restarted 0.10 gateway receives it on its reply route and shows it in the person's chat.
    await gateway._manager_client.handle_response(notice)

    (event,) = delivered
    assert event.text == notice.msg_content.text
    assert (event.channel_name, event.chat_id, event.session_key) == ("cli", "owner-chat", "cli:owner")


@pytest.mark.asyncio
async def test_a_knowledge_page_request_queued_before_the_upgrade_is_answered_in_the_persons_web_chat(
    world, caplog
) -> None:
    with caplog.at_level(logging.INFO, logger="test.retired_gateway_commands"):
        envelope = await _lead_handles(world, _queued_0_9_command(world, fixture=KNOWLEDGE_FIXTURE))

    command = envelope.msg_content
    assert isinstance(command, RetiredGatewayCommand)
    assert (command.message_class, command.command_id) == ("ManagerKnowledgeCommand", "mgr_knowledge_5d2e7a41")
    (routing_key, notice), = world.lead.team_comms.sent
    assert routing_key == world.config.manager.response_queue_name
    assert notice.msg_content.text == (
        "Your Knowledge page request was not carried out\n\nYour Knowledge page request was sent before the team "
        "was upgraded, so nothing was changed. Please send it again."
    )
    assert notice.msg_header.idempotency_key == "retired_gateway_command:mgr_knowledge_5d2e7a41"
    context = notice.msg_meta.gateway_context
    assert (context.session_key, context.channel_name, context.chat_id, context.canonical_user_id) == (
        "website:web-chat:owner", "website", "web-chat", PERSON,
    )
    answered = next(r.getMessage() for r in caplog.records if "event=retired_gateway_command_answered" in r.getMessage())
    assert "command_class=ManagerKnowledgeCommand" in answered


@pytest.mark.asyncio
async def test_a_topic_command_queued_before_the_upgrade_is_answered(world) -> None:
    envelope = await _lead_handles(world, _queued_0_9_command(world, fixture=TOPIC_FIXTURE))
    assert isinstance(envelope.msg_content, RetiredGatewayCommand)
    assert envelope.msg_content.message_class == "ManagerTopicCommand"
    assert requires_execution_slot(envelope) is False
    (_, notice), = world.lead.team_comms.sent
    assert notice.msg_content.text == (
        "Your topic command was not carried out\n\nYour topic command was sent before the team was upgraded, "
        "so nothing was changed. Please send it again."
    )
    assert notice.msg_header.idempotency_key == "retired_gateway_command:manager_topic_41c2a7b9"


@pytest.mark.asyncio
async def test_a_redelivered_command_sends_the_same_notice(world) -> None:
    """A lead that stops before acknowledging handles it again; the notice keeps its idempotency key."""

    payload = _queued_0_9_command(world)
    await _lead_handles(world, payload)
    await _lead_handles(world, payload)

    keys = {notice.msg_header.idempotency_key for _route, notice in world.lead.team_comms.sent}
    assert keys == {"retired_gateway_command:manager_memory_0f3c2a9b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "event", "reason"),
    [
        ("response", "retired_gateway_command_response_ignored", None),
        ("unsigned", "retired_gateway_command_unanswered", "unsigned_sender"),
        ("scheduler_signed", "retired_gateway_command_unanswered", "sender_not_gateway"),
        ("names_another_person", "retired_gateway_command_unanswered", "person_mismatch"),
        ("not_team_lead", "retired_gateway_command_unanswered", "not_team_lead"),
        ("no_chat_route", "retired_gateway_command_unanswered", "no_chat_route"),
    ],
)
async def test_a_retired_command_that_cannot_be_answered_tells_nobody(
    world, monkeypatch, caplog, case: str, event: str, reason: str | None
) -> None:
    payload_fields: dict[str, Any] = {}
    if case == "response":
        payload_fields["command_type"] = "response"
    elif case == "unsigned":
        payload_fields["signed"] = None
    elif case == "scheduler_signed":
        use_scheduler_key(monkeypatch)
        scheduler = ContextSigner.from_env("scheduler")
        assert scheduler is not None
        payload_fields["signed"] = scheduler.issue(team_id=world.lead.team_id, person_id=PERSON, person_roles=("owner",))
    elif case == "names_another_person":
        payload_fields["claimed_person"] = "Someone_else"
    elif case == "not_team_lead":
        world.lead.is_team_lead = False
    payload = _queued_0_9_command(world, **payload_fields)
    if case == "no_chat_route":
        document = json.loads(payload)
        document["msg_header"]["response_routing_key"] = None
        payload = json.dumps(document).encode()

    with caplog.at_level(logging.INFO, logger="test.retired_gateway_commands"):
        await _lead_handles(world, payload)

    assert world.lead.team_comms.sent == []
    assert _memory_unchanged(world)
    line = next(r.getMessage() for r in caplog.records if f"event={event}" in r.getMessage())
    assert "command_class=ManagerMemoryCommand" in line and "command_id=manager_memory_0f3c2a9b" in line
    if reason is not None:
        assert f"reason={reason}" in line


@pytest.mark.asyncio
async def test_a_0_9_leads_late_reply_is_dropped_by_the_gateway_not_set_aside(world, caplog) -> None:
    """A reply the 0.9 lead sent before the upgrade decodes and is dropped like any reply nobody waits for."""

    delivered: list[Any] = []
    gateway = UserGateway(
        world.config,
        output_sink=None,
        event_sink=delivered.append,
        state_store=GatewayStateStore(str(world.tmp_path / "gateway.db")),
        manager_transport=_CaptureGatewayManagerTransport(),
    )
    reply = decode_envelope(_queued_0_9_command(world, command_type="response"))

    with caplog.at_level(logging.WARNING):
        await gateway._manager_client.handle_response(reply)

    assert isinstance(reply.msg_content, RetiredGatewayCommand)
    assert delivered == []
    assert "event=gateway_unmatched_manager_control_response_dropped" in caplog.text


@pytest.mark.asyncio
async def test_agents_cannot_send_a_retired_gateway_command() -> None:
    set_agent_ref(SimpleNamespace(id="worker_1", name="Worker", team_id="team_test"))
    try:
        forged = {
            "msg_content": {"message_class": "ManagerMemoryCommand", "command_id": "forged", "command_type": "request"},
            "msg_meta": {"to_agent_name": "TeamLead", "gateway_context": {"canonical_user_id": PERSON}},
        }
        result = await send_message(json.dumps(forged))
    finally:
        set_agent_ref(None)

    assert "reserved for the authenticated gateway" in result


def test_every_retired_command_has_a_name_people_know() -> None:
    """A family that moves to the team services adds its class and its plain name together."""

    assert set(get_args(RetiredGatewayCommandClass)) == set(RETIRED_GATEWAY_COMMAND_NAMES)
