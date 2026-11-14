"""``/upgrade`` in the gateway: who may run it, the card it returns, and the chat message.

End to end with the real broker: an owner's ``/upgrade`` opens the export
page, the browser submits a passphrase, the export process (played here by its
two operator calls) collects it and reports the result, and the gateway posts
one message in the chat where ``/upgrade`` ran, with the file, what it holds
and the next steps. Also covered: members and administrators who are not
owners are refused, a team not started by the launcher is pointed to the host
command, every way a page can end has its own message, the command service
routes ``/upgrade``, and Web Chat keeps the page link out of history.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from access_control.caller_tokens import CALLER_GATEWAY, CALLER_OPERATOR
from assistant_gateway.application.broker_session_watch import (
    WATCH_EXPIRED,
    WATCH_FINISHED,
    WATCH_UNCONFIRMED,
    BrokerSessionOutcome,
)
from assistant_gateway.application.command_help import WEB_CHAT_COMMAND_GUIDE, build_interactive_session_help
from assistant_gateway.application.commands import (
    TeamExportCommandHandler,
    TeamExportCommandRequest,
)
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.application.link_results import GatewayLinkResults
from assistant_gateway.application.team_exports import NOT_LAUNCHED_TEXT, team_export_result_text
from assistant_gateway.domain.message_types import LINK_RESULT_MESSAGE_TYPE, TEAM_EXPORT_COMMAND_MESSAGE_TYPE
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.interactive_apps.website_handlers.conversation import WebsiteConversationHandlers
from assistant_gateway.interactive_apps.website_support import _WEB_CHAT_ALLOWED_COMMANDS
from assistant_gateway.runtime_ports import SystemGatewayClock
from secure_tool_broker.service import SecureToolBrokerService
from team_transfer.export_requests import (
    EXPORT_REQUESTS_BY_SUPERVISOR,
    EXPORT_REQUESTS_ENV,
    TEAM_EXPORT_PASSPHRASE_ROUTE,
    TEAM_EXPORT_RESULT_ROUTE,
    take_export_request,
)
from runtime_settings.instance_layout import InstanceLayout
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, caller_headers, caller_token, open_gate

PASSPHRASE = "correct horse battery staple"


class _Identity:
    def __init__(self, roles: tuple[str, ...] = ("owner", "admin"), person: str = "usr_owner") -> None:
        self.roles = roles
        self.person = person

    def canonical_user_id_for_context(self, **_context: str | None) -> str:
        return self.person

    def team_roles_for_person(self, canonical_user_id: str) -> tuple[str, ...]:
        return self.roles


class _RecordingBroker:
    auth_unavailable_message = None

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def user_form_url(self, internal_url: str) -> str:
        return internal_url.replace("broker.internal", "agents.example.test")

    async def create_auth_session(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return {"method": "export", "login_url": "https://broker.internal/auth/team/v1.signed", "expires_at": "2026-10-14T11:00:00+00:00"}


class _Watches:
    def __init__(self) -> None:
        self.exports: list[dict[str, object]] = []

    def watch_team_export(self, **request: object) -> None:
        self.exports.append(request)


def _request(command: str = "/upgrade", session_key: str | None = "session_web") -> TeamExportCommandRequest:
    return TeamExportCommandRequest(
        command=command,
        actor=GatewayActorContext(channel_name="website", user_id="usr_owner", chat_id="web"),
        session_key=session_key,
        correlation_id="web_req_1",
    )


def _handler(identity: _Identity, broker: _RecordingBroker, watches: _Watches, *, supervised: bool = True):
    return TeamExportCommandHandler(
        identity=identity,
        secure_broker=broker,  # type: ignore[arg-type]
        team_id="team_ua",
        link_results=watches,  # type: ignore[arg-type]
        supervised=lambda: supervised,
    )


@pytest.mark.asyncio
async def test_an_owner_gets_the_export_page_card_and_the_gateway_watches_it() -> None:
    broker, watches = _RecordingBroker(), _Watches()

    action = await _handler(_Identity(), broker, watches).execute(_request())

    assert broker.requests == [
        {
            "provider": "team",
            "method": "export",
            "context": {
                "canonical_user_id": "usr_owner",
                "team_id": "team_ua",
                "principal_type": "user",
                "person_roles": ["owner", "admin"],
            },
            "options": None,
        }
    ]
    assert action.kind == "broker_authentication" and action.label == "Open"
    assert action.url == "https://agents.example.test/auth/team/v1.signed" and action.url not in action.description
    assert action.description.startswith("Team export page opened.") and "not sent to the agent" in action.description
    assert action.receipt == "Team export page opened."
    [watch] = watches.exports
    assert watch["target"].session_key == "session_web" and watch["target"].correlation_id == "web_req_1"
    assert watch["page"].status_url == "https://broker.internal/auth/team/v1.signed/status"


@pytest.mark.asyncio
@pytest.mark.parametrize("roles", [("member",), ("admin",), ()])
async def test_people_who_are_not_owners_are_refused(roles: tuple[str, ...]) -> None:
    broker, watches = _RecordingBroker(), _Watches()

    result = await _handler(_Identity(roles), broker, watches).execute(_request())

    assert result == "Only an owner can export the team."
    assert broker.requests == [] and watches.exports == []


@pytest.mark.asyncio
async def test_a_team_not_started_by_the_launcher_is_pointed_to_the_host_command() -> None:
    broker = _RecordingBroker()

    result = await _handler(_Identity(), broker, _Watches(), supervised=False).execute(_request())

    assert result == NOT_LAUNCHED_TEXT and "assistant_cli team export" in result
    assert broker.requests == []


@pytest.mark.asyncio
async def test_extra_words_get_the_usage_and_unknown_people_are_refused() -> None:
    broker = _RecordingBroker()

    assert await _handler(_Identity(), broker, _Watches()).execute(_request("/upgrade now")) == "Usage: /upgrade"
    unknown = await _handler(_Identity(person=""), broker, _Watches()).execute(_request())
    assert unknown == "Only people registered in this team can export it."
    assert broker.requests == []


def _outcome(ending: str, status: str = "", result: dict | None = None) -> BrokerSessionOutcome:
    return BrokerSessionOutcome(ending=ending, payload={"status": status, "result": result})  # type: ignore[arg-type]


_SUCCESS = {
    "export_id": "exp_aaaaaaaaaaaaaaaaaaaaaa",
    "team_id": "team_ua",
    "owner_person_id": "usr_owner",
    "ok": True,
    "file_name": "alpha-20261014-101500Z.uatransfer",
    "size_bytes": 3_355_443,
    "sections": [
        {"name": "people", "records": 3, "size_bytes": 4_096},
        {"name": "broker_credentials", "records": 2, "size_bytes": 1_000, "encrypted": True},
    ],
}


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            _outcome(WATCH_FINISHED, "success", _SUCCESS),
            "Exported the team to exports/alpha-20261014-101500Z.uatransfer in this instance's data folder (3.2 MB). "
            "It holds:\n- People: 3 records, 4 KB\n- Stored sign-ins and keys: 2 records, 1000 bytes (encrypted)\n\n"
            "Next steps:\n"
            "1. Stop this instance. Its chat bot tokens and model sign-ins move with the file, and two running "
            "instances must not use them at once.\n"
            "2. Start the new version with a blank data folder.\n"
            "3. Copy the file into the new data folder's import folder, then choose Import a team export on the "
            "Let's Begin page.",
        ),
        (
            _outcome(WATCH_FINISHED, "failed", {"ok": False, "error": "The broker refused the request (500)."}),
            "The team was not exported. The broker refused the request (500). Run /upgrade to try again.",
        ),
        (_outcome(WATCH_FINISHED, "closed"), "The team export page was closed without exporting."),
        (
            _outcome(WATCH_EXPIRED, "created"),
            "The team export page expired without reporting an export. If you started one, look in the exports "
            "folder of this instance's data folder; otherwise run /upgrade again.",
        ),
        (
            _outcome(WATCH_UNCONFIRMED),
            "Couldn't confirm whether the team export finished. Look in the exports folder of this instance's data folder.",
        ),
    ],
)
def test_every_ending_has_its_own_message(outcome: BrokerSessionOutcome, expected: str) -> None:
    assert team_export_result_text(outcome) == expected


@pytest.mark.asyncio
async def test_the_chat_hears_how_the_export_ended_end_to_end_with_the_real_broker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setenv(EXPORT_REQUESTS_ENV, EXPORT_REQUESTS_BY_SUPERVISOR)
    server = TestServer(SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate()).build_app())
    await server.start_server()
    events: list = []

    class _Output:
        async def emit(self, event) -> None:
            events.append(event)

    class _Sessions:
        def get_session(self, session_key: str):
            return SimpleNamespace(
                session_key=session_key, channel_name="telegram", user_id="tg_1", chat_id="4242",
                thread_id=None, conversation_id="conversation_tg",
            )

    try:
        client = HttpGatewaySecureBroker(
            internal_base_url=str(server.make_url("")).rstrip("/"), public_base_url=None, token=caller_token(CALLER_GATEWAY)
        )
        results = GatewayLinkResults(
            broker=client, sessions=_Sessions(), event_output=_Output(), clock=SystemGatewayClock(), poll_interval_seconds=0.01  # type: ignore[arg-type]
        )
        handler = TeamExportCommandHandler(
            identity=_Identity(), secure_broker=client, team_id=TEST_TEAM, link_results=results, supervised=lambda: True
        )
        action = await handler.execute(
            TeamExportCommandRequest(
                command="/upgrade",
                actor=GatewayActorContext(channel_name="telegram", user_id="tg_1", chat_id="4242"),
                session_key="session_tg",
                correlation_id="upgrade_cmd_tg",
            )
        )
        async with aiohttp.ClientSession() as browser:
            submitted = await browser.post(
                action.url, data={"action": "export", "passphrase": PASSPHRASE, "passphrase_again": PASSPHRASE}
            )
            assert "Exporting the team" in await submitted.text()
            # The export process: collect the passphrase, then report the result.
            request = take_export_request(InstanceLayout.at(tmp_path).team_export_requests_dir)
            assert request is not None
            operator = caller_headers(CALLER_OPERATOR)
            identity = {"export_id": request.export_id, "team_id": TEST_TEAM, "owner_person_id": "usr_owner"}
            collected = await browser.post(server.make_url(TEAM_EXPORT_PASSPHRASE_ROUTE), headers=operator, json=identity)
            assert (await collected.json()) == {"passphrase": PASSPHRASE}
            reported = await browser.post(
                server.make_url(TEAM_EXPORT_RESULT_ROUTE),
                headers=operator,
                json={**_SUCCESS, **identity},
            )
            assert reported.status == 200
        for _ in range(500):
            if events:
                break
            await asyncio.sleep(0.01)
        await results.close()
    finally:
        await server.close()

    [event] = events
    assert event.message_type == LINK_RESULT_MESSAGE_TYPE and event.correlation_id == "upgrade_cmd_tg"
    assert (event.channel_name, event.chat_id) == ("telegram", "4242")
    assert event.text.startswith("Exported the team to exports/alpha-20261014-101500Z.uatransfer")
    assert PASSPHRASE not in event.text and action.url not in event.text


def test_upgrade_is_listed_in_help_and_kept_out_of_web_chat_history() -> None:
    assert "/upgrade" in _WEB_CHAT_ALLOWED_COMMANDS
    assert any(item.syntax == "/upgrade" for item in WEB_CHAT_COMMAND_GUIDE)
    assert "/upgrade - export the whole team" in build_interactive_session_help(include_reply=False)
    assert WebsiteConversationHandlers._command_message_type("/upgrade") == TEAM_EXPORT_COMMAND_MESSAGE_TYPE


@pytest.mark.asyncio
async def test_the_command_service_routes_upgrade() -> None:
    from assistant_gateway.application.interactive_commands import _run_team_export

    class _Commands:
        def __init__(self) -> None:
            self.requests: list[TeamExportCommandRequest] = []

        async def execute(self, request: TeamExportCommandRequest) -> str:
            self.requests.append(request)
            return "opened"

    commands = _Commands()
    request = SimpleNamespace(
        channel_name="telegram", user_id="tg_1", chat_id="4242", thread_id=None, canonical_user_id="usr_owner",
        current_session_key="session_tg", correlation_id="req_1",
    )

    assert await _run_team_export(commands, request, "/upgrade") == "opened"  # type: ignore[arg-type]
    assert commands.requests[0].session_key == "session_tg" and commands.requests[0].actor.canonical_user_id == "usr_owner"
    assert await _run_team_export(None, request, "/upgrade") == "Team export is not available in this chat."  # type: ignore[arg-type]
