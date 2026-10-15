from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from mcp.shared.message import SessionMessage

from access_control.identity import ContextVerifier
from custom_agents import access_runtime
from custom_agents.sdk.strands import mcp as strands_mcp
from mcp_control import sdk_compat
from mcp_control import transports as mcp_transports
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_verify_keys
from tests.change_approval_support import SignedTaskManager, use_signed_contexts


def _brokered_client():
    return strands_mcp.BrokeredMCPClient(
        connection_id="mcp_conn_test_1234",
        record_digest="a" * 64,
        tool_name_prefix="crawl4ai",
        tool_timeout_seconds=30,
        named_agent_type="WebResearchAgent",
    )


def _empty_discovery_response() -> dict:
    return {
        "schema_version": 1,
        "record_type": "mcp_discovery_response",
        "connection_id": "mcp_conn_test_1234",
        "record_digest": "a" * 64,
        "descriptor_sha256": "b" * 64,
        "operation_catalog_sha256": "c" * 64,
        "tools": [],
    }


@pytest.mark.asyncio
async def test_brokered_transport_fails_closed_in_agent_process() -> None:
    with pytest.raises(
        RuntimeError,
        match="Brokered MCP clients do not open transports in agent processes",
    ):
        async with strands_mcp._unreachable_brokered_transport():
            pytest.fail("brokered transport unexpectedly opened in the agent process")


def test_sse_transport_uses_server_url_environment_override(monkeypatch):
    observed = {}
    sentinel = object()

    def fake_sse_client(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return sentinel

    monkeypatch.setenv(
        "CRAWL4AI_MCP_SERVER_URL",
        "http://crawl4ai:11235/mcp/sse",
    )
    monkeypatch.setattr(mcp_transports, "_fail_fast_sse_client", fake_sse_client)

    transport = mcp_transports.build_mcp_transport(
        {
            "transport": "sse",
            "server_url": "http://127.0.0.1:11235/mcp/sse",
            "server_url_env": "CRAWL4AI_MCP_SERVER_URL",
            "headers": {"Authorization": "Bearer shared-token"},
            "timeout": 30,
            "sse_read_timeout": 300,
        }
    )

    assert transport() is sentinel
    assert observed == {
        "url": "http://crawl4ai:11235/mcp/sse",
        "headers": {"Authorization": "Bearer shared-token"},
        "timeout": 30.0,
        "sse_read_timeout": 300.0,
        "httpx_client_factory": mcp_transports.create_mcp_http_client,
    }


@pytest.mark.asyncio
async def test_pinned_network_backend_uses_only_attested_addresses() -> None:
    calls: list[tuple[str, int]] = []
    sentinel = object()

    class FakeBackend:
        async def connect_tcp(self, host, port, **_kwargs):
            calls.append((host, port))
            return sentinel

        async def sleep(self, _seconds):
            return None

    backend = mcp_transports._PinnedAsyncNetworkBackend(
        expected_host="mcp.example.test",
        expected_port=443,
        addresses=("203.0.113.10",),
    )
    backend._backend = FakeBackend()

    connected = await backend.connect_tcp("mcp.example.test", 443)
    assert connected is sentinel
    assert calls == [("203.0.113.10", 443)]

    with pytest.raises(mcp_transports.httpcore.ConnectError, match="unapproved origin"):
        await backend.connect_tcp("rebound.example.test", 443)


@pytest.mark.asyncio
async def test_pinned_http_transport_rejects_cross_origin_redirect_target() -> None:
    transport = mcp_transports._PinnedAsyncHTTPTransport(
        expected_scheme="https",
        expected_host="mcp.example.test",
        expected_port=443,
        addresses=("203.0.113.10",),
    )
    try:
        with pytest.raises(httpx.ConnectError, match="unapproved origin"):
            await transport.handle_async_request(
                httpx.Request("GET", "https://other.example.test/mcp")
            )
    finally:
        await transport.aclose()


def test_pinned_sse_transport_uses_address_pinning_factory(monkeypatch) -> None:
    observed = {}
    sentinel = object()

    def fake_sse_client(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return sentinel

    monkeypatch.setattr(mcp_transports, "_fail_fast_sse_client", fake_sse_client)
    transport = mcp_transports.build_mcp_transport(
        {
            "transport": "sse",
            "server_url": "https://mcp.example.test/sse",
            "mcp_pinned_addresses": ["203.0.113.10"],
        }
    )

    assert transport() is sentinel
    assert observed["url"] == "https://mcp.example.test/sse"
    assert observed["httpx_client_factory"] is not mcp_transports.create_mcp_http_client


class _EndpointStream(httpx.AsyncByteStream):
    """Keep a mock SSE connection open after advertising its POST endpoint."""

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def __aiter__(self):
        yield b"event: endpoint\ndata: /messages/?session_id=test-session\n\n"
        await self._closed.wait()

    async def aclose(self) -> None:
        self._closed.set()


@pytest.mark.asyncio
async def test_sse_post_504_is_delivered_to_the_pending_session():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_EndpointStream(),
            )
        return httpx.Response(504, request=request)

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    request = SessionMessage(
        sdk_compat.jsonrpc_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "screenshot", "arguments": {}},
            }
        )
    )

    async with mcp_transports._fail_fast_sse_client(
        "https://crawl.example.test/mcp/sse",
        httpx_client_factory=client_factory,
    ) as (read_stream, write_stream):
        await write_stream.send(request)
        failure = await asyncio.wait_for(read_stream.receive(), timeout=1)

    assert isinstance(failure, httpx.HTTPStatusError)
    assert failure.response.status_code == 504


class _MockMcpSessionStream(httpx.AsyncByteStream):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def __aiter__(self):
        yield (
            f"event: endpoint\ndata: /messages/?session_id={self.session_id}\n\n"
        ).encode()
        while (message := await self.messages.get()) is not None:
            yield message

    async def aclose(self) -> None:
        self.messages.put_nowait(None)

    def respond(self, payload: dict) -> None:
        message = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
        self.messages.put_nowait(message)


class _MockMcpSseService:
    def __init__(
        self,
        *,
        fail_first_tool_call: bool = False,
        hang_tool_calls: bool = False,
    ) -> None:
        self.fail_first_tool_call = fail_first_tool_call
        self.hang_tool_calls = hang_tool_calls
        self.tool_call_count = 0
        self.session_count = 0
        self.sessions: dict[str, _MockMcpSessionStream] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.session_count += 1
            session_id = f"session-{self.session_count}"
            stream = _MockMcpSessionStream(session_id)
            self.sessions[session_id] = stream
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        session_id = request.url.params["session_id"]
        stream = self.sessions[session_id]
        message = json.loads(request.content)
        method = message.get("method")

        if method == "tools/call":
            self.tool_call_count += 1
            if self.fail_first_tool_call and self.tool_call_count == 1:
                return httpx.Response(504, request=request)
            if self.hang_tool_calls:
                return httpx.Response(202, request=request)

        response = self._response_for(message)
        if response is not None:
            stream.respond(response)
        return httpx.Response(202, request=request)

    @staticmethod
    def _response_for(message: dict) -> dict | None:
        request_id = message.get("id")
        if request_id is None:
            return None
        method = message["method"]
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {},
                    "serverInfo": {"name": "mock-crawl4ai", "version": "0.9.2"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "screenshot",
                            "description": "Capture a screenshot",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        if method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "screenshot ready"}],
                    "isError": False,
                },
            }
        raise AssertionError(f"Unexpected MCP method: {method}")

    def client_factory(self, **kwargs):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self),
            **kwargs,
        )


@pytest.mark.parametrize(
    "value",
    [0, -1, float("inf"), float("nan"), "not-a-number", None],
)
def test_tool_timeout_must_be_a_positive_finite_number(value):
    with pytest.raises(ValueError, match="positive number"):
        strands_mcp._tool_timeout_seconds({"tool_timeout_seconds": value})


def _web_research_agent(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """A WebResearchAgent whose task a person asked for through the gateway (signed context)."""

    use_signed_contexts(monkeypatch)
    return SimpleNamespace(
        id="agent_web",
        team_id="team_ua",
        is_team_lead=False,
        named_agent_type="WebResearchAgent",
        authorization_config={"enabled": False},
        task_manager=SignedTaskManager(
            team_id="team_ua",
            person_id="user_ach",
            storage_settings=resolve_storage_settings(
                {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}}
            ),
            roles=("member",),
            task_id="task_mcp",
        ),
    )


@pytest.fixture
def _forget_cached_access():
    yield
    access_runtime._runtime_access.cache_clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_forget_cached_access")
async def test_brokered_invocation_names_the_agent_and_its_signed_person(
    tmp_path: Path, monkeypatch
) -> None:
    """The broker decides the call from the agent and the person in the signed task context."""

    observed = {}
    client = _brokered_client()
    agent = _web_research_agent(tmp_path, monkeypatch)

    async def fake_post(suffix, payload):
        observed["suffix"] = suffix
        observed["payload"] = payload
        return {
            "schema_version": 1,
            "record_type": "mcp_invocation_response",
            "result": {"content": [{"type": "text", "text": "answer"}], "isError": False},
        }

    monkeypatch.setattr(strands_mcp, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(client, "_handle_tool_result", lambda tool_use_id, _result: {"toolUseId": tool_use_id})

    result = await client.call_tool_async("call-1", "ask", {"query": "status"})

    payload = observed["payload"]
    assert observed["suffix"] == "invoke"
    assert result == {"toolUseId": "call-1"}
    assert payload["task_id"] == "task_mcp"
    assert payload["principal"] == {
        "principal_type": "Agent",
        "principal_id": "agent_web",
        "on_behalf_of_user_id": "user_ach",
        "team_id": "team_ua",
        "roles": ["worker"],
        "named_agent_type": "WebResearchAgent",
        "attributes": {},
    }
    verified = ContextVerifier.from_value(context_verify_keys()).verify(payload["task_context"], team_id="team_ua")
    assert verified.person_id == "user_ach"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_forget_cached_access")
async def test_brokered_invocation_hashes_long_provider_id_but_returns_original(
    tmp_path: Path, monkeypatch
) -> None:
    observed = {}
    client = _brokered_client()
    agent = _web_research_agent(tmp_path, monkeypatch)
    original_id = "call_123__thought__" + "signed-provider-payload" * 24

    async def fake_post(suffix, payload):
        observed["suffix"] = suffix
        observed["payload"] = payload
        return {
            "schema_version": 1,
            "record_type": "mcp_invocation_response",
            "result": {
                "content": [{"type": "text", "text": "answer"}],
                "isError": False,
            },
        }

    monkeypatch.setattr(strands_mcp, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "_handle_tool_result",
        lambda tool_use_id, _result: {"toolUseId": tool_use_id},
    )

    result = await client.call_tool_async(original_id, "ask", {"query": "status"})

    expected_alias = strands_mcp._broker_tool_use_id(original_id)
    assert observed["suffix"] == "invoke"
    assert observed["payload"]["tool_use_id"] == expected_alias
    assert expected_alias.startswith("mcp-tool-use:sha256:")
    assert len(expected_alias) <= 240
    assert result == {"toolUseId": original_id}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_forget_cached_access")
async def test_brokered_invocation_error_keeps_original_long_provider_id(
    tmp_path: Path, monkeypatch
) -> None:
    client = _brokered_client()
    agent = _web_research_agent(tmp_path, monkeypatch)
    original_id = "call_456__thought__" + "provider-signature" * 24

    async def fake_post(_suffix, _payload):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(strands_mcp, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "_handle_tool_execution_error",
        lambda tool_use_id, error: {
            "toolUseId": tool_use_id,
            "error": str(error),
        },
    )

    result = await client.call_tool_async(original_id, "ask", {})

    assert result == {
        "toolUseId": original_id,
        "error": "broker unavailable",
    }


@pytest.mark.asyncio
async def test_brokered_invocation_without_an_agent_is_refused_before_the_broker(monkeypatch) -> None:
    client = _brokered_client()
    posted = []

    async def fake_post(suffix, payload):
        posted.append((suffix, payload))
        return {}

    monkeypatch.setattr(strands_mcp, "get_agent_ref", lambda: None)
    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client, "_handle_tool_execution_error", lambda tool_use_id, error: {"toolUseId": tool_use_id, "error": str(error)}
    )

    result = await client.call_tool_async("call-1", "ask", {})

    assert posted == []
    assert "no runtime agent context" in result["error"]


def test_broker_tool_use_id_preserves_short_ids_and_rejects_empty_ids() -> None:
    assert strands_mcp._broker_tool_use_id(" call-short ") == "call-short"
    with pytest.raises(ValueError, match="requires a tool_use_id"):
        strands_mcp._broker_tool_use_id("   ")


@pytest.mark.asyncio
@pytest.mark.parametrize("with_task", [False, True])
@pytest.mark.usefixtures("_forget_cached_access")
async def test_brokered_discovery_names_only_the_connection_version_and_agent_type(
    tmp_path: Path, monkeypatch, with_task: bool
) -> None:
    """Listing tools needs no person or task: the broker checks the process and the attachment."""

    observed = {}
    client = _brokered_client()
    agent = _web_research_agent(tmp_path, monkeypatch) if with_task else None

    async def fake_post(suffix, payload):
        observed["suffix"] = suffix
        observed["payload"] = payload
        return _empty_discovery_response()

    monkeypatch.setattr(strands_mcp, "get_agent_ref", lambda: agent)
    monkeypatch.setattr(client, "_post", fake_post)

    assert await client.load_tools() == []
    assert observed == {
        "suffix": "tools",
        "payload": {
            "schema_version": 1,
            "record_type": "mcp_discovery_request",
            "record_digest": "a" * 64,
            "named_agent_type": "WebResearchAgent",
        },
    }


@pytest.mark.asyncio
async def test_a_failed_brokered_discovery_raises_for_the_loader_to_classify(monkeypatch) -> None:
    """Nothing is swallowed: custom_agents.mcp_server_tools decides whether and when to try again."""

    client = _brokered_client()
    request = httpx.Request("POST", "http://127.0.0.1:18600/v1/mcp/connections/test/tools")
    response = httpx.Response(502, text="Brokered MCP discovery failed.", request=request)

    async def fake_post(_suffix, _payload):
        raise httpx.HTTPStatusError("discovery failed", request=request, response=response)

    monkeypatch.setattr(client, "_post", fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        await client.load_tools()
    assert client._loaded_tools is None, "no empty list is cached, so the next try asks the broker again"


def test_websocket_transport_uses_environment_url_and_auth_headers(monkeypatch):
    observed = {}
    sentinel = object()

    def fake_websocket_client(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return sentinel

    monkeypatch.setenv(
        "CRAWL4AI_MCP_SERVER_URL",
        "wss://crawl.example.test/mcp/ws",
    )
    monkeypatch.setattr(
        mcp_transports,
        "_websocket_client",
        fake_websocket_client,
    )

    transport = mcp_transports.build_mcp_transport(
        {
            "transport": "websocket",
            "server_url_env": "CRAWL4AI_MCP_SERVER_URL",
            "headers": {"Authorization": "Bearer shared-token"},
            "timeout": 45,
        }
    )

    assert transport() is sentinel
    assert observed == {
        "url": "wss://crawl.example.test/mcp/ws",
        "headers": {"Authorization": "Bearer shared-token"},
        "open_timeout": 45.0,
        "max_message_bytes": 64 * 1024 * 1024,
        "pinned_addresses": (),
    }


def test_shipped_crawl4ai_mcp_server_takes_its_token_from_a_linked_account():
    config = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    servers = config["agent"]["external_mcp_servers"]

    assert config["mcp"]["network_policy"] == {
        "allow_loopback": False,
        "allow_private_networks": False,
        "allowed_private_hosts": ["crawl4ai"],
    }
    assert servers == [
        {
            "name": "crawl4ai",
            "transport": "sse",
            "server_url_env": "CRAWL4AI_MCP_SERVER_URL",
            # The token is the connection's linked account (/link mcp:crawl4ai team),
            # never an environment variable.
            "credential_injections": [
                {
                    "target_kind": "header",
                    "target_name": "Authorization",
                    "field_name": "api_token",
                    "template": "Bearer {value}",
                }
            ],
            "tool_name_prefix": "crawl4ai",
            "startup_timeout": 60,
            "timeout": 30,
            "sse_read_timeout": 300,
            "tool_timeout_seconds": 300,
            "max_message_bytes": 67108864,
        }
    ]


def _brokered_profile() -> dict:
    return {
        "name": "reports",
        "transport": "streamable_http",
        "tool_name_prefix": "reports",
        "mcp_brokered": True,
        "mcp_connection_id": "mcp_conn_reports_0123456789abcdef",
        "mcp_connection_record_digest": "a" * 64,
        "mcp_target_agent_type": "WebResearchAgent",
    }


@pytest.mark.asyncio
async def test_a_brokered_client_names_its_tools_with_the_server_prefix(monkeypatch) -> None:
    """Whether a call may run is decided per call, so the client carries no rules."""

    client = strands_mcp.brokered_mcp_client(_brokered_profile())
    response = _empty_discovery_response() | {
        "tools": [{"name": "search", "inputSchema": {"type": "object"}, "operation_digest": "d" * 64}]
    }

    async def fake_post(_suffix, _payload):
        return response

    monkeypatch.setattr(client, "_post", fake_post)

    assert isinstance(client, strands_mcp.BrokeredMCPClient)
    assert client.ua_connection_id == "mcp_conn_reports_0123456789abcdef"
    assert [tool.tool_name for tool in await client.load_tools()] == ["reports_search"]


@pytest.mark.parametrize("missing", ["mcp_connection_record_digest", "mcp_connection_id", "name", "mcp_brokered"])
def test_a_brokered_client_needs_its_installed_identity(missing: str) -> None:
    profile = _brokered_profile()
    del profile[missing]

    with pytest.raises(ValueError, match="missing its installed identity"):
        strands_mcp.brokered_mcp_client(profile)


def test_mcp_attachment_metadata_is_not_forwarded_to_transport() -> None:
    profile = {
        "transport": "streamable_http",
        "server_url": "https://example.test/rpc",
        "attachment": {"approver_class": "user", "risk": "high"},
    }

    stripped = strands_mcp.strip_mcp_attachment_metadata(profile)
    [resolved] = strands_mcp.resolve_mcp_profile_configs(["research"], {"research": profile})

    assert "attachment" not in stripped
    assert stripped["server_url"] == profile["server_url"]
    assert "attachment" not in resolved
    assert resolved["name"] == "research"
