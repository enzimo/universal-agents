"""A thread's Run concurrently setting decides how its requests run.

The person sets it per thread with the switch below Send in Web Chat (owner,
2026-10-29): the gateway saves it on the session, refuses a change while a
request is open in the thread's conversation, and gives each request the
setting it has when the request is admitted (``GatewayContext.parallel_thread``).
The team lead runs a request with the setting on beside its other work
(custom_agents.execution_lanes). These tests use the real gateway, so they
check what reaches the team lead, not only what the gateway stores.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.application.session_service import PARALLEL_MARK_CLEARED_NOTICE
from assistant_gateway.application.website_application import WebChatParallelSettingResult
from assistant_gateway.domain.sessions import ParallelSettingOutcome
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType
from tests.test_gateway_task_steering import RUN_STATE_RUNNING, _SteeringTransport
from tests.test_gateway_task_steering import _config as steering_config
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp

WEB_CHAT_TOKEN = "uaweb_parallel_test"
LOCKED_TEXT = "This chat is working on a request. You can change Run concurrently after it replies."


class _AnsweringTransport(_SteeringTransport):
    """The manager: answers each request at once and accepts each piece of guidance."""

    def __init__(self) -> None:
        super().__init__()
        self.durable: list[MessageEnvelope] = []

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        del routing_key
        self.durable.append(envelope)
        assert self.gateway is not None
        await self.gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=Task(
                    task_id=envelope.msg_content.task_id, task_type=MessageMetaType.RESPONSE, answer="Done."
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_agent_1", to_routing_path="team_steering_gateway", agent_signature=""
                ),
                msg_meta=envelope.msg_meta.model_copy(
                    update={"requester_type": RequesterType.USER_REQUESTER, "is_response_requested": False}
                ),
            )
        )

    def marks(self) -> list[bool]:
        return [envelope.msg_meta.gateway_context.parallel_thread for envelope in self.durable]


def _gateway(tmp_path: Path, *, web_chat: bool = False, monkeypatch=None) -> tuple[UserGateway, _AnsweringTransport]:
    config = steering_config(tmp_path)
    if web_chat:
        config.network.public_origin = "http://127.0.0.1"
        config.interactive_apps.internal_website.enabled = True
        config.interactive_apps.internal_website.web_chat_enabled = True
        config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
        monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", WEB_CHAT_TOKEN)
    transport = _AnsweringTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=transport,
    )
    transport.gateway = gateway
    return gateway, transport


def _set(gateway: UserGateway, session, value: bool) -> ParallelSettingOutcome:
    return gateway.session_repository.set_session_parallel(
        session.session_key, conversation_id=session.conversation_id, parallel_thread=value
    )


async def _web_chat_thread(client: TestClient) -> dict:
    await client.post("/api/web-chat/auth", json={"token": WEB_CHAT_TOKEN})
    created = await client.post("/api/web-chat/sessions")
    assert created.status == 201
    return (await created.json())["session"]


async def _send(client: TestClient, thread: dict, text: str, *, request_id: str, with_file: bool = False):
    payload = {
        "session_key": thread["session_key"],
        "conversation_id": thread["conversation_id"],
        "message": text,
        "client_request_id": request_id,
    }
    if not with_file:
        return await client.post("/api/web-chat/chat", json=payload)
    form = FormData()
    for key, value in payload.items():
        form.add_field(key, value)
    form.add_field("attachments[]", b"notes", filename="notes.txt", content_type="text/plain")
    return await client.post("/api/web-chat/chat", data=form)


async def _switch(client: TestClient, thread: dict, value, **extra):
    body = {"parallel_thread": value, "conversation_id": thread["conversation_id"], **extra}
    return await client.patch(f"/api/web-chat/sessions/{thread['session_key']}", json=body)


async def _listed(client: TestClient, thread: dict) -> dict:
    sessions = (await (await client.get("/api/web-chat/sessions")).json())["sessions"]
    return next(item for item in sessions if item["session_key"] == thread["session_key"])


async def _thread_states(client: TestClient) -> dict:
    return await (await client.get("/api/web-chat/events")).json()


@pytest.mark.asyncio
async def test_each_request_carries_its_threads_setting_and_others_do_not(tmp_path: Path) -> None:
    gateway, transport = _gateway(tmp_path)
    route = {"channel_name": "cli", "user_id": "user", "chat_id": "cli"}
    concurrent = gateway.application.start_new_session(**route)
    ordinary = gateway.application.start_new_session(**route)
    assert not concurrent.parallel_thread, "a new thread starts with Run concurrently off"
    assert _set(gateway, concurrent, True) is ParallelSettingOutcome.SAVED

    await gateway.application.send_user_message("First request", session_key=concurrent.session_key, **route)
    await gateway.application.send_user_message(
        "Later request in the same thread", session_key=concurrent.session_key, **route
    )
    await gateway.application.send_user_message("Ordinary request", session_key=ordinary.session_key, **route)

    assert [
        (envelope.msg_meta.gateway_context.parallel_thread, envelope.msg_meta.gateway_context.delivery_mode)
        for envelope in transport.durable
    ] == [(True, "interactive"), (True, "interactive"), (False, "interactive")]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_file", [False, True])
async def test_the_switch_keeps_the_conversation_and_later_requests_follow_it(
    tmp_path: Path, monkeypatch, with_file
) -> None:
    """Owner, 2026-10-29: the switch continues the current thread with its history."""

    gateway, transport = _gateway(tmp_path, web_chat=True, monkeypatch=monkeypatch)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        thread = await _web_chat_thread(client)
        assert thread["parallel_thread"] is False
        assert (await _send(client, thread, "Message 0", request_id="reply_0")).status == 200
        before = await _thread_states(client)

        switched = await _switch(client, thread, True)
        assert switched.status == 200, await switched.text()
        assert (await switched.json())["parallel_thread"] is True
        assert (await _listed(client, thread))["parallel_thread"] is True
        after = await _thread_states(client)
        assert after["threads_revision"] != before["threads_revision"], "other open pages reload the setting"
        for index in (1, 2):
            response = await _send(client, thread, f"Message {index}", request_id=f"reply_{index}", with_file=with_file)
            assert response.status == 200, await response.text()

        assert (await _switch(client, thread, False)).status == 200
        assert (await _send(client, thread, "Message 3", request_id="reply_3")).status == 200
        history = await (await client.get(f"/api/web-chat/sessions/{thread['session_key']}/messages")).json()

    assert transport.marks() == [False, True, True, False]
    assert {envelope.msg_meta.conversation_id for envelope in transport.durable} == {thread["conversation_id"]}
    first_task = transport.durable[0].msg_content.task_id
    assert transport.durable[1].msg_meta.followup_to_task_id == first_task, "the model keeps the thread's history"
    user_rows = [item["content"] for item in history["messages"] if item["role"] == "user"]
    assert user_rows == ["Message 0", "Message 1", "Message 2", "Message 3"]


@pytest.mark.asyncio
async def test_the_switch_is_locked_while_a_request_is_open(tmp_path: Path, monkeypatch) -> None:
    """A follow-up sent while a request is open is guidance for that request.

    Changing the setting then could not apply to anything the person sees,
    so the gateway refuses it until the request's reply is recorded; a request
    open in another conversation of the same thread does not lock it.
    """

    gateway, transport = _gateway(tmp_path, web_chat=True, monkeypatch=monkeypatch)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        thread = await _web_chat_thread(client)
        record = {
            "session_key": thread["session_key"],
            "request_kind": "interactive",
            "request_text": "Prepare the report",
            "followup_to_task_id": None,
            "run_state": RUN_STATE_RUNNING,
        }
        gateway.state_store.record_request(
            correlation_id="corr_elsewhere", conversation_id="conv_older", active_task_id="task_older", **record
        )
        assert (await _thread_states(client))["open_request_session_keys"] == []
        assert (await _switch(client, thread, True)).status == 200
        assert (await _switch(client, thread, False)).status == 200

        gateway.state_store.record_request(
            correlation_id="corr_open", conversation_id=thread["conversation_id"], active_task_id="task_open", **record
        )
        assert (await _thread_states(client))["open_request_session_keys"] == [thread["session_key"]]
        refused = await _switch(client, thread, True)
        assert refused.status == 409 and await refused.text() == LOCKED_TEXT
        assert (await _listed(client, thread))["parallel_thread"] is False

        follow_up = await _send(client, thread, "Also cover June", request_id="follow_up")
        assert follow_up.status == 200, await follow_up.text()
        assert transport.durable == [], "the follow-up starts no request of its own"
        assert [envelope.msg_content.target_task_id for envelope, _ in transport.operational] == ["task_open"]

        gateway.state_store.request_repository.finish_request("corr_open")
        assert (await _thread_states(client))["open_request_session_keys"] == []
        assert (await _switch(client, thread, True)).status == 200


@pytest.mark.asyncio
async def test_the_switch_refuses_other_people_stale_pages_and_bad_values(tmp_path: Path, monkeypatch) -> None:
    gateway, _transport = _gateway(tmp_path, web_chat=True, monkeypatch=monkeypatch)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        thread = await _web_chat_thread(client)
        sessions = app.application.sessions
        assert sessions.set_web_chat_session_parallel(
            session_key=thread["session_key"], conversation_id=thread["conversation_id"],
            parallel_thread=True, owner_canonical_user_id="another-person",
        ) is WebChatParallelSettingResult.NOT_FOUND
        cli = gateway.application.start_new_session(channel_name="cli", user_id="user", chat_id="cli")
        assert sessions.set_web_chat_session_parallel(
            session_key=cli.session_key, conversation_id=cli.conversation_id,
            parallel_thread=True, owner_canonical_user_id=None,
        ) is WebChatParallelSettingResult.NOT_FOUND, "only Web Chat threads have the switch"

        stale = await _switch(client, thread, True, conversation_id="old-conversation")
        assert stale.status == 409
        assert await stale.text() == "This chat moved to another conversation. Reload it and try again."
        missing = await client.patch("/api/web-chat/sessions/no-such-thread",
                                     json={"parallel_thread": True, "conversation_id": "conv"})
        assert missing.status == 404
        for value in ("true", "yes", 1, None):
            assert (await _switch(client, thread, value)).status == 400
        no_conversation = await client.patch(f"/api/web-chat/sessions/{thread['session_key']}",
                                             json={"parallel_thread": True})
        assert no_conversation.status == 400
        both = await _switch(client, thread, True, session_alias="Report")
        assert both.status == 400

        assert (await _listed(client, thread))["parallel_thread"] is False


@pytest.mark.asyncio
async def test_a_request_takes_the_setting_saved_while_it_waited_for_admission(tmp_path: Path, monkeypatch) -> None:
    """The setting is read when the request is recorded as open, not when the message arrives.

    A message waits on the person's first-time setup check before it is
    admitted; a switch change saved in that gap must reach the request.
    """

    gateway, transport = _gateway(tmp_path, web_chat=True, monkeypatch=monkeypatch)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())

    class _SwitchDuringSetupCheck:
        thread: dict | None = None

        async def before_request(self, person_id: str, *, session) -> None:
            del person_id
            assert self.thread is not None
            assert _set(gateway, session, True) is ParallelSettingOutcome.SAVED

    check = _SwitchDuringSetupCheck()
    gateway._interactive_submission_service._personal_setup = check
    async with TestClient(TestServer(app.build_app())) as client:
        check.thread = await _web_chat_thread(client)
        response = await _send(client, check.thread, "Start the report", request_id="during_setup")
        assert response.status == 200, await response.text()

    assert transport.marks() == [True]


@pytest.mark.asyncio
async def test_a_thread_moved_to_another_conversation_turns_the_setting_off(tmp_path: Path) -> None:
    """Owner, 2026-10-29: the setting is the person's word about the thread's own conversation.

    ``/switch_conversation`` and ``/switch_topic`` move a thread to another
    conversation, which may be continued elsewhere, so they turn the setting
    off and tell the person; the thread's next request runs with ordinary work.
    Web Chat does not offer them; a Web Chat thread reaches them after the
    debug-only ``/switch_session`` moves it to another app.
    """

    gateway, transport = _gateway(tmp_path)
    route = {"channel_name": "cli", "user_id": "user", "chat_id": "cli"}
    thread = gateway.application.start_new_session(**route)
    assert _set(gateway, thread, True) is ParallelSettingOutcome.SAVED

    kept = gateway._session_service.switch_conversation(
        session_key=thread.session_key, conversation_id=thread.conversation_id, last_task_id=None
    )
    assert kept is not None and kept.parallel_thread, "staying in its own conversation keeps the setting"

    gateway.state_store.register_conversation_session(
        session_key=thread.session_key, conversation_id="conv_older", last_task_id="task_older"
    )
    result = await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text="/switch_conversation conv_older", current_session_key=thread.session_key, **route
        )
    )

    assert result.text == f"Active conversation: conv_older\n{PARALLEL_MARK_CLEARED_NOTICE}"
    moved = gateway.session_repository.get_session(thread.session_key)
    assert moved is not None and moved.conversation_id == "conv_older" and not moved.parallel_thread
    await gateway.application.send_user_message("Next request", session_key=thread.session_key, **route)
    assert transport.durable[-1].msg_meta.gateway_context.parallel_thread is False
    again = await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text=f"/switch_conversation {thread.conversation_id}", current_session_key=thread.session_key, **route
        )
    )
    assert again.text == f"Active conversation: {thread.conversation_id}", "no notice once the setting is off"
