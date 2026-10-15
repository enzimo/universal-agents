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

import pytest
import pytest_asyncio

from assistant_gateway import gateway as gateway_module
from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.domain.message_types import (
    TOOL_SETUP_MESSAGE_TYPE,
    TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE,
)
from assistant_gateway.domain.request_state import ToolSetupRecord
from assistant_gateway.gateway import UserGateway
from custom_messages.contracts.credentials_v1 import ToolSetupRequestV1, ToolSetupResultV1
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from tests.test_gateway_account_links import (  # noqa: F401 - pytest fixtures
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
    assert result.message == "The person finished the account link for http:portainer on their own."
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
        request_id="access_req_1:http:portainer",
        person_id=owner,
        person_roles=gateway._request_context_service.team_roles(owner),
        tool_id="http:portainer",
        owner="team",
        reason="Restart the stack",
    )
    setups.after_proposal_links(request_id="access_req_1", forms=[(form, "http:portainer", "team")], then=release)
    await asyncio.sleep(0.05)
    assert not released.is_set(), "the task waits while the approver's form is open"

    broker.complete(form.session_id)
    await asyncio.wait_for(released.wait(), timeout=5)


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

    def workflow_url(self, task_id: str):
        return "/workflows/access_flow_gitea" if task_id == TASK else None


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
    record = _setups(gateway).get_tool_setup("tool_setup_req_step")
    assert (record.kind, record.status) == ("external_step", "waiting")
    setups = gateway._credential_workflow.tool_setups
    assert await setups.recover_unwatched() == 0, "a restart does not end it: the person still marks it done"
    assert await setups.external_step_done("tool_setup_req_step", person_id="someone-else") is False

    assert await setups.external_step_done("tool_setup_req_step", person_id=owner) is True

    [result] = await _results(transport)
    assert (result.status, result.kind, result.source) == ("completed", "external_step", "person_action")
    assert await setups.external_step_done("tool_setup_req_step", person_id=owner) is False, "once"


@pytest.mark.asyncio
async def test_an_external_step_the_catalog_lacks_is_refused(setup_gateway) -> None:
    gateway, _broker, transport = setup_gateway
    _waiting_task(gateway, owner=gateway.config.user.canonical_user_id)
    gateway._credential_workflow.tool_setups.guide = _Guide()

    response = await _request_external_step(gateway, step_id="made_up")

    assert response.message_type == TOOL_SETUP_UNAVAILABLE_MESSAGE_TYPE
    [result] = await _results(transport)
    assert (result.status, result.reason_code) == ("unavailable", "tool_setup_step_unknown")


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


@pytest.mark.asyncio
async def test_a_page_nobody_watches_after_a_restart_is_closed_too(chat_setup_gateway) -> None:
    gateway, _broker, transport, events = chat_setup_gateway
    _setups(gateway).record_tool_setup(
        ToolSetupRecord(
            setup_request_id="tool_setup_req_old", parent_task_id=TASK, person_id=gateway.config.user.canonical_user_id,
            tool_id="cli:github", kind="sign_in", owner_kind="person", session_key=SESSION,
            correlation_id=CHAT_REQUEST, conversation_id="conversation_link", status="waiting",
            created_at="2026-09-28T08:00:00+00:00",
        )
    )

    assert await gateway._credential_workflow.tool_setups.recover_unwatched() == 1

    [closing] = _closings(events)
    assert closing.correlation_id == "tool_setup_req_old"
    assert "gateway restarted" in closing.text


@pytest.mark.asyncio
async def test_the_persons_own_link_closes_the_task_card_without_repeating_the_link_result(chat_setup_gateway) -> None:
    gateway, broker, transport, events = chat_setup_gateway
    owner = gateway.config.user.canonical_user_id
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
    assert closing.text == "You finished the account link for http:portainer yourself, so this page is no longer needed."
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
