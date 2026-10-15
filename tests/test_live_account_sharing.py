"""Live: whose account the broker uses follows the team's sharing, against the real services.

The offline tests (``tests/test_account_sharing.py``,
``tests/test_onboarding_account_keys.py``) prove the choices with a fake
service. These send the same calls to the real Exa, Brave Search and
Crawl4AI APIs through a real broker, so the keys the broker picks are the
keys the services accept:

- an Exa key typed during onboarding for a private service becomes the
  administrator's own account: their search reaches Exa, another person's is
  refused before anything is sent; the Brave key for a shared service serves
  another person's search;
- the team's own Crawl4AI server takes only the team's token: a person's own
  link form is refused, the administrator links the team's token through the
  real form, and a member's crawl and work for nobody both reach the server.

Run with ``RUN_LIVE_ACCOUNT_SHARING=1``. The keys come from the env file named
by ``UA_LIVE_ACCOUNT_SHARING_ENV`` (default ``debug.env``): ``EXA_API_KEY``,
``BRAVE_SEARCH_API_KEY``, ``CRAWL4AI_API_TOKEN`` and ``CRAWL4AI_BASE_URL``.
They are read from the file only, never put in this process's environment.
The credential store is the in-memory Sealbox the other broker tests use.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from dotenv import dotenv_values

from access_catalog.catalog import CatalogSource
from access_control.account_sharing import SharingNotChosen, TeamAccountSharing
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.onboarding_account_keys import OnboardingAccountKeyHandover, write_handover
from custom_messages.contracts.access_v2 import AccountLinkRequestV1, AccountSharing, LinkOwnerKind
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.services import CRAWL4AI_URL_SETTING
from secure_tool_broker import credential_catalog
from secure_tool_broker.account_links import CREDENTIAL_SOURCE_FIELD, NEW_CREDENTIAL, NEW_FIELD_PREFIX
from tests.broker_access_support import TEST_TEAM, caller_token, gateway_headers
from tests.broker_web_support import build_gate, fake_sealbox, isolate_credentials, start_broker, work_for

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMIN = "owner-1"
MEMBER = "user-1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_ACCOUNT_SHARING") != "1",
        reason="Set RUN_LIVE_ACCOUNT_SHARING=1 to call the real Exa, Brave Search and Crawl4AI APIs.",
    ),
]


@pytest.fixture(scope="module")
def keys() -> dict[str, str]:
    path = Path(os.getenv("UA_LIVE_ACCOUNT_SHARING_ENV", str(PROJECT_ROOT / "debug.env"))).expanduser()
    if not path.is_file():
        pytest.skip(f"No env file with the service keys at {path}.")
    values = {name: str(value or "").strip() for name, value in dotenv_values(path).items()}
    needed = ("EXA_API_KEY", "BRAVE_SEARCH_API_KEY", "CRAWL4AI_API_TOKEN", "CRAWL4AI_BASE_URL")
    missing = [name for name in needed if not values.get(name)]
    if missing:
        pytest.skip(f"{path} has no {', '.join(missing)}.")
    return values


@pytest.fixture
def live_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys: dict[str, str]):
    """A real broker that sends to the real services, recording each call it makes."""

    real_send = credential_catalog._send_api_request
    isolate_credentials(tmp_path, monkeypatch)
    fake_sealbox(monkeypatch)
    gate = build_gate(tmp_path, service_urls={CRAWL4AI_URL_SETTING: keys["CRAWL4AI_BASE_URL"].rstrip("/")})
    running, stop = start_broker(gate, monkeypatch)
    sent: list[str] = []

    def recording_send(request, *, timeout: float):
        sent.append(urllib.parse.urlsplit(request.full_url).hostname or "")
        return real_send(request, timeout=timeout)

    monkeypatch.setattr(credential_catalog, "_send_api_request", recording_send)
    running.hosts = sent  # type: ignore[attr-defined]
    yield running
    stop()


def _hand_over(instance: Path, running) -> None:
    from access_control.caller_tokens import CALLER_GATEWAY

    handover = OnboardingAccountKeyHandover(
        instance_root=instance,
        team_id=TEST_TEAM,
        broker=HttpGatewaySecureBroker(
            internal_base_url=running.base_url, public_base_url=running.base_url, token=caller_token(CALLER_GATEWAY)
        ),
        roles_of=lambda person_id: ("owner",) if person_id == ADMIN else None,
        send_notice=lambda _notice: None,
        store=lambda: running.gate.links,
        catalog=lambda: running.gate.catalog,
        retry_delays=(0.0,),
    )
    asyncio.run(handover.run(asyncio.Event()))


def test_onboarding_keys_serve_whom_the_team_chose_at_the_real_services(
    tmp_path: Path, live_broker, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_tools.broker_web_requests import send_web_request
    from custom_tools.exa_tools import exa_search

    instance = tmp_path / "instance"
    InstanceLayout.at(instance).ensure_directories()
    write_handover(
        instance,
        team_id=TEST_TEAM,
        person_id=ADMIN,
        keys={"http:exa": keys["EXA_API_KEY"], "http:brave-search": keys["BRAVE_SEARCH_API_KEY"]},
        sharing={"http:exa": AccountSharing.PRIVATE, "http:brave-search": AccountSharing.SHARED},
    )
    _hand_over(instance, live_broker)

    work_for(monkeypatch, ADMIN, roles=("owner",))
    mine = exa_search(query="Universal Agents multi-agent framework", num_results=1)
    assert mine["status"] == "success", mine
    assert "api.exa.ai" in live_broker.hosts

    sent = len(live_broker.hosts)
    work_for(monkeypatch, MEMBER)
    theirs = exa_search(query="anything", num_results=1)
    assert theirs["status"] == "error" and "/link http:exa" in str(theirs)
    assert len(live_broker.hosts) == sent, "nothing reached Exa for the other person"

    # Brave is shared: the team's key from onboarding serves the member.
    brave = send_web_request(
        "GET", "https://api.search.brave.com/res/v1/web/search", params={"q": "universal agents", "count": 1},
        reason="Live sharing check",
    )
    assert brave.ok and live_broker.hosts[-1] == "api.search.brave.com", brave.http_status


def _post_json(url: str, body: dict, headers: dict[str, str]) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={**headers, "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _link_request(owner: LinkOwnerKind, person_id: str, roles: tuple[str, ...]) -> dict:
    return AccountLinkRequestV1(
        request_id=f"req-{owner.value}", team_id=TEST_TEAM, tool_id="http:crawl4ai", owner_kind=owner,
        person_id=person_id, person_roles=roles, reason="Live sharing check",
    ).model_dump(mode="json")


def test_the_teams_own_crawl4ai_server_takes_only_the_teams_token(
    live_broker, keys: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_tools.broker_web_requests import send_web_request

    sessions = f"{live_broker.base_url}/v1/account-link/sessions"
    own_status, own_text = _post_json(sessions, _link_request(LinkOwnerKind.PERSON, MEMBER, ("member",)), gateway_headers())
    team_status, team_text = _post_json(sessions, _link_request(LinkOwnerKind.TEAM, ADMIN, ("owner",)), gateway_headers())
    assert own_status == 403 and "uses only the team's account" in own_text
    assert team_status == 200, team_text
    form = urllib.parse.urlencode(
        {CREDENTIAL_SOURCE_FIELD: NEW_CREDENTIAL, f"{NEW_FIELD_PREFIX}api_token": keys["CRAWL4AI_API_TOKEN"], "confirm": "yes"}
    ).encode()
    with urllib.request.urlopen(
        urllib.request.Request(f"{live_broker.base_url}/account-link/{json.loads(team_text)['session_id']}", data=form),
        timeout=30,
    ) as submitted:
        assert submitted.status == 200
    links = live_broker.gate.links
    assert links.active_link(TEST_TEAM, "http:crawl4ai", LinkOwnerKind.TEAM, TEST_TEAM) is not None
    with pytest.raises(SharingNotChosen):
        TeamAccountSharing(links, TEST_TEAM).choose_tool(
            CatalogSource(None).current().tools["http:crawl4ai"], AccountSharing.PRIVATE, by=ADMIN
        )

    crawl = keys["CRAWL4AI_BASE_URL"].rstrip("/") + "/crawl"
    for person in (MEMBER, None):  # a member's work, and work that serves nobody
        work_for(monkeypatch, person)
        response = send_web_request("POST", crawl, json_body={"urls": ["https://example.com"]}, reason="Live sharing check")
        assert response.ok, (person, response.http_status)
