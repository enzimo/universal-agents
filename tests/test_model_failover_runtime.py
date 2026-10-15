from __future__ import annotations

import pytest
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_agents import model_failover_runtime
from custom_messages.enums import ProgressState
from default_values import LLM_DEFAULTS
from runtime_settings.instance_layout import InstanceLayout


class FakeTaskManager:
    def __init__(self, task_id: str = "task_1") -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self.task_id = task_id
        self.info = SimpleNamespace(
            state=ProgressState.IN_PROGRESS,
            task=SimpleNamespace(task_id=task_id),
        )
        self.history: list[dict[str, Any]] = []
        self.directives: list[tuple[str, str]] = []
        self.state_updates: list[tuple[str, ProgressState, str]] = []
        self.enqueued: list[tuple[str, bool]] = []

    def append_history_entry(self, task_id: str, message: str, **kwargs: Any) -> None:
        self.history.append({"task_id": task_id, "message": message, **kwargs})

    def get_task_info(self, task_id: Optional[str]) -> Any:
        if task_id == self.task_id:
            return self.info
        return None

    def is_terminal_state(self, state: ProgressState) -> bool:
        return state in {
            ProgressState.COMPLETED,
            ProgressState.FAILED,
            ProgressState.CANCELLED,
        }

    def append_runtime_resume_directive(self, task_id: str, directive: str) -> None:
        self.directives.append((task_id, directive))

    def update_main_task_state(
        self, task_id: str, state: ProgressState, updated_at: str
    ) -> bool:
        self.info.state = state
        self.state_updates.append((task_id, state, updated_at))
        return True

    def enqueue_task(self, task_id: str, *, front: bool = False) -> None:
        self.enqueued.append((task_id, front))


@dataclass
class FakeAgent:
    # Failover state and LLM overrides are found through the instance layout.
    instance_root: Path
    llm_failover: dict[str, Any]
    task_manager: Optional[FakeTaskManager] = None
    model_provider: str = "litellm"
    model_id: str = "primary/model"
    team_id: str = "team_test"
    named_agent_type: str = "TeamArchitect"
    id: str = "agent_id_test"
    name: str = "manager"
    llm_override_path: Optional[str] = None
    reasoning_effort: Optional[str] = None
    reasoning_summary: Optional[str] = None
    litellm_api_mode: str = "chat_completions"
    context_window_tokens: Optional[int] = None
    supports_image_input: Optional[bool] = None
    supports_audio_input: Optional[bool] = None
    supports_file_input: Optional[bool] = None
    model_capabilities: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._configured_model_provider = self.model_provider
        self._configured_model_id = self.model_id
        self._logger = logging.getLogger(f"FakeAgent.{self.id}")
        self.applied: list[tuple[str, str, str]] = []
        self.persistent_refreshes = 0
        self.activity_events: list[dict[str, Any]] = []

    def refresh_persistent_llm_override(self) -> None:
        self.persistent_refreshes += 1

    def apply_runtime_llm_override(
        self,
        provider: str,
        model_id: str,
        *,
        reasoning_effort: Any = None,
        reasoning_summary: Any = None,
        litellm_api_mode: Any = "chat_completions",
        context_window_tokens: Any = None,
        supports_image_input: Any = None,
        supports_audio_input: Any = None,
        supports_file_input: Any = None,
        source: str,
    ) -> None:
        self.model_provider = provider
        self.model_id = model_id
        self.applied.append((provider, model_id, source))
        self.reasoning_effort = reasoning_effort
        self.reasoning_summary = reasoning_summary
        self.litellm_api_mode = litellm_api_mode
        self.context_window_tokens = context_window_tokens
        self.supports_image_input = supports_image_input
        self.supports_audio_input = supports_audio_input
        self.supports_file_input = supports_file_input

    def _is_provider_overload_error(self, error: BaseException) -> bool:
        return "rate limit" in str(error).lower() or "busy" in str(error).lower()

    def emit_activity_event(self, **kwargs: Any) -> None:
        self.activity_events.append(kwargs)


def _failover_config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "enabled": True,
        "same_provider_retry": {
            "max_attempts": 2,
            "initial_delay_seconds": 1,
            "max_delay_seconds": 3,
        },
        "switch_for": {"min_turns": 2, "min_seconds": 60},
        "demotion": {
            "failure_threshold": 3,
            "failure_window_seconds": 3600,
            "demotion_seconds": 86400,
        },
        "notify_user": True,
        "fallbacks": [
            {"provider": "openai", "model_id": "fallback/model"},
            {"provider": "anthropic", "model_id": "second/model"},
        ],
    }
    config.update(overrides)
    return config


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [None, "concise"])
async def test_active_primary_lease_uses_saved_profile_without_renewing_lease(
    tmp_path: Path, summary: Optional[str],
) -> None:
    import yaml

    agent = FakeAgent(
        instance_root=tmp_path, reasoning_summary="auto", litellm_api_mode="responses",
        llm_failover=_failover_config(tmp_path, fallbacks=[{
            "provider": "openai", "model_id": "fallback/model",
        }]),
    )
    path = _override_path(tmp_path)
    profile = {
        "provider": "litellm", "model_id": "primary/model",
        "reasoning_summary": "auto", "litellm_api_mode": "responses",
    }
    path.write_text(yaml.safe_dump({"agent_types": {agent.named_agent_type: profile}}), encoding="utf-8")
    for model_id in ("fallback/model", "primary/model"):
        await model_failover_runtime.handle_provider_failure(
            agent, error=RuntimeError("rate limit"), task_id=None, tool_calls_started=False,
        )
        assert agent.model_id == model_id
    lease_before = _read_state(agent)["active_lease"]
    assert lease_before["reasoning_summary"] == "auto"

    profile["reasoning_summary"] = summary
    path.write_text(yaml.safe_dump({"agent_types": {agent.named_agent_type: profile}}), encoding="utf-8")
    model_failover_runtime.refresh_runtime_llm_selection(agent)

    assert agent.model_id == "primary/model"
    assert agent.reasoning_summary == summary
    lease_after = _read_state(agent)["active_lease"]
    assert lease_after["reasoning_summary"] == summary
    for field_name in ("started_at", "expires_at", "turns_remaining"):
        assert lease_after[field_name] == lease_before[field_name]


@pytest.mark.asyncio
async def test_tool_caps_refresh_for_primary_and_active_fallback_without_resetting_selection(tmp_path: Path) -> None:
    from custom_agents.tool_loading import tool_limit

    agent = FakeAgent(instance_root=tmp_path, llm_failover=_failover_config(tmp_path))
    path = InstanceLayout.at(tmp_path).llm_overrides_path
    path.parent.mkdir(parents=True, exist_ok=True)

    def write_caps(primary, fallback):
        path.write_text(
            f"model_capabilities:\n  litellm:\n    primary/model:\n      max_tools_per_request: {primary}\n"
            f"    fallback/model:\n      max_tools_per_request: {fallback}\n", encoding="utf-8"
        )

    agent.llm_failover["fallbacks"] = [{"provider": "litellm", "model_id": "fallback/model"}]
    write_caps(64, 80)
    model_failover_runtime.refresh_runtime_llm_selection(agent)
    assert tool_limit(agent)[0] == 64
    await model_failover_runtime.handle_provider_failure(
        agent, error=RuntimeError("rate limit"), task_id=None, tool_calls_started=False
    )
    assert agent.model_id == "fallback/model" and tool_limit(agent)[0] == 80
    write_caps(70, 90)
    model_failover_runtime.refresh_runtime_llm_selection(agent)
    assert agent.model_id == "fallback/model" and tool_limit(agent)[0] == 90
    path.unlink()
    model_failover_runtime.refresh_runtime_llm_selection(agent)
    assert agent.model_id == "fallback/model" and tool_limit(agent)[0] == 128


@pytest.mark.parametrize("field_name,previous_value", [
    ("reasoning_effort", "high"),
    ("reasoning_summary", "auto"),
    ("context_window_tokens", 800_000),
])
def test_next_turn_selection_preserves_explicit_null_override(
    tmp_path: Path, field_name: str, previous_value: Any,
) -> None:
    import yaml

    agent = FakeAgent(
        instance_root=tmp_path, llm_failover=_failover_config(tmp_path, fallbacks=[]),
        litellm_api_mode="responses",
    )
    setattr(agent, field_name, previous_value)
    override_path = InstanceLayout.at(tmp_path).llm_overrides_path
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(yaml.safe_dump({"agent_types": {agent.named_agent_type: {
        "provider": agent.model_provider, "model_id": agent.model_id,
        "litellm_api_mode": "responses", field_name: None,
    }}}), encoding="utf-8")

    for _ in range(2):
        model_failover_runtime.refresh_runtime_llm_selection(agent)
        assert getattr(agent, field_name) is None
        assert agent.model_id == "primary/model"


def test_failover_defaults_to_disabled_without_hidden_candidates() -> None:
    assert LLM_DEFAULTS.failover.value["enabled"] is False
    assert LLM_DEFAULTS.failover.value["fallbacks"] == []


def _state_file(agent: FakeAgent) -> Path:
    """Failover state is a runtime record in state/model_failover."""
    return (
        InstanceLayout.at(agent.instance_root).model_failover_dir
        / f"{agent.team_id}_{agent.named_agent_type}.json"
    )


def _read_state(agent: FakeAgent) -> dict[str, Any]:
    return json.loads(_state_file(agent).read_text(encoding="utf-8"))


def _override_path(tmp_path: Path) -> Path:
    """LLM_OVERRIDES.yaml lives in the instance's control/ folder."""
    layout = InstanceLayout.at(tmp_path)
    layout.control_dir.mkdir(parents=True, exist_ok=True)
    return layout.llm_overrides_path


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_calls_started", [False, True])
async def test_credential_failure_does_not_switch_retry_requeue_or_demote(tmp_path, tool_calls_started):
    from custom_agents.model_keys import ModelKeyUnavailableError
    from model_providers.endpoints import openai_compatible_endpoint
    from strands.types.exceptions import ModelThrottledException

    tasks = FakeTaskManager()
    agent = FakeAgent(instance_root=tmp_path, llm_failover=_failover_config(tmp_path), task_manager=tasks)
    error = ModelThrottledException("Error code: 503: service unavailable")
    error.__cause__ = ModelKeyUnavailableError(openai_compatible_endpoint("https://model.example"), "broker HTTP 503 credential store failure")

    outcome = await model_failover_runtime.handle_provider_failure(
        agent, error=error, task_id=tasks.task_id, tool_calls_started=tool_calls_started,
    )

    assert outcome is None
    assert not agent.applied and not agent.activity_events
    assert not tasks.history and not tasks.enqueued and not tasks.state_updates
    assert not _state_file(agent).exists()


@pytest.mark.asyncio
async def test_handle_provider_failure_switches_and_retries_inline_before_tool_activity(
    tmp_path: Path,
) -> None:
    task_manager = FakeTaskManager()
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=_failover_config(tmp_path),
        task_manager=task_manager,
    )

    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit from primary provider"),
        task_id=task_manager.task_id,
        tool_calls_started=False,
    )

    assert outcome is not None
    assert outcome.retry_inline is True
    assert outcome.requeue_required is False
    assert outcome.from_candidate.key == "litellm/primary/model"
    assert outcome.to_candidate.key == "openai/fallback/model"
    assert agent.model_provider == "openai"
    assert agent.model_id == "fallback/model"
    assert agent.applied[-1] == (
        "openai",
        "fallback/model",
        "model_failover_after_provider_error",
    )
    assert task_manager.history
    assert "Switching to openai/fallback/model" in task_manager.history[-1]["message"]
    assert agent.activity_events
    assert agent.activity_events[-1]["user_visible"] is True
    state = _read_state(agent)
    assert state["active_lease"]["provider"] == "openai"
    assert state["providers"]["litellm/primary/model"]["failures"]


@pytest.mark.asyncio
async def test_provider_failure_after_tool_activity_requeues_partial_task(
    tmp_path: Path,
) -> None:
    task_manager = FakeTaskManager()
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=_failover_config(tmp_path),
        task_manager=task_manager,
    )
    error = RuntimeError("provider is busy after a tool call")

    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=error,
        task_id=task_manager.task_id,
        tool_calls_started=True,
    )

    assert outcome is not None
    assert outcome.retry_inline is False
    assert outcome.requeue_required is True
    assert await model_failover_runtime.requeue_task_after_failover(
        agent,
        task_id=task_manager.task_id,
        outcome=outcome,
        error=error,
    )
    assert task_manager.info.state == ProgressState.PARTIAL
    assert task_manager.enqueued == [(task_manager.task_id, True)]
    assert task_manager.directives
    directive = task_manager.directives[-1][1]
    assert "Do not repeat completed side effects" in directive
    assert "Current provider is openai/fallback/model" in directive


@pytest.mark.asyncio
async def test_demoted_primary_is_skipped_after_failover_window_expires(
    tmp_path: Path,
) -> None:
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=_failover_config(
            tmp_path,
            switch_for={"min_turns": 1, "min_seconds": 0},
            demotion={
                "failure_threshold": 1,
                "failure_window_seconds": 3600,
                "demotion_seconds": 86400,
            },
        ),
    )

    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit"),
        task_id=None,
        tool_calls_started=False,
    )
    assert outcome is not None
    assert agent.model_provider == "openai"

    model_failover_runtime.record_model_success(agent, task_id=None)
    model_failover_runtime.refresh_runtime_llm_selection(agent)

    assert agent.model_provider == "openai"
    assert agent.applied[-1] == ("openai", "fallback/model", "model_failover_selection")
    state = _read_state(agent)
    assert state["providers"]["litellm/primary/model"]["demoted_until"]


@pytest.mark.asyncio
async def test_disabling_failover_clears_existing_active_lease(
    tmp_path: Path, caplog
) -> None:
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=_failover_config(tmp_path),
    )
    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit"),
        task_id=None,
        tool_calls_started=False,
    )
    assert outcome is not None
    assert "active_lease" in _read_state(agent)

    agent.llm_failover = {**agent.llm_failover, "enabled": False}
    caplog.set_level(logging.WARNING)
    model_failover_runtime.refresh_runtime_llm_selection(agent)

    assert agent.persistent_refreshes == 1
    assert "active_lease" not in _read_state(agent)
    assert "reason=failover_disabled" in caplog.text


@pytest.mark.asyncio
async def test_failover_active_lease_preserves_model_capabilities_after_refresh(
    tmp_path: Path,
) -> None:
    config = _failover_config(
        tmp_path,
        fallbacks=[
            {
                "provider": "litellm",
                "model_id": "minimax/minimax-m3",
                "supports_image_input": True,
                "supports_audio_input": False,
                "supports_file_input": True,
                "reasoning_effort": "high",
                "reasoning_summary": "auto",
                "litellm_api_mode": "responses",
                "context_window_tokens": 131072,
            }
        ],
    )
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=config,
    )

    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit"),
        task_id=None,
        tool_calls_started=False,
    )

    assert outcome is not None
    assert agent.model_provider == "litellm"
    assert agent.model_id == "minimax/minimax-m3"
    assert agent.supports_image_input is True
    assert agent.supports_audio_input is False
    assert agent.supports_file_input is True
    assert agent.reasoning_effort == "high"
    assert agent.reasoning_summary == "auto"
    assert agent.litellm_api_mode == "responses"
    assert agent.context_window_tokens == 131072
    state = _read_state(agent)
    assert state["active_lease"]["supports_image_input"] is True
    assert state["active_lease"]["supports_audio_input"] is False
    assert state["active_lease"]["supports_file_input"] is True
    assert state["active_lease"]["reasoning_effort"] == "high"
    assert state["active_lease"]["reasoning_summary"] == "auto"
    assert state["active_lease"]["litellm_api_mode"] == "responses"
    assert state["active_lease"]["context_window_tokens"] == 131072

    restarted_agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=config,
        supports_image_input=None,
        supports_audio_input=None,
        supports_file_input=None,
    )
    model_failover_runtime.refresh_runtime_llm_selection(restarted_agent)

    assert restarted_agent.model_provider == "litellm"
    assert restarted_agent.model_id == "minimax/minimax-m3"
    assert restarted_agent.supports_image_input is True
    assert restarted_agent.supports_audio_input is False
    assert restarted_agent.supports_file_input is True
    assert restarted_agent.reasoning_effort == "high"
    assert restarted_agent.reasoning_summary == "auto"
    assert restarted_agent.litellm_api_mode == "responses"
    assert restarted_agent.context_window_tokens == 131072


@pytest.mark.asyncio
async def test_removed_failover_target_invalidates_active_lease_immediately(
    tmp_path: Path,
    caplog,
) -> None:
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=_failover_config(tmp_path),
    )
    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit"),
        task_id=None,
        tool_calls_started=False,
    )
    assert outcome is not None
    assert agent.model_id == "fallback/model"

    agent.llm_failover = _failover_config(tmp_path, fallbacks=[])
    caplog.set_level(logging.WARNING)
    model_failover_runtime.refresh_runtime_llm_selection(agent)

    assert agent.applied[-1] == ("litellm", "primary/model", "model_failover_selection")
    assert "active_lease" not in _read_state(agent)
    assert "event=model_failover_lease_invalidated" in caplog.text
    assert "reason=target_removed" in caplog.text


@pytest.mark.asyncio
async def test_stale_inflight_failure_applies_new_primary_without_creating_lease(
    tmp_path: Path,
) -> None:
    override_path = _override_path(tmp_path)
    override_path.write_text(
        "default: {provider: litellm, model_id: replacement/model}\n",
        encoding="utf-8",
    )
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_override_path=str(override_path),
        llm_failover=_failover_config(tmp_path),
    )

    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit from superseded primary"),
        task_id=None,
        tool_calls_started=False,
    )

    assert outcome is not None
    assert outcome.selection_reason == "configured_primary_changed"
    assert outcome.to_candidate.key == "litellm/replacement/model"
    assert agent.applied[-1] == (
        "litellm",
        "replacement/model",
        "model_selection_superseded_after_provider_error",
    )
    state = _read_state(agent)
    assert "active_lease" not in state
    assert "litellm/primary/model" not in state["providers"]


@pytest.mark.asyncio
async def test_default_state_path_scopes_by_team_and_agent_type(tmp_path: Path) -> None:
    config = _failover_config(tmp_path)
    agent = FakeAgent(instance_root=tmp_path, llm_failover=config, id="agent_id_one")

    outcome = await model_failover_runtime.handle_provider_failure(
        agent,
        error=RuntimeError("rate limit"),
        task_id=None,
        tool_calls_started=False,
    )

    assert outcome is not None
    failover_dir = InstanceLayout.at(tmp_path).model_failover_dir
    assert (failover_dir / "team_test_TeamArchitect.json").exists()
    assert not (failover_dir / "team_test_TeamArchitect_agent_id_one.json").exists()


def test_retry_strategy_config_uses_same_provider_retry_values(tmp_path: Path) -> None:
    agent = FakeAgent(
        instance_root=tmp_path,
        llm_failover=_failover_config(tmp_path),
    )

    assert model_failover_runtime.retry_strategy_config(agent) == {
        "max_attempts": 2,
        "initial_delay": 1,
        "max_delay": 3,
    }
