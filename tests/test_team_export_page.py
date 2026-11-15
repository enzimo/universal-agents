"""``/upgrade``'s export page in the real broker app, over HTTP.

Covered:

* only owners open the page (administrators and members are refused), and a
  submission re-checks the person's current standing and roles;
* the page lists what the export holds and has one passphrase form; a short
  or mismatched passphrase starts nothing and says why in red at the form;
* a valid submission leaves a request without any secret for the supervisor,
  and the page waits;
* only the operator caller collects the passphrase, once, and only for the
  export, team and owner it was typed for; the broker then holds no copy;
* the export's result ends the page, and its status route says how;
* a passphrase nobody collects is dropped after the pickup time;
* a team not started by the launcher gets a plain explanation;
* the page cannot be closed while its export runs, and Close ends it otherwise.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from access_control.caller_tokens import CALLER_GATEWAY, CALLER_MANAGER, CALLER_OPERATOR
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker import team_export_page
from secure_tool_broker.auth_flows import BrokerAuthSession
from secure_tool_broker.defaults import TEAM_EXPORT_PASSPHRASE_ROUTE, TEAM_EXPORT_RESULT_ROUTE
from secure_tool_broker.service import SecureToolBrokerService
from team_transfer.export_requests import (
    EXPORT_REQUESTS_BY_SUPERVISOR,
    EXPORT_REQUESTS_ENV,
    take_export_request,
)
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, caller_headers, gateway_headers, open_gate

PASSPHRASE = "correct horse battery staple"


@pytest.fixture(autouse=True)
def launched_team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstanceLayout:
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path))
    monkeypatch.setenv(EXPORT_REQUESTS_ENV, EXPORT_REQUESTS_BY_SUPERVISOR)
    return InstanceLayout.at(tmp_path)


@pytest_asyncio.fixture
async def broker() -> TestClient:
    app = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate()).build_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


async def _open_page(client: TestClient, roles: list[str], person: str = "usr_owner"):
    return await client.post(
        "/v1/auth/team/sessions",
        headers=gateway_headers(),
        json={
            "context": {"canonical_user_id": person, "principal_type": "user", "team_id": TEST_TEAM, "person_roles": roles},
            "method": "export",
        },
    )


async def _page_path(client: TestClient) -> str:
    created = await _open_page(client, ["owner"])
    assert created.status == 200, await created.text()
    return "/" + (await created.json())["login_url"].split("/", 3)[3]


async def _submit(client: TestClient, page: str, passphrase: str = PASSPHRASE, again: str | None = None) -> str:
    response = await client.post(
        page, data={"action": "export", "passphrase": passphrase, "passphrase_again": again if again is not None else passphrase}
    )
    assert response.status == 200
    return await response.text()


EXPORT_PROCESS_ID = 4321


async def _collect(
    client: TestClient, export_id: str, *, owner: str = "usr_owner", label: str = CALLER_OPERATOR, process_id: int = EXPORT_PROCESS_ID
):
    return await client.post(
        TEAM_EXPORT_PASSPHRASE_ROUTE,
        headers=caller_headers(label),
        json={"export_id": export_id, "team_id": TEST_TEAM, "owner_person_id": owner, "process_id": process_id},
    )


def _result(export_id: str, *, ok: bool = True, token: str = "") -> dict:
    return {
        "export_id": export_id,
        "team_id": TEST_TEAM,
        "owner_person_id": "usr_owner",
        "ok": ok,
        "collection_token": token,
        "file_name": "team-20261014-101500Z.uatransfer" if ok else "",
        "size_bytes": 48_213 if ok else 0,
        "sections": [{"name": "people", "records": 2, "size_bytes": 812, "encrypted": False}] if ok else [],
        "error": "" if ok else "The broker refused the request (500).",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("roles", "allowed"), [(["member"], False), (["admin"], False), (["owner"], True)])
async def test_only_owners_open_the_export_page(broker: TestClient, roles: list[str], allowed: bool) -> None:
    created = await _open_page(broker, roles)

    if allowed:
        body = await created.json()
        assert created.status == 200
        lifetime = datetime.fromisoformat(body["expires_at"]) - datetime.now(timezone.utc)
        assert timedelta(minutes=59) < lifetime <= timedelta(hours=1)
        return
    assert created.status == 403
    body = json.loads(await created.text())
    assert body == {"error": "Only an owner can export the team.", "reason_code": "team_export_refused"}


@pytest.mark.asyncio
async def test_the_page_lists_what_moves_and_has_one_passphrase_form(broker: TestClient) -> None:
    page = await (await broker.get(await _page_path(broker))).text()

    assert "Export the team for an upgrade" in page
    for title in ("People", "Schedules", "Knowledge pages", "Team files and agent types"):
        assert f"<li>{title}</li>" in page
    assert "<li>Stored sign-ins and keys (encrypted)</li>" in page and "<li>Secrets from team.env (encrypted)</li>" in page
    assert page.count('type="password"') == 2 and page.count("<form") == 3  # the form and two Close buttons
    assert "This page stays open for one hour." in page
    assert "never sent to the agent" in page


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("passphrase", "again", "message"),
    [
        ("too short", "too short", "at least 12 characters"),
        (PASSPHRASE, PASSPHRASE + "!", "The two passphrases are different"),
    ],
)
async def test_a_bad_passphrase_starts_nothing_and_says_why_at_the_form(
    broker: TestClient, launched_team: InstanceLayout, passphrase: str, again: str, message: str
) -> None:
    page = await _page_path(broker)

    text = await _submit(broker, page, passphrase, again)

    assert '<p class="status status-error" role="alert">Nothing was exported.' in text and message in text
    assert passphrase not in text
    assert take_export_request(launched_team.team_export_requests_dir) is None


@pytest.mark.asyncio
async def test_the_operator_collects_the_passphrase_once_and_the_result_ends_the_page(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    page = await _page_path(broker)

    waiting = await _submit(broker, page)

    assert "Exporting the team" in waiting and ">Close</button>" not in waiting
    request_files = list(launched_team.team_export_requests_dir.iterdir())
    assert len(request_files) == 1 and request_files[0].stat().st_mode & 0o777 == 0o600
    assert PASSPHRASE not in request_files[0].read_text(encoding="utf-8")
    request = take_export_request(launched_team.team_export_requests_dir)
    assert request is not None and request.owner_person_id == "usr_owner" and request.team_id == TEST_TEAM

    # Another process's token, another owner, or a second collection get nothing.
    assert (await _collect(broker, request.export_id, label=CALLER_GATEWAY)).status == 403
    assert (await _collect(broker, request.export_id, label=CALLER_MANAGER)).status == 403
    assert (await _collect(broker, request.export_id, owner="usr_member")).status == 404
    collected = await _collect(broker, request.export_id)
    body = await collected.json()
    assert collected.status == 200 and body["passphrase"] == PASSPHRASE and len(body["collection_token"]) >= 16
    token = body["collection_token"]
    assert (await _collect(broker, request.export_id)).status == 409

    status = await (await broker.get(f"{page}/status")).json()
    assert status["status"] == "exporting" and status["result"] is None
    # Close is ignored while the export runs: the page must hear how it ended.
    await broker.post(page, data={"action": "close"})
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "exporting"

    finished = await broker.post(
        TEAM_EXPORT_RESULT_ROUTE, headers=caller_headers(CALLER_OPERATOR), json=_result(request.export_id, token=token)
    )
    assert finished.status == 200

    status = await (await broker.get(f"{page}/status")).json()
    assert status["status"] == "success" and status["result"]["file_name"] == "team-20261014-101500Z.uatransfer"
    assert "collection_token" not in status["result"]
    shown = await (await broker.get(page)).text()
    assert "The team is exported" in shown and "exports/team-20261014-101500Z.uatransfer" in shown
    assert "People: 2 records, 812 bytes" in shown and "Stop this instance." in shown
    # The export is forgotten: a second result has nowhere to go.
    again = await broker.post(
        TEAM_EXPORT_RESULT_ROUTE, headers=caller_headers(CALLER_OPERATOR), json=_result(request.export_id, token=token)
    )
    assert again.status == 404


@pytest.mark.asyncio
async def test_a_failed_export_says_so_in_red_and_how_to_try_again(broker: TestClient, launched_team: InstanceLayout) -> None:
    page = await _page_path(broker)
    await _submit(broker, page)
    request = take_export_request(launched_team.team_export_requests_dir)
    assert request is not None

    await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=caller_headers(CALLER_OPERATOR), json=_result(request.export_id, ok=False))

    shown = await (await broker.get(page)).text()
    assert '<p class="status status-error" role="alert">The broker refused the request (500).' in shown
    assert "Run /upgrade in the chat to try again." in shown
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "failed"


def _session(roles: tuple[str, ...] = ("owner",)) -> BrokerAuthSession:
    now = datetime.now(timezone.utc)
    return BrokerAuthSession(
        provider="team",
        method="export",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        owner_canonical_user_id="usr_owner",
        owner_team_id=TEST_TEAM,
        owner_roles=roles,
        state={"status": "created"},
    )


def test_a_passphrase_nobody_collects_is_dropped_after_the_pickup_time(launched_team: InstanceLayout) -> None:
    flow = team_export_page.TeamExportFlow()
    session = _session()
    flow.submit(session, {"action": "export", "passphrase": PASSPHRASE, "passphrase_again": PASSPHRASE}, logging.getLogger())
    assert session.state["status"] == "exporting"

    later = datetime.now(timezone.utc) + timedelta(seconds=team_export_page.PICKUP_SECONDS + 1)
    team_export_page._EXPORTS.drop_unstarted(session, later)

    assert session.state["status"] == "failed"
    assert "No export process picked up the passphrase within five minutes" in session.state["result"]["error"]
    assert "Another export may have been running" in session.state["result"]["error"]
    assert flow.status_payload(session)["status"] == "failed"


def test_a_team_not_started_by_the_launcher_is_told_plainly(monkeypatch: pytest.MonkeyPatch, launched_team: InstanceLayout) -> None:
    monkeypatch.delenv(EXPORT_REQUESTS_ENV)
    session = _session()

    team_export_page.TeamExportFlow().submit(
        session, {"action": "export", "passphrase": PASSPHRASE, "passphrase_again": PASSPHRASE}, logging.getLogger()
    )

    assert session.state["status"] == "created"
    assert "not started by the launcher" in session.state["form_error"]
    assert take_export_request(launched_team.team_export_requests_dir) is None


class _People:
    def __init__(self, person) -> None:
        self._person = person

    def synced(self) -> bool:
        return True

    def person(self, person_id: str):
        return self._person


@pytest.mark.parametrize(
    ("standing", "roles", "message"),
    [("active", ("admin",), "Only an owner can export the team."), ("disabled", ("owner",), "not active in this team")],
)
def test_each_submission_rechecks_the_persons_current_standing_and_roles(
    standing: str, roles: tuple[str, ...], message: str, launched_team: InstanceLayout
) -> None:
    from access_control.people_directory import PersonNow
    from custom_messages.contracts.access_v2 import PersonStanding
    from secure_tool_broker.model_key_page import use_people_directory

    session = _session()
    use_people_directory(_People(PersonNow("usr_owner", PersonStanding(standing), roles)))
    try:
        team_export_page.TeamExportFlow().submit(
            session, {"action": "export", "passphrase": PASSPHRASE, "passphrase_again": PASSPHRASE}, logging.getLogger()
        )
    finally:
        use_people_directory(None)

    assert message in session.state["form_error"]
    assert take_export_request(launched_team.team_export_requests_dir) is None


@pytest.mark.asyncio
async def test_close_ends_a_page_that_is_not_exporting(broker: TestClient) -> None:
    page = await _page_path(broker)

    closed = await (await broker.post(page, data={"action": "close"})).text()

    assert "This page is closed." in closed and "window.close()" in closed
    assert (await broker.get(page)).status == 410
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "closed"


# -- review fixes (2026-11-14) ------------------------------------------------------


async def _submitted_export(client: TestClient, layout: InstanceLayout) -> tuple[str, str]:
    page = await _page_path(client)
    await _submit(client, page)
    request = take_export_request(layout.team_export_requests_dir)
    assert request is not None
    return page, request.export_id


@pytest.mark.asyncio
async def test_a_page_rebuilt_after_a_broker_restart_never_starts_an_export(launched_team: InstanceLayout) -> None:
    first = TestClient(TestServer(SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate()).build_app()))
    await first.start_server()
    closed_page, open_page = await _page_path(first), await _page_path(first)
    await first.post(closed_page, data={"action": "close"})
    assert (await first.get(closed_page)).status == 410
    await first.close()

    # The broker restarts with the same root secret: the signed links open again.
    restarted = TestClient(TestServer(SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate()).build_app()))
    await restarted.start_server()
    try:
        for page in (closed_page, open_page):
            shown = await (await restarted.post(
                page, data={"action": "export", "passphrase": PASSPHRASE, "passphrase_again": PASSPHRASE}
            )).text()
            assert "The secure broker restarted after this page opened" in shown and "Run /upgrade" in shown
            assert 'type="password"' not in shown
            status = await (await restarted.get(f"{page}/status")).json()
            assert status["status"] == "restarted"
        assert take_export_request(launched_team.team_export_requests_dir) is None
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_the_page_says_exactly_what_the_passphrase_protects(broker: TestClient) -> None:
    page = await (await broker.get(await _page_path(broker))).text()

    assert "Your passphrase encrypts the stored sign-ins, keys and secrets in the file." in page
    assert "can be read by anyone who has the file, so keep the file private" in page
    assert "one encrypted file" not in page and "nobody can open the file without it" not in page


@pytest.mark.asyncio
async def test_the_file_holds_list_follows_the_sections_the_export_writes(
    broker: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import team_transfer.registry as registry
    from team_transfer.broker_credentials import BrokerCredentialsSection
    from team_transfer.people import PeopleSection

    monkeypatch.setattr(registry, "TRANSFER_SECTIONS", (PeopleSection(), BrokerCredentialsSection()))

    page = await (await broker.get(await _page_path(broker))).text()

    assert "<li>People</li><li>Stored sign-ins and keys (encrypted)</li></ul>" in page
    assert "<li>Schedules</li>" not in page


@pytest.mark.asyncio
async def test_a_passphrase_is_never_handed_out_after_the_pickup_time_even_when_nobody_reads_the_page(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    page, export_id = await _submitted_export(broker, launched_team)
    for waiting in team_export_page._EXPORTS._by_id.values():
        waiting.requested_at -= timedelta(minutes=30)

    late = await _collect(broker, export_id)

    assert late.status == 410
    assert export_id not in team_export_page._EXPORTS._by_id
    assert (await _collect(broker, export_id)).status == 404
    status = await (await broker.get(f"{page}/status")).json()
    assert status["status"] == "failed" and "within five minutes" in status["result"]["error"]


@pytest.mark.asyncio
async def test_only_the_process_that_collected_the_passphrase_can_end_the_export(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    page, export_id = await _submitted_export(broker, launched_team)
    token = (await (await _collect(broker, export_id)).json())["collection_token"]
    operator = caller_headers(CALLER_OPERATOR)

    # A second process (a duplicated request) got 409 on collection and reports a failure.
    other = await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id, ok=False))
    wrong = await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id, ok=False, token="x" * 32))
    assert (other.status, wrong.status) == (409, 409)
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "exporting"

    real = await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id, token=token))
    assert real.status == 200
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "success"


@pytest.mark.asyncio
async def test_a_process_that_failed_before_collecting_may_report_only_a_failure(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    page, export_id = await _submitted_export(broker, launched_team)
    operator = caller_headers(CALLER_OPERATOR)

    claimed_success = await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id))
    assert claimed_success.status == 409
    failure = await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id, ok=False))
    assert failure.status == 200
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "failed"


@pytest.mark.asyncio
async def test_the_supervisor_ends_an_export_whose_process_exited_without_reporting(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    from secure_tool_broker.defaults import TEAM_EXPORT_ENDED_ROUTE

    operator = caller_headers(CALLER_OPERATOR)
    page, export_id = await _submitted_export(broker, launched_team)
    token = (await (await _collect(broker, export_id)).json())["collection_token"]
    ended = {
        "export_id": export_id, "team_id": TEST_TEAM, "owner_person_id": "usr_owner", "exit_code": -9,
        "process_id": EXPORT_PROCESS_ID,
    }

    # Only the operator caller reaches it.
    assert (await broker.post(TEAM_EXPORT_ENDED_ROUTE, headers=gateway_headers(), json=ended)).status == 403
    recorded = await broker.post(TEAM_EXPORT_ENDED_ROUTE, headers=operator, json=ended)

    assert (await recorded.json()) == {"status": "recorded"}
    status = await (await broker.get(f"{page}/status")).json()
    assert status["status"] == "failed"
    assert "The export process ended unexpectedly (exit code -9)" in status["result"]["error"]
    # A result arriving afterwards has nowhere to go.
    late = await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id, token=token))
    assert late.status == 404

    # An export that already reported keeps its own result.
    page, export_id = await _submitted_export(broker, launched_team)
    token = (await (await _collect(broker, export_id)).json())["collection_token"]
    await broker.post(TEAM_EXPORT_RESULT_ROUTE, headers=operator, json=_result(export_id, token=token))
    ignored = await broker.post(TEAM_EXPORT_ENDED_ROUTE, headers=operator, json={**ended, "export_id": export_id, "exit_code": 1})
    assert (await ignored.json()) == {"status": "ignored"}
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "success"


@pytest.mark.asyncio
async def test_a_process_that_ended_before_collecting_says_nothing_was_exported(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    from secure_tool_broker.defaults import TEAM_EXPORT_ENDED_ROUTE

    page, export_id = await _submitted_export(broker, launched_team)
    ended = {
        "export_id": export_id, "team_id": TEST_TEAM, "owner_person_id": "usr_owner", "exit_code": 2,
        "process_id": EXPORT_PROCESS_ID,
    }

    recorded = await broker.post(TEAM_EXPORT_ENDED_ROUTE, headers=caller_headers(CALLER_OPERATOR), json=ended)

    assert (await recorded.json()) == {"status": "recorded"}
    status = await (await broker.get(f"{page}/status")).json()
    assert status["status"] == "failed"
    assert "before it received the passphrase, so nothing was exported" in status["result"]["error"]
    assert not status["result"].get("unconfirmed")
    assert "The team was not exported" in await (await broker.get(page)).text()


def test_the_export_routes_sit_under_the_operator_only_transfer_prefix() -> None:
    from secure_tool_broker import defaults

    for route in (defaults.TEAM_EXPORT_PASSPHRASE_ROUTE, defaults.TEAM_EXPORT_RESULT_ROUTE, defaults.TEAM_EXPORT_ENDED_ROUTE):
        assert route.startswith(defaults.TRANSFER_ROUTE_PREFIX)


def test_a_waiting_exports_text_form_never_holds_the_passphrase() -> None:
    waiting = team_export_page._WaitingExport(
        session=None, team_id=TEST_TEAM, owner_person_id="usr_owner", requested_at=datetime.now(timezone.utc),
        passphrase=PASSPHRASE,
    )

    assert PASSPHRASE not in repr(waiting)


# -- second review (2026-11-14) ------------------------------------------------------

LONG_PASSPHRASE = "SECRETPASS-" + "q" * 1100


@pytest.mark.asyncio
async def test_an_overlong_passphrase_is_refused_at_the_form(broker: TestClient, launched_team: InstanceLayout) -> None:
    page = await _page_path(broker)

    text = await _submit(broker, page, LONG_PASSPHRASE)

    assert "Nothing was exported. Use a passphrase of at most 1,024 characters." in text
    assert "SECRETPASS" not in text
    assert take_export_request(launched_team.team_export_requests_dir) is None


def test_a_passphrase_reply_that_fails_validation_never_repeats_the_passphrase() -> None:
    from pydantic import ValidationError

    from team_transfer.export_requests import TeamExportPassphraseV1

    with pytest.raises(ValidationError) as raised:
        TeamExportPassphraseV1(passphrase=LONG_PASSPHRASE, collection_token="t" * 32)

    assert "SECRETPASS" not in str(raised.value) and "qqqq" not in str(raised.value)


@pytest.mark.asyncio
async def test_a_passphrase_that_cannot_be_handed_over_is_dropped_without_reaching_a_log(
    broker: TestClient, launched_team: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    page, export_id = await _submitted_export(broker, launched_team)
    # As if an overlong passphrase had got past the form.
    team_export_page._EXPORTS._by_id[export_id].passphrase = LONG_PASSPHRASE

    collected = await _collect(broker, export_id)

    assert collected.status == 500 and "SECRETPASS" not in await collected.text()
    assert export_id not in team_export_page._EXPORTS._by_id
    status = await (await broker.get(f"{page}/status")).json()
    assert status["status"] == "failed" and "could not be handed to the export process" in status["result"]["error"]
    assert "SECRETPASS" not in caplog.text and "event=team_export_passphrase_unusable" in caplog.text


@pytest.mark.asyncio
async def test_only_the_collecting_processs_exit_ends_a_collected_export(
    broker: TestClient, launched_team: InstanceLayout
) -> None:
    from secure_tool_broker.defaults import TEAM_EXPORT_ENDED_ROUTE

    page, export_id = await _submitted_export(broker, launched_team)
    await _collect(broker, export_id, process_id=4321)
    report = {"export_id": export_id, "team_id": TEST_TEAM, "owner_person_id": "usr_owner", "exit_code": 1}
    operator = caller_headers(CALLER_OPERATOR)

    other = await broker.post(TEAM_EXPORT_ENDED_ROUTE, headers=operator, json={**report, "process_id": 9999})
    assert (await other.json()) == {"status": "ignored"}
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "exporting"

    own = await broker.post(TEAM_EXPORT_ENDED_ROUTE, headers=operator, json={**report, "process_id": 4321})
    assert (await own.json()) == {"status": "recorded"}
    shown = await (await broker.get(page)).text()
    assert "The export's outcome is not known" in shown and "check the exports folder" in shown


@pytest.mark.asyncio
async def test_a_result_with_a_non_ascii_token_is_refused_plainly(broker: TestClient, launched_team: InstanceLayout) -> None:
    page, export_id = await _submitted_export(broker, launched_team)
    await _collect(broker, export_id)

    refused = await broker.post(
        TEAM_EXPORT_RESULT_ROUTE, headers=caller_headers(CALLER_OPERATOR), json=_result(export_id, ok=False, token="é" * 20)
    )

    assert refused.status == 400
    assert (await (await broker.get(f"{page}/status")).json())["status"] == "exporting"


def test_a_success_whose_size_and_parts_were_unreadable_shows_neither() -> None:
    from types import SimpleNamespace

    result = {
        **_result("exp_aaaaaaaaaaaaaaaaaaaaaa", ok=True),
        "size_bytes": 0,
        "sections": [],
        "note": "The file was written, but its size and parts could not be read afterwards.",
    }
    panel = team_export_page._result_panel(SimpleNamespace(state={team_export_page._RESULT: result}))

    assert "could not be read afterwards" in panel
    assert "0 bytes" not in panel and "It holds" not in panel
    assert "Next steps" in panel
