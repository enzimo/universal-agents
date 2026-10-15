"""Access review buttons in chat channels, with the real access Inbox (Cedar and SQLite).

Covers: an announcement carries the request and its choices; Telegram and
Discord show them as buttons (Approve / Approve read only / Approve for this
task only / Deny for access, Approve / Deny for a staged change) plus an Inbox
link; a press decides as the person linked to the pressing chat account (the
real request-context lookup) through ``GatewayAccessInbox.decide``, wakes the
waiting agent and updates the message; a second press finds the request
closed; an unlinked or unauthorized account decides nothing and keeps the
buttons; and a hosted adapter sends the press to the gateway as the
``access.review_decision`` command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.adapter_host.gateway_application import (
    GatewayAdapterRequestHandler,
    gateway_delivery_to_wire,
)
from assistant_gateway.adapter_host.remote_host import RemoteGatewayApplication
from assistant_gateway.application.access_review_service import GatewayAccessReviewControls
from assistant_gateway.application.access_reviews import (
    NOT_LINKED_TEXT,
    AccessReviewDecisionRequestV1,
    AccessReviewOutcome,
    decode_review_control,
    encode_review_control,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestContextSettings,
)
from assistant_gateway.contracts.adapter_host_v1 import (
    ACCESS_REVIEW_DECISION_COMMAND,
    ADAPTER_HOST_PROTOCOL_VERSION,
    ChannelCommandV1,
)
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestResolvedV1,
    AccessRequestStatus,
    AccessReviewChoice,
    ChangeProposalV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.test_gateway_discord_app import DiscordInteractiveApp, _FakeBot, _FakeMessageable
from tests.test_gateway_discord_app import _config as discord_config
from tests.test_gateway_telegram_app import TelegramInteractiveApp, _FakeApp
from tests.test_gateway_telegram_app import _config as telegram_config

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = AgentPrincipal("agent-lead", "TeamArchitect", "team_lead", TEAM, work_id="task-1")
# work_id: the request its task works on (task-only access is bound to it).
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="task-1")
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
SCHEDULES = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")
# Chat accounts linked to gateway users (channel, external id) -> (person, display name).
LINKED = {
    ("telegram", "111"): ("owner-1", "Owner One"),
    ("telegram", "222"): ("member-1", "Member Two"),
    ("discord", "333"): ("owner-1", "Owner One"),
}

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Identity:
    """Active users and their team roles, as the gateway identity store keeps them."""

    roles = {"owner-1": "owner", "member-1": "member"}

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active", display_name=person_id) if person_id in self.roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM, role=role)] if role else []


class LinkedAccounts:
    """The gateway identity service's channel-account lookup (``/auth`` links)."""

    def resolve_canonical_user(self, *, channel_name: str, external_user_id: str, chat_scope: str, **_: Any) -> Any:
        linked = LINKED.get((channel_name, external_user_id))
        if linked is None:
            return None
        person_id, name = linked
        return SimpleNamespace(user=SimpleNamespace(canonical_user_id=person_id, display_name=name))


class ChannelApplication:
    """The channel-facing application, as far as review buttons use it."""

    def __init__(self, controls: GatewayAccessReviewControls) -> None:
        self.controls = controls

    async def decide_access_review(self, request: Any) -> Any:
        return await self.controls.decide(request)

    def resolve_session(self, **_: Any) -> Any:
        return SimpleNamespace(session_key="s-hosted", conversation_id="conv-hosted")


@dataclass
class Manager:
    connected: bool = True
    published: list[tuple[str, Any]] = field(default_factory=list)

    async def publish_durable(self, envelope: Any, *, routing_key: str) -> None:
        self.published.append((routing_key, envelope))


def _session(key: str, conversation_id: str, channel: str, user_id: str, chat_id: str) -> Any:
    return SimpleNamespace(
        session_key=key,
        owner_canonical_user_id="owner-1",
        status="active",
        channel_name=channel,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=None,
        conversation_id=conversation_id,
    )


@dataclass
class Harness:
    inbox: GatewayAccessInbox
    agents: AccessRequests
    manager: Manager
    emitted: list[Any]
    application: ChannelApplication

    def status(self, request_id: str) -> AccessRequestStatus:
        return self.inbox.requests().service.store.get_request(request_id).status


@pytest.fixture
def harness(tmp_path: Path):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    sessions = {
        "conv-tg": _session("s-tg", "conv-tg", "telegram", "111", "456"),
        "conv-dc": _session("s-dc", "conv-dc", "discord", "333", "123"),
    }
    manager = Manager()
    emitted: list[Any] = []

    async def emit(event: Any) -> None:
        emitted.append(event)

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type=LEAD.agent_type,
            inbox_url="https://ua.example/webchat",
            instance_root=None,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=True,
        ),
        memberships=MembershipRoles(Identity()),
        manager_client=manager,
        session_repository=SimpleNamespace(get_session_by_conversation_id=sessions.get),
        proactive_session=lambda: None,
        emit_delivery_event=emit,
    )
    request_context = GatewayRequestContextService(
        settings=GatewayRequestContextSettings(
            instance_name="test",
            team_id=TEAM,
            default_canonical_user_id="owner-1",
            default_display_name="Owner One",
            default_timezone="UTC",
            heartbeat_source_id=None,
            users_enabled=True,
        ),
        identity=LinkedAccounts(),  # type: ignore[arg-type]
    )
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD.agent_type),
    )
    agents = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type=LEAD.agent_type)
    application = ChannelApplication(GatewayAccessReviewControls(request_context=request_context, decider=inbox))
    yield Harness(inbox, agents, manager, emitted, application)
    inbox.close()
    service.cedar.close()


def _ask(harness: Harness, conversation_id: str) -> Any:
    return harness.agents.submit(
        requester=WORKER,
        person=OWNER,
        target=SCHEDULES,
        level=AccessLevel.WRITE,
        reason="Track the weekly report",
        task_id="task-1",
        conversation_id=conversation_id,
    ).request


async def _announced(harness: Harness) -> Any:
    await harness.inbox.run_once()
    [event] = harness.emitted
    return event


def _telegram_query(button: Any, *, user_id: int, text: str) -> Any:
    return SimpleNamespace(
        data=button.callback_data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=456), message_thread_id=None, message_id=7, text=text),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def test_button_data_is_short_and_round_trips() -> None:
    for choice in AccessReviewChoice:
        data = encode_review_control("access_req_0123456789abcdef", choice.value)
        assert len(data.encode()) <= 64, "Telegram allows 64 bytes of callback data"
        assert decode_review_control(data) == ("access_req_0123456789abcdef", choice.value)
    assert decode_review_control("uaar:x:access_req_1") is None
    assert decode_review_control("uaar:a:") is None
    assert decode_review_control("uap:old-token") is None


@pytest.mark.asyncio
async def test_telegram_buttons_decide_as_the_linked_person_and_update_the_message(
    harness: Harness, tmp_path: Path
) -> None:
    request = _ask(harness, "conv-tg")
    event = await _announced(harness)
    assert event.access_request_id == request.request_id
    # Chat has no Keep box: "for this task" and "keep" are separate buttons.
    assert event.review_choices == ("approve_task", "approve", "approve_read", "deny")
    app = TelegramInteractiveApp(telegram_config(tmp_path), application=harness.application, bridge=SimpleNamespace())
    app._app = _FakeApp()

    await app.deliver_event(event)

    [sent] = app._app.bot.sent_messages
    rows = sent["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows] == [
        "Allow for this task",
        "Allow and keep",
        "Allow reading only",
        "Deny",
        "Open Inbox",
    ]
    assert rows[-1][0].url == event.review_url
    read_only = _telegram_query(rows[2][0], user_id=111, text=event.text)

    await app._on_review_callback(SimpleNamespace(callback_query=read_only), SimpleNamespace())

    stored = harness.inbox.requests().service.store.get_request(request.request_id)
    assert stored.status is AccessRequestStatus.GRANTED
    assert stored.decision is AccessReviewChoice.APPROVE_READ
    assert stored.decided_by.id == "owner-1"
    [(route, envelope)] = harness.manager.published
    assert route == WORKER.agent_id and isinstance(envelope.msg_content, AccessRequestResolvedV1)
    read_only.edit_message_text.assert_awaited_once_with(
        text=f"{event.text}\n\nAllowed reading only by Owner One.", reply_markup=None
    )
    read_only.answer.assert_awaited_once_with("Allowed reading only by Owner One.")

    deny_later = _telegram_query(rows[3][0], user_id=111, text=event.text)
    await app._on_review_callback(SimpleNamespace(callback_query=deny_later), SimpleNamespace())

    assert harness.status(request.request_id) is AccessRequestStatus.GRANTED
    deny_later.edit_message_text.assert_awaited_once_with(
        text=f"{event.text}\n\nThis request was already approved.", reply_markup=None
    )
    assert len(harness.manager.published) == 1, "a closed request wakes nobody again"


@pytest.mark.asyncio
async def test_an_unlinked_or_unauthorized_account_decides_nothing_and_keeps_the_buttons(
    harness: Harness, tmp_path: Path
) -> None:
    request = _ask(harness, "conv-tg")
    event = await _announced(harness)
    app = TelegramInteractiveApp(telegram_config(tmp_path), application=harness.application, bridge=SimpleNamespace())
    app._app = _FakeApp()
    await app.deliver_event(event)
    approve = app._app.bot.sent_messages[0]["reply_markup"].inline_keyboard[0][0]

    for user_id, expected in ((999, NOT_LINKED_TEXT), (222, "")):
        query = _telegram_query(approve, user_id=user_id, text=event.text)
        await app._on_review_callback(SimpleNamespace(callback_query=query), SimpleNamespace())
        (answer,), options = query.answer.await_args
        assert options == {"show_alert": True}
        if expected:
            assert answer == expected[:200]
        else:
            assert answer and "Approved" not in answer, "the member's refusal is explained"
        query.edit_message_text.assert_not_awaited()
        query.edit_message_reply_markup.assert_not_awaited()

    assert harness.status(request.request_id) is AccessRequestStatus.WAITING
    assert harness.manager.published == []


@pytest.mark.asyncio
async def test_discord_buttons_decide_a_staged_change_with_approve_or_deny(harness: Harness, tmp_path: Path) -> None:
    change = harness.agents.submit_change(
        requester=LEAD,
        person=OWNER,
        proposal=ChangeProposalV1(
            kind="agent_definition",
            change_id="generated_agent_crawler",
            plan_digest="a" * 64,
            title="Create the crawler specialist",
            summary="Create the specialist CrawlerAgent. Available as soon as it is created; no restart.",
            effect=("Creates the CrawlerAgent specialist.",),
        ),
        reason="The plan needs it",
        task_id="task-lead",
        conversation_id="conv-dc",
    ).request
    event = await _announced(harness)
    assert event.review_choices == ("approve_all", "deny")
    target = _FakeMessageable(123)
    app = DiscordInteractiveApp(discord_config(tmp_path), application=harness.application, bridge=SimpleNamespace())
    app._bot = _FakeBot({123: target})

    await app.deliver_event(event)

    view = target.sent_messages[-1]["view"]
    assert [(item.label, item.custom_id) for item in view.children[:2]] == [
        ("Approve all as proposed", encode_review_control(change.request_id, "approve_all")),
        ("Deny", encode_review_control(change.request_id, "deny")),
    ]
    assert view.children[-1].url == event.review_url
    message = SimpleNamespace(id=5, content=event.text, edit=AsyncMock())
    interaction = SimpleNamespace(
        data={"custom_id": view.children[1].custom_id},
        user=SimpleNamespace(id=333),
        channel=SimpleNamespace(id=123),
        message=message,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await app._on_component_interaction(interaction)

    assert harness.status(change.request_id) is AccessRequestStatus.DENIED
    message.edit.assert_awaited_once_with(content=f"{event.text}\n\nDenied by Owner One.", view=None)
    interaction.followup.send.assert_awaited_once_with("Denied by Owner One.", ephemeral=True)
    ignored = SimpleNamespace(data={"custom_id": "something-else"}, response=SimpleNamespace(defer=AsyncMock()))
    await app._on_component_interaction(ignored)
    ignored.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_hosted_adapters_send_the_press_to_the_gateway_as_a_command(harness: Harness) -> None:
    request = _ask(harness, "conv-tg")
    event = await _announced(harness)
    wire = gateway_delivery_to_wire(event, plugin_id="telegram", adapter_instance_id="telegram-1")
    assert (wire.access_request_id, wire.review_choices) == (request.request_id, event.review_choices)

    submitted: list[ChannelCommandV1] = []
    remote = RemoteGatewayApplication(
        lambda: SimpleNamespace(submit=AsyncMock(side_effect=submitted.append)),
        plugin_id="telegram",
        adapter_instance_id="telegram-1",
        channel_name="telegram",
        default_session_key="default",
    )
    press = AccessReviewDecisionRequestV1(
        request_id=request.request_id, choice="approve", channel_name="telegram", user_id="111", chat_id="456"
    )
    result = await remote.decide_access_review(press)
    assert result.outcome is AccessReviewOutcome.SUBMITTED
    [command] = submitted
    assert command.command_name == ACCESS_REVIEW_DECISION_COMMAND
    assert command.arguments == {"request_id": request.request_id, "choice": "approve"}

    handler = GatewayAdapterRequestHandler(harness.application, lambda: None)  # type: ignore[arg-type]
    [delivery] = await handler.handle_command(command)

    assert harness.status(request.request_id) is AccessRequestStatus.GRANTED
    assert delivery.text == "Allowed and kept by Owner One."
    assert (delivery.chat_id, delivery.session_key) == ("456", "s-hosted")
    with pytest.raises(ValueError, match="exactly request_id and choice"):
        await handler.handle_command(command.model_copy(update={"arguments": {"request_id": "x"}}))


def test_channel_command_wire_contract_accepts_the_review_command() -> None:
    ChannelCommandV1(
        protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
        message_type="channel.command.v1",
        request_id="adapter_cmd_1",
        idempotency_key="adapter:1",
        plugin_id="telegram",
        adapter_instance_id="telegram-1",
        session_epoch="pending",
        stream_position=1,
        channel_name="telegram",
        external_user_id="111",
        chat_id="456",
        command_name=ACCESS_REVIEW_DECISION_COMMAND,
        arguments={"request_id": "access_req_1", "choice": "approve"},
        received_at_utc=datetime.now(timezone.utc),
    )
