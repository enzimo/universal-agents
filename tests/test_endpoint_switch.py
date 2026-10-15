"""Switching the team's OpenAI-compatible endpoint (``llm.litellm_api_base``) through a staged plan.

A small local OpenAI-compatible server accepts one key. Covered: staging is
refused while there is no key for the new address, and while the operator's
``LITELLM_API_BASE`` would override the change; the Inbox text names the new
host and where its key comes from; the plan is a runtime settings change for
administrators, never a model choice; staging tests the address, with and
without ``/v1``, and stages the form that answers; applying tests the new
endpoint again, saves it only when the test passes, and needs a manager restart.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import endpoint_switch
from custom_agents.model_choice import MODEL_CHOICE_CHANGE_KIND
from custom_agents.model_keys import ModelKeys
from custom_agents.runtime_settings_service import RuntimeSettingsService
from custom_messages.contracts.status_v1 import EnvironmentOverridesV1
from custom_tools.providers.runtime_settings import plan_runtime_setting_change
from custom_tools.runtime_context import set_agent_ref
from docker.settings_environment import write_environment_overrides
from tests.model_key_support import env_model_keys
from tests.test_runtime_settings_tools import (  # noqa: F401 - _signed_task_contexts is an autouse fixture
    _apply_settings_change,
    _fake_agent,
    _signed_task_contexts,
    _write_config,
)

GOOD_KEY = "sk-bifrost-key-0000000001"


class _Bifrost(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path != "/v1/chat/completions":
            # Like bifrost.example.net without /v1.
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ok = self.headers.get("Authorization") == f"Bearer {GOOD_KEY}"
        body = (
            {"choices": [{"message": {"content": "I'm connected and ready to go!"}}]}
            if ok
            else {"error": {"message": "Incorrect API key provided"}}
        )
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def bifrost():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Bifrost)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture
def lead(tmp_path: Path):
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path)
    set_agent_ref(agent)
    yield agent, config_path
    set_agent_ref(None)


def _keys(monkeypatch: pytest.MonkeyPatch, keys: ModelKeys) -> None:
    monkeypatch.setattr(endpoint_switch, "process_model_keys", lambda: keys)


def _stage(base_url: str) -> dict:
    return json.loads(plan_runtime_setting_change._tool_func(path="llm.litellm_api_base", value=base_url))


def test_staging_is_refused_while_the_new_address_has_no_key(lead, monkeypatch, bifrost) -> None:
    _keys(monkeypatch, ModelKeys(None, env_lookup={}.get))

    plan = _stage(bifrost)

    assert "There is no key for 127.0.0.1" in plan["error"] and "/auth model" in plan["error"]


def test_staging_is_refused_while_the_operators_variable_would_override_it(lead, monkeypatch, bifrost) -> None:
    _agent, config_path = lead
    _keys(monkeypatch, env_model_keys(GOOD_KEY))
    write_environment_overrides(
        GatewayInstanceConfig.load_saved(config_path).layout.environment_overrides_path,
        EnvironmentOverridesV1(written_at=datetime.now(timezone.utc), setting_variables=("LITELLM_API_BASE",)),
    )

    plan = _stage(bifrost)

    assert "LITELLM_API_BASE is set in team.env" in plan["error"]


def test_the_inbox_text_names_the_host_and_whose_key_and_it_is_not_a_model_choice(lead, monkeypatch, bifrost) -> None:
    agent, _config_path = lead
    _keys(monkeypatch, env_model_keys(GOOD_KEY))

    plan = _stage(bifrost)
    proposal = RuntimeSettingsService(agent).describe(plan["change_id"])

    assert proposal.kind != MODEL_CHOICE_CHANGE_KIND
    assert proposal.owner_person_id is None, "the person who asked cannot approve a team endpoint alone"
    assert any("will go to 127.0.0.1" in line and "LITELLM_API_KEY" in line for line in proposal.effect)
    assert plan["restart_scope"] == "none"
    assert GOOD_KEY not in json.dumps(plan) + " ".join(proposal.effect)


def test_staging_tests_the_address_and_stages_the_form_that_answers(lead, monkeypatch, bifrost) -> None:
    _keys(monkeypatch, env_model_keys(GOOD_KEY))

    plan = _stage(bifrost.removesuffix("/v1"))

    assert plan["changes"][0]["value"] == bifrost


def test_staging_is_refused_when_the_new_endpoint_refuses_the_key(lead, monkeypatch, bifrost) -> None:
    _keys(monkeypatch, env_model_keys("sk-wrong-key-0000000002"))

    plan = _stage(bifrost)

    assert "The provider refused the key." in plan["error"] and "401" in plan["error"]


def test_applying_tests_the_endpoint_first_and_saves_it_only_when_it_works(lead, monkeypatch, bifrost) -> None:
    _agent, config_path = lead
    _keys(monkeypatch, env_model_keys(GOOD_KEY))
    before = GatewayInstanceConfig.load_saved(config_path).llm.litellm_api_base
    staged = _stage(bifrost)["change_id"]
    # The key stops working between staging and the administrator's approval.
    _keys(monkeypatch, env_model_keys("sk-wrong-key-0000000002"))

    failed = json.loads(_apply_settings_change(staged, confirmed=True))

    assert "Nothing was changed" in failed["error"] and "401" in failed["error"]
    assert GatewayInstanceConfig.load_saved(config_path).llm.litellm_api_base == before

    _keys(monkeypatch, env_model_keys(GOOD_KEY))
    applied = json.loads(_apply_settings_change(_stage(bifrost)["change_id"], confirmed=True))

    assert applied["status"] == "applied" and applied["requires_restart"] is False
    assert applied["preflight"]["tested_before_write"] is True
    assert GatewayInstanceConfig.load_saved(config_path).llm.litellm_api_base == bifrost


def test_after_approval_agents_switch_at_their_next_turn_without_a_restart(lead, monkeypatch, bifrost) -> None:
    """MemReviewAgent stands for the agents a manager restart never reached."""

    from types import SimpleNamespace

    from custom_agents.endpoint_refresh import forget_cached_configuration, refresh_team_endpoint

    _agent, config_path = lead
    _keys(monkeypatch, env_model_keys(GOOD_KEY))
    forget_cached_configuration()
    before = GatewayInstanceConfig.load_saved(config_path).llm.litellm_api_base
    rebuilt: list[str] = []
    worker = SimpleNamespace(
        id="mem-1", named_agent_type="MemReviewAgent", instance_config_path=str(config_path), litellm_api_base=before
    )
    worker._rebuild_runtime_llm_components = lambda: rebuilt.append(worker.litellm_api_base)
    assert refresh_team_endpoint(worker) is False  # nothing changed yet

    json.loads(_apply_settings_change(_stage(bifrost.removesuffix("/v1"))["change_id"], confirmed=True))

    assert refresh_team_endpoint(worker) is True
    assert worker.litellm_api_base == bifrost and rebuilt == [bifrost]
    assert refresh_team_endpoint(worker) is False  # once per change


def test_an_endpoint_with_a_password_in_it_is_refused(lead, monkeypatch) -> None:
    _keys(monkeypatch, env_model_keys(GOOD_KEY))

    plan = _stage("https://user:secret@bifrost.example.com/v1")

    assert "user name or password" in plan["error"]


def test_a_refusal_for_an_address_without_a_key_names_the_saved_endpoints(lead, monkeypatch) -> None:
    from custom_messages.contracts.model_keys_v1 import SavedEndpointsV1, SavedEndpointV1
    from model_providers.endpoints import EndpointKind

    _keys(monkeypatch, ModelKeys(None, env_lookup={}.get))
    saved = SavedEndpointV1(
        address="https://bifrost.example.net",
        base_url="https://bifrost.example.net/v1",
        kind=EndpointKind.OPENAI_COMPATIBLE,
        saved_at=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(endpoint_switch, "saved_team_endpoints", lambda: SavedEndpointsV1(endpoints=(saved,)))

    plan = _stage("https://bifrots.example.net")

    assert "There is no key for bifrots.example.net" in plan["error"]
    assert "Endpoints with a saved key: https://bifrost.example.net/v1." in plan["error"]


def test_the_configuration_view_lists_saved_endpoints_and_how_to_switch(monkeypatch) -> None:
    from types import SimpleNamespace

    from custom_agents.runtime_configuration_context import model_endpoints_context
    from custom_messages.contracts.model_keys_v1 import SavedEndpointsV1, SavedEndpointV1
    from model_providers.endpoints import EndpointKind

    now = datetime.now(timezone.utc)
    saved = SavedEndpointsV1(
        endpoints=(
            SavedEndpointV1(address="https://bifrost.example.net", base_url="https://bifrost.example.net/v1",
                            kind=EndpointKind.OPENAI_COMPATIBLE, tested_model_id="openai/gpt-6-luna", saved_at=now),
            SavedEndpointV1(address="https://litellm.example.net", kind=EndpointKind.OPENAI_COMPATIBLE, saved_at=now),
        )
    )
    monkeypatch.setattr("custom_agents.model_keys.saved_team_endpoints", lambda: saved)

    view = model_endpoints_context(SimpleNamespace(litellm_api_base="https://litellm.example.net"))

    assert view["current_openai_compatible_endpoint"] == "https://litellm.example.net"
    bifrost, litellm = view["saved_endpoints"]
    assert (bifrost["host"], bifrost["base_url"], bifrost["in_use"]) == ("bifrost.example.net", "https://bifrost.example.net/v1", False)
    assert litellm["in_use"] is True and litellm["base_url"] == "https://litellm.example.net"
    assert "never ask for a key or for the address" in view["how_to_switch"]
