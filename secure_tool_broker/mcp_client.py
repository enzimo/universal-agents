"""Broker-native MCP SDK client; no agent SDK is loaded in this runtime."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import logging
from typing import Any, Mapping

from mcp import ClientSession, types

from mcp_control import sdk_compat
from mcp_control.transports import build_mcp_transport

LOGGER = logging.getLogger(__name__)


class _SessionOwner:
    """Enter and exit SDK task groups in one task, independent of HTTP callers."""

    def __init__(self, config: Mapping[str, Any], timeout: timedelta) -> None:
        self.ready: asyncio.Future[ClientSession] = (
            asyncio.get_running_loop().create_future()
        )
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(
            self._run(config, timeout), name="broker-mcp-session"
        )
        self.task.add_done_callback(self._finished)
        # A caller can be cancelled before consuming a failed initialization.
        self.ready.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )

    async def _run(self, config: Mapping[str, Any], timeout: timedelta) -> None:
        try:
            async with AsyncExitStack() as stack:
                transport = build_mcp_transport(config)
                streams = await stack.enter_async_context(transport())
                session = await stack.enter_async_context(
                    ClientSession(streams[0], streams[1], read_timeout_seconds=sdk_compat.read_timeout(timeout))
                )
                await sdk_compat.negotiate(session)
                self.ready.set_result(session)
                await self._stop.wait()
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            raise

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not self.ready.done():
            self.ready.cancel()
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.warning(
                "MCP session owner failed. event=mcp_session_owner_failed error_type=%s",
                type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def close(self) -> None:
        if not self._stop.is_set():
            self._stop.set()
            # Initialization can still be waiting for an unresponsive peer.
            # Once initialized, let the owning task unwind its contexts normally.
            if not self.ready.done():
                self.task.cancel()
        # Shield the owner from cancellation of a request awaiting cleanup.
        # Its failures are logged above, never substituted for an invocation error.
        await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))


class BrokerMCPClient:
    """Own one MCP session on the broker event loop."""

    def __init__(self, server_config: Mapping[str, Any]) -> None:
        self._server_config = dict(server_config)
        self._owner: _SessionOwner | None = None
        self._owners: set[_SessionOwner] = set()
        self._operation_timeout = timedelta(
            seconds=float(self._server_config.get("tool_timeout_seconds", 300))
        )

    async def _require_session(self) -> tuple[_SessionOwner, ClientSession]:
        # No await between checking and publishing the owner: concurrent HTTP
        # requests on this event loop share the same initialization future.
        if self._owner is None or self._owner.task.done():
            self._owner = _SessionOwner(self._server_config, self._operation_timeout)
            self._owners.add(self._owner)
            owner = self._owner
            owner.task.add_done_callback(lambda _task: self._owners.discard(owner))
        owner = self._owner
        try:
            return owner, await asyncio.shield(owner.ready)
        except BaseException:
            await self._discard(owner)
            raise

    async def _discard(self, owner: _SessionOwner) -> None:
        # A late failure from an old connection must not close its replacement.
        if self._owner is owner:
            self._owner = None
        await owner.close()

    async def list_tools(self) -> tuple[types.Tool, ...]:
        owner, session = await self._require_session()
        tools: list[types.Tool] = []
        cursor: str | None = None
        try:
            while True:
                response = await session.list_tools(params=sdk_compat.pagination_params(cursor))
                tools.extend(response.tools)
                cursor = sdk_compat.next_cursor(response)
                if not cursor:
                    return tuple(tools)
        except BaseException:
            await self._discard(owner)
            raise

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> types.CallToolResult:
        owner, session = await self._require_session()
        try:
            return await session.call_tool(
                name,
                dict(arguments),
                read_timeout_seconds=sdk_compat.read_timeout(self._operation_timeout),
            )
        except BaseException:
            # A failed session is never silently reused. The failed invocation
            # is not retried because an MCP operation may have side effects.
            await self._discard(owner)
            raise

    async def close(self) -> None:
        self._owner = None
        # Include owners already closing after a failed request.
        await asyncio.gather(*(owner.close() for owner in tuple(self._owners)))
