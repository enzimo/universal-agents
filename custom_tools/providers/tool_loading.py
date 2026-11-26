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
  An MCP server (``mcp:<name>``) is never loaded this way: its tools load
  before model calls from the agent's access (``custom_agents.mcp_server_tools``),
  so use_tools and skills say that, or give the request_access call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands import tool

from access_catalog.catalog import mcp_tool_id
from access_catalog.tool_functions import functions_of_tools, mcp_function_names, mcp_tool_ids
from custom_messages.contracts.access_v2 import AccessLevel, TargetKind, TargetRefV1
from custom_tools.provider_contract import ToolProvider, tool_name
from custom_tools.runtime_context import get_agent_ref
from custom_tools.tool_search import document, rank
from custom_agents.tool_presentation import CALL_TOOL_NAME, on_request_groups, withheld_functions

logger = logging.getLogger(__name__)

MAX_FIND_RESULTS = 25
STATUS_LOADED = "loaded"
STATUS_REFUSED = "refused"
# A tool that never waits to be loaded and whose functions the agent has now.
STATUS_ALREADY_SHOWN = "already_in_your_tool_list"
# A skill's declared group in the index setups: listed in the tool index and reached with call_tool.
STATUS_IN_INDEX = "in_your_tool_index"
# An MCP server the agent may use: its tools load on their own before model calls (custom_agents.mcp_server_tools).
STATUS_LOADS_ON_ITS_OWN = "loads_on_its_own"
MCP_LOADS_ON_ITS_OWN = (
    "MCP server tools load on their own before your next model call while you have access to the server; "
    "nothing to load. find_tools shows a server's load status when its tools are missing."
)
# Every installed MCP server's catalog id starts with this (access_catalog.catalog.mcp_tool_id).
MCP_TOOL_ID_PREFIX = mcp_tool_id("")


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


def _function_texts(functions: list[str]) -> list[str]:
    """The descriptions of these trusted functions (MCP tool names have none here)."""

    from custom_tools.tool_lookup import trusted_tools

    tools = trusted_tools()
    return [str((getattr(tools[name], "tool_spec", None) or {}).get("description") or "") for name in functions if name in tools]


def _best_matches(candidates: list[tuple[Any, list[str]]], query: str) -> list[tuple[Any, list[str]]]:
    """The (tool, functions) ranked for the query, best first; all of them without a query.

    Ranking is ``custom_tools.tool_search.rank``: weighted words with rarity
    across tools. A tool that matches only some words still counts: a request
    in plain words ("save a team memory fact") rarely uses only words a tool's
    description has, and requiring every word found nothing, which agents
    read as having no such tool.
    """

    if not str(query or "").strip():
        return candidates
    by_id = {tool_def.tool_id: (tool_def, functions) for tool_def, functions in candidates}
    documents = [document(tool_def, functions, _function_texts(functions)) for tool_def, functions in candidates]
    return [by_id[doc.tool_id] for _score, doc in rank(documents, query)]


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


def _shown_candidate(catalog: Any, tool_def: Any, withheld: frozenset[str]) -> tuple[Any, list[str]] | None:
    """A catalog tool and the functions find_tools reports for it, or None when this setting withholds them all.

    An MCP server's functions are its discovered tools, ``<prefix>_<tool>``. Withheld
    functions (call_tool in full_definitions, run_tool_script when scripts are off) are
    left out, and a tool whose every function is withheld is not offered at all. An MCP
    server with no tools discovered yet has no catalog functions, so it stays listed
    with its load status.
    """

    own = functions_of_tools(catalog, {tool_def.tool_id})
    mcp = mcp_function_names(catalog, tool_def.tool_id)
    if own and own <= withheld and not mcp:
        return None
    return tool_def, sorted((own - withheld) | mcp)


def _find_with_authorization(agent: Any, query: str) -> list[dict[str, Any]]:
    from custom_agents.mcp_server_tools import server_status
    from custom_agents.tool_loading import hidden_tool_ids
    from custom_tools.providers.access import _context

    _agent_ref, requests, principal, person, _task_id = _context()
    task_id = principal.work_id
    person_id = person.person_id if person else None
    catalog = requests.service.catalog
    hidden = hidden_tool_ids(agent, catalog)
    withheld = withheld_functions(agent, catalog)
    grants = requests.service.agent_grants(principal, task_id=task_id, person_id=person_id)
    candidates = [
        candidate
        for tool_def in sorted(catalog.tools.values(), key=lambda t: t.tool_id)
        if (candidate := _shown_candidate(catalog, tool_def, withheld)) is not None
    ]
    results: list[dict[str, Any]] = []
    for tool_def, functions in _best_matches(candidates, query):
        held = _available_level(requests, principal, person, tool_def.tool_id, grants)
        found = _found_tool(tool_def, functions, held, server_status(agent, tool_def.tool_id))
        found.update(_loading_view(agent, catalog, tool_def.tool_id, functions, hidden if held else set()))
        results.append(found)
        if len(results) >= MAX_FIND_RESULTS:
            break
    return results


def _find_without_authorization(agent: Any, query: str) -> list[dict[str, Any]]:
    """The tools the agent's definition gives it (authorization off has no access to ask for)."""

    from custom_agents.access_runtime import runtime_access
    from custom_agents.tool_loading import hidden_tool_ids

    catalog = runtime_access(agent).catalog
    hidden = hidden_tool_ids(agent, catalog)
    withheld = withheld_functions(agent, catalog)
    names = {tool_name(tool) for tool in getattr(agent, "tools", ()) or ()}
    candidates = [
        (tool_def, sorted((functions_of_tools(catalog, {tool_def.tool_id}) & names) - withheld))
        for tool_def in sorted(catalog.tools.values(), key=lambda t: t.tool_id)
    ]
    results: list[dict[str, Any]] = []
    for tool_def, functions in _best_matches([(t, f) for t, f in candidates if f], query):
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

    Describe what you need in plain words; the best matches come first.
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
        text = str(query or "")
        try:
            results = _find_with_authorization(agent, text) if _authorization_on(agent) else _find_without_authorization(agent, text)
        except AuthorizationOff:
            results = _find_without_authorization(agent, text)
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


def _mcp_server_answer(agent: Any, catalog: Any, tool_id: str) -> dict[str, Any]:
    """What use_tools and skills say about an MCP server: nothing loads it, so say what is true.

    An MCP server has no catalog functions and never waits in on_request; the
    tool-list sync loads its tools before model calls from the agent's access
    (``custom_agents.mcp_server_tools``). With authorization on the access
    decision says whether the agent may use it (a missing grant gets the
    request_access call); with it off the server shows for the agent types
    its record names. Loads nothing and grants nothing.
    """

    from custom_agents.mcp_server_tools import server_loader, server_status

    if _authorization_on(agent):
        refusal = _may_load(agent, catalog, tool_id)
    else:
        servers = server_loader(agent)
        refusal = "" if servers is not None and servers.shows(tool_id) else f"{tool_id} is not set up for your agent type."
    if refusal:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": refusal}
    registry = _registry(agent)
    names = (set(registry.registry) | set(registry.dynamic_tools)) if registry is not None else set()
    shown = sorted(mcp_function_names(catalog, tool_id) & names)
    if shown:
        return {"tool_id": tool_id, "status": STATUS_ALREADY_SHOWN, "functions": shown}
    result: dict[str, Any] = {"tool_id": tool_id, "status": STATUS_LOADS_ON_ITS_OWN, "reason": MCP_LOADS_ON_ITS_OWN}
    if load_status := server_status(agent, tool_id):
        result["load_status"] = load_status
    return result


def _load_one(agent: Any, catalog: Any, tool_id: str, keep: set[str]) -> dict[str, Any]:
    from custom_agents.tool_loading import group_functions, hidden_tool_ids, loadable_functions, loaded_tools, tool_limit

    if tool_id not in catalog.tools:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": "Not in the tool catalog; find_tools lists tools."}
    if mcp_tool_ids(catalog, [tool_id]):
        return _mcp_server_answer(agent, catalog, tool_id)
    if tool_id not in on_request_groups(agent, catalog):
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
    if servers := [item["tool_id"] for item in results if item["status"] == STATUS_LOADS_ON_ITS_OWN]:
        parts.append(f"{', '.join(servers)}: {MCP_LOADS_ON_ITS_OWN}")
    return " ".join(parts) or "Nothing was loaded."


def load_tool_groups(agent: Any, catalog: Any, tool_ids: list[str]) -> list[dict[str, Any]]:
    """Load these groups for the agent's task, one result per group; ``use_tools`` and skills share it.

    Strands can run tool calls concurrently. Capacity is reserved and the
    loaded set changed together under the agent's ``LoadedTools`` lock,
    counting every group in this call.
    """

    from custom_agents.tool_loading import loaded_tools

    with loaded_tools(agent).lock:
        return [_load_one(agent, catalog, tool_id, set(tool_ids)) for tool_id in tool_ids]


CALL_TOOL_HINT = f"{CALL_TOOL_NAME}(name='<function>', inputs={{...}})"


def _skill_note(skill_name: str, results: list[dict[str, Any]]) -> str:
    """What the skill result tells the model about the groups the skill declares."""

    def ids(status: str) -> list[str]:
        return [item["tool_id"] for item in results if item["status"] == status]

    lines = [f"Tool groups the skill {skill_name} uses (ua-tools):"]
    if loaded := ids(STATUS_LOADED):
        lines.append(f"- Loaded for this task: {', '.join(loaded)}. Their functions are in your tool list from your next step.")
    if in_index := ids(STATUS_IN_INDEX):
        lines.append(
            f"- In your tool index, nothing to load: {', '.join(in_index)}. "
            f"Call their functions in one step with {CALL_TOOL_HINT}."
        )
    if shown := ids(STATUS_ALREADY_SHOWN):
        lines.append(f"- Already in your tool list: {', '.join(shown)}.")
    if servers := ids(STATUS_LOADS_ON_ITS_OWN):
        lines.append(f"- {', '.join(servers)}: {MCP_LOADS_ON_ITS_OWN}")
    lines.extend(
        f"- {item['tool_id']} has not loaded: {item['load_status']}" for item in results if item.get("load_status")
    )
    lines.extend(f"- Not loaded, {item['tool_id']}: {item['reason']}" for item in results if item["status"] == STATUS_REFUSED)
    if unloaded := sorted({tool_id for item in results for tool_id in item.get("unloaded_to_make_room", ())}):
        lines.append(f"- Unloaded to make room: {', '.join(unloaded)}. Call one of their functions to load it again.")
    return "\n".join(lines)


def _index_group(agent: Any, catalog: Any, tool_id: str) -> dict[str, Any]:
    """A declared catalog function group in the index setups: reported, never loaded.

    The index already lists the group's held, offered functions with their
    signatures and call_tool reaches them in one step, so loading their full
    definitions would put back the tokens the index saves (lead ruling at turn
    4 of the round-two run). A group the agent lacks gets use_tools' own
    refusal, with the request_access call.
    """

    from custom_agents.tool_loading import group_functions

    refusal = _may_load(agent, catalog, tool_id)
    if refusal:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": refusal}
    offered_functions = group_functions(agent, catalog, tool_id)
    if not offered_functions:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": "Your tool settings offer no functions from this group."}
    # Withheld functions (run_tool_script with scripts off) are neither shown nor indexed, and call_tool refuses them.
    functions = offered_functions - withheld_functions(agent, catalog)
    if not functions:
        return {"tool_id": tool_id, "status": STATUS_REFUSED, "reason": WITHHELD_FUNCTION_ERROR}
    if functions <= set(_shown_functions(agent, catalog, tool_id)):
        return {"tool_id": tool_id, "status": STATUS_ALREADY_SHOWN, "functions": sorted(functions)}
    return {"tool_id": tool_id, "status": STATUS_IN_INDEX, "functions": sorted(functions)}


def _skill_group_results(agent: Any, catalog: Any, tool_ids: list[str]) -> list[dict[str, Any]]:
    """One result per declared group, in declared order.

    In full_definitions every group goes through use_tools' path. In the
    index setups only MCP server groups do (the index cannot list their
    tools); catalog function groups are reported by ``_index_group``.
    """

    from custom_agents.tool_presentation import index_enabled

    if not index_enabled(agent):
        return load_tool_groups(agent, catalog, tool_ids)
    servers = mcp_tool_ids(catalog, tool_ids)
    by_id = {item["tool_id"]: item for item in load_tool_groups(agent, catalog, [t for t in tool_ids if t in servers])}
    by_id.update({tool_id: _index_group(agent, catalog, tool_id) for tool_id in tool_ids if tool_id not in servers})
    return [by_id[tool_id] for tool_id in tool_ids]


def load_skill_tool_groups(agent: Any, skill_name: str, metadata: Any) -> str:
    """Load the groups a skill declares under ``ua-tools`` when it is activated; the note for the skill result.

    The same per-group path as ``use_tools``: only groups the agent holds and
    is offered load, within the model's tool cap, and nothing is granted. In
    the index setups catalog function groups are not loaded but reported as
    in the index (``_skill_group_results``). An id that is no catalog tool is
    skipped with a warning (the skill is wrong, not the agent;
    ``scripts/check_skill_tool_names.py`` finds these). Returns empty when the
    skill declares no known group.
    """

    from custom_agents.access_runtime import runtime_access
    from custom_agents.tool_loading import declared_skill_tool_ids

    declared = declared_skill_tool_ids(metadata, skill_name=skill_name)
    if not declared:
        return ""
    catalog = runtime_access(agent).catalog
    unknown = [tool_id for tool_id in declared if tool_id not in catalog.tools]
    if unknown:
        logger.warning(
            "A skill declares tool ids that are not in the tool catalog; skipping them. "
            "event=skill_declared_tool_unknown agent_id=%s skill_name=%s tool_ids=%s",
            getattr(agent, "id", None),
            skill_name,
            ",".join(unknown),
        )
    known = [tool_id for tool_id in declared if tool_id in catalog.tools]
    if not known:
        return ""
    results = _skill_group_results(agent, catalog, known)
    logger.info(
        "Activating a skill handled the tool groups it declares. event=skill_tool_groups_loaded agent_id=%s "
        "skill_name=%s loaded=%s in_index=%s refused=%s",
        getattr(agent, "id", None),
        skill_name,
        ",".join(item["tool_id"] for item in results if item["status"] == STATUS_LOADED),
        ",".join(item["tool_id"] for item in results if item["status"] == STATUS_IN_INDEX),
        ",".join(item["tool_id"] for item in results if item["status"] == STATUS_REFUSED),
    )
    return _skill_note(skill_name, results)


def skill_tools_fallback_note(agent: Any, tool_ids: list[str]) -> str:
    """What a skill result says when loading its declared groups failed: how to reach them without the loader.

    In the index setups catalog function groups are called through call_tool;
    in full_definitions they load with use_tools. MCP servers (ids from
    ``mcp_tool_id``) load on their own in every setting. Needs no catalog,
    because the catalog may be what failed.
    """

    from custom_agents.tool_presentation import index_enabled

    servers = [tool_id for tool_id in tool_ids if tool_id.startswith(MCP_TOOL_ID_PREFIX)]
    groups = [tool_id for tool_id in tool_ids if tool_id not in servers]
    parts = ["The tool groups this skill uses could not be checked automatically."]
    if groups and index_enabled(agent):
        parts.append(f"Call the functions of {', '.join(groups)} from your tool index with {CALL_TOOL_HINT}.")
    elif groups:
        parts.append(f"Load them with {_use_tools_ids(groups)} if the procedure needs them.")
    if servers:
        parts.append(f"{', '.join(servers)}: {MCP_LOADS_ON_ITS_OWN}")
    return " ".join(parts)


def _use_tools_ids(tool_ids: list[str]) -> str:
    return f"use_tools(tool_ids={tool_ids!r})"


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
    from custom_tools.providers.access import _guarded

    def run() -> str:
        agent = _agent()
        catalog = runtime_access(agent).catalog
        wanted = list(dict.fromkeys(str(item or "").strip() for item in (tool_ids or []) if str(item or "").strip()))
        if not wanted:
            raise ValueError("Name at least one tool group, for example tool_ids=['fn:objectives'].")
        results = load_tool_groups(agent, catalog, wanted)
        return _json({"tools": results, "message": _summary(results)})

    return _guarded(run)


MAX_DESCRIBED = 10


def _closest_names(name: str, known: set[str]) -> list[str]:
    import difflib

    return difflib.get_close_matches(name, sorted(known), n=3, cutoff=0.5)


WITHHELD_FUNCTION_ERROR = "Not available with this team's tool presentation setting."


def _described_entry(
    name: str, tools: Any, catalog: Any, held: set[str], withheld: frozenset[str], *, scripts: bool
) -> dict[str, Any]:
    """What describe_tools answers for one name; a withheld function is not described."""

    from custom_agents.tool_index import described

    if name in withheld:
        return {"name": name, "error": WITHHELD_FUNCTION_ERROR}
    tool_def = tools.get(name)
    if tool_def is None:
        return {"name": name, "error": "No tool function has this name.", "closest": _closest_names(name, set(tools) - withheld)}
    entry = described(name, tool_def, catalog, scripts=scripts)
    entry["you_hold_it"] = name in held
    if not entry["you_hold_it"]:
        entry["how_to_get"] = "find_tools shows whether you may use it and how to ask for access."
    return entry


@tool
def describe_tools(names: list[str]) -> str:
    """Get the exact inputs of tool functions before calling them, such as names=['knowledge_search'].

    For each function: its signature, its full description, the group
    use_tools loads for it, and whether you hold it now. When tool scripts are
    on, also whether run_tool_script may call it. A name that is no tool
    function comes back with the closest real names. Describing a tool never
    loads it or gives access.
    """

    from custom_agents.access_runtime import runtime_access
    from custom_agents.strands_parts import current_parts
    from custom_agents.tool_index import held_functions
    from custom_agents.tool_presentation import scripts_enabled
    from custom_tools.tool_lookup import trusted_tools
    from custom_tools.providers.access import _guarded

    def run() -> str:
        agent = _agent()
        wanted = list(dict.fromkeys(str(item or "").strip() for item in (names or []) if str(item or "").strip()))
        if not wanted:
            raise ValueError("Name at least one tool function, for example names=['knowledge_search'].")
        catalog = runtime_access(agent).catalog
        tools = trusted_tools()
        held = held_functions(agent, current_parts(agent))
        withheld = withheld_functions(agent, catalog)
        scripts = scripts_enabled(agent)
        results = [_described_entry(name, tools, catalog, held, withheld, scripts=scripts) for name in wanted[:MAX_DESCRIBED]]
        payload: dict[str, Any] = {"tools": results}
        if len(wanted) > MAX_DESCRIBED:
            payload["not_described"] = wanted[MAX_DESCRIBED:]
        return _json(payload)

    return _guarded(run)


@tool
def call_tool(name: str, inputs: dict[str, Any] | None = None) -> str:
    """Call a tool function listed in your tool index in one step, without loading its group first.

    Args:
        name: Exact name of the catalog tool function to call (for example 'knowledge_search').
        inputs: Named arguments for that function as a JSON object; omit or pass {} when it takes none.
    """

    # CallToolHook rewrites every valid call_tool invocation into the real tool
    # call before execution. This fallback body runs only if invoked directly
    # outside the index runtime hook, and fails closed by raising so Strands
    # records status='error' (and ChangesFirstToolExecutor stops the step).
    del name, inputs
    raise RuntimeError(
        "call_tool cannot execute outside the tool index runtime hook. "
        "Call the target function directly or enable the tool index."
    )


TOOL_LOADING_TOOLS = (find_tools, describe_tools, use_tools, call_tool)
PROVIDER = ToolProvider("tool_loading", TOOL_LOADING_TOOLS)

__all__ = [
    "PROVIDER",
    "STATUS_ALREADY_SHOWN",
    "STATUS_IN_INDEX",
    "STATUS_LOADS_ON_ITS_OWN",
    "TOOL_LOADING_TOOLS",
    "call_tool",
    "describe_tools",
    "find_tools",
    "load_skill_tool_groups",
    "load_tool_groups",
    "skill_tools_fallback_note",
    "use_tools",
]
