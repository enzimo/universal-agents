from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from custom_agents import runtime_model_switch, strands_agent
from custom_agents.model_runtime_profile import RUNTIME_LLM_REFERENCES, runtime_llm_profile_from_agent
from custom_agents.runtime_model_switch import (
    RuntimeModelSwitchProbeError,
    probe_strands_model,
)
from custom_agents.strands_agent import UniversalAgent
from runtime_settings.projection import _project_model


def test_baseline_probe_builds_and_calls_the_candidate_with_its_own_request_settings(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _project_model(
        llm_cfg={
            "provider": "litellm", "model_id": "baseline/model",
            "litellm_api_base": "https://baseline.example.test/v1",
            "temperature": 0.7, "max_output_tokens": 256,
            "request_timeout_seconds": 17, "stream_idle_timeout_seconds": 9,
            "model_call_timeout_seconds": 23, "stream": False,
        },
        llm_failover={}, resolved_llm_override_path=tmp_path / "LLM_OVERRIDES.yaml",
    )
    candidate = runtime_llm_profile_from_agent(SimpleNamespace(**settings))
    agent = SimpleNamespace(
        instance_root=str(tmp_path), model_id="lead/model", request_timeout_seconds=91,
    )
    agent._configure_model_for_profile = lambda profile, **kwargs: UniversalAgent._configure_model_for_profile(
        agent, profile, **kwargs,
    )
    built = []
    invoked = []
    model = object()

    def build(config):
        built.append(config)
        return model

    def probe(candidate_model, *, timeout_seconds):
        invoked.append((candidate_model, timeout_seconds))
        return {"status": "passed"}

    monkeypatch.setattr(strands_agent, "build_strands_model", build)
    monkeypatch.setattr(strands_agent, "probe_strands_model", probe)

    result = UniversalAgent.probe_runtime_llm_profile(agent, candidate, request_settings=settings)

    [config] = built
    assert config.model_id == "baseline/model"
    assert config.litellm_api_base == "https://baseline.example.test/v1"
    assert config.temperature == 0.7
    assert config.max_output_tokens == 256
    assert config.stream is False
    assert config.request_timeout_seconds == 17
    assert config.stream_idle_timeout_seconds == 9
    assert config.model_call_timeout_seconds == 23
    assert invoked == [(model, 17.0)]
    assert result["ready"] is True
    assert agent.model_id == "lead/model"
    assert agent.request_timeout_seconds == 91


def test_probe_strands_model_reports_timeout(monkeypatch) -> None:
    class _SlowProbeAgent:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def invoke_async(self, prompt: str, *, structured_output_model, limits):
            del prompt, structured_output_model, limits
            await asyncio.sleep(0.05)

    monkeypatch.setattr(runtime_model_switch, "Agent", _SlowProbeAgent)

    with pytest.raises(RuntimeModelSwitchProbeError, match="timed out"):
        probe_strands_model(object(), timeout_seconds=0.001)


def test_apply_runtime_llm_override_restores_all_model_dependent_state_on_failure() -> None:
    old_components = {
        "_strands_model": object(),
        "_conversation_compactor": object(),
        "_conversation_summary_generator": object(),
        "_context_engine": object(),
        "_context_debug_dump_plugin": object(),
        "_strands_agent": object(),
        "_runtime_guardrails_plugin": object(),
        "_runtime_steering_plugin": object(),
        "_tool_result_budget_monitor": object(),
        "_skills_fingerprint": (("skill", 1, 1),),
    }
    agent = SimpleNamespace(
        id="agent_1",
        model_provider="litellm",
        model_id="old/model",
        supports_image_input=False,
        supports_audio_input=False,
        _logger=logging.getLogger("test.runtime_model_switch"),
        **old_components,
    )

    def _failing_rebuild() -> None:
        agent._strands_model = object()
        agent._context_engine = object()
        agent._strands_agent = object()
        raise RuntimeError("simulated rebuild failure")

    agent._rebuild_runtime_llm_components = _failing_rebuild

    with pytest.raises(RuntimeError, match="simulated rebuild failure"):
        UniversalAgent.apply_runtime_llm_override(
            agent,
            "litellm",
            "candidate/model",
            supports_image_input=True,
            supports_audio_input=True,
            source="test",
        )

    assert agent.model_provider == "litellm"
    assert agent.model_id == "old/model"
    assert agent.supports_image_input is False
    assert agent.supports_audio_input is False
    for field, old_value in old_components.items():
        assert getattr(agent, field) is old_value


def _running_agent(tmp_path, *, failover: bool) -> tuple[SimpleNamespace, list[int | None]]:
    """A running InternalImprovementAgent at 800,000 tokens, with the real refresh methods."""

    override = tmp_path / "LLM_OVERRIDES.yaml"
    override.write_text(
        "agent_types:\n  InternalImprovementAgent:\n    provider: litellm\n    model_id: gpt-6-luna\n"
        "    context_window_tokens: 800000\n",
        encoding="utf-8",
    )
    rebuilt_with: list[int | None] = []
    agent = SimpleNamespace(
        id="iia_1",
        team_id="team_1",
        named_agent_type="InternalImprovementAgent",
        instance_root=str(tmp_path),
        llm_override_path=str(override),
        model_provider="litellm",
        model_id="gpt-6-luna",
        _configured_model_provider="litellm",
        _configured_model_id="gpt-6-luna",
        reasoning_effort=None,
        reasoning_summary=None,
        litellm_api_mode=None,
        context_window_tokens=800_000,
        supports_image_input=None,
        supports_audio_input=None,
        supports_file_input=None,
        model_capabilities={},
        llm_failover={"enabled": failover},
        _logger=logging.getLogger("test.runtime_model_switch"),
    )
    for field in RUNTIME_LLM_REFERENCES:
        setattr(agent, field, object())
    agent._rebuild_runtime_llm_components = lambda: rebuilt_with.append(agent.context_window_tokens)
    agent.apply_runtime_llm_profile = lambda profile, source="": UniversalAgent.apply_runtime_llm_profile(
        agent, profile, source=source
    )
    agent.apply_runtime_llm_override = lambda *args, **kwargs: UniversalAgent.apply_runtime_llm_override(
        agent, *args, **kwargs
    )
    agent.refresh_persistent_llm_override = lambda: UniversalAgent.refresh_persistent_llm_override(agent)
    return agent, rebuilt_with


@pytest.mark.parametrize("failover", [False, True])
@pytest.mark.parametrize(
    "saved,expected_window",
    [
        # TeamLead's change for another agent type (current_agent.llm.*, target_agent_type).
        (
            "agent_types:\n  InternalImprovementAgent:\n    provider: litellm\n    model_id: gpt-6-luna\n"
            "    context_window_tokens: 1050000\n",
            1_050_000,
        ),
        # TeamLead's workspace default (workspace_default.llm.*), for a type without its own entry.
        ("default:\n  provider: litellm\n  model_id: gpt-6-luna\n  context_window_tokens: 1050000\n", 1_050_000),
        (
            "agent_types:\n  InternalImprovementAgent:\n    provider: litellm\n    model_id: gpt-6-luna\n"
            "    context_window_tokens: null\n",
            None,
        ),
        ("default:\n  provider: litellm\n  model_id: gpt-6-luna\n  context_window_tokens: null\n", None),
    ],
    ids=["agent_type", "workspace_default", "clear_agent_type", "clear_workspace_default"],
)
def test_a_saved_model_change_reaches_another_running_agent_at_its_next_turn_without_a_restart(
    tmp_path, failover: bool, saved: str, expected_window: int | None
) -> None:
    # Owner's report 2026-10-11: TeamLead was told these changes need a manager restart.
    # They do not: every turn starts by reading LLM_OVERRIDES.yaml again (turn.py _prepare_turn),
    # and the agent rebuilds its model and compactor with the new window.
    from custom_agents import model_failover_runtime

    agent, rebuilt_with = _running_agent(tmp_path, failover=failover)
    (tmp_path / "LLM_OVERRIDES.yaml").write_text(saved, encoding="utf-8")

    assert agent.context_window_tokens == 800_000  # an idle agent still shows the old value
    model_failover_runtime.refresh_runtime_llm_selection(agent)

    assert agent.context_window_tokens == expected_window
    assert rebuilt_with == [expected_window]
