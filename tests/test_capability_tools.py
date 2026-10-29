"""The merged capability tools (custom_tools.providers.capabilities).

``mcp_connections``, ``hosted_mcp_servers``, ``installed_permissions`` and
``specialist_definitions`` each replace several functions, one action per
call. The services behind them keep their own tests
(tests/test_mcp_follows_grants.py, tests/test_mcp_hosting.py,
tests/test_installed_permissions.py, tests/test_generated_agent_provisioning.py);
these check what the merge adds: each action reaches its service, the inputs
each action takes are checked before anything happens, and access is decided
per action with real Cedar, so a block can still stop exactly one action.
"""

from __future__ import annotations

import asyncio
import contextvars
from pathlib import Path
from typing import Any

import pytest

import custom_agents.agent_catalog  # noqa: F401 - installs the agent catalog lookup, as runtime composition does
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
from custom_tools.providers.capabilities import (
    hosted_mcp_servers,
    installed_permissions,
    mcp_connections,
    specialist_definitions,
)
from tests.task_work_support import set_agent_ref
from tests.change_approval_support import change_agent, run_apply_change, use_signed_contexts
from tests.test_access_request_tools import BINARY, OWNER, TEAM, decide, make_agent, owner_service
from tests.test_access_request_tools import storage as access_storage
from tests.tool_result_support import tool_json

storage = access_storage  # the shared fixture: a manager database and verify keys


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


@pytest.fixture
def lead(tmp_path: Path):
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="capability-tools-test", instance_root=str(root), team_id="team_caps", timezone="UTC"
    )
    config_path = config.save(root / "instance_config.yaml")
    return change_agent(config, config_path, authorization_enabled=False)


def call(agent: Any, tool: Any, **inputs: Any) -> dict[str, Any]:
    """Call the tool function as the agent, the way the runtime does (async ones run to completion)."""

    def run() -> Any:
        set_agent_ref(agent)
        result = tool._tool_func(**inputs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    return tool_json(contextvars.copy_context().run(run))


def test_a_specialist_is_staged_seen_and_staged_for_removal_through_one_tool(lead) -> None:
    plan = call(
        lead, specialist_definitions, action="plan", agent_type="ReviewSynthAgent", display_name="Review Synthesizer",
        role="Synthesize bounded review findings.", description="Used by the capability tool tests.",
        prompt="Review the supplied evidence and return a concise synthesis.", default_tools=["fn:publishing"],
        reason="Create a reviewed synthesis specialist.",
    )
    assert plan.get("change_id"), plan

    applied = run_apply_change(lead, plan["change_id"], confirmed=True)
    status = call(lead, specialist_definitions, action="status", agent_type="ReviewSynthAgent")
    removal = call(lead, specialist_definitions, action="plan_removal", change_id=plan["change_id"], reason="Not needed.")

    assert applied["status"] == "applied", applied
    assert status["status"] == "ready", status
    assert removal.get("change_id") and removal["change_id"] != plan["change_id"], removal


def test_a_procedure_proposal_is_inert_and_listed(lead) -> None:
    proposed = call(
        lead, installed_permissions, action="propose", handler_id="example.reviewed_echo", display_name="Reviewed echo",
        purpose="Echo a reviewed value.", justification="The person asked for an echo.",
    )
    listed = call(lead, installed_permissions, action="list")

    assert "error" not in proposed, proposed
    assert listed["installed_permissions"] == []
    assert [item["requested_handler_id"] for item in listed["capability_proposals"]] == ["example.reviewed_echo"]


@pytest.mark.parametrize(
    ("tool", "inputs", "message"),
    [
        (mcp_connections, {"action": "propose", "connection_name": "tickets"}, "mcp_connections(action='propose') needs endpoint."),
        (mcp_connections, {"action": "list", "endpoint": "https://x.test/mcp"}, "mcp_connections(action='list') does not take endpoint."),
        (hosted_mcp_servers, {"action": "start"}, "hosted_mcp_servers(action='start') needs hosted_server_id."),
        (hosted_mcp_servers, {"action": "plan_install", "hosted_server_id": "h1"}, "hosted_mcp_servers(action='plan_install') needs reason."),
        (hosted_mcp_servers, {"action": "publish"}, "hosted_mcp_servers has no action 'publish'"),
        (installed_permissions, {"action": "propose", "handler_id": "x"}, "installed_permissions(action='propose') needs display_name, justification, purpose."),
        (specialist_definitions, {"action": "plan_removal", "change_id": "c1"}, "specialist_definitions(action='plan_removal') needs reason."),
        (specialist_definitions, {"action": "status", "prompt": "x"}, "specialist_definitions(action='status') does not take prompt."),
    ],
)
def test_each_tool_says_which_inputs_an_action_takes_and_does_nothing_else(lead, tool, inputs, message) -> None:
    if tool is mcp_connections:
        inputs = {**inputs, "tool_context": None}

    result = call(lead, tool, **inputs)

    assert result["error_code"] == "invalid_input" and result["error"].startswith(message), result
    assert call(lead, installed_permissions, action="list")["capability_proposals"] == []


def test_a_false_flag_counts_as_left_out(lead) -> None:
    # Models often send confirmed=false or visible=false with every call.
    listed = call(lead, mcp_connections, action="list", confirmed=False, tool_context=None)
    status = call(lead, specialist_definitions, action="status", agent_type="Nobody", visible=False)

    assert listed == {"connections": []}
    assert status.get("error_code") != "invalid_input", status


@pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_access_is_decided_per_action_so_a_block_stops_exactly_one(storage, tmp_path) -> None:
    from access_control.requests import AccessRequests
    from access_catalog.catalog import load_catalog

    lead_agent = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    worker = make_agent(storage, tmp_path / "worker", agent_type="WebResearchAgent")
    service = owner_service(storage)
    catalog = load_catalog(None)
    try:
        for function in ("mcp_connections", "hosted_mcp_servers", "installed_permissions", "specialist_definitions"):
            for action in catalog.functions[function].values:
                decision = decide(lead_agent, function, {"action": action})
                assert decision.allowed, (function, action, decision.reason)
                assert decision.operation == f"{function}.{action}"
        assert not decide(worker, "hosted_mcp_servers", {"action": "list"}).allowed

        AccessRequests(service=service, inbox=None, lead_agent_type="TeamArchitect").block(
            OWNER,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:capabilities#hosted_mcp_servers.start"),
            reason="No starting hosted servers this week",
        )

        assert not decide(lead_agent, "hosted_mcp_servers", {"action": "start"}).allowed
        assert decide(lead_agent, "hosted_mcp_servers", {"action": "stop"}).allowed
        assert decide(lead_agent, "mcp_connections", {"action": "list"}).allowed
    finally:
        service.cedar.close()
        access_runtime._runtime_access.cache_clear()
