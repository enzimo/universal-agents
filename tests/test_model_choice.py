"""Model choice: TeamLead switches agents' models at once for administrators (owner's decision 2026-09-27).

Real Cedar and the SQLite access store. Covers: a runtime settings plan of
model-choice settings only is the ``model_choice`` change kind; with the
Trusted profile an administrator's TeamLead applies it at once (``held_access``),
the chat gets a plain message and the administrator an Inbox notice with the
previous values; a member's Trusted profile leaves model choice out, so their
change waits for an administrator in the Inbox; a plan that also changes
another setting, or the request API (endpoint contract), waits too; endpoint
settings cannot be staged at all; a provider nobody set up is refused with
authorization on; another agent type's change says its agents use it from
their next turn, with no restart; a block on model choice sends the change to the Inbox; a member
never holds model choice even with a grant; and with authorization off
nothing changes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import InterruptException

# Runtime composition installs the cycle-free catalog lookup the services use.
from custom_agents import agent_catalog as _agent_catalog  # noqa: F401
from access_control.entities import PersonPrincipal, agent_type_id
from access_control.starter_profiles import apply_starter_profile
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents import access_runtime
from custom_agents.model_choice import MODEL_CHOICE_CHANGE_KIND, MODEL_CHOICE_TOOL_ID
from custom_agents.runtime_settings_service import RuntimeSettingsService
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessReviewChoice,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from tests.change_approval_support import (
    SignedTaskManager,
    ToolCall,
    change_agent,
    decide,
    needs_cedar,
    owner,
    run_apply_change,
    stored_request,
    use_signed_contexts,
)

TEAM = "team_model_choice"
LEAD = "TeamArchitect"
MEMBER_ID = "member_model_choice"
NEW_MODEL = "deepseek/deepseek-v4.1-flash"
MODEL_CHOICE_TOOL = TargetRefV1(kind=TargetKind.TOOL, id=MODEL_CHOICE_TOOL_ID)


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch):
    use_signed_contexts(monkeypatch)
    yield
    access_runtime._runtime_access.cache_clear()


class _NoPause(ToolCall):
    """A tool context for a change that must apply without waiting for anyone."""

    def interrupt(self, name: str, reason: Any = None) -> Any:
        raise AssertionError(f"unexpected pause for approval: {reason}")


def _config(tmp_path: Path) -> tuple[GatewayInstanceConfig, Path]:
    root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="model-choice-test", instance_root=str(root), team_id=TEAM, timezone="UTC"
    )
    return config, config.save(root / "instance_config.yaml")


def _agent(tmp_path: Path, *, authorization_enabled: bool = True) -> SimpleNamespace:
    """TeamArchitect working for the instance owner, with a live model it can test and switch."""

    config, config_path = _config(tmp_path)
    live = SimpleNamespace(probed=[], activated=[])

    def probe(profile: Any) -> dict[str, Any]:
        live.probed.append(profile.model_id)
        return {"status": "passed", "ready": True, "provider": profile.provider, "model_id": profile.model_id}

    agent = change_agent(
        config,
        config_path,
        authorization_enabled=authorization_enabled,
        model_provider="litellm",
        model_id="old/model",
        reasoning_effort=None,
        reasoning_summary=None,
        litellm_api_mode="chat_completions",
        context_window_tokens=None,
        supports_image_input=None,
        supports_audio_input=None,
        supports_file_input=None,
        probe_runtime_llm_profile=probe,
        live=live,
    )

    def activate(profile: Any, *, source: str = "test") -> None:
        live.activated.append(profile.model_id)
        agent.model_provider = profile.provider
        for field, value in profile.model_dump(mode="python").items():
            if field != "provider":
                setattr(agent, field, value)

    agent.apply_runtime_llm_profile = activate
    return agent


def _as_member(agent: SimpleNamespace) -> PersonPrincipal:
    """Let the agent's task work for a member instead of the owner."""

    agent.task_manager = SignedTaskManager(
        team_id=agent.team_id,
        person_id=MEMBER_ID,
        storage_settings=agent.task_manager.storage_settings,
        roles=("member",),
    )
    access_runtime._runtime_access.cache_clear()
    return PersonPrincipal(MEMBER_ID, agent.team_id, ("member",))


def _trusted(agent: SimpleNamespace, person: PersonPrincipal) -> list[GrantV1]:
    service = access_runtime.runtime_requests(agent).service
    return apply_starter_profile(service, person, "trusted", lead_agent_type=LEAD)


def _stage(agent: SimpleNamespace, changes: list[dict[str, Any]], **target: str) -> dict[str, Any]:
    from tests.task_work_support import set_agent_ref

    set_agent_ref(agent)
    return RuntimeSettingsService(agent).stage(changes, reason="The person asked for a faster model.", **target)


def _model_switch(agent: SimpleNamespace, **target: str) -> dict[str, Any]:
    return _stage(
        agent,
        [
            {"path": "current_agent.llm.model_id", "value": NEW_MODEL},
            {"path": "current_agent.llm.supports_image_input", "value": True},
            {"path": "current_agent.llm.supports_file_input", "value": False},
        ],
        **target,
    )


def _notices(agent: SimpleNamespace, person_id: str, roles: tuple[str, ...]) -> list:
    store = access_runtime.runtime_requests(agent).service.store
    return [
        notice
        for notice, _read in store.notices_for(agent.team_id, person_id=person_id, roles=roles)
        if notice.kind is AccessNoticeKind.MODEL_CHANGED
    ]


def _waits_for_inbox(agent: SimpleNamespace, change_id: str) -> ToolCall:
    call = ToolCall()
    with pytest.raises(InterruptException):
        run_apply_change(agent, change_id, call)
    return call


@needs_cedar
def test_an_administrators_model_change_applies_at_once_with_a_message_and_a_notice(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    grants = _trusted(agent, owner(agent))
    plan = _model_switch(agent)

    proposal = RuntimeSettingsService(agent).describe(plan["change_id"])
    applied = run_apply_change(agent, plan["change_id"], _NoPause())

    assert MODEL_CHOICE_TOOL_ID in {grant.target.id for grant in grants}
    assert proposal.kind == MODEL_CHOICE_CHANGE_KIND
    assert "applies at once" in plan["instructions"]
    # Owner's decision 2026-10-10: the model answered a test call while staging,
    # and the person confirms before it applies, even with model choice held.
    assert plan["model_test"]["status"] == "passed"
    assert "ask them to confirm. When they confirm, call apply_change" in plan["instructions"]
    assert applied["status"] == "applied", applied
    assert applied["approval"]["mode"] == "held_access"
    assert applied["approval"]["approved_by"] == LEAD
    assert applied["result"]["message"] == (
        f"Switched {LEAD} to {NEW_MODEL} (images on, files off). It takes effect now."
    )
    assert agent.live.activated == [NEW_MODEL]
    [notice] = _notices(agent, owner(agent).person_id, ("owner", "admin"))
    assert notice.recipient_person_id == owner(agent).person_id
    assert notice.title == f"{LEAD}: Switched {LEAD} to {NEW_MODEL} (images on, files off)"
    assert "Before, TeamArchitect had model old/model, images not set, files not set." in notice.summary
    assert "ask TeamLead to switch TeamArchitect back to model old/model" in notice.summary


@needs_cedar
def test_another_agent_types_model_change_applies_at_once_and_reaches_it_at_its_next_turn(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))
    plan = _stage(
        agent,
        [{"path": "current_agent.llm.model_id", "value": NEW_MODEL}],
        target_scope="agent_type",
        target_agent_type="WebResearchAgent",
    )

    applied = run_apply_change(agent, plan["change_id"], _NoPause())

    assert applied["status"] == "applied", applied
    assert applied["approval"]["mode"] == "held_access"
    assert applied["result"]["requires_restart"] is False
    assert applied["result"]["message"] == (
        f"Switched WebResearchAgent to {NEW_MODEL}. Running WebResearchAgent agents switch at their next turn; "
        "no restart is needed. Until then, their status shows the previous values."
    )
    assert "WebResearchAgent" in applied["result"]["running_agents"]
    assert agent.live.activated == []
    assert len(_notices(agent, owner(agent).person_id, ("owner",))) == 1


@needs_cedar
def test_a_members_model_change_waits_for_an_administrator(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    member = _as_member(agent)
    grants = _trusted(agent, member)
    plan = _model_switch(agent)

    call = _waits_for_inbox(agent, plan["change_id"])
    request = stored_request(agent, call)
    nothing_applied = agent.live.activated == []
    requests = access_runtime.runtime_requests(agent)
    requests.decide(call.request_id, owner(agent), AccessReviewChoice.APPROVE)
    call.response = {"kind": "access_request_decision", "status": "granted"}
    applied = run_apply_change(agent, plan["change_id"], call)

    assert MODEL_CHOICE_TOOL_ID not in {grant.target.id for grant in grants}
    assert nothing_applied
    assert request.change is not None and request.change.kind == MODEL_CHOICE_CHANGE_KIND
    assert applied["status"] == "applied", applied
    assert applied["approval"]["mode"] == "person_approval"
    assert applied["result"]["message"].startswith(f"Switched {LEAD} to {NEW_MODEL}")
    assert _notices(agent, MEMBER_ID, ("member",)) == []


@needs_cedar
@pytest.mark.parametrize(
    "extra",
    [
        # A team default in the same plan (same file): the whole plan is reviewed.
        {"path": "workspace_default.llm.reasoning_effort", "value": "high", "target_scope": "team"},
        # The request API switches endpoints (Chat Completions or Responses).
        {"path": "current_agent.llm.litellm_api_mode", "value": "responses"},
    ],
    ids=["mixed_plan", "api_mode"],
)
def test_a_plan_beyond_model_choice_waits_for_a_person(tmp_path: Path, extra: dict[str, Any]) -> None:
    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))
    plan = _stage(agent, [{"path": "current_agent.llm.model_id", "value": NEW_MODEL}, extra])

    call = _waits_for_inbox(agent, plan["change_id"])

    assert stored_request(agent, call).change.kind == "runtime_settings"
    assert agent.live.activated == []


@needs_cedar
def test_a_switch_sets_the_new_models_context_window_as_model_choice(tmp_path: Path) -> None:
    """Owner's decision 2026-10-10: a model switch sets the context window."""

    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))
    plan = _stage(
        agent,
        [
            {"path": "current_agent.llm.model_id", "value": NEW_MODEL},
            {"path": "current_agent.llm.context_window_tokens", "value": 1_000_000},
        ],
    )

    proposal = RuntimeSettingsService(agent).describe(plan["change_id"])
    applied = run_apply_change(agent, plan["change_id"], _NoPause())

    assert "context_window_notice" not in plan
    assert proposal.kind == MODEL_CHOICE_CHANGE_KIND
    assert applied["status"] == "applied", applied
    assert applied["result"]["message"] == (
        f"Switched {LEAD} to {NEW_MODEL} (context window 1,000,000 tokens). It takes effect now."
    )
    assert agent.context_window_tokens == 1_000_000


@needs_cedar
def test_endpoints_are_not_settings_agents_can_stage(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    with pytest.raises(ValueError, match="not agent-controllable"):
        _stage(agent, [{"path": "current_agent.llm.litellm_api_base", "value": "https://example.test/v1"}])


@needs_cedar
def test_a_provider_this_instance_is_not_set_up_for_is_refused(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))

    with pytest.raises(ValueError, match="gemini is not a model provider this instance is set up for") as refused:
        _stage(agent, [{"path": "current_agent.llm.provider", "value": "gemini"}])
    same_provider = _stage(
        agent,
        [
            {"path": "current_agent.llm.provider", "value": "litellm"},
            {"path": "current_agent.llm.model_id", "value": NEW_MODEL},
        ],
    )
    applied = run_apply_change(agent, same_provider["change_id"], _NoPause())

    assert "set up here: litellm" in str(refused.value)
    assert applied["status"] == "applied", applied
    assert applied["result"]["message"] == f"Switched {LEAD} to {NEW_MODEL} on litellm. It takes effect now."


@needs_cedar
def test_a_block_on_model_choice_sends_the_change_to_the_inbox(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))
    access_runtime.runtime_requests(agent).block(
        owner(agent),
        subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(agent.team_id, LEAD)),
        target=MODEL_CHOICE_TOOL,
        reason="Ask me before switching models.",
    )
    plan = _model_switch(agent)

    call = _waits_for_inbox(agent, plan["change_id"])
    decide(agent, call)
    applied = run_apply_change(agent, plan["change_id"], call)

    assert applied["approval"]["mode"] == "person_approval"
    assert _notices(agent, owner(agent).person_id, ("owner",)) == []


@needs_cedar
def test_only_administrators_hold_model_choice_even_with_a_grant(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    requests = access_runtime.runtime_requests(agent)
    service = requests.service
    member = PersonPrincipal(MEMBER_ID, agent.team_id, ("member",))
    service.grant_by_person(
        owner(agent),
        GrantV1(
            grant_id="new",
            team_id=agent.team_id,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=MEMBER_ID),
            target=MODEL_CHOICE_TOOL,
            level=AccessLevel.WRITE,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=owner(agent).person_id),
            reason="Try to let a member switch models.",
            created_at_utc=service.clock(),
        ),
    )
    target, level = requests.change_target(MODEL_CHOICE_CHANGE_KIND)

    assert service.holds(owner(agent), target, level, task_id=None)
    assert not service.holds(member, target, level, task_id=None)
    assert not service.may_give(member, target, level, task_id=None)
    assert requests.may_change_directly(owner(agent), MODEL_CHOICE_CHANGE_KIND)
    assert not requests.may_change_directly(member, MODEL_CHOICE_CHANGE_KIND)


@needs_cedar
def test_the_trusted_profile_follows_whether_the_person_is_an_administrator(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    as_member = PersonPrincipal(MEMBER_ID, agent.team_id, ("member",))
    as_admin = PersonPrincipal(MEMBER_ID, agent.team_id, ("admin",))

    member_grants = _trusted(agent, as_member)
    again = _trusted(agent, as_member)
    admin_grants = _trusted(agent, as_admin)

    assert [grant.grant_id for grant in again] == [grant.grant_id for grant in member_grants]
    assert MODEL_CHOICE_TOOL_ID not in {grant.target.id for grant in member_grants}
    assert MODEL_CHOICE_TOOL_ID in {grant.target.id for grant in admin_grants}


def test_with_authorization_off_agreement_still_applies_any_provider_change(tmp_path: Path) -> None:
    agent = _agent(tmp_path, authorization_enabled=False)
    other_provider = _stage(agent, [{"path": "current_agent.llm.provider", "value": "gemini"}])
    plan = _model_switch(agent)

    unconfirmed = run_apply_change(agent, plan["change_id"])
    assert unconfirmed["status"] == "agreement_required"
    applied = run_apply_change(agent, plan["change_id"], confirmed=True)

    assert other_provider["authority_mode"] == "authorization_disabled_agreement"
    assert applied["approval"]["mode"] == "agreement"
    assert applied["result"]["message"] == (
        f"Switched {LEAD} to {NEW_MODEL} (images on, files off). It takes effect now."
    )


@needs_cedar
def test_the_applied_switch_shows_every_model_setting_the_agent_type_now_runs_with(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))
    plan = _model_switch(agent)

    applied = run_apply_change(agent, plan["change_id"], _NoPause())

    [report] = applied["result"]["model_settings"]
    assert report["agent_type"] == LEAD
    table = report["table"]
    assert table.startswith(f"{LEAD} now runs with:\n\n| Setting | Value |\n|---|---|\n")
    for row in (
        "| Provider | litellm |",
        f"| Model | {NEW_MODEL} |",
        "| Reasoning effort | not set |",
        "| Reasoning summary | off |",
        "| Request API (LiteLLM) | chat_completions |",
        "| Context window (tokens) | automatic |",
        "| Image input | on |",
        "| Audio input | not set |",
        "| File input | off |",
    ):
        assert row in table
    # DeepSeek is not an OpenAI reasoning model: no suggestion.
    assert "suggestion" not in report


@needs_cedar
def test_switching_to_an_openai_reasoning_model_suggests_the_responses_api(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    _trusted(agent, owner(agent))
    plan = _stage(
        agent,
        [{"path": "current_agent.llm.model_id", "value": "openai/gpt-6-luna"}],
        target_scope="agent_type",
        target_agent_type="WebResearchAgent",
    )

    applied = run_apply_change(agent, plan["change_id"], _NoPause())

    [report] = applied["result"]["model_settings"]
    assert report["agent_type"] == "WebResearchAgent"
    assert "| Model | openai/gpt-6-luna |" in report["table"]
    assert "WebResearchAgent's request API to responses and its reasoning summary to auto" in report["suggestion"]
    assert "needs an administrator's approval" in report["suggestion"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"provider": "litellm", "model_id": "gpt-6-terra", "litellm_api_mode": "responses"}, "reasoning summary to auto"),
        (
            {"provider": "litellm", "model_id": "gpt-6-terra", "litellm_api_mode": "responses", "reasoning_summary": "auto"},
            None,
        ),
        ({"provider": "openai", "model_id": "gpt-6-terra"}, None),
        ({"provider": "litellm", "model_id": "anthropic/claude-sonnet-5"}, None),
    ],
)
def test_the_responses_suggestion_names_only_the_missing_step(values: dict[str, Any], expected: str | None) -> None:
    from custom_agents.model_settings_view import responses_suggestion

    suggestion = responses_suggestion("WebResearchAgent", values)

    assert (suggestion is None) if expected is None else (expected in suggestion and "request API" not in suggestion)
