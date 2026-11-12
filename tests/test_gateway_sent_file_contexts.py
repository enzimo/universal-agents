"""The gateway vouches for the files it sends to a request (access_control.ownership.sent_files_access).

A new request and each guidance sent to it carry a context the gateway signs
for that message, naming the request's task and its conversation, with an id
bound to that message and its files. The team lead keeps a message's files
only under such a context. It refuses a guidance's files under the request's
own context, which every worker of the request holds a copy of, and under
the guidance's context reused with other files. Every agent process receives
each guidance. Uses the real gateway with Web Chat uploads, so it checks what
reaches the team lead.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_control.ownership import request_context_id, sent_files_access, steering_context_id
from custom_agents.task_context_runtime import context_verifier
from custom_messages.enums import RoutingType
from tests.broker_access_support import forget_task_context_keys, use_gateway_key
from tests.test_gateway_parallel_threads import _gateway, _send, _web_chat_thread
from tests.test_gateway_task_steering import RUN_STATE_RUNNING
from tests.test_gateway_website_app import InternalWebsiteInteractiveApp


@pytest.fixture
def gateway_key(monkeypatch: pytest.MonkeyPatch):
    use_gateway_key(monkeypatch)
    yield
    forget_task_context_keys()


@pytest.mark.asyncio
async def test_a_request_and_its_guidance_carry_contexts_naming_the_task_and_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway_key: None
) -> None:
    gateway, transport = _gateway(tmp_path, web_chat=True, monkeypatch=monkeypatch)

    async def unanswered(envelope, *, routing_key):
        # The lead does not answer, so the guidance stays held and is sent again.
        transport.operational.append((envelope, routing_key))

    monkeypatch.setattr(transport, "publish_operational", unanswered)
    guidance_service = gateway._thread_guidance
    guidance_service._settings = dataclasses.replace(guidance_service._settings, status_timeout_seconds=0.2)
    app = InternalWebsiteInteractiveApp(gateway.config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        thread = await _web_chat_thread(client)
        conversation_id = thread["conversation_id"]
        assert (await _send(client, thread, "Read this.", request_id="request", with_file=True)).status == 200
        # A request still running in the thread: the next message is guidance for it.
        gateway.state_store.record_request(
            correlation_id="corr_open",
            session_key=thread["session_key"],
            request_kind="interactive",
            request_text="Read this.",
            conversation_id=conversation_id,
            followup_to_task_id=None,
            active_task_id="task_open",
            run_state=RUN_STATE_RUNNING,
        )
        assert (await _send(client, thread, "And this.", request_id="guidance", with_file=True)).status == 200
        await guidance_service.retry_held()

    [request] = transport.durable
    [(guidance, routing_key), (resent, _)] = transport.operational
    assert request.msg_content.attachments and guidance.msg_content.attachments
    # Guidance goes to every agent process of the team, not only the lead.
    assert routing_key == RoutingType.ALL_AGENTS_ROUTE.value
    verifier = context_verifier()
    request_context = verifier.verify(request.msg_meta.gateway_context.task_context)
    guidance_context = verifier.verify(guidance.msg_meta.gateway_context.task_context)
    resent_context = verifier.verify(resent.msg_meta.gateway_context.task_context)
    task_id = request.msg_content.task_id
    assert (request_context.root_task_id, request_context.conversation_id) == (task_id, conversation_id)
    assert (guidance_context.root_task_id, guidance_context.conversation_id) == ("task_open", conversation_id)
    assert guidance_context.person_id == request_context.person_id
    # Each context is bound to its message and files; a resend is the same message.
    assert request_context.context_id == request_context_id(request.msg_content)
    assert guidance_context.context_id == steering_context_id(guidance.msg_content)
    assert resent_context.context_id == guidance_context.context_id
    assert resent.msg_meta.gateway_context.task_context.signature != guidance.msg_meta.gateway_context.task_context.signature

    # What the team lead decides for each message's files, from the message as it arrives.
    person = request_context.person_id
    assert sent_files_access(
        sender=request_context,
        task_id=task_id,
        conversation_id=conversation_id,
        person_id=person,
        message_context_id=request_context_id(request.msg_content),
    ).allowed
    assert sent_files_access(
        sender=guidance_context,
        task_id="task_open",
        conversation_id=conversation_id,
        person_id=person,
        message_context_id=steering_context_id(guidance.msg_content),
    ).allowed
    # The request's own context sent back with a guidance (a worker's copy) is refused.
    copied = sent_files_access(
        sender=request_context,
        task_id=task_id,
        conversation_id=conversation_id,
        person_id=person,
        message_context_id=steering_context_id(guidance.msg_content),
    )
    assert (copied.allowed, copied.reason) == (False, "context_not_for_message")
    # The guidance's context, which every agent process received, reused with another file.
    other_file = guidance.msg_content.attachments[0].model_copy(update={"attachment_id": "att_bobs"})
    reused = sent_files_access(
        sender=guidance_context,
        task_id="task_open",
        conversation_id=conversation_id,
        person_id=person,
        message_context_id=steering_context_id(guidance.msg_content.model_copy(update={"attachments": [other_file]})),
    )
    assert (reused.allowed, reused.reason) == (False, "context_not_for_message")
