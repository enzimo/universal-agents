"""Exercise tool setup requests (an account link form) arriving after the chat HTTP response has ended."""

from __future__ import annotations

from secure_tools.broker_pages import card_view_url

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway import team_view
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.application.ports import GatewaySecureBrokerRefusal
from assistant_gateway.application.request_service import request_task_ended
from assistant_gateway.application.tool_setup import PageEnding
from assistant_gateway.domain.request_state import RequestRecord
from assistant_gateway.interactive_apps.website_handlers.workflows import WebsiteWorkflowHandlers
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import TaskSteering, TaskSteeringInterruptRecord
from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1
from custom_messages.enums import TaskSteeringType
from custom_messages.contracts.routing_v1 import (
    MessageHeader,
    MessageMeta,
    GatewayContext,
)
from tests.test_gateway_phase1 import (
    _build_test_config,
    _CaptureGatewayManagerTransport,
)
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp


@pytest_asyncio.fixture
async def handoff(tmp_path, monkeypatch):
    config = _build_test_config(tmp_path)
    config.network.public_origin = "http://gateway.example"
    config.security_layer.mode = "broker"
    config.security_layer.broker_internal_base_url = "http://127.0.0.1:8766"
    config.security_layer.broker_public_port = 18600
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config, output_sink=None, state_store=store, manager_transport=transport
    )
    owner = config.user.canonical_user_id
    store.create_session(
        session_key="session_binding",
        conversation_id="conversation_binding",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    store.record_request(
        correlation_id="correlation_binding",
        session_key="session_binding",
        request_kind="interactive",
        request_text="bind a credential",
        conversation_id="conversation_binding",
        followup_to_task_id=None,
        status="waiting_for_permission",
        owner_canonical_user_id=owner,
    )
    store.update_request_coordination(
        "correlation_binding",
        status="waiting_for_permission",
        active_task_id="task_binding",
    )
    content = ToolSetupRequestV1(
        setup_request_id="account_link_request_test",
        parent_task_id="task_binding",
        tool_id="cli:huggingface",
        kind="account_link",
        owner="person",
        reason="To upload the weekly model, choose your Hugging Face account.",
    )
    envelope = MessageEnvelope(
        msg_content=content,
        msg_meta=MessageMeta(
            msg_correlation_id="correlation_binding",
            conversation_id="conversation_binding",
            gateway_context=GatewayContext(
                session_key="session_binding",
                canonical_user_id=owner,
                channel_name="website",
                chat_id="web-chat",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id=config.manager.manager_agent_id,
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )
    create = AsyncMock(
        return_value={
            "status": "pending",
            "session_id": "link_bearer_secret",
            "tool_display_name": "Hugging Face (hf)",
            "form_url": "http://127.0.0.1:8766/account-link/link_bearer_secret",
            "status_url": "http://127.0.0.1:8766/v1/account-link/sessions/link_bearer_secret",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=15)
            ).isoformat(),
            "message": "Open the link to choose which stored credential this tool uses.",
        }
    )
    monkeypatch.setattr(gateway._secure_broker, "create_account_link_session", create)
    # Retain a real pending page watch, but do not contact a private broker.
    release = asyncio.Event()

    async def watch(*args, **kwargs):
        await release.wait()

    monkeypatch.setattr(gateway._credential_workflow.tool_setups, "_watch", watch)
    yield gateway, envelope, create, transport
    await gateway._credential_workflow.close()


@pytest.mark.asyncio
async def test_resumed_setup_page_delivered_through_authenticated_polling(
    handoff, caplog, monkeypatch
):
    gateway, envelope, create, transport = handoff
    # A steering interrupt has closed this queue before the request arrives.
    interrupt = envelope.model_copy(deep=True)
    interrupt.msg_content = TaskSteering(
        steering_type=TaskSteeringType.INTERRUPT_REQUEST,
        target_task_id="task_binding",
        status_reason="The task is waiting.",
        interrupts=[
            TaskSteeringInterruptRecord(
                interrupt_id="steering_interrupt",
                reason={"kind": "question"},
            )
        ],
    )
    async with gateway._manager_client.response_channel("correlation_binding"):
        await gateway._manager_client.handle_response(interrupt)
        steering = await gateway._response_coordinator.await_terminal_response(
            correlation_id="correlation_binding",
            session_key="session_binding",
            query_handler=None,
        )
        assert steering.message_type == "task_steering"
    await gateway._manager_client.handle_response(envelope)
    with pytest.raises(RuntimeError, match="not registered"):
        gateway._manager_client.get_response_channel("correlation_binding")
    create.assert_awaited_once()
    (request,) = create.await_args.args
    owner = gateway.config.user.canonical_user_id
    assert request.request_id == "account_link_request_test"
    assert request.person_id == owner
    assert request.person_roles == gateway._request_context_service.team_roles(owner)
    assert request.tool_id == "cli:huggingface"
    assert request.owner_kind.value == "person"
    assert transport.durable == []  # No synthetic failure wakes the agent to retry.
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway)
    async with TestClient(TestServer(app.build_app())) as client:
        denied = await client.get("/api/web-chat/events")
        assert denied.status == 401
        await client.post("/api/web-chat/auth", json={"token": "uaweb_test_token"})
        result = await client.get("/api/web-chat/events")
        assert result.status == 200
        assert result.headers["Cache-Control"] == "no-store"
        payload = await result.json()
        assert len(payload["transient_actions"]) == 1
        action = payload["transient_actions"][0]
        assert action["kind"] == "tool_setup"
        assert action["action"]["label"] == "Choose your account for Hugging Face (hf)"
        assert "link_bearer_secret" in action["action"]["url"]
        # A broker page also carries its card view, the same address with view=card (A2).
        assert action["action"]["frame_url"] == card_view_url(action["action"]["url"])
        # The card shows the task's own sentence and what a link does, never the page address.
        assert action["content"] == (
            "To upload the weekly model, choose your Hugging Face account.\n"
            "Linking an account gives nobody access by itself."
        )
        assert "link_bearer_secret" not in json.dumps(payload["messages"])
        again = await client.get(
            "/api/web-chat/events", params={"after": payload["next_cursor"]}
        )
        assert (await again.json())["transient_actions"] == payload["transient_actions"]
    assert (
        gateway.application.transcripts.list_credential_actions(
            owner_canonical_user_id="someone_else"
        )
        == ()
    )
    history = gateway.application.transcripts.list_messages(
        owner_canonical_user_id=owner,
        session_key="session_binding",
        before_cursor=None,
        limit=20,
    )
    assert "link_bearer_secret" not in history.model_dump_json()
    # A reload shows this page's own line, never the generic one.
    assert history.messages[-1].content == "Account link form opened for Hugging Face (hf) (your account)."
    assert "A setup page is ready" not in history.model_dump_json()
    assert "link_bearer_secret" not in caplog.text


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_create_another_broker_session(handoff):
    gateway, envelope, create, transport = handoff
    await asyncio.gather(*(gateway.inbound_messages.handle(envelope) for _ in range(3)))
    create.assert_awaited_once()
    assert transport.durable == []
    actions = gateway.application.transcripts.list_credential_actions(
        owner_canonical_user_id=gateway.config.user.canonical_user_id
    )
    assert len(actions) == 1
    # Another page's result changes the shared chat request's status; this page
    # still waits, so its card stays (a card follows its own page).
    gateway.state_store.update_request_status(
        "correlation_binding", "waiting_for_credential_result"
    )
    assert _listed(gateway) == [envelope.msg_content.setup_request_id]


@pytest.mark.asyncio
async def test_delegated_setup_without_chat_request_gets_its_own_form(handoff):
    gateway, envelope, create, transport = handoff
    envelope.msg_meta.msg_correlation_id = "delegated_correlation"
    envelope.msg_content.parent_task_id = "delegated_task"
    await asyncio.gather(*(gateway.inbound_messages.handle(envelope) for _ in range(3)))
    create.assert_awaited_once()
    assert transport.durable == []  # Never wake the task with a synthetic failure.
    record = gateway.state_store.get_request("delegated_correlation")
    assert record.followup_to_task_id == "delegated_task"
    assert record.owner_canonical_user_id == gateway.config.user.canonical_user_id
    # The original chat task keeps its own coordination record.
    assert gateway.state_store.get_request("correlation_binding").active_task_id == "task_binding"
    finished = await gateway._credential_workflow.tool_setups._finish(
        envelope.msg_content.setup_request_id,
        PageEnding("declined", "The form was closed without linking anything."),
        attempt=1,
        source="task_request",
    )
    assert finished
    result = transport.durable[-1][0].msg_content
    assert transport.durable[-1][0].msg_meta.msg_correlation_id == "delegated_correlation"
    assert result.parent_task_id == "delegated_task"
    assert result.status == "declined"
    assert result.message == "The form was closed without linking anything."


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["owner", "conversation", "sender"])
async def test_delegated_setup_rejects_untrusted_lineage_even_on_redelivery(
    handoff, mismatch
):
    gateway, envelope, create, _ = handoff
    envelope.msg_meta.msg_correlation_id = "delegated_correlation"
    envelope.msg_content.parent_task_id = "delegated_task"
    if mismatch == "owner":
        envelope.msg_meta.gateway_context.canonical_user_id = "another_owner"
    elif mismatch == "conversation":
        envelope.msg_meta.conversation_id = "another_conversation"
    else:
        envelope.msg_header.from_agent_id = "background_worker"
    await asyncio.gather(*(gateway.inbound_messages.handle(envelope) for _ in range(3)))
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["owner", "task", "conversation", "context_session", "channel"]
)
async def test_mismatched_lineage_never_opens_broker(handoff, field):
    gateway, envelope, create, _ = handoff
    if field == "owner":
        envelope.msg_meta.gateway_context.canonical_user_id = "another_user"
    elif field == "task":
        envelope.msg_content.parent_task_id = "another_task"
    elif field == "conversation":
        envelope.msg_meta.conversation_id = "another_conversation"
    elif field == "context_session":
        envelope.msg_meta.gateway_context.session_key = "another_session"
    else:
        gateway.session_repository.upsert_session(
            session_key="session_binding",
            channel_name="telegram",
            conversation_id="conversation_binding",
            last_task_id="task_binding",
            manager_agent_id=gateway.config.manager.manager_agent_id,
            user_id=gateway.config.user.canonical_user_id,
            chat_id="other",
            thread_id=None,
        )
    assert await gateway._credential_handoffs.try_present(envelope) is False
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_issued_form_is_not_reminted_after_memory_loss(handoff):
    gateway, envelope, create, _ = handoff
    await gateway.inbound_messages.handle(envelope)
    gateway._credential_handoffs._pending.clear()
    assert await gateway._credential_handoffs.try_present(envelope) is False
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_form_is_removed_from_live_poll(handoff):
    gateway, envelope, _, _ = handoff
    await gateway.inbound_messages.handle(envelope)
    pending = gateway._credential_handoffs._pending
    item = pending[envelope.msg_content.setup_request_id]
    pending[item.request_id] = replace(
        item, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    assert (
        gateway.application.transcripts.list_credential_actions(
            owner_canonical_user_id=gateway.config.user.canonical_user_id
        )
        == ()
    )


@pytest.mark.asyncio
async def test_broker_refusal_publishes_unavailable_and_tells_the_person_why(handoff):
    gateway, envelope, create, transport = handoff
    envelope.msg_content.owner = "team"
    create.side_effect = GatewaySecureBrokerRefusal(
        "Only a team administrator or owner can link the team's account.",
        status=403,
        reason_code="account_link_team_admin_required",
    )
    await gateway.inbound_messages.handle(envelope)
    create.assert_awaited_once()
    assert len(transport.durable) == 1
    result = transport.durable[0][0].msg_content
    assert (result.status, result.owner, result.needs_administrator) == ("unavailable", "team", True)
    assert result.message == (
        "Only a team administrator or owner can link the team's account. A team administrator or "
        "owner can link the team's account with /link cli:huggingface team."
    )
    assert (
        gateway.application.transcripts.list_credential_actions(
            owner_canonical_user_id=gateway.config.user.canonical_user_id
        )
        == ()
    )
    history = gateway.application.transcripts.list_messages(
        owner_canonical_user_id=gateway.config.user.canonical_user_id,
        session_key="session_binding",
        before_cursor=None,
        limit=20,
    )
    # The refusal carries no form link, so the explanation itself is kept.
    assert history.messages[-1].content == result.message
    assert history.messages[-1].message_type == "tool_setup_unavailable"


@pytest.mark.asyncio
async def test_completed_request_redelivery_does_not_reopen_the_form(handoff):
    gateway, envelope, create, transport = handoff
    await gateway.inbound_messages.handle(envelope)
    gateway.state_store.update_request_status("correlation_binding", "delivered")
    gateway._credential_handoffs._pending.clear()
    await gateway.inbound_messages.handle(envelope)
    create.assert_awaited_once()
    assert transport.durable == []


@pytest.mark.asyncio
async def test_set_up_tools_opens_a_sibling_page_beside_one_that_waits(handoff):
    """set_up_tools opens all of a task's pages at once; each gets its own form (owner's decision 2026-09-28)."""

    gateway, envelope, create, _transport = handoff
    await gateway.inbound_messages.handle(envelope)
    sibling = envelope.model_copy(deep=True)
    sibling.msg_content.setup_request_id = "account_link_request_sibling"
    sibling.msg_content.tool_id = "http:portainer"

    assert await gateway._credential_handoffs.try_present(sibling) is True

    assert create.await_count == 2
    actions = gateway.application.transcripts.list_credential_actions(
        owner_canonical_user_id=gateway.config.user.canonical_user_id
    )
    assert len(actions) == 2
    # A page of another task still does not open in this chat request.
    stranger = sibling.model_copy(deep=True)
    stranger.msg_content.setup_request_id = "account_link_request_stranger"
    stranger.msg_content.parent_task_id = "task_other"
    assert await gateway._credential_handoffs.try_present(stranger) is False


@pytest.mark.asyncio
async def test_the_setup_workflow_notice_shows_in_web_chat_with_its_link(handoff):
    """set_up_tools tells the person about its workflow in one chat message (live run, 2026-09-28)."""

    from custom_messages.contracts.delivery_v1 import UserNotification

    gateway, envelope, _create, _transport = handoff
    # Quiet hours (22:00-08:00 in the owner's zone by default) hold a non-urgent
    # notice for the digest; this test is about Web Chat showing it, at any hour.
    gateway.config.delivery.quiet_hours.enabled = False
    owner = gateway.config.user.canonical_user_id
    link = "http://gateway.example/workflows/access_flow_958d726dd6b4"
    notice = envelope.model_copy(deep=True)
    notice.msg_content = UserNotification(
        text=f"I need 2 things before I can list your private repos: Access to GitHub; Sign in to GitHub. "
        f"Follow along here: {link}",
        reason="tool_setup_workflow",
        source_task_id="task_binding",
    )
    notice.msg_meta.msg_correlation_id = "notify_workflow"
    notice.msg_meta.followup_to_task_id = "task_binding"

    await gateway.inbound_messages.handle(notice)

    history = gateway.application.transcripts.list_messages(
        owner_canonical_user_id=owner, session_key="session_binding", before_cursor=None, limit=20
    )
    [shown] = [message for message in history.messages if message.message_type == "user_notification"]
    assert link in shown.content


# Each page's card stays until that page ends --------------------------------------------
#
# Incident (2026-11-14, summarised): set_up_tools opened two outside steps for one
# task, one through the live reply and one through the handoff. Marking the first
# done removed the second's card from Web Chat and the workflow page, because each
# page's result changes the chat request the pages share, and the card list
# followed that request's status instead of the card's own page.

CHAT_REQUEST = "correlation_binding"
# The workflow page lists the cards of its own conversation and chat request.
WORKFLOW = {"source_session_key": "session_binding", "source_correlation_id": CHAT_REQUEST}


class _Steps:
    """Catalog steps done outside Universal Agents for two tools, and the task's workflow page."""

    TOOLS = {
        ("http:registrar_one", "create_key"): ("Registrar One", "https://registrar-one.example.com/account/api"),
        ("http:registrar_two", "create_token"): ("Registrar Two", "https://registrar-two.example.com/profile/tokens"),
    }

    def display_name(self, tool_id: str):
        return next((name for (tool, _step), (name, _url) in self.TOOLS.items() if tool == tool_id), None)

    def external_step(self, tool_id: str, step_id: str):
        found = self.TOOLS.get((tool_id, step_id))
        if found is None:
            return None
        name, url = found
        return name, ExternalSetupStepV1(
            step_id=step_id,
            title=f"Create a {name} API key",
            instructions=f"Sign in to {name}, open the API page and create a key that can read your account.",
            url=url,
        )

    def workflow_url(self, task_id: str):
        return "/workflows/access_flow_registrars" if task_id == "task_binding" else None


def _page(
    envelope: MessageEnvelope,
    setup_request_id: str,
    *,
    tool_id: str,
    kind: str = "account_link",
    owner: str = "person",
    step_id: str | None = None,
) -> MessageEnvelope:
    page = envelope.model_copy(deep=True)
    page.msg_content = ToolSetupRequestV1(
        setup_request_id=setup_request_id,
        parent_task_id="task_binding",
        tool_id=tool_id,
        kind=kind,
        owner=owner,
        step_id=step_id,
        reason=f"Set up {tool_id} so I can compare the two accounts.",
    )
    return page


def _outside_step(envelope: MessageEnvelope, setup_request_id: str, tool_id: str) -> MessageEnvelope:
    step_id = "create_key" if tool_id == "http:registrar_one" else "create_token"
    return _page(envelope, setup_request_id, tool_id=tool_id, kind="external_step", step_id=step_id)


async def _open_through_the_live_reply(gateway: UserGateway, page: MessageEnvelope):
    """The page arrives while the chat waits for the task's reply (what a Web Chat send waits on)."""

    async with gateway._manager_client.response_channel(CHAT_REQUEST):
        await gateway._manager_client.handle_response(page)
        response = await gateway._response_coordinator.await_terminal_response(
            correlation_id=CHAT_REQUEST, session_key="session_binding", query_handler=None
        )
    assert response.transient_action is not None, response.text
    return response


def _listed(gateway: UserGateway) -> list[str]:
    """The pages whose cards Web Chat lists (its live events read)."""

    owner = gateway.config.user.canonical_user_id
    return [
        item.request_id
        for item in gateway.application.transcripts.list_credential_actions(owner_canonical_user_id=owner)
    ]


def _on_workflow_page(gateway: UserGateway) -> list[str]:
    """The pages whose cards the workflow page lists (the filter its read uses)."""

    handlers = WebsiteWorkflowHandlers(application=gateway.application, operations=None)
    return [action["request_id"] for action in handlers._credential_actions(gateway.config.user.canonical_user_id, WORKFLOW)]


async def _end(gateway: UserGateway, setup_request_id: str, ending: str) -> None:
    """End one page: Mark Done for an outside step, otherwise how its page watcher would end it."""

    setups = gateway._credential_workflow.tool_setups
    if ending == "marked_done":
        assert await setups.external_step_done(setup_request_id, person_id=gateway.config.user.canonical_user_id, step_title="Create a key")
        return
    assert await setups._finish(
        setup_request_id, PageEnding(ending, f"The page ended: {ending}."), attempt=1, source="task_request"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sibling_kind", ["external_step", "account_link"])
@pytest.mark.parametrize("first_ending", ["marked_done", "completed", "declined", "expired"])
async def test_one_page_ending_leaves_the_other_pages_card_until_its_own_page_ends(handoff, first_ending, sibling_kind):
    gateway, envelope, _create, transport = handoff
    gateway._credential_workflow.tool_setups.guide = _Steps()
    first_id, sibling_id = "tool_setup_req_first", "tool_setup_req_sibling"
    if first_ending == "marked_done":
        first = _outside_step(envelope, first_id, "http:registrar_one")
    else:
        first = _page(envelope, first_id, tool_id="http:registrar_one")
    await _open_through_the_live_reply(gateway, first)
    if sibling_kind == "external_step":
        sibling = _outside_step(envelope, sibling_id, "http:registrar_two")
    else:
        sibling = _page(envelope, sibling_id, tool_id="http:registrar_two")
    assert await gateway._credential_handoffs.try_present(sibling) is True
    assert _listed(gateway) == [sibling_id] == _on_workflow_page(gateway)

    await _end(gateway, first_id, first_ending)

    result = transport.durable[-1][0].msg_content
    expected_status = "completed" if first_ending == "marked_done" else first_ending
    assert (result.setup_request_id, result.status) == (first_id, expected_status)
    assert gateway.state_store.get_request(CHAT_REQUEST).status == "waiting_for_credential_result"
    expected = [sibling_id]
    if first_ending == "expired":
        # The run offers an expired page again; the new page opens beside the sibling.
        again = _page(envelope, "tool_setup_req_first_again", tool_id="http:registrar_one")
        assert await gateway._credential_handoffs.try_present(again) is True
        expected.append("tool_setup_req_first_again")
    assert _listed(gateway) == expected
    assert _on_workflow_page(gateway) == expected

    await _end(gateway, sibling_id, "marked_done" if sibling_kind == "external_step" else "completed")

    assert _listed(gateway) == expected[1:]
    assert _on_workflow_page(gateway) == expected[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "declined", "unavailable", "expired"])
async def test_a_card_closes_when_its_own_page_ends(handoff, caplog, status):
    gateway, envelope, _create, _transport = handoff
    caplog.set_level(logging.INFO)
    page_id = envelope.msg_content.setup_request_id
    await gateway.inbound_messages.handle(envelope)
    assert _listed(gateway) == [page_id]

    await _end(gateway, page_id, status)

    assert _listed(gateway) == []
    assert (
        f"event=gateway_credential_handoff_card_closed request_id={page_id} correlation_id={CHAT_REQUEST} "
        f"session_key=session_binding reason=page_ended page_status={status}"
    ) in caplog.text


@pytest.mark.asyncio
async def test_a_page_offered_again_replaces_the_expired_pages_card(handoff):
    gateway, envelope, _create, _transport = handoff
    await gateway.inbound_messages.handle(envelope)
    await _end(gateway, envelope.msg_content.setup_request_id, "expired")

    again = _page(envelope, "account_link_request_again", tool_id="cli:huggingface")
    assert await gateway._credential_handoffs.try_present(again) is True

    assert _listed(gateway) == ["account_link_request_again"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["completed", "delivered", "finished_run"])
async def test_cards_close_when_their_task_ends_while_the_page_still_waits(handoff, caplog, ending):
    gateway, envelope, _create, _transport = handoff
    caplog.set_level(logging.INFO)
    page_id = envelope.msg_content.setup_request_id
    if ending == "finished_run":
        # A thread's request records that it ended in its run state; a refused
        # final reply finishes it and leaves its status as it was.
        gateway.state_store.record_request(
            correlation_id=CHAT_REQUEST,
            session_key="session_binding",
            request_kind="interactive",
            request_text="bind a credential",
            conversation_id="conversation_binding",
            followup_to_task_id=None,
            status="waiting_for_permission",
            owner_canonical_user_id=gateway.config.user.canonical_user_id,
            active_task_id="task_binding",
            run_state="running",
        )
    await gateway.inbound_messages.handle(envelope)
    assert _listed(gateway) == [page_id]

    if ending == "finished_run":
        assert gateway._request_service.finish(CHAT_REQUEST)
        assert gateway.state_store.get_request(CHAT_REQUEST).status == "waiting_for_credential"
    else:
        gateway.state_store.update_request_status(CHAT_REQUEST, ending)

    assert _listed(gateway) == []
    assert gateway.state_store.tool_setup_repository.get_tool_setup(page_id).status == "waiting"
    assert f"request_id={page_id} correlation_id={CHAT_REQUEST} session_key=session_binding reason=task_ended" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "reason"), [("request_id", "page_record_missing"), ("correlation_id", "request_missing")]
)
async def test_a_card_whose_records_are_missing_is_closed_with_a_warning(handoff, caplog, field, reason):
    gateway, envelope, _create, _transport = handoff
    page_id = envelope.msg_content.setup_request_id
    await gateway.inbound_messages.handle(envelope)
    pending = gateway._credential_handoffs._pending
    pending["account_link_request_orphan"] = replace(pending[page_id], **{field: "unknown_id"})

    assert _listed(gateway) == [page_id]

    [warning] = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "event=gateway_credential_handoff_card_closed" in record.getMessage()
    ]
    assert f"reason={reason}" in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "tool_id", "owner"),
    [
        ("sign_in", "cli:x", "person"),
        ("team_sign_in_app", "cli:x", "team"),
        ("account_link", "http:registrar_one", "person"),
        ("external_step", "http:registrar_one", "person"),
    ],
)
async def test_a_cards_lifetime_follows_whether_it_holds_a_bearer_link(handoff, monkeypatch, kind, tool_id, owner):
    """A card with a page's bearer link stays in memory at most 31 minutes; an outside step's keeps its own expiry."""

    gateway, envelope, create, _transport = handoff
    gateway._credential_workflow.tool_setups.guide = _Steps()
    # The broker reports pages that stay open for two hours.
    two_hours = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    create.return_value = {**create.return_value, "expires_at": two_hours}
    monkeypatch.setattr(
        gateway._secure_broker,
        "create_auth_session",
        AsyncMock(
            return_value={
                "status": "created",
                "login_url": "http://127.0.0.1:8766/auth/x/v1.signed_session",
                "expires_at": two_hours,
            }
        ),
    )
    if kind == "external_step":
        page = _outside_step(envelope, "tool_setup_req_kind", tool_id)
    else:
        page = _page(envelope, "tool_setup_req_kind", tool_id=tool_id, kind=kind, owner=owner)
    opened_at = datetime.now(timezone.utc)

    assert await gateway._credential_handoffs.try_present(page) is True

    [card] = gateway._credential_handoffs._pending.values()
    if kind == "external_step":
        assert card.expires_at == datetime.fromisoformat(card.action.expires_at_utc)
        assert card.expires_at > opened_at + timedelta(hours=47)
    else:
        assert opened_at + timedelta(minutes=30) < card.expires_at <= datetime.now(timezone.utc) + timedelta(minutes=31)


# A page ending while the team lead is away (review of the 2026-11-14 incident fixes) ------


def _thread_request(gateway: UserGateway) -> None:
    """The chat request as Web Chat records it: a thread request whose run is running."""

    gateway.state_store.record_request(
        correlation_id=CHAT_REQUEST,
        session_key="session_binding",
        request_kind="interactive",
        request_text="bind a credential",
        conversation_id="conversation_binding",
        followup_to_task_id=None,
        status="waiting_for_permission",
        owner_canonical_user_id=gateway.config.user.canonical_user_id,
        active_task_id="task_binding",
        run_state="running",
    )


async def _lead_announces(gateway: UserGateway, lead_id: str) -> None:
    """The team's records name ``lead_id`` as the team lead, and the lead follower routes chats to it, as on
    release/0.10 (``assistant_gateway.team_view.TeamLeadFollower``, the one caller of ``set_lead``)."""

    await gateway._manager_client.set_lead(lead_id, source=team_view.SOURCE)


def _setup_results(transport) -> list[tuple[str, str, str | None]]:
    """Each setup result sent: its page, the lead's queue and the NATS message id."""

    return [
        (envelope.msg_content.setup_request_id, routing_key, envelope.msg_header.idempotency_key)
        for envelope, routing_key in transport.durable
        if isinstance(envelope.msg_content, ToolSetupResultV1)
    ]


@pytest.mark.asyncio
async def test_a_page_ending_while_the_team_lead_is_away_reaches_the_next_lead_and_keeps_the_other_cards(
    handoff, caplog
):
    gateway, envelope, _create, transport = handoff
    caplog.set_level(logging.INFO)
    _thread_request(gateway)
    setups = gateway._credential_workflow.tool_setups
    setups.guide = _Steps()
    gateway.manager_transitions.start()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    await _open_through_the_live_reply(gateway, _outside_step(envelope, "tool_setup_req_first", "http:registrar_one"))
    assert await gateway._credential_handoffs.try_present(
        _outside_step(envelope, "tool_setup_req_sibling", "http:registrar_two")
    )

    # The team lead stops while the person's chat still waits for its reply: the lead
    # follower clears the route, and the unavailable transition fails that wait.
    async with gateway._manager_client.response_channel(CHAT_REQUEST):
        await gateway._manager_client.mark_manager_unavailable(reason="the team lead stopped")
    request = gateway.state_store.get_request(CHAT_REQUEST)
    assert (request.status, request.run_state) == ("manager_unavailable", "running")

    assert await setups.external_step_done(
        "tool_setup_req_first", person_id=gateway.config.user.canonical_user_id, step_title="Create a key"
    )

    assert _setup_results(transport) == []
    assert gateway.state_store.tool_setup_repository.get_tool_setup("tool_setup_req_first").result_state == "unsent"
    assert gateway.state_store.get_request(CHAT_REQUEST).run_state == "running"
    assert _listed(gateway) == ["tool_setup_req_sibling"] == _on_workflow_page(gateway), "the task still runs"
    assert "reason=task_ended" not in caplog.text

    await _lead_announces(gateway, "manager_replacement")

    assert _setup_results(transport) == [
        (
            "tool_setup_req_first",
            "manager_replacement",
            "secure_result:tool_setup_result:tool_setup_req_first:manager_replacement",
        )
    ]
    assert gateway.state_store.tool_setup_repository.get_tool_setup("tool_setup_req_first").result_state == "sent"
    # The new lead's next page for the task opens beside the sibling.
    later = _page(envelope, "tool_setup_req_later", tool_id="http:registrar_one")
    later.msg_header.from_agent_id = "manager_replacement"
    assert await gateway._credential_handoffs.try_present(later) is True
    assert _listed(gateway) == ["tool_setup_req_sibling", "tool_setup_req_later"]

    await _lead_announces(gateway, "manager_third")

    assert len(_setup_results(transport)) == 1, "a result is sent once"


@pytest.mark.asyncio
async def test_a_page_for_a_running_task_opens_while_its_request_says_manager_unavailable(handoff, caplog):
    """The window before any kept result is resent: the request still reads manager_unavailable (finding F4).

    The lead stopped while the chat's reply wait was open, a new lead is back,
    no kept result rewrote the status, and the new lead sends a page for the
    same task: the run state, not the status, decides that the task still runs.
    """

    gateway, envelope, _create, _transport = handoff
    caplog.set_level(logging.INFO)
    _thread_request(gateway)
    gateway._credential_workflow.tool_setups.guide = _Steps()
    gateway.manager_transitions.start()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]

    async with gateway._manager_client.response_channel(CHAT_REQUEST):
        await gateway._manager_client.mark_manager_unavailable(reason="the team lead stopped")
    await _lead_announces(gateway, "manager_replacement")
    request = gateway.state_store.get_request(CHAT_REQUEST)
    assert (request.status, request.run_state) == ("manager_unavailable", "running")

    later = _page(envelope, "tool_setup_req_after_outage", tool_id="http:registrar_one")
    later.msg_header.from_agent_id = "manager_replacement"
    assert await gateway._credential_handoffs.try_present(later) is True
    assert _listed(gateway) == ["tool_setup_req_after_outage"]


def _request(*, status: str, run_state: str | None) -> RequestRecord:
    return RequestRecord(
        correlation_id=CHAT_REQUEST, session_key="session_binding", request_kind="interactive",
        request_text="bind a credential", conversation_id="conversation_binding", followup_to_task_id=None,
        created_at="2026-11-19T10:00:00+00:00", status=status, run_state=run_state,
    )


@pytest.mark.parametrize(
    ("status", "run_state", "ended"),
    [
        ("manager_unavailable", "running", False),
        ("completed", "running", False),
        ("waiting_for_credential", "finished", True),
        ("manager_unavailable", None, True),
        ("delivered", None, True),
        ("waiting_for_credential_result", None, False),
    ],
)
def test_a_threads_run_state_decides_whether_its_task_ended(status, run_state, ended):
    """Thread requests end with their run; other rows at a terminal status (card removal and page admission)."""

    assert request_task_ended(_request(status=status, run_state=run_state)) is ended
