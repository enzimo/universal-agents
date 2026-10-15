"""Tool setup pages on access workflow steps (real Cedar, SQLite and TaskManager).

Covers the owner's decision of 2026-09-28: one setup piece needs no workflow,
two or more get one workflow with a step per piece and a final work step
(``custom_agents.tool_setup_runs``). Trusted code links each setup page a
``set_up_tools`` run opens to the one step that expects it, also
when the page comes from another task with the same signed task context; a
page no single step expects is kept as "not on a step" and logged. Each result
marks the step: done, still waiting after an expired page, failed after the
third, blocked by the person when they close the page, and done when the
person finishes the setup on their own with ``/link``. A step cannot be
reported complete while a page it expects is unfinished, and the workflow page
shows the setup lines in plain words.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from assistant_gateway.access_workflows_page import GatewayAccessWorkflowPage
from assistant_gateway.application.access_workflows import WorkflowPageRefused
from custom_agents import access_runtime
from custom_agents.access_workflow_setup_hooks import link_setup_request, record_setup_result
from access_catalog.catalog import CatalogSource
from custom_agents.tool_setup_runs import (
    WORK_STEP_ID,
    ToolSetupRunV1,
    needs_workflow,
    plan_run,
    workflow_proposal,
)
from custom_agents.access_workflows import workflow_service_for
from custom_messages.contracts.access_workflow_v2 import WorkflowEventV2, WorkflowPhase, WorkflowStepState
from custom_messages.contracts.credentials_v1 import TOOL_SETUP_SOURCE_PERSON, ToolSetupRequestV1, ToolSetupResultV1
from custom_tools.providers import access_workflows as tools
from tests.access_workflow_support import (
    OWNER,
    TEAM,
    WORKER_TYPE,
    make_agent,
    needs_cedar,
    run_tool,
    signed_context,
    use_signed_contexts,
)

pytestmark = needs_cedar
GITHUB = "cli:github"
GOOGLE = "cli:google"


@pytest.fixture(autouse=True)
def _signed(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def _sign_in_step(tool_id: str, name: str) -> dict[str, Any]:
    return {
        "step": "sign_in",
        "who": "person",
        "fix_with": "setup_page",
        "kind": "sign_in",
        "owner": "person",
        "say": f"To check your {name}, I need you to sign in once.",
    }


def _report(tool_id: str, *steps: dict[str, Any]) -> dict[str, Any]:
    return {"tool_id": tool_id, "ready": not steps, "next_steps": list(steps)}


ACME_REPORTS = [_report(GITHUB, _sign_in_step(GITHUB, "GitHub")), _report(GOOGLE, _sign_in_step(GOOGLE, "Gmail"))]
CATALOG = CatalogSource(None).current()
NAMES = {GITHUB: "GitHub", GOOGLE: "Google"}


def _run(reports: list[dict[str, Any]]) -> ToolSetupRunV1:
    plan = plan_run(reports, CATALOG, is_admin=True)
    return ToolSetupRunV1(
        run_id="tool_setup_run_test", team_id=TEAM, person_id=OWNER.person_id, task_id="task-lead",
        agent_id="agent-TeamArchitect", agent_type="TeamArchitect",
        tool_ids=tuple(report["tool_id"] for report in reports), pieces=tuple(plan.pieces),
    )


# -- the rule: one piece, no workflow; two or more, one workflow ----------------------


def test_two_missing_sign_ins_get_one_workflow_with_a_work_step() -> None:
    run = _run(ACME_REPORTS)

    assert needs_workflow(run.pieces)
    proposal, run = workflow_proposal(run, NAMES, title="Check", goal="Find Acme", target_agent_type=WORKER_TYPE)
    steps = {step["step_id"]: step for step in proposal["steps"]}
    assert list(steps) == ["sign_in_github", "sign_in_google", WORK_STEP_ID]
    assert steps["sign_in_github"]["tool_setup"] == [{"tool_id": GITHUB, "kind": "sign_in", "owner": "person"}]
    assert steps["sign_in_github"]["kind"] == "account" and steps["sign_in_github"]["title"] == "Sign in to GitHub"
    assert steps[WORK_STEP_ID]["depends_on"] == ["sign_in_github", "sign_in_google"]
    assert [piece.workflow_step_id for piece in run.pieces] == ["sign_in_github", "sign_in_google"]


def test_one_piece_needs_no_workflow_and_alternatives_do_not_count() -> None:
    link_step = {"step": "account_link", "who": "person", "fix_with": "setup_page", "kind": "account_link",
                 "owner": "person"}
    team_link = {"step": "team_account_link", "who": "administrator", "alternative_to": "account_link",
                 "fix_with": "setup_page", "kind": "account_link", "owner": "team"}

    run = _run([_report("cli:huggingface", link_step, team_link)])

    assert [piece.kind for piece in run.pieces] == ["account_link"]
    assert not needs_workflow(run.pieces)


def test_access_and_a_sign_in_after_the_team_app_are_pieces_in_order() -> None:
    app = {"step": "team_sign_in_app", "who": "administrator", "fix_with": "setup_page",
           "kind": "team_sign_in_app", "owner": "team"}
    access = {"step": "team_lead_access", "who": "team_lead", "fix_with": "request_access"}

    run = _run([_report(GOOGLE, access, app, _sign_in_step(GOOGLE, "Gmail"))])
    proposal, _ = workflow_proposal(run, NAMES, title="Check", goal="Read mail", target_agent_type=WORKER_TYPE)

    steps = {step["step_id"]: step for step in proposal["steps"]}
    assert steps["sign_in_google"]["depends_on"] == ["team_sign_in_app_google"]
    assert steps["team_sign_in_app_google"]["tool_setup"][0]["owner"] == "team"
    assert steps["access_google"]["tool_access"] == [{"tool_id": GOOGLE, "level": "read"}]
    assert steps[WORK_STEP_ID]["depends_on"] == ["access_google", "team_sign_in_app_google", "sign_in_google"]


# -- linking and results ------------------------------------------------------------


def _publish(agent: Any, reports: list[dict[str, Any]] = ACME_REPORTS) -> str:
    proposal, _run_with_steps = workflow_proposal(
        _run(reports), NAMES, title="Check GitHub and Gmail for Acme",
        goal="Find anything from Acme in the person's GitHub and Gmail", target_agent_type=WORKER_TYPE,
    )
    result = run_tool(agent, tools.submit_access_workflow, proposal, "acme-check")
    assert "workflow_id" in result, result
    return result["workflow_id"]


def _request(tool_id: str, *, task_id: str = "task-lead", kind: str = "sign_in") -> ToolSetupRequestV1:
    return ToolSetupRequestV1(
        parent_task_id=task_id, tool_id=tool_id, kind=kind, owner="person", reason="To check it, sign in once."
    )


def _result(request: ToolSetupRequestV1, status: str, **extra: Any) -> ToolSetupResultV1:
    return ToolSetupResultV1(
        setup_request_id=request.setup_request_id,
        status=status,
        parent_task_id=request.parent_task_id,
        tool_id=request.tool_id,
        kind=request.kind,
        owner=request.owner,
        **extra,
    )


def _ask(agent: Any, tool_id: str, **kwargs: Any) -> ToolSetupRequestV1:
    """TeamLead's outbox emitted a setup request: link it (as the outbox does)."""

    request = _request(tool_id, task_id=agent._active_execution_task_id, **kwargs)
    link_setup_request(agent, request, agent.task_manager.get_task_info(request.parent_task_id).meta)
    return request


def _answer(agent: Any, request: ToolSetupRequestV1, status: str, **extra: Any) -> None:
    """The gateway's result arrived: record it (as the result handler does)."""

    meta = agent.task_manager.get_task_info(request.parent_task_id).meta
    record_setup_result(agent, _result(request, status, **extra), meta)


def _step(agent: Any, workflow_id: str, step_id: str):
    return workflow_service_for(agent).repository.get(workflow_id).step(step_id)


def test_each_page_shows_on_its_step_and_completing_both_frees_the_work(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)

    github = _ask(lead, GITHUB)
    google = _ask(lead, GOOGLE)
    step = _step(lead, workflow_id, "sign_in_github")
    assert step.state is WorkflowStepState.WAITING
    assert [link.setup_request_id for link in step.setups] == [github.setup_request_id]
    assert _step(lead, workflow_id, "sign_in_google").setups[0].setup_request_id == google.setup_request_id

    _answer(lead, github, "completed", account_label="exampleuser")
    step = _step(lead, workflow_id, "sign_in_github")
    assert step.state is WorkflowStepState.COMPLETE
    assert step.summary == "GitHub sign-in: Signed in as exampleuser."
    assert _step(lead, workflow_id, WORK_STEP_ID).state is WorkflowStepState.PENDING

    _answer(lead, google, "completed", account_label="me@acme.example")
    record = workflow_service_for(lead).repository.get(workflow_id)
    assert record.step("sign_in_google").state is WorkflowStepState.COMPLETE
    assert not record.incomplete_dependencies(record.step(WORK_STEP_ID)), "the work step can start"
    codes = [event.code for event in workflow_service_for(lead).repository.events(workflow_id, offset=0, limit=50)]
    assert codes.count("setup_waiting") == 2 and codes.count("setup_completed") == 2


def test_an_expired_page_waits_again_and_the_third_fails_the_step(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)

    for attempt in (1, 2, 3):
        request = _ask(lead, GITHUB)
        assert _step(lead, workflow_id, "sign_in_github").setups[-1].attempt == attempt
        _answer(lead, request, "expired", attempt=attempt)
        step = _step(lead, workflow_id, "sign_in_github")
        if attempt < 3:
            assert step.state is WorkflowStepState.WAITING
            assert step.summary == "GitHub sign-in: The page expired."
    assert step.state is WorkflowStepState.FAILED
    assert step.summary == "GitHub sign-in: The page expired; no new page will be offered."
    assert "/link cli:github" in step.recovery


def test_closing_the_page_blocks_the_step_like_a_denial(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)
    request = _ask(lead, GOOGLE)

    _answer(lead, request, "declined")

    step = _step(lead, workflow_id, "sign_in_google")
    assert step.state is WorkflowStepState.FAILED and step.blocked_by_person
    evidence = {item["step_id"]: item for item in workflow_service_for(lead).evidence(
        workflow_service_for(lead).repository.get(workflow_id)
    )}
    assert evidence["sign_in_google"]["reason_code"] == "setup_declined"
    # TeamLead cannot reopen it.
    result = run_tool(
        lead, tools.update_access_workflow, workflow_id,
        {"step_id": "sign_in_google", "state": "running", "summary": "Trying again"},
    )
    assert "closed a setup page" in result["reason"], result


def test_the_persons_own_link_marks_the_step(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)
    request = _ask(lead, GITHUB)

    # The person ran /link cli:github in chat; the gateway answers the waiting page's id.
    _answer(lead, request, "completed", account_label="exampleuser", source=TOOL_SETUP_SOURCE_PERSON, attempt=0)

    step = _step(lead, workflow_id, "sign_in_github")
    assert step.state is WorkflowStepState.COMPLETE and step.setups[0].status == "completed"
    events = workflow_service_for(lead).repository.events(workflow_id, offset=0, limit=50)
    assert any("on their own" in event.message for event in events)


def test_a_page_no_step_expects_is_not_on_a_step_and_logged(tmp_path: Path, caplog) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)

    with caplog.at_level(logging.WARNING, logger="custom_agents.access_workflow_setups"):
        request = _ask(lead, "cli:huggingface", kind="account_link")
    _answer(lead, request, "completed")

    record = workflow_service_for(lead).repository.get(workflow_id)
    assert [link.setup_request_id for link in record.unassigned_setups] == [request.setup_request_id]
    assert record.unassigned_setups[0].status == "completed"
    assert all(not step.setups for step in record.steps if step.tool_setup[:1] and step.tool_setup[0].tool_id == GITHUB)
    assert "event=workflow_setup_unassigned" in caplog.text


def test_a_page_from_another_task_of_the_same_request_is_linked(tmp_path: Path) -> None:
    signed = signed_context()
    lead = make_agent(tmp_path, signed=signed)
    workflow_id = _publish(lead)
    # A worker reported the missing sign-in; TeamLead asks for it in another task
    # that carries the same signed task context.
    escalation = make_agent(tmp_path, task_id="task-escalation", signed=signed)
    request = _ask(escalation, GITHUB)
    # Another person's task never reaches this workflow.
    other = make_agent(tmp_path, task_id="task-other", signed=signed_context(OWNER.__class__("user-2", TEAM, ("member",))))
    _ask(other, GITHUB)

    step = _step(lead, workflow_id, "sign_in_github")
    assert [link.setup_request_id for link in step.setups] == [request.setup_request_id]
    assert step.setups[0].from_task_id == "task-escalation"
    _answer(escalation, request, "completed", account_label="exampleuser")
    assert _step(lead, workflow_id, "sign_in_github").state is WorkflowStepState.COMPLETE


def test_a_step_cannot_be_reported_complete_before_its_page_is_done(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)
    _ask(lead, GITHUB)

    result = run_tool(
        lead, tools.update_access_workflow, workflow_id,
        {"step_id": "sign_in_github", "state": "complete", "summary": "Done"},
    )

    assert "setup page this step expects is not finished" in result["reason"], result
    assert workflow_service_for(lead).repository.get(workflow_id).phase is not WorkflowPhase.COMPLETE


def test_publishing_checks_the_setup_fits_the_tool(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    steps, _run_with_steps = workflow_proposal(_run(ACME_REPORTS), NAMES, title="x", goal="x", target_agent_type=WORKER_TYPE)
    steps = steps["steps"]
    steps[0]["tool_setup"] = [{"tool_id": "cli:huggingface", "kind": "sign_in"}]
    plan = {"title": "Check", "goal": "Check things", "target_agent_type": WORKER_TYPE, "steps": steps}

    result = run_tool(lead, tools.submit_access_workflow, plan, "bad-plan")

    assert "cli:huggingface has no personal sign-in" in result["reason"], result


def test_the_workflow_page_shows_setup_lines_in_plain_words(tmp_path: Path) -> None:
    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)
    github = _ask(lead, GITHUB)
    _ask(lead, GOOGLE)
    stray = _ask(lead, "cli:huggingface", kind="account_link")
    _answer(lead, github, "completed", account_label="exampleuser")
    page = GatewayAccessWorkflowPage(
        service=workflow_service_for(lead),
        access_inbox=SimpleNamespace(review_url=lambda request_id: f"/inbox/{request_id}"),
        team_id=TEAM,
    )

    payload = page._read(workflow_id, OWNER.person_id, 0)

    steps = {step["step_id"]: step for step in payload["workflow"]["steps"]}
    [line] = steps["sign_in_github"]["setups"]
    assert (line["title"], line["status_text"]) == ("GitHub sign-in", "Signed in as exampleuser")
    assert steps["sign_in_google"]["review_status"].startswith("Waiting for the person: ")
    assert steps["sign_in_google"]["explanation"] == "Waiting for the person to finish the setup page in their chat."
    assert [item["setup_request_id"] for item in payload["setups_not_on_a_step"]] == [stray.setup_request_id]


@pytest.mark.asyncio
async def test_the_person_marks_a_step_done_on_the_workflow_page(tmp_path: Path) -> None:
    """Only a waiting step done outside Universal Agents, and only the workflow's own person."""

    lead = make_agent(tmp_path)
    workflow_id = _publish(lead)
    github = _ask(lead, GITHUB)
    marked: list[tuple[str, str]] = []

    async def external_step_done(setup_request_id: str, *, person_id: str) -> bool:
        marked.append((setup_request_id, person_id))
        return True

    inbox = SimpleNamespace(review_url=lambda request_id: "", tool_setups=SimpleNamespace(external_step_done=external_step_done))
    page = GatewayAccessWorkflowPage(service=workflow_service_for(lead), access_inbox=inbox, team_id=TEAM)
    service = workflow_service_for(lead)
    record = service.repository.get(workflow_id)
    step = record.step("sign_in_github")
    external = step.setups[0].model_copy(update={"kind": "external_step", "step_id": "create_token"})
    service._save(
        record,
        steps=tuple(item.model_copy(update={"setups": (external,)}) if item.step_id == step.step_id else item for item in record.steps),
        event=WorkflowEventV2(workflow_id=workflow_id, kind="progress", code="test", message="An external step."),
    )

    with pytest.raises(WorkflowPageRefused):
        await page.mark_setup_done(workflow_id, owner_id=OWNER.person_id, setup_request_id="tool_setup_req_unknown")
    with pytest.raises(PermissionError):
        await page.mark_setup_done(workflow_id, owner_id="user-2", setup_request_id=github.setup_request_id)
    result = await page.mark_setup_done(workflow_id, owner_id=OWNER.person_id, setup_request_id=github.setup_request_id)

    assert result == {"marked_done": github.setup_request_id}
    assert marked == [(github.setup_request_id, OWNER.person_id)]
