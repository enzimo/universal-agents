"""Decline on broker pages (A4 broker side, A6).

Decline is a small form of its own on each open page, posted to the page's own
address. It goes through the same one-time claim as a submission, so a page is
either answered or declined, never both. A declined sign-in whose CLI process
was still running stays declined when Decline stops that process (C01), and
nothing is stored. Forms posted from the sandboxed card (``Origin: null``) work;
a cross-site post without the private link is refused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer, unused_port

from secure_tool_broker import auth_flows
from secure_tool_broker.auth_flows import BrokerAuthSession, GoogleWebAuthFlow, get_auth_flow
from secure_tool_broker.defaults import DECLINE_FIELD
from secure_tools.broker_pages import card_view_url
from custom_messages.contracts.credentials_v1 import CREDENTIAL_DECLINED
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.providers import ToolResult
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, gateway_headers
from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    _isolated_credentials,
    link_request,
    world,
)

CHAT = "http://chat.example.test:8080"
DECLINE = {DECLINE_FIELD: "yes"}
FROM_CARD = {"Origin": "null"}
# Stands in for ``gh auth login --web``: prints the device code and waits.
# With TRAP_TERM it ends with exit code 0 when stopped, as if it had just
# finished signing in.
FAKE_GH = """#!/bin/sh
echo "! First copy your one-time code: ABCD-1234"
echo "Open this URL to continue in your web browser: https://github.com/login/device"
if [ -n "$TRAP_TERM" ]; then
  sleep 30 >/dev/null 2>&1 &
  pid=$!
  trap 'kill $pid; exit 0' TERM
  wait
  exit 0
fi
exec sleep 30
"""


def _context(roles: list[str] | None = None) -> dict:
    context: dict = {"canonical_user_id": "user-1", "principal_type": "user", "team_id": TEST_TEAM}
    if roles is not None:
        context["person_roles"] = roles
    return context


@pytest.fixture(autouse=True)
def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


@pytest_asyncio.fixture
async def broker(world):  # noqa: F811 - the imported fixture
    gate, _store = world
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate, web_chat_origin=CHAT)
    client = TestClient(TestServer(service.build_app(), port=unused_port()))
    await client.start_server()
    yield client, service
    await client.close()
    for session in list(service._auth_sessions.values()):
        flow = get_auth_flow(session.provider, session.method)
        if flow is not None:
            flow.cleanup(session)


def _path(url: str) -> str:
    return "/" + url.split("/", 3)[3]


async def _collection(client: TestClient, flow: str = "") -> dict:
    request: dict = {"credential_key": "portainer_token", "service_name": "Portainer"}
    if flow:
        request["flow"] = flow
    created = await client.post(
        "/v1/credential-collection/sessions", headers=gateway_headers(), json={"request": request, "context": _context()}
    )
    assert created.status == 200, await created.text()
    return await created.json()


async def _sign_in(client: TestClient, provider: str, method: str) -> str:
    created = await client.post(
        f"/v1/auth/{provider}/sessions", headers=gateway_headers(), json={"context": _context(), "method": method}
    )
    assert created.status == 200, await created.text()
    return _path((await created.json())["login_url"])


def _outcome(page: str) -> str | None:
    found = re.search(r'data-card-outcome="([a-z]+)"', page)
    return found.group(1) if found else None


# -- the Decline form ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_decline_is_a_form_of_its_own_with_only_its_marker(broker) -> None:
    client, _service = broker
    page = await (await client.get(card_view_url(_path((await _collection(client))["form_url"])))).text()
    declines = [form for form in re.findall(r"<form\b.*?</form>", page, re.S) if DECLINE_FIELD in form]
    assert len(declines) == 1
    assert re.findall(r"<input\b[^>]*>", declines[0]) == [f'<input type="hidden" name="{DECLINE_FIELD}" value="yes">']
    assert "Goes straight to the secure broker, not to the chat or agents." in page


@pytest.mark.asyncio
async def test_long_lived_pages_keep_their_own_close_instead_of_decline(broker) -> None:
    client, _service = broker
    for provider, method, roles in (("model", "team", ["admin"]), ("team", "export", ["owner"])):
        created = await client.post(
            f"/v1/auth/{provider}/sessions", headers=gateway_headers(), json={"context": _context(roles), "method": method}
        )
        page = await (await client.get(card_view_url(_path((await created.json())["login_url"])))).text()
        assert DECLINE_FIELD not in page
        assert ">Close</button>" in page


# -- credential collection and deletion ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["", "credential_deletion"])
async def test_a_declined_credential_form_stores_nothing_and_reports_declined(broker, monkeypatch, flow) -> None:
    client, _service = broker
    calls: list[str] = []
    monkeypatch.setattr("secure_tool_broker.service.collect_credential", lambda *a, **k: calls.append("collect"))
    monkeypatch.setattr("secure_tool_broker.service.delete_credential", lambda *a, **k: calls.append("delete"))
    session = await _collection(client, flow)
    page = card_view_url(_path(session["form_url"]))

    declined = await client.post(page, data=DECLINE, headers=FROM_CARD)
    assert declined.status == 200
    assert _outcome(await declined.text()) == "declined"
    assert _outcome(await (await client.get(page)).text()) == "declined", "a reload still says declined"

    status = await (await client.get(_path(session["status_url"]), headers=gateway_headers())).json()
    assert status["status"] == CREDENTIAL_DECLINED
    assert status["result"]["content"]["status"] == CREDENTIAL_DECLINED

    answered = await client.post(
        page, data={"field__username": "me", "field__password": "pw", "confirm_delete": "yes"}, headers=FROM_CARD
    )
    assert _outcome(await answered.text()) == "declined"
    assert calls == [], "nothing is stored or deleted after a decline"


@pytest.mark.asyncio
async def test_a_stored_credential_cannot_be_declined_afterwards(broker, monkeypatch) -> None:
    client, _service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.collect_credential",
        lambda *a, **k: ToolResult(status="success", content={"status": "credential_stored"}),
    )
    session = await _collection(client)
    page = card_view_url(_path(session["form_url"]))
    await client.post(page, data={"field__username": "me", "field__password": "pw"}, headers=FROM_CARD)

    late = await client.post(page, data=DECLINE, headers=FROM_CARD)
    assert _outcome(await late.text()) == "finished"
    status = await (await client.get(_path(session["status_url"]), headers=gateway_headers())).json()
    assert status["status"] == "credential_stored"


@pytest.mark.asyncio
async def test_a_decline_during_a_submission_is_refused(broker) -> None:
    client, service = broker
    session = await _collection(client)
    held = service._credential_collection_sessions[session["session_id"]]
    held.submission_in_progress = True
    refused = await client.post(card_view_url(_path(session["form_url"])), data=DECLINE)
    assert refused.status == 409
    assert held.status == "pending"


# -- account links -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declined_account_link_links_nothing_and_cannot_be_used_again(broker, caplog) -> None:
    client, _service = broker
    caplog.set_level(logging.INFO)
    created = await (await client.post("/v1/account-link/sessions", headers=gateway_headers(), json=link_request())).json()
    page = card_view_url(_path(created["form_url"]))

    declined = await client.post(page, data=DECLINE, headers=FROM_CARD)
    assert _outcome(await declined.text()) == "declined"
    assert "event=account_link_declined" in caplog.text
    status = await (await client.get(_path(created["status_url"]), headers=gateway_headers())).json()
    assert status["status"] == "account_link_declined"

    again = await client.post(page, data={"credential_key": "portainer_token", "confirm": "yes"})
    assert again.status == 409


# -- sign-in pages -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declined_token_form_reports_declined_and_takes_no_token(broker, monkeypatch) -> None:
    client, _service = broker
    calls: list[str] = []
    monkeypatch.setattr("secure_tool_broker.service.login_github_with_token", lambda *a, **k: calls.append("login"))
    path = await _sign_in(client, "github", "token")

    declined = await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)
    assert _outcome(await declined.text()) == "declined"
    assert (await (await client.get(f"{path}/status")).json())["status"] == "declined"
    assert _outcome(await (await client.get(card_view_url(path))).text()) == "declined"

    late = await client.post(card_view_url(path), data={"token": "ghp_x"})
    assert late.status == 410
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("finishes_on_stop", [False, True])
async def test_declining_a_running_github_sign_in_stays_declined(broker, monkeypatch, caplog, finishes_on_stop) -> None:
    """Decline stops gh; the monitor neither turns it into an error nor stores a sign-in (C01)."""

    client, service = broker
    caplog.set_level(logging.INFO)
    if finishes_on_stop:
        monkeypatch.setenv("TRAP_TERM", "1")
    finalized: list[str] = []
    monkeypatch.setattr(auth_flows, "_finalize_github_web_auth", lambda *a, **k: finalized.append("stored") or True)
    path = await _sign_in(client, "github", "web")
    assert (await client.get(card_view_url(path))).status == 200
    session = next(iter(service._auth_sessions.values()))
    assert session.process is not None and session.process.poll() is None

    declined = await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)
    assert _outcome(await declined.text()) == "declined"
    session.thread.join(timeout=10)
    assert not session.thread.is_alive()

    payload = await (await client.get(f"{path}/status", headers=FROM_CARD)).json()
    assert payload["status"] == "declined"
    assert payload["failure_reason"] is None
    assert finalized == []
    assert "event=broker_auth_process_failed" not in caplog.text
    # gh that ended with 0 as it was stopped may keep its own sign-in: the log says so.
    expected_event = "broker_auth_signed_in_after_decline" if finishes_on_stop else "broker_auth_process_ended_after_decline"
    assert f"event={expected_event}" in caplog.text


def test_declining_a_running_google_sign_in_stays_declined(tmp_path: Path, caplog) -> None:
    """The same through the real monitor for gog, whose private home Decline removes."""

    caplog.set_level(logging.INFO)
    gog_home = tmp_path / "gog-home"
    gog_home.mkdir()
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        owner_canonical_user_id="user-1",
        state={"status": "pending", "gog_home": str(gog_home)},
    )
    session.process = subprocess.Popen(
        ["sh", "-c", "echo 'Visit the URL'; exec sleep 30"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=0,
    )
    logger = logging.getLogger("test.google_decline")
    thread = threading.Thread(
        target=auth_flows._monitor_web_auth_process,
        kwargs={"session": session, "timeout_seconds": 60, "logger": logger},
    )
    thread.start()

    session.state["status"] = "declined"
    session.used = True
    GoogleWebAuthFlow().cleanup(session)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert session.state["status"] == "declined"
    assert "failure_reason" not in session.state
    assert not gog_home.exists()
    assert "event=broker_auth_process_failed" not in caplog.text


# -- forged and card submissions -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/credential-collection/forged", "/account-link/forged", "/auth/github/v1.forged.link"]
)
async def test_a_cross_site_post_without_the_private_link_is_refused(broker, path) -> None:
    client, _service = broker
    for headers in ({"Origin": "https://evil.example"}, FROM_CARD):
        forged = await client.post(path, data={**DECLINE, "token": "x"}, headers=headers)
        assert forged.status == 404


# -- Decline only while a page is open and offers it (C15, C16) -----------------------


async def _running_github_sign_in(client: TestClient, service: SecureToolBrokerService) -> tuple[str, BrokerAuthSession]:
    path = await _sign_in(client, "github", "web")
    assert (await client.get(card_view_url(path))).status == 200
    return path, next(iter(service._auth_sessions.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", ["success", "error"])
async def test_a_stale_decline_never_changes_an_ended_sign_in(broker, caplog, ended) -> None:
    """A second view (the card and its own tab) may still show Decline after the sign-in ended."""

    client, service = broker
    caplog.set_level(logging.INFO)
    path, session = await _running_github_sign_in(client, service)
    session.state["status"] = ended

    answer = await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)
    assert answer.status == 200
    assert _outcome(await answer.text()) == ("finished" if ended == "success" else None)
    assert (await (await client.get(f"{path}/status")).json())["status"] == ended
    assert not session.used
    assert "event=broker_auth_decline_ignored" in caplog.text
    assert "reason=page_already_ended" in caplog.text


@pytest.mark.asyncio
async def test_a_decline_after_the_sign_in_process_finished_reports_the_real_result(broker, caplog) -> None:
    client, service = broker
    caplog.set_level(logging.INFO)
    path, session = await _running_github_sign_in(client, service)
    session.process.terminate()  # stands in for gh having exited...
    session.process.wait()
    session.thread.join(timeout=10)
    finished = subprocess.Popen(["true"])  # ...with exit code 0
    finished.wait()
    session.process = finished
    session.state["status"] = "pending"

    await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)
    assert session.state["status"] != "declined"
    assert not session.used
    assert "reason=sign_in_already_finished" in caplog.text


@pytest.mark.asyncio
async def test_the_pages_with_their_own_close_ignore_a_decline(broker) -> None:
    from secure_tool_broker import team_export_page

    client, service = broker
    keys = await client.post(
        "/v1/auth/model/sessions", headers=gateway_headers(), json={"context": _context(["admin"]), "method": "team"}
    )
    export = await client.post(
        "/v1/auth/team/sessions", headers=gateway_headers(), json={"context": _context(["owner"]), "method": "export"}
    )
    for created in (keys, export):
        path = _path((await created.json())["login_url"])
        session = service._auth_sessions[path.rsplit("/", 1)[1]]
        if session.provider == "team":
            # An export is running: the page must stay to hear how it ends.
            session.state[team_export_page._STATUS] = team_export_page.STATUS_EXPORTING
        before = dict(session.state)
        answer = await client.post(card_view_url(path), data=DECLINE)
        assert answer.status == 200
        assert _outcome(await answer.text()) is None
        assert session.state == before
        assert not session.used


def test_a_decline_while_the_sign_in_is_being_stored_keeps_the_page_declined(monkeypatch, caplog) -> None:
    """The process exited 0 and the broker is storing the sign-in when Decline arrives."""

    caplog.set_level(logging.INFO)
    now = datetime.now(timezone.utc)
    session = BrokerAuthSession(
        provider="github",
        method="web",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        owner_canonical_user_id="user-1",
        state={"status": "pending"},
    )

    def finalize_while_declined(held: BrokerAuthSession, _logger) -> bool:
        held.state["status"] = "declined"  # Decline lands during the store
        return True

    monkeypatch.setattr(auth_flows, "_finalize_github_web_auth", finalize_while_declined)
    session.process = subprocess.Popen(
        ["sh", "-c", "exit 0"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    auth_flows._monitor_web_auth_process(session=session, timeout_seconds=30, logger=logging.getLogger("test"))

    assert session.state["status"] == "declined"
    assert "event=broker_auth_signed_in_after_decline" in caplog.text


# -- the result is recorded before the claim is released (review finding F4) -----------


def _decline_right_after_release(monkeypatch: pytest.MonkeyPatch, decline) -> dict:
    """Run ``decline(session)`` in the loop step right after a submission releases its claim.

    That is where a Decline whose handler was waiting on ``request.post()``
    resumes; before the fix the submission had not recorded its result yet.
    """

    from secure_tool_broker import form_claims

    seen: dict = {}
    release = form_claims.finish_form_submission

    def release_then_decline(session) -> None:
        release(session)
        if seen:
            return

        def decline_now() -> None:
            try:
                decline(session)
                seen["decline"] = "accepted"
            except Exception as exc:  # noqa: BLE001 - the refusal is what the test looks for
                seen["decline"] = f"refused {type(exc).__name__}"

        seen["scheduled"] = True
        asyncio.get_running_loop().call_soon(decline_now)

    monkeypatch.setattr(form_claims, "finish_form_submission", release_then_decline)
    return seen


@pytest.mark.asyncio
async def test_a_decline_just_after_a_stored_credential_is_refused(broker, monkeypatch) -> None:
    from secure_tool_broker.page_decline import decline_credential_collection

    client, service = broker
    stored: list[dict] = []

    def collect(_payload, fields, **_kwargs) -> ToolResult:
        stored.append(dict(fields))
        return ToolResult(status="success", content={"status": "credential_stored"})

    monkeypatch.setattr("secure_tool_broker.service.collect_credential", collect)
    created = await _collection(client)
    seen = _decline_right_after_release(
        monkeypatch,
        lambda session: decline_credential_collection(
            created["session_id"], session, prune_expired=lambda: None, logger=logging.getLogger("test")
        ),
    )

    answer = await client.post(
        card_view_url(_path(created["form_url"])), data={"field__username": "me", "field__password": "pw"}, headers=FROM_CARD
    )
    await asyncio.sleep(0)
    status = await (await client.get(_path(created["status_url"]), headers=gateway_headers())).json()
    reopened = await client.get(card_view_url(_path(created["form_url"])))

    assert seen["decline"].startswith("refused")
    assert stored == [{"username": "me", "password": "pw"}]
    assert status["status"] == "credential_stored"
    assert _outcome(await answer.text()) == "finished"
    assert _outcome(await reopened.text()) == "finished"


@pytest.mark.asyncio
async def test_a_decline_just_after_a_token_sign_in_is_refused(broker, monkeypatch) -> None:
    from secure_tool_broker.page_decline import decline_auth_session

    client, service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.login_github_with_token", lambda *a, **k: ToolResult(status="success", content={})
    )
    path = await _sign_in(client, "github", "token")
    session = next(iter(service._auth_sessions.values()))

    def decline(held) -> None:
        # With no flow, decline_auth_session never waits: it runs to its end in this step.
        coroutine = decline_auth_session("sid", held, None, prune_expired=lambda: None, logger=logging.getLogger("test"))
        try:
            coroutine.send(None)
        except StopIteration:
            return

    seen = _decline_right_after_release(monkeypatch, decline)

    answer = await client.post(card_view_url(path), data={"token": "ghp_x"}, headers=FROM_CARD)
    await asyncio.sleep(0)

    assert seen["decline"].startswith("refused")
    assert session.state["status"] == "success" and session.used
    assert (await (await client.get(f"{path}/status")).json())["status"] == "success"
    assert _outcome(await answer.text()) == "finished"


def _bare_sign_in() -> BrokerAuthSession:
    now = datetime.now(timezone.utc)
    return BrokerAuthSession(
        provider="github", method="token", created_at=now, expires_at=now + timedelta(minutes=5),
        submission_in_progress=True,
    )


@pytest.mark.asyncio
async def test_the_result_is_recorded_while_the_claim_is_still_held() -> None:
    from secure_tool_broker.form_claims import run_claimed_form_thread

    session = _bare_sign_in()
    held_while_recording: list[bool] = []

    def record(result: str) -> None:
        held_while_recording.append(session.submission_in_progress)
        session.state["status"] = result

    assert await run_claimed_form_thread(session, lambda: "success", on_result=record) == "success"
    await asyncio.sleep(0)

    assert held_while_recording == [True]
    assert session.state["status"] == "success" and not session.submission_in_progress


@pytest.mark.asyncio
async def test_the_result_is_recorded_when_the_browser_went_away() -> None:
    from secure_tool_broker.form_claims import run_claimed_form_thread

    session = _bare_sign_in()
    worker_may_finish = threading.Event()
    recorded = asyncio.Event()

    def work() -> str:
        worker_may_finish.wait(5)
        return "success"

    def record(result: str) -> None:
        session.state["status"] = result
        recorded.set()

    handler = asyncio.ensure_future(run_claimed_form_thread(session, work, on_result=record))
    await asyncio.sleep(0.05)
    handler.cancel()  # the browser disconnected
    worker_may_finish.set()
    await asyncio.wait_for(recorded.wait(), 5)
    await asyncio.sleep(0)

    assert session.state["status"] == "success" and not session.submission_in_progress


@pytest.mark.asyncio
async def test_a_failed_worker_records_nothing_and_releases_the_claim() -> None:
    from secure_tool_broker.form_claims import run_claimed_form_thread

    session = _bare_sign_in()
    calls: list[object] = []

    def fail() -> None:
        raise RuntimeError("store unavailable")

    with pytest.raises(RuntimeError):
        await run_claimed_form_thread(session, fail, on_result=calls.append)
    await asyncio.sleep(0)

    assert calls == [] and not session.submission_in_progress and not session.used


@pytest.mark.asyncio
async def test_a_result_that_cannot_be_recorded_still_ends_the_page(caplog) -> None:
    from secure_tool_broker.form_claims import run_claimed_form_thread

    session = _bare_sign_in()

    def broken(_result: object) -> None:
        raise KeyError("status")

    caplog.set_level(logging.ERROR)
    await run_claimed_form_thread(session, lambda: "success", on_result=broken, log_ref="auth_github abc123")
    await asyncio.sleep(0)

    assert session.used and session.state["status"] == "error" and not session.submission_in_progress
    assert "event=broker_form_result_record_failed page=auth_github abc123" in caplog.text
