"""The web research tools send their requests through the secure tool broker.

Exa, Brave Search and the Crawl4AI screenshot need a key. The tools send the
broker a request without any key; the broker's gate decides it (the tools are
in the internet set every agent holds unless blocked), chooses the account link
(the person's own first, then the team's) and adds the key where the link says.
These tests run the real broker app, access store and credential catalog; only
the service itself is faked, so they see exactly what the broker sent.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
from tests.task_work_support import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from tests.broker_access_support import TEST_TEAM
from tests.broker_web_support import CEDAR_BINARY, build_gate, isolate_credentials, start_broker, work_for

TEAM_EXA_KEY = "team-exa-secret-value"
PERSON_EXA_KEY = "person-exa-secret-value"
TEAM_BRAVE_KEY = "team-brave-secret-value"
CRAWL4AI_TOKEN = "crawl4ai-secret-token"
CRAWL4AI_URL = "http://crawl4ai.test:11235"
SECRETS = (TEAM_EXA_KEY, PERSON_EXA_KEY, TEAM_BRAVE_KEY, CRAWL4AI_TOKEN)


class _TaskManager:
    current_task_id = "task-1"

    def get_task_info(self, _task_id: str):
        return SimpleNamespace(meta=SimpleNamespace(conversation_id="conversation-1"))


class _Agent:
    """An agent whose task writes research files for no person (folder rules only)."""

    authorization_config = {"enabled": False}
    team_id = TEST_TEAM
    id = "agent-1"
    named_agent_type = "GeneralAssistant"
    task_manager = _TaskManager()

    def __init__(self, layout: InstanceLayout) -> None:
        self.instance_root = str(layout.root)
        self.workspace_dir = str(layout.workspaces_dir)


@pytest.fixture
def agent(tmp_path: Path):
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    set_agent_ref(_Agent(layout))
    yield layout
    set_agent_ref(None)


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolate_credentials(tmp_path, monkeypatch)
    monkeypatch.setenv("TEST_TEAM_EXA_KEY", TEAM_EXA_KEY)
    monkeypatch.setenv("TEST_PERSON_EXA_KEY", PERSON_EXA_KEY)
    monkeypatch.setenv("TEST_TEAM_BRAVE_KEY", TEAM_BRAVE_KEY)
    monkeypatch.setenv("TEST_CRAWL4AI_TOKEN", CRAWL4AI_TOKEN)


@pytest.fixture
def broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets):
    gate = build_gate(tmp_path, service_urls={"web_services.crawl4ai.base_url": CRAWL4AI_URL})
    running, stop = start_broker(gate, monkeypatch)
    work_for(monkeypatch, "user-1")
    yield running
    stop()


def _header(call, name: str) -> str | None:
    return next((value for key, value in call.headers.items() if key.lower() == name.lower()), None)


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _assert_no_secret_left_the_broker(broker) -> None:
    sent_by_tools = json.dumps(broker.tool_requests)
    for secret in SECRETS:
        assert secret not in sent_by_tools, "a tool request carried a key"
    for request in broker.tool_requests:
        params = request["params"]
        assert "${" not in json.dumps(params), "no credential placeholders"
        assert not {name.lower() for name in params.get("headers") or {}} & {"x-api-key", "x-subscription-token", "authorization"}


def _team_exa(broker) -> None:
    broker.store_credential(CredentialOwnerScope.team(TEST_TEAM), "exa_team", "TEST_TEAM_EXA_KEY", "api_key")
    broker.link("http:exa", "exa_team")


def test_exa_search_sends_no_key_and_the_broker_adds_the_teams_linked_key(broker, agent) -> None:
    from custom_tools.exa_tools import exa_search

    _team_exa(broker)
    broker.answer_with({"results": [{"title": "Result", "url": "https://example.com"}], "requestId": "r1"})

    result = exa_search(query="agent frameworks", num_results=3, text={"maxCharacters": 500})

    assert result["status"] == "success", result
    assert json.loads(_text(result))["results"][0]["title"] == "Result"
    [call] = broker.sent
    assert call.method == "POST" and call.url == "https://api.exa.ai/search"
    assert _header(call, "x-api-key") == TEAM_EXA_KEY
    assert call.body == {"query": "agent frameworks", "type": "auto", "numResults": 3, "contents": {"text": {"maxCharacters": 500}}}
    _assert_no_secret_left_the_broker(broker)
    assert TEAM_EXA_KEY not in _text(result)


def test_the_persons_own_linked_key_beats_the_teams(broker, agent) -> None:
    from custom_tools.exa_tools import exa_get_contents

    _team_exa(broker)
    broker.store_credential(CredentialOwnerScope.user("user-1"), "exa_mine", "TEST_PERSON_EXA_KEY", "api_key")
    broker.link("http:exa", "exa_mine", person_id="user-1")
    broker.answer_with({"results": [], "statuses": []})

    result = exa_get_contents(urls=["https://example.com/page"], text=True)

    assert result["status"] == "success", result
    [call] = broker.sent
    assert call.url == "https://api.exa.ai/contents" and call.body == {"urls": ["https://example.com/page"], "text": True}
    assert _header(call, "x-api-key") == PERSON_EXA_KEY
    _assert_no_secret_left_the_broker(broker)


def test_a_missing_link_is_a_plain_refusal_with_the_link_commands(broker, agent) -> None:
    from custom_tools.exa_tools import exa_search

    result = exa_search(query="anything")

    assert result["status"] == "error"
    text = _text(result)
    assert "No account is linked for Exa web search" in text
    assert "/link http:exa team" in text and "/link http:exa." in text
    assert broker.sent == [], "nothing was sent without a key"


def test_system_work_uses_the_teams_linked_key(broker, agent, monkeypatch) -> None:
    from custom_tools.exa_tools import exa_search

    work_for(monkeypatch, None)
    _team_exa(broker)
    broker.answer_with({"results": []})

    result = exa_search(query="maintenance lookup")

    assert result["status"] == "success", result
    assert _header(broker.sent[0], "x-api-key") == TEAM_EXA_KEY


def test_brave_search_goes_through_the_broker_and_saves_artifacts(broker, agent) -> None:
    from custom_tools.web_research_artifact_tools import brave_web_search_artifact

    broker.store_credential(CredentialOwnerScope.team(TEST_TEAM), "brave_team", "TEST_TEAM_BRAVE_KEY", "api_key")
    broker.link("http:brave-search", "brave_team")
    broker.answer_with(
        {
            "query": {"original": "brave research", "more_results_available": False},
            "web": {"results": [{"title": "Brave result", "url": "https://example.com/b", "description": "snippet"}]},
        }
    )

    result = brave_web_search_artifact("brave research", count=5)
    payload = json.loads(_text(result))

    assert payload["status"] == "success", payload
    assert payload["top_results"][0]["title"] == "Brave result"
    assert Path(payload["files"]["raw_json"]).is_file()
    [call] = broker.sent
    assert call.method == "GET" and call.url.startswith("https://api.search.brave.com/res/v1/web/search?")
    assert "q=brave+research" in call.url and "count=5" in call.url
    assert _header(call, "X-Subscription-Token") == TEAM_BRAVE_KEY
    assert _header(call, "Accept-Encoding") is None, "the broker does not unpack compressed answers"
    _assert_no_secret_left_the_broker(broker)


def test_brave_without_a_link_fails_with_the_link_commands(broker, agent) -> None:
    from custom_tools.web_research_artifact_tools import brave_llm_context_artifact

    result = brave_llm_context_artifact("agent grounding")
    payload = json.loads(_text(result))

    assert result["status"] == "error"
    assert payload["failure_stage"] == "authentication"
    assert payload["failure_reason"] == "account_link_missing"
    assert any("/link http:brave-search team" in hint for hint in payload["mitigation_hints"])
    assert Path(payload["artifact_dir"], "manifest.json").is_file()


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def test_the_screenshot_tool_reaches_the_configured_crawl4ai_server_through_the_broker(
    broker, agent, monkeypatch
) -> None:
    from custom_tools.web_service_tools import capture_web_screenshot

    monkeypatch.setenv("CRAWL4AI_BASE_URL", CRAWL4AI_URL)
    broker.store_credential(CredentialOwnerScope.team(TEST_TEAM), "crawl4ai_team", "TEST_CRAWL4AI_TOKEN", "api_token")
    broker.link("http:crawl4ai", "crawl4ai_team")
    png = _png(8, 6)
    broker.answer_with({"success": True, "results": [{"success": True, "url": "https://example.com", "screenshot": base64.b64encode(png).decode()}]})

    result = capture_web_screenshot(url="https://example.com", width_px=8, height_px=6)
    payload = json.loads(_text(result))

    assert payload["status"] == "success", payload
    assert Path(payload["path"]).read_bytes() == png
    [call] = broker.sent
    assert call.url == f"{CRAWL4AI_URL}/crawl"
    assert _header(call, "Authorization") == f"Bearer {CRAWL4AI_TOKEN}"
    assert broker.tool_requests[0]["params"]["max_response_bytes"] > 1_000_000, "screenshots may exceed 1 MB"
    _assert_no_secret_left_the_broker(broker)


def test_the_crawl4ai_token_goes_only_to_the_configured_address(broker, agent) -> None:
    """Another port on the same host is not the Crawl4AI tool, so the broker refuses it."""

    catalog = broker.gate.catalog
    assert catalog.resolve_http("POST", f"{CRAWL4AI_URL}/crawl").tool_id == "http:crawl4ai"
    assert catalog.resolve_http("POST", "http://crawl4ai.test:9999/crawl").tool_id == "http:unlisted"
    assert catalog.resolve_http("POST", "https://crawl4ai.test:11235/crawl").tool_id == "http:unlisted"


@pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_with_authorization_on_agents_use_the_web_tools_without_asking_unless_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets, agent
) -> None:
    from access_control.requests import AccessRequests
    from access_control.entities import PersonPrincipal
    from access_control.inbox import StoredAccessInbox
    from custom_tools.web_research_artifact_tools import brave_web_search_artifact

    gate = build_gate(tmp_path, authorization=True)
    running, stop = start_broker(gate, monkeypatch)
    try:
        work_for(monkeypatch, "user-1")
        running.store_credential(CredentialOwnerScope.team(TEST_TEAM), "brave_team", "TEST_TEAM_BRAVE_KEY", "api_key")
        running.link("http:brave-search", "brave_team")
        running.answer_with({"web": {"results": []}})

        allowed = json.loads(_text(brave_web_search_artifact("first search")))
        assert allowed["status"] == "success", "the internet set covers it; no grant or request needed"

        assert gate.access is not None
        requests = AccessRequests(service=gate.access, inbox=StoredAccessInbox(gate.access.store), lead_agent_type="TeamArchitect")
        requests.block(
            PersonPrincipal("admin-1", TEST_TEAM, ("admin",)),
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEST_TEAM}/GeneralAssistant"),
            target=TargetRefV1(kind=TargetKind.TOOL, id="http:brave-search"),
            reason="No Brave for this agent type.",
        )
        blocked_result = brave_web_search_artifact("second search")
        blocked = json.loads(_text(blocked_result))

        assert blocked_result["status"] == "error"
        assert blocked["failure_reason"] == "access_denied"
        assert len(running.sent) == 1, "the blocked request never reached Brave"
    finally:
        stop()
