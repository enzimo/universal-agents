"""Live: a real TeamArchitect uses the action tools that replaced several functions each.

A tool that takes an ``action`` argument (custom_tools.action_inputs) only
helps if models call it correctly, so this asks a real TeamArchitect, on the
configured LiteLLM endpoint, for work that needs one, in both authorization
modes, and checks the calls and the stored result.

Run with ``RUN_LIVE_TOOL_LOADING=1`` and the settings described in
``tests/test_live_tool_loading.py``.
"""

from __future__ import annotations

import os

import pytest

from tests.test_live_tool_loading import MAX_MODEL_CALLS, answered, ask, team_lead

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TOOL_LOADING") != "1",
        reason="Set RUN_LIVE_TOOL_LOADING=1 to call the configured model provider.",
    ),
]


@pytest.mark.parametrize("authorization", [False, True], ids=["authorization_off", "authorization_on"])
def test_a_real_team_lead_saves_and_reads_a_team_fact_with_the_memory_tool(tmp_path, monkeypatch, authorization) -> None:
    from custom_agents import access_runtime

    agent, watch = team_lead(tmp_path, monkeypatch, authorization)

    result = ask(
        agent,
        "Remember this for the whole team: deployments happen on Fridays. Save it as a team memory fact, then "
        "list the team's memory facts and tell me in one short sentence what is saved.",
    )

    calls = [(inputs.get("action"), inputs.get("bank")) for name, inputs in watch.calls if name == "memory"]
    assert ("create", "coremem") in calls and ("list", "coremem") in calls, watch.calls
    assert len(watch.request_sizes) <= MAX_MODEL_CALLS, (len(watch.request_sizes), watch.calls)
    answered(result)
    facts = agent._pinned_memory_repository.list_coremem(team_id="team_live")
    assert any("friday" in fact.fact_text.lower() for fact in facts), facts
    access_runtime._runtime_access.cache_clear()


@pytest.mark.parametrize("authorization", [False, True], ids=["authorization_off", "authorization_on"])
def test_a_real_team_lead_loads_its_capability_tools_and_lists_mcp_connections(tmp_path, monkeypatch, authorization) -> None:
    from custom_agents import access_runtime

    agent, watch = team_lead(tmp_path, monkeypatch, authorization)

    result = ask(
        agent,
        "Which MCP connections does the team have? Use your capability tools to list them (load them first if "
        "they are not in your tool list), then answer in one short sentence.",
    )

    actions = [inputs.get("action") for name, inputs in watch.calls if name == "mcp_connections"]
    assert "list" in actions, watch.calls
    assert max(watch.request_sizes) <= 128 and len(watch.request_sizes) <= MAX_MODEL_CALLS, (watch.request_sizes, watch.calls)
    answered(result)
    access_runtime._runtime_access.cache_clear()
