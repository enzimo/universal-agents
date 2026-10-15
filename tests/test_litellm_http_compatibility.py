"""Exercise the installed Strands/LiteLLM stack through a local HTTP provider."""

import json

from aiohttp import web
import pytest

from custom_agents.reasoning_litellm_model import ReasoningAwareLiteLLMModel


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_installed_litellm_provider_round_trip(streaming, unused_tcp_port):
    requests = []

    async def completion(request):
        body = await request.json()
        requests.append(body)
        base = {"id": "local-completion", "created": 1, "model": "local-test"}
        if not body.get("stream", False):
            return web.json_response(
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "hello"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for delta, finish in [
            ({"role": "assistant", "content": "hello"}, None),
            ({}, "stop"),
        ]:
            chunk = {
                **base,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completion)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
        model = ReasoningAwareLiteLLMModel(
            model_id="openai/local-test",
            client_args={
                "api_base": f"http://127.0.0.1:{unused_tcp_port}/v1",
                "api_key": "local-test",
            },
            params={"stream": streaming},
        )
        events = [
            event
            async for event in model.stream(
                [
                    {"role": "user", "content": [{"text": "hello"}]},
                ]
            )
        ]
        assert requests[0]["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ]
        assert requests[0].get("stream", False) is streaming
        assert (
            "".join(
                event.get("contentBlockDelta", {}).get("delta", {}).get("text", "")
                for event in events
            )
            == "hello"
        )
        assert any(
            event.get("messageStop", {}).get("stopReason") == "end_turn"
            for event in events
        )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_the_factory_model_sends_the_endpoints_key_and_explains_a_rejection(unused_tcp_port):
    """The real LiteLLM request carries the key looked up for its endpoint; a 401 becomes a person's message."""

    from custom_agents.model_keys import ModelKeyRejectedError
    from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
    from tests.model_key_support import env_model_keys

    authorizations = []

    async def completion(request):
        authorizations.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") != "Bearer sk-endpoint-key-000001":
            return web.json_response({"error": {"message": "Incorrect API key provided", "code": 401}}, status=401)
        return web.json_response(
            {
                "id": "c", "created": 1, "model": "local-test", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completion)
    runner = web.AppRunner(app)
    await runner.setup()
    base = f"http://127.0.0.1:{unused_tcp_port}/v1"
    messages = [{"role": "user", "content": [{"text": "hello"}]}]
    try:
        await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()

        def model(key):
            return build_strands_model(
                StrandsModelFactoryConfig(
                    provider="litellm", model_id="local-test", litellm_api_base=base, stream=False,
                    model_keys=env_model_keys(key),
                )
            )

        events = [event async for event in model("sk-endpoint-key-000001").stream(messages)]
        assert authorizations[-1] == "Bearer sk-endpoint-key-000001"
        assert any("messageStop" in event for event in events)

        with pytest.raises(ModelKeyRejectedError) as raised:
            [event async for event in model("sk-wrong-key-00000002").stream(messages)]
        assert f"127.0.0.1:{unused_tcp_port}" in str(raised.value)
        assert "LITELLM_API_KEY" in str(raised.value)
    finally:
        await runner.cleanup()
