"""Access workflows as a view over access requests (real Cedar, SQLite and TaskManager).

Covers: publishing checks each step's change kinds and tools against the
catalog; a request is linked to the one running step that expects it, by
change kind or by tool and level; a delegated worker's request is shown on
its parent's step only through the shared signed task context; a request no
single step expects is kept as "not on a step"; a denied request closes its
step and a new request for it is refused; progress rules; persistence
(idempotency, versions, owner isolation); the configuration move; and which
tools the team lead holds and sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from access_control.decisions import HandDownRefused
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from assistant_gateway.config import INSTANCE_CONFIG_SCHEMA_VERSION, GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.access_workflows import (
    AccessWorkflowService,
    WorkflowRequester,
    workflow_service_for,
)
from custom_agents.team_config_helpers import filter_agent_tools
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessRequestStatus,
    AccessReviewChoice,
    TargetKind,
    TargetRefV1,
)
from custom_messages.contracts.access_workflow_v2 import WorkflowProgressV2, WorkflowProposalV2
from custom_tools.providers import access_workflows as tools
from storage.access_workflow_repository import AccessWorkflowRepository, WorkflowConflict
from custom_tools.provider_contract import tool_name
from tests.broker_access_support import context_signer
from tests.access_workflow_support import (
    OWNER,
    TEAM,
    WORKER_TYPE,
    StagedChanges,
    make_agent,
    needs_cedar,
    proposal,
    run_tool,
    signed_context,
    storage_settings,
    use_signed_contexts,
)

pytestmark = needs_cedar
SCHEDULES = "fn:schedules"


@pytest.fixture(autouse=True)
def _signed(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


@pytest.fixture
def lead(tmp_path: Path):
    return make_agent(tmp_path)


def publish(agent, plan=None, key="weekly-report") -> dict:
    result = run_tool(agent, tools.submit_access_workflow, plan or proposal(), key)
    assert "workflow_id" in result, result
    return result


def progress(agent, workflow_id: str, step_id: str, state: str, summary: str = "Working on it", **extra) -> dict:
    return run_tool(
        agent, tools.update_access_workflow, workflow_id, {"step_id": step_id, "state": state, "summary": summary, **extra}
    )


def workflow(agent, workflow_id: str):
    return workflow_service_for(agent).repository.get(workflow_id)


def ask_change(agent, changes: StagedChanges, name: str):
    """The agent asks to apply one staged change, as apply_change does (watched)."""

    requests = access_runtime.runtime_requests(agent)
    person, task_id = access_runtime.task_person(agent, access_runtime.runtime_access(agent))
    return requests.submit_change(
        requester=access_runtime.agent_principal(agent),
        person=person,
        proposal=changes.describe(agent, changes.stage(name)),
        reason="Needed for the plan",
        task_id=task_id,
    ).request


def ask_tool(agent, tool_id: str, level: AccessLevel):
    requests = access_runtime.runtime_requests(agent)
    person, task_id = access_runtime.task_person(agent, access_runtime.runtime_access(agent))
    return requests.submit(
        requester=access_runtime.agent_principal(agent),
        person=person,
        target=TargetRefV1(kind=TargetKind.TOOL, id=tool_id),
        level=level,
        reason="The weekly report runs on a schedule",
        task_id=task_id,
    ).request


def people_side(agent) -> AccessRequests:
    """Where people decide (the gateway): the same store, no workflow watcher."""

    service = access_runtime.runtime_access(agent).service
    return AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type="TeamArchitect")


SPECIALISTS = StagedChanges("agent_definition", "specialist_test_")
RESTARTS = StagedChanges("runtime_restart", "runtime_restart_test_")


# -- publishing ------------------------------------------------------------------


def test_publishing_checks_change_kinds_and_tools_against_the_catalog(lead) -> None:
    unknown_kind = proposal()
    unknown_kind["steps"][1]["change_kinds"] = ["tool.attach"]
    refused = run_tool(lead, tools.submit_access_workflow, unknown_kind, "k1")
    assert refused["status"] == "refused" and "not a kind of change in the tool catalog" in refused["reason"]

    unknown_tool = proposal()
    unknown_tool["steps"][3]["tool_access"] = [{"tool_id": "mcp:crawl4ai", "level": "write"}]
    refused = run_tool(lead, tools.submit_access_workflow, unknown_tool, "k2")
    assert "mcp:crawl4ai is not in the tool catalog" in refused["reason"]

    # A plan that installs an MCP server (an access proposal) may name its tool before it exists.
    installs = proposal()
    installs["steps"][1]["change_kinds"] = ["access_proposal"]
    installs["steps"][3]["tool_access"] = [{"tool_id": "mcp:crawl4ai", "level": "write"}]
    assert "workflow_id" in run_tool(lead, tools.submit_access_workflow, installs, "k3")


EXAMPLES = json.loads((Path(__file__).resolve().parents[1] / "data/examples/access_workflows.json").read_text())


@pytest.mark.parametrize("example", EXAMPLES, ids=[item["title"] for item in EXAMPLES])
def test_the_documented_examples_are_valid_plans_that_end_with_a_check(example, tmp_path) -> None:
    agent = make_agent(tmp_path)
    flow = publish(agent, example, key=example["title"])
    record = workflow(agent, flow["workflow_id"])
    assert record.steps[-1].step_id == "verify", "setup alone does not finish a goal"
    early = progress(agent, flow["workflow_id"], "verify", "running")
    assert early["status"] == "refused"
    for step in record.steps:
        done = progress(agent, flow["workflow_id"], step.step_id, "complete", "Checked result for the example")
    assert done["phase"] == "complete"


def test_account_steps_need_no_approval_and_graphs_must_be_valid() -> None:
    plan = proposal()
    plan["steps"][0]["tool_access"] = [{"tool_id": SCHEDULES, "level": "read"}]
    with pytest.raises(ValueError, match="needs no approval"):
        WorkflowProposalV2.model_validate(plan)
    cycle = proposal()
    cycle["steps"][0]["depends_on"] = ["verify"]
    with pytest.raises(ValueError, match="cycle"):
        WorkflowProposalV2.model_validate(cycle)
    missing = proposal()
    missing["steps"][1]["depends_on"] = ["nowhere"]
    with pytest.raises(ValueError, match="Unknown workflow prerequisite"):
        WorkflowProposalV2.model_validate(missing)


def test_a_workflow_needs_a_person_and_is_published_once_per_task(lead, tmp_path) -> None:
    first = publish(lead)
    again = publish(lead)
    assert again["workflow_id"] == first["workflow_id"], "the same key returns the same plan"
    other = run_tool(lead, tools.submit_access_workflow, proposal(title="Another plan"), "other-key")
    assert other["status"] == "refused" and "existing workflow" in other["reason"]

    system = make_agent(tmp_path, task_id="task-system", signed=context_signer().issue(team_id=TEAM, person_id=None))
    refused = run_tool(system, tools.submit_access_workflow, proposal(), "k")
    assert refused["status"] == "refused" and "works for nobody" in refused["reason"]


def test_the_configured_step_limit_applies(tmp_path: Path) -> None:
    config_path = tmp_path / "instance_config.yaml"
    config_path.write_text(
        f"schema_version: {INSTANCE_CONFIG_SCHEMA_VERSION}\ninstance_name: test\ninstance_root: {tmp_path}\n"
        "access_workflows:\n  max_steps: 3\n", encoding="utf-8"
    )
    agent = make_agent(tmp_path)
    agent._access_workflow_limits_loaded = False
    agent.instance_config_path = str(config_path)
    refused = run_tool(agent, tools.submit_access_workflow, proposal(), "k")
    assert refused["reason"] == "A workflow may have at most 3 steps."


def test_the_old_human_interaction_keys_are_refused_with_their_new_names(tmp_path: Path) -> None:
    # The whole human_interactions block is refused (4d-6); the message still
    # names where the two workflow limits went.
    config_path = tmp_path / "instance_config.yaml"
    config_path.write_text(
        f"schema_version: {INSTANCE_CONFIG_SCHEMA_VERSION}\ninstance_name: test\ninstance_root: {tmp_path}\n"
        "human_interactions:\n  workflow_diagnostic_event_limit: 50\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="access_workflows.diagnostic_event_limit"):
        GatewayInstanceConfig.load(str(config_path))


# -- linking requests to steps ---------------------------------------------------


def test_a_change_request_is_linked_to_the_running_step_expecting_its_kind(lead) -> None:
    flow = publish(lead)
    progress(lead, flow["workflow_id"], "account", "complete", "The owner linked the account")
    progress(lead, flow["workflow_id"], "specialist", "running")

    request = ask_change(lead, SPECIALISTS, "crawler")

    record = workflow(lead, flow["workflow_id"])
    assert record.step("specialist").request_ids == (request.request_id,)
    assert record.step("specialist").state.value == "waiting" and record.phase.value == "waiting"
    assert record.step("restart").request_ids == ()
    view = run_tool(lead, tools.get_access_workflow, flow["workflow_id"])
    [attach] = [step for step in view["steps_now"] if step["step_id"] == "specialist"]
    assert attach["requests"][0]["outcome"] == "pending" and attach["requests"][0]["kind"] == "change"
    assert attach["status"] == "Waiting for 1 decision(s); 0 of 1 approved"


def test_a_workers_request_is_linked_to_its_parents_step_by_tool_and_level(lead, tmp_path) -> None:
    flow = publish(lead)
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "research", "running")
    # The delegated task carries the lead task's signed context unchanged.
    worker_task = lead.task_manager.get_task_info("task-lead").meta.gateway_context.task_context
    worker = make_agent(tmp_path, agent_type=WORKER_TYPE, lead=False, task_id="task-worker", signed=worker_task)

    covered = ask_tool(worker, SCHEDULES, AccessLevel.READ)  # write expected; read is covered
    too_much = ask_tool(worker, SCHEDULES, AccessLevel.FULL)

    record = workflow(lead, flow["workflow_id"])
    assert record.step("research").request_ids == (covered.request_id,)
    assert record.unassigned_request_ids == (too_much.request_id,), "full is more than the step expects"
    view = run_tool(lead, tools.get_access_workflow, flow["workflow_id"])
    [research] = [step for step in view["steps_now"] if step["step_id"] == "research"]
    assert research["requests"][0]["from_worker"] is True
    assert research["requests"][0]["agent_type"] == WORKER_TYPE
    assert view["requests_not_on_a_step"][0]["request_id"] == too_much.request_id


def test_a_request_from_another_signed_context_is_never_shown_on_the_plan(lead, tmp_path) -> None:
    flow = publish(lead)
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "research", "running")
    # Same person, same worker type, but another request of theirs (another context).
    stranger = make_agent(tmp_path, agent_type=WORKER_TYPE, lead=False, task_id="task-other", signed=signed_context())

    request = ask_tool(stranger, SCHEDULES, AccessLevel.READ)

    record = workflow(lead, flow["workflow_id"])
    assert request.request_id not in record.linked_request_ids()
    assert request.request_id not in record.unassigned_request_ids


def test_requests_no_single_open_step_expects_are_kept_as_not_on_a_step(lead) -> None:
    flow = publish(lead)
    # Nothing is running yet.
    early = ask_change(lead, RESTARTS, "early")
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "restart", "running")
    on_step = ask_change(lead, RESTARTS, "later")

    record = workflow(lead, flow["workflow_id"])
    assert record.unassigned_request_ids == (early.request_id,)
    assert record.step("restart").request_ids == (on_step.request_id,)
    codes = [event.code for event in workflow_service_for(lead).repository.events(flow["workflow_id"], limit=50)]
    assert "request_not_on_step" in codes and "request_waiting" in codes


def test_a_denied_request_closes_its_step_and_a_new_request_for_it_is_refused(lead) -> None:
    flow = publish(lead)
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "specialist", "running")
    request = ask_change(lead, SPECIALISTS, "first")
    # The person denies it in the Inbox; the workflow learns it from the access store.
    people_side(lead).decide(request.request_id, OWNER, AccessReviewChoice.DENY)

    with pytest.raises(HandDownRefused, match="denied a request for the step"):
        ask_change(lead, SPECIALISTS, "second")

    record = workflow(lead, flow["workflow_id"])
    attach = record.step("specialist")
    assert attach.blocked_by_person and attach.state.value == "failed"
    assert record.phase.value == "failed"
    refused = progress(lead, flow["workflow_id"], "specialist", "running")
    assert refused["status"] == "refused" and "denied" in refused["reason"]
    stored = access_runtime.runtime_requests(lead).service.store.list_requests(TEAM)
    assert [item.status for item in stored] == [AccessRequestStatus.DENIED], "the second request was never stored"


def test_a_step_is_complete_only_after_its_requests_are_approved_and_changes_applied(lead) -> None:
    flow = publish(lead)
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "specialist", "running")
    request = ask_change(lead, SPECIALISTS, "crawler")

    waiting = progress(lead, flow["workflow_id"], "specialist", "complete", "Done")
    assert "is pending" in waiting["reason"]
    people_side(lead).decide(request.request_id, OWNER, AccessReviewChoice.APPROVE)
    not_applied = progress(lead, flow["workflow_id"], "specialist", "complete", "Done")
    assert "not applied yet" in not_applied["reason"]
    store = access_runtime.runtime_requests(lead).service.store
    store.claim_change(request.request_id, at=request.created_at_utc)
    done = progress(lead, flow["workflow_id"], "specialist", "complete", "Created and checked")
    assert [step["state"] for step in done["steps"] if step["step_id"] == "specialist"] == ["complete"]


def test_progress_follows_dependencies_and_uncertain_results_are_checked_first(lead) -> None:
    flow = publish(lead)
    early = progress(lead, flow["workflow_id"], "verify", "running")
    assert "prerequisites first" in early["reason"]
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "research", "running")
    missing_recovery = progress(lead, flow["workflow_id"], "research", "outcome_unknown", "The call timed out")
    assert missing_recovery["status"] == "refused"
    progress(lead, flow["workflow_id"], "research", "outcome_unknown", "The call timed out",
             recovery="Check whether the schedule exists")
    retry = progress(lead, flow["workflow_id"], "research", "running")
    assert "Check the previous result" in retry["reason"]
    required = progress(lead, flow["workflow_id"], "restart", "omitted", "Skip")
    assert "required step cannot be left out" in required["reason"]


def test_a_step_can_be_corrected_until_a_request_is_linked(lead) -> None:
    flow = publish(lead)
    corrected = run_tool(lead, tools.revise_access_workflow_step, flow["workflow_id"], "restart",
                         ["team_skill_install"], [], "The plan installs a skill instead of restarting")
    assert corrected["access_changed"] is False
    assert workflow(lead, flow["workflow_id"]).step("restart").change_kinds == ("team_skill_install",)
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "specialist", "running")
    ask_change(lead, SPECIALISTS, "crawler")
    refused = run_tool(lead, tools.revise_access_workflow_step, flow["workflow_id"], "specialist",
                       ["runtime_restart"], [], "Wrong kind")
    assert "without linked requests" in refused["reason"]


# -- persistence and ownership -----------------------------------------------------


def test_versions_fence_concurrent_updates_and_the_definition_is_immutable(lead, tmp_path) -> None:
    flow = publish(lead)
    service = AccessWorkflowService(AccessWorkflowRepository(storage_settings(tmp_path)))
    record = service.repository.get(flow["workflow_id"])
    service.progress(record, WorkflowProgressV2(step_id="account", state="running", summary="Starting"))
    with pytest.raises(WorkflowConflict):
        service.progress(record, WorkflowProgressV2(step_id="account", state="running", summary="Stale copy"))
    current = service.repository.get(flow["workflow_id"])
    tampered = current.model_copy(update={"title": "Something else", "version": current.version + 1})
    with pytest.raises(WorkflowConflict, match="immutable"):
        service.repository.save(tampered, expected_version=current.version)


def test_only_the_owner_reads_and_only_the_publishing_task_updates(lead, tmp_path) -> None:
    flow = publish(lead)
    service = workflow_service_for(lead)
    with pytest.raises(PermissionError, match="not found"):
        service.require_owner(flow["workflow_id"], owner_id="someone-else", team_id=TEAM)
    with pytest.raises(PermissionError):
        service.require_owner(flow["workflow_id"], owner_id=OWNER.person_id, team_id="team-2")
    later = make_agent(tmp_path, task_id="task-later")
    refused = progress(later, flow["workflow_id"], "account", "running")
    assert "Only the task that published" in refused["reason"]
    readable = run_tool(later, tools.get_access_workflow, flow["workflow_id"])
    assert readable["can_update_from_current_task"] is False
    restarted = WorkflowRequester("agent-new-process", "TeamArchitect", TEAM, OWNER.person_id)
    assert service.require_agent(flow["workflow_id"], requester=restarted, task_id="task-lead").workflow_id == flow["workflow_id"]


def test_diagnostics_list_structure_without_request_reasons_or_titles(lead) -> None:
    flow = publish(lead)
    progress(lead, flow["workflow_id"], "account", "complete", "Linked")
    progress(lead, flow["workflow_id"], "specialist", "running")
    ask_change(lead, SPECIALISTS, "crawler")
    service = workflow_service_for(lead)
    report = service.diagnostics(service.repository.get(flow["workflow_id"]))
    text = json.dumps(report)
    assert report["schema_version"] == 3
    assert "Needed for the plan" not in text and "Create the crawler specialist" not in text
    [attach] = [step for step in report["steps"] if step["step_id"] == "specialist"]
    assert attach["requests"][0]["outcome"] == "pending" and "title" not in attach["requests"][0]
    assert {event["code"] for event in report["events"]} >= {"plan_submitted", "request_waiting"}


# -- tools and access --------------------------------------------------------------


def test_the_team_lead_holds_the_workflow_tools_and_a_worker_does_not(lead, tmp_path) -> None:
    assert access_runtime.decide_call(lead, "submit_access_workflow", {}).allowed
    assert access_runtime.decide_call(lead, "request_access_workflow_approvals", {}).allowed
    worker = make_agent(tmp_path, agent_type=WORKER_TYPE, lead=False, task_id="task-worker")
    assert not access_runtime.decide_call(worker, "submit_access_workflow", {}).allowed


def test_with_authorization_off_only_the_batch_tool_is_hidden() -> None:
    names = [tool_name(tool) for tool in tools.PROVIDER.tools]
    assert "plan_routine_team_delegation" not in names
    visible = {tool_name(tool) for tool in filter_agent_tools(tools.PROVIDER.tools,
                                                              authorization_enabled=False)}
    assert visible == {"submit_access_workflow", "update_access_workflow", "revise_access_workflow_step",
                       "get_access_workflow"}
    assert len(filter_agent_tools(tools.PROVIDER.tools)) == 5


def test_with_authorization_off_a_plan_is_still_published_and_followed(tmp_path) -> None:
    agent = make_agent(tmp_path, enabled=False)
    flow = publish(agent)
    assert progress(agent, flow["workflow_id"], "account", "complete", "Linked")["phase"] == "running"
    batch = run_tool(agent, tools.get_access_workflow, flow["workflow_id"])
    assert batch["steps_now"][0]["state"] == "complete"


def test_a_folder_request_is_shown_off_the_steps_without_an_error(lead) -> None:
    """Steps name tools and changes, not folders, so a folder request is expected off the steps."""

    flow = publish(lead)
    requests = access_runtime.runtime_requests(lead)
    person, task_id = access_runtime.task_person(lead, access_runtime.runtime_access(lead))
    request = requests.submit(
        requester=access_runtime.agent_principal(lead),
        person=person,
        target=TargetRefV1(kind=TargetKind.FOLDER, id="data/reports"),
        level=AccessLevel.READ,
        reason="The report data",
        task_id=task_id,
    ).request

    record = workflow(lead, flow["workflow_id"])
    assert request.request_id in record.unassigned_request_ids
    events = workflow_service_for(lead).repository.events(flow["workflow_id"], limit=50)
    [event] = [event for event in events if event.code == "folder_request_not_on_step"]
    assert event.kind == "progress"
    assert not any(event.code == "request_not_on_step" for event in events)
