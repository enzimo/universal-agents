"""Finding tools and loading the ones that wait until a task needs them (``custom_agents.tool_loading``).

Every agent holds these through its role's standard tool set
(``agent_runtime``), in both authorization modes:

- ``find_tools`` searches the tool catalog. Each result says whether the tool
  is in the agent's tool list now, the ``use_tools`` call that loads it when
  it loads on request, and, with authorization on, the levels it needs, the
  agent's level and the ``request_access`` call that asks for it.
- ``use_tools`` adds groups that load on request to the agent's tool list for
  its current task, from its next model call. It changes what the model is
  shown, never what it may use. A load that would make one request carry more
  tools than the model takes (``max_tools_per_request``) first unloads the
  least recently used loaded groups, and is refused if it still does not fit.
"""

from __future__ import annotations

import json
from typing import Any

from strands import tool

from access_catalog.tool_functions import functions_of_tools, mcp_function_names
from custom_messages.contracts.access_v2 import AccessLevel, TargetKind, TargetRefV1
from custom_tools.provider_contract import ToolProvider, tool_name
from custom_tools.runtime_context import get_agent_ref

MAX_FIND_RESULTS = 25
STATUS_LOADED = "loaded"
STATUS_REFUSED = "refused"
# A tool that never waits to be loaded and whose functions the agent has now.
STATUS_ALREADY_SHOWN = "already_in_your_tool_list"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, sort_keys=True)


def _agent() -> Any:
    agent = get_agent_ref()
    if agent is None:
        raise RuntimeError("No agent is running this tool.")
    return agent


def _authorization_on(agent: Any) -> bool:
    from custom_agents.access_runtime import authorization_enabled

    return authorization_enabled(getattr(agent, "authorization_config", None))


def _use_tools_call(tool_id: str) -> str:
    return f"use_tools(tool_ids=[{tool_id!r}])"


def _match_count(tool_def: Any, functions: list[str], words: list[str]) -> int:
    """How many of the search words appear in the tool's id, name, description, operations or functions."""

    text = " ".join(
        [tool_def.tool_id, tool_def.display_name, tool_def.description, *(o.name for o in tool_def.operations), *functions]
    ).lower()
    return sum(1 for word in set(words) if word in text)


def _best_matches(candidates: list[tuple[Any, list[str]]], words: list[str]) -> list[tuple[Any, list[str]]]:
    """The (tool, functions) that match the most search words, best first; all of them without words.

    A tool that matches only some words still counts: a request in plain
    words ("save a team memory fact") rarely uses only words a tool's
    description has, and requiring every word found nothing, which agents
    read as having no such tool.
    """

    if not words:
        return candidates
    scored = [(_match_count(tool_def, functions, words), tool_def, functions) for tool_def, functions in candidates]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: (-item[0], item[1].tool_id))
    return [(tool_def, functions) for _count, tool_def, functions in scored]


def _registry(agent: Any) -> Any | None:
    """The tool registry of the Strands agent the current turn calls (custom_agents.strands_parts)."""

    from custom_agents.strands_parts import turn_strands_agent

    return getattr(turn_strands_agent(agent), "tool_registry", None)


def _shown_functions(agent: Any, catalog: Any, tool_id: str) -> list[str]:
    """The tool's functions in the agent's tool list now."""

    registry = _registry(agent)
    if registry is None:
        return []
    return sorted(functions_of_tools(catalog, {tool_id}) & (set(registry.registry) | set(registry.dynamic_tools)))


def _in_tool_list(agent: Any, functions: list[str]) -> bool:
    registry = _registry(agent)
    if registry is None or not functions:
        return False
    names = set(registry.registry) | set(registry.dynamic_tools)
    return all(name in names for name in functions)


def _loading_view(agent: Any, catalog: Any, tool_id: str, functions: list[str], hidden: set[str]) -> dict[str, Any]:
    """Whether the tool is in the agent's list, and how to load it when it waits to be loaded."""

    view: dict[str, Any] = {"in_your_tool_list": _in_tool_list(agent, functions)}
    if tool_id in hidden:
        view["loads_on_request"] = True
        view["load_with"] = _use_tools_call(tool_id)
    return view


def _found_tool(tool_def: Any, functions: list[str], held: AccessLevel | None, load_status: str | None) -> dict[str, Any]:
    """One find_tools result with authorization on: what the tool gives, the levels it needs, and how to ask.

    ``load_status`` says why an MCP server's tools are not in the agent's list
    although it may use them (``custom_agents.mcp_server_tools``).
    """

    levels = sorted({o.level for o in tool_def.operations}, key=lambda level: level.rank)
    wanted = levels[-1] if levels else AccessLevel.READ
    found: dict[str, Any] = {
        "tool_id": tool_def.tool_id,
        "name": tool_def.display_name,
        "description": tool_def.description[:300],
        "functions": functions,
        "operation_levels": [level.value for level in levels],
        "your_level": held.value if held else None,
        "how_to_get": None
        if held is not None and held.rank >= wanted.rank
        else f"request_access(tool_id={tool_def.tool_id!r}, level='<read|write|full>', reason='<why>')",
    }
    if load_status:
        found["load_status"] = load_status
    return found


def _find_with_authorization(agent: Any, words: list[str]) -> list[dict[str, Any]]:
    from custom_agents.mcp_server_tools import server_status
    from custom_agents.tool_loading import hidden_tool_ids
    from custom_tools.providers.access import _context

    _agent_ref, requests, principal, person, _task_id = _context()
    task_id = principal.work_id
    person_id = person.person_id if person else None
    catalog = requests.service.catalog
    hidden = hidden_tool_ids(agent, catalog)
    grants = requests.service.agent_grants(principal, task_id=task_id, person_id=person_id)
    # An MCP server's functions are its discovered tools, <prefix>_<tool>.
    candidates = [
        (tool_def, sorted(functions_of_tools(catalog, {tool_def.tool_id}) | mcp_function_names(catalog, tool_def.tool_id)))
        for tool_def in sorted(catalog.tools.values(), key=lambda t: t.tool_id)
    ]
    results: list[dict[str, Any]] = []
    for tool_def, functions in _best_matches(candidates, words):
        held = _available_level(requests, principal, person, tool_def.tool_id, grants)
        found = _found_tool(tool_def, functions, held, server_status(agent, tool_def.tool_id))
        found.update(_loading_view(agent, catalog, tool_def.tool_id, functions, hidden if held else set()))
        results.append(found)
        if len(results) >= MAX_FIND_RESULTS:
            break
    return results


def _find_without_authorization(agent: Any, words: list[str]) -> list[dict[str, Any]]:
    """The tools the agent's definition gives it (authorization off has no access to ask for)."""

    from custom_agents.access_runtime import runtime_access
    from custom_agents.tool_loading import hidden_tool_ids

    catalog = runtime_access(agent).catalog
    hidden = hidden_tool_ids(agent, catalog)
    names = {tool_name(tool) for tool in getattr(agent, "tools", ()) or ()}
    candidates = [
        (tool_def, sorted(functions_of_tools(catalog, {tool_def.tool_id}) & names))
        for tool_def in sorted(catalog.tools.values(), key=lambda t: t.tool_id)
    ]
    results: list[dict[str, Any]] = []
    for tool_def, functions in _best_matches([(t, f) for t, f in candidates if f], words):
        results.append(
            {
                "tool_id": tool_def.tool_id,
                "name": tool_def.display_name,
                "description": tool_def.description[:300],
                "functions": functions,
                **_loading_view(agent, catalog, tool_def.tool_id, functions, hidden),
            }
        )
        if len(results) >= MAX_FIND_RESULTS:
            break
    return results


@tool
def find_tools(query: str = "") -> str:
    """Search the tool catalog by purpose or function name, including tools not in your tool list now.

    Tools matching the most words of the query come first.
    Some tool groups wait until a task needs them: a result with
    loads_on_request gives the use_tools call that adds it to your list from
    your next step. With access control on, each result also shows the levels
    its operations need, your level, and the request_access call that asks for
    it; once access is granted, its functions are in your list from your next
    step. For an MCP server whose tools did not load, load_status says why
    (not answering, or refused) and when it is tried again; tell the person.
    """

    from custom_agents.access_runtime import AuthorizationOff
    from custom_tools.providers.access import _guarded

    def run() -> str:
        agent = _agent()
        words = [word for word in str(query or "").lower().split() if word]
        try:
            results = _find_with_authorization(agent, words) if _authorization_on(agent) else _find_without_authorization(agent, words)
        except AuthorizationOff:
            results = _find_without_authorization(agent, words)
        return _json({"query": query, "tools": results, "truncated": len(results) >= MAX_FIND_RESULTS})

    return _guarded(run)


# -- use_tools ---------------------------------------------------------------------


def _available_level(requests: Any, principal: Any, person: Any, tool_id: str, grants: list[Any]) -> AccessLevel | None:
    """Held access or access the decision may automatically hand on; this writes nothing."""

    target = TargetRefV1(kind=TargetKind.TOOL, id=tool_id)
    person_id = person.person_id if person else None
    from custom_tools.providers.access import _held_level

    held = _held_level(requests, principal, target, principal.work_id, grants, person_id)
    if held is not None:
        return held
    for level in (AccessLevel.FULL, AccessLevel.WRITE, AccessLevel.READ):
        if requests.service.may_hand_on_for_task(principal, person, target, level, principal.work_id):
            return level
    return None


def _may_load(agent: Any, catalog: Any, tool_id: str) -> str:
    """Why the agent may not load a group, or empty when it may."""

    if _authorization_on(agent):
        from custom_tools.providers.access import _context

        _agent_ref, requests, principal, person, _task_id = _context()
        person_id = person.person_id if person else None
        grants = requests.service.agent_grants(principal, task_id=principal.work_id, person_id=person_id)
        if _available_level(requests, principal, person, tool_id, grants) is None:
            return (
                f"You have no access to {tool_id} for this work. Ask with "
                f"request_access(tool_id={tool_id!r}, level='<read|write|full>', reason='<why>')."
            )
        return ""
    names = {tool_name(item) for item in getattr(agent, "tools", ()) or ()}
    if not functions_of_tools(catalog, {tool_id}) & names:
        return f"{tool_id} is not one of your tools."
    return ""


def _fits(agent: Any, catalog: Any, tool_id: str, keep: set[str]) -> tuple[bool, list[str], int, int]:
    """Whether loading a group keeps the next request within the model's cap.

    (fits, groups to unload to make room, least recently used first, tools it
    adds, the cap). It counts what the next request will carry once the
    tool-list sync applies everything loaded now (``projected_names``), and it
    changes nothing itself: only the sync changes the registry.
    """

    from custom_agents.tool_loading import definition_tool_ids, loadable_functions, loaded_tools, projected_names, tool_limit, work_preloaded

    limit, _provider, _model = tool_limit(agent)
    registry = _registry(agent)
    adding = loadable_functions(agent, catalog, tool_id)
    if registry is None:
        return True, [], len(adding), limit
    projected = projected_names(agent, catalog, registry)
    new = adding - projected
    if len(projected) + len(new) <= limit:
        return True, [], len(new), limit
    chosen: list[str] = []
    remaining = set(projected)
    required = work_preloaded(agent) | definition_tool_ids(agent, catalog)
    for oldest in loaded_tools(agent).by_least_recent(excluding=keep | {tool_id} | required):
        chosen.append(oldest)
        remaining -= loadable_functions(agent, catalog, oldest) - adding
        if len(remaining | adding) <= limit:
            return True, chosen, len(new), limit
    return False, [], len(new), limit


def _higher_cap_hint(agent: Any, model_id: str) -> str:
    """How an agent at its model's tool cap asks for more (custom_agents.model_tool_limit).

    Only TeamLead stages runtime settings, so a worker reports the need and
    TeamLead asks for its agent type's model.
    """

    if getattr(agent, "is_team_lead", False):
        return (
            "When the task needs them all at once, ask for a higher cap with "
            "plan_runtime_setting_change(path='model.max_tools_per_request', value=<tools>, reason='<why>'); "
            "an administrator approves it, and it applies only after one request with that many tools succeeds."
        )
    agent_type = str(getattr(agent, "named_agent_type", "") or "your agent type")
    return (
        "When the task needs them all at once, say so in your result: TeamLead can ask an administrator to raise "
        f"the tool cap of {model_id} for {agent_type}."
    )


def _load_one(agent: Any, catalog: Any, tool_id: str, keep: set[str]) -> dict[str, Any]:
    from custom_agents.tool_loading import group_functions, hidden_tool_ids, loadable_functions, loaded_tools, tool_limit

    if tool_id not in catalog.tools:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": "Not in the tool catalog; find_tools lists tools."}
    if tool_id not in catalog.on_request:
        # Nothing to load. Saying "refused" here made agents conclude they had
        # no access to tools they were holding (memory, 2026-10-10).
        shown = _shown_functions(agent, catalog, tool_id)
        if shown:
            return {
                "tool_id": tool_id,
                "status": STATUS_ALREADY_SHOWN,
                "functions": shown,
                "reason": "Nothing to load: these functions are always in your tool list. Call them directly.",
            }
        return {
            "tool_id": tool_id,
            "status": STATUS_REFUSED,
            "reason": "This tool does not load on request, and none of its functions is in your tool list now; "
            "find_tools shows whether you may use it and how to ask for access.",
        }
    refusal = _may_load(agent, catalog, tool_id)
    if refusal:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": refusal}
    functions = group_functions(agent, catalog, tool_id)
    if not functions:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": "Your tool settings offer no functions from this group."}
    if tool_id not in hidden_tool_ids(agent, catalog):
        loaded_tools(agent).used(tool_id)
        return {"tool_id": tool_id, "status": STATUS_LOADED, "functions": sorted(functions)}
    fits, unloaded, adding, limit = _fits(agent, catalog, tool_id, keep)
    if not fits:
        _limit, provider, model_id = tool_limit(agent)
        return {
            "tool_id": tool_id,
            "status": STATUS_REFUSED,
            "reason": (
                f"Loading {tool_id} adds {adding} tools, and {model_id} ({provider}) takes at most {limit} in one "
                "request even after unloading what you are not using. Finish with the tools you have loaded first. "
                + _higher_cap_hint(agent, model_id)
            ),
        }
    state = loaded_tools(agent)
    for oldest in unloaded:
        state.unload(oldest)
    state.load([tool_id])
    result: dict[str, Any] = {
        "tool_id": tool_id,
        "status": STATUS_LOADED,
        "functions": sorted(loadable_functions(agent, catalog, tool_id)),
    }
    if unloaded:
        result["unloaded_to_make_room"] = unloaded
    return result


def _summary(results: list[dict[str, Any]]) -> str:
    loaded = [item["tool_id"] for item in results if item["status"] == STATUS_LOADED]
    shown = [item["tool_id"] for item in results if item["status"] == STATUS_ALREADY_SHOWN]
    parts = []
    if loaded:
        parts.append(f"Loaded {', '.join(loaded)}: their functions are in your tool list from your next step.")
    if shown:
        parts.append(f"Already in your tool list: {', '.join(shown)}; call their functions directly.")
    return " ".join(parts) or "Nothing was loaded."


@tool
def use_tools(tool_ids: list[str]) -> str:
    """Load tool groups that wait until a task needs them, such as tool_ids=['fn:objectives'].

    The prompt's "Tools you can load" list and find_tools name them. Their
    functions are in your tool list from your next step, for this task. A
    loaded group leaves your list after several model calls without use and
    when the task ends; load it again when you need it. Loading never gives
    access: with access control on, a group you have no access to is refused
    with the request_access call that asks for it.
    """

    from custom_agents.access_runtime import runtime_access
    from custom_agents.tool_loading import loaded_tools
    from custom_tools.providers.access import _guarded

    def run() -> str:
        agent = _agent()
        catalog = runtime_access(agent).catalog
        wanted = list(dict.fromkeys(str(item or "").strip() for item in (tool_ids or []) if str(item or "").strip()))
        if not wanted:
            raise ValueError("Name at least one tool group, for example tool_ids=['fn:objectives'].")
        # Strands can run use_tools calls concurrently. Reserve capacity and
        # change the loaded set together, including every group in this call.
        with loaded_tools(agent).lock:
            results = [_load_one(agent, catalog, tool_id, set(wanted)) for tool_id in wanted]
        return _json({"tools": results, "message": _summary(results)})

    return _guarded(run)


TOOL_LOADING_TOOLS = (find_tools, use_tools)
PROVIDER = ToolProvider("tool_loading", TOOL_LOADING_TOOLS)

__all__ = ["PROVIDER", "STATUS_ALREADY_SHOWN", "TOOL_LOADING_TOOLS", "find_tools", "use_tools"]
