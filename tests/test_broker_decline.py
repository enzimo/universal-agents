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
# finished signing in. With WRITE_HOSTS it first writes its sign-in file, as gh
# does once GitHub answered, and is then stopped before it exits (F9).
FAKE_GH = """#!/bin/sh
echo "! First copy your one-time code: ABCD-1234"
echo "Open this URL to continue in your web browser: https://github.com/login/device"
if [ -n "$WRITE_HOSTS" ]; then
  printf 'github.com:\\n    oauth_token: gho_probe_value\\n' > "$GH_CONFIG_DIR/hosts.yml"
fi
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

    # A token sent after the Decline is not taken; the page still shows it was declined.
    late = await client.post(card_view_url(path), data={"token": "ghp_x"})
    assert late.status == 200 and _outcome(await late.text()) == "declined"
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
    # gh that ended with 0 as it was stopped may keep its own sign-in: the page and card say so (F9).
    page = await declined.text()
    assert _outcome(page) == ("finished" if finishes_on_stop else "declined")
    assert ("Nothing was saved" in page) is not finishes_on_stop
    session.thread.join(timeout=10)
    assert not session.thread.is_alive()

    payload = await (await client.get(f"{path}/status", headers=FROM_CARD)).json()
    assert payload["status"] == "declined"
    assert payload["sign_in_may_remain"] is finishes_on_stop
    assert payload["failure_reason"] is None
    assert finalized == []
    assert "event=broker_auth_process_failed" not in caplog.text
    expected_event = "broker_auth_signed_in_after_decline" if finishes_on_stop else "broker_auth_process_ended_after_decline"
    other_event = "broker_auth_process_ended_after_decline" if finishes_on_stop else "broker_auth_signed_in_after_decline"
    assert f"event={expected_event}" in caplog.text and f"event={other_event}" not in caplog.text


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


# -- a sign-in page that ended shows its ending, not an error (review finding F3) ---------


@pytest.mark.asyncio
async def test_a_token_sign_in_that_finished_shows_it_in_the_other_view(broker, monkeypatch) -> None:
    client, service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.login_github_with_token", lambda *a, **k: ToolResult(status="success", content={})
    )
    path = await _sign_in(client, "github", "token")
    await client.post(path, data={"token": "ghp_x"})  # finished in its own tab

    reopened = await client.get(card_view_url(path))  # the card, rebuilt after a thread switch
    stale = await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)  # its Decline, no longer current

    assert reopened.status == 200 and _outcome(await reopened.text()) == "finished"
    assert stale.status == 200 and _outcome(await stale.text()) == "finished"
    assert next(iter(service._auth_sessions.values())).state["status"] == "success"


@pytest.mark.asyncio
async def test_a_token_sign_in_declined_in_its_own_tab_shows_declined_in_the_card(broker) -> None:
    client, _service = broker
    path = await _sign_in(client, "github", "token")
    await client.post(path, data=DECLINE)  # own tab

    reopened = await client.get(card_view_url(path))
    second = await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)

    assert reopened.status == 200 and _outcome(await reopened.text()) == "declined"
    assert second.status == 200 and _outcome(await second.text()) == "declined"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "method", "roles", "patched", "fields", "command"),
    [
        ("github", "token", None, "login_github_with_token", {"token": "ghp_x"}, "/link cli:github"),
        # A wrong master password: the ordinary Bitwarden failure (critique C09).
        ("bitwarden", "form", None, "login_bitwarden_with_api_key", {"master_password": "wrong"}, "/auth bitwarden"),
        ("google", "team_client", ["admin"], "store_team_client", {"credentials_json": "{}"}, "/auth google team_client"),
    ],
)
async def test_a_token_form_that_failed_says_so_when_opened_again(
    broker, monkeypatch, provider, method, roles, patched, fields, command
) -> None:
    client, _service = broker
    monkeypatch.setattr(
        f"secure_tool_broker.service.{patched}", lambda *a, **k: ToolResult(status="error", content={}, error="refused")
    )
    created = await client.post(
        f"/v1/auth/{provider}/sessions", headers=gateway_headers(), json={"context": _context(roles), "method": method}
    )
    assert created.status == 200, await created.text()
    path = _path((await created.json())["login_url"])
    assert (await client.post(path, data=fields)).status == 400

    for answer in (
        await client.get(card_view_url(path)),
        await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD),
    ):
        page = await answer.text()
        assert answer.status == 200
        assert "This page did not finish." in page and f"<code>{command}</code>" in page
        assert "Nothing was stored" not in page
        assert _outcome(page) is None and "<form method=\"post\">" not in page


@pytest.mark.asyncio
async def test_a_saved_bitwarden_restart_unlock_is_recorded_before_the_claim_ends(broker, monkeypatch) -> None:
    """What a later view shows is part of the result (critique C10)."""

    from secure_tool_broker import form_claims
    from secure_tool_broker.ended_pages import BITWARDEN_RESTART_UNLOCK_STATE

    client, service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.save_bitwarden_restart_unlock_request",
        lambda **_k: ToolResult(
            status="success",
            content={"action": "pending_restart_unlock.saved", "pending_file": "pending-unlock.json", "expires_at_epoch": 0},
        ),
    )
    at_release: list[object] = []
    release = form_claims.finish_form_submission

    def watch_release(session) -> None:
        at_release.append(session.state.get(BITWARDEN_RESTART_UNLOCK_STATE))
        release(session)

    monkeypatch.setattr(form_claims, "finish_form_submission", watch_release)
    path = await _sign_in(client, "bitwarden", "form")
    answer = await (await client.post(path, data={"master_password": "pw", "submit_mode": "defer_restart"})).text()

    assert at_release and isinstance(at_release[0], dict) and at_release[0]["pending_file"] == "pending-unlock.json"
    assert "Bitwarden restart unlock saved." in answer and "pending-unlock.json" in answer


@pytest.mark.asyncio
async def test_the_team_google_app_and_a_bitwarden_restart_unlock_show_their_own_ending(broker, monkeypatch) -> None:
    client, _service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.store_team_client", lambda *a, **k: ToolResult(status="success", content={})
    )
    monkeypatch.setattr(
        "secure_tool_broker.service.save_bitwarden_restart_unlock_request",
        lambda **_k: ToolResult(
            status="success",
            content={"action": "pending_restart_unlock.saved", "pending_file": "pending-unlock.json", "expires_at_epoch": 0},
        ),
    )
    created = await client.post(
        "/v1/auth/google/sessions", headers=gateway_headers(), json={"context": _context(["admin"]), "method": "team_client"}
    )
    google = _path((await created.json())["login_url"])
    await client.post(google, data={"credentials_json": "{}"})
    bitwarden = await _sign_in(client, "bitwarden", "form")
    await client.post(bitwarden, data={"master_password": "pw", "submit_mode": "defer_restart"})

    google_page = await (await client.get(card_view_url(google))).text()
    bitwarden_page = await (await client.get(card_view_url(bitwarden))).text()

    assert "The team's Google sign-in app is stored." in google_page and _outcome(google_page) == "finished"
    # Still no outcome: the person has to restart the broker, and only this page says so.
    assert "Bitwarden restart unlock saved." in bitwarden_page and "pending-unlock.json" in bitwarden_page
    assert _outcome(bitwarden_page) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "method", "roles", "outcome"),
    [("model", "team", ["admin"], "finished"), ("team", "export", ["owner"], "declined")],
)
async def test_a_page_closed_in_its_own_tab_shows_closed_in_the_card(broker, provider, method, roles, outcome) -> None:
    client, _service = broker
    created = await client.post(
        f"/v1/auth/{provider}/sessions", headers=gateway_headers(), json={"context": _context(roles), "method": method}
    )
    page = _path((await created.json())["login_url"])
    opened = await (await client.get(page)).text()
    form_id = re.search(r'name="form_id" value="([^"]*)"', opened)
    await client.post(page, data={"action": "close", **({"form_id": form_id.group(1)} if form_id else {})})

    reopened = await client.get(card_view_url(page))
    stale = await client.post(card_view_url(page), data={"action": "save"}, headers=FROM_CARD)

    for answer in (reopened, stale):
        text = await answer.text()
        assert answer.status == 200 and "This page is closed." in text and _outcome(text) == outcome


# A Decline whose request body is still arriving while the other view's submission finishes
# (critique C08): it is not applied, and its view shows the finished ending, not an error.
_FORM_FROM_CARD = {"Content-Type": "application/x-www-form-urlencoded", **FROM_CARD}


async def _decline_arriving_during(client: TestClient, path: str, submit):
    body_may_go = asyncio.Event()

    async def body():
        whole = f"{DECLINE_FIELD}=yes".encode()
        yield whole[:3]  # the handler starts and passes its checks, then waits for the rest
        await body_may_go.wait()
        yield whole[3:]

    decline = asyncio.ensure_future(client.post(path, data=body(), headers=_FORM_FROM_CARD))
    await asyncio.sleep(0.3)
    answer = await submit()
    body_may_go.set()
    return answer, await decline


@pytest.mark.asyncio
async def test_a_decline_still_arriving_when_a_credential_is_stored_shows_the_stored_ending(broker, monkeypatch) -> None:
    client, _service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.collect_credential",
        lambda *_a, **_k: ToolResult(status="success", content={"status": "credential_stored"}),
    )
    created = await _collection(client)
    path = card_view_url(_path(created["form_url"]))

    answer, late = await _decline_arriving_during(
        client, path, lambda: client.post(path, data={"field__username": "me", "field__password": "pw"}, headers=FROM_CARD)
    )
    status = await (await client.get(_path(created["status_url"]), headers=gateway_headers())).json()

    assert _outcome(await answer.text()) == "finished"
    assert late.status == 200 and _outcome(await late.text()) == "finished"
    assert status["status"] == "credential_stored"


@pytest.mark.asyncio
async def test_a_decline_still_arriving_when_a_token_sign_in_finishes_shows_the_finished_ending(broker, monkeypatch) -> None:
    client, service = broker
    monkeypatch.setattr(
        "secure_tool_broker.service.login_github_with_token", lambda *a, **k: ToolResult(status="success", content={})
    )
    path = card_view_url(await _sign_in(client, "github", "token"))

    answer, late = await _decline_arriving_during(
        client, path, lambda: client.post(path, data={"token": "ghp_x"}, headers=FROM_CARD)
    )

    assert _outcome(await answer.text()) == "finished"
    assert late.status == 200 and _outcome(await late.text()) == "finished"
    assert next(iter(service._auth_sessions.values())).state["status"] == "success"


# -- a declined gh sign-in that may have left gh signed in (review finding F9) -----------


@pytest.mark.asyncio
async def test_a_decline_after_gh_wrote_its_sign_in_says_a_sign_in_may_remain(broker, monkeypatch, caplog) -> None:
    """gh wrote hosts.yml and was stopped before it exited (exit -15): nothing claims nothing was saved."""

    client, service = broker
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("WRITE_HOSTS", "1")
    path = await _sign_in(client, "github", "web")
    assert (await client.get(card_view_url(path))).status == 200
    session = next(iter(service._auth_sessions.values()))
    hosts = Path(session.state["gh_hosts_file"])
    for _ in range(100):  # gh writes its file right after printing the code
        if hosts.exists():
            break
        await asyncio.sleep(0.05)
    assert hosts.exists() and session.process.poll() is None

    declined = await client.post(card_view_url(path), data=DECLINE, headers=FROM_CARD)
    session.thread.join(timeout=10)
    page = await declined.text()
    reopened = await (await client.get(card_view_url(path))).text()
    payload = await (await client.get(f"{path}/status", headers=FROM_CARD)).json()

    assert session.process.returncode != 0  # stopped, not finished
    for shown in (page, reopened):
        assert "may have finished signing in just before you declined" in shown and "/auth status" in shown
        assert "Nothing was saved" not in shown
        assert _outcome(shown) == "finished"  # the card reads "Done.", which claims nothing about saving
    # The task and the gateway are still told the page was declined, with the flag.
    assert payload["status"] == "declined" and payload["sign_in_may_remain"] is True
    assert "event=broker_auth_signed_in_after_decline" in caplog.text
    assert "event=broker_auth_process_ended_after_decline" not in caplog.text
    assert "gho_probe_value" not in caplog.text and "gho_probe_value" not in page
    # The leftover is not removed (it may hold an earlier sign-in of the person's).
    assert hosts.exists()


def test_a_sign_in_process_that_ignores_stop_is_killed_and_waited_for() -> None:
    process = subprocess.Popen(
        ["sh", "-c", "trap '' TERM; echo ready; while true; do sleep 0.1; done"],
        stdout=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"

    auth_flows._terminate_process(process)

    assert process.poll() is not None  # gone before a declined sign-in reads what it left
