"""Decline on broker pages (A4 broker side, A6).

Decline is a small form of its own on each open page, posted to the page's own
address. It goes through the same one-time claim as a submission, so a page is
either answered or declined, never both. A declined sign-in whose CLI process
was still running stays declined when Decline stops that process (C01), and
nothing is stored. Forms posted from the sandboxed card (``Origin: null``) work;
a cross-site post without the private link is refused.
"""

from __future__ import annotations

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
from secure_tool_broker.defaults import CREDENTIAL_DECLINED_STATUS, DECLINE_FIELD, card_view_url
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
    assert status["status"] == CREDENTIAL_DECLINED_STATUS
    assert status["result"]["content"]["status"] == CREDENTIAL_DECLINED_STATUS

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
    assert "event=broker_auth_process_ended_after_decline" in caplog.text


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
