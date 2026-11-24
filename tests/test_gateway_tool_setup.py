"""Tool setup pages a task asks for (``ToolSetupRequestV1``), through the real gateway.

The fake broker mirrors the secure broker's account link and sign-in routes
(``tests/test_gateway_account_links.py``); the gateway talks to it through the
real ``HttpGatewaySecureBroker`` and stores its waits in real SQLite. Covered:

- each kind opens the right page for the person the task works for, in the
  person's chat, and the task gets one ``ToolSetupResultV1``: a completed
  sign-in (with the account name), a completed account link, a declined form,
  an expired page;
- re-offers: an expired page may be offered again, and the fourth page of a
  kind for a tool in one task is refused; after the person closed a page,
  nothing more is offered for that tool in the task;
- the team's sign-in app is an administrator's step: refused with a plain
  explanation for a member, opened for an administrator;
- the person finishing the same setup with ``/link`` or ``/auth ...
  team_client`` ends the task's wait (exactly one result);
- after a gateway restart, a wait nobody watches is reported;
- an approved proposal's account link forms are watched until they end.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import get_args

import pytest
import pytest_asyncio

from assistant_gateway import gateway as gateway_module
from assistant_gateway.application.broker_session_watch import (
    WATCH_EXPIRED,
    WATCH_FINISHED,
    WATCH_UNCONFIRMED,
    BrokerSessionOutcome,
)
from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.application.transient_presentation import TOOL_SETUP_READY_TEXT
from assistant_gateway.application.tool_setup import _what, account_link_page_ending, sign_in_page_ending
from assistant_gateway.domain.message_types import (
    TOOL_SETUP_MESSAGE_TYPE,
    TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE,
)
from assistant_gateway.domain.request_state import ToolSetupRecord
from assistant_gateway.gateway import UserGateway
from custom_messages.contracts.credentials_v1 import ToolSetupKind, ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from tests.test_gateway_account_links import (  # noqa: F401 - pytest fixtures
    BROKER_TOKEN,
    PUBLIC_BROKER_BASE,
    _member,
    _waiting_task,
    fake_broker,
    linked_gateway,
)

TASK = "task_link"


@pytest.fixture
def setup_gateway(linked_gateway):  # noqa: F811 - the imported pytest fixture
    """A gateway, the fake broker and the manager transport (``linked_gateway``)."""

    return linked_gateway



CHAT_REQUEST = "correlation_link"
SESSION = "session_link"


def _setup_envelope(
    gateway: UserGateway,
    *,
    tool_id: str = "http:portainer",
    kind: str = "account_link",
    owner: str = "person",
    setup_request_id: str = "tool_setup_req_test",
    reason: str = "To list your containers, I need to know which account Portainer should use.",
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=ToolSetupRequestV1(
            setup_request_id=setup_request_id,
            parent_task_id=TASK,
            tool_id=tool_id,
            kind=kind,
            owner=owner,
            reason=reason,
        ),
        msg_meta=MessageMeta(
            msg_correlation_id=CHAT_REQUEST,
            conversation_id="conversation_link",
            gateway_context=GatewayContext(session_key=SESSION),
        ),
        msg_header=MessageHeader(
            from_agent_id=gateway.config.manager.manager_agent_id,
            to_routing_path=gateway.config.manager.response_queue_name,
            agent_signature="",
        ),
    )


async def _request_setup(gateway: UserGateway, **fields):
    """Deliver the task's request to the chat waiting for the task's reply."""

    envelope = _setup_envelope(gateway, **fields)
    async with gateway._manager_client.response_channel(CHAT_REQUEST):
        await gateway._manager_client.handle_response(envelope)
        return await gateway._response_coordinator.await_terminal_response(
            correlation_id=CHAT_REQUEST, session_key=SESSION, query_handler=None
        )


async def _results(transport, count: int = 1, *, timeout: float = 5.0) -> list[ToolSetupResultV1]:
    async def wait() -> list[ToolSetupResultV1]:
        while len([e for e, _ in transport.durable if isinstance(e.msg_content, ToolSetupResultV1)]) < count:
            await asyncio.sleep(0.01)
        return [e.msg_content for e, _ in transport.durable if isinstance(e.msg_content, ToolSetupResultV1)]

    return await asyncio.wait_for(wait(), timeout=timeout)


def _setups(gateway: UserGateway):
    return gateway.state_store.tool_setup_repository


def _known(gateway: UserGateway, person_id: str, tool_id: str):
    return gateway.access_inbox.setup_states().get(gateway.config.manager.team_id, person_id, tool_id)


async def _settle(gateway: UserGateway) -> None:
    """Wait until every page watch the setups started has ended."""

    for _ in range(500):
        if gateway._credential_workflow.tool_setups.pending_count == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a tool setup page watch did not end")


# Opening pages and reporting how they ended --------------------------------------------


@pytest.mark.asyncio
async def test_account_link_page_opens_for_the_task_person_and_reports_the_link(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    _waiting_task(gateway, owner=owner)

    response = await _request_setup(gateway)

    [request] = broker.requests
    assert (request["tool_id"], request["owner_kind"], request["person_id"]) == ("http:portainer", "person", owner)
    form_url = f"{PUBLIC_BROKER_BASE}/account-link/link_session_1"
    assert response.message_type == TOOL_SETUP_MESSAGE_TYPE
    assert response.text.startswith(
        "To list your containers, I need to know which account Portainer should use. "
        "Open *Choose your account for Portainer*:"
    )
    assert form_url in response.text
    assert form_url not in (response.transcript_text or "")
    action = response.transient_action
    assert action is not None and action.kind == "tool_setup"
    assert action.label == "Choose your account for Portainer" and action.url == form_url
    assert "Linking an account gives nobody access by itself." in action.description
    assert gateway.state_store.get_request(CHAT_REQUEST).status == "waiting_for_credential"
    [waiting] = _setups(gateway).list_tool_setups(status="waiting")
    assert (waiting.parent_task_id, waiting.person_id, waiting.kind) == (TASK, owner, "account_link")
    assert transport.durable == []  # The task keeps waiting for the person.

    broker.complete("link_session_1", link_id="account_link_portainer")
    [result] = await _results(transport)

    assert (result.status, result.source, result.attempt) == ("completed", "task_request", 1)
    assert (result.tool_id, result.kind, result.owner) == ("http:portainer", "account_link", "person")
    assert result.link_id == "account_link_portainer"
    assert result.message == "The account link for Portainer is done."
    envelope, routing_key = transport.durable[-1]
    assert routing_key == gateway.config.manager.manager_agent_id
    assert envelope.msg_meta.msg_correlation_id == CHAT_REQUEST
    assert envelope.msg_meta.gateway_context.canonical_user_id == owner
    record = gateway.state_store.get_request(CHAT_REQUEST)
    assert (record.request_kind, record.followup_to_task_id) == ("tool_setup_result", TASK)
    assert _setups(gateway).get_tool_setup("tool_setup_req_test").status == "completed"
    # The person's last known state for the tool (access_control.tool_setup_states).
    state = _known(gateway, owner, "http:portainer")
    assert (state.state.value, state.how, state.source) == ("ready", "own_link", "setup_page")


@pytest.mark.asyncio
async def test_sign_in_page_opens_and_reports_the_account_signed_in_with(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    _waiting_task(gateway, owner=owner)

    response = await _request_setup(
        gateway, tool_id="cli:x", kind="sign_in", reason="To post for you on X, I need you to sign in once."
    )

    assert broker.requests == []
    [(provider, body)] = broker.sign_ins
    assert (provider, body["method"]) == ("x", "web")
    assert body["context"] == {
        "canonical_user_id": owner,
        "team_id": gateway.config.manager.team_id,
        "principal_type": "user",
    }
    assert response.transient_action.label == "Sign in to X"
    assert response.transient_action.url == f"{PUBLIC_BROKER_BASE}/auth/x/v1.signed_session"
    assert "never to the agent" in response.transient_action.description

    broker.sign_in_status["x"] = {"status": "success", "username": "exampleuser"}
    [result] = await _results(transport)

    assert (result.status, result.kind, result.account_label) == ("completed", "sign_in", "exampleuser")
    assert result.message == "The X sign-in is done (account exampleuser)."


@pytest.mark.asyncio
async def test_a_declined_form_stops_further_pages_for_that_tool(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    await _request_setup(gateway)

    broker.decline("link_session_1")
    [declined] = await _results(transport)
    assert declined.status == "declined"
    assert declined.message == "The form for the account link for Portainer was closed without linking anything."

    refused = await _request_setup(gateway, setup_request_id="tool_setup_req_again")

    assert len(broker.requests) == 1, "no second form opens"
    assert refused.message_type == TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE
    assert "/link http:portainer" in refused.text
    _, again = await _results(transport, 2)
    assert (again.status, again.reason_code) == ("unavailable", "tool_setup_declined_before")


@pytest.mark.asyncio
async def test_expired_pages_may_be_offered_three_times_and_the_fourth_is_refused(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    broker.sign_in_expires_in = -timedelta(minutes=2)  # every page is already past its time

    for attempt in (1, 2, 3):
        response = await _request_setup(
            gateway, tool_id="cli:github", kind="sign_in", setup_request_id=f"tool_setup_req_{attempt}"
        )
        assert response.transient_action is not None
        results = await _results(transport, attempt)
        assert (results[-1].status, results[-1].attempt) == ("expired", attempt)
        assert results[-1].message == "The page for the GitHub sign-in expired before it was finished."

    refused = await _request_setup(gateway, tool_id="cli:github", kind="sign_in", setup_request_id="tool_setup_req_4")

    assert len(broker.sign_ins) == 3, "no fourth page opens"
    assert refused.transient_action is None
    assert "already offered 3 times" in refused.text
    fourth = (await _results(transport, 4))[-1]
    assert (fourth.status, fourth.reason_code, fourth.attempt) == ("unavailable", "tool_setup_offer_limit", 3)


@pytest.mark.asyncio
async def test_the_team_sign_in_app_is_explained_to_a_member(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    member = _member(gateway)
    _waiting_task(gateway, owner=member)

    refused = await _request_setup(
        gateway, tool_id="cli:google", kind="team_sign_in_app", owner="team", setup_request_id="tool_setup_req_member"
    )

    assert broker.sign_ins == [], "nothing opens for a member"
    assert refused.text == (
        "Setting up the team's Google sign-in app is an administrator's step. "
        "A team administrator or owner can do it with /auth google team_client."
    )
    [result] = await _results(transport)
    assert (result.status, result.needs_administrator, result.reason_code) == (
        "unavailable",
        True,
        "tool_setup_admin_required",
    )


@pytest.mark.asyncio
async def test_the_team_sign_in_app_page_opens_for_an_administrator(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    # The owner is an administrator: the page opens with their roles for the broker to check.
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)

    opened = await _request_setup(
        gateway, tool_id="cli:google", kind="team_sign_in_app", owner="team", setup_request_id="tool_setup_req_admin"
    )
    [(provider, body)] = broker.sign_ins
    assert (provider, body["method"]) == ("google", "team_client")
    assert "admin" in body["context"]["person_roles"]
    assert opened.transient_action.label == "Set up the team's Google sign-in app"
    broker.sign_in_status["google"] = {"status": "success"}
    [done] = await _results(transport)
    assert (done.status, done.kind, done.owner) == ("completed", "team_sign_in_app", "team")
    assert done.message == "The team's Google sign-in app is done."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "kind", "owner", "expected"),
    [
        ("http:portainer", "sign_in", "person", "does not use a personal sign-in"),
        ("cli:github", "account_link", "person", "uses each person's own sign-in"),
        ("http:portainer", "team_sign_in_app", "team", "has no team sign-in app"),
    ],
)
async def test_a_setup_that_does_not_fit_the_tool_is_refused(setup_gateway, tool_id, kind, owner, expected) -> None:
    gateway, broker, transport = setup_gateway
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)

    refused = await _request_setup(gateway, tool_id=tool_id, kind=kind, owner=owner)

    assert expected in refused.text
    assert broker.requests == [] and broker.sign_ins == []
    [result] = await _results(transport)
    assert (result.status, result.reason_code) == ("unavailable", "tool_setup_kind_mismatch")


@pytest.mark.asyncio
async def test_a_team_link_refused_for_a_member_names_who_can_link_it(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    member = _member(gateway)
    _waiting_task(gateway, owner=member)

    refused = await _request_setup(gateway, owner="team")

    assert broker.requests[0]["person_roles"] == ["member"]
    assert refused.text == (
        "Only a team administrator or owner can link the team's account. "
        "A team administrator or owner can link the team's account with /link http:portainer team."
    )
    [result] = await _results(transport)
    assert (result.status, result.needs_administrator) == ("unavailable", True)


@pytest.mark.asyncio
async def test_a_created_link_for_another_tool_is_never_reported_as_linked(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    await _request_setup(gateway)

    broker.sessions["link_session_1"]["request"]["tool_id"] = "http:grafana"
    broker.complete("link_session_1")
    [result] = await _results(transport)

    assert (result.status, result.reason_code, result.link_id) == ("unavailable", "tool_setup_link_mismatch", None)


# The person finishing the setup on their own ------------------------------------------


@pytest.mark.asyncio
async def test_the_persons_own_link_ends_the_tasks_wait_once(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    _waiting_task(gateway, owner=owner)
    gateway._credential_workflow.link_results._poll_interval_seconds = 0.01
    gateway._credential_workflow.tool_setups.guide = _Guide()
    await _request_setup(gateway)

    # The person runs /link themselves in the same chat and finishes that form.
    reply = await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text="/link http:portainer",
            current_session_key=SESSION,
            channel_name="cli",
            user_id=owner,
            chat_id=f"cli:{gateway.config.instance_name}",
            canonical_user_id=owner,
            correlation_id="corr_person_link",
        )
    )
    assert reply.transient_action is not None
    broker.complete("link_session_2", link_id="account_link_from_command")
    [result] = await _results(transport)

    assert (result.status, result.source, result.setup_request_id) == ("completed", "person_action", "tool_setup_req_test")
    state = _known(gateway, owner, "http:portainer")
    assert (state.state.value, state.how, state.source) == ("ready", "own_link", "person_action")
    assert result.message == "The person finished the account link for Portainer on their own."
    # The task's own form expires later; the task already has its one result.
    broker.refuse("link_session_1", error="expired", reason_code="account_link_session_expired")
    await _settle(gateway)
    assert len(await _results(transport)) == 1


@pytest.mark.asyncio
async def test_storing_the_team_sign_in_app_with_auth_ends_an_administrators_wait(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    _waiting_task(gateway, owner=owner)
    gateway._credential_workflow.link_results._poll_interval_seconds = 0.01
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id="tool_setup_req_app",
            parent_task_id=TASK,
            person_id=owner,
            tool_id="cli:google",
            kind="team_sign_in_app",
            owner_kind="team",
            session_key=SESSION,
            correlation_id=CHAT_REQUEST,
            conversation_id="conversation_link",
            status="waiting",
            created_at="2026-09-28T00:00:00+00:00",
        )
    )

    # Someone else's Google waited for the team's app; they are checked again, not asked again.
    states = gateway.access_inbox.setup_states()
    team = gateway.config.manager.team_id
    states.note_needs_setup(team, "user-other", "cli:google", step="team_sign_in_app", source="tool_call")
    reply = await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text="/auth google team_client",
            current_session_key=SESSION,
            channel_name="cli",
            user_id=owner,
            chat_id=f"cli:{gateway.config.instance_name}",
            canonical_user_id=owner,
            correlation_id="corr_auth_app",
        )
    )
    assert reply.transient_action is not None
    broker.sign_in_status["google"] = {"status": "success"}
    [result] = await _results(transport)

    assert (result.status, result.source, result.kind) == ("completed", "person_action", "team_sign_in_app")
    assert states.get(team, "user-other", "cli:google").state.value == "unknown"


# Recovery after a gateway restart -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wait_nobody_watches_after_a_restart_is_reported_once(setup_gateway) -> None:
    gateway, _broker, transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    _waiting_task(gateway, owner=owner)
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id="tool_setup_req_before_restart",
            parent_task_id=TASK,
            person_id=owner,
            tool_id="cli:github",
            kind="sign_in",
            owner_kind="person",
            session_key=SESSION,
            correlation_id=CHAT_REQUEST,
            conversation_id="conversation_link",
            status="waiting",
            created_at="2026-09-28T00:00:00+00:00",
        )
    )

    # The gateway starts the recovery with its other start-up access work.
    start_config = SimpleNamespace(
        instance_root=gateway.config.instance_root,
        manager=gateway.config.manager,
        authorization=SimpleNamespace(enabled=False),
    )
    tasks = gateway_module._access_maintenance_tasks(
        start_config,
        access_inbox=gateway.access_inbox,
        broker=gateway._secure_broker,
        tool_setups=gateway._credential_workflow.tool_setups,
        credential_workflow=gateway._credential_workflow,
        status_page=gateway.application.status,
        stop_event=asyncio.Event(),
    )
    recovery = next(task for task in tasks if task.get_name() == "gateway_tool_setup_recovery")
    # Blocks are not enforced with authorization off, so stale ones are not reported.
    assert "gateway_stale_blocks" not in {task.get_name() for task in tasks}
    assert await recovery == 1
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    [result] = await _results(transport)
    assert (result.status, result.reason_code, result.attempt) == ("unavailable", "tool_setup_page_not_watched", 1)
    assert await gateway._credential_workflow.tool_setups.recover_unwatched() == 0


# An approved proposal's account link forms ----------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_proposals_forms_are_watched_until_they_end(setup_gateway) -> None:
    gateway, broker, _transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    setups = gateway._credential_workflow.tool_setups
    released = asyncio.Event()

    async def release() -> None:
        released.set()

    form = await setups.open_proposal_link(
        request_id="access_req_1:http:portainer:account",
        person_id=owner,
        person_roles=gateway._request_context_service.team_roles(owner),
        tool_id="http:portainer",
        owner="team",
        reason="Restart the stack",
        purpose="account",
    )
    setups.after_proposal_links(
        request_id="access_req_1",
        forms=[(form, "http:portainer", "team", "account")],
        then=release,
    )
    await asyncio.sleep(0.05)
    assert not released.is_set(), "the task waits while the approver's form is open"

    broker.complete(form.session_id)
    await asyncio.wait_for(released.wait(), timeout=5)


@pytest.mark.asyncio
async def test_an_approved_proposals_gate_form_passes_gate_purpose_and_verifies_result(setup_gateway) -> None:
    gateway, broker, _transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    setups = gateway._credential_workflow.tool_setups
    released = asyncio.Event()

    async def release() -> None:
        released.set()

    form = await setups.open_proposal_link(
        request_id="access_req_gate:http:portainer:gate",
        person_id=owner,
        person_roles=gateway._request_context_service.team_roles(owner),
        tool_id="http:portainer",
        owner="team",
        reason="Pass the Cloudflare gate",
        purpose="gate",
    )
    assert broker.requests[-1]["purpose"] == "gate"
    setups.after_proposal_links(
        request_id="access_req_gate",
        forms=[(form, "http:portainer", "team", "gate")],
        then=release,
    )
    broker.complete(form.session_id)
    await asyncio.wait_for(released.wait(), timeout=5)

    # A gate form whose broker result claims purpose="account" is rejected as a mismatch.
    wrong = account_link_page_ending(
        BrokerSessionOutcome(
            ending=WATCH_FINISHED,
            payload={
                "status": "account_link_created",
                "result": {
                    "status": "account_link_created",
                    "link_id": "link-1",
                    "tool_id": "http:portainer",
                    "owner_kind": "team",
                    "purpose": "account",
                },
            },
        ),
        what="the team gate credential for Portainer",
        tool_id="http:portainer",
        owner="team",
        purpose="gate",
    )
    assert (wrong.status, wrong.reason_code) == ("unavailable", "tool_setup_link_mismatch")


@pytest.mark.asyncio
async def test_after_proposal_links_reports_each_form_ending_as_it_finishes(setup_gateway) -> None:
    gateway, broker, _transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    roles = gateway._request_context_service.team_roles(owner)
    setups = gateway._credential_workflow.tool_setups
    released = asyncio.Event()
    first_ended = asyncio.Event()
    recorded: list[tuple[str, str, str]] = []

    async def release() -> None:
        released.set()

    async def on_form_ended(form, tool_id: str, owner_kind: str, ending) -> None:
        recorded.append((form.session_id, tool_id, ending.status))
        if form.session_id == form_one.session_id:
            first_ended.set()

    form_one = await setups.open_proposal_link(
        request_id="access_req_2:http:portainer",
        person_id=owner,
        person_roles=roles,
        tool_id="http:portainer",
        owner="team",
        reason="Restart the stack",
    )
    form_two = await setups.open_proposal_link(
        request_id="access_req_2:cli:x",
        person_id=owner,
        person_roles=roles,
        tool_id="cli:x",
        owner="team",
        reason="Post updates",
    )
    setups.after_proposal_links(
        request_id="access_req_2",
        forms=[(form_one, "http:portainer", "team", "account"), (form_two, "cli:x", "team", "account")],
        then=release,
        on_form_ended=on_form_ended,
    )

    broker.complete(form_one.session_id)
    await asyncio.wait_for(first_ended.wait(), timeout=5)
    assert recorded == [(form_one.session_id, "http:portainer", "completed")]
    assert not released.is_set(), "the agent still waits for the second form"

    broker.complete(form_two.session_id)
    await asyncio.wait_for(released.wait(), timeout=5)
    assert len(recorded) == 2


@pytest.mark.asyncio
async def test_after_proposal_links_watch_failure_clears_link_and_waits_for_siblings(
    setup_gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watch that raises still reports an unconfirmed ending and does not wake the agent early."""

    from assistant_gateway.application.tool_setup import REASON_PAGE_UNCONFIRMED, _OpenedPage

    gateway, broker, _transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    roles = gateway._request_context_service.team_roles(owner)
    setups = gateway._credential_workflow.tool_setups
    released = asyncio.Event()
    failed_recorded = asyncio.Event()
    recorded: list[tuple[str, str, str | None, str]] = []

    async def release() -> None:
        released.set()

    def on_form_ended(form, tool_id: str, owner_kind: str, ending) -> None:
        recorded.append((tool_id, ending.status, ending.reason_code, ending.message))
        if tool_id == "http:portainer":
            failed_recorded.set()

    form_one = await setups.open_proposal_link(
        request_id="access_req_3:http:portainer",
        person_id=owner,
        person_roles=roles,
        tool_id="http:portainer",
        owner="team",
        reason="Restart the stack",
    )
    form_two = await setups.open_proposal_link(
        request_id="access_req_3:cli:x",
        person_id=owner,
        person_roles=roles,
        tool_id="cli:x",
        owner="team",
        reason="Post updates",
    )

    real_page = setups._account_link_page

    def patched_page(tool_id: str, owner_kind: str, form, *, purpose: str = "account"):
        if tool_id == "http:portainer":
            async def boom():
                raise ValueError("malformed expires_at_utc")

            return _OpenedPage(
                label="Choose the team's account for Portainer",
                receipt="opened",
                url=form.form_url,
                expires_at_utc=form.expires_at_utc,
                watch=boom,
            )
        return real_page(tool_id, owner_kind, form, purpose=purpose)

    monkeypatch.setattr(setups, "_account_link_page", patched_page)
    setups.after_proposal_links(
        request_id="access_req_3",
        forms=[(form_one, "http:portainer", "team", "account"), (form_two, "cli:x", "team", "account")],
        then=release,
        on_form_ended=on_form_ended,
    )

    await asyncio.wait_for(failed_recorded.wait(), timeout=5)
    assert recorded == [
        (
            "http:portainer",
            "unavailable",
            REASON_PAGE_UNCONFIRMED,
            "Nobody could confirm whether the account link for Portainer finished.",
        )
    ]
    await asyncio.sleep(0.05)
    assert not released.is_set(), "then() must wait until the sibling form finishes"

    broker.complete(form_two.session_id)
    await asyncio.wait_for(released.wait(), timeout=5)
    assert [entry[0] for entry in recorded] == ["http:portainer", "cli:x"]


@pytest.mark.asyncio
async def test_inbox_follow_up_links_dismiss_through_real_tool_setups_watch(setup_gateway) -> None:
    """End-to-end: GatewayAccessInbox + real GatewayToolSetups.after_proposal_links + FakeBroker (C07)."""

    from custom_messages.contracts.access_v2 import (
        AccessLevel,
        AccessRequestKind,
        AccessRequestV1,
        ProposedAccountLinkV1,
        SubjectKind,
        SubjectRefV1,
        TargetKind,
        TargetRefV1,
    )

    gateway, broker, _transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    inbox = gateway.access_inbox
    requests = inbox.requests()
    request = AccessRequestV1(
        request_id="access_req_e2e_follow_ups",
        team_id=gateway.config.manager.team_id,
        kind=AccessRequestKind.ACCESS,
        requester_agent_id=None,
        person_id=owner,
        requested_by_person_id=owner,
        subject=SubjectRefV1(kind=SubjectKind.USER, id=owner),
        target=TargetRefV1(kind=TargetKind.TOOL, id="cli:x"),
        level=AccessLevel.WRITE,
        reason="Approve account links end-to-end",
        created_at_utc=requests.service.clock(),
    )
    requests.service.store.create_request(request)
    inbox.proposals.account_links[request.request_id] = [
        ProposedAccountLinkV1(tool_id="http:portainer", owner="team", purpose="Restart the stack"),
        ProposedAccountLinkV1(tool_id="cli:x", owner="team", purpose="Post updates"),
    ]

    decided = await inbox.decide(owner, request.request_id, "approve")
    assert len(decided["follow_up_links"]) == 2
    assert decided["follow_up_outcomes"] == []

    # Broker opened two real sessions; complete the first and check review() drops only that link.
    broker.complete("link_session_1")
    await _until(
        lambda: len(inbox._follow_ups.snapshot(request.request_id, viewer_id=owner, now=requests.service.clock()).links)
        == 1
    )
    after_first = await inbox.review(owner, request.request_id)
    assert after_first is not None
    assert len(after_first["follow_up_links"]) == 1
    assert "X" in after_first["follow_up_links"][0]["label"]
    assert after_first["follow_up_outcomes"] == ["The account link for Portainer is done."]
    assert after_first["follow_up_settling"] is False

    # Decline the second form and check both links clear with both plain outcomes kept.
    broker.decline("link_session_2")
    await _settle(gateway)
    after_second = await inbox.review(owner, request.request_id)
    assert after_second is not None
    assert after_second["follow_up_links"] == []
    assert after_second["follow_up_settling"] is False
    assert after_second["follow_up_outcomes"] == [
        "The account link for Portainer is done.",
        "The form for the account link for X (xurl) was closed without linking anything.",
    ]


# Steps done outside Universal Agents (set_up_tools external steps) ----------------------


class _Guide:
    """The catalog's words for one external step, and the task's workflow page."""

    def external_step(self, tool_id: str, step_id: str):
        from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1

        if (tool_id, step_id) != ("http:gitea", "create_token"):
            return None
        return "Gitea", ExternalSetupStepV1(
            step_id="create_token",
            title="Create a Gitea token",
            instructions="In Gitea, open Settings, then Applications, and generate a token that can read repositories.",
            url="https://gitea.example.com/user/settings/applications",
        )

    def display_name(self, tool_id: str):
        return {"http:gitea": "Gitea", "http:portainer": "Portainer"}.get(tool_id)

    def workflow_url(self, task_id: str):
        return "/workflows/access_flow_gitea" if task_id == TASK else None


STEP = "Create a Gitea token"


async def _request_external_step(gateway: UserGateway, step_id: str = "create_token"):
    envelope = _setup_envelope(gateway, tool_id="http:gitea", kind="account_link", reason="To list your Gitea repos.")
    envelope.msg_content = ToolSetupRequestV1(
        setup_request_id="tool_setup_req_step", parent_task_id=TASK, tool_id="http:gitea", kind="external_step",
        owner="person", step_id=step_id, reason="Create a Gitea token so I can list your Gitea repos.",
    )
    async with gateway._manager_client.response_channel(CHAT_REQUEST):
        await gateway._manager_client.handle_response(envelope)
        return await gateway._response_coordinator.await_terminal_response(
            correlation_id=CHAT_REQUEST, session_key=SESSION, query_handler=None
        )


@pytest.mark.asyncio
async def test_an_external_step_shows_the_catalogs_words_and_ends_when_marked_done(setup_gateway) -> None:
    gateway, broker, transport = setup_gateway
    owner = gateway.config.user.canonical_user_id
    _waiting_task(gateway, owner=owner)
    gateway._credential_workflow.tool_setups.guide = _Guide()

    response = await _request_external_step(gateway)

    assert broker.requests == [] and broker.sign_ins == [], "no broker page for a step outside Universal Agents"
    assert response.message_type == TOOL_SETUP_MESSAGE_TYPE
    assert "generate a token that can read repositories" in response.text
    assert "Mark done on the workflow page: /workflows/access_flow_gitea" in response.text
    assert response.transient_action.url == "https://gitea.example.com/user/settings/applications"
    # Chat history keeps the whole step in the catalog's words, never the task's reason.
    assert response.transcript_text == (
        "Setup step for Gitea shown: Create a Gitea token. In Gitea, open Settings, then Applications, "
        "and generate a token that can read repositories. Open: https://gitea.example.com/user/settings/applications "
        "When it is done, choose Mark done on the workflow page: /workflows/access_flow_gitea"
    )
    record = _setups(gateway).get_tool_setup("tool_setup_req_step")
    assert (record.kind, record.status) == ("external_step", "waiting")
    setups = gateway._credential_workflow.tool_setups
    assert await setups.recover_unwatched() == 0, "a restart does not end it: the person still marks it done"
    assert await setups.external_step_done("tool_setup_req_step", person_id="someone-else", step_title=STEP) is False

    assert await setups.external_step_done("tool_setup_req_step", person_id=owner, step_title=STEP) is True

    [result] = await _results(transport)
    assert (result.status, result.kind, result.source) == ("completed", "external_step", "person_action")
    assert await setups.external_step_done("tool_setup_req_step", person_id=owner, step_title=STEP) is False, "once"


@pytest.mark.asyncio
async def test_an_external_step_the_catalog_lacks_is_refused(setup_gateway) -> None:
    gateway, _broker, transport = setup_gateway
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    gateway._credential_workflow.tool_setups.guide = _Guide()

    response = await _request_external_step(gateway, step_id="made_up")

    assert response.message_type == TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE
    [result] = await _results(transport)
    assert (result.status, result.reason_code) == ("unavailable", "tool_setup_step_unknown")


# What chat history keeps for each page (incident 2026-11-14) ----------------------------

WEB_SESSION = "session_web_setup"
WEB_REQUEST = "correlation_web_setup"
WEB_CONVERSATION = "conversation_web_setup"
REASON = "Needed so I can compare the two accounts you named."


def _web_chat_waiting_task(gateway: UserGateway, *, owner: str) -> None:
    """A Web Chat request whose task already waits, so a later page opens through the handoff."""

    gateway.state_store.create_session(
        session_key=WEB_SESSION, conversation_id=WEB_CONVERSATION,
        manager_agent_id=gateway.config.manager.manager_agent_id, channel_name="website", user_id=owner,
        chat_id="web-chat", owner_canonical_user_id=owner,
    )
    gateway.state_store.record_request(
        correlation_id=WEB_REQUEST, session_key=WEB_SESSION, request_kind="interactive",
        request_text="compare my accounts", conversation_id=WEB_CONVERSATION, followup_to_task_id=None,
        status="waiting_for_permission", owner_canonical_user_id=owner,
    )
    gateway.state_store.update_request_coordination(WEB_REQUEST, status="waiting_for_permission", active_task_id=TASK)


def _page_request(setup_request_id: str, *, tool_id: str, kind: str, owner: str) -> ToolSetupRequestV1:
    return ToolSetupRequestV1(
        setup_request_id=setup_request_id, parent_task_id=TASK, tool_id=tool_id, kind=kind, owner=owner,
        step_id="create_token" if kind == "external_step" else None, reason=REASON,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "kind", "owner"),
    [
        ("cli:x", "sign_in", "person"),
        ("cli:google", "team_sign_in_app", "team"),
        ("http:portainer", "account_link", "person"),
        ("http:portainer", "account_link", "team"),
        ("http:gitea", "external_step", "person"),
    ],
)
async def test_chat_history_keeps_each_pages_own_line_and_never_its_address(setup_gateway, tool_id, kind, owner) -> None:
    gateway, _broker, _transport = setup_gateway
    person = gateway.config.user.canonical_user_id  # an administrator, so team pages open too
    _waiting_task(gateway, owner=person)
    _web_chat_waiting_task(gateway, owner=person)
    setups = gateway._credential_workflow.tool_setups
    setups.guide = _Guide()
    # The live reply: the line Web Chat stores for it is the reply's transcript_text.
    live = await setups.start(
        request_content=_page_request("tool_setup_req_live", tool_id=tool_id, kind=kind, owner=owner),
        request_envelope=_setup_envelope(gateway), correlation_id=CHAT_REQUEST,
        conversation_id="conversation_link", session_key=SESSION,
    )
    assert live.message_type == TOOL_SETUP_MESSAGE_TYPE, live.text
    # A later page of the same task, after the chat reply closed: the handoff.
    later = _setup_envelope(gateway)
    later.msg_content = _page_request("tool_setup_req_later", tool_id=tool_id, kind=kind, owner=owner)
    later.msg_meta = MessageMeta(
        msg_correlation_id=WEB_REQUEST, conversation_id=WEB_CONVERSATION,
        gateway_context=GatewayContext(
            session_key=WEB_SESSION, canonical_user_id=person, channel_name="website", chat_id="web-chat"
        ),
    )

    assert await gateway._credential_handoffs.try_present(later) is True

    card = gateway._credential_handoffs._pending["tool_setup_req_later"].action
    history = gateway.application.transcripts.list_messages(
        owner_canonical_user_id=person, session_key=WEB_SESSION, before_cursor=None, limit=20
    )
    [line] = [message for message in history.messages if message.message_type == TOOL_SETUP_MESSAGE_TYPE]
    assert line.content == card.receipt == live.transcript_text
    assert line.content != TOOL_SETUP_READY_TEXT
    kept = history.model_dump_json()
    assert REASON not in kept and REASON not in live.transcript_text, "the task's own words stay out"
    assert BROKER_TOKEN not in kept
    if kind == "external_step":
        for part in (
            "Create a Gitea token.",
            "generate a token that can read repositories.",
            "Open: https://gitea.example.com/user/settings/applications",
            "Mark done on the workflow page: /workflows/access_flow_gitea",
        ):
            assert part in line.content
    else:
        # The page's bearer address, its broker session and its status route are never kept.
        assert card.url and card.url not in kept and live.transient_action.url not in kept
        for secret in ("link_session_", "signed_session", "/account-link/", "/auth/", "/v1/", PUBLIC_BROKER_BASE):
            assert secret not in kept


# Closing the card when a page ends (live run, 2026-09-28) ---------------------------------


@pytest_asyncio.fixture
async def chat_setup_gateway(tmp_path, fake_broker):  # noqa: F811 - the imported pytest fixture
    """A gateway whose chat messages are captured (the posts that close setup cards)."""

    from dataclasses import replace

    from assistant_gateway.store import GatewayStateStore
    from tests.test_gateway_phase1 import _build_test_config, _CaptureGatewayManagerTransport

    broker, client = fake_broker
    events: list = []
    transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        _build_test_config(tmp_path),
        output_sink=None,
        event_sink=events.append,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=transport,
        secure_broker=client,
    )
    gateway._credential_workflow._settings = replace(gateway._credential_workflow._settings, poll_interval_seconds=0.01)
    gateway._credential_workflow.link_results._poll_interval_seconds = 0.01
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    yield gateway, broker, transport, events
    await gateway._credential_workflow.close()


def _closings(events: list) -> list:
    from assistant_gateway.domain.message_types import TOOL_SETUP_RESULT_MESSAGE_TYPE

    return [event for event in events if event.message_type == TOOL_SETUP_RESULT_MESSAGE_TYPE]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "text"),
    [
        ("completed", "The X sign-in is done (account exampleuser)."),
        ("expired", "The page for the X sign-in expired before it was finished."),
    ],
)
async def test_a_sign_in_page_that_ends_posts_one_message_that_closes_its_card(chat_setup_gateway, ending, text) -> None:
    gateway, broker, transport, events = chat_setup_gateway
    if ending == "expired":
        broker.sign_in_expires_in = -timedelta(minutes=2)
    response = await _request_setup(gateway, tool_id="cli:x", kind="sign_in", reason="To post on X, sign in once.")
    assert response.transient_action.request_id == "tool_setup_req_test", "the card knows its page"
    if ending == "completed":
        broker.sign_in_status["x"] = {"status": "success", "username": "exampleuser"}

    await _results(transport)
    await _settle(gateway)

    [closing] = _closings(events)
    assert (closing.correlation_id, closing.session_key, closing.text) == ("tool_setup_req_test", SESSION, text)


@pytest.mark.asyncio
async def test_a_closed_form_posts_one_closing_message(chat_setup_gateway) -> None:
    gateway, broker, transport, events = chat_setup_gateway
    await _request_setup(gateway)

    broker.decline("link_session_1")
    await _results(transport)
    await _settle(gateway)

    [closing] = _closings(events)
    assert closing.correlation_id == "tool_setup_req_test"
    assert closing.text == "The form for the account link for Portainer was closed without linking anything."


def _waiting_record(
    gateway: UserGateway,
    tool_id: str,
    kind: str,
    owner: str,
    setup_request_id: str = "tool_setup_req_old",
    created_at: str = "2026-09-28T08:00:00+00:00",
) -> None:
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id=setup_request_id, parent_task_id=TASK, person_id=gateway.config.user.canonical_user_id,
            tool_id=tool_id, kind=kind, owner_kind=owner, session_key=SESSION,
            correlation_id=CHAT_REQUEST, conversation_id="conversation_link", status="waiting",
            created_at=created_at,
        )
    )


# Each broker page kind: the tool, its kind and owner, and the page in words (sign-in names, catalog names).
PAGE_KINDS = [
    ("cli:github", "sign_in", "person", "the GitHub sign-in"),
    ("cli:google", "team_sign_in_app", "team", "the team's Google sign-in app"),
    ("http:portainer", "account_link", "person", "the account link for Portainer"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_id", "kind", "owner", "what"), PAGE_KINDS)
async def test_a_page_nobody_watches_after_a_restart_is_closed_with_its_name(
    chat_setup_gateway, tool_id, kind, owner, what
) -> None:
    gateway, _broker, transport, events = chat_setup_gateway
    gateway._credential_workflow.tool_setups.guide = _Guide()
    _waiting_record(gateway, tool_id, kind, owner)

    assert await gateway._credential_workflow.tool_setups.recover_unwatched() == 1

    [closing] = _closings(events)
    [result] = await _results(transport)
    line = f"The gateway restarted while the page for {what} was open, so nobody could see how it ended."
    assert (closing.correlation_id, closing.text, result.message) == ("tool_setup_req_old", line, line)


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_reported_is_logged_and_the_later_ones_still_are(chat_setup_gateway, caplog) -> None:
    """A stored kind this version does not know (a rollback, a hand edit) must not keep every later page waiting."""

    gateway, _broker, transport, events = chat_setup_gateway
    _waiting_record(
        gateway, "cli:github", "password_reset", "person",
        setup_request_id="tool_setup_req_bad", created_at="2026-09-28T07:00:00+00:00",
    )
    _waiting_record(gateway, "cli:github", "sign_in", "person")

    with caplog.at_level("WARNING"):
        assert await gateway._credential_workflow.tool_setups.recover_unwatched() == 1

    [closing] = _closings(events)
    [result] = await _results(transport)
    assert (closing.correlation_id, result.setup_request_id) == ("tool_setup_req_old", "tool_setup_req_old")
    [failure] = [
        record for record in caplog.records if "event=gateway_tool_setup_recovery_failed" in record.getMessage()
    ]
    assert "setup_request_id=tool_setup_req_bad" in failure.getMessage() and failure.exc_info is not None
    assert "status=waiting" in failure.getMessage(), "it failed before it was claimed, so the next start tries again"
    assert _setups(gateway).get_tool_setup("tool_setup_req_bad").status == "waiting"


@pytest.mark.asyncio
async def test_a_result_whose_chat_is_gone_is_marked_unsendable_and_logged_once(chat_setup_gateway, caplog) -> None:
    """The page is reported, but its result can never be sent: it leaves the kept results with one error line."""

    gateway, _broker, transport, _events = chat_setup_gateway
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id="tool_setup_req_no_chat", parent_task_id=TASK, person_id=gateway.config.user.canonical_user_id,
            tool_id="cli:github", kind="sign_in", owner_kind="person", session_key="session_gone",
            correlation_id=CHAT_REQUEST, conversation_id="conversation_link", status="waiting",
            created_at="2026-09-28T08:00:00+00:00",
        )
    )

    with caplog.at_level("WARNING"):
        assert await gateway._credential_workflow.tool_setups.recover_unwatched() == 1
        assert (await gateway._credential_workflow.tool_setups.resend_kept_results()).sent == 0

    assert transport.durable == []
    assert _setups(gateway).get_tool_setup("tool_setup_req_no_chat").result_state == "unsendable"
    [unsendable] = [record for record in caplog.records if "event=gateway_tool_setup_result_unsendable" in record.getMessage()]
    assert unsendable.levelname == "ERROR"
    assert "setup_request_id=tool_setup_req_no_chat task_id=task_link tool_id=cli:github kind=sign_in" in unsendable.getMessage()
    assert "reason=session_missing" in unsendable.getMessage()
    assert "event=gateway_tool_setup_recovery_failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_id", "kind", "owner", "what"), PAGE_KINDS)
async def test_finishing_the_setup_with_link_or_auth_names_the_page(chat_setup_gateway, tool_id, kind, owner, what) -> None:
    gateway, _broker, transport, events = chat_setup_gateway
    gateway._credential_workflow.tool_setups.guide = _Guide()
    _waiting_record(gateway, tool_id, kind, owner)
    label = None if kind == "account_link" else "exampleuser"

    told = await gateway._credential_workflow.tool_setups.person_finished(
        person_id=gateway.config.user.canonical_user_id, tool_id=tool_id, kind=kind,
        owner=owner if kind == "account_link" else None, account_label=label,
    )

    assert told == 1
    [closing] = _closings(events)
    [result] = await _results(transport)
    assert closing.text == f"You finished {what} yourself, so this page is no longer needed."
    assert result.message == f"The person finished {what} on their own" + (" (account exampleuser)." if label else ".")


@pytest.mark.asyncio
async def test_marking_an_outside_step_done_names_the_step_not_an_account_link(chat_setup_gateway) -> None:
    """The 2026-11-14 incident: Mark Done read 'You finished the account link for <tool id> yourself ...'."""

    gateway, _broker, transport, events = chat_setup_gateway
    owner = gateway.config.user.canonical_user_id
    setups = gateway._credential_workflow.tool_setups
    setups.guide = _Guide()
    await _request_external_step(gateway)

    assert await setups.external_step_done("tool_setup_req_step", person_id=owner, step_title=STEP) is True
    assert await setups.external_step_done("tool_setup_req_step", person_id=owner, step_title=STEP) is False

    [closing] = _closings(events)
    [result] = await _results(transport)
    assert closing.correlation_id == "tool_setup_req_step"
    assert closing.text == "You marked the Gitea setup step 'Create a Gitea token' done."
    assert "account link" not in closing.text
    assert (result.source, result.message) == (
        "person_action",
        "The person marked the Gitea setup step 'Create a Gitea token' done.",
    )


@pytest.mark.asyncio
async def test_a_refused_form_names_the_page_that_did_not_finish(chat_setup_gateway) -> None:
    gateway, broker, transport, events = chat_setup_gateway
    await _request_setup(gateway)

    broker.refuse(
        "link_session_1",
        error="That credential is not one of this tool's credentials",
        reason_code="account_link_credential_mismatch",
    )
    await _results(transport)
    await _settle(gateway)

    [closing] = _closings(events)
    assert closing.text == (
        "The account link for Portainer did not finish. That credential is not one of this tool's credentials."
    )


@pytest.mark.asyncio
async def test_a_tool_the_catalog_lacks_is_named_by_its_id_and_logged(chat_setup_gateway, caplog) -> None:
    gateway, _broker, _transport, events = chat_setup_gateway
    gateway._credential_workflow.tool_setups.guide = _Guide()
    _waiting_record(gateway, "http:removed_tool", "account_link", "person")

    with caplog.at_level("WARNING"):
        await gateway._credential_workflow.tool_setups.recover_unwatched()

    [closing] = _closings(events)
    assert closing.text.startswith("The gateway restarted while the page for the account link for http:removed_tool was")
    assert any(
        "event=gateway_tool_setup_name_unknown" in record.getMessage() and "http:removed_tool" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_step_marked_done_without_its_stored_title_names_the_tool_and_is_logged(chat_setup_gateway, caplog) -> None:
    gateway, _broker, _transport, events = chat_setup_gateway
    setups = gateway._credential_workflow.tool_setups
    setups.guide = _Guide()
    _waiting_record(gateway, "http:gitea", "external_step", "person", setup_request_id="tool_setup_req_step")

    with caplog.at_level("WARNING"):
        assert await setups.external_step_done(
            "tool_setup_req_step", person_id=gateway.config.user.canonical_user_id, step_title="  "
        )

    [closing] = _closings(events)
    assert closing.text == "You marked the Gitea setup step done."
    assert any("event=gateway_tool_setup_step_title_missing" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_persons_own_link_closes_the_task_card_without_repeating_the_link_result(chat_setup_gateway) -> None:
    gateway, broker, transport, events = chat_setup_gateway
    owner = gateway.config.user.canonical_user_id
    gateway._credential_workflow.tool_setups.guide = _Guide()
    await _request_setup(gateway)

    await gateway.command_application.execute(
        GatewayCommandRequest(
            raw_text="/link http:portainer", current_session_key=SESSION, channel_name="cli", user_id=owner,
            chat_id=f"cli:{gateway.config.instance_name}", canonical_user_id=owner, correlation_id="corr_person_link",
        )
    )
    broker.complete("link_session_2", link_id="account_link_from_command")
    await _results(transport)
    broker.refuse("link_session_1", error="expired", reason_code="account_link_session_expired")
    await _settle(gateway)

    [closing] = _closings(events)
    link_results = [event for event in events if event.message_type == "link_result"]
    assert closing.correlation_id == "tool_setup_req_test"
    assert closing.text == "You finished the account link for Portainer yourself, so this page is no longer needed."
    assert all(closing.text != event.text for event in link_results)


@pytest.mark.asyncio
async def test_a_refused_page_posts_no_closing_message(chat_setup_gateway) -> None:
    gateway, _broker, transport, events = chat_setup_gateway

    refused = await _request_setup(gateway, tool_id="cli:github", kind="team_sign_in_app", owner="team")

    assert refused.transient_action is None
    await _results(transport)
    assert _closings(events) == []



@pytest.mark.asyncio
async def test_web_chat_records_the_closing_line_under_the_pages_own_id(chat_setup_gateway) -> None:
    gateway, _broker, _transport, _events = chat_setup_gateway
    owner = gateway.config.user.canonical_user_id
    gateway.state_store.create_session(
        session_key="session_web", conversation_id="conversation_web",
        manager_agent_id=gateway.config.manager.manager_agent_id, channel_name="website", user_id=owner,
        chat_id="web-chat", owner_canonical_user_id=owner,
    )
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id="tool_setup_req_web", parent_task_id=TASK, person_id=owner, tool_id="cli:github",
            kind="sign_in", owner_kind="person", session_key="session_web", correlation_id=CHAT_REQUEST,
            conversation_id="conversation_web", status="waiting", created_at="2026-09-28T08:00:00+00:00",
        )
    )

    await gateway._credential_workflow.tool_setups.recover_unwatched()

    page = gateway._transcript_service.history_page(
        owner_canonical_user_id=owner, session_key="session_web", before_cursor=None
    )
    [line] = [message for message in page.messages if message.message_type == "tool_setup_result"]
    assert (line.correlation_id, line.role) == ("tool_setup_req_web", "system")


# The words for each page and each way it ends (the 2026-11-14 Mark Done line) ------------


def test_every_setup_kind_in_the_contract_has_its_own_words() -> None:
    """A kind added to the contract fails here until ``_what`` names it, never borrowing another kind's words."""

    phrases = {kind: _what(kind, "Gitea", "Create a Gitea token") for kind in get_args(ToolSetupKind)}

    assert phrases == {
        "sign_in": "the Gitea sign-in",
        "account_link": "the account link for Gitea",
        "team_sign_in_app": "the team's Gitea sign-in app",
        "external_step": "the Gitea setup step 'Create a Gitea token'",
    }
    assert _what("external_step", "Gitea") == "the Gitea setup step", "a step whose title is not known"
    with pytest.raises(ValueError):
        _what("password_reset", "Gitea")


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            BrokerSessionOutcome(WATCH_EXPIRED),
            ("expired", "The page for the GitHub sign-in expired before it was finished.", None),
        ),
        (
            BrokerSessionOutcome(WATCH_UNCONFIRMED),
            ("unavailable", "Nobody could confirm whether the GitHub sign-in finished.", "tool_setup_page_unconfirmed"),
        ),
        (
            BrokerSessionOutcome(WATCH_FINISHED, {"status": "pending"}),
            ("unavailable", "Nobody could confirm whether the GitHub sign-in finished.", "tool_setup_page_unconfirmed"),
        ),
        (
            BrokerSessionOutcome(
                WATCH_FINISHED,
                {"status": "error", "message": "The sign-in was cancelled", "failure_reason": "sign_in_cancelled"},
            ),
            ("unavailable", "The GitHub sign-in did not finish. The sign-in was cancelled.", "sign_in_cancelled"),
        ),
        (
            BrokerSessionOutcome(WATCH_FINISHED, {"status": "error"}),
            ("unavailable", "The GitHub sign-in did not finish.", "tool_setup_sign_in_failed"),
        ),
        (
            BrokerSessionOutcome(WATCH_FINISHED, {"status": "success", "username": "exampleuser"}),
            ("completed", "The GitHub sign-in is done (account exampleuser).", None),
        ),
        (BrokerSessionOutcome(WATCH_FINISHED, {"status": "success"}), ("completed", "The GitHub sign-in is done.", None)),
    ],
)
def test_each_way_a_sign_in_page_ends_names_the_page(outcome, expected) -> None:
    ending = sign_in_page_ending(outcome, what="the GitHub sign-in")

    assert (ending.status, ending.message, ending.reason_code) == expected
    assert ending.closing == "", "the chat shows the same line the task hears"


def _link_result(**result) -> BrokerSessionOutcome:
    return BrokerSessionOutcome(WATCH_FINISHED, {"result": result})


_CREATED = {"status": "account_link_created", "link_id": "account_link_1", "owner_kind": "person"}


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            BrokerSessionOutcome(WATCH_EXPIRED),
            ("expired", "The form for the account link for Portainer expired before it was used.", None),
        ),
        (
            BrokerSessionOutcome(WATCH_UNCONFIRMED),
            (
                "unavailable",
                "Nobody could confirm whether the account link for Portainer finished.",
                "tool_setup_page_unconfirmed",
            ),
        ),
        (
            _link_result(**_CREATED, tool_id="http:portainer"),
            ("completed", "The account link for Portainer is done.", None),
        ),
        (
            _link_result(**_CREATED, tool_id="http:other"),
            (
                "unavailable",
                "Nobody could confirm whether the account link for Portainer finished.",
                "tool_setup_link_mismatch",
            ),
        ),
        (
            _link_result(**{**_CREATED, "owner_kind": "team"}, tool_id="http:portainer"),
            (
                "unavailable",
                "Nobody could confirm whether the account link for Portainer finished.",
                "tool_setup_link_mismatch",
            ),
        ),
        (
            _link_result(status="account_link_declined"),
            ("declined", "The form for the account link for Portainer was closed without linking anything.", None),
        ),
        (
            _link_result(status="account_link_unavailable", reason_code="account_link_session_expired"),
            ("expired", "The form for the account link for Portainer expired before it was used.", None),
        ),
        (
            _link_result(
                status="account_link_unavailable",
                reason_code="account_link_credential_mismatch",
                error="That credential is not one of this tool's credentials",
            ),
            (
                "unavailable",
                "The account link for Portainer did not finish. That credential is not one of this tool's credentials.",
                "account_link_credential_mismatch",
            ),
        ),
        (
            _link_result(status="account_link_unavailable"),
            ("unavailable", "The account link for Portainer did not finish.", "tool_setup_page_refused"),
        ),
    ],
)
def test_each_way_an_account_link_form_ends_names_the_page(outcome, expected) -> None:
    ending = account_link_page_ending(
        outcome, what="the account link for Portainer", tool_id="http:portainer", owner="person"
    )

    assert (ending.status, ending.message, ending.reason_code) == expected
    assert ending.closing == "", "the chat shows the same line the task hears"


# Results kept while no team lead can be reached (review of the 2026-11-14 incident fixes) ---


class _FlakyTransport:
    """The gateway's manager transport whose next ``failures`` durable publishes raise, as when NATS is down."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.durable: list[tuple[MessageEnvelope, str]] = []
        # Every durable publish tried, sent or not: (queue, NATS message id).
        self.attempts: list[tuple[str, str | None]] = []
        self.operational: list[tuple[MessageEnvelope, str]] = []
        self.release: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def publish_durable(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        self.entered.set()
        self.attempts.append((routing_key, envelope.msg_header.idempotency_key))
        if self.release is not None:
            await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise ConnectionError("nats: connection closed")
        self.durable.append((envelope, routing_key))

    async def publish_operational(self, envelope: MessageEnvelope, *, routing_key: str) -> None:
        self.operational.append((envelope, routing_key))


@pytest_asyncio.fixture
async def flaky_gateway(tmp_path, fake_broker):  # noqa: F811 - the imported pytest fixture
    """A gateway whose manager transport can fail, and whose kept results are retried quickly."""

    from dataclasses import replace

    from assistant_gateway.application import tool_setup as tool_setup_module
    from assistant_gateway.store import GatewayStateStore
    from tests.test_gateway_phase1 import _build_test_config

    _broker, client = fake_broker
    events: list = []
    transport = _FlakyTransport()
    gateway = UserGateway(
        _build_test_config(tmp_path),
        output_sink=None,
        event_sink=events.append,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=transport,
        secure_broker=client,
    )
    gateway._credential_workflow._settings = replace(gateway._credential_workflow._settings, poll_interval_seconds=0.01)
    original_retry = tool_setup_module.SECURE_RESULT_RETRY_MIN_SECONDS
    tool_setup_module.SECURE_RESULT_RETRY_MIN_SECONDS = 0.01
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    yield gateway, transport, events
    tool_setup_module.SECURE_RESULT_RETRY_MIN_SECONDS = original_retry
    await gateway._credential_workflow.close()


def _setup_results(transport) -> list[tuple[ToolSetupResultV1, str, str]]:
    """Each setup result sent: the result, the lead's queue it went to and its NATS message id."""

    return [
        (envelope.msg_content, routing_key, envelope.msg_header.idempotency_key)
        for envelope, routing_key in transport.durable
        if isinstance(envelope.msg_content, ToolSetupResultV1)
    ]


def _kept_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "event=gateway_tool_setup_result_kept" in record.getMessage()]


async def _until(condition, *, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_a_page_ending_without_a_team_lead_keeps_its_result_for_the_next_start(
    chat_setup_gateway, tmp_path, caplog
) -> None:
    from assistant_gateway.store import GatewayStateStore
    from tests.test_gateway_phase1 import _CaptureGatewayManagerTransport

    gateway, _broker, transport, events = chat_setup_gateway
    _waiting_record(gateway, "cli:github", "sign_in", "person")
    await gateway._manager_client.mark_manager_unavailable(reason="the team lead restarted")

    with caplog.at_level("INFO"):
        assert await gateway._credential_workflow.tool_setups.recover_unwatched() == 1

    assert _setup_results(transport) == []
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "unsent"
    [kept] = _kept_lines(caplog)
    assert (
        "setup_request_id=tool_setup_req_old task_id=task_link tool_id=cli:github kind=sign_in "
        "status=unavailable reason=no_route"
    ) in kept
    assert "event=gateway_secure_result_not_sent reason=no_route" in caplog.text
    assert gateway.state_store.get_request(CHAT_REQUEST).status == "pending", "the chat request is left as it was"
    assert [closing.correlation_id for closing in _closings(events)] == ["tool_setup_req_old"], "the card still closes"

    # The gateway restarts on the same store and finds the team lead.
    after_restart = _CaptureGatewayManagerTransport()
    restarted = UserGateway(
        gateway.config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=after_restart,
    )
    # Connecting found the restarted team lead (refresh_manager_id) before the start tasks run.
    lead = "manager_after_restart"
    restarted._manager_client._manager_id = lead
    start_config = SimpleNamespace(
        instance_root=gateway.config.instance_root,
        manager=gateway.config.manager,
        authorization=SimpleNamespace(enabled=False),
    )
    tasks = gateway_module._access_maintenance_tasks(
        start_config,
        access_inbox=restarted.access_inbox,
        broker=restarted._secure_broker,
        tool_setups=restarted._credential_workflow.tool_setups,
        credential_workflow=restarted._credential_workflow,
        status_page=restarted.application.status,
        stop_event=asyncio.Event(),
    )
    resend = next(task for task in tasks if task.get_name() == "gateway_tool_setup_resend")
    try:
        assert (await resend).sent == 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    [(result, queue, message_id)] = _setup_results(after_restart)
    assert (result.setup_request_id, result.reason_code) == ("tool_setup_req_old", "tool_setup_page_not_watched")
    assert (queue, message_id) == (lead, f"secure_result:tool_setup_result:tool_setup_req_old:{lead}")
    assert result.broker_session_id is None
    sent = restarted.state_store.tool_setup_repository.get_tool_setup("tool_setup_req_old")
    assert (sent.result_state, sent.result_json) == ("sent", None), "the kept copy goes once it is sent"
    assert (await restarted._credential_workflow.tool_setups.resend_kept_results()).sent == 0
    assert len(_setup_results(after_restart)) == 1, "sent once"
    await restarted._credential_workflow.close()


@pytest.mark.asyncio
async def test_a_failed_publish_keeps_the_result_and_the_retry_sends_it(flaky_gateway, caplog) -> None:
    gateway, transport, _events = flaky_gateway
    setups = gateway._credential_workflow.tool_setups
    transport.failures = 2
    _waiting_record(gateway, "http:portainer", "account_link", "person")

    with caplog.at_level("INFO"):
        assert await setups.recover_unwatched() == 1
        await _until(lambda: len(_setup_results(transport)) == 1)
        await _until(lambda: setups._retries.count == 0)

    [kept] = _kept_lines(caplog)
    assert "setup_request_id=tool_setup_req_old task_id=task_link tool_id=http:portainer kind=account_link" in kept
    assert "reason=publish_failed" in kept
    assert "event=gateway_secure_result_not_sent reason=publish_failed" in caplog.text
    assert "error_type=ConnectionError" in caplog.text
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "sent"
    assert gateway.state_store.get_request(CHAT_REQUEST).status == "waiting_for_credential_result"
    assert len(_setup_results(transport)) == 1, "the retry stopped after the pass that sent it"


@pytest.mark.asyncio
async def test_a_result_kept_under_one_lead_is_sent_to_the_next_under_its_own_message_id(flaky_gateway) -> None:
    gateway, transport, _events = flaky_gateway
    setups = gateway._credential_workflow.tool_setups
    first_lead = gateway._manager_client.manager_id
    transport.failures = 1
    _waiting_record(gateway, "cli:github", "sign_in", "person")

    assert await setups.recover_unwatched() == 1
    assert setups._retries.count == 1, "a failed publish while a lead is known starts the retry"
    # The lead stops before the retry: the retry finds no lead and ends; the next lead's arrival sends it.
    gateway._manager_client._manager_id = None
    await _until(lambda: setups._retries.count == 0)
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "unsent"
    # The failed publish to the known lead left the chat request waiting for the result, never finished.
    assert gateway.state_store.get_request(CHAT_REQUEST).status == "waiting_for_credential_result"

    gateway._manager_client._manager_id = "manager_replacement"
    assert (await setups.resend_kept_results()).sent == 1

    [(result, queue, message_id)] = _setup_results(transport)
    assert result.setup_request_id == "tool_setup_req_old"
    assert transport.attempts == [
        (first_lead, f"secure_result:tool_setup_result:tool_setup_req_old:{first_lead}"),
        ("manager_replacement", "secure_result:tool_setup_result:tool_setup_req_old:manager_replacement"),
    ], "each lead gets its own message id, so NATS does not drop the copy for the new lead as a repeat"
    assert (queue, message_id) == transport.attempts[-1]


@pytest.mark.asyncio
async def test_a_result_that_can_never_be_sent_does_not_keep_the_retry_going(flaky_gateway, caplog) -> None:
    """One kept result whose chat is gone, one unreadable, one whose publish fails once (critique C01)."""

    gateway, transport, _events = flaky_gateway
    setups = gateway._credential_workflow.tool_setups
    for page_id, session_key, created_at in (
        ("tool_setup_req_gone", "session_gone", "2026-09-28T07:00:00+00:00"),
        ("tool_setup_req_garbled", SESSION, "2026-09-28T07:30:00+00:00"),
    ):
        _setups(gateway).record_tool_setup(
            ToolSetupRecord(
                setup_request_id=page_id, parent_task_id=TASK, person_id=gateway.config.user.canonical_user_id,
                tool_id="cli:github", kind="sign_in", owner_kind="person", session_key=session_key,
                correlation_id=CHAT_REQUEST, conversation_id="conversation_link", status="waiting",
                created_at=created_at,
            )
        )
        assert _setups(gateway).finish_tool_setup(page_id, status="expired", result_json='{"not": "a result"}'
                                                  if page_id.endswith("garbled") else _result_json(page_id))
    transport.failures = 1
    _waiting_record(gateway, "cli:github", "sign_in", "person")

    with caplog.at_level("INFO"):
        assert await setups.recover_unwatched() == 1
        await _until(lambda: len(_setup_results(transport)) == 1)
        await _until(lambda: setups._retries.count == 0)
        assert (await setups.resend_kept_results()).sent == 0

    assert [result.setup_request_id for result, _queue, _id in _setup_results(transport)] == ["tool_setup_req_old"]
    states = {page_id: _setups(gateway).get_tool_setup(page_id).result_state for page_id in (
        "tool_setup_req_gone", "tool_setup_req_garbled", "tool_setup_req_old")}
    assert states == {"tool_setup_req_gone": "unsendable", "tool_setup_req_garbled": "unsendable", "tool_setup_req_old": "sent"}
    unsendable = [record.getMessage() for record in caplog.records if "event=gateway_tool_setup_result_unsendable" in record.getMessage()]
    assert len(unsendable) == 2, "each is logged once"
    assert {line.rsplit("reason=", 1)[1] for line in unsendable} == {"session_missing", "unreadable"}


def _result_json(setup_request_id: str) -> str:
    return ToolSetupResultV1(
        setup_request_id=setup_request_id, status="expired", parent_task_id=TASK, tool_id="cli:github",
        kind="sign_in", owner="person", message="The page expired.",
    ).model_dump_json()


@pytest.mark.asyncio
async def test_a_page_ending_while_another_send_waits_is_sent_once_after_it(flaky_gateway) -> None:
    """Sends take turns: a pass that waits for the lock reads the kept results afresh, and skips none (critique C03)."""

    gateway, transport, _events = flaky_gateway
    setups = gateway._credential_workflow.tool_setups
    transport.release = asyncio.Event()
    _waiting_record(gateway, "cli:github", "sign_in", "person", setup_request_id="tool_setup_req_first")
    _waiting_record(
        gateway, "http:portainer", "account_link", "person",
        setup_request_id="tool_setup_req_second", created_at="2026-09-28T09:00:00+00:00",
    )
    first = asyncio.create_task(setups._report_unwatched(_setups(gateway).get_tool_setup("tool_setup_req_first")))
    await transport.entered.wait()
    # While the first page's publish waits: a lead-available transition's pass and the second page's ending.
    transition = asyncio.create_task(setups.resend_kept_results())
    second = asyncio.create_task(setups._report_unwatched(_setups(gateway).get_tool_setup("tool_setup_req_second")))
    await asyncio.sleep(0.05)
    assert not transition.done() and not second.done(), "both wait for the first send"

    transport.release.set()
    assert await first and await second
    await transition

    assert [result.setup_request_id for result, _queue, _id in _setup_results(transport)] == [
        "tool_setup_req_first",
        "tool_setup_req_second",
    ]
    assert {record.result_state for record in _setups(gateway).list_tool_setups(parent_task_id=TASK)} == {"sent"}


@pytest.mark.asyncio
async def test_an_error_while_sending_still_closes_the_card_and_keeps_the_result(
    chat_setup_gateway, monkeypatch, caplog
) -> None:
    """A database error in a send pass is logged; the closing line still posts (C07) and the retry sends it (C09)."""

    import sqlite3

    from assistant_gateway.application import tool_setup as tool_setup_module

    gateway, _broker, transport, events = chat_setup_gateway
    setups = gateway._credential_workflow.tool_setups
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MIN_SECONDS", 0.01)
    requests = gateway._credential_workflow._publisher._request_service
    original_record = requests.record
    calls = 0

    def record_failing_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_record(**kwargs)

    monkeypatch.setattr(requests, "record", record_failing_once)
    _waiting_record(gateway, "cli:github", "sign_in", "person")

    with caplog.at_level("INFO"):
        assert await setups.recover_unwatched() == 1

    assert [closing.correlation_id for closing in _closings(events)] == ["tool_setup_req_old"]
    assert _setup_results(transport) == []
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "unsent"
    [failed] = [record for record in caplog.records if "event=gateway_tool_setup_resend_failed" in record.getMessage()]
    assert "setup_request_id=tool_setup_req_old task_id=task_link error_type=OperationalError" in failed.getMessage()
    assert "reason=error" in _kept_lines(caplog)[0]
    assert "event=gateway_tool_setup_recovery_failed" not in caplog.text

    # The lead stays the same and no other page ends: the retry sends it by itself.
    await _until(lambda: len(_setup_results(transport)) == 1)
    await _until(lambda: setups._retries.count == 0)
    [(result, _queue, _message_id)] = _setup_results(transport)
    assert result.setup_request_id == "tool_setup_req_old"
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "sent"


@pytest.mark.asyncio
async def test_a_result_failing_on_every_pass_ends_the_retry_with_one_line(chat_setup_gateway, monkeypatch, caplog) -> None:
    """The retry gives up after RESEND_ERROR_PASSES_LIMIT error passes in a row; the result stays kept (C09)."""

    import sqlite3

    from assistant_gateway.application import tool_setup as tool_setup_module

    gateway, _broker, transport, _events = chat_setup_gateway
    setups = gateway._credential_workflow.tool_setups
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MIN_SECONDS", 0.001)
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MAX_SECONDS", 0.002)

    def record_always_failing(**_kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(gateway._credential_workflow._publisher._request_service, "record", record_always_failing)
    _waiting_record(gateway, "cli:github", "sign_in", "person")

    with caplog.at_level("INFO"):
        assert await setups.recover_unwatched() == 1
        await _until(lambda: setups._retries.count == 0)

    assert _setup_results(transport) == []
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "unsent", "kept for the next start"
    lines = [record.getMessage() for record in caplog.records]
    [gave_up] = [line for line in lines if "event=gateway_tool_setup_resend_gave_up" in line]
    assert f"error_passes={tool_setup_module.RESEND_ERROR_PASSES_LIMIT}" in gave_up
    failed = [line for line in lines if "event=gateway_tool_setup_resend_failed" in line]
    assert len(failed) == 1 + tool_setup_module.RESEND_ERROR_PASSES_LIMIT, "the page's own pass, then each retry"


@pytest.mark.asyncio
async def test_a_result_that_fails_every_time_does_not_hold_back_newer_ones(chat_setup_gateway, monkeypatch, caplog) -> None:
    """An error on the oldest kept result keeps it and logs it; the pass goes on to the newer ones (critique C11)."""

    import sqlite3

    from assistant_gateway.application import tool_setup as tool_setup_module

    gateway, _broker, transport, _events = chat_setup_gateway
    setups = gateway._credential_workflow.tool_setups
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MIN_SECONDS", 0.001)
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MAX_SECONDS", 0.002)
    requests = gateway._credential_workflow._publisher._request_service
    original_record = requests.record

    def record_failing_for_one_request(**kwargs):
        if kwargs["correlation_id"] == "correlation_broken":
            raise sqlite3.OperationalError("database disk image is malformed")
        return original_record(**kwargs)

    monkeypatch.setattr(requests, "record", record_failing_for_one_request)
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id="tool_setup_req_broken", parent_task_id=TASK, person_id=gateway.config.user.canonical_user_id,
            tool_id="cli:github", kind="sign_in", owner_kind="person", session_key=SESSION,
            correlation_id="correlation_broken", conversation_id="conversation_link", status="waiting",
            created_at="2026-09-28T07:00:00+00:00",
        )
    )
    assert _setups(gateway).finish_tool_setup(
        "tool_setup_req_broken", status="expired", result_json=_result_json("tool_setup_req_broken")
    )
    _waiting_record(gateway, "http:portainer", "account_link", "person")

    with caplog.at_level("INFO"):
        assert await setups.recover_unwatched() == 1
        await _until(lambda: setups._retries.count == 0)

    assert [result.setup_request_id for result, _queue, _id in _setup_results(transport)] == ["tool_setup_req_old"]
    assert _setups(gateway).get_tool_setup("tool_setup_req_broken").result_state == "unsent"
    assert _setups(gateway).get_tool_setup("tool_setup_req_old").result_state == "sent"
    assert _kept_lines(caplog) == [], "the newer page's own result was sent"
    failed = [record.getMessage() for record in caplog.records if "event=gateway_tool_setup_resend_failed" in record.getMessage()]
    assert failed and all("setup_request_id=tool_setup_req_broken" in line for line in failed)
    assert "event=gateway_tool_setup_resend_gave_up" in caplog.text, "the retry still ends"


def _kept_result_row(gateway: UserGateway, setup_request_id: str) -> None:
    """A page that ended earlier and whose result is kept, unsent."""

    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id=setup_request_id, parent_task_id=TASK, person_id=gateway.config.user.canonical_user_id,
            tool_id="cli:github", kind="sign_in", owner_kind="person", session_key=SESSION,
            correlation_id=CHAT_REQUEST, conversation_id="conversation_link", status="waiting",
            created_at="2026-09-28T07:00:00+00:00",
        )
    )
    assert _setups(gateway).finish_tool_setup(
        setup_request_id, status="expired", result_json=_result_json(setup_request_id)
    )


@pytest.mark.asyncio
async def test_a_database_outage_longer_than_the_give_up_does_not_end_the_retry(
    chat_setup_gateway, monkeypatch, caplog
) -> None:
    """Passes that cannot even list the kept results do not count toward the give-up (review FAIL-2)."""

    import sqlite3

    from assistant_gateway.application import tool_setup as tool_setup_module

    gateway, _broker, transport, _events = chat_setup_gateway
    setups = gateway._credential_workflow.tool_setups
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MIN_SECONDS", 0.001)
    monkeypatch.setattr(tool_setup_module, "SECURE_RESULT_RETRY_MAX_SECONDS", 0.002)
    _kept_result_row(gateway, "tool_setup_req_kept")
    store = _setups(gateway)
    real_listing = store.list_unsent_tool_setup_results
    outage = tool_setup_module.RESEND_ERROR_PASSES_LIMIT + 3
    listings = 0

    def listing_during_an_outage():
        nonlocal listings
        listings += 1
        if listings <= outage:
            raise sqlite3.OperationalError("unable to open database file")
        return real_listing()

    monkeypatch.setattr(store, "list_unsent_tool_setup_results", listing_during_an_outage)

    with caplog.at_level("INFO"):
        sending = await setups.resend_kept_results()
        assert (sending.failed, sending.listing_failed, sending.result_failed) == (True, True, False)
        await _until(lambda: len(_setup_results(transport)) == 1)
        await _until(lambda: setups._retries.count == 0)

    assert listings == outage + 1, "the retry kept going through the outage and sent the result after it"
    assert "event=gateway_tool_setup_resend_gave_up" not in caplog.text
    assert _setups(gateway).get_tool_setup("tool_setup_req_kept").result_state == "sent"


@pytest.mark.asyncio
async def test_no_retry_starts_after_the_gateway_began_to_stop(flaky_gateway, caplog) -> None:
    """A lead announcing itself while the gateway stops must not leave a retry running after close (review CONC-02)."""

    gateway, transport, _events = flaky_gateway
    setups = gateway._credential_workflow.tool_setups
    transport.failures = 100  # NATS is closing: every publish fails while the route is still known
    _kept_result_row(gateway, "tool_setup_req_kept")

    await setups.close()
    with caplog.at_level("INFO"):
        sending = await setups.resend_kept_results()

    assert sending.stopped is not None and sending.stopped.value == "publish_failed"
    assert (setups._retries.count, setups._retrying) == (0, False)
    assert "event=gateway_tool_setup_retry_skipped_closed" in caplog.text
    await asyncio.sleep(0.05)
    assert len(transport.attempts) == 1, "nothing tries again after close"
    assert _setups(gateway).get_tool_setup("tool_setup_req_kept").result_state == "unsent", "kept for the next start"
