"""The merged Objective tools (custom_tools.providers.objectives and objective_work): what the merge adds.

Each action runs the function it replaced, unchanged; tests/test_objective_tools.py
and tests/test_objective_work_sessions.py exercise them through the merged
tools. These check the merge itself: which inputs each action takes, that each
action passes its inputs on with the old defaults, and that access is decided
per action with real Cedar, so a block can still stop exactly one action.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_tools.providers.objective_work as work_provider
import custom_tools.providers.objectives as provider
from custom_agents import access_runtime
from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
from custom_tools.providers.objective_work import objective_work_profiles, objective_work_sessions
from custom_tools.providers.objectives import objectives
from tests.test_access_request_tools import BINARY, OWNER, TEAM, decide, make_agent, owner_service
from tests.test_access_request_tools import storage as access_storage
from tests.tool_result_support import tool_json

storage = access_storage  # the shared fixture: a manager database and verify keys


@pytest.mark.parametrize(
    ("tool", "inputs", "message"),
    [
        (
            objectives,
            {"action": "update_status"},
            "objectives(action='update_status') needs expected_version, status.",
        ),
        (
            objectives,
            {"action": "update_progress", "progress_summary": "x"},
            "objectives(action='update_progress') needs expected_version.",
        ),
        (objectives, {"action": "list", "progress_summary": "x"}, "objectives(action='list') does not take progress_summary."),
        (objective_work_sessions, {"action": "suggest"}, "objective_work_sessions(action='suggest') needs objective_id."),
        (
            objective_work_sessions,
            {"action": "enable", "objective_id": "obj_1"},
            "objective_work_sessions(action='enable') needs confirmation_summary.",
        ),
        (
            objective_work_profiles,
            {"action": "apply", "proposal_id": "p1", "confirmation_summary": "Yes"},
            "objective_work_profiles(action='apply') needs proposal_digest.",
        ),
        (objective_work_profiles, {"action": "list", "save": True}, "objective_work_profiles(action='list') does not take save."),
    ],
)
def test_each_tool_says_which_inputs_an_action_takes(tool, inputs, message) -> None:
    result = tool_json(tool(**inputs))

    assert (result["error_code"], result["error"]) == ("invalid_input", message)


def test_each_action_runs_the_function_it_replaced_with_its_old_defaults(monkeypatch) -> None:
    calls = []

    def recorder(name):
        return lambda *args, **kwargs: calls.append((name, args, kwargs)) or "{}"

    for name in ("_list_objectives", "_update_progress"):
        monkeypatch.setattr(provider, name, recorder(name))
    for name in ("_list_profiles", "_suggest_session", "_disable_sessions"):
        monkeypatch.setattr(work_provider, name, recorder(name))

    objectives(action="list")
    objectives(action="update_progress", expected_version="v1", progress_summary="Half done.", user_visible=False)
    objective_work_profiles(action="list", include_proposals=False)
    objective_work_profiles(action="list")
    objective_work_sessions(action="suggest", objective_id="obj_1")
    objective_work_sessions(action="disable", objective_id="obj_1", confirmed=True)

    assert calls == [
        ("_list_objectives", (), {"limit": 20, "statuses": "", "horizons": "", "include_closed": False}),
        (
            "_update_progress",
            ("v1",),
            {
                "objective_id": "", "task_id": "", "plan_id": "", "progress_summary": "Half done.",
                "current_focus": "", "criterion_updates_json": "", "user_visible": False,
            },
        ),
        ("_list_profiles", (), {"include_proposals": False}),
        ("_list_profiles", (), {"include_proposals": True}),
        ("_suggest_session", ("obj_1",), {"estimated_sessions": 3}),
        ("_disable_sessions", ("obj_1",), {"confirmed": True}),
    ]


@pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_access_is_decided_per_action_so_a_block_stops_exactly_one(storage, tmp_path) -> None:
    from access_catalog.catalog import load_catalog
    from access_control.requests import AccessRequests

    lead = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    worker = make_agent(storage, tmp_path / "worker", agent_type="WebResearchAgent")
    service = owner_service(storage)
    catalog = load_catalog(None)
    try:
        for function in ("objectives", "objective_work_sessions", "objective_work_profiles"):
            for action in catalog.functions[function].values:
                decision = decide(lead, function, {"action": action})
                assert decision.allowed, (function, action, decision.reason)
                assert decision.operation == f"{function}.{action}"
        assert catalog.resolve_function("objective_work_profiles", {"action": "remove"}).level.value == "full"
        assert not decide(worker, "objectives", {"action": "list"}).allowed

        AccessRequests(service=service, inbox=SimpleNamespace(), lead_agent_type="TeamArchitect").block(
            OWNER,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:objectives#objective_work_profiles.remove"),
            reason="Keep the reusable profiles this week",
        )

        assert not decide(lead, "objective_work_profiles", {"action": "remove"}).allowed
        assert decide(lead, "objective_work_profiles", {"action": "list"}).allowed
        assert decide(lead, "objectives", {"action": "update_status"}).allowed
    finally:
        service.cedar.close()
        access_runtime._runtime_access.cache_clear()
