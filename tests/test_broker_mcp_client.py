"""Exercise the broker with the real MCP/AnyIO session and a controlled SSE peer."""

import asyncio
from builtins import ExceptionGroup
import json

from aiohttp import web
from aiohttp.test_utils import TestServer
import httpx
from mcp_control.sdk_compat import MCPError, is_error
import pytest

from mcp_control import transports
from secure_tool_broker.mcp_client import BrokerMCPClient
from tests.test_strands_mcp import _MockMcpSseService


def client_for(monkeypatch, service):
    monkeypatch.setattr(
        "secure_tool_broker.mcp_client.build_mcp_transport",
        lambda _config: (
            lambda: transports._fail_fast_sse_client(
                "https://crawl.example.test/mcp/sse",
                httpx_client_factory=service.client_factory,
            )
        ),
    )
    return BrokerMCPClient({"tool_timeout_seconds": 1})


@pytest.mark.asyncio
async def test_discovery_then_504_in_another_request_closes_and_reconnects(monkeypatch):
    service = _MockMcpSseService(fail_first_tool_call=True)
    client = client_for(monkeypatch, service)
    try:
        tools = await asyncio.create_task(client.list_tools())
        assert tools[0].name == "screenshot"
        with pytest.raises(MCPError):
            await asyncio.create_task(client.call_tool("screenshot", {}))
        assert service.tool_call_count == 1, "Never replay a failed side effect"
        result = await asyncio.create_task(client.call_tool("screenshot", {}))
        assert is_error(result) is False
        assert service.session_count == 2
        assert service.tool_call_count == 2
    finally:
        await client.close()


class _WaitingService(_MockMcpSseService):
    def __init__(self, method):
        super().__init__()
        self.method = method
        self.waiting = asyncio.Event()

    async def __call__(self, request):
        if (
            request.method == "POST"
            and json.loads(request.content).get("method") == self.method
        ):
            self.waiting.set()
            return httpx.Response(202, request=request)
        return await super().__call__(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["initialize", "tools/call"])
async def test_cancelled_request_closes_session_in_its_owner_task(monkeypatch, method):
    service = _WaitingService(method)
    client = client_for(monkeypatch, service)
    task = asyncio.create_task(client.call_tool("screenshot", {}))
    await asyncio.wait_for(service.waiting.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()
    assert not client._owners
    # A subsequent explicit call may reconnect; the cancelled call is not replayed.
    service.method = "never"
    try:
        assert not is_error(await client.call_tool("screenshot", {}))
        assert service.session_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tool_timeout_discards_session_without_replaying(monkeypatch):
    service = _MockMcpSseService(hang_tool_calls=True)
    client = client_for(monkeypatch, service)
    try:
        with pytest.raises(MCPError, match="Timed out"):
            await asyncio.wait_for(client.call_tool("screenshot", {}), 3)
        assert service.tool_call_count == 1
        assert not client._owners
        service.hang_tool_calls = False
        assert not is_error(await client.call_tool("screenshot", {}))
        assert service.session_count == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_initialization_does_not_poison_the_next_request(monkeypatch):
    service = _MockMcpSseService()
    unauthorized = True

    async def respond(request):
        if unauthorized:
            return httpx.Response(401, request=request)
        return await service(request)

    monkeypatch.setattr(
        service,
        "client_factory",
        lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    client = client_for(monkeypatch, service)
    try:
        with pytest.raises(ExceptionGroup) as caught:
            await asyncio.create_task(client.list_tools())
        assert "different task" not in str(caught.value)
        assert not client._owners
        unauthorized = False
        assert (await client.list_tools())[0].name == "screenshot"
    finally:
        await client.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_http_504_after_discovery_has_no_cross_task_cleanup_error(caplog):
    queues = {}
    executed = []

    async def sse(request):
        session_id = str(len(queues) + 1)
        queue = queues[session_id] = asyncio.Queue()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            f"event: endpoint\ndata: /messages?session_id={session_id}\n\n".encode()
        )
        try:
            while request.transport is not None and not request.transport.is_closing():
                try:
                    message = await asyncio.wait_for(queue.get(), 0.1)
                except TimeoutError:
                    continue
                await response.write(
                    f"event: message\ndata: {json.dumps(message)}\n\n".encode()
                )
        except ConnectionError:
            pass
        return response

    async def post(request):
        message = await request.json()
        if message.get("method") == "tools/call":
            # A timeout cannot tell the client whether a side effect happened.
            executed.append(message["params"]["name"])
            if len(executed) == 1:
                return web.Response(status=504)
        result = _MockMcpSseService._response_for(message)
        if result is not None:
            await queues[request.query["session_id"]].put(result)
        return web.Response(status=202)

    app = web.Application()
    app.router.add_get("/sse", sse)
    app.router.add_post("/messages", post)
    async with TestServer(app) as server:
        client = BrokerMCPClient(
            {
                "transport": "sse",
                "server_url": str(server.make_url("/sse")),
                "tool_timeout_seconds": 3,
            }
        )
        try:
            await asyncio.create_task(client.list_tools())
            with pytest.raises(MCPError):
                await asyncio.create_task(client.call_tool("screenshot", {}))
            assert executed == ["screenshot"]
            assert len(queues) == 1
            assert not is_error(await asyncio.create_task(client.call_tool("screenshot", {})))
            assert len(queues) == 2
            assert executed == ["screenshot", "screenshot"]
        finally:
            await asyncio.create_task(client.close())
    assert "504 Gateway Timeout" in caplog.text
    assert "different task" not in caplog.text
    assert "mcp_session_owner_failed" not in caplog.text


@pytest.mark.asyncio
async def test_late_failure_does_not_close_a_replacement_session(monkeypatch):
    service = _MockMcpSseService(fail_first_tool_call=True)
    client = client_for(monkeypatch, service)
    await client.list_tools()
    delayed = asyncio.Event()
    release = asyncio.Event()
    discard = client._discard

    async def delay_old_failure(owner):
        if asyncio.current_task().get_name() == "late-failure":
            delayed.set()
            await release.wait()
        await discard(owner)

    monkeypatch.setattr(client, "_discard", delay_old_failure)
    late = asyncio.create_task(client.call_tool("screenshot", {}), name="late-failure")
    other = asyncio.create_task(client.call_tool("screenshot", {}))
    try:
        with pytest.raises(Exception):
            await asyncio.wait_for(other, 3)
        await asyncio.wait_for(delayed.wait(), 3)
        assert not is_error(await client.call_tool("screenshot", {}))
        assert service.session_count == 2
        release.set()
        with pytest.raises(Exception):
            await late
        assert not is_error(await client.call_tool("screenshot", {}))
        assert service.session_count == 2, "Late cleanup must leave the replacement open"
    finally:
        release.set()
        await asyncio.gather(late, other, return_exceptions=True)
        await client.close()
