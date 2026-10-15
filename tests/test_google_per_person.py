"""Google Workspace (gog) runs for one person at a time, with real child processes.

A small fake ``gog`` program stands in for the real CLI. It keeps its OAuth
client and keyring under ``GOG_HOME`` like gog 0.41 does, logs every call to a
file next to itself, and reports which account, client and home a command ran
with. The credential store is the real file-backed broker state. Covered:

* two people's runs use separate private homes and their own tokens;
* a person without a stored sign-in is refused before gog starts;
* the team's OAuth client is used when a person has none, and a person's own
  client wins; only an administrator can store the team's client;
* the browser sign-in and the token import store the sign-in for the person;
* the status reports "authenticated" only when Google accepts the sign-in;
* gog gets an allowlisted environment, never the broker's secrets;
* with no Google service-account file anywhere (GOOGLE_APPLICATION_CREDENTIALS
  is refused at startup), two people's agent requests through the broker's
  HTTP service each run gog with their own sign-in;
* paging bookmarks survive the broker's HTTP response and reach the next
  command, while secret-marked fields remain hidden.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from secure_tool_broker import google_workspace
from secure_tool_broker.auth_flows import BrokerAuthSession, GoogleWebAuthFlow
from secure_tool_broker.credential_state import broker_credential_file, credential_owner_context
from secure_tool_broker.credential_state_store import read_credential_state
from secure_tool_broker.executors import execute_validated_action, get_auth_status
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.providers import ToolContext, ValidatedAction
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, agent_context, caller_headers, gateway_headers, open_gate

ALICE = "alice"
BOB = "bob"
TEAM_CLIENT = json.dumps({"installed": {"client_id": "team-app.apps.googleusercontent.com", "client_secret": "s1"}})
OWN_CLIENT = json.dumps({"installed": {"client_id": "alice-app.apps.googleusercontent.com", "client_secret": "s2"}})

FAKE_GOG = r'''
import json, os, sys
from pathlib import Path

LOG = Path(__file__).with_name("gog-calls.jsonl")
home = Path(os.environ["GOG_HOME"])
data = home / "data"
args = sys.argv[1:]
with LOG.open("a") as log:
    log.write(json.dumps({"args": args, "home": str(home), "env": sorted(os.environ)}) + "\n")
assert os.environ.get("GOG_KEYRING_PASSWORD"), "the file keyring needs its password"
assert os.environ.get("GOG_KEYRING_BACKEND") == "file"
words = [a for a in args if not a.startswith("--")]

def token():
    path = data / "keyring" / "token.json"
    return json.loads(path.read_text()) if path.exists() else None

def client_id():
    path = data / "credentials.json"
    return json.loads(path.read_text())["installed"]["client_id"] if path.exists() else None

if words[:3] == ["auth", "credentials", "set"]:
    data.mkdir(parents=True, exist_ok=True)
    (data / "credentials.json").write_text(sys.stdin.read())
elif words[:3] == ["auth", "tokens", "import"]:
    assert client_id(), "gog needs its OAuth client before a token"
    (data / "keyring").mkdir(parents=True, exist_ok=True)
    (data / "keyring" / "token.json").write_text(sys.stdin.read())
elif words[:2] == ["auth", "list"]:
    current = token()
    accounts = [] if current is None else [
        {"email": current["email"], "valid": current["refresh_token"] != "revoked",
         "error": "invalid_grant" if current["refresh_token"] == "revoked" else ""}
    ]
    print(json.dumps({"accounts": accounts}))
elif words[:2] == ["auth", "add"]:
    email = words[2]
    print("Visit this URL to authorize: https://accounts.google.com/o/oauth2/auth?client_id="
          + str(client_id()) + "&redirect_uri=http%3A%2F%2F127.0.0.1%3A1&state=abc", flush=True)
    print("Paste redirect URL (Enter or Ctrl-D): ", flush=True)
    sys.stdin.readline()
    (data / "keyring").mkdir(parents=True, exist_ok=True)
    (data / "keyring" / "token.json").write_text(json.dumps({"email": email, "refresh_token": "rt-" + email}))
    print("email\t" + email, flush=True)
elif words[:3] == ["auth", "tokens", "export"]:
    out = args[args.index("--out") + 1]
    Path(out).write_text(json.dumps(token()))
else:
    current = token()
    if current is None:
        print("not authenticated", file=sys.stderr)
        sys.exit(1)
    pages_file = Path(__file__).with_name("gog-pages.json")
    if pages_file.exists():
        pages = json.loads(pages_file.read_text())
        bookmark = args[args.index("--page") + 1] if "--page" in args else ""
        print(json.dumps(pages[bookmark]))
    else:
        print(json.dumps({"account": current["email"], "client_id": client_id(), "home": str(home)}))
'''


@pytest.fixture
def gog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fake gog on PATH, a file-backed credential store, and broker secrets gog must not see."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    program = bin_dir / "gog"
    program.write_text(f"#!{sys.executable}\n{FAKE_GOG}", encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))
    monkeypatch.setenv("GOG_KEYRING_PASSWORD", "keyring-canary")
    # Secrets the broker process may hold; gog must never receive them.
    monkeypatch.setenv("GOG_ACCESS_TOKEN", "ambient-google-canary")
    monkeypatch.setenv("BW_SESSION", "ambient-bitwarden-canary")
    return bin_dir / "gog-calls.jsonl"


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _sign_in(person: str, email: str, *, refresh_token: str | None = None) -> None:
    token = json.dumps({"email": email, "refresh_token": refresh_token or f"rt-{email}"})
    assert google_workspace.store_person_token(person, token).status == "success"


def _team_client() -> None:
    assert google_workspace.store_team_client(TEST_TEAM, TEAM_CLIENT, stored_by="admin").status == "success"


def _search(person: str) -> tuple[str, dict]:
    result = execute_validated_action(
        ValidatedAction(provider="google", action="gmail_search", params={"query": "from:me"}),
        ToolContext(reason="test", canonical_user_id=person, team_id=TEST_TEAM),
    )
    return result.status, result.content if result.status == "success" else {"error": result.error, **result.content}


def test_two_people_use_separate_private_homes_and_their_own_sign_ins(gog: Path) -> None:
    _team_client()
    _sign_in(ALICE, "alice@example.com")
    _sign_in(BOB, "bob@example.com")

    alice_status, alice = _search(ALICE)
    bob_status, bob = _search(BOB)

    assert (alice_status, bob_status) == ("success", "success"), (alice, bob)
    assert alice["json"]["account"] == "alice@example.com"
    assert bob["json"]["account"] == "bob@example.com"
    alice_home, bob_home = Path(alice["json"]["home"]), Path(bob["json"]["home"])
    with credential_owner_context(ALICE):
        alice_runs = broker_credential_file("gog", "runs", "anchor").parent
    with credential_owner_context(BOB):
        bob_runs = broker_credential_file("gog", "runs", "anchor").parent
    assert alice_home.is_relative_to(alice_runs)
    assert bob_home.is_relative_to(bob_runs)
    assert alice_runs != bob_runs
    # Each run's home is removed when it ends: nothing is left for the next run.
    assert not alice_home.exists() and not bob_home.exists()
    # gog gets an allowlisted environment, never the broker's other secrets.
    for call in _calls(gog):
        assert "GOG_ACCESS_TOKEN" not in call["env"]
        assert "BW_SESSION" not in call["env"]
        assert "SECURE_TOOL_BROKER_CREDENTIALS_DIR" not in call["env"]


@pytest.mark.asyncio
async def test_two_peoples_agent_requests_run_gog_with_their_own_sign_ins(gog: Path) -> None:
    """The path agents use (secure_cli -> broker HTTP service -> gog), per person.

    Google tools no longer take a team-wide service-account file
    (GOOGLE_APPLICATION_CREDENTIALS); each person's own sign-in is the only
    Google identity a request can use.
    """

    _team_client()
    _sign_in(ALICE, "alice@example.com")
    _sign_in(BOB, "bob@example.com")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())

    def request(person: str) -> dict:
        return {
            "provider": "google",
            "action": "gmail_search",
            "params": {"query": "from:me"},
            "reason": "check mail",
            "context": agent_context(person, task_id=f"task-{person}"),
        }

    async with TestClient(TestServer(service.build_app())) as client:
        alice = await client.post("/v1/tools/google", headers=caller_headers(), json=request(ALICE))
        bob = await client.post("/v1/tools/google", headers=caller_headers(), json=request(BOB))
        alice_body, bob_body = json.loads(await alice.text()), json.loads(await bob.text())

    assert (alice.status, bob.status) == (200, 200), (alice_body, bob_body)
    assert alice_body["status"] == bob_body["status"] == "success", (alice_body, bob_body)
    assert alice_body["content"]["json"]["account"] == "alice@example.com"
    assert bob_body["content"]["json"]["account"] == "bob@example.com"
    assert alice_body["content"]["json"]["home"] != bob_body["content"]["json"]["home"]
    calls = _calls(gog)
    assert calls, "gog ran for both people"
    for call in calls:
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in call["env"]
        assert "GOOGLE_APPLICATION_CREDENTIALS_FILE" not in call["env"]


@pytest.mark.asyncio
async def test_a_broker_search_reads_the_next_page_without_exposing_secret_fields(gog: Path) -> None:
    """The bookmark returned to the agent must work on its next broker call."""

    bookmark = "09123457812345678901"
    first_page = {
        "threads": [{"id": "first-page"}],
        "nextPageToken": bookmark,
        "passwordPageToken": "opaque-password",
        "client_secret_page_token": "opaque-client-secret",
        "sessionSyncToken": "opaque-session-id",
    }
    last_page = {"threads": [{"id": "last-page"}]}
    gog.with_name("gog-pages.json").write_text(json.dumps({"": first_page, bookmark: last_page}))
    _team_client()
    _sign_in(ALICE, "alice@example.com")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    common = {
        "provider": "google",
        "reason": "read every matching thread",
        "context": agent_context(ALICE, task_id="paging-test"),
    }

    async with TestClient(TestServer(service.build_app())) as client:
        first = await client.post(
            "/v1/tools/google", headers=caller_headers(),
            json={**common, "action": "gmail_search", "params": {"query": "from:me"}},
        )
        first_body = await first.json()
        assert first.status == 200 and first_body["status"] == "success", first_body
        result = first_body["content"]["json"]
        assert result == {
            **first_page,
            "passwordPageToken": "[REDACTED]",
            "client_secret_page_token": "[REDACTED]",
            "sessionSyncToken": "[REDACTED]",
        }

        following = await client.post(
            "/v1/tools/google", headers=caller_headers(),
            json={**common, "action": "gog.exec", "params": {
                "argv": ["gmail", "search", "from:me", "--page", result["nextPageToken"]],
            }},
        )
        following_body = await following.json()
        assert following.status == 200 and following_body["status"] == "success", following_body
        assert following_body["content"]["json"] == last_page


def test_a_person_without_a_sign_in_is_refused_before_gog_runs(gog: Path) -> None:
    _team_client()
    _sign_in(ALICE, "alice@example.com")

    status, refusal = _search(BOB)

    assert status == "error"
    assert refusal["error_code"] == "auth_required"
    assert "/link cli:google" in refusal["error"]
    assert refusal["user_action"] == "/link cli:google"
    assert _calls(gog) == [], "gog never ran, so no keyring could be used"


def test_a_request_without_a_person_is_refused(gog: Path) -> None:
    result = execute_validated_action(
        ValidatedAction(provider="google", action="gmail_search", params={"query": "x"}),
        ToolContext(reason="test"),
    )
    assert result.status == "error"
    assert result.content["error_code"] == "person_required"
    assert _calls(gog) == []


def test_the_team_client_is_used_until_a_person_stores_their_own(gog: Path) -> None:
    _team_client()
    _sign_in(ALICE, "alice@example.com")
    _sign_in(BOB, "bob@example.com")

    assert _search(ALICE)[1]["json"]["client_id"] == "team-app.apps.googleusercontent.com"
    # A person pastes their own app on the sign-in page, which stores it for them.
    stored = google_workspace.store_person_client(ALICE, OWN_CLIENT)
    assert stored.status == "success"

    assert _search(ALICE)[1]["json"]["client_id"] == "alice-app.apps.googleusercontent.com"
    assert _search(BOB)[1]["json"]["client_id"] == "team-app.apps.googleusercontent.com"


def test_without_any_client_the_person_is_told_who_sets_it_up(gog: Path) -> None:
    _sign_in(ALICE, "alice@example.com")

    status, refusal = _search(ALICE)

    assert status == "error"
    assert refusal["error_code"] == "google_oauth_client_missing"
    assert "/auth google team_client" in refusal["error"]
    assert _calls(gog) == []


def test_a_missing_keyring_password_is_explained(gog: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _team_client()
    _sign_in(ALICE, "alice@example.com")
    monkeypatch.delenv("GOG_KEYRING_PASSWORD")

    status, refusal = _search(ALICE)

    assert status == "error"
    assert refusal["error_code"] == "google_keyring_password_missing"
    assert "GOG_KEYRING_PASSWORD" in refusal["error"]


def test_a_web_application_client_is_refused_for_the_team() -> None:
    web_client = json.dumps({"web": {"client_id": "x", "client_secret": "y"}})
    result = google_workspace.store_team_client(TEST_TEAM, web_client, stored_by="admin")
    assert result.status == "error"
    assert "Desktop app" in (result.error or "")


def test_status_is_authenticated_only_when_google_accepts_the_sign_in(gog: Path) -> None:
    _team_client()
    with credential_owner_context(ALICE):
        assert get_auth_status("google", team_id=TEST_TEAM).status == "auth_required"

    _sign_in(ALICE, "alice@example.com")
    with credential_owner_context(ALICE):
        status = get_auth_status("google", team_id=TEST_TEAM)
    assert (status.status, status.authenticated) == ("authenticated", True)
    assert "alice@example.com" in status.message

    _sign_in(ALICE, "alice@example.com", refresh_token="revoked")
    with credential_owner_context(ALICE):
        status = get_auth_status("google", team_id=TEST_TEAM)
    assert (status.status, status.authenticated) == ("auth_required", False)
    assert "no longer works" in status.message


def test_token_import_is_checked_with_google_and_stored_for_the_person(gog: Path) -> None:
    _team_client()
    good = json.dumps({"email": "alice@example.com", "refresh_token": "rt-a"})
    revoked = json.dumps({"email": "bob@example.com", "refresh_token": "revoked"})

    stored = google_workspace.login_google_with_token_json(good, person_id=ALICE, team_id=TEST_TEAM)
    refused = google_workspace.login_google_with_token_json(revoked, person_id=BOB, team_id=TEST_TEAM)

    assert stored.status == "success", stored
    assert stored.content["account"] == "alice@example.com"
    with credential_owner_context(ALICE):
        assert read_credential_state("gog.token_json") == good
    assert refused.status == "error"
    with credential_owner_context(BOB):
        assert read_credential_state("gog.token_json") is None


def _wait_for(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_browser_sign_in_runs_in_a_private_home_and_stores_the_persons_token(gog: Path) -> None:
    _team_client()
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        owner_canonical_user_id=ALICE,
        owner_team_id=TEST_TEAM,
    )
    flow = GoogleWebAuthFlow()
    logger = logging.getLogger(__name__)

    flow.submit(session, {"email": "alice@example.com", "services": "all", "drive_scope": "full"}, logger)
    _wait_for(lambda: "accounts.google.com" in str(session.state.get("external_url") or ""))
    home = Path(session.state["gog_home"])
    with credential_owner_context(ALICE):
        assert home.is_relative_to(broker_credential_file("gog", "runs", "anchor").parent)
    assert "team-app" in session.state["external_url"]

    flow.submit(session, {"redirect_url": "http://127.0.0.1:1/?code=abc&state=abc"}, logger)
    _wait_for(lambda: session.state.get("status") in {"success", "error"})

    assert session.state["status"] == "success", session.state.get("error")
    with credential_owner_context(ALICE):
        assert json.loads(read_credential_state("gog.token_json"))["email"] == "alice@example.com"
    with credential_owner_context(BOB):
        assert read_credential_state("gog.token_json") is None
    assert not home.exists()
    # The person can see the sign-in stored for them (never the agent).
    flow.submit(session, {"export_token_json": "1"}, logger)
    assert "alice@example.com" in session.state["exported_token_json"]


def test_browser_sign_in_without_any_client_asks_before_starting(gog: Path) -> None:
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        owner_canonical_user_id=ALICE,
        owner_team_id=TEST_TEAM,
    )

    GoogleWebAuthFlow().submit(session, {"email": "alice@example.com"}, logging.getLogger(__name__))

    assert session.state["status"] == "created"
    assert "/auth google team_client" in session.state["error"]
    assert session.process is None
    assert _calls(gog) == []


def test_browser_sign_in_can_store_the_persons_own_client(gog: Path) -> None:
    session = BrokerAuthSession(
        provider="google",
        method="web",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        owner_canonical_user_id=ALICE,
        owner_team_id=TEST_TEAM,
    )
    flow = GoogleWebAuthFlow()
    flow.submit(
        session,
        {"email": "alice@example.com", "credentials_json": OWN_CLIENT},
        logging.getLogger(__name__),
    )
    _wait_for(lambda: "accounts.google.com" in str(session.state.get("external_url") or ""))

    assert "alice-app" in session.state["external_url"]
    assert google_workspace.oauth_client_for(ALICE, TEST_TEAM).owner == "person"
    flow.cleanup(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("roles", "allowed"),
    [(["member"], False), (["admin"], True), (["owner"], True)],
)
async def test_only_an_administrator_can_store_the_teams_client(gog: Path, roles: list[str], allowed: bool) -> None:
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    context = {"canonical_user_id": ALICE, "principal_type": "user", "team_id": TEST_TEAM, "person_roles": roles}
    async with TestClient(TestServer(service.build_app())) as client:
        created = await client.post(
            "/v1/auth/google/sessions",
            headers=gateway_headers(),
            json={"context": context, "method": "team_client"},
        )
        body = json.loads(await created.text())
        if not allowed:
            assert created.status == 403
            assert body["reason_code"] == "google_team_client_admin_required"
            assert google_workspace.oauth_client_for(ALICE, TEST_TEAM) is None
            return
        assert created.status == 200
        form_path = "/" + body["login_url"].split("/", 3)[3]
        form = await client.get(form_path)
        assert "Set the team's Google sign-in app." in await form.text()
        submitted = await client.post(form_path, data={"credentials_json": TEAM_CLIENT})
        assert submitted.status == 200, await submitted.text()

    client_record = google_workspace.oauth_client_for(BOB, TEST_TEAM)
    assert client_record is not None and client_record.owner == "team"


@pytest.mark.asyncio
async def test_a_team_client_session_for_another_team_is_refused(gog: Path) -> None:
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    context = {"canonical_user_id": ALICE, "principal_type": "user", "team_id": "other-team", "person_roles": ["owner"]}
    async with TestClient(TestServer(service.build_app())) as client:
        created = await client.post(
            "/v1/auth/google/sessions", headers=gateway_headers(), json={"context": context, "method": "team_client"}
        )
    assert created.status == 403
