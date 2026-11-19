"""Messages left on a reply wait when it closes are passed on, not lost.

Review of 2026-11-16 (summarised): ``set_up_tools`` sends all of a task's setup
pages at once. The chat's live reply ends at the first page, and while the
gateway opened that page through the broker, the second page was queued on the
same reply wait. Closing the wait discarded it: no page record, no card, no
result to the task and no log line, so the setup run waited forever. The
existing setup tests all delivered later pages after the wait had closed, which
is why none caught it.

Now a message still queued when its wait closes gets the handling it would have
had arriving after the close (``assistant_gateway.application.reply_leftovers``).
The gateway-level tests here wait for that background work by polling for its
last effect, and the file imports nothing the fix added at module level, so on
code without the fix they fail on the missing page or card, not on an import.

The messaging project will move these tests to the gateway's new receiver when
it replaces this reply path on release/0.10.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import pytest

from custom_messages.contracts.control_v1 import AgentStatus, SystemStatusMessage
from custom_messages.contracts.credentials_v1 import ToolSetupResultV1
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskSteering
from custom_messages.enums import ControlType, MessageMetaType, TaskSteeringType
from tests.test_gateway_credential_handoff import (  # noqa: F401 - pytest fixture
    CHAT_REQUEST,
    _listed,
    _page,
    handoff,
)
from tests.test_gateway_manager_client import _FakeManagerRuntime, _client, _settings

TASK = "task_binding"
SESSION = "session_binding"


@pytest.fixture
def chat(handoff):  # noqa: F811 - the imported pytest fixture
    """A gateway whose Web Chat request waits on its task (``tests/test_gateway_credential_handoff.handoff``)."""

    return handoff


async def _until(check: Callable[[], bool], *, what: str, seconds: float = 5.0) -> None:
    """Wait for the background work that passes leftovers on."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not check():
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting until {what}.")
        await asyncio.sleep(0.01)


# A1: two setup pages of one wave, the first a broker page --------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("arrival", ["before_the_wait", "during_the_first_pages_broker_call"])
async def test_a_second_setup_page_queued_on_the_live_reply_opens_with_its_card(chat, caplog, arrival):
    gateway, envelope, create, transport = chat
    caplog.set_level(logging.INFO)
    client = gateway._manager_client
    first = _page(envelope, "tool_setup_req_first", tool_id="cli:huggingface")
    second = _page(envelope, "tool_setup_req_second", tool_id="http:registrar_two")
    opened = create.return_value

    async def broker_call(request):
        # The second page reaches the live reply's wait while the gateway waits
        # on the broker to open the first.
        if arrival == "during_the_first_pages_broker_call" and request.request_id == "tool_setup_req_first":
            await client.handle_response(second)
        return opened

    create.side_effect = broker_call

    # What a Web Chat send waits on (interactive_submission._submit_new_task).
    async with client.response_channel(CHAT_REQUEST, task_id=TASK):
        await client.handle_response(first)
        if arrival == "before_the_wait":
            await client.handle_response(second)
        response = await gateway._response_coordinator.await_terminal_response(
            correlation_id=CHAT_REQUEST, session_key=SESSION, query_handler=None, response_task_id=TASK
        )
    assert response.transient_action is not None
    assert response.transient_action.request_id == "tool_setup_req_first"

    await _until(lambda: _listed(gateway) == ["tool_setup_req_second"], what="the second page's card is listed")

    setups = gateway.state_store.tool_setup_repository
    assert [setups.get_tool_setup(page).status for page in ("tool_setup_req_first", "tool_setup_req_second")] == [
        "waiting",
        "waiting",
    ]
    assert [call.args[0].request_id for call in create.await_args_list] == [
        "tool_setup_req_first",
        "tool_setup_req_second",
    ]
    # Opened, not refused: the task was sent no result for either page.
    assert not [e for e, _ in transport.durable if isinstance(e.msg_content, ToolSetupResultV1)]
    assert (
        f"event=gateway_reply_leftover_passed_on correlation_id={CHAT_REQUEST} task_id={TASK} "
        f"message_id={second.msg_header.message_id} content_type=ToolSetupRequestV1 "
        "content_id=tool_setup_req_second route=response"
    ) in caplog.text


# A2: a kind other than a setup page ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_notification_left_on_the_live_reply_reaches_the_chat(chat):
    gateway, envelope, _create, _transport = chat
    # Quiet hours would hold a non-urgent notice for the digest; this test is about it not being lost.
    gateway.config.delivery.quiet_hours.enabled = False
    client = gateway._manager_client
    owner = gateway.config.user.canonical_user_id
    notice = envelope.model_copy(deep=True)
    notice.msg_content = UserNotification(text="Both setup pages are open; I will continue once they are done.", source_task_id=TASK)

    async with client.response_channel(CHAT_REQUEST, task_id=TASK):
        await client.handle_response(_page(envelope, "tool_setup_req_first", tool_id="cli:huggingface"))
        await client.handle_response(notice)
        await gateway._response_coordinator.await_terminal_response(
            correlation_id=CHAT_REQUEST, session_key=SESSION, query_handler=None, response_task_id=TASK
        )

    def shown() -> list[str]:
        history = gateway.application.transcripts.list_messages(
            owner_canonical_user_id=owner, session_key=SESSION, before_cursor=None, limit=20
        )
        return [message.content for message in history.messages if message.message_type == "user_notification"]

    await _until(lambda: bool(shown()), what="the notification is in the chat history")
    assert shown() == [notice.msg_content.text]


# A3: a timed-out wait ---------------------------------------------------------------------


def _chat_that_cannot_show_late_pages(gateway, envelope) -> MessageEnvelope:
    """A Telegram request waiting on its task: late setup pages are shown only in Web Chat."""

    owner = gateway.config.user.canonical_user_id
    store = gateway.state_store
    store.create_session(
        session_key="session_telegram",
        conversation_id="conversation_telegram",
        manager_agent_id=gateway.config.manager.manager_agent_id,
        channel_name="telegram",
        user_id=owner,
        chat_id="telegram-chat",
        owner_canonical_user_id=owner,
    )
    store.record_request(
        correlation_id="correlation_telegram",
        session_key="session_telegram",
        request_kind="interactive",
        request_text="compare the two registrar accounts",
        conversation_id="conversation_telegram",
        followup_to_task_id=None,
        status="running",
        owner_canonical_user_id=owner,
    )
    store.update_request_coordination("correlation_telegram", status="running", active_task_id="task_telegram")
    page = _page(envelope, "tool_setup_req_telegram", tool_id="http:registrar_two")
    page.msg_content.parent_task_id = "task_telegram"
    page.msg_meta = MessageMeta(
        msg_correlation_id="correlation_telegram",
        conversation_id="conversation_telegram",
        gateway_context=GatewayContext(
            session_key="session_telegram", canonical_user_id=owner, channel_name="telegram", chat_id="telegram-chat"
        ),
    )
    return page


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_kind", ["web_chat", "other_chat"])
async def test_a_setup_page_left_on_a_timed_out_wait_ends_in_a_result_the_task_receives(chat, chat_kind):
    """Opened where the chat shows late pages, otherwise refused with a result to the task; never lost."""

    gateway, envelope, _create, transport = chat
    client = gateway._manager_client
    coordinator = gateway._response_coordinator
    coordinator._settings = dataclasses.replace(coordinator._settings, request_timeout_seconds=0.05)
    if chat_kind == "web_chat":
        correlation_id, task_id, session_key = CHAT_REQUEST, TASK, SESSION
        page = _page(envelope, "tool_setup_req_late", tool_id="http:registrar_two")
    else:
        correlation_id, task_id, session_key = "correlation_telegram", "task_telegram", "session_telegram"
        page = _chat_that_cannot_show_late_pages(gateway, envelope)

    async def submission():
        # Like interactive_submission._submit_new_task: the wait ends with a timeout.
        async with client.response_channel(correlation_id, task_id=task_id):
            try:
                return await coordinator.await_terminal_response(
                    correlation_id=correlation_id, session_key=session_key, query_handler=None, response_task_id=task_id
                )
            finally:
                # The page arrives as the wait gives up, before the wait has closed.
                await client.handle_response(page)

    with pytest.raises(TimeoutError):
        await submission()
    assert gateway.state_store.get_request(correlation_id).status == "timed_out"

    def results() -> list[ToolSetupResultV1]:
        return [e.msg_content for e, _ in transport.durable if isinstance(e.msg_content, ToolSetupResultV1)]

    page_id = page.msg_content.setup_request_id
    if chat_kind == "web_chat":
        # A timed-out request still runs, so its late page opens; the task gets the page's own result later.
        await _until(lambda: _listed(gateway) == [page_id], what="the late page's card is listed")
        assert gateway.state_store.tool_setup_repository.get_tool_setup(page_id).status == "waiting"
        assert results() == []
    else:
        await _until(lambda: bool(results()), what="the task is sent the page's result")
        [result] = results()
        assert (result.setup_request_id, result.status, result.reason_code) == (
            page_id,
            "unavailable",
            "tool_setup_chat_not_waiting",
        )
        assert _listed(gateway) == []


# How each leftover is handled: by the way it came in (manager client) -------------------

CORRELATION = "corr_left"
WAITING_TASK = "task_left"


def _envelope(content, *, from_agent_id: str = "manager_1") -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=content,
        msg_header=MessageHeader(from_agent_id=from_agent_id),
        msg_meta=MessageMeta(msg_correlation_id=CORRELATION),
    )


def _reply(answer: str) -> MessageEnvelope:
    return _envelope(Task(task_id=WAITING_TASK, task_type=MessageMetaType.RESPONSE, answer=answer))


def _notice(text: str) -> MessageEnvelope:
    return _envelope(UserNotification(text=text, source_task_id=WAITING_TASK))


async def _passed_on(client) -> None:
    """Wait until every leftover the client is passing on has been handled."""

    await asyncio.gather(*tuple(client._leftovers._running))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("way_in", "content", "delivered"),
    [
        ("response", Task(task_id=WAITING_TASK, task_type=MessageMetaType.RESPONSE, answer="Done"), True),
        ("response", UserNotification(text="Still working.", source_task_id=WAITING_TASK), True),
        # Agents' operational messages to each other carry the request's correlation too.
        (
            "operational",
            TaskSteering(steering_type=TaskSteeringType.STATUS, target_task_id=WAITING_TASK, status_reason="Applied."),
            False,
        ),
        ("gateway", SystemStatusMessage(system_status="The team lead is unavailable."), False),
    ],
    ids=["final_reply", "notification", "operational_steering", "gateway_notice"],
)
async def test_leftovers_get_the_handling_of_the_way_they_came_in(caplog, way_in, content, delivered):
    # Imported here so the file still runs on code without the fix (module docstring).
    from assistant_gateway.application.reply_leftovers import ArrivalRoute

    caplog.set_level(logging.DEBUG, logger="test.gateway.manager_client")
    unsolicited: list[MessageEnvelope] = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)
    leftover = _envelope(content, from_agent_id="worker_1" if way_in == "operational" else "manager_1")
    arrive: dict[str, Callable[[], Awaitable[object]]] = {
        "response": lambda: client.handle_response(leftover),
        "operational": lambda: client.handle_operational(leftover),
        "gateway": lambda: client.queue_response(CORRELATION, leftover, route=ArrivalRoute.GATEWAY),
    }

    async with client.response_channel(CORRELATION, task_id=WAITING_TASK):
        await arrive[way_in]()
    await _passed_on(client)

    assert unsolicited == ([leftover] if delivered else [])
    if way_in == "operational":
        assert "dropped unmatched operational response type=TaskSteering" in caplog.text
    if way_in == "gateway":
        assert "event=gateway_reply_leftover_not_passed_on reason=gateway_notice" in caplog.text


@pytest.mark.asyncio
async def test_a_leftover_agent_status_is_applied_without_noting_presence_again() -> None:
    """The team lead answers discovery twice (it is asked again every second); the first answer ends the wait."""

    runtime = _FakeManagerRuntime()
    runtime.connected = True
    presence: list = []
    available: list = []
    client = _client(runtime, settings=_settings(manager_id=None), presence=presence, available=available)
    seen = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

    def answer(at: datetime) -> MessageEnvelope:
        request = runtime.operational[-1][0]
        return MessageEnvelope(
            msg_content=AgentStatus(
                self_id="manager_1",
                team_id="team_test",
                named_agent_type="TeamArchitect",
                status_type=MessageMetaType.RESPONSE,
                info_type=ControlType.STATUS_REQUEST_BASIC,
                is_paused=False,
                is_executing=False,
                has_execution_capacity=True,
                is_busy=False,
                name="manager",
                agent_role="manager",
                last_seen_at=at.isoformat(),
            ),
            msg_header=MessageHeader(from_agent_id="manager_1"),
            msg_meta=MessageMeta(msg_correlation_id=request.msg_meta.msg_correlation_id),
        )

    async def both_answers(_request: MessageEnvelope) -> None:
        await client.handle_operational(answer(seen))
        await client.handle_operational(answer(seen + timedelta(seconds=1)))

    runtime.operational_publish_hook = both_answers

    assert await client.discover_manager_id(timeout_seconds=1.0) == "manager_1"
    assert client.manager_id is None  # Discovery itself only reports the id.
    await _passed_on(client)

    # Each status was noted once, when it arrived.
    assert [(status.last_seen_at, source) for status, _at, source in presence] == [
        (seen.isoformat(), "operational"),
        ((seen + timedelta(seconds=1)).isoformat(), "operational"),
    ]
    # The leftover answer was applied as a late status, as it would have been a moment later.
    assert client.manager_id == "manager_1"
    assert [(transition.manager_id, transition.source) for transition in available] == [
        ("manager_1", "operational_unmatched")
    ]


# A3: every way a wait ends ---------------------------------------------------------------


async def _answered(queue: asyncio.Queue[MessageEnvelope]) -> None:
    await queue.get()


async def _timed_out(_queue: asyncio.Queue[MessageEnvelope]) -> None:
    await asyncio.wait_for(asyncio.Event().wait(), timeout=0.01)


async def _failed(_queue: asyncio.Queue[MessageEnvelope]) -> None:
    raise RuntimeError("The caller failed.")


async def _cancelled(_queue: asyncio.Queue[MessageEnvelope]) -> None:
    asyncio.current_task().cancel()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "raised"),
    [(_answered, None), (_timed_out, TimeoutError), (_cancelled, asyncio.CancelledError), (_failed, RuntimeError)],
    ids=["answered", "timed_out", "cancelled", "failed"],
)
async def test_leftovers_are_passed_on_however_the_wait_ends(ending, raised) -> None:
    unsolicited: list[MessageEnvelope] = []
    client = _client(_FakeManagerRuntime(), unsolicited=unsolicited)
    answer, first, second = _reply("Here it is."), _notice("One more thing."), _notice("And another.")

    async def wait() -> None:
        async with client.response_channel(CORRELATION, task_id=WAITING_TASK) as queue:
            for envelope in (answer, first, second):
                await client.handle_response(envelope)
            await ending(queue)

    waiter = asyncio.create_task(wait())
    if raised is None:
        await waiter
    else:
        with pytest.raises(raised):
            await waiter
    await _passed_on(client)

    # Oldest first; an answered wait read only its answer.
    assert unsolicited == ([first, second] if raised is None else [answer, first, second])
    assert client.pending_response_ids == ()


@pytest.mark.asyncio
async def test_one_failing_leftover_does_not_stop_the_others(caplog) -> None:
    caplog.set_level(logging.INFO, logger="test.gateway.manager_client")
    delivered: list[MessageEnvelope] = []
    client = _client(_FakeManagerRuntime())
    broken, fine = _notice("This one fails."), _notice("This one arrives.")

    async def on_unsolicited(envelope: MessageEnvelope) -> None:
        if envelope is broken:
            raise RuntimeError("The chat could not be reached.")
        delivered.append(envelope)

    client._on_unsolicited = on_unsolicited
    async with client.response_channel(CORRELATION, task_id=WAITING_TASK):
        await client.handle_response(broken)
        await client.handle_response(fine)
    await _passed_on(client)

    assert delivered == [fine]
    assert (
        f"event=gateway_reply_leftover_failed correlation_id={CORRELATION} task_id={WAITING_TASK} "
        f"message_id={broken.msg_header.message_id} content_type=UserNotification"
    ) in caplog.text
