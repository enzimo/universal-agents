"""Loading installed MCP servers' tools into a running agent (access plan 7d), in both authorization modes.

An installed MCP server is one catalog tool, ``mcp:<name>``; its functions are
the server's own tools, named ``<prefix>_<tool>``. The connection itself lives
in the secure broker: an agent lists the tools through a brokered client
(``custom_agents.sdk.strands.mcp.BrokeredMCPClient``) and registers them as
Strands dynamic tools, so they appear on the next model call without a
restart.

Before every model call ``MCPServerTools`` keeps the servers one agent shows
equal to what it should show (``custom_agents.tool_sync``): with
authorization on, the servers it holds for the current person; with it off,
the active servers ``custom_agents.providers.mcp_connections.shown_without_authorization``
picks for its type. A server installed while the agent runs therefore loads
on its next model call in both modes.

A server whose tools could not be listed is not tried on every model call:

- The broker refused (no access, no account linked, the server is not
  active): nothing is retried on a timer. The server is tried again when
  access changes (a grant, block or account link bumps the access store
  version), when its record changes (an administrator updates or reinstalls
  it), or when the agent restarts.
- Anything else (a network error, a timeout, a broker or server error) means
  the server is not answering. It is tried again after 1, 2, 4, 8 and 16
  minutes, then every 30 minutes, until ``mcp.load_retry_window_minutes``
  (60 by default) have passed since the first failure; the last try is at
  the end of that window. Then the agent stops trying and tells
  administrators with an Inbox notice whose id comes from the connection and
  the UTC date, so every agent that gives up on the same server stores one
  notice per day. It then waits for the same changes as after a refusal. An
  access change also makes a server that is still being retried due at once.

The state is small and lives in this process; a restart starts afresh.
``MCPServerTools.status`` describes a server that did not load, for
``find_tools`` and ``mcp_connections(action="list")``, so an agent can tell the person
why a server's tools are missing. Showing a tool decides nothing: the
broker's access gate decides every call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

import httpx

from access_control.people import PERSON_ROLE_ADMIN
from custom_agents.access_runtime import send_runtime_notice_once
from custom_messages.contracts.access_v2 import AccessNoticeKind

logger = logging.getLogger(__name__)

# Waits after the first five failed tries of a server that does not answer;
# after that it is tried every LATER_RETRY_GAP until the retry window ends.
RETRY_GAPS = tuple(timedelta(minutes=minutes) for minutes in (1, 2, 4, 8, 16))
LATER_RETRY_GAP = timedelta(minutes=30)
MAX_ERROR_TEXT = 300
UNAVAILABLE_NOTICE_PREFIX = "mcp_server_unavailable"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clock_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def retry_gap(tries: int) -> timedelta:
    """How long to wait after the ``tries``-th failed try of a server that does not answer."""

    return RETRY_GAPS[tries - 1] if tries <= len(RETRY_GAPS) else LATER_RETRY_GAP


def load_retry_window(agent: Any) -> timedelta:
    """``mcp.load_retry_window_minutes`` from the agent's MCP settings."""

    from runtime_settings.mcp import MCPSettings

    settings = MCPSettings.model_validate(dict(getattr(agent, "mcp_config", None) or {}))
    return timedelta(minutes=settings.load_retry_window_minutes)


def unavailable_notice_id(connection_id: str, moment: datetime) -> str:
    """One notice per server and UTC day, whichever agent gives up first (``send_runtime_notice_once``)."""

    digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:32]
    return f"{UNAVAILABLE_NOTICE_PREFIX}_{moment.astimezone(timezone.utc):%Y%m%d}_{digest}"


def error_text(exc: BaseException) -> str:
    """A short, one-line reason for a failed listing; the broker's errors carry no credentials."""

    if isinstance(exc, httpx.HTTPStatusError):
        text = f"the secure broker answered HTTP {exc.response.status_code}: {exc.response.text}"
    else:
        text = str(exc) or type(exc).__name__
    return " ".join(text.split())[:MAX_ERROR_TEXT]


class LoadFailure(StrEnum):
    REFUSED = "refused"  # the broker refused; waits for a change
    NOT_ANSWERING = "not_answering"  # tried again on the schedule
    GAVE_UP = "gave_up"  # administrators were told; waits for a change


@dataclass(frozen=True)
class ServerFailure:
    """Why one server's tools are not loaded, and when it is tried again (None: after a change)."""

    kind: LoadFailure
    connection_id: str
    record_digest: str
    first_failed_at: datetime
    last_failed_at: datetime
    error: str
    tries: int
    next_try_at: datetime | None

    def due(self, now: datetime) -> bool:
        return self.next_try_at is not None and now >= self.next_try_at

    def for_record(self, record: Any) -> bool:
        return (self.connection_id, self.record_digest) == (record.connection_id, record.record_digest)

    def describe(self) -> str:
        """The status in plain words, for the agent to pass on."""

        if self.kind is LoadFailure.REFUSED:
            return (
                f"Not loaded: the secure broker refused to list its tools ({self.error}). "
                "It is tried again when access or the server changes."
            )
        since = _clock_text(self.first_failed_at)
        if self.kind is LoadFailure.NOT_ANSWERING and self.next_try_at is not None:
            return (
                f"Not loaded: the server has not answered since {since} ({self.error}); "
                f"trying again at {_clock_text(self.next_try_at)}."
            )
        return (
            f"Not loaded: the server has not answered since {since} ({self.error}). Agents stopped trying at "
            f"{_clock_text(self.last_failed_at)} and administrators were told in the Inbox; it is tried again "
            "when access or the server changes, or when the agent restarts."
        )


_FAILURE_LOGS = {
    LoadFailure.REFUSED: (
        "The secure broker refused to list an MCP server's tools; it is tried again when access or the server "
        "changes. event=mcp_server_load_refused"
    ),
    LoadFailure.NOT_ANSWERING: "An MCP server did not answer; it is tried again later. event=mcp_server_load_failed",
    LoadFailure.GAVE_UP: (
        "Stopped trying an MCP server that does not answer; administrators were told. event=mcp_server_load_gave_up"
    ),
}


def _mcp_store(agent: Any) -> Any:
    from custom_agents.providers.mcp_connections import resolve_agent_mcp_connection_paths
    from mcp_control.registry import MCPConnectionStore

    return MCPConnectionStore(resolve_agent_mcp_connection_paths(agent))


async def load_server_tools(agent: Any, record: Any) -> list[Any]:
    """List an active server's tools through the broker, as this agent's type; raises when listing fails."""

    from custom_agents.sdk.strands.mcp import brokered_mcp_client
    from mcp_control.registry import brokered_mcp_server_config

    named_agent_type = str(getattr(agent, "named_agent_type", "") or "").strip()
    client = brokered_mcp_client(brokered_mcp_server_config(record, named_agent_type=named_agent_type))
    tools = await client.load_tools()
    return [tool for tool in tools if tool is not None]


@dataclass(frozen=True)
class _LoadedServer:
    connection_id: str
    names: tuple[str, ...]


class MCPServerTools:
    """The installed MCP servers whose tools one running agent shows (module docstring).

    ``shows`` picks the active records to show; by default the servers set
    with ``want`` (the servers held with authorization on).
    """

    def __init__(
        self,
        agent: Any,
        *,
        shows: Callable[[Any], bool] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.agent = agent
        self.wanted: frozenset[str] = frozenset()
        self._shows = shows or (lambda record: record.descriptor.capability_id in self.wanted)
        self.clock = clock
        self.retry_window = load_retry_window(agent)
        self._loaded: dict[str, _LoadedServer] = {}
        self._failures: dict[str, ServerFailure] = {}
        self._shown: frozenset[str] = frozenset()  # the servers shown at the last registry read
        self._access_version: int | None = None

    def want(self, tool_ids: Iterable[str]) -> None:
        """The servers the agent holds now (authorization on)."""

        self.wanted = frozenset(tool_ids)

    def failures(self) -> dict[str, ServerFailure]:
        """The servers whose tools are not loaded now, by tool id (for the Status page)."""

        return dict(self._failures)

    def status(self, tool_id: str) -> str | None:
        """Why the server's tools are not loaded, in plain words; None when nothing failed."""

        failure = self._failures.get(tool_id)
        return failure.describe() if failure is not None else None

    async def sync(self, registry: Any, *, changed: bool, access_version: int) -> None:
        """Load the servers to show and unload the rest.

        ``changed``: something the caller watches changed (grants, the task,
        the tool catalog, which reloads when a record changes). The registry
        is read only then, when access changed, or when a retry is due.
        """

        access_changed = access_version != self._access_version
        self._access_version = access_version
        now = self.clock()
        if not (changed or access_changed or self._retry_due(now)):
            return
        records = await asyncio.to_thread(self._active_records)
        shown = {tool_id: record for tool_id, record in records.items() if self._shows(record)}
        self._shown = frozenset(shown)
        self._forget_failures(records, access_changed=access_changed, now=now)
        self._unload_hidden(registry, shown)
        for tool_id, record in sorted(shown.items()):
            failure = self._failures.get(tool_id)
            if tool_id not in self._loaded and (failure is None or failure.due(now)):
                await self._load(registry, tool_id, record)

    def _retry_due(self, now: datetime) -> bool:
        """A shown server's retry is due (a hidden one waits until it is shown again)."""

        return any(failure.due(now) for tool_id, failure in self._failures.items() if tool_id in self._shown)

    def _active_records(self) -> dict[str, Any]:
        from mcp_control.registry import MCPConnectionLifecycle

        return {
            record.descriptor.capability_id: record
            for record in _mcp_store(self.agent).list_records()
            if record.lifecycle is MCPConnectionLifecycle.ACTIVE
        }

    def _forget_failures(self, records: dict[str, Any], *, access_changed: bool, now: datetime) -> None:
        """A failure ends when its server is no longer active or its record changed; access changes retry at once.

        After an access change a refused or given-up server starts afresh, and
        a server still being retried keeps its window but is tried now. A
        failure outlives a switch to another person's task, which changes what
        is shown but not whether the server answers.
        """

        for tool_id, failure in list(self._failures.items()):
            record = records.get(tool_id)
            if record is None or not failure.for_record(record):
                del self._failures[tool_id]
            elif access_changed and failure.next_try_at is None:
                del self._failures[tool_id]
            elif access_changed:
                self._failures[tool_id] = replace(failure, next_try_at=now)

    def _unload_hidden(self, registry: Any, shown: dict[str, Any]) -> None:
        for tool_id in sorted(self._loaded):
            record = shown.get(tool_id)
            if record is None or record.connection_id != self._loaded[tool_id].connection_id:
                self._unload(registry, tool_id)

    async def _load(self, registry: Any, tool_id: str, record: Any) -> None:
        try:
            tools = await load_server_tools(self.agent, record)
        except Exception as exc:  # noqa: BLE001 - classified, reported and retried; calls stay decided by the broker
            await self._failed(tool_id, record, exc)
            return
        self._failures.pop(tool_id, None)
        names = register_dynamic(self.agent, registry, tools, tool_id=tool_id)
        self._loaded[tool_id] = _LoadedServer(record.connection_id, tuple(names))
        logger.info(
            "An MCP server's tools were added to the agent. event=mcp_server_tools_added "
            "agent_id=%s tool_id=%s connection_id=%s tools=%s",
            getattr(self.agent, "id", None),
            tool_id,
            record.connection_id,
            ",".join(names),
        )

    async def _failed(self, tool_id: str, record: Any, exc: Exception) -> None:
        from custom_agents.sdk.strands.mcp import BrokerRefusal

        now = self.clock()
        if isinstance(exc, BrokerRefusal):
            failure = ServerFailure(
                LoadFailure.REFUSED, record.connection_id, record.record_digest, now, now, error_text(exc), 1, None
            )
        else:
            failure = self._not_answering(self._failures.get(tool_id), record, exc, now)
        self._failures[tool_id] = failure
        logger.warning(
            "%s agent_id=%s tool_id=%s connection_id=%s tries=%s since=%s next_try_at=%s error=%s",
            _FAILURE_LOGS[failure.kind],
            getattr(self.agent, "id", None),
            tool_id,
            record.connection_id,
            failure.tries,
            failure.first_failed_at.isoformat(),
            failure.next_try_at.isoformat() if failure.next_try_at else None,
            failure.error,
        )
        if failure.kind is LoadFailure.GAVE_UP:
            await asyncio.to_thread(self._tell_administrators, tool_id, record, failure)

    def _not_answering(self, previous: ServerFailure | None, record: Any, exc: Exception, now: datetime) -> ServerFailure:
        """The next state of a server that did not answer: tried again on the schedule, or given up."""

        ongoing = previous is not None and previous.kind is LoadFailure.NOT_ANSWERING and previous.for_record(record)
        first = previous.first_failed_at if ongoing and previous is not None else now
        tries = previous.tries + 1 if ongoing and previous is not None else 1
        failure = ServerFailure(
            LoadFailure.NOT_ANSWERING, record.connection_id, record.record_digest, first, now, error_text(exc), tries, None
        )
        give_up_at = first + self.retry_window
        if now >= give_up_at:
            return replace(failure, kind=LoadFailure.GAVE_UP)
        return replace(failure, next_try_at=min(now + retry_gap(tries), give_up_at))

    def _tell_administrators(self, tool_id: str, record: Any, failure: ServerFailure) -> None:
        name = record.descriptor.connection_name
        send_runtime_notice_once(
            self.agent,
            notice_id=unavailable_notice_id(record.connection_id, failure.last_failed_at),
            kind=AccessNoticeKind.MCP_SERVER_UNAVAILABLE,
            title=f"The MCP server {name} is not answering",
            summary=(
                f"Agents could not reach the MCP server {name} ({tool_id}) since "
                f"{_clock_text(failure.first_failed_at)} and stopped trying at {_clock_text(failure.last_failed_at)}. "
                f"Last error: {failure.error}. Until it answers, agents cannot use its tools. Check that the server "
                "is running, then update or reinstall it in Settings (Team, MCP Connections). Agents try again "
                "when it changes or when they restart."
            ),
            roles=(PERSON_ROLE_ADMIN,),
        )

    def _unload(self, registry: Any, tool_id: str) -> None:
        loaded = self._loaded.pop(tool_id)
        for name in loaded.names:
            registry.dynamic_tools.pop(name, None)
        logger.info(
            "An MCP server's tools were removed from the agent. event=mcp_server_tools_removed "
            "agent_id=%s tool_id=%s tools=%s",
            getattr(self.agent, "id", None),
            tool_id,
            ",".join(loaded.names),
        )


def server_loader(agent: Any) -> MCPServerTools | None:
    """The MCP loader of the Strands agent the current turn calls, else of the agent's main one.

    Each Strands agent's tool-list sync has its own (``custom_agents.strands_parts``).
    """

    from custom_agents.strands_parts import current_parts

    parts = current_parts(agent)
    return getattr(getattr(parts, "sync", None), "servers", None)


def server_status(agent: Any, tool_id: str) -> str | None:
    """Why an MCP server's tools are not loaded for this agent, in plain words; None when nothing failed."""

    servers = server_loader(agent)
    return servers.status(tool_id) if servers is not None else None


def register_dynamic(agent: Any, registry: Any, tools: Iterable[Any], *, tool_id: str) -> list[str]:
    """Register tools as Strands dynamic tools; a name already in use is skipped and logged."""

    names: list[str] = []
    for tool in tools:
        try:
            registry.register_dynamic_tool(tool)
        except ValueError as exc:
            logger.warning(
                "An MCP tool could not be offered because its name is taken. event=mcp_tool_register_failed "
                "agent_id=%s tool_id=%s tool=%s error=%s",
                getattr(agent, "id", None),
                tool_id,
                tool.tool_name,
                exc,
            )
            continue
        names.append(tool.tool_name)
    return names


__all__ = [
    "LATER_RETRY_GAP",
    "LoadFailure",
    "MCPServerTools",
    "RETRY_GAPS",
    "server_loader",
    "ServerFailure",
    "error_text",
    "load_retry_window",
    "load_server_tools",
    "register_dynamic",
    "retry_gap",
    "server_status",
    "unavailable_notice_id",
]
