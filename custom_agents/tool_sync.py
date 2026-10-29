"""The model's tool list follows the agent's grants, without restarts (access plan section 8.7, Phase 7).

An agent starts with the functions of its role's standard tool sets
(``access_control.standard_grants``: its own runtime tools, the team lead's
orchestration tools, internet access) plus tools that are not catalog
functions (plugins). Before every model call ``GrantToolSync`` compares the
grants valid for the current task's person (decision D13) with what the model
can see:

- functions of newly held tools are registered as Strands dynamic tools, so a
  grant given during a task is callable on the next model call of that task;
- functions of tools no longer held (revoked, expired, blocked as a whole, or
  held only for another person's work) are removed the way Strands removes
  its own dynamic tools, so switching from A's task to B's shows B's tools;
- installed MCP servers (``mcp:<name>``, access plan 7d) follow the same rule
  as a whole: a held server's tools are listed through the broker and
  registered as dynamic tools, and removed when it is no longer held
  (``custom_agents.mcp_server_tools``, which also decides when a server that
  could not be listed is tried again and when to stop and tell
  administrators).

The comparison runs again when the access store's version, the task, the
person or the tool catalog changes, or a relevant grant or block expires.
The access hook
(``custom_agents.access_runtime``) and, for MCP calls, the broker's access
gate still decide every call; this only decides what the model is shown.

With authorization off (decision D8) there are no grants: the agent keeps the
tools its definition lists, and ``MCPServerSync`` loads the same way the
active MCP servers meant for its type (the installed servers whose record
names it, and the operator's configured servers its configuration lists).
It reads the MCP registry again when the tool catalog reloads (a record
changed, for example a server installed with the person's agreement) or the
access store changes (an account link), so such a server loads on the
agent's next model call. No MCP server is loaded at startup in either mode.

In both modes the groups the catalog loads on request (``on_request``) are
left out until the agent loads them for its task with ``use_tools``
(``custom_agents.tool_loading``): both hooks count the model call first,
which unloads groups idle too long or loaded for another task, and then show
only the groups loaded now. A request carries at most the model's tool cap,
which ``tool_loading.ToolLimitHook`` checks after these hooks.

Each Strands agent has its own hook, with its own record of what it shows
and its own loaded groups (``custom_agents.strands_parts``), so two turns
running at once never write each other's tool lists. The hook's MCP loader
(``sync.servers``) lets ``custom_agents.mcp_server_tools.server_status`` tell
``find_tools`` and ``mcp_connections(action="list")`` why a server's tools are
missing.

The operator's tool settings (``secure_cli`` off, allow and block lists,
skill installation off) apply to added functions exactly as at startup
(``custom_agents.team_config_helpers.filter_agent_tools``); MCP tools were
never subject to them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from strands.hooks import BeforeModelCallEvent, HookRegistry

from access_catalog.tool_functions import functions_of_tool_sets, functions_of_tools, mcp_tool_ids, tool_ids_of_grants
from access_control.identity import IdentityRejected
from access_control.standard_grants import INTERNET_SET, STANDARD_TOOL_SETS_BY_ROLE
from custom_agents.access_runtime import agent_principal, authorization_enabled, runtime_access, task_access
from custom_agents.mcp_server_tools import MCPServerTools
from custom_agents.providers.mcp_connections import CONFIGURED_CONNECTION_IDS, shown_without_authorization
from custom_agents.tool_loading import LoadedTools, offered, shown_names, work_key, work_preloaded
from custom_messages.contracts.access_v2 import GrantKind
from custom_tools.provider_contract import tool_name
from storage.access_repository import AccessRepository

logger = logging.getLogger(__name__)

TOOL_SYNC_HOOK_ORDER = -100.0  # before other model-call hooks look at the tool list


def provider_tools() -> dict[str, Any]:
    """Every trusted function tool, by name."""

    from custom_tools.registry import ALL_PROVIDER_TOOLS

    return {tool_name(tool): tool for tool in ALL_PROVIDER_TOOLS}


def standard_functions(agent: Any) -> set[str]:
    """The functions of the agent's role's standard tool sets."""

    role = agent_principal(agent).role
    catalog = runtime_access(agent).catalog
    return functions_of_tool_sets(catalog, (*STANDARD_TOOL_SETS_BY_ROLE.get(role, ()), INTERNET_SET))


def startup_tools(agent: Any, tools: list[Any]) -> list[Any]:
    """What the model sees when the agent starts.

    With authorization on: the role's standard functions and every tool that
    is not a catalog function; ``GrantToolSync`` adds the rest, and MCP
    servers, from grants. With it off: the tools the agent's definition gives
    it. In both modes without the groups that load on request
    (``custom_agents.tool_loading``).
    """

    catalog = runtime_access(agent).catalog
    if not authorization_enabled(getattr(agent, "authorization_config", None)):
        # Startup ignores what is loaded: those are added and removed by the hook,
        # so a rebuild during an objective session does not keep them for good.
        shown = shown_names(agent, catalog, (tool_name(tool) for tool in tools), at_start=True)
        return [tool for tool in tools if tool_name(tool) in shown]
    provider = provider_tools()
    standard = shown_names(agent, catalog, standard_functions(agent), at_start=True)
    kept = [tool for tool in tools if tool_name(tool) not in provider]
    base = offered(agent, [provider[name] for name in sorted(standard) if name in provider])
    return [*base, *kept]


def _begin_model_call(agent: Any, loaded: LoadedTools) -> None:
    """Count the model call for loaded groups: those idle too long or loaded for another task leave."""

    dropped, idle = loaded.begin_model_call(work_key(agent))
    if dropped or idle:
        logger.info(
            "Loaded tool groups left the agent's tool list. event=tool_groups_unloaded agent_id=%s "
            "task_changed=%s idle=%s",
            getattr(agent, "id", None),
            ",".join(dropped),
            ",".join(idle),
        )


class _DynamicFunctions:
    """Catalog functions managed by a hook, including standard functions registered at startup."""

    def __init__(self, agent: Any, initial_names: Iterable[str] = ()) -> None:
        self.agent = agent
        self.managed: set[str] = set(initial_names)

    def apply(self, registry: Any, desired: set[str]) -> None:
        provider = provider_tools()
        wanted = {tool_name(tool) for tool in offered(self.agent, [provider[n] for n in desired if n in provider])}
        added = sorted(name for name in wanted - self.managed if name not in registry.registry)
        removed = sorted(self.managed - wanted)
        for name in removed:
            registry.registry.pop(name, None)
            registry.dynamic_tools.pop(name, None)
        registered: list[str] = []
        for name in added:
            try:
                registry.register_dynamic_tool(provider[name])
            except ValueError as exc:  # another tool already uses the name; this one stays hidden
                logger.warning(
                    "A tool could not be offered. event=tool_sync_register_failed agent_id=%s tool=%s error=%s",
                    getattr(self.agent, "id", None),
                    name,
                    exc,
                )
                continue
            registered.append(name)
        self.managed = (self.managed - set(removed)) | set(registered)
        self._log(registered, removed)

    def withdraw(self, registry: Any, names: set[str]) -> None:
        """Take functions out now (the model's tool cap, ``tool_loading.ToolLimitHook``), keeping the record true."""

        removed = sorted(name for name in names & self.managed)
        for name in removed:
            registry.registry.pop(name, None)
            registry.dynamic_tools.pop(name, None)
        self.managed -= set(removed)
        self._log([], removed)

    def _log(self, registered: list[str], removed: list[str]) -> None:
        if registered or removed:
            logger.info(
                "The agent's tools changed. event=tool_sync_applied agent_id=%s added=%s removed=%s",
                getattr(self.agent, "id", None),
                ",".join(registered),
                ",".join(removed),
            )


# -- the hook ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolView:
    """What the grants valid now let the model see: catalog functions and MCP servers."""

    functions: set[str]
    servers: set[str]


class GrantToolSync:
    """Strands hook: keeps the model's catalog functions and MCP servers equal to the grants valid now."""

    name = "grant_tool_sync"

    def __init__(self, agent: Any, static_names: Iterable[str], parts: Any) -> None:
        self.agent = agent
        # The Strands agent's own tool state (custom_agents.strands_parts).
        self.parts = parts
        startup_names = frozenset(static_names)
        # Standard catalog functions obey grants too. Only plugins outside
        # the catalog remain static; a withdrawn standard function is restored
        # through the same dynamic registry used for other granted functions.
        catalog_names = startup_names & provider_tools().keys()
        self.static_names = startup_names - catalog_names
        self._key: tuple[Any, ...] | None = None
        self._expires_at: datetime | None = None
        self.functions = _DynamicFunctions(agent, catalog_names)
        self.servers = MCPServerTools(agent)
        self.access_version = 0

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call, order=TOOL_SYNC_HOOK_ORDER)

    async def before_model_call(self, event: BeforeModelCallEvent) -> None:
        registry = event.agent.tool_registry
        try:
            await asyncio.to_thread(_begin_model_call, self.agent, self.parts.loaded)
            view = await asyncio.to_thread(self.desired)
            if view is not None:
                self.functions.apply(registry, view.functions)
                self.servers.want(view.servers)
            await self.servers.sync(registry, changed=view is not None, access_version=self.access_version)
        except Exception:  # noqa: BLE001 - the list stays as it was; the access checks still decide every call
            logger.exception(
                "Could not update the agent's tools from its grants. event=tool_sync_failed agent_id=%s",
                getattr(self.agent, "id", None),
            )

    def desired(self) -> ToolView | None:
        """The dynamic functions and MCP servers to show now, or None when nothing changed since last time."""

        access = runtime_access(self.agent)
        if access.service is None:
            return None
        try:
            principal, person, _task_id = task_access(self.agent, access)
        except (IdentityRejected, ValueError):
            principal, person = agent_principal(self.agent), None
        person_id = person.person_id if person else None
        catalog = access.catalog
        # A scheduled run counts its schedule's grants, so the schedule is part of the key.
        # Access for one task is bound to the request it works on (work_id).
        work_id = principal.work_id
        loaded = self.parts.loaded
        key = (
            access.service.store.version(access.team_id),
            work_id,
            person_id,
            principal.schedule_id,
            id(catalog),
            loaded.version,
            frozenset(work_preloaded(self.agent)),
        )
        self.access_version = key[0]
        if key == self._key and (self._expires_at is None or access.service.clock() < self._expires_at):
            return None
        # What the agent's current work counts ("access follows the work"), and
        # for a worker's delegated task what the team lead may add to it at
        # once, so the first call to such a tool can do that.
        grants = access.service.agent_grants(principal, task_id=work_id, person_id=person_id)
        grants += access.service.hand_on_sources(principal, person_id)
        if person is not None:
            # A block on the person stops their agents too.
            grants += [g for g in access.service.valid_grants(person.subjects(), task_id=work_id) if g.kind is GrantKind.BLOCK]
        held_tools = tool_ids_of_grants(catalog, grants)
        view = ToolView(
            functions=shown_names(self.agent, catalog, functions_of_tools(catalog, held_tools), loaded=loaded)
            - self.static_names,
            servers=mcp_tool_ids(catalog, held_tools),
        )
        # Expiry changes no store version. Reconsider this view at the next
        # relevant deadline, including TeamLead's hand-on sources and blocks
        # on the person, so timed restrictions and access both end on time.
        self._expires_at = min((g.expires_at_utc for g in grants if g.expires_at_utc is not None), default=None)
        self._key = key
        return view


class MCPServerSync:
    """Strands hook with authorization off: the agent shows the MCP servers meant for it (module docstring)."""

    name = "mcp_server_sync"

    def __init__(self, agent: Any, static_names: Iterable[str], parts: Any) -> None:
        agent_type = str(getattr(agent, "named_agent_type", "") or "")
        configured = frozenset(getattr(agent, CONFIGURED_CONNECTION_IDS, None) or ())
        self.agent = agent
        # The Strands agent's own tool state (custom_agents.strands_parts).
        self.parts = parts
        self.static_names = frozenset(static_names)
        self.servers = MCPServerTools(
            agent,
            shows=lambda record: shown_without_authorization(record, agent_type=agent_type, configured_ids=configured),
        )
        self.functions = _DynamicFunctions(agent)
        self._key: tuple[int, int] | None = None
        self._loaded_key: tuple[Any, ...] | None = None
        self._store: AccessRepository | None = None

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call, order=TOOL_SYNC_HOOK_ORDER)

    async def before_model_call(self, event: BeforeModelCallEvent) -> None:
        registry = event.agent.tool_registry
        try:
            await asyncio.to_thread(_begin_model_call, self.agent, self.parts.loaded)
            self.show_loaded(registry)
            key = await asyncio.to_thread(self.watched)
            changed, self._key = key != self._key, key
            await self.servers.sync(registry, changed=changed, access_version=key[0])
        except Exception:  # noqa: BLE001 - the list stays as it was; the broker still decides every MCP call
            logger.exception(
                "Could not update the agent's MCP servers. event=tool_sync_failed agent_id=%s",
                getattr(self.agent, "id", None),
            )

    def show_loaded(self, registry: Any) -> None:
        """The definition's functions of the groups loaded now (authorization off has no grants to follow)."""

        catalog = runtime_access(self.agent).catalog
        loaded = self.parts.loaded
        loaded_key = (loaded.version, frozenset(work_preloaded(self.agent)), id(catalog))
        if loaded_key == self._loaded_key:
            return
        self._loaded_key = loaded_key
        names = {tool_name(tool) for tool in getattr(self.agent, "tools", ()) or ()}
        self.functions.apply(registry, shown_names(self.agent, catalog, names, loaded=loaded) - self.static_names)

    def watched(self) -> tuple[int, int]:
        """The access store's version (account links) and the current tool catalog (MCP records)."""

        access = runtime_access(self.agent)
        if self._store is None:
            self._store = AccessRepository(self.agent.task_manager.storage_settings)
        return self._store.version(access.team_id), id(access.catalog)


def install_tool_sync(agent: Any, tools: list[Any], parts: Any) -> GrantToolSync | MCPServerSync:
    """The model-call hook for one Strands agent whose startup tools are ``tools``.

    With authorization on it follows grants (``GrantToolSync``); with it off
    it loads the MCP servers meant for the agent (``MCPServerSync``). ``parts``
    are that Strands agent's tool state; the caller keeps the hook in them
    (``custom_agents.strands_parts``).
    """

    static_names = [tool_name(tool) for tool in tools]
    if authorization_enabled(getattr(agent, "authorization_config", None)):
        return GrantToolSync(agent, static_names, parts)
    return MCPServerSync(agent, static_names, parts)


__all__ = [
    "GrantToolSync",
    "MCPServerSync",
    "TOOL_SYNC_HOOK_ORDER",
    "ToolView",
    "install_tool_sync",
    "standard_functions",
    "startup_tools",
]
