"""SDK-neutral MCP v1 transports with origin and DNS pinning controls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlparse

import anyio
import httpcore
import httpx
from anyio.abc import TaskStatus
from httpx_sse import SSEError, aconnect_sse
from mcp import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

# MCP SDK 1.x only: the pinned HTTP clients below are httpx clients built for
# the SDK's 1.x client factory. The 2.x SDK takes httpx2 clients, so the move
# ports _PinnedAsyncHTTPTransport, _pinned_http_client_factory and the SSE
# reader to httpx2 (access plan 7e; TODO "mcp 2.x"). Other SDK names come from
# mcp_control.sdk_compat.
from mcp.shared._httpx_utils import McpHttpClientFactory, create_mcp_http_client

from mcp_control import sdk_compat
from websockets.asyncio.client import connect as websocket_connect
from websockets.typing import Subprotocol


LOGGER = logging.getLogger(__name__)


def _normalized_dns_name(value: str) -> str:
    return str(value or "").strip().rstrip(".").encode("idna").decode("ascii").lower()


class _PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect an approved origin only to its prevalidated address snapshot."""

    def __init__(
        self,
        *,
        expected_host: str,
        expected_port: int,
        addresses: tuple[str, ...],
    ) -> None:
        from httpcore._backends.auto import AutoBackend

        self._expected_host = _normalized_dns_name(expected_host)
        self._expected_port = int(expected_port)
        self._addresses = tuple(str(item) for item in addresses)
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        if (
            _normalized_dns_name(host) != self._expected_host
            or int(port) != self._expected_port
        ):
            raise httpcore.ConnectError(
                "MCP redirect or transport attempted an unapproved origin."
            )
        last_error: Exception | None = None
        for address in self._addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("MCP endpoint has no approved network address.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        raise httpcore.ConnectError("MCP remote transports cannot open Unix sockets.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport that preserves TLS SNI while pinning the TCP address."""

    def __init__(
        self,
        *,
        expected_scheme: str,
        expected_host: str,
        expected_port: int,
        addresses: tuple[str, ...],
    ) -> None:
        super().__init__(trust_env=False, retries=0)
        self._expected_origin = (
            str(expected_scheme).lower(),
            _normalized_dns_name(expected_host),
            int(expected_port),
        )
        # HTTPX does not expose a resolver hook. HTTP Core does, and retains the
        # original URL host for Host and TLS SNI while this backend pins TCP.
        self._pool._network_backend = _PinnedAsyncNetworkBackend(
            expected_host=expected_host,
            expected_port=expected_port,
            addresses=addresses,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_origin = (
            request.url.scheme.lower(),
            _normalized_dns_name(request.url.host),
            int(
                request.url.port
                or (443 if request.url.scheme.lower() == "https" else 80)
            ),
        )
        if request_origin != self._expected_origin:
            raise httpx.ConnectError(
                "MCP redirect attempted an unapproved origin.",
                request=request,
            )
        return await super().handle_async_request(request)


def _pinned_http_client_factory(
    *,
    endpoint: str,
    addresses: tuple[str, ...],
) -> McpHttpClientFactory:
    parsed = urlparse(endpoint)
    if not parsed.hostname:
        raise ValueError("Pinned MCP HTTP transport requires an endpoint host.")
    expected_port = parsed.port or (443 if parsed.scheme == "https" else 80)

    def create_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            auth=auth,
            follow_redirects=True,
            trust_env=False,
            transport=_PinnedAsyncHTTPTransport(
                expected_scheme=parsed.scheme,
                expected_host=parsed.hostname or "",
                expected_port=expected_port,
                addresses=addresses,
            ),
        )

    return create_client


@asynccontextmanager
async def _streamable_http_transport(
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
    sse_read_timeout: float,
    terminate_on_close: bool,
    httpx_client_factory: McpHttpClientFactory,
):
    client = httpx_client_factory(
        headers=headers,
        timeout=httpx.Timeout(timeout, read=sse_read_timeout),
    )
    async with client:
        async with streamable_http_client(
            url,
            http_client=client,
            terminate_on_close=terminate_on_close,
        ) as streams:
            yield streams


def _same_origin_endpoint(url: str, endpoint_path: str) -> str:
    """The POST endpoint an SSE server announces; it must share the connection's origin."""

    endpoint_url = urljoin(url, endpoint_path)
    source = urlparse(url)
    endpoint = urlparse(endpoint_url)
    if source.netloc != endpoint.netloc or source.scheme != endpoint.scheme:
        raise ValueError(
            "MCP SSE endpoint origin does not match "
            f"the connection origin: {endpoint_url}"
        )
    return endpoint_url


@asynccontextmanager
async def _fail_fast_sse_client(
    url: str,
    headers: dict[str, Any] | None = None,
    timeout: float = 5,
    sse_read_timeout: float = 60 * 5,
    httpx_client_factory: McpHttpClientFactory = create_mcp_http_client,
    auth: httpx.Auth | None = None,
):
    """Open an MCP SSE transport that fails pending calls when POST delivery fails.

    The MCP Python SDK logs and swallows exceptions from its SSE ``post_writer``.
    That leaves the event stream open and any corresponding JSON-RPC request
    waiting forever. This transport preserves the upstream wire behavior while
    delivering the POST exception to the session and closing its read stream so
    Strands can return a tool error immediately.
    """
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    async with anyio.create_task_group() as task_group:
        try:
            async with httpx_client_factory(
                headers=headers,
                auth=auth,
                timeout=httpx.Timeout(timeout, read=sse_read_timeout),
            ) as client:
                async with aconnect_sse(client, "GET", url) as event_source:
                    event_source.response.raise_for_status()

                    async def sse_reader(
                        task_status: TaskStatus[str] = anyio.TASK_STATUS_IGNORED,
                    ) -> None:
                        try:
                            async for event in event_source.aiter_sse():
                                if event.event == "endpoint":
                                    task_status.started(_same_origin_endpoint(url, event.data))
                                    continue

                                if event.event != "message" or not event.data:
                                    continue
                                try:
                                    message = SessionMessage(
                                        sdk_compat.parse_jsonrpc_message(event.data)
                                    )
                                except Exception as exc:
                                    LOGGER.exception("Unable to parse MCP SSE message")
                                    await read_stream_writer.send(exc)
                                    continue
                                await read_stream_writer.send(message)
                        except SSEError:
                            LOGGER.exception("MCP SSE reader failed")
                            raise
                        except Exception as exc:
                            LOGGER.exception("MCP SSE reader failed")
                            await read_stream_writer.send(exc)
                        finally:
                            await read_stream_writer.aclose()

                    async def post_writer(endpoint_url: str) -> None:
                        try:
                            async with write_stream_reader:
                                async for session_message in write_stream_reader:
                                    response = await client.post(
                                        endpoint_url,
                                        json=session_message.message.model_dump(
                                            by_alias=True,
                                            mode="json",
                                            exclude_none=True,
                                        ),
                                    )
                                    response.raise_for_status()
                        except Exception as exc:
                            LOGGER.exception(
                                "MCP SSE POST failed; closing the session",
                                extra={"mcp_endpoint_url": endpoint_url},
                            )
                            # The active ClientSession normally consumes this
                            # immediately. Bound the notification in case it has
                            # already closed so transport teardown cannot hang.
                            with anyio.move_on_after(1, shield=True):
                                await read_stream_writer.send(exc)
                            await read_stream_writer.aclose()
                        finally:
                            await write_stream.aclose()

                    endpoint_url = await task_group.start(sse_reader)
                    task_group.start_soon(post_writer, endpoint_url)
                    try:
                        yield read_stream, write_stream
                    finally:
                        task_group.cancel_scope.cancel()
        finally:
            await read_stream_writer.aclose()
            await write_stream.aclose()


async def _websocket_client(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    open_timeout: float = 30,
    max_message_bytes: int = 64 * 1024 * 1024,
    pinned_addresses: tuple[str, ...] = (),
):
    """Open an MCP WebSocket transport with optional authentication headers."""
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    parsed = urlparse(url)
    pinned_socket: socket.socket | None = None
    if pinned_addresses:
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        loop = asyncio.get_running_loop()
        last_error: Exception | None = None
        for address in pinned_addresses:
            candidate = socket.socket(
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
            )
            candidate.setblocking(False)
            try:
                await asyncio.wait_for(
                    loop.sock_connect(candidate, (address, port)),
                    timeout=open_timeout,
                )
                pinned_socket = candidate
                break
            except Exception as exc:
                candidate.close()
                last_error = exc
        if pinned_socket is None:
            if last_error is not None:
                raise last_error
            raise ConnectionError("MCP endpoint has no approved network address.")

    websocket_options: dict[str, Any] = {
        "subprotocols": [Subprotocol("mcp")],
        "additional_headers": dict(headers or {}) or None,
        "open_timeout": open_timeout,
        "max_size": max_message_bytes,
        "proxy": None,
    }
    if pinned_socket is not None:
        websocket_options["sock"] = pinned_socket
        if parsed.scheme == "wss":
            websocket_options["server_hostname"] = parsed.hostname

    try:
        websocket_context = websocket_connect(url, **websocket_options)
        async with websocket_context as websocket:

            async def websocket_reader() -> None:
                async with read_stream_writer:
                    async for raw_text in websocket:
                        try:
                            message = SessionMessage(
                                sdk_compat.parse_jsonrpc_message(raw_text)
                            )
                        except Exception as exc:
                            # Match the MCP SDK transport by delivering validation
                            # failures to the session through the read stream.
                            await read_stream_writer.send(exc)
                            continue
                        await read_stream_writer.send(message)

            async def websocket_writer() -> None:
                async with write_stream_reader:
                    async for session_message in write_stream_reader:
                        payload = session_message.message.model_dump(
                            by_alias=True,
                            mode="json",
                            exclude_none=True,
                        )
                        await websocket.send(json.dumps(payload))

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(websocket_reader)
                task_group.start_soon(websocket_writer)
                yield read_stream, write_stream
                task_group.cancel_scope.cancel()
    finally:
        if pinned_socket is not None:
            pinned_socket.close()


def build_mcp_transport(server_config: Mapping[str, Any]):
    transport = (server_config.get("transport") or "").strip().lower()
    pinned_addresses = tuple(
        str(item).strip()
        for item in (server_config.get("mcp_pinned_addresses") or ())
        if str(item).strip()
    )

    if transport == "stdio":
        command = server_config.get("command")
        if not command:
            raise ValueError("stdio MCP server config requires `command`.")
        return lambda: stdio_client(
            StdioServerParameters(
                command=command,
                args=list(server_config.get("args") or []),
                env=dict(server_config.get("env") or {}) or None,
                cwd=server_config.get("cwd"),
            )
        )

    if transport == "sse":
        server_url_env = str(server_config.get("server_url_env") or "").strip()
        server_url = (
            str(os.getenv(server_url_env) or "").strip() if server_url_env else ""
        ) or server_config.get("server_url")
        if not server_url:
            raise ValueError("sse MCP server config requires `server_url`.")
        httpx_client_factory = (
            _pinned_http_client_factory(
                endpoint=str(server_url),
                addresses=pinned_addresses,
            )
            if pinned_addresses
            else create_mcp_http_client
        )
        return lambda: _fail_fast_sse_client(
            server_url,
            headers=dict(server_config.get("headers") or {}) or None,
            timeout=float(server_config.get("timeout", 5)),
            sse_read_timeout=float(server_config.get("sse_read_timeout", 300)),
            httpx_client_factory=httpx_client_factory,
        )

    if transport in {"streamable_http", "streamable-http"}:
        server_url_env = str(server_config.get("server_url_env") or "").strip()
        server_url = (
            str(os.getenv(server_url_env) or "").strip() if server_url_env else ""
        ) or server_config.get("server_url")
        if not server_url:
            raise ValueError("streamable_http MCP server config requires `server_url`.")
        httpx_client_factory = (
            _pinned_http_client_factory(
                endpoint=str(server_url),
                addresses=pinned_addresses,
            )
            if pinned_addresses
            else create_mcp_http_client
        )
        return lambda: _streamable_http_transport(
            server_url,
            headers=dict(server_config.get("headers") or {}) or None,
            timeout=float(server_config.get("timeout", 30)),
            sse_read_timeout=float(server_config.get("sse_read_timeout", 300)),
            terminate_on_close=bool(server_config.get("terminate_on_close", True)),
            httpx_client_factory=httpx_client_factory,
        )

    if transport in {"websocket", "web_socket", "web-socket", "ws"}:
        server_url_env = str(server_config.get("server_url_env") or "").strip()
        server_url = (
            str(os.getenv(server_url_env) or "").strip() if server_url_env else ""
        ) or server_config.get("server_url")
        if not server_url:
            raise ValueError("websocket MCP server config requires `server_url`.")
        return lambda: _websocket_client(
            str(server_url),
            headers=dict(server_config.get("headers") or {}) or None,
            open_timeout=float(server_config.get("timeout", 30)),
            max_message_bytes=int(
                server_config.get("max_message_bytes", 64 * 1024 * 1024)
            ),
            pinned_addresses=pinned_addresses,
        )

    raise ValueError(
        f"Unsupported MCP transport '{server_config.get('transport')}'. "
        "Supported values: stdio, sse, streamable_http, websocket."
    )


__all__ = ["build_mcp_transport"]
