"""Exercise tool setup requests (an account link form) arriving after the chat HTTP response has ended."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.application.ports import GatewaySecureBrokerRefusal
from assistant_gateway.application.tool_setup import PageEnding
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.task_v1 import TaskSteering, TaskSteeringInterruptRecord
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
    assert "A setup page is ready" in history.model_dump_json()
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
    gateway.state_store.update_request_status(
        "correlation_binding", "waiting_for_credential_result"
    )
    assert (
        gateway.application.transcripts.list_credential_actions(
            owner_canonical_user_id=gateway.config.user.canonical_user_id
        )
        == ()
    )


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
