"""The runtime's decision point for function tools, with real Cedar and SQLite.

Covers: standard grants (own runtime tools need no grant), tools that need a
grant, starter profiles and a team lead's hand-down, calls the broker decides,
authorization off, unsigned tasks, system work with no person, and the
Strands hook cancelling a refused call.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal
from access_control.starter_profiles import REASON_PREFIX, applied_profile, apply_starter_profile, chosen_by_person
from assistant_gateway.access_setup import apply_configured_starter_profile
from custom_agents import access_runtime
from custom_agents.access_runtime import AccessDecisionHook, decide_call
from custom_messages.contracts.access_v2 import GrantStatus, GrantV1, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys
from strands.models.model import Model as _StrandsModel

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
OWNER = PersonPrincipal("user-1", TEAM, ("admin",))

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    access_runtime._runtime_access.cache_clear()


def agent(storage, *, agent_type="WebResearchAgent", lead=False, role="worker", person="user-1", enabled=True, signed=True):
    signed_context = (
        context_signer().issue(team_id=TEAM, person_id=person, person_roles=("admin",)) if signed else None
    )
    meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=signed_context), conversation_id="c1")
    task_manager = SimpleNamespace(
        storage_settings=storage,
        current_task_id="task-1",
        get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        terminal_callbacks=[],
    )
    task_manager.add_task_terminal_callback = task_manager.terminal_callbacks.append
    return SimpleNamespace(
        id=f"agent-{agent_type}",
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=lead,
        access_role=role,
        authorization_config={"enabled": enabled, "cedar_binary_path": str(BINARY)},
        task_manager=task_manager,
        instance_root=None,
    )


def admin_service(storage) -> AccessService:
    return AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
    )


def test_own_runtime_tools_need_no_grant_and_other_tools_do(storage) -> None:
    worker = agent(storage)

    assert decide_call(worker, "send_message", {}).allowed
    # Internet access is standard for every role (decision D14).
    assert decide_call(worker, "exa_search", {}).allowed
    refused = decide_call(worker, "publish_single_page_site", {})
    assert not refused.allowed and refused.tool_id == "fn:publishing" and refused.level.value == "write"
    assert not decide_call(worker, "instantiate_agent", {}).allowed, "managing workers is the team lead's"
    assert decide_call(agent(storage, agent_type="TeamArchitect", lead=True), "instantiate_agent", {}).allowed


def test_a_starter_profile_lets_the_team_lead_hand_standard_tools_to_workers(storage) -> None:
    service = admin_service(storage)
    try:
        created = apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")
        assert [grant.reason for grant in created] == [f"{REASON_PREFIX}trusted"] * 5
        # Model choice and credential lookup only because the owner is an administrator
        # (tests/test_model_choice.py, tests/test_broker_access_gate.py).
        assert {grant.target.id for grant in created} == {
            "standard", "sandboxed_code", "fn:model_choice", "cli:sealbox", "procedure:credentials"
        }
        assert apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect") == created
        # The profile's grants serve the applying person's work only (decision D13).
        assert {grant.for_person_id for grant in created} == {OWNER.person_id}
        lead = agent(storage, agent_type="TeamArchitect", lead=True)
        assert decide_call(lead, "publish_single_page_site", {}).allowed
        lead_for_someone_else = agent(storage, agent_type="TeamArchitect", lead=True, person="user-2")
        assert not decide_call(lead_for_someone_else, "publish_single_page_site", {}).allowed
        # Trusted includes sandboxed shell and Python, never running outside the sandbox.
        assert decide_call(lead, "shell", {"command": "ls"}).allowed
        assert decide_call(lead, "python_env", {"action": "run"}).allowed
        from access_control.entities import AgentPrincipal as _Agent
        from custom_messages.contracts.access_v2 import AccessLevel

        lead_principal = _Agent("agent-TeamArchitect", "TeamArchitect", "team_lead", TEAM)
        unconfined = TargetRefV1(kind=TargetKind.TOOL, id="code:unconfined")
        assert not service.holds(lead_principal, unconfined, AccessLevel.FULL, task_id="task-1", person_id=OWNER.person_id)

        # The worker's first call adds it to its task from what TeamLead may hand on.
        assert decide_call(agent(storage), "publish_single_page_site", {}).allowed
        [handed] = [
            grant for grant in service.store.list_team(TEAM)
            if grant.subject.id == f"{TEAM}/WebResearchAgent" and grant.target.id == "fn:publishing"
        ]
        assert handed.for_person_id == OWNER.person_id and handed.lifetime.value == "task"

        # Switching profiles replaces the earlier profile's grants, and with them the hand-down.
        apply_starter_profile(service, OWNER, "manual", lead_agent_type="TeamArchitect")
        assert all(g.status is GrantStatus.REVOKED for g in service.store.list_team(TEAM))
        assert not decide_call(agent(storage), "publish_single_page_site", {}).allowed
    finally:
        service.cedar.close()


def test_reapplying_a_profile_leaves_access_that_covers_it_alone(storage) -> None:
    from custom_messages.contracts.access_v2 import AccessLevel

    service = admin_service(storage)
    try:
        apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")
        # The owner later gives the team lead more on one profile tool: it replaces the profile's grant.
        lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect")
        broader = service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=lead,
                target=TargetRefV1(kind=TargetKind.TOOL, id="cli:sealbox"),
                level=AccessLevel.FULL,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=datetime.now(timezone.utc),
                for_person_id=OWNER.person_id,
            ),
        )
        active_before = {g.grant_id for g in service.store.list_team(TEAM) if g.status is GrantStatus.ACTIVE}
        assert broader.grant_id in active_before

        # Startup re-applies the configured profile: nothing is revoked or given again.
        apply_starter_profile(service, OWNER, "trusted", lead_agent_type="TeamArchitect")

        active_after = {g.grant_id for g in service.store.list_team(TEAM) if g.status is GrantStatus.ACTIVE}
        assert active_after == active_before
    finally:
        service.cedar.close()


def test_the_configured_profile_is_the_first_users_default_until_they_choose(storage, tmp_path, caplog) -> None:
    config = SimpleNamespace(
        authorization=SimpleNamespace(enabled=True, starter_profile="trusted", cedar_binary_path=str(BINARY)),
        manager=SimpleNamespace(team_id=TEAM, named_agent_type="TeamArchitect"),
        user=SimpleNamespace(canonical_user_id=OWNER.person_id),
        instance_root_path=tmp_path,
        manager_storage_settings=storage,
    )
    memberships = SimpleNamespace(roles_of=lambda _person_id, _team_id: OWNER.roles)
    service = admin_service(storage)
    try:
        apply_configured_starter_profile(config, memberships)
        assert applied_profile(service, OWNER.person_id) == "trusted" and not chosen_by_person(service, OWNER.person_id)
        # Until the person chooses, a changed setting applies at the next start.
        config.authorization.starter_profile = "careful"
        apply_configured_starter_profile(config, memberships)
        assert applied_profile(service, OWNER.person_id) == "careful"

        # "manual" leaves no grants, and the person's own choice survives restarts.
        apply_starter_profile(service, OWNER, "manual", lead_agent_type="TeamArchitect")
        config.authorization.starter_profile = "trusted"
        with caplog.at_level("INFO"):
            apply_configured_starter_profile(config, memberships)
        assert applied_profile(service, OWNER.person_id) == "manual" and chosen_by_person(service, OWNER.person_id)
        assert not [g for g in service.store.list_team(TEAM) if g.status is GrantStatus.ACTIVE]
        assert "event=starter_profile_kept" in caplog.text
    finally:
        service.cedar.close()


def test_broker_calls_off_mode_unsigned_tasks_and_system_work(storage) -> None:
    worker = agent(storage)
    assert decide_call(worker, "secure_cli", {"provider": "google"}) is None, "the broker decides"
    assert decide_call(agent(storage, enabled=False), "shell", {"command": "rm -rf /"}) is None, "authorization off"

    unsigned = decide_call(agent(storage, signed=False), "send_message", {})
    assert not unsigned.allowed and "verifiably" in unsigned.reason

    memory = agent(storage, agent_type="MemReviewAgent", role="system", person=None)
    assert decide_call(memory, "list_pending_memory_review_jobs", {}).allowed
    assert not decide_call(agent(storage, person=None), "send_message", {}).allowed, "only system agents work for nobody"


def test_the_hook_cancels_a_refused_call_with_the_next_step(storage) -> None:
    hook = AccessDecisionHook(agent(storage))
    registered = object()  # the agent has the tool (Strands' selected_tool)
    refused = SimpleNamespace(
        tool_use={"name": "publish_single_page_site", "input": {"title": "x"}}, cancel_tool=False, selected_tool=registered
    )
    allowed = SimpleNamespace(tool_use={"name": "send_message", "input": {}}, cancel_tool=False, selected_tool=registered)

    asyncio.run(hook.before_tool_call(refused))
    asyncio.run(hook.before_tool_call(allowed))

    payload = json.loads(refused.cancel_tool)
    assert payload["status"] == "access_denied" and payload["tool_id"] == "fn:publishing"
    assert "request_access" in payload["next_step"]
    assert allowed.cancel_tool is False


class _AnswersWithStructuredOutput(_StrandsModel):
    """A scripted model that answers the way real models do: by calling Strands' structured-output tool."""

    def __init__(self, tool_input: dict) -> None:
        self.tool_input = tool_input
        self.config: dict = {}

    def update_config(self, **model_config) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover - not used
        raise NotImplementedError
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        name = tool_specs[0]["name"] if tool_specs else "missing"
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": "call-1"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(self.tool_input)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_the_models_structured_answer_is_not_refused_as_an_unknown_tool(storage) -> None:
    """A clean instance's first chat failed: the access hook refused ModelAgentTurnDecision (the
    runtime's turn answer, delivered through Strands' structured-output tool) as fn:unregistered."""

    from pydantic import BaseModel
    from strands import Agent

    class ModelAgentTurnDecision(BaseModel):
        answer: str

    team_lead = agent(storage, agent_type="TeamArchitect", lead=True)
    strands_agent = Agent(
        model=_AnswersWithStructuredOutput({"answer": "Hi Jordan"}),
        tools=[],
        hooks=[AccessDecisionHook(team_lead)],
        callback_handler=None,
    )

    result = strands_agent("Hello", structured_output_model=ModelAgentTurnDecision)

    assert result.structured_output == ModelAgentTurnDecision(answer="Hi Jordan")


class _CallsDelegateTaskAsATool(_AnswersWithStructuredOutput):
    """A scripted model that first calls its delegate_task action as if it were a tool."""

    def __init__(self, tool_input: dict) -> None:
        super().__init__(tool_input)
        self.results_seen: list[dict] = []
        self.calls = 0

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        self.results_seen.extend(
            block["toolResult"] for message in messages for block in message.get("content", []) if "toolResult" in block
        )
        if self.calls > 1:
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event
            return
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {"name": "delegate_task", "toolUseId": "call-0"}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({"task": {"problem_statement": "research"}})}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_a_tool_the_agent_does_not_have_is_unknown_not_an_access_refusal(storage) -> None:
    """A fresh instance's TeamLead called its delegate_task action as a tool; the access hook answered
    access_denied for fn:unregistered, so TeamLead told the person delegation was broken."""

    from pydantic import BaseModel
    from strands import Agent

    from custom_agents.runtime_guardrails import RuntimeGuardrailsPlugin

    class ModelAgentTurnDecision(BaseModel):
        answer: str

    model = _CallsDelegateTaskAsATool({"answer": "delegated"})
    team_lead = agent(storage, agent_type="TeamArchitect", lead=True)
    strands_agent = Agent(
        model=model, tools=[], hooks=[AccessDecisionHook(team_lead), RuntimeGuardrailsPlugin()], callback_handler=None
    )

    result = strands_agent("Research this", structured_output_model=ModelAgentTurnDecision)

    [unknown] = model.results_seen
    texts = [block["text"] for block in unknown["content"]]
    assert texts[0] == "Unknown tool: delegate_task"
    assert "access_denied" not in json.dumps(unknown)
    assert 'action whose action_type is "delegate_task"' in texts[1]
    assert result.structured_output == ModelAgentTurnDecision(answer="delegated")
