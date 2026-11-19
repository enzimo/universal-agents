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
last effect, so on code without the fix they fail on the missing page or card.

The messaging project will move these tests to the gateway's new receiver when
it replaces this reply path on release/0.10.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest

from custom_messages.contracts.credentials_v1 import ToolSetupResultV1
from tests.test_gateway_credential_handoff import (  # noqa: F401 - pytest fixture
    CHAT_REQUEST,
    _listed,
    _page,
    handoff,
)

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
