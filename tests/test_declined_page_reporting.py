"""How a page the person declined reaches the waiting task and the chat (A4, C08).

A declined credential form must stay ``credential_declined`` from the broker's
session status, through the gateway's status mapping, to the agent's answer and
the chat's fallback text, for every flow (collection, deletion, manual
sign-up). A missed status list would turn it into ``credential_unavailable``,
whose answer asks the person to try again. A declined sign-in ends the task's
wait as ``declined`` (so the same page is not offered again in that task), and
``/link`` says the page was closed.
"""

from __future__ import annotations

from typing import get_args

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application import credential_workflow
from assistant_gateway.application.broker_session_watch import WATCH_FINISHED, BrokerSessionOutcome
from assistant_gateway.application.delivery_presentation import _render_credential_result_fallback
from assistant_gateway.application.link_results import sign_in_result_text
from assistant_gateway.application.tool_setup import sign_in_page_ending
from custom_agents import task_message_runtime
from custom_messages.contracts.credentials_v1 import (
    CREDENTIAL_DECLINED,
    TOOL_SETUP_DECLINED,
    SecureCredentialCollectionResult,
    SecureCredentialResultStatus,
)
from secure_tool_broker.defaults import DECLINE_FIELD
from secure_tools.broker_pages import SIGN_IN_FINAL_STATUSES, SIGN_IN_STATUS_DECLINED
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, gateway_headers, open_gate

RETRY_WORDS = ("try again", "please start", "start the request again")


def test_the_gateway_takes_every_contract_status_and_ends_the_watch_on_declined() -> None:
    assert credential_workflow._RESULT_STATUSES == frozenset(get_args(SecureCredentialResultStatus))
    assert CREDENTIAL_DECLINED in credential_workflow._TERMINAL_COLLECTION_STATUSES
    assert "manual_signup_required" not in credential_workflow._TERMINAL_COLLECTION_STATUSES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "service_name"),
    [("", "Portainer"), ("credential_deletion", "Portainer"), ("manual_signup", "Example Mail")],
)
async def test_a_declined_form_stays_declined_from_the_broker_to_the_agents_answer(flow, service_name) -> None:
    request: dict = {"credential_key": "portainer_token", "service_name": service_name}
    if flow:
        request["flow"] = flow
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        created = await (
            await client.post(
                "/v1/credential-collection/sessions",
                headers=gateway_headers(),
                json={
                    "request": request,
                    "context": {"canonical_user_id": "user-1", "principal_type": "user", "team_id": TEST_TEAM},
                },
            )
        ).json()
        form = "/" + created["form_url"].split("/", 3)[3]
        await client.post(form, data={DECLINE_FIELD: "yes"}, headers={"Origin": "null"})
        payload = await (await client.get("/" + created["status_url"].split("/", 3)[3], headers=gateway_headers())).json()

    # The gateway: the watch ends and the task's status is declined.
    status = credential_workflow._collection_result_status(payload)
    assert status == CREDENTIAL_DECLINED
    assert payload["status"] in credential_workflow._TERMINAL_COLLECTION_STATUSES

    # What publish_result sends the task (its metadata always names the flow).
    result = SecureCredentialCollectionResult(
        credential_request_id="cred-1",
        status=status,
        credential_key="portainer_token",
        service_name=service_name,
        metadata={"flow": flow or "credential_collection"},
        message=str(payload["result"]["content"].get("message") or ""),
    )
    answer = task_message_runtime._credential_declined_answer(result)
    fallback = _render_credential_result_fallback(result)
    for text in (answer, fallback):
        assert "declined" in text
        assert not any(words in text.lower() for words in RETRY_WORDS), text
    if flow == "credential_deletion":
        assert "not deleted" in answer
    orphaned, _errors = task_message_runtime._orphaned_credential_collection_response(result)
    assert orphaned == answer


def test_a_declined_sign_in_ends_the_tasks_wait_as_declined() -> None:
    assert SIGN_IN_STATUS_DECLINED in SIGN_IN_FINAL_STATUSES
    ending = sign_in_page_ending(
        BrokerSessionOutcome(WATCH_FINISHED, {"status": SIGN_IN_STATUS_DECLINED}), what="the GitHub sign-in"
    )
    assert ending.status == TOOL_SETUP_DECLINED
    assert ending.message == "The GitHub sign-in was closed without finishing."


def test_a_declined_link_sign_in_says_the_page_was_closed() -> None:
    text = sign_in_result_text(
        BrokerSessionOutcome(WATCH_FINISHED, {"status": SIGN_IN_STATUS_DECLINED}),
        display_name="GitHub",
        restart_command="/link cli:github",
    )
    assert text == "You closed the GitHub sign-in page without finishing. Run /link cli:github to try again."


# A declined sign-in whose CLI may have signed in just before Decline stopped it (review finding F9).
_MAY_REMAIN = {"status": SIGN_IN_STATUS_DECLINED, "sign_in_may_remain": True}


def test_a_declined_sign_in_that_may_remain_says_so_to_the_task() -> None:
    ending = sign_in_page_ending(BrokerSessionOutcome(WATCH_FINISHED, _MAY_REMAIN), what="the GitHub sign-in")

    assert ending.status == TOOL_SETUP_DECLINED
    assert ending.message == (
        "The GitHub sign-in was declined. The sign-in may have finished just before it was declined, "
        "so it may remain; check with /auth status."
    )


def test_a_declined_link_sign_in_that_may_remain_says_so_to_the_person() -> None:
    text = sign_in_result_text(
        BrokerSessionOutcome(WATCH_FINISHED, _MAY_REMAIN), display_name="GitHub", restart_command="/link cli:github"
    )

    assert text == (
        "You declined the GitHub sign-in page. The sign-in may have finished just before it was declined, "
        "so it may remain; check with /auth status."
    )
    assert "without finishing" not in text
