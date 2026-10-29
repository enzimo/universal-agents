"""Live: a real TeamArchitect on a model that takes at most 128 tools answers, loads a group and uses it.

Before tools loaded on request, every TeamArchitect request carried 142 tools
(169 with authorization off) and OpenAI models refused it ("Invalid 'tools':
array too long"). This builds a real TeamArchitect from its definition with
create_strands_agent, on the configured LiteLLM endpoint, and asks it to list
objectives: its requests must be accepted, and the objectives group must load
(with use_tools, or by calling objectives directly) and run.
``tests/test_live_action_tools.py`` uses the same TeamArchitect (``team_lead``,
``ask``) for the other action tools.

Run with ``RUN_LIVE_TOOL_LOADING=1``, ``TEAM_NATS_PROVIDER_SMOKE_API_BASE`` and
``TEAM_NATS_PROVIDER_SMOKE_MODEL_ID`` (for example
``https://bifrost.example.net/v1`` and ``openai/gpt-6-luna``); the key is the
``LITELLM_API_KEY`` in the env file named by ``TEAM_NATS_LIVE_ENV`` (default
``debug.env``), read from the file and given to the model through the
runtime's key lookup (``custom_agents.model_keys``), never as a fixed key.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from strands.hooks import AfterToolCallEvent, BeforeModelCallEvent, HookProvider, HookRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = PROJECT_ROOT / "target" / "debug" / "ua-cedar-authorizer"
MAX_MODEL_CALLS = 10

pytestmark = [
    pytest.mark.live,
    pytest.mark.external,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TOOL_LOADING") != "1",
        reason="Set RUN_LIVE_TOOL_LOADING=1 to call the configured model provider.",
    ),
]


def _settings() -> tuple[str, str, dict[str, str]]:
    base = str(os.getenv("TEAM_NATS_PROVIDER_SMOKE_API_BASE") or "").strip()
    model_id = str(os.getenv("TEAM_NATS_PROVIDER_SMOKE_MODEL_ID") or "").strip()
    env_path = Path(os.getenv("TEAM_NATS_LIVE_ENV", str(PROJECT_ROOT / "debug.env"))).expanduser()
    values = {k: str(v or "") for k, v in dotenv_values(env_path).items()} if env_path.is_file() else {}
    if not base or not model_id or not values.get("LITELLM_API_KEY"):
        pytest.skip("Set TEAM_NATS_PROVIDER_SMOKE_API_BASE, TEAM_NATS_PROVIDER_SMOKE_MODEL_ID and LITELLM_API_KEY.")
    return base, model_id, values


class Watch(HookProvider):
    """The tools each request carried, the tools that ran with their inputs, and the Objectives listed."""

    def __init__(self) -> None:
        self.request_sizes: list[int] = []
        self.ran: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.objective_results: list[dict] = []

    def register_hooks(self, registry: HookRegistry, **_kw) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model, order=1000)
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        registry = event.agent.tool_registry
        self.request_sizes.append(len(registry.get_all_tool_specs()))
        if len(self.request_sizes) > MAX_MODEL_CALLS:
            event.cancel = "Live tool-loading check exceeded its model-call bound."

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        name = str(event.tool_use.get("name") or "")
        inputs = dict(event.tool_use.get("input") or {})
        self.ran.append(name)
        self.calls.append((name, inputs))
        if name == "objectives" and inputs.get("action") == "list":
            assert event.result["status"] == "success", event.result
            for content in event.result.get("content", []):
                if "text" in content:
                    self.objective_results.append(json.loads(content["text"]))


def team_lead(tmp_path, monkeypatch, authorization: bool):
    """A real TeamArchitect from its definition on the configured endpoint, working on a person's task.

    Gateway tasks carry identity and a conversation in both modes. Private
    history still needs them with authorization off; absent context caused
    unrelated thread calls to fail and the live model to keep retrying.
    """

    from custom_agents import access_runtime
    from custom_agents.model_keys import ModelKeys
    from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
    from custom_agents.strands_team import AGENT_TYPE_MAP, create_strands_agent
    from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
    from custom_messages.contracts.task_v1 import Task
    from custom_messages.enums import MessageMetaType
    from tests.task_work_support import bind_task, set_agent_ref
    from tests.broker_access_support import context_signer, context_verify_keys

    base, model_id, values = _settings()
    if authorization and not CEDAR_BINARY.is_file():
        pytest.skip("build ua-cedar-authorizer first (cargo build)")
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    keys = ModelKeys(None, env_lookup=values.get)
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm", model_id=model_id, litellm_api_base=base, max_output_tokens=1_000, model_keys=keys,
            request_timeout_seconds=120, stream_idle_timeout_seconds=60, model_call_timeout_seconds=120,
        )
    )
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(tmp_path / "instance"))
    config = dict(AGENT_TYPE_MAP["TeamArchitect"])
    config.update({
        "team_id": "team_live", "is_team_lead": True, "named_agent_type": "TeamArchitect",
        "authorization_config": {"enabled": authorization, "cedar_binary_path": str(CEDAR_BINARY)},
        "task_history_db_path": str(tmp_path / "t.db"), "storage_sqlite_path": str(tmp_path / "t.db"),
        "instance_root": str(tmp_path / "instance"),
        "model_provider": "litellm", "model_id": model_id, "litellm_api_base": base,
    })
    with patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=model):
        agent = create_strands_agent(config)
    set_agent_ref(agent)
    meta = MessageMeta(conversation_id="conv-live")
    meta.gateway_context = type("Gateway", (), {})()
    meta.gateway_context.task_context = context_signer().issue(
        team_id="team_live", person_id="usr_live_owner", person_roles=("owner",)
    )
    agent.task_manager.add_task(Task(task_id="task-live", task_type=MessageMetaType.REQUEST), MessageHeader(), meta, owner_person_id=None)
    bind_task("task-live")
    agent._conversation_store.ensure_thread(
        conversation_id="conv-live", team_id="team_live", owner_agent_id=agent.id, owner_agent_name=agent.name,
        requester_name="Live test owner", requester_id="usr_live_owner", root_task_id="task-live",
        owner_canonical_user_id="usr_live_owner",
    )
    watch = Watch()
    agent._strands_agent.hooks.add_hook(watch)
    return agent, watch


def ask(agent, text: str):
    """Invoke the agent as the runtime does: it ends its turn with its turn decision (structured output).

    A raw SDK call without it contradicts the system prompt and can loop over
    successful tools while looking for a permitted way to answer.
    """

    from custom_agents.sdk.strands.turn import _turn_decision_initial_guidance

    decision_model = agent.turn_executor._turn_decision_structured_output_model()
    return agent._strands_agent(
        "This is task task-live in conversation conv-live for usr_live_owner. "
        + text
        + " "
        + _turn_decision_initial_guidance(decision_model.__name__),
        structured_output_model=decision_model,
        structured_output_prompt=agent.turn_executor._turn_decision_structured_output_prompt(),
    )


def answered(result) -> str:
    """The task answer of the agent's turn decision (it must give one)."""

    assert result.structured_output is not None, "The model did not return a structured answer."
    decision = result.structured_output.to_runtime_decision()
    assert decision.actions[-1].action_type == "task_response", decision
    assert decision.actions[-1].task.answer, decision
    return decision.actions[-1].task.answer


@pytest.mark.parametrize("authorization", [False, True], ids=["authorization_off", "authorization_on"])
def test_a_real_team_lead_answers_within_the_cap_and_loads_objectives(tmp_path, monkeypatch, authorization) -> None:
    from custom_agents import access_runtime
    from custom_agents.tool_loading import loaded_tools

    agent, watch = team_lead(tmp_path, monkeypatch, authorization)

    result = ask(
        agent,
        "Use your objectives tools to list the team's objectives once (load them first if they are not in your "
        "tool list), then tell me how many there are in one short sentence. Leave the conversation's tags unchanged.",
    )

    assert watch.request_sizes and max(watch.request_sizes) <= 128, watch.request_sizes
    assert len(watch.request_sizes) <= MAX_MODEL_CALLS, "The model did not finish within the live check's bound."
    assert "fn:objectives" in loaded_tools(agent).loaded() or "use_tools" in watch.ran, watch.ran
    assert ("objectives", "list") in [(name, inputs.get("action")) for name, inputs in watch.calls], watch.calls
    assert watch.objective_results and any(isinstance(item.get("objectives"), list) for item in watch.objective_results), watch.objective_results
    answered(result)
    access_runtime._runtime_access.cache_clear()


def test_the_raise_test_request_finds_the_providers_real_limit() -> None:
    """The request that guards a cap raise (custom_agents.model_tool_limit) passes under the limit and fails over it."""

    from custom_agents.model_keys import ModelKeys
    from custom_agents.runtime_model_switch import RuntimeModelSwitchProbeError, probe_tool_capacity
    from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model

    base, model_id, values = _settings()
    model = build_strands_model(
        StrandsModelFactoryConfig(
            provider="litellm", model_id=model_id, litellm_api_base=base, max_output_tokens=64,
            model_keys=ModelKeys(None, env_lookup=values.get),
        )
    )

    passed = probe_tool_capacity(model, tool_count=120, timeout_seconds=120)
    assert passed["status"] == "passed" and passed["tools_sent"] == 120
    if os.getenv("TEAM_NATS_PROVIDER_SMOKE_TOOL_LIMIT") == "128":
        with pytest.raises(RuntimeModelSwitchProbeError, match="refused a request with 129 tools"):
            probe_tool_capacity(model, tool_count=129, timeout_seconds=120)


def test_team_lead_raises_a_real_models_cap_through_staging_and_apply(tmp_path, monkeypatch) -> None:
    """Staging and applying a raise against the real model (custom_agents.model_tool_limit).

    The model starts with a cap of 100 in LLM_OVERRIDES.yaml. A raise to 120
    is tested at staging and again before it is saved; with the provider's
    limit at 128 (``TEAM_NATS_PROVIDER_SMOKE_TOOL_LIMIT=128``), a raise to 129
    is refused at staging and nothing is staged.
    """

    import yaml

    from custom_agents.model_keys import ModelKeys
    from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
    from custom_agents.tool_loading import tool_limit
    from custom_tools.providers.runtime_settings import plan_runtime_setting_change
    from tests.task_work_support import set_agent_ref
    from custom_agents import access_runtime
    from tests.broker_access_support import context_verify_keys
    from tests.test_runtime_settings_tools import _apply_settings_change, _fake_agent, _overrides_path, _write_config

    base, model_id, values = _settings()
    # The gateway's test signing keys, as the offline runtime settings tests use.
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    config_path = _write_config(tmp_path)
    overrides = _overrides_path(config_path)
    overrides.parent.mkdir(parents=True, exist_ok=True)
    overrides.write_text(yaml.safe_dump({"model_capabilities": {"litellm": {model_id: {"max_tools_per_request": 100}}}}))
    agent = _fake_agent(config_path)
    agent.model_id = model_id
    agent.model_capabilities = {}
    agent.request_timeout_seconds = 120
    agent._configure_model_for_profile = lambda profile: build_strands_model(
        StrandsModelFactoryConfig(
            provider=profile.provider, model_id=profile.model_id, litellm_api_base=base, max_output_tokens=64,
            model_keys=ModelKeys(None, env_lookup=values.get),
        )
    )
    set_agent_ref(agent)
    try:
        def stage(value: int) -> dict:
            return json.loads(plan_runtime_setting_change._tool_func(
                path="model.max_tools_per_request", value=value, reason="Live test of a cap raise."
            ))

        if os.getenv("TEAM_NATS_PROVIDER_SMOKE_TOOL_LIMIT") == "128":
            refused = stage(129)
            assert "change_id" not in refused and "refused a request with 129 tools" in refused["error"], refused
        plan = stage(120)
        assert plan["model_test"]["tools_sent"] == 120, plan
        result = json.loads(_apply_settings_change(plan["change_id"], confirmed=True))
    finally:
        set_agent_ref(None)
        access_runtime._runtime_access.cache_clear()

    assert not result.get("error"), result
    saved = yaml.safe_load(overrides.read_text())
    assert saved["model_capabilities"]["litellm"][model_id]["max_tools_per_request"] == 120
    assert tool_limit(agent) == (120, "litellm", model_id)
