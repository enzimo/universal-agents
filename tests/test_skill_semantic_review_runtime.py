"""The selected skill review with the runtime's real model selection, model keys and model clients.

Staging runs as a sync tool call of a real turn, as in production. Local
servers stand for the model providers; a fake broker stands for the key
store. Covered:

* a person's turn reviews with the person's own model, at their own endpoint
  and with their own key, which the key lookup found through the turn's
  verified signed context; the team's endpoint gets nothing;
* a person whose work may not use the team's keys, and who has no model of
  their own, gets ``model_key_refused`` with the message telling them to set
  one up, and no plan;
* a model whose client belongs to the event loop of its first call (as a
  pooled async HTTP client does) reviews from a real Strands turn's sync tool;
* the factory's native Anthropic model, whose one async client keeps pooled
  connections from the turn's loop, reviews twice in one turn with no retry
  and the team's key on every request.

The Anthropic case sets ``max_tokens`` on its model: Strands requires it and
the factory sets none, an older gap outside this change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from strands import Agent, tool
from strands.models import Model

from access_control.person_settings import PersonSettings
from custom_agents import access_runtime, person_model_runtime
from custom_agents.model_keys import ModelKeys, own_model_needed_message
from custom_agents.skill_installation import SkillInstallationService, resolve_skill_installation_paths
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_messages.contracts.person_settings_v1 import PersonModelProfileV1
from custom_tools.runtime_context import current_task_work, set_agent_ref
from model_providers.endpoints import EndpointKind, openai_compatible_endpoint, well_known_endpoint
from storage.access_repository import AccessRepository
from tests.change_approval_support import TASK_ID, use_signed_contexts
from tests.task_work_support import run_turn, strands_parts, with_turn_bookkeeping
from tests.test_model_keys_runtime import FakeBroker
from tests.test_skill_installation import REPOSITORY_URL, _agent, _patch_acquisition, _repository, _write_config

TEAM_MODEL = "team/review-model"
OWN_MODEL = "openai/own-review-model"
ANTHROPIC_MODEL = "claude-review-test"
TEAM_KEY = "sk-team-key-0000000001"
OWN_KEY = "sk-own-key-00000000001"
STAGE_TOOL = "stage_skill"
DECISION_TOOL = "SemanticDecision"


def _decision(text: str) -> dict[str, Any]:
    """A complete decision with no finding for the files in a review prompt."""

    files = json.loads(text)["untrusted_files"]
    return {"complete": True, "inspected_files": [f["path"] for f in files], "findings": [], "summary": "Reviewed."}


def _review_prompt(messages: list[dict[str, Any]]) -> str:
    """The review prompt among a request's messages (string or block content)."""

    for message in messages:
        content = message.get("content")
        for block in content if isinstance(content, list) else [{"text": content}]:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str) and "untrusted_files" in text:
                return text
    raise AssertionError("a review request without the review prompt")


class FakeProvider:
    """A local model provider: OpenAI-compatible chat completions and the Anthropic Messages API.

    A request that offers the decision tool is a review, answered with a
    decision covering every supplied file. Any other request is the parent
    turn's: it asks for the staging tool until ``stagings`` results came
    back, then ends. A request saying ``warm`` waits a little, so several at
    once each open their own connection.
    """

    def __init__(self, *, stagings: int = 1) -> None:
        self.stagings = stagings
        self.requests: list[dict[str, Any]] = []
        self.peers: set[Any] = set()

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._openai)
        app.router.add_post("/v1/messages", self._anthropic)
        return app

    def _record(self, request: web.Request, body: dict[str, Any], key: str) -> bool:
        review = DECISION_TOOL in [t.get("name") or t.get("function", {}).get("name") for t in body.get("tools") or []]
        self.peers.add(request.transport.get_extra_info("peername") if request.transport else None)
        self.requests.append({"review": review, "model": body.get("model"), "key": key})
        return review

    async def _openai(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self._record(request, body, request.headers.get("Authorization", "")):
            arguments = json.dumps(_decision(_review_prompt(body["messages"])))
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                          "function": {"name": DECISION_TOOL, "arguments": arguments}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "done"}, "stop"
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": body["model"]}
        chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
        stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return web.Response(body=stream.encode(), content_type="text/event-stream")

    async def _anthropic(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self._record(request, body, request.headers.get("X-Api-Key", "")):
            blocks: list[dict[str, Any]] = [
                {"type": "tool_use", "name": DECISION_TOOL, "input": _decision(_review_prompt(body["messages"]))}
            ]
        elif _text_of(body) == "warm":
            await asyncio.sleep(0.3)
            blocks = [{"type": "text", "text": "warm"}]
        elif _tool_results(body) < self.stagings:
            blocks = [{"type": "tool_use", "name": STAGE_TOOL, "input": {}}]
        else:
            blocks = [{"type": "text", "text": "done"}]
        stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"
        return web.Response(body=_anthropic_events(blocks, stop, len(self.requests)), content_type="text/event-stream")


def _text_of(body: dict[str, Any]) -> str:
    content = body["messages"][0]["content"]
    return content if isinstance(content, str) else str(content[0].get("text", ""))


def _tool_results(body: dict[str, Any]) -> int:
    return sum(1 for message in body["messages"] if isinstance(message["content"], list)
               for block in message["content"] if block.get("type") == "tool_result")


def _anthropic_events(blocks: list[dict[str, Any]], stop: str, request_number: int) -> bytes:
    events: list[dict[str, Any]] = [{"type": "message_start", "message": {
        "id": f"msg_{request_number}", "type": "message", "role": "assistant", "model": ANTHROPIC_MODEL,
        "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {"type": "tool_use", "id": f"toolu_{request_number}_{index}", "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [{"type": "content_block_start", "index": index, "content_block": start},
                   {"type": "content_block_delta", "index": index, "delta": delta},
                   {"type": "content_block_stop", "index": index}]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@pytest_asyncio.fixture
async def providers():
    """Two local providers: the team's and a person's own."""

    started = []
    for _ in range(2):
        fake = FakeProvider()
        server = TestServer(fake.build_app())
        await server.start_server()
        started.append((fake, server))
    yield [(fake, str(server.make_url("")).rstrip("/")) for fake, server in started]
    for _fake, server in started:
        await server.close()


class RecordingBroker(FakeBroker):
    """The fake key broker, also remembering the address of each lookup."""

    def __init__(self) -> None:
        super().__init__(key=TEAM_KEY)
        self.addresses: list[str] = []

    def lookup(self, address, context=None, **kwargs):
        self.addresses.append(address)
        return super().lookup(address, context, **kwargs)


@pytest.fixture
def broker(monkeypatch):
    """The process's model keys come only from this broker: no environment override."""

    broker = RecordingBroker()
    keys = ModelKeys(broker, env_lookup={}.get)
    monkeypatch.setattr("custom_agents.model_keys.process_model_keys", lambda: keys)
    monkeypatch.setattr("custom_agents.strands_model_factory.process_model_keys", lambda: keys)
    person_model_runtime.forget_cached_profiles()
    yield broker
    person_model_runtime.forget_cached_profiles()


@pytest.fixture
def installation(tmp_path, monkeypatch, broker):
    """Selected review on; TeamArchitect works on a task its owner asked for through the gateway (signed)."""

    use_signed_contexts(monkeypatch)
    config, path = _write_config(tmp_path)
    config.workspace.skill_installation.semantic_review.mode = "task_model"
    agent = with_turn_bookkeeping(_agent(config, path))
    _patch_acquisition(monkeypatch, _repository(tmp_path))
    yield agent, SkillInstallationService(agent)
    access_runtime._runtime_access.cache_clear()


def _use_team_model(agent: Any, team_url: str) -> None:
    """The agent's team model at ``team_url``, and what building a person's model reads from the agent."""

    agent._strands_model = build_strands_model(StrandsModelFactoryConfig(
        provider="litellm", model_id=TEAM_MODEL, litellm_api_base=f"{team_url}/v1", context_window_tokens=64_000))
    agent.model_provider, agent.model_id = "litellm", TEAM_MODEL
    agent._resolved_context_window_tokens = lambda: 64_000
    agent.max_output_tokens = agent.request_timeout_seconds = agent.reasoning_trace_retention_messages = None
    agent.stream_idle_timeout_seconds, agent.model_call_timeout_seconds = 60.0, 120.0


def _signed_context(agent: Any) -> dict[str, Any]:
    return agent.task_manager.meta.gateway_context.task_context.model_dump(mode="json")


async def _stage_in_persons_turn(agent: Any, service: SkillInstallationService) -> dict[str, Any]:
    """A turn that chooses its model as the runtime does, then stages from a sync tool."""

    async def turn() -> dict[str, Any]:
        set_agent_ref(agent)
        await asyncio.to_thread(person_model_runtime.select_turn_model, agent, TASK_ID)
        return await asyncio.to_thread(lambda: service.stage(repository_url=REPOSITORY_URL))

    return await run_turn(agent, TASK_ID, turn)


@pytest.mark.asyncio
async def test_a_persons_turn_reviews_with_their_own_model_and_their_own_key(installation, broker, providers):
    agent, service = installation
    (team, team_url), (own, own_url) = providers
    _use_team_model(agent, team_url)
    person = agent.task_manager.person_id
    PersonSettings(AccessRepository(agent.task_manager.storage_settings), agent.team_id).set_own_model(
        person,
        PersonModelProfileV1(kind=EndpointKind.OPENAI_COMPATIBLE, base_url=f"{own_url}/v1", model_id=OWN_MODEL,
                             context_window_tokens=200_000, tested_at_utc=datetime.now(timezone.utc)),
        by=person,
    )
    broker.personal[person] = OWN_KEY

    plan = await _stage_in_persons_turn(agent, service)

    report = json.loads(Path(plan["semantic_review"]["report_path"]).read_text())
    assert (report["status"], report["reason"]) == ("completed", "assessed"), report
    assert report["model_id"] == OWN_MODEL
    assert report["provider"] == person_model_runtime.factory_provider(EndpointKind.OPENAI_COMPATIBLE)
    assert own.requests == [{"review": True, "model": OWN_MODEL, "key": f"Bearer {OWN_KEY}"}]
    assert team.requests == []
    # The key lookup named the person only through the turn's verified signed context.
    assert broker.addresses == [openai_compatible_endpoint(f"{own_url}/v1").address]
    (context,) = broker.contexts
    assert (context["canonical_user_id"], context["task_id"]) == (person, TASK_ID)
    assert context["task_context"] == _signed_context(agent)


@pytest.mark.asyncio
async def test_work_that_may_not_use_the_team_keys_refuses_the_review_with_the_own_model_message(
    installation, broker, providers
):
    agent, service = installation
    (team, team_url), _own = providers
    _use_team_model(agent, team_url)
    person = agent.task_manager.person_id
    broker.own_model_only.add(person)

    with pytest.raises(PermissionError) as refused:
        await _stage_in_persons_turn(agent, service)

    assert "(model_key_refused)" in str(refused.value)
    assert own_model_needed_message() in str(refused.value)
    assert team.requests == []
    (context,) = broker.contexts
    assert context["canonical_user_id"] == person and context["task_context"] == _signed_context(agent)
    paths = resolve_skill_installation_paths(agent)
    assert not list(paths.plans.glob("*.json"))


async def _stage_from_a_strands_turn(agent: Any, service: SkillInstallationService, model: Any,
                                     before: Any = None) -> tuple[list[dict[str, Any]], Any]:
    """A real Strands turn on ``model`` whose sync staging tool the model asks for (``FakeProvider``)."""

    plans: list[dict[str, Any]] = []

    @tool(name=STAGE_TOOL)
    def stage_skill() -> str:
        """Stage the reviewed skill."""

        plan = service.stage(repository_url=REPOSITORY_URL)
        plans.append(plan)
        return str(plan["semantic_review"]["status"])

    async def turn() -> Any:
        set_agent_ref(agent)
        if before is not None:
            await before()
        parent = Agent(model=model, tools=[stage_skill], callback_handler=None)
        current_task_work().strands = strands_parts(parent)
        return await parent.invoke_async("Stage the reviewed skill.")

    result = await run_turn(agent, TASK_ID, turn)
    return plans, result


class LoopBoundModel(Model):
    """A model whose client belongs to the event loop of its first call, as a pooled async HTTP client does.

    It asks once for the staging tool, answers reviews with a complete
    decision, and fails on any other loop than its first.
    """

    def __init__(self) -> None:
        self.loops: list[asyncio.AbstractEventLoop] = []

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "loop-bound-review"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("Use the SDK formatting tool, not a second provider path")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        loop = asyncio.get_running_loop()
        if self.loops and loop is not self.loops[0]:
            raise RuntimeError("This model's client is bound to a different event loop")
        self.loops.append(loop)
        yield {"messageStart": {"role": "assistant"}}
        if DECISION_TOOL in [spec["name"] for spec in tool_specs or []]:
            name, arguments = DECISION_TOOL, json.dumps(_decision(messages[0]["content"][0]["text"]))
        elif not any("toolResult" in block for message in messages for block in message["content"]):
            name, arguments = STAGE_TOOL, "{}"
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": f"call-{len(self.loops)}"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": arguments}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


@pytest.mark.asyncio
async def test_a_model_bound_to_the_turns_loop_reviews_from_a_strands_turns_sync_tool(installation):
    agent, service = installation
    model = LoopBoundModel()
    agent._strands_model = model

    plans, result = await _stage_from_a_strands_turn(agent, service, model)

    assert str(result.stop_reason) == "end_turn"
    assert [plan["semantic_review"]["status"] for plan in plans] == ["completed"]
    assert len(model.loops) == 3, "the turn's call, the review, and the turn's answer"
    assert agent._owned_tasks == set()


@pytest.mark.asyncio
async def test_the_factorys_anthropic_client_reviews_twice_in_one_turn_with_no_retry(installation, broker, caplog):
    agent, service = installation
    provider = FakeProvider(stagings=2)
    server = TestServer(provider.build_app())
    await server.start_server()
    model = build_strands_model(StrandsModelFactoryConfig(provider="anthropic", model_id=ANTHROPIC_MODEL))
    model.update_config(max_tokens=256)  # the factory sets none (module docstring)
    model.client.base_url = str(server.make_url("")).rstrip("/")
    agent._strands_model = model
    caplog.set_level(logging.DEBUG, logger="anthropic")

    async def open_three_idle_connections() -> None:
        async def warm() -> None:
            async for _event in model.stream([{"role": "user", "content": [{"text": "warm"}]}]):
                pass

        await asyncio.gather(warm(), warm(), warm())
        assert len(provider.peers) == 3

    try:
        plans, result = await _stage_from_a_strands_turn(agent, service, model, before=open_three_idle_connections)
    finally:
        await server.close()

    assert str(result.stop_reason) == "end_turn"
    assert [plan["semantic_review"]["status"] for plan in plans] == ["completed", "completed"]
    reviews = [request for request in provider.requests if request["review"]]
    assert len(reviews) == 2
    assert len(provider.requests) == 3 + 3 + 2, "three warm-ups, three turn steps and two reviews: nothing retried"
    assert "Retrying request" not in caplog.text
    assert {request["key"] for request in provider.requests} == {TEAM_KEY}
    assert set(broker.addresses) == {well_known_endpoint(EndpointKind.ANTHROPIC).address}
