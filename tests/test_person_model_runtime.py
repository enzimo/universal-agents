"""Each person's own model in an agent's turns (``custom_agents.person_model_runtime``).

Two local OpenAI-compatible servers stand for the team's provider and Alice's
own one. One agent object runs turns for Alice, Bob (no model of his own) and
work for nobody, back to back. Covered:

* a turn for Alice runs on her model: the request reaches her server with her
  model id, and the turn's context window and settings are hers;
* the next turns, for Bob and for nobody, run on the team's model again;
* the agent's own model fields never change (failover, the settings apply and
  the status views read them), and failover stays out of a person's model;
* the safety-net hook puts the right model in place before a call, for a
  Strands agent that was rebuilt with the team's model mid-turn;
* who a task works for comes only from its verified signed context.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from access_control.person_settings import PersonSettings
from custom_agents import access_runtime, model_failover_runtime, person_model_runtime
from custom_agents.model_keys import ModelKeys
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1
from custom_messages.contracts.routing_v1 import GatewayContext
from model_providers.endpoints import EndpointKind
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import TEST_TEAM, context_signer, context_verify_keys

TEAM_MODEL = "team/model-1"
ALICE_MODEL = "openai/gpt-6-luna"


class FakeProvider:
    """Streams one chat completion and remembers the model each request named."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        return app

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.models.append(body["model"])
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunk = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}],
        }
        done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        for item in (chunk, done):
            await response.write(f"data: {json.dumps(item)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        return response


@pytest_asyncio.fixture
async def servers():
    started = []
    for _ in range(2):
        fake = FakeProvider()
        server = TestServer(fake.build_app())
        await server.start_server()
        started.append((fake, server, str(server.make_url("/v1")).rstrip("/")))
    yield started
    for _fake, server, _url in started:
        await server.close()


@pytest.fixture(autouse=True)
def _keys_and_contexts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    # Every endpoint's key from the environment: this test is about which model runs, not keys.
    keys = ModelKeys(None, env_lookup={"LITELLM_API_KEY": "sk-test-key-000000001"}.get)
    monkeypatch.setattr("custom_agents.model_keys.process_model_keys", lambda: keys)
    monkeypatch.setattr("custom_agents.strands_model_factory.process_model_keys", lambda: keys)
    access_runtime._runtime_access.cache_clear()
    person_model_runtime.forget_cached_profiles()
    yield
    access_runtime._runtime_access.cache_clear()
    person_model_runtime.forget_cached_profiles()


def _meta(person: str | None, *, signed: bool = True) -> SimpleNamespace:
    if person is None:
        return SimpleNamespace(gateway_context=None)
    task_context = context_signer().issue(team_id=TEST_TEAM, person_id=person, person_roles=("member",)) if signed else None
    return SimpleNamespace(gateway_context=GatewayContext(canonical_user_id=person, task_context=task_context))


def _agent(tmp_path: Path, team_url: str) -> SimpleNamespace:
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    team_model = build_strands_model(
        StrandsModelFactoryConfig(provider="litellm", model_id=TEAM_MODEL, litellm_api_base=team_url, context_window_tokens=64_000)
    )
    tasks = {
        "t-alice": SimpleNamespace(meta=_meta("alice")),
        "t-bob": SimpleNamespace(meta=_meta("bob")),
        "t-nobody": SimpleNamespace(meta=_meta(None)),
        "t-forged": SimpleNamespace(meta=_meta("alice", signed=False)),
    }
    return SimpleNamespace(
        id="agent-1",
        team_id=TEST_TEAM,
        model_provider="litellm",
        model_id=TEAM_MODEL,
        authorization_config={},
        instance_root=str(tmp_path),
        task_manager=SimpleNamespace(storage_settings=storage, get_task_info=tasks.get),
        _strands_model=team_model,
        _strands_agent=SimpleNamespace(model=team_model),
        _context_engine=SimpleNamespace(context_window_tokens=64_000),
        _resolved_context_window_tokens=lambda: 64_000,
        max_output_tokens=None,
        request_timeout_seconds=None,
        stream_idle_timeout_seconds=60.0,
        model_call_timeout_seconds=120.0,
        reasoning_trace_retention_messages=None,
        _active_execution_task_id=None,
    )


def _save_alices_model(agent: SimpleNamespace, base_url: str) -> None:
    PersonSettings(AccessRepository(agent.task_manager.storage_settings), TEST_TEAM).set_own_model(
        "alice",
        PersonModelProfileV1(
            kind=EndpointKind.OPENAI_COMPATIBLE,
            base_url=base_url,
            model_id=ALICE_MODEL,
            supports_images=True,
            context_window_tokens=200_000,
            tested_at_utc=datetime.now(timezone.utc),
        ),
        by="alice",
    )


async def _call(model) -> None:
    async for _event in model.stream([{"role": "user", "content": [{"text": "hi"}]}]):
        pass


@pytest.mark.asyncio
async def test_two_people_back_to_back_each_get_the_right_model(tmp_path: Path, servers) -> None:
    (team, _team_server, team_url), (alices, _alice_server, alice_url) = servers
    agent = _agent(tmp_path, team_url)
    _save_alices_model(agent, alice_url)
    settings = SimpleNamespace(model_provider="litellm", model_id=TEAM_MODEL, litellm_api_base=team_url,
                               litellm_api_mode="chat_completions", reasoning_effort="high", reasoning_summary="auto",
                               supports_image_input=False)
    import dataclasses

    @dataclasses.dataclass
    class _Settings:
        model_provider: str
        model_id: str
        litellm_api_base: str | None
        litellm_api_mode: str
        reasoning_effort: str | None
        reasoning_summary: str | None
        supports_image_input: bool | None

    turn_settings = _Settings(**vars(settings))

    bundle = await asyncio.to_thread(person_model_runtime.select_turn_model, agent, "t-alice")
    await _call(agent._strands_agent.model)
    alices_settings = person_model_runtime.turn_settings_with_person_model(agent, turn_settings)
    assert agent._context_engine.context_window_tokens == 200_000
    assert person_model_runtime.person_model_active(agent)
    person_model_runtime.clear_turn_model(agent)

    await asyncio.to_thread(person_model_runtime.select_turn_model, agent, "t-bob")
    await _call(agent._strands_agent.model)
    person_model_runtime.clear_turn_model(agent)
    await asyncio.to_thread(person_model_runtime.select_turn_model, agent, "t-nobody")
    await _call(agent._strands_agent.model)

    assert bundle is not None and bundle.person_id == "alice"
    assert alices.models == [ALICE_MODEL]
    assert team.models == [TEAM_MODEL, TEAM_MODEL]
    assert agent._strands_agent.model is agent._strands_model
    assert agent._context_engine.context_window_tokens == 64_000
    assert (alices_settings.model_id, alices_settings.litellm_api_base) == (ALICE_MODEL, alice_url)
    assert alices_settings.reasoning_summary is None and alices_settings.supports_image_input is True
    # The agent's own model fields stay the team's.
    assert (agent.model_provider, agent.model_id) == ("litellm", TEAM_MODEL)


def test_failover_stays_out_of_a_persons_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(tmp_path, "http://127.0.0.1:9/v1")
    _save_alices_model(agent, "http://127.0.0.1:8/v1")
    monkeypatch.setattr(model_failover_runtime, "is_enabled", lambda _agent: True)
    monkeypatch.setattr(model_failover_runtime, "is_failover_eligible_error", lambda _agent, _error: True)
    person_model_runtime.select_turn_model(agent, "t-alice")

    outcome = asyncio.run(
        model_failover_runtime.handle_provider_failure(agent, error=RuntimeError("429"), task_id="t-alice", tool_calls_started=False)
    )

    assert outcome is None
    model_failover_runtime.record_model_success(agent, task_id="t-alice")  # returns without touching the team's lease


def test_the_hook_puts_the_persons_model_back_on_a_rebuilt_agent(tmp_path: Path) -> None:
    agent = _agent(tmp_path, "http://127.0.0.1:9/v1")
    _save_alices_model(agent, "http://127.0.0.1:8/v1")
    hook = person_model_runtime.PersonModelHook(agent)
    rebuilt = SimpleNamespace(model=agent._strands_model)  # e.g. a fresh Strands agent after a settings change

    agent._active_execution_task_id = "t-alice"
    hook._before_model_call(SimpleNamespace(agent=rebuilt))
    alices_model = rebuilt.model
    agent._active_execution_task_id = "t-nobody"
    hook._before_model_call(SimpleNamespace(agent=rebuilt))

    assert alices_model is not agent._strands_model
    assert rebuilt.model is agent._strands_model


def test_only_a_verified_signed_context_selects_a_persons_model(tmp_path: Path) -> None:
    agent = _agent(tmp_path, "http://127.0.0.1:9/v1")
    _save_alices_model(agent, "http://127.0.0.1:8/v1")

    assert person_model_runtime.select_turn_model(agent, "t-forged") is None
    assert agent._strands_agent.model is agent._strands_model


def test_a_litellm_model_without_parameters_still_formats_a_request() -> None:
    """Regression: the factory stores params=None for a model with no timeout, temperature or limit."""

    model = build_strands_model(
        StrandsModelFactoryConfig(provider="litellm", model_id=ALICE_MODEL, litellm_api_base="http://127.0.0.1:9/v1")
    )
    request = model.format_request([{"role": "user", "content": [{"text": "hi"}]}])

    assert request["model"].endswith(ALICE_MODEL) and request["stream"] is True
