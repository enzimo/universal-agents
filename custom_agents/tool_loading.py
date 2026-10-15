"""Tools that load on request: an agent sees its everyday tools and adds the rest for a task when it needs them.

Why: a model request may carry only so many tools (OpenAI models refuse more
than 128), and the team lead's standard tools alone were 140, so every one of
its requests on such a model was refused. Their definitions also cost about
30,000 tokens on every call. Groups used now and then therefore wait until a
task needs them. Loading changes what the model is shown, never what it may
use: the access hook (``custom_agents.access_runtime``) still decides every
call, and with authorization on only held tools are ever shown
(``custom_agents.tool_sync``).

How it works:

- **Which groups.** The catalog's ``on_request`` list (``functions.yaml``):
  tool id -> the functions that stay shown for everyone (``keep_shown``). A
  group stays shown for an agent type whose definition names it in
  ``default_tools``, because the definition says the type works with it.
- **Hidden at start.** ``custom_agents.tool_sync.startup_tools`` leaves out the
  functions of hidden groups, in both authorization modes.
- **Loading.** ``use_tools`` (``custom_tools.providers.tool_loading``) records
  the group for the agent's current task (``LoadedTools``); the tool-list sync
  adds its functions before the next model call. ``find_tools`` says which
  groups can be loaded, and the prompt carries a short list of them
  (``loadable_tools_text``), which costs far less than their definitions.
- **Calling one that is not loaded.** ``OnRequestToolHook`` gives such a call
  its tool and loads the group; the access hook decides as usual. Without the
  definition the model guessed the inputs, so a wrong call returns an error
  and the next call shows the definition.
- **Unloading.** A loaded group not used for ``LOADED_TOOL_IDLE_MODEL_CALLS``
  of the agent's model calls leaves the list, and all loaded groups leave
  when the agent's task changes. Model calls are counted rather than time,
  because a task may wait days for an approval and the count behaves the same
  on every run. Loading again brings a group back.
- **Loaded for the work.** An objective work session's turns have
  ``fn:objectives``: their checkpoint pass must call
  ``record_objective_work_checkpoint`` (``custom_agents.sdk.strands.turn``).
- **The cap.** ``max_tools_per_request`` per model, from the
  ``model_capabilities`` maps (``LLM_OVERRIDES.yaml`` over ``llm`` in the
  instance configuration; ``DEFAULT_MAX_TOOLS_PER_REQUEST`` without an entry),
  for the model the call uses (a person's own model while active,
  ``custom_agents.person_model_runtime``). ``use_tools`` refuses a load that
  would go over it after unloading the least recently used groups, and
  ``ToolLimitHook`` checks every request: over the cap it unloads loaded
  groups, least recently used first. Only the tool-list sync changes the
  registry's catalog functions, before each model call in the event loop:
  ``use_tools`` runs in a tool thread and changes only ``LoadedTools``,
  counting what the next request will carry (``projected_names``), and the
  hook removes a group's functions through the sync's own record. If the agent's always-shown tools alone
  are over it, nothing more can be removed: it logs the problem and tells
  administrators once per agent type and model, and the provider decides.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from access_catalog.tool_functions import functions_of_tools
from custom_agents.model_runtime_profile import DEFAULT_MAX_TOOLS_PER_REQUEST
from custom_tools.provider_contract import tool_name

logger = logging.getLogger(__name__)

# Model calls a loaded group may go unused before it leaves the tool list (module docstring).
LOADED_TOOL_IDLE_MODEL_CALLS = 8
OBJECTIVES_TOOL_ID = "fn:objectives"
# After the tool-list sync (-100) and the person's model (-50), before the default hooks.
TOOL_LIMIT_HOOK_ORDER = -40.0
# Before the access hook (order inf), so the call it gives a tool is still decided.
ON_REQUEST_TOOL_HOOK_ORDER = -10.0
_STATE_ATTRIBUTE = "_loaded_tools"
# Where custom_agents.tool_sync.install_tool_sync keeps the agent's tool-list sync,
# the one writer of the registry's catalog functions.
SYNC_ATTRIBUTE = "_tool_list_sync"


@dataclass
class LoadedTools:
    """One agent's loaded on-request groups for its current task, with when each was last used.

    ``last_used`` holds the model call (``model_calls``) at which a group was
    loaded or last called. ``version`` changes whenever the set changes, so the
    tool-list sync knows to look again.
    """

    work_key: str | None = None
    model_calls: int = 0
    last_used: dict[str, int] = field(default_factory=dict)
    version: int = 0
    # A load holds this across projection, eviction and insertion. State
    # helpers take the same lock so concurrent Strands tool calls cannot both
    # spend the same capacity; reentrancy lets the transaction use them.
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def begin_model_call(self, work_key: str | None) -> tuple[list[str], list[str]]:
        """Count a model call: (groups dropped because the task changed, groups unloaded as idle)."""

        with self.lock:
            dropped: list[str] = []
            if work_key != self.work_key:
                dropped = sorted(self.last_used)
                self.last_used.clear()
                self.work_key = work_key
            self.model_calls += 1
            idle = sorted(
                tool_id
                for tool_id, used_at in self.last_used.items()
                if self.model_calls - used_at > LOADED_TOOL_IDLE_MODEL_CALLS
            )
            for tool_id in idle:
                del self.last_used[tool_id]
            if dropped or idle:
                self.version += 1
            return dropped, idle

    def load(self, tool_ids: Iterable[str]) -> list[str]:
        """Load groups for the current task; the ones newly loaded (a loaded one counts as used again)."""

        with self.lock:
            added = [tool_id for tool_id in tool_ids if tool_id not in self.last_used]
            for tool_id in tool_ids:
                self.last_used[tool_id] = self.model_calls
            if added:
                self.version += 1
            return added

    def used(self, tool_id: str) -> None:
        with self.lock:
            if tool_id in self.last_used:
                self.last_used[tool_id] = self.model_calls

    def unload(self, tool_id: str) -> bool:
        with self.lock:
            if self.last_used.pop(tool_id, None) is None:
                return False
            self.version += 1
            return True

    def least_recently_used(self, excluding: Iterable[str] = ()) -> str | None:
        oldest = self.by_least_recent(excluding)
        return oldest[0] if oldest else None

    def by_least_recent(self, excluding: Iterable[str] = ()) -> list[str]:
        """The loaded groups, least recently used first."""

        with self.lock:
            skip = set(excluding)
            return [tool_id for _used_at, tool_id in sorted((u, t) for t, u in self.last_used.items() if t not in skip)]

    def loaded(self) -> frozenset[str]:
        with self.lock:
            return frozenset(self.last_used)


def loaded_tools(agent: Any) -> LoadedTools:
    state = getattr(agent, _STATE_ATTRIBUTE, None)
    if state is None:
        state = LoadedTools()
        setattr(agent, _STATE_ATTRIBUTE, state)
    return state


# -- what the agent works with -------------------------------------------------------


def _filter_settings(agent: Any) -> dict[str, Any]:
    settings = dict(getattr(agent, "tool_filter", None) or {})
    return {
        "authorization_enabled": True,
        "skill_installation_enabled": bool(settings.get("skill_installation_enabled", True)),
        "tool_allowlist": settings.get("tool_allowlist"),
        "tool_blocklist": settings.get("tool_blocklist"),
    }


def offered(agent: Any, tools: Iterable[Any]) -> list[Any]:
    """The operator's tool settings, as applied at startup (allow and block lists, skill installation)."""

    from custom_agents.team_config_helpers import filter_agent_tools

    return filter_agent_tools(tools, **_filter_settings(agent))


def offered_names(agent: Any, names: Iterable[str]) -> set[str]:
    """The function names the operator's tool settings keep (``offered`` by name)."""

    from custom_agents.team_config_helpers import filter_agent_tool_names

    return filter_agent_tool_names(names, **_filter_settings(agent))


def definition_tool_ids(agent: Any, catalog: Any) -> set[str]:
    """The catalog tools the agent's definition names in ``default_tools``; they stay shown."""

    from custom_agents.default_tools import default_tool_ids

    agent_type = str(getattr(agent, "named_agent_type", "") or "")
    try:
        return default_tool_ids(agent_type, tuple(getattr(agent, "default_tools", ()) or ()), catalog)
    except ValueError:
        # The definition was checked when it loaded; a catalog without one of its tools shows nothing extra.
        logger.warning(
            "An agent's default tools are not all in the tool catalog. event=tool_loading_default_tools_unknown "
            "agent_id=%s agent_type=%s",
            getattr(agent, "id", None),
            agent_type,
        )
        return set()


def work_preloaded(agent: Any) -> set[str]:
    """Groups the agent's current work has without loading them (module docstring)."""

    from custom_tools.runtime_context import active_task_context

    _task_id, meta = active_task_context(agent)
    if meta is not None and getattr(meta, "objective_work_session_id", None):
        return {OBJECTIVES_TOOL_ID}
    return set()


def work_key(agent: Any) -> str | None:
    """The task loaded groups belong to: they leave the list when it changes."""

    from custom_tools.runtime_context import active_task_context

    return active_task_context(agent)[0]


def hidden_tool_ids(agent: Any, catalog: Any, *, at_start: bool = False) -> set[str]:
    """On-request groups not shown to this agent right now, or before anything is loaded (``at_start``)."""

    on_request = set(getattr(catalog, "on_request", {}) or {})
    if not on_request:
        return set()
    hidden = on_request - definition_tool_ids(agent, catalog)
    if at_start:
        return hidden
    return hidden - loaded_tools(agent).loaded() - work_preloaded(agent)


def is_hidden(name: str, catalog: Any, hidden: set[str]) -> bool:
    """Whether a function belongs to a hidden group and is not one of its kept-shown functions."""

    binding = catalog.functions.get(name)
    if binding is None or binding.tool_id not in hidden:
        return False
    return name not in catalog.on_request.get(binding.tool_id, frozenset())


def shown_names(agent: Any, catalog: Any, names: Iterable[str], *, at_start: bool = False) -> set[str]:
    """The function names, without those of groups hidden from this agent now (or at start)."""

    hidden = hidden_tool_ids(agent, catalog, at_start=at_start)
    return {name for name in names if not is_hidden(name, catalog, hidden)}


def group_of(name: str, catalog: Any) -> str | None:
    """The on-request group a function belongs to, or None."""

    binding = catalog.functions.get(name)
    if binding is None or binding.tool_id not in getattr(catalog, "on_request", {}):
        return None
    return str(binding.tool_id)


def loadable_tool_ids(agent: Any, catalog: Any) -> list[str]:
    """On-request groups the agent's standing tools include and does not see at once.

    With authorization off these are the groups among its definition's tools;
    with it on, those of its role's standard tool sets. Groups a grant gives
    later are found with ``find_tools``.
    """

    from custom_agents.access_runtime import agent_principal, authorization_enabled

    on_request = set(getattr(catalog, "on_request", {}) or {}) - definition_tool_ids(agent, catalog)
    if authorization_enabled(getattr(agent, "authorization_config", None)):
        from access_control.standard_grants import INTERNET_SET, STANDARD_TOOL_SETS_BY_ROLE

        sets = {*STANDARD_TOOL_SETS_BY_ROLE.get(agent_principal(agent).role, ()), INTERNET_SET}
        standing = {tool.tool_id for tool in catalog.tools.values() if set(tool.tool_sets) & sets}
    else:
        names = {tool_name(tool) for tool in getattr(agent, "tools", ()) or ()}
        standing = {tool_id for tool_id in on_request if functions_of_tools(catalog, {tool_id}) & names}
    return sorted(on_request & standing)


def loadable_tools_text(agent: Any, catalog: Any) -> str:
    """The prompt's short list of the groups the agent can load, or empty when there are none."""

    tool_ids = loadable_tool_ids(agent, catalog)
    if not tool_ids:
        return ""
    lines = [
        "Tools you can load:",
        "These tool groups are not in your tool list until you load them for the task with "
        "use_tools(tool_ids=[...]); the result says when they are ready. find_tools searches the rest of the "
        f"catalog. A loaded group leaves your list after {LOADED_TOOL_IDLE_MODEL_CALLS} model calls without use "
        "and when the task ends; load it again when you need it.",
    ]
    for tool_id in tool_ids:
        tool = catalog.tools[tool_id]
        kept = catalog.on_request.get(tool_id, frozenset())
        functions = sorted(functions_of_tools(catalog, {tool_id}) - kept - catalog.not_offered)
        summary = str(tool.description or "").split(". ")[0].rstrip(".")
        about = f": {summary}." if summary else "."
        lines.append(f"- {tool_id} ({tool.display_name}){about} Functions: {', '.join(functions)}")
    return "\n".join(lines)


# -- the cap ----------------------------------------------------------------------------


def tool_limit(agent: Any) -> tuple[int, str, str]:
    """(the most tools one request may carry, provider, model) for the model the agent's call uses."""

    from config_loader import lookup_configured_model_tool_limit
    from custom_agents.person_model_runtime import active_bundle

    bundle = active_bundle(agent)
    if bundle is not None:
        provider, model_id = bundle.provider, bundle.profile.model_id
    else:
        provider = str(getattr(agent, "model_provider", "") or "")
        model_id = str(getattr(agent, "model_id", "") or "")
    # The selection paths refresh LLM_OVERRIDES.yaml between turns, including
    # active failover leases; model-call hooks only read this cached map.
    for source in (getattr(agent, "_persistent_model_capabilities", None), getattr(agent, "model_capabilities", None)):
        limit = lookup_configured_model_tool_limit(source, provider=provider, model_id=model_id)
        if limit is not None:
            return limit, provider, model_id
    return DEFAULT_MAX_TOOLS_PER_REQUEST, provider, model_id


def shown_count(registry: Any) -> int:
    """The tools the next request carries: the agent's tools, dynamic ones and the turn's answer tool."""

    return len(registry_names(registry))


def registry_names(registry: Any) -> set[str]:
    """The names the registry shows now (copied, so another thread's change cannot break the read)."""

    return set(list(registry.registry)) | set(list(registry.dynamic_tools))


def group_functions(agent: Any, catalog: Any, tool_id: str) -> set[str]:
    """The group's offered functions, restricted to the definition with authorization off."""

    from custom_agents.access_runtime import authorization_enabled

    names = functions_of_tools(catalog, {tool_id}) - catalog.not_offered
    if not authorization_enabled(getattr(agent, "authorization_config", None)):
        names &= {tool_name(tool) for tool in getattr(agent, "tools", ()) or ()}
    return offered_names(agent, names)


def loadable_functions(agent: Any, catalog: Any, tool_id: str) -> set[str]:
    """The functions loading a group adds, excluding its already-shown functions."""

    return group_functions(agent, catalog, tool_id) - catalog.on_request.get(tool_id, frozenset())


def projected_names(agent: Any, catalog: Any, registry: Any) -> set[str]:
    """The names the next request will carry once the tool-list sync applies the groups loaded now.

    Only the sync changes the registry (it runs before each model call), so a
    load decided between calls is counted here before it is shown.
    """

    present = registry_names(registry)
    hidden = hidden_tool_ids(agent, catalog)
    leaving = {name for name in present if is_hidden(name, catalog, hidden)}
    coming: set[str] = set()
    for tool_id in loaded_tools(agent).loaded() | work_preloaded(agent):
        coming |= loadable_functions(agent, catalog, tool_id)
    return (present - leaving) | coming


class ToolLimitHook(HookProvider):
    """Before every model call, keep the request within the model's tool cap (module docstring)."""

    name = "tool_limit"

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call, order=TOOL_LIMIT_HOOK_ORDER)

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        try:
            self._fit(event.agent.tool_registry)
        except Exception:  # noqa: BLE001 - the call goes on with its tools; the provider decides
            logger.exception(
                "Could not check the agent's tools against its model's cap. event=tool_limit_check_failed agent_id=%s",
                getattr(self.agent, "id", None),
            )

    def _fit(self, registry: Any) -> None:
        from custom_agents.access_runtime import runtime_access

        limit, provider, model_id = tool_limit(self.agent)
        count = shown_count(registry)
        if count <= limit:
            return
        catalog = runtime_access(self.agent).catalog
        state = loaded_tools(self.agent)
        sync = getattr(self.agent, SYNC_ATTRIBUTE, None)
        while count > limit:
            tool_id = state.least_recently_used(excluding=work_preloaded(self.agent) | definition_tool_ids(self.agent, catalog))
            if tool_id is None or sync is None:
                break
            state.unload(tool_id)
            if tool_id in hidden_tool_ids(self.agent, catalog):
                # Through the sync, so its record of what it shows stays true.
                sync.functions.withdraw(registry, loadable_functions(self.agent, catalog, tool_id))
            logger.info(
                "A loaded tool group left the agent's tool list to stay within the model's cap. "
                "event=tool_group_unloaded agent_id=%s tool_id=%s reason=tool_limit limit=%d",
                getattr(self.agent, "id", None),
                tool_id,
                limit,
            )
            count = shown_count(registry)
        if count > limit:
            self._report(count, limit, provider, model_id)

    def _report(self, count: int, limit: int, provider: str, model_id: str) -> None:
        from access_control.people import PERSON_ROLE_ADMIN
        from custom_agents.access_runtime import send_runtime_notice_once
        from custom_messages.contracts.access_v2 import AccessNoticeKind

        agent_type = str(getattr(self.agent, "named_agent_type", "") or "an agent")
        logger.error(
            "An agent's always-shown tools are more than its model accepts in one request. "
            "event=tool_limit_exceeded agent_id=%s agent_type=%s provider=%s model=%s tools=%d limit=%d",
            getattr(self.agent, "id", None),
            agent_type,
            provider,
            model_id,
            count,
            limit,
        )
        send_runtime_notice_once(
            self.agent,
            notice_id=f"tool_limit:{agent_type}:{provider}/{model_id}",
            kind=AccessNoticeKind.TOOL_LIMIT_REACHED,
            title=f"{agent_type} has more tools than {model_id} accepts",
            summary=(
                f"Each request from {agent_type} carries {count} tools, but {model_id} ({provider}) takes at most "
                f"{limit} (max_tools_per_request), so the provider may refuse its requests. If the model accepts "
                f"more, ask TeamLead to request a higher cap for {agent_type}'s model (model.max_tools_per_request "
                f"with target_agent_type={agent_type}; it is tested with one request, then you approve it in the "
                "Inbox), or set max_tools_per_request for the model under model_capabilities in LLM_OVERRIDES.yaml. "
                "Otherwise choose another model for this agent type, or give it fewer tools."
            ),
            roles=(PERSON_ROLE_ADMIN,),
        )


class OnRequestToolHook(HookProvider):
    """A call to a function of a group not loaded gets its tool and loads the group; calls mark groups used."""

    name = "on_request_tools"

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def _may_load(self, name: str) -> bool:
        """With authorization on the access hook decides the call; with it off only the definition's tools load.

        Authorization off allows every catalog tool (decision D8), so loading a
        function the agent's definition does not give it would widen its tools.
        """

        from custom_agents.access_runtime import authorization_enabled

        if authorization_enabled(getattr(self.agent, "authorization_config", None)):
            return True
        return name in {tool_name(tool) for tool in getattr(self.agent, "tools", ()) or ()}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call, order=ON_REQUEST_TOOL_HOOK_ORDER)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        from custom_agents.access_runtime import runtime_access

        name = str(event.tool_use.get("name") or "")
        catalog = runtime_access(self.agent).catalog
        tool_id = group_of(name, catalog)
        if tool_id is None:
            return
        state = loaded_tools(self.agent)
        if event.selected_tool is not None:
            state.used(tool_id)
            return
        if tool_id not in hidden_tool_ids(self.agent, catalog):
            return
        # The agent's own tools: its definition's, which hold every group it may load.
        tool = next((item for item in getattr(self.agent, "tools", ()) or () if tool_name(item) == name), None)
        if tool is None or not offered(self.agent, [tool]) or not self._may_load(name):
            return
        state.load([tool_id])
        event.selected_tool = tool
        logger.info(
            "A call to a tool that was not loaded loaded its group. event=tool_group_loaded_on_call agent_id=%s "
            "tool_id=%s function=%s",
            getattr(self.agent, "id", None),
            tool_id,
            name,
        )


__all__ = [
    "LOADED_TOOL_IDLE_MODEL_CALLS",
    "LoadedTools",
    "OBJECTIVES_TOOL_ID",
    "OnRequestToolHook",
    "ToolLimitHook",
    "definition_tool_ids",
    "group_of",
    "group_functions",
    "hidden_tool_ids",
    "is_hidden",
    "loadable_tool_ids",
    "loadable_tools_text",
    "loaded_tools",
    "offered",
    "offered_names",
    "SYNC_ATTRIBUTE",
    "loadable_functions",
    "projected_names",
    "registry_names",
    "shown_count",
    "shown_names",
    "tool_limit",
    "work_key",
    "work_preloaded",
]
