"""A model call that stops producing output ends instead of hanging.

Reproduces the reported hang: an OpenAI reasoning model reached through a
LiteLLM Responses endpoint streamed a few reasoning summaries, then sent
nothing more while the connection stayed open. The HTTP client's read timeout
only notices a connection with no bytes at all, and proxy keep-alive pings
reset it, so the call never ended.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from strands import Agent
from strands.models.model import Model

from tests.model_key_support import env_model_keys
from custom_agents.model_stream_watchdog import (
    ModelStreamLimits,
    guard_model_stream,
    watch_model_stream,
)
from custom_agents.openai_reasoning_responses_model import OpenAIReasoningResponsesModel
from custom_agents.provider_error_runtime import (
    PROVIDER_TIMEOUT_USER_MESSAGE,
    ModelStreamTimeoutError,
    ProviderTimeoutError,
    is_provider_failover_eligible_error,
    is_provider_timeout_error,
)
from custom_agents.sdk.strands.turn import _TurnState
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.tool_stop import ToolStop
from tests.llm_turn_fixtures import turn_executor_for

_FAST = ModelStreamLimits(idle_timeout_seconds=0.3, call_timeout_seconds=5.0)


class _Source:
    """A provider stream double that records whether it was closed."""

    def __init__(self, events: list[dict], *, then: str = "stop", gap: float = 0.0) -> None:
        self.events = events
        self.then = then
        self.gap = gap
        self.closed = False

    async def stream(self) -> AsyncIterator[dict]:
        try:
            for event in self.events:
                yield event
            if self.then == "stall":
                await asyncio.Event().wait()
            elif self.then == "trickle":
                while True:
                    await asyncio.sleep(self.gap)
                    yield {"contentBlockDelta": {"delta": {"text": "."}}}
            elif self.then == "read_timeout":
                raise httpx.ReadTimeout("")
        finally:
            self.closed = True


async def _collect(stream: AsyncIterator[dict]) -> list[dict]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_stalled_stream_ends_at_idle_limit_and_is_a_provider_timeout(caplog):
    source = _Source([{"messageStart": {"role": "assistant"}}], then="stall")
    received: list[dict] = []
    started = time.monotonic()

    with caplog.at_level(logging.WARNING), pytest.raises(ModelStreamTimeoutError) as raised:
        async for event in watch_model_stream(source.stream(), _FAST, model_id="gpt-test"):
            received.append(event)

    assert received == [{"messageStart": {"role": "assistant"}}]
    assert 0.25 < time.monotonic() - started < 2.0
    assert raised.value.limit == "idle" and raised.value.events_received == 1
    assert source.closed, "The provider stream must be closed so its connection is released."
    assert is_provider_timeout_error(raised.value)
    assert is_provider_failover_eligible_error(raised.value)
    assert "413" not in str(raised.value)
    log = [record.getMessage() for record in caplog.records]
    assert any("event=model_stream_timeout limit=idle" in line and "model_id=gpt-test" in line
               for line in log)


@pytest.mark.asyncio
async def test_stream_that_keeps_trickling_ends_at_call_limit():
    source = _Source([], then="trickle", gap=0.05)
    limits = ModelStreamLimits(idle_timeout_seconds=1.0, call_timeout_seconds=0.4)

    with pytest.raises(ModelStreamTimeoutError) as raised:
        await _collect(watch_model_stream(source.stream(), limits, model_id="m"))

    assert raised.value.limit == "call" and raised.value.events_received > 3
    assert source.closed


@pytest.mark.asyncio
async def test_http_read_timeout_inside_the_stream_is_reported_as_a_timeout():
    source = _Source([{"messageStart": {"role": "assistant"}}], then="read_timeout")

    with pytest.raises(ModelStreamTimeoutError) as raised:
        await _collect(watch_model_stream(source.stream(), _FAST, model_id="m"))

    assert raised.value.limit == "read"
    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)
    assert is_provider_timeout_error(raised.value)


@pytest.mark.asyncio
async def test_healthy_stream_passes_through_unchanged(caplog):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]
    source = _Source(events)

    with caplog.at_level(logging.WARNING):
        assert await _collect(watch_model_stream(source.stream(), _FAST, model_id="m")) == events

    assert source.closed and not caplog.records


@pytest.mark.asyncio
async def test_a_consumer_that_stops_early_closes_the_provider_stream():
    source = _Source([{"messageStart": {}}, {"messageStop": {}}], then="stall")
    watched = watch_model_stream(source.stream(), _FAST, model_id="m")

    assert await anext(watched) == {"messageStart": {}}
    await watched.aclose()

    assert source.closed


def test_limits_must_be_positive():
    with pytest.raises(ValueError):
        ModelStreamLimits(idle_timeout_seconds=0, call_timeout_seconds=1)


def test_factory_guards_every_model_once_and_keeps_its_class():
    config = StrandsModelFactoryConfig(
        provider="litellm", model_id="gpt-5.5", litellm_api_mode="responses",
        litellm_api_base="http://127.0.0.1:9", model_keys=env_model_keys("sk-test"),
    )
    model = build_strands_model(config)
    guarded_stream = model.stream

    assert isinstance(model, OpenAIReasoningResponsesModel)
    assert guard_model_stream(model, _FAST).stream is guarded_stream


# --- A real Responses stream from a local server --------------------------------------

_RESPONSE = {
    "id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-5.5",
    "status": "in_progress", "output": [], "parallel_tool_calls": True,
    "tool_choice": "auto", "tools": [],
}


def _sse(event_type: str, sequence: int, **payload) -> bytes:
    body = {"type": event_type, "sequence_number": sequence, **payload}
    return f"event: {event_type}\ndata: {json.dumps(body)}\n\n".encode()


class _ResponsesServer:
    """A local Responses endpoint that finishes, stalls, or stalls with pings."""

    def __init__(self) -> None:
        self.mode = "complete"
        self.first_event_delay = 0.0
        self.ping_sent = asyncio.Event()
        self.client_closed = asyncio.Event()
        self.stopping = asyncio.Event()
        self.base_url = ""

    async def handle(self, request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(self.first_event_delay)
        response = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_sse("response.created", 1, response=_RESPONSE))
        await response.write(_sse(
            "response.reasoning_summary_text.delta", 2, item_id="rs_1", output_index=0,
            summary_index=0, delta="**Verifying Gmail capabilities**",
        ))
        if self.mode == "complete":
            await response.write(_sse(
                "response.output_text.delta", 3, item_id="msg_1", output_index=1,
                content_index=0, delta="All set.",
            ))
            await response.write(_sse(
                "response.completed", 4, response={**_RESPONSE, "status": "completed"},
            ))
            return response
        try:
            while not self.stopping.is_set():
                await asyncio.sleep(0.05)
                if self.mode == "comment_ping":
                    await response.write(b": ping\n\n")
                elif self.mode == "event_ping":
                    await response.write(b'event: ping\ndata: {"type": "ping"}\n\n')
                if self.mode in {"comment_ping", "event_ping"}:
                    self.ping_sent.set()
        except (ConnectionResetError, asyncio.CancelledError):
            self.client_closed.set()
            raise
        return response


@pytest_asyncio.fixture
async def responses_server():
    server = _ResponsesServer()
    app = web.Application()
    app.router.add_post("/v1/responses", server.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    server.base_url = f"http://127.0.0.1:{port}/v1"
    try:
        yield server
    finally:
        server.stopping.set()
        await runner.cleanup()


def _litellm_responses_model(base_url: str, *, request_timeout: float = 30.0):
    return build_strands_model(StrandsModelFactoryConfig(
        provider="litellm", model_id="gpt-5.5", litellm_api_mode="responses",
        reasoning_summary="auto", litellm_api_base=base_url, model_keys=env_model_keys("sk-test"),
        request_timeout_seconds=request_timeout,
        stream_idle_timeout_seconds=0.5, model_call_timeout_seconds=10.0,
    ))


_USER = [{"role": "user", "content": [{"text": "Set up Gmail drafts."}]}]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["comment_ping", "event_ping"])
@pytest.mark.parametrize("first_event_delay", [0.0, 0.75], ids=["ready", "slow-first-event"])
async def test_keepalive_pings_no_longer_keep_a_stalled_responses_call_open(
    responses_server, mode, first_event_delay
):
    responses_server.mode = mode
    responses_server.first_event_delay = first_event_delay
    model = _litellm_responses_model(responses_server.base_url)
    # The short idle limit tests a stalled response after reasoning arrived.
    # Cold client setup and initial response parsing can take longer than it.
    # Keep the real adapter and key binding, but start this phase's watchdog
    # after that setup. The other integrations below exercise model.stream's
    # factory-installed wrapper for complete responses and read timeouts.
    stream = model.stream.__wrapped__(_USER)
    try:
        async with asyncio.timeout(10):
            while True:
                event = await anext(stream)
                if "reasoningContent" in str(event):
                    break
            await responses_server.ping_sent.wait()
            with pytest.raises(ModelStreamTimeoutError) as raised:
                await _collect(watch_model_stream(
                    stream,
                    ModelStreamLimits(idle_timeout_seconds=0.5, call_timeout_seconds=5.0),
                    model_id="gpt-5.5",
                ))

            assert raised.value.limit == "idle"
            assert raised.value.limit_seconds == 0.5
            await asyncio.wait_for(responses_server.client_closed.wait(), 3)
    finally:
        await stream.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_silent_responses_stream_is_a_classified_timeout(responses_server):
    responses_server.mode = "silent"
    model = _litellm_responses_model(responses_server.base_url, request_timeout=0.3)

    with pytest.raises(ModelStreamTimeoutError) as raised:
        await _collect(model.stream(_USER))

    # Before the fix this surfaced as a bare httpx.ReadTimeout('') that the
    # provider error classifier did not treat as a timeout.
    assert raised.value.limit == "read"
    assert is_provider_timeout_error(raised.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_complete_responses_stream_is_unchanged(responses_server):
    events = await _collect(
        _litellm_responses_model(responses_server.base_url).stream(_USER)
    )

    text = "".join(
        event["contentBlockDelta"]["delta"].get("text", "")
        for event in events
        if "contentBlockDelta" in event
    )
    assert text == "All set."
    assert events[-1]["messageStop"]["stopReason"] == "end_turn"


# --- The turn runtime treats a stopped call as a provider timeout ---------------------


class _StalledProvider(Model):
    """A Strands provider that starts a response and then sends nothing more."""

    def update_config(self, **config):
        pass

    def get_config(self):
        return {"model_id": "gpt-5.5"}

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("No structured output expected")
        yield

    async def stream(self, *args, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        await asyncio.Event().wait()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_turn_ends_a_stalled_model_call_with_the_provider_timeout_message(caplog):
    sdk = Agent(model=guard_model_stream(_StalledProvider(), _FAST), callback_handler=None)
    subject = SimpleNamespace(
        id="lead", model_provider="litellm", model_id="gpt-5.5",
        litellm_api_mode="responses", reasoning_summary=None,
        llm_failover={"enabled": False}, _strands_agent=sdk,
        _logger=logging.getLogger("stalled_turn_test"),
    )
    task = Task(task_id="task_stalled", task_type=MessageMetaType.REQUEST,
                problem_statement="Draft the Gmail message.")
    turn = _TurnState(task, MessageHeader(), MessageMeta())
    turn.full_prompt, turn.prompt_content = "Draft the Gmail message.", task
    turn.active_task_id, turn.span_attributes = task.task_id, {}
    turn.context_request = turn.assembly = None
    turn.invoke_kwargs = {"invocation_state": {}}
    turn.runtime_instruction_parts = []
    turn.tool_stop = ToolStop(agent_id="lead", task_id=task.task_id)  # set by _prepare_turn

    with caplog.at_level(logging.WARNING), pytest.raises(ProviderTimeoutError) as raised:
        await asyncio.wait_for(turn_executor_for(subject)._invoke_model(turn), 5)

    assert str(raised.value) == PROVIDER_TIMEOUT_USER_MESSAGE
    assert isinstance(raised.value.__cause__, ModelStreamTimeoutError)
    assert any("event=model_stream_timeout" in record.getMessage() for record in caplog.records)
