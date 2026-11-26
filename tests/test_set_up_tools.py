"""``set_up_tools``: TeamLead sets up the tools a request needs in one call (owner's decision 2026-09-28).

Real Cedar, SQLite, TaskManager and access store; the broker's account check
and the gateway's pages are stood in for at their message boundaries
(``tests.tool_setup_run_support``). Covered:

- the GitHub example end to end with a scripted TeamLead model (a real
  Strands agent): one call, access asked for and the sign-in page opened at
  once, one workflow page, the pause, and the per-tool result after resume;
- one missing piece: no workflow, one page;
- an expired page is offered again automatically; the third expiry stops;
- a closed page stops the run and is not asked for again;
- an administrator's step for a member: explained, nothing asked or opened;
- a step done outside Universal Agents, marked done on the workflow page;
- prerequisite tools set up first, in order;
- a web API tool proposed with setup data works with no code change;
- an agent restart during the wait resumes the task from a directive.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from strands import Agent
from strands.interrupt import InterruptException
from strands.models.model import Model as _StrandsModel

from custom_agents import access_runtime
from custom_agents.access_workflows import workflow_service_for
from custom_agents.task_human_interaction_blockers import get_human_interaction_blocker, answer_held
from custom_agents import tool_setup_run_runtime
from custom_agents.runtime_recovery import recover_runtime_after_restart
from custom_agents.task_manager import TaskManager
from custom_agents.tool_setup_runs import TOOL_SETUP_RUN_KIND, TOOL_SETUP_RUN_RESPONSE_KIND
from custom_messages.contracts.access_v2 import AccessRequestStatus, AccessReviewChoice
from custom_messages.enums import ProgressState
from access_catalog.catalog import CatalogSource
from access_catalog.instance_tools import write_instance_tool
from access_control.proposals import ProposalCompiler
from custom_messages.contracts.access_v2 import AccessProposalV1, ProposedHttpToolV1
from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1, ToolSetupSpecV1
from custom_tools.providers import access_workflows as workflow_tools
from custom_tools.providers.tool_setup import set_up_tools
from tests.task_work_support import set_agent_ref
from tests.access_workflow_support import TEAM, ToolCall, needs_cedar, run_tool, use_signed_contexts
from tests.tool_setup_run_support import (
    MEMBER,
    broker_states,
    decide,
    gateway_result,
    lead_agent,
    pause_turn,
    sent_pages,
    the_run,
)

pytestmark = needs_cedar
GITHUB = "cli:github"
SIGNED_OUT = {"sign_in": {"state": "missing"}, "team_sign_in_app": "not_needed"}


@pytest.fixture(autouse=True)
def _signed(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


def call(agent, tool_ids, tool_call: ToolCall, **kwargs) -> dict:
    return run_tool(agent, set_up_tools, tool_ids, tool_call, **kwargs)


def start(agent, tool_ids, **kwargs) -> tuple[ToolCall, InterruptException]:
    """The first call: it pauses the turn."""

    tool_call = ToolCall("tooluse-setup", "set_up_tools")
    with pytest.raises(InterruptException) as raised:
        call(agent, tool_ids, tool_call, **kwargs)
    return tool_call, raised.value


def store(agent):
    return access_runtime.runtime_access(agent).service.store


@pytest.mark.asyncio
async def test_repos_in_github_with_access_and_sign_in_missing(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})

    tool_call, raised = start(lead, [GITHUB], purpose="list the repos you can access in GitHub")

    [reason] = tool_call.reasons
    assert reason["kind"] == TOOL_SETUP_RUN_KIND
    [request] = store(lead).list_requests(TEAM)
    assert request.status is AccessRequestStatus.WAITING and request.target.id == GITHUB
    run = the_run(lead)
    assert run.workflow_id, "two pieces: one workflow page"
    record = workflow_service_for(lead).repository.get(run.workflow_id)
    assert [step.step_id for step in record.steps] == ["access_github", "sign_in_github", "do_the_work"]
    assert record.step("access_github").request_ids == (request.request_id,)

    await pause_turn(lead, raised.interrupt)
    [page] = sent_pages(lead)
    assert (page.tool_id, page.kind) == (GITHUB, "sign_in")
    assert "list the repos you can access in GitHub" in page.reason
    assert get_human_interaction_blocker(lead.task_manager, "task-lead")["kind"] == TOOL_SETUP_RUN_KIND
    assert lead.task_manager.get_task_info("task-lead").state == ProgressState.WAITING

    await decide(lead, request.request_id, AccessReviewChoice.APPROVE)
    assert not lead._pending_interrupt_responses, "the sign-in still waits"
    await gateway_result(lead, page, "completed", account_label="exampleuser")

    steering = lead._pending_interrupt_responses["task-lead"]
    [answer] = steering.interrupt_responses
    assert answer["response"]["kind"] == TOOL_SETUP_RUN_RESPONSE_KIND
    # The record stays, marked, until the paused turn really resumes: the
    # answer waits for the task's lane (test_setup_run_resume_wait).
    blocker = get_human_interaction_blocker(lead.task_manager, "task-lead")
    assert blocker["run_id"] == run.run_id and answer_held(blocker, lead.id)
    tool_call.response = answer["response"]
    result = call(lead, [GITHUB], tool_call, purpose="list the repos you can access in GitHub")
    assert result["status"] == "ready"
    [github] = result["tools"]
    assert github["ready"] is True
    assert github["what_happened"] == ["TeamLead may use GitHub.", "Signed in to GitHub as exampleuser."]
    record = workflow_service_for(lead).repository.get(run.workflow_id)
    assert record.step("access_github").state.value == "complete"
    assert record.step("sign_in_github").state.value == "complete"
    # Only the work step is TeamLead's; finishing it completes the workflow.
    assert result["workflow"]["work_step_id"] == "do_the_work"
    for state in ("running", "complete"):
        progress = {"step_id": "do_the_work", "state": state, "summary": "Listed the repositories."}
        assert "phase" in run_tool(lead, workflow_tools.update_access_workflow, run.workflow_id, progress)
    assert workflow_service_for(lead).repository.get(run.workflow_id).phase.value == "complete"


class _ScriptedTeamLead(_StrandsModel):
    """TeamLead's model, scripted: it calls set_up_tools at once, then answers from its result."""

    def __init__(self) -> None:
        self.config: dict = {}
        self.tool_results: list[dict] = []

    def update_config(self, **model_config) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        results = [block["toolResult"] for message in messages for block in message.get("content", []) if "toolResult" in block]
        yield {"messageStart": {"role": "assistant"}}
        if not results:
            call = {"tool_ids": [GITHUB], "purpose": "see which repositories you can access in GitHub"}
            yield {"contentBlockStart": {"start": {"toolUse": {"name": "set_up_tools", "toolUseId": "call-setup"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(call)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        outcome = json.loads(results[-1]["content"][0]["text"])
        self.tool_results.append(outcome)
        github = outcome["tools"][0]
        answer = f"{' '.join(github['what_happened'])} Checking your repositories now."
        yield {"contentBlockDelta": {"delta": {"text": answer}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.mark.asyncio
async def test_the_github_example_end_to_end_with_a_scripted_team_lead(tmp_path: Path, monkeypatch) -> None:
    """"What repos can you access in GitHub?" with access and the sign-in both missing."""

    lead = lead_agent(tmp_path)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    model = _ScriptedTeamLead()
    team_lead = Agent(model=model, tools=[set_up_tools], callback_handler=None)
    set_agent_ref(lead)

    paused = await asyncio.to_thread(team_lead, "What repos can you access in GitHub?")

    assert paused.stop_reason == "interrupt", "no question to the person: the call pauses on the setup"
    [interrupt] = paused.interrupts
    await pause_turn(lead, interrupt)
    [request] = store(lead).list_requests(TEAM)
    [page] = sent_pages(lead)
    await decide(lead, request.request_id, AccessReviewChoice.APPROVE)
    await gateway_result(lead, page, "completed", account_label="exampleuser")
    steering = lead._pending_interrupt_responses.pop("task-lead")
    [answer] = steering.interrupt_responses

    resumed = await asyncio.to_thread(
        team_lead, [{"interruptResponse": {"interruptId": answer["interrupt_id"], "response": answer["response"]}}]
    )

    [outcome] = model.tool_results
    assert outcome["status"] == "ready" and outcome["workflow_url"].startswith("/workflows/")
    assert str(resumed).strip() == (
        "TeamLead may use GitHub. Signed in to GitHub as exampleuser. Checking your repositories now."
    )
    assert len(store(lead).list_requests(TEAM)) == 1 and len(sent_pages(lead)) == 1, "nothing was asked twice"


@pytest.mark.asyncio
async def test_work_a_worker_does_asks_once_for_access_the_team_lead_may_hand_on(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path)
    broker_states(monkeypatch, {GITHUB: {"sign_in": {"state": "signed_in", "account": "exampleuser"}}})

    tool_call, raised = start(lead, [GITHUB], agent_type="WebResearchAgent", purpose="triage the GitHub issues")

    # Workers keep no access: one request, for TeamLead, which it may hand on to the worker's tasks.
    run = the_run(lead)
    assert [piece.kind for piece in run.pieces] == ["access"] and run.workflow_id is None
    [request] = store(lead).list_requests(TEAM)
    assert request.subject.id == f"{TEAM}/TeamArchitect" and request.hand_on is True
    await pause_turn(lead, raised.interrupt)
    await decide(lead, request.request_id, AccessReviewChoice.APPROVE)

    [grant] = [store(lead).get(grant_id) for grant_id in store(lead).get_request(request.request_id).grant_ids]
    assert grant.delegation_depth == 1, "the card's hand-on box starts ticked"
    tool_call.response = lead._pending_interrupt_responses["task-lead"].interrupt_responses[0]["response"]
    result = call(lead, [GITHUB], tool_call, agent_type="WebResearchAgent", purpose="triage the GitHub issues")
    [github] = result["tools"]
    assert github["what_happened"][0] == "TeamLead may use GitHub and hand it on to WebResearchAgent's tasks."


def test_one_missing_sign_in_needs_no_workflow(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})

    start(lead, [GITHUB], purpose="check your GitHub")

    run = the_run(lead)
    assert run.workflow_id is None
    assert [(piece.kind, piece.status) for piece in run.pieces] == [("sign_in", "pending")]
    assert all(piece.kind != "access" for piece in run.pieces), "authorization is off: nothing to ask for"


def test_a_ready_tool_is_used_at_once(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: {"sign_in": {"state": "signed_in", "account": "exampleuser"}}})

    result = call(lead, [GITHUB], ToolCall("tooluse-ready", "set_up_tools"))

    assert result["status"] == "ready" and result["tools"][0]["account"] == "exampleuser"
    assert "do the work now" in result["next_step"]


@pytest.mark.asyncio
async def test_expired_pages_are_offered_again_and_the_third_expiry_stops(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    tool_call, raised = start(lead, [GITHUB])
    await pause_turn(lead, raised.interrupt)

    for attempt in (1, 2, 3):
        pages = sent_pages(lead)
        assert len(pages) == attempt, "trusted code offers a fresh page by itself"
        await gateway_result(lead, pages[-1], "expired", attempt=attempt)

    assert len(sent_pages(lead)) == 3, "no fourth page"
    tool_call.response = lead._pending_interrupt_responses["task-lead"].interrupt_responses[0]["response"]
    result = call(lead, [GITHUB], tool_call)
    assert result["status"] == "not_ready"
    [line] = result["tools"][0]["what_happened"]
    assert "/link cli:github" in line and "Tell the person plainly" in result["next_step"]


@pytest.mark.asyncio
async def test_a_closed_page_stops_the_setup_and_is_not_asked_again(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT, "cli:google": SIGNED_OUT | {"team_sign_in_app": "configured"}})
    tool_call, raised = start(lead, [GITHUB, "cli:google"], purpose="check your GitHub and Gmail for Acme")
    await pause_turn(lead, raised.interrupt)
    github, google = sent_pages(lead)

    await gateway_result(lead, github, "declined")

    assert "task-lead" in lead._pending_interrupt_responses, "a closed page ends the wait at once"
    tool_call.response = {"kind": TOOL_SETUP_RUN_RESPONSE_KIND}
    result = call(lead, [GITHUB, "cli:google"], tool_call)
    lines = {tool["tool_id"]: tool["what_happened"] for tool in result["tools"]}
    assert "closed it without finishing; do not ask again" in lines[GITHUB][0]
    await gateway_result(lead, google, "completed", account_label="me@acme.example")
    assert len(sent_pages(lead)) == 2, "the stopped run opens nothing more"
    record = workflow_service_for(lead).repository.get(the_run(lead).workflow_id)
    assert record.step("sign_in_github").blocked_by_person


def test_an_administrators_step_is_explained_to_a_member_and_nothing_is_opened(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, person=MEMBER, enabled=False)
    broker_states(monkeypatch, {"cli:google": SIGNED_OUT | {"team_sign_in_app": "missing"}})

    result = call(lead, ["cli:google"], ToolCall("tooluse-member", "set_up_tools"))

    assert result["status"] == "needs_administrator"
    assert any("/auth google team_client" in line for line in result["say"])
    assert "end the task" in result["next_step"]
    assert lead.team_comms.send_message.await_count == 0


def _gitea(tool_id: str = "http:gitea", *, prerequisites: tuple[str, ...] = (), steps: bool = True) -> ProposedHttpToolV1:
    step = ExternalSetupStepV1(
        step_id="create_token",
        title="Create a Gitea token",
        instructions="In Gitea, open Settings, then Applications, and generate a token that can read repositories.",
        url="https://gitea.example.com/user/settings/applications",
        verify="Gitea shows the new token once; keep it for the next step.",
    )
    return ProposedHttpToolV1(
        tool_id=tool_id,
        display_name="Gitea" if tool_id == "http:gitea" else "Gitea CI",
        hosts=("gitea.example.com",) if tool_id == "http:gitea" else ("ci.example.com",),
        setup=ToolSetupSpecV1(external_steps=(step,) if steps else (), prerequisites=prerequisites),
    )


UNLINKED = {"link": {"own": False, "team": False}}


@pytest.mark.asyncio
async def test_a_proposed_web_tool_with_setup_data_works_with_no_code_change(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "instance"
    tool = _gitea()
    compiled = ProposalCompiler(catalog=CatalogSource(root).current(), team_id=TEAM, holds=lambda *_: True, account_sharing=_no_sharing_choices()).compile(
        AccessProposalV1(title="Add Gitea", reason="Read the team's Gitea repositories", tools=(tool,)),
        person=None,
        task_id=None,
    )
    step_row = next(row for row in compiled.rows if row.item_id == "tool-0-step-1")
    assert "setup_steps" in step_row.flags and "Create a Gitea token" in step_row.text
    assert "https://gitea.example.com/user/settings/applications" in step_row.text
    write_instance_tool(root, tool)  # what approving the proposal writes
    lead = lead_agent(tmp_path, enabled=False, instance_root=root)
    broker_states(monkeypatch, {"http:gitea": UNLINKED})
    tool_call, raised = start(lead, ["http:gitea"], purpose="list the Gitea repos")

    run = the_run(lead)
    assert [(piece.kind, piece.step_id) for piece in run.pieces] == [("external_step", "create_token"), ("account_link", None)]
    record = workflow_service_for(lead).repository.get(run.workflow_id)
    assert [step.step_id for step in record.steps] == ["gitea_create_token", "account_link_gitea", "do_the_work"]
    assert record.step("account_link_gitea").depends_on == ("gitea_create_token",)
    await pause_turn(lead, raised.interrupt)
    [token_step] = sent_pages(lead)
    assert (token_step.kind, token_step.step_id) == ("external_step", "create_token"), "the link waits for the token"

    await gateway_result(lead, token_step, "completed", source="person_action")  # marked done on the workflow page
    events = workflow_service_for(lead).repository.events(run.workflow_id)
    [marked] = [event for event in events if event.code == "setup_completed" and event.step_id == "gitea_create_token"]
    assert marked.message == "A setup page ended: completed (the person marked it done)."
    token, link = sent_pages(lead)
    assert link.kind == "account_link"
    await gateway_result(lead, link, "completed")
    record = workflow_service_for(lead).repository.get(run.workflow_id)
    assert record.step("gitea_create_token").setups[0].step_title == "Create a Gitea token"
    assert record.step("gitea_create_token").state.value == "complete"
    tool_call.response = {"kind": TOOL_SETUP_RUN_RESPONSE_KIND}
    result = call(lead, ["http:gitea"], tool_call)
    assert result["status"] == "ready"
    assert result["tools"][0]["what_happened"] == ["Gitea: Create a Gitea token: done.", "Link your account to Gitea: done."]


@pytest.mark.asyncio
async def test_prerequisite_tools_are_set_up_first(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "instance"
    write_instance_tool(root, _gitea(steps=False))
    write_instance_tool(root, _gitea("http:gitea-ci", prerequisites=("http:gitea",), steps=False))
    lead = lead_agent(tmp_path, enabled=False, instance_root=root)
    broker_states(monkeypatch, {"http:gitea": UNLINKED, "http:gitea-ci": UNLINKED})

    _call, raised = start(lead, ["http:gitea-ci"])

    assert the_run(lead).tool_ids == ("http:gitea", "http:gitea-ci"), "the prerequisite joins, first"
    await pause_turn(lead, raised.interrupt)
    [first] = sent_pages(lead)
    assert first.tool_id == "http:gitea"
    await gateway_result(lead, first, "completed")
    assert [page.tool_id for page in sent_pages(lead)] == ["http:gitea", "http:gitea-ci"]


@pytest.mark.asyncio
async def test_after_an_agent_restart_the_task_resumes_from_a_directive(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    _call, raised = start(lead, [GITHUB])
    await pause_turn(lead, raised.interrupt)
    [page] = sent_pages(lead)
    lead._interrupted_turn_sessions.clear()  # the paused turn is gone after a restart

    await gateway_result(lead, page, "completed", account_label="exampleuser")

    manager = lead.task_manager
    assert get_human_interaction_blocker(manager, "task-lead") is None
    assert manager.get_task_info("task-lead").state == ProgressState.ACCEPTED
    directive = manager.read_runtime_resume_directive("task-lead")
    assert "Signed in to GitHub as exampleuser" in directive and '"status": "ready"' in directive
    await asyncio.wait_for(lead._pending_work_task, 2)  # the retained drain runs the task
    lead._continue_pending_work.assert_awaited()


def _restarted(lead, tmp_path: Path) -> SimpleNamespace:
    """The same agent after a restart: its saved work in a new TaskManager, no paused turn or held answer."""

    manager = TaskManager(db_path=str(tmp_path / "manager.db"))
    manager.set_owner_identity(lead.id, lead.name, TEAM, lead.named_agent_type)
    state = {key: value for key, value in vars(lead).items() if not key.startswith("_tool_setup_run")}
    return SimpleNamespace(
        **{
            **state,
            "task_manager": manager,
            "_interrupted_turn_sessions": {},
            "_pending_interrupt_responses": {},
            "_pending_work_task": None,
            "_logger": logging.getLogger("test.set_up_tools.restart"),
            # What restart recovery reads (runtime_recovery.recover_runtime_after_restart).
            "deferred_inbox": SimpleNamespace(
                stored_team_id=AsyncMock(return_value=None),
                restore_role=AsyncMock(return_value=False),
                restore=AsyncMock(return_value=False),
            ),
            "same_role_runtime_takeover_enabled": False,
            "_agent_state_store": None,
            "_recovery_retry_deadline_seconds": lambda: 900.0,
            "_parse_iso_timestamp": lambda value: datetime.fromisoformat(value) if value else None,
        }
    )


@pytest.mark.asyncio
async def test_a_restart_while_the_answer_waits_for_its_lane_resumes_the_task_with_the_result(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The run ended with the paused turn live, the lane stayed busy, and the agent restarted.

    The answer lived only in memory. Restart recovery keeps the task waiting
    because of its record, then the setup-run recovery resumes it from a note
    with the run's result, once, without waking the drain before startup ends.
    """
    lead = lead_agent(tmp_path, enabled=False, instance_root=tmp_path)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    _call, raised = start(lead, [GITHUB])
    await pause_turn(lead, raised.interrupt)
    [page] = sent_pages(lead)
    await gateway_result(lead, page, "completed", account_label="exampleuser")
    assert answer_held(get_human_interaction_blocker(lead.task_manager, "task-lead"), lead.id)
    await asyncio.wait_for(lead._pending_work_task, 2)  # the busy lane never ran the answer

    agent = _restarted(lead, tmp_path)
    with caplog.at_level(logging.WARNING):
        await recover_runtime_after_restart(agent)
    # Review F7 (EVD-02): restart recovery itself ran without an error of its own.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    manager = agent.task_manager
    assert get_human_interaction_blocker(manager, "task-lead") is None
    assert manager.get_task_info("task-lead").state == ProgressState.ACCEPTED
    assert list(manager.task_state.task_queue) == ["task-lead"]
    note = manager.read_runtime_resume_directive("task-lead")
    assert "Signed in to GitHub as exampleuser" in note and '"status": "ready"' in note
    assert agent._pending_work_task is None, "startup wakes the drain itself once it is ready"

    # Review C03: a second pass past the pacing window looks again and resumes nothing more.
    reads = []
    original = tool_setup_run_runtime._setup_run_waits
    monkeypatch.setattr(tool_setup_run_runtime, "_setup_run_waits", lambda item: reads.append(1) or original(item))
    assert await tool_setup_run_runtime.resume_ended_setup_runs(agent, now=time.monotonic() + 3600) == 0
    assert reads == [1]
    assert manager.read_runtime_resume_directive("task-lead").count(tool_setup_run_runtime.RUN_ENDED_NOTE) == 1
    resumes = [entry for entry in manager.get_task_history_entries("task-lead", limit=None)
               if entry.summary == "Tool setup ended; the task resumes."]
    assert len(resumes) == 1 and list(manager.task_state.task_queue) == ["task-lead"]


@pytest.mark.asyncio
async def test_a_denied_access_request_stops_the_setup(tmp_path: Path, monkeypatch) -> None:
    lead = lead_agent(tmp_path)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    tool_call, raised = start(lead, [GITHUB])
    await pause_turn(lead, raised.interrupt)
    [request] = store(lead).list_requests(TEAM)

    await decide(lead, request.request_id, AccessReviewChoice.DENY)

    tool_call.response = lead._pending_interrupt_responses["task-lead"].interrupt_responses[0]["response"]
    result = call(lead, [GITHUB], tool_call)
    assert result["status"] == "not_ready"
    assert "The person denied access to GitHub; do not ask again." in result["tools"][0]["what_happened"]
    record = workflow_service_for(lead).repository.get(the_run(lead).workflow_id)
    assert record.step("access_github").blocked_by_person


def _no_sharing_choices():
    """A team that has made no sharing choices: every service has its starting sharing."""

    from access_control.account_sharing import TeamAccountSharing

    class _NoChoices:
        def account_sharing(self, team_id, subject):
            return None

        def team_account_sharing(self, team_id):
            return []

        def set_account_sharing(self, record):
            raise AssertionError("compiling a proposal records no choice")

    return TeamAccountSharing(_NoChoices(), TEAM)


@pytest.mark.asyncio
async def test_a_second_copy_of_a_page_result_is_ignored_even_after_the_task_ended(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The gateway sends a kept result again when a team lead is back, so a copy can arrive twice (C02).

    The run applies each result once, before the check for an ended task, so a
    late copy never posts a second answer, a second history entry or a second
    workflow event; a result whose task had already ended is recorded too, so
    its copy is recognised as well.
    """

    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from custom_agents import tool_setup_runtime

    lead = lead_agent(tmp_path, enabled=False)
    answers = AsyncMock()
    monkeypatch.setattr(tool_setup_runtime, "_publish_task_response", answers)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT, "cli:google": SIGNED_OUT | {"team_sign_in_app": "configured"}})
    _tool_call, raised = start(lead, [GITHUB, "cli:google"], purpose="check your GitHub and Gmail for Acme")
    await pause_turn(lead, raised.interrupt)
    github, google = sent_pages(lead)
    workflow_id = the_run(lead).workflow_id

    def workflow_events() -> int:
        return len(workflow_service_for(lead).repository.events(workflow_id, limit=500))

    def github_history() -> list[str]:
        entries = lead.task_manager.get_task_history_entries("task-lead", limit=None)
        return [entry.summary for entry in entries if entry.summary.startswith("Setup page (sign_in) for cli:github")]

    await gateway_result(lead, github, "completed", account_label="exampleuser")
    events_after_first = workflow_events()
    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
    assert workflow_events() == events_after_first
    assert github_history() == ["Setup page (sign_in) for cli:github: completed."]
    assert the_run(lead).results_applied == (github.setup_request_id,)

    # The task ends while a result is still on its way, and copies keep arriving.
    lead.task_manager.complete_task("task-lead", datetime.now(timezone.utc).isoformat())
    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
        await gateway_result(lead, google, "completed", account_label="me@acme.example")
        await gateway_result(lead, google, "completed", account_label="me@acme.example")

    assert answers.await_count == 1, "only the first copy of the result whose task had ended answers"
    assert github_history() == ["Setup page (sign_in) for cli:github: completed."]
    assert the_run(lead).results_applied == (github.setup_request_id, google.setup_request_id)
    duplicates = [record.getMessage() for record in caplog.records if "event=tool_setup_result_duplicate" in record.getMessage()]
    assert len(duplicates) == 3
    assert f"setup_request_id={google.setup_request_id} status=completed" in duplicates[-1]


@pytest.mark.asyncio
async def test_a_copy_of_an_expired_pages_result_is_recognised_after_the_page_was_offered_again(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The piece carries the new page's id; the run's applied ids still recognise the old result's copy.

    Without them, a copy arriving after the task ended matched no run and
    posted the orphaned-result answer ("... was not finished") to the person.
    """

    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from custom_agents import tool_setup_runtime

    lead = lead_agent(tmp_path, enabled=False)
    answers = AsyncMock()
    monkeypatch.setattr(tool_setup_runtime, "_publish_task_response", answers)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    _tool_call, raised = start(lead, [GITHUB])
    await pause_turn(lead, raised.interrupt)
    [first] = sent_pages(lead)

    await gateway_result(lead, first, "expired", attempt=1)
    await gateway_result(lead, first, "expired", attempt=1)
    pages = sent_pages(lead)
    assert len(pages) == 2, "one fresh page, not two"
    [piece] = the_run(lead).pieces
    assert (piece.setup_request_id, piece.attempt) == (pages[-1].setup_request_id, 2)

    lead.task_manager.complete_task("task-lead", datetime.now(timezone.utc).isoformat())
    with caplog.at_level("INFO"):
        await gateway_result(lead, first, "expired", attempt=1)

    answers.assert_not_awaited()
    assert f"event=tool_setup_result_duplicate run_id={the_run(lead).run_id}" in caplog.text


@pytest.mark.asyncio
async def test_a_result_replayed_into_a_run_stored_by_the_previous_version_is_handled_as_before(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A run saved by the previous version records no result ids (C10, finding F2).

    A copy replayed into it after the upgrade (an inbox message the stopped
    agent had not cleared) gets the previous version's handling: the done
    piece stays done, the follow-up steps run again so the task still
    continues, and only from then on is a further copy ignored.
    """

    from custom_agents.tool_setup_run_runtime import run_repository
    from storage.tool_setup_run_repository import RUN_TABLE

    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT, "cli:google": SIGNED_OUT | {"team_sign_in_app": "configured"}})
    _tool_call, raised = start(lead, [GITHUB, "cli:google"], purpose="check your GitHub and Gmail for Acme")
    await pause_turn(lead, raised.interrupt)
    github, _google = sent_pages(lead)
    await gateway_result(lead, github, "completed", account_label="exampleuser")
    run = the_run(lead)
    # Store the run as the previous version wrote it: the same run without the result ids.
    stored = json.loads(run.model_dump_json())
    del stored["results_applied"], stored["results_handled"]
    repository = run_repository(lead)
    with repository._connect() as conn:
        conn.execute(
            f"UPDATE {RUN_TABLE} SET payload_json = {repository.dialect.bind} WHERE run_id = {repository.dialect.bind}",
            (json.dumps(stored), run.run_id),
        )
        conn.commit()
    assert (the_run(lead).results_applied, the_run(lead).results_handled) == ((), ()), "it loads"

    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")

    assert "event=tool_setup_result_duplicate" not in caplog.text, "the previous version's copy is handled as it was"
    assert [piece.status for piece in the_run(lead).pieces if piece.tool_id == GITHUB] == ["done"]
    assert _github_history(lead) == ["Setup page (sign_in) for cli:github: completed."] * 2
    assert the_run(lead).results_handled == (github.setup_request_id,)

    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
    assert f"event=tool_setup_result_duplicate run_id={run.run_id}" in caplog.text
    assert len(_github_history(lead)) == 2


# A result whose first handling failed partway (finding F1) --------------------------------


def _github_history(lead) -> list[str]:
    entries = lead.task_manager.get_task_history_entries("task-lead", limit=None)
    return [entry.summary for entry in entries if entry.summary.startswith("Setup page (sign_in) for cli:github")]


def _page_on_its_step(lead, setup_request_id: str) -> tuple[str, str] | None:
    """The page's workflow step state and the page's own status on it."""

    record = workflow_service_for(lead).repository.get(the_run(lead).workflow_id)
    for step in record.steps:
        for link in step.setups:
            if link.setup_request_id == setup_request_id:
                return str(getattr(step.state, "value", step.state)), str(link.status)
    return None


async def _github_page_after_google(tmp_path: Path, monkeypatch):
    """Two tools, so a workflow tracks the pages; the Google page finished first."""

    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT, "cli:google": SIGNED_OUT | {"team_sign_in_app": "configured"}})
    _tool_call, raised = start(lead, [GITHUB, "cli:google"], purpose="check your GitHub and Gmail for Acme")
    await pause_turn(lead, raised.interrupt)
    github, google = sent_pages(lead)
    await gateway_result(lead, google, "completed", account_label="me@acme.example")
    return lead, github


def _failing_once(real, error: Exception):
    calls = 0

    def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return real(*args, **kwargs)

    return call


def _async_failing_once(real, error: Exception):
    calls = 0

    async def call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return await real(*args, **kwargs)

    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_step", ["task_lookup", "workflow_step", "history", "next_pages"])
async def test_a_result_whose_first_handling_failed_continues_the_task_when_dispatched_again(
    tmp_path: Path, monkeypatch, caplog, failing_step: str
) -> None:
    """The run applied the result, then a follow-up step failed; the inbox dispatches it again and the task resumes."""

    from custom_agents import tool_setup_run_runtime as run_runtime

    lead, github = await _github_page_after_google(tmp_path, monkeypatch)
    error = RuntimeError("the store is unavailable for a moment")
    if failing_step == "task_lookup":
        monkeypatch.setattr(lead.task_manager, "get_task_info", _failing_once(lead.task_manager.get_task_info, error))
    elif failing_step == "workflow_step":
        monkeypatch.setattr(run_runtime, "record_setup_result", _failing_once(run_runtime.record_setup_result, error))
    elif failing_step == "history":
        monkeypatch.setattr(
            run_runtime, "append_wait_history", _async_failing_once(run_runtime.append_wait_history, error)
        )
    else:
        monkeypatch.setattr(run_runtime, "advance", _async_failing_once(run_runtime.advance, error))

    with pytest.raises(RuntimeError):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
    assert the_run(lead).results_applied[-1] == github.setup_request_id
    assert github.setup_request_id not in the_run(lead).results_handled
    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")

    assert "event=tool_setup_result_handled_again" in caplog.text
    assert "event=tool_setup_result_duplicate" not in caplog.text
    assert "task-lead" in lead._pending_interrupt_responses, "the task resumes"
    assert the_run(lead).status == "finished"
    assert _page_on_its_step(lead, github.setup_request_id)[1] == "completed", "its workflow step records the page"
    assert 1 <= len(_github_history(lead)) <= 2, "at most one repeated history entry"
    assert github.setup_request_id in the_run(lead).results_handled

    caplog.clear()
    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
    assert "event=tool_setup_result_duplicate" in caplog.text, "once handled, a copy is ignored"


@pytest.mark.asyncio
async def test_an_answer_for_an_ended_task_that_failed_is_sent_when_dispatched_again_and_only_once(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock

    from custom_agents import tool_setup_runtime

    lead, github = await _github_page_after_google(tmp_path, monkeypatch)
    lead.task_manager.complete_task("task-lead", datetime.now(timezone.utc).isoformat())
    answers = AsyncMock(side_effect=[TimeoutError("nats: timeout"), None, None])
    monkeypatch.setattr(tool_setup_runtime, "_publish_task_response", answers)

    with pytest.raises(TimeoutError):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
    await gateway_result(lead, github, "completed", account_label="exampleuser")
    await gateway_result(lead, github, "completed", account_label="exampleuser")

    assert answers.await_count == 2, "the failed answer is sent again; a later copy does not answer a third time"


@pytest.mark.asyncio
async def test_a_late_copy_of_an_expired_pages_result_leaves_the_completed_step_alone(tmp_path: Path, monkeypatch) -> None:
    lead, first = await _github_page_after_google(tmp_path, monkeypatch)
    await gateway_result(lead, first, "expired", attempt=1)
    second = sent_pages(lead)[-1]
    assert second.setup_request_id != first.setup_request_id
    await gateway_result(lead, second, "completed", account_label="exampleuser")
    step_before, pages_before = _page_on_its_step(lead, second.setup_request_id), len(sent_pages(lead))

    await gateway_result(lead, first, "expired", attempt=1)

    assert _page_on_its_step(lead, second.setup_request_id) == step_before
    assert len(sent_pages(lead)) == pages_before


@pytest.mark.asyncio
async def test_a_first_result_for_a_piece_whose_page_send_failed_is_not_a_duplicate(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The page request raised (NATS may still have stored it), so the piece failed; the page opened anyway (F2)."""

    lead = lead_agent(tmp_path, enabled=False)
    broker_states(monkeypatch, {GITHUB: SIGNED_OUT})
    lead.team_comms.send_message.side_effect = TimeoutError("nats: timeout")
    _tool_call, raised = start(lead, [GITHUB], purpose="list the repos")
    await pause_turn(lead, raised.interrupt)
    [page] = sent_pages(lead)
    lead.team_comms.send_message.side_effect = None

    with caplog.at_level("INFO"):
        await gateway_result(lead, page, "completed", account_label="exampleuser")

    assert "event=tool_setup_result_duplicate" not in caplog.text
    assert [piece.status for piece in the_run(lead).pieces] == ["failed"], "a stale result never changes the piece"
    assert _github_history(lead) == ["Setup page (sign_in) for cli:github: completed."]


@pytest.mark.asyncio
async def test_recovery_and_a_copy_of_a_result_whose_handling_failed_resume_the_task_once(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Setup-run recovery advances the run before the copy arrives; the copy still finishes its steps, without a second resume."""

    from custom_agents import paused_wait_resume
    from custom_agents import tool_setup_run_runtime as run_runtime

    lead, github = await _github_page_after_google(tmp_path, monkeypatch)
    # The first handling stops at the task lookup: no workflow step, history entry or advance yet.
    monkeypatch.setattr(
        lead.task_manager, "get_task_info", _failing_once(lead.task_manager.get_task_info, RuntimeError("store busy"))
    )
    with pytest.raises(RuntimeError):
        await gateway_result(lead, github, "completed", account_label="exampleuser")
    assert the_run(lead).status == "waiting", "the result was saved, its advance lost"
    assert _page_on_its_step(lead, github.setup_request_id)[1] != "completed"
    wakes = []
    real_wake = paused_wait_resume.schedule_pending_work
    monkeypatch.setattr(paused_wait_resume, "schedule_pending_work", lambda agent: (wakes.append(1), real_wake(agent))[1])
    monkeypatch.setattr(run_runtime, "RECENT_RUN_SECONDS", -1.0)

    assert await run_runtime.resume_ended_setup_runs(lead) == 1
    assert "task-lead" in lead._pending_interrupt_responses and len(wakes) == 1, "recovery resumed the task"
    assert _page_on_its_step(lead, github.setup_request_id)[1] != "completed", "recovery does not record the page"

    with caplog.at_level("INFO"):
        await gateway_result(lead, github, "completed", account_label="exampleuser")

    assert "event=tool_setup_result_handled_again" in caplog.text
    assert len(wakes) == 1, "the copy does not resume the task again"
    assert _page_on_its_step(lead, github.setup_request_id)[1] == "completed"
    assert _github_history(lead) == ["Setup page (sign_in) for cli:github: completed."]
    assert github.setup_request_id in the_run(lead).results_handled
