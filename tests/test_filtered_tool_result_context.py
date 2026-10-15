from __future__ import annotations

from custom_agents.task_operation_runtime import TaskOperationRuntime
import json
import math
from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.event_loop._retry import ModelRetryStrategy
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent
from strands.models.model import Model
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException

from custom_agents.filtered_tool_result_context import (
    FilteredToolResultConversationManager,
)
from custom_agents.tool_result_offloader import TaskAwareWorkspaceStorage
from runtime_settings.instance_layout import InstanceLayout


def _storage(tmp_path, **identity):
    """Offloaded tool results live in the instance's state/tool_results.

    The runtime hands the manager the per-call offloader's storage, which
    knows the agent, team and task (identity) even when a hook only sees the
    Strands Agent.
    """
    return TaskAwareWorkspaceStorage(artifact_root=InstanceLayout.at(tmp_path).tool_results_dir, **identity)


def _agent(tmp_path, messages, *, model=None):
    return SimpleNamespace(
        messages=messages,
        model=model,
        workspace_dir=str(tmp_path),
        task_manager=SimpleNamespace(
            operations_runtime=TaskOperationRuntime(),
            current_task_id="task_filter_test",
        ),
    )


def _tool_messages(text: str):
    return [
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "tool_1", "name": "web_fetch"}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "tool_1",
                        "status": "success",
                        "content": [{"text": text}],
                    }
                }
            ],
        },
    ]


class _CountingModel:
    def __init__(self) -> None:
        self.calls = []

    async def count_tokens(
        self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None
    ):
        self.calls.append(messages)
        return max(1, len(json.dumps(messages, ensure_ascii=False, default=str)) // 4)


def _model() -> _CountingModel:
    return _CountingModel()


def _event(agent, *, projected_input_tokens=None, invocation_state=None):
    return SimpleNamespace(
        agent=agent,
        projected_input_tokens=projected_input_tokens,
        invocation_state=invocation_state
        if invocation_state is not None
        else {"request_state": {}},
    )


def _successful_model_call(manager, agent, *, invocation_state=None):
    """Run the hooks around a model response that accepted these results."""
    state = invocation_state if invocation_state is not None else {"request_state": {}}
    manager._on_before_model_call(BeforeModelCallEvent(agent=agent, invocation_state=state))
    manager._on_after_model_call(
        AfterModelCallEvent(
            agent=agent,
            invocation_state=state,
            stop_response=AfterModelCallEvent.ModelStopResponse(
                message={"role": "assistant", "content": [{"text": "Read the results."}]},
                stop_reason="end_turn",
            ),
        )
    )


class _PressureModel(_CountingModel):
    def __init__(self, *, context_window_limit=None, utilization=None) -> None:
        super().__init__()
        self.context_window_limit = context_window_limit
        self.utilization = utilization
        self.utilization_calls = []

    def estimate_utilization(self, input_tokens: int) -> float:
        self.utilization_calls.append(input_tokens)
        if self.utilization is not None:
            return float(self.utilization)
        return input_tokens / self.context_window_limit


class _AlwaysOverflowModel(Model):
    def __init__(self) -> None:
        self.calls = 0
        self._config = {"model_id": "test-overflow", "context_window_limit": 1000}

    def update_config(self, **model_config):
        self._config.update(model_config)

    def get_config(self):
        return dict(self._config)

    async def structured_output(
        self, output_model, prompt, system_prompt=None, **kwargs
    ):
        del output_model, prompt, system_prompt, kwargs
        if False:
            yield {}

    async def stream(
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        *,
        tool_choice=None,
        system_prompt_content=None,
        invocation_state=None,
        **kwargs,
    ):
        del (
            messages,
            tool_specs,
            system_prompt,
            tool_choice,
            system_prompt_content,
            invocation_state,
            kwargs,
        )
        self.calls += 1
        raise ContextWindowOverflowException("synthetic provider overflow")
        if False:
            yield {}


class _ProgressiveTestConversationManager(FilteredToolResultConversationManager):
    """Test-only reducer that proves repeated progress still obeys the cap."""

    def _manage_messages(self, agent, **kwargs):
        if kwargs.get("emit_pressure_warnings", True):
            return 0
        text = agent.messages[0]["content"][0]["text"]
        agent.messages[0]["content"][0]["text"] = text[: max(1, len(text) // 2)]
        return 1


def test_reactive_overflow_re_raises_without_adding_warning_messages(tmp_path):
    messages = [{"role": "user", "content": [{"text": "x" * 10_000}]}]
    agent = _agent(tmp_path, messages)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={"max_total_tokens": 1000},
        model=_model(),
    )
    invocation_state = {"request_state": {}}
    manager._on_before_invocation(SimpleNamespace(invocation_state=invocation_state))
    overflow = ContextWindowOverflowException("provider rejected input")

    with pytest.raises(ContextWindowOverflowException) as raised:
        manager.reduce_context(agent, e=overflow)

    assert raised.value is overflow
    assert messages == [{"role": "user", "content": [{"text": "x" * 10_000}]}]


def test_reactive_overflow_reduces_current_tool_result_without_warning_messages(
    tmp_path,
):
    text = "large current tool result " * 1200
    messages = _tool_messages(text)
    agent = _agent(tmp_path, messages)
    model = _model()
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={
            "max_total_tokens": 500,
            "warning_ratio": 0.75,
            "aggressive_ratio": 0.90,
        },
        model=model,
    )
    invocation_state = {"request_state": {}}
    manager._on_before_invocation(SimpleNamespace(invocation_state=invocation_state))
    tokens_before = manager._message_token_count(messages)

    manager.reduce_context(
        agent,
        e=ContextWindowOverflowException("provider rejected input"),
    )

    tokens_after = manager._message_token_count(messages)
    replacement = json.loads(
        messages[1]["content"][0]["toolResult"]["content"][0]["text"]
    )
    assert replacement["artifact_only"] is True
    assert tokens_after < tokens_before
    assert not any(
        message["role"] == "user"
        and "inner_loop_context_warning" in message["content"][0].get("text", "")
        for message in messages
    )


def test_reactive_overflow_re_raises_when_fixed_overhead_exhausts_context_window(
    tmp_path,
):
    text = "large current tool result " * 1200
    messages = _tool_messages(text)
    model = _PressureModel(context_window_limit=100)
    agent = _agent(tmp_path, messages, model=model)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={
            "max_total_tokens": 10000,
            "projected_token_pressure": {"enabled": False},
        },
        model=model,
    )
    invocation_state = {"request_state": {}}
    manager._on_before_invocation(SimpleNamespace(invocation_state=invocation_state))
    manager._on_before_model_call(
        _event(
            agent,
            projected_input_tokens=10_000,
            invocation_state=invocation_state,
        )
    )
    overflow = ContextWindowOverflowException("provider rejected input")

    with pytest.raises(ContextWindowOverflowException) as raised:
        manager.reduce_context(agent, e=overflow)

    assert raised.value is overflow
    replacement = json.loads(
        messages[1]["content"][0]["toolResult"]["content"][0]["text"]
    )
    assert replacement["artifact_only"] is True


def test_real_strands_overflow_path_stops_after_no_progress(tmp_path):
    model = _AlwaysOverflowModel()
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={"max_total_tokens": 1000},
        model=model,
    )
    agent = Agent(
        model=model,
        conversation_manager=manager,
        callback_handler=None,
        retry_strategy=None,
    )

    with pytest.raises(ContextWindowOverflowException):
        agent("x" * 10_000)

    assert model.calls == 1


def test_real_strands_overflow_path_caps_retries_despite_repeated_progress(tmp_path):
    model = _AlwaysOverflowModel()
    manager = _ProgressiveTestConversationManager(
        storage=_storage(tmp_path),
        config={"reactive_overflow": {"max_total_recovery_attempts": 2}},
        model=model,
    )
    agent = Agent(
        model=model,
        conversation_manager=manager,
        callback_handler=None,
        retry_strategy=None,
    )

    with pytest.raises(ContextWindowOverflowException):
        agent("x" * 10_000)

    assert model.calls == 3


def test_filtered_context_keeps_comms_shaped_json_lenient(tmp_path):
    payload = {
        "message_class": "Task",
        "task_id": "task_1",
        "msg_meta": {"conversation_id": "conversation_1"},
        "items": [{"value": index} for index in range(50)],
    }
    text = json.dumps(payload)
    messages = _tool_messages(text)
    agent = _agent(tmp_path, messages)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={
            "max_total_tokens": 25000,
            "filters": {
                "json_large": {
                    "trigger_tokens": 3,
                    "comms_json_lenient": True,
                    "comms_json_trigger_tokens": 25000,
                }
            },
        },
        model=_model(),
    )

    _successful_model_call(manager, agent)
    manager.apply_management(agent, invocation_state={"request_state": {}})

    assert messages[1]["content"][0]["toolResult"]["content"][0]["text"] == text


def test_filtered_context_can_reduce_old_offloaded_result_to_artifact_only(tmp_path):
    payload = {
        "offloaded_tool_result": True,
        "artifact_manifest_path": str(
            InstanceLayout.at(tmp_path).tool_results_dir / "task" / "manifest.json"
        ),
        "original_tokens": 50000,
        "original_chars": 200000,
        "preview": "large preview " * 1000,
    }
    messages = _tool_messages(json.dumps(payload)) + [
        {"role": "assistant", "content": [{"text": "recent summary"}]}
    ]
    agent = _agent(tmp_path, messages)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={
            "max_total_tokens": 625,
            "warning_ratio": 0.50,
            "aggressive_ratio": 0.60,
            "preserve_recent_messages": 1,
        },
        model=_model(),
    )

    _successful_model_call(manager, agent)
    manager.apply_management(agent, invocation_state={"request_state": {}})

    replacement = json.loads(
        messages[1]["content"][0]["toolResult"]["content"][0]["text"]
    )
    assert replacement["filtered_tool_result"] is True
    assert replacement["artifact_only"] is True
    assert replacement["artifact_manifest_path"].endswith("manifest.json")


def test_filtered_context_never_filters_system_message_content(tmp_path):
    system_text = "<html><body>" + ("system contract " * 1000) + "</body></html>"
    messages = [
        {
            "role": "system",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "system_tool",
                        "status": "success",
                        "content": [{"text": system_text}],
                    }
                }
            ],
        },
        *_tool_messages("<html><body>" + ("normal tool " * 300) + "</body></html>"),
    ]
    agent = _agent(tmp_path, messages)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={
            "max_total_tokens": 25000,
            "filters": {"html_document": {"trigger_tokens": 25}},
        },
        model=_model(),
    )

    _successful_model_call(manager, agent)
    manager.apply_management(agent, invocation_state={"request_state": {}})

    assert messages[0]["content"][0]["toolResult"]["content"][0]["text"] == system_text
    normal_replacement = json.loads(
        messages[2]["content"][0]["toolResult"]["content"][0]["text"]
    )
    assert normal_replacement["filtered_tool_result"] is True


def test_projected_pressure_skips_when_projected_tokens_missing(tmp_path):
    text = "plain old tool output " * 1200
    messages = _tool_messages(text) + [
        {"role": "assistant", "content": [{"text": "recent summary"}]}
    ]
    model = _PressureModel(context_window_limit=1000)
    agent = _agent(tmp_path, messages, model=model)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={"max_total_tokens": 25000, "preserve_recent_messages": 1},
        model=model,
    )
    _successful_model_call(manager, agent)

    manager._on_before_model_call(_event(agent))

    assert messages[1]["content"][0]["toolResult"]["content"][0]["text"] == text


def test_projected_pressure_warning_emitted_once_per_invocation(tmp_path):
    text = "plain old tool output " * 1200
    messages = _tool_messages(text) + [
        {"role": "assistant", "content": [{"text": "recent summary"}]}
    ]
    model = _PressureModel(context_window_limit=1000)
    agent = _agent(tmp_path, messages, model=model)
    invocation_state = {"request_state": {}}
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={
            "max_total_tokens": 25000,
            "preserve_recent_messages": 1,
            "projected_token_pressure": {"threshold_ratio": 0.85, "target_ratio": 0.50},
        },
        model=model,
    )
    _successful_model_call(manager, agent, invocation_state=invocation_state)

    manager._on_before_model_call(
        _event(agent, projected_input_tokens=950, invocation_state=invocation_state)
    )
    manager._on_before_model_call(
        _event(agent, projected_input_tokens=950, invocation_state=invocation_state)
    )

    projected_warning_messages = [
        message
        for message in messages
        if message["role"] == "user"
        and "projected_token_pressure" in message["content"][0].get("text", "")
    ]
    assert len(projected_warning_messages) == 1


def _tool_exchange(tool_use_id: str, name: str, text: str) -> list[dict]:
    return [
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": tool_use_id, "name": name, "input": {}}}]},
        {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}}],
        },
    ]


def _result_text(messages: list[dict], tool_use_id: str) -> str:
    for message in messages:
        for block in message.get("content", []):
            result = block.get("toolResult") if isinstance(block, dict) else None
            if result and result["toolUseId"] == tool_use_id:
                return result["content"][0]["text"]
    raise AssertionError(f"no result for {tool_use_id}")


_STAGED_PLAN = json.dumps(
    {
        "change_id": "settings_change_01KSTAGED",
        "changes": [{"path": "current_agent.llm.model_id", "value": "anthropic/claude-haiku-5-5"}],
        "instructions": "Tell the person it works and ask them to confirm. " * 40,
    }
)
_HTML_DOCUMENT = "<html><body>" + "<p>Important content for the next step</p>" * 400 + "</body></html>"


def test_projected_pressure_keeps_a_result_the_model_has_not_read(tmp_path):
    """Nova's model switch: the staged change id must reach the model under pressure."""

    messages = _tool_exchange("old_read", "web_fetch", "old research text " * 80)
    # As in the log: a large fixed overhead, so no amount of shrinking reaches the target.
    model = _PressureModel(context_window_limit=10_000, utilization=0.95)
    agent = _agent(tmp_path, messages, model=model)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path), config={"preserve_recent_messages": 2}, model=model
    )
    state = {"request_state": {}}
    manager._on_before_invocation(SimpleNamespace(invocation_state=state))
    _successful_model_call(manager, agent, invocation_state=state)
    messages.extend(_tool_exchange("stage_1", "plan_runtime_settings_change", _STAGED_PLAN))

    manager._on_before_model_call(_event(agent, projected_input_tokens=9_500, invocation_state=state))

    assert json.loads(_result_text(messages, "old_read"))["artifact_only"] is True
    assert _result_text(messages, "stage_1") == _STAGED_PLAN


def test_provider_rejection_shrinks_read_results_before_unread_ones(tmp_path):
    messages = _tool_exchange("old_read", "web_fetch", "old research text " * 80)
    model = _model()
    agent = _agent(tmp_path, messages, model=model)
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={"projected_token_pressure": {"enabled": False}},
        model=model,
    )
    state = {"request_state": {}}
    manager._on_before_invocation(SimpleNamespace(invocation_state=state))
    _successful_model_call(manager, agent, invocation_state=state)
    messages.extend(_tool_exchange("stage_1", "plan_runtime_settings_change", _STAGED_PLAN))
    manager._on_before_model_call(BeforeModelCallEvent(agent=agent, invocation_state=state))
    # The new result is the larger one; shrinking the read one alone reaches the target.
    total = manager._message_token_count(messages)
    manager.config["max_total_tokens"] = math.ceil((total - 150) / manager.config["warning_ratio"])

    manager.reduce_context(agent, e=ContextWindowOverflowException("too long"), invocation_state=state)

    assert json.loads(_result_text(messages, "old_read"))["artifact_only"] is True
    assert _result_text(messages, "stage_1") == _STAGED_PLAN


class _ScriptedLeadUnderPressure(Model):
    """Calls the staging tool, then records what the next model call receives."""

    def __init__(self) -> None:
        self._config = {"model_id": "scripted", "context_window_limit": 5_500}
        self.second_call_results: list[str] = []

    def update_config(self, **model_config):
        self._config.update(model_config)

    def get_config(self):
        return dict(self._config)

    async def count_tokens(self, messages, tool_specs=None, system_prompt=None, system_prompt_content=None):
        return max(1, len(json.dumps(messages, ensure_ascii=False, default=str)) // 4)

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        results = [
            block["toolResult"]["content"][0]["text"]
            for message in messages
            for block in message.get("content", [])
            if "toolResult" in block
        ]
        yield {"messageStart": {"role": "assistant"}}
        if not results:
            yield {"contentBlockStart": {"start": {"toolUse": {"name": "stage_model_switch", "toolUseId": "stage_1"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            # As in the reported log: the provider counts ~90% of the window
            # before the conversation (system prompt, tool specs, reply schema).
            usage = {"inputTokens": 4_950, "outputTokens": 20, "totalTokens": 4_970}
            yield {"metadata": {"usage": usage, "metrics": {"latencyMs": 1}}}
            return
        self.second_call_results = results
        yield {"contentBlockDelta": {"delta": {"text": "It works. Confirm?"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def test_real_strands_turn_shows_the_staged_change_id_under_projected_pressure(tmp_path):
    @tool
    def stage_model_switch() -> str:
        """Stage the model switch."""
        return _STAGED_PLAN

    model = _ScriptedLeadUnderPressure()
    manager = FilteredToolResultConversationManager(storage=_storage(tmp_path), model=model)
    # A brand-new conversation: only the fixed overhead fills the window.
    agent = Agent(model=model, tools=[stage_model_switch], conversation_manager=manager, callback_handler=None)

    agent("Switch your model to anthropic/claude-haiku-5-5 with high effort.")

    pressure_warnings = [
        message
        for message in agent.messages
        if message["role"] == "user" and "projected_pressure" in message["content"][0].get("text", "")
    ]
    assert pressure_warnings, "the scenario must run under projected pressure"
    assert model.second_call_results == [_STAGED_PLAN]


class _RetryingLead(_ScriptedLeadUnderPressure):
    """Read old research, call a tool, then reject or retry its next response."""

    def __init__(self, *, failure: str) -> None:
        super().__init__()
        self._config["context_window_limit"] = 10_000
        self.failure = failure
        self.calls = 0
        self.result_attempts: list[dict[str, str]] = []
        self.manager = None
        self.accepted_message_tokens = None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls += 1
        self.result_attempts.append(
            {
                block["toolResult"]["toolUseId"]: block["toolResult"]["content"][0]["text"]
                for message in messages
                for block in message.get("content", [])
                if "toolResult" in block
            }
        )
        if self.failure == "overflow" and self.calls >= 2:
            message_tokens = await self.count_tokens(messages)
            if self.accepted_message_tokens is None:
                # The provider needs a small reduction. Old research is enough,
                # so the fresh staging result must remain available on retry.
                self.accepted_message_tokens = message_tokens - 100
                self.manager.config["max_total_tokens"] = math.ceil(
                    self.accepted_message_tokens / self.manager.config["warning_ratio"]
                )
            if message_tokens > self.accepted_message_tokens:
                raise ContextWindowOverflowException("provider input limit exceeded")
        if self.failure == "throttle" and self.calls == 2:
            raise ModelThrottledException("provider rate limit")

        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            yield {"contentBlockStart": {"start": {"toolUse": {"name": "fresh_result", "toolUseId": "stage_1"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            # Fixed provider input makes the fresh result encounter pressure.
            input_tokens = 8_850 if self.failure == "overflow" else 9_500
            usage = {"inputTokens": input_tokens, "outputTokens": 20, "totalTokens": input_tokens + 20}
            yield {"metadata": {"usage": usage, "metrics": {"latencyMs": 1}}}
            return
        yield {"contentBlockDelta": {"delta": {"text": "Read the fresh result."}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def test_real_strands_overflow_keeps_unread_staging_result_when_old_research_is_enough(tmp_path):
    @tool
    def fresh_result() -> str:
        """Return a staged settings change."""
        return _STAGED_PLAN

    old_text = "old research text " * 100
    model = _RetryingLead(failure="overflow")
    manager = FilteredToolResultConversationManager(
        storage=_storage(tmp_path),
        config={"projected_token_pressure": {"enabled": False}},
        model=model,
    )
    model.manager = manager
    agent = Agent(
        model=model,
        tools=[fresh_result],
        messages=_tool_exchange("old_read", "web_fetch", old_text),
        conversation_manager=manager,
        callback_handler=None,
        retry_strategy=None,
    )

    agent("Stage the model switch.")

    assert model.calls == 3
    rejected = model.result_attempts[1]
    retried = model.result_attempts[2]
    assert rejected == {"old_read": old_text, "stage_1": _STAGED_PLAN}
    assert json.loads(retried["old_read"])["artifact_only"] is True
    assert retried["stage_1"] == _STAGED_PLAN


@pytest.mark.parametrize("failure", ["throttle", "discarded_response"])
def test_real_strands_retry_keeps_fresh_html_with_default_recent_protection(tmp_path, failure):
    html = _HTML_DOCUMENT

    @tool
    def fresh_result() -> str:
        """Return the HTML document needed for the next step."""
        return html

    model = _RetryingLead(failure=failure)
    manager = FilteredToolResultConversationManager(storage=_storage(tmp_path), model=model)
    agent = Agent(
        model=model,
        tools=[fresh_result],
        conversation_manager=manager,
        callback_handler=None,
        retry_strategy=ModelRetryStrategy(max_attempts=2, initial_delay=0, max_delay=0),
    )
    if failure == "discarded_response":
        def discard_second_response(event: AfterModelCallEvent) -> None:
            if model.calls == 2:
                event.retry = True

        agent.add_hook(discard_second_response, AfterModelCallEvent)

    agent("Read the document.")

    assert manager.config["preserve_recent_messages"] == 8
    assert model.calls == 3
    assert model.result_attempts[1]["stage_1"] == html
    assert model.result_attempts[2]["stage_1"] == html
    # Hygiene filtering still runs after the model has accepted the document.
    assert json.loads(_result_text(agent.messages, "stage_1"))["filter"] == "html_document"


def test_cancelled_model_call_does_not_mark_fresh_html_read(tmp_path):
    html = _HTML_DOCUMENT

    @tool
    def fresh_result() -> str:
        """Return the HTML document needed for the next step."""
        return html

    model = _RetryingLead(failure="cancel")
    manager = FilteredToolResultConversationManager(storage=_storage(tmp_path), model=model)
    agent = Agent(model=model, tools=[fresh_result], conversation_manager=manager, callback_handler=None)

    def cancel_after_tool(event: BeforeModelCallEvent) -> None:
        if any(block.get("toolResult") for message in event.agent.messages for block in message.get("content", [])):
            event.cancel = "Stopped before reading the result."

    agent.add_hook(cancel_after_tool, BeforeModelCallEvent)

    agent("Read the document.")

    assert model.calls == 1
    assert _result_text(agent.messages, "stage_1") == html
    assert "stage_1" not in manager._seen_tool_result_ids


def test_success_marks_only_the_results_in_the_attempted_input(tmp_path):
    html = _HTML_DOCUMENT
    messages = _tool_exchange("in_request", "web_fetch", html)
    agent = _agent(tmp_path, messages)
    manager = FilteredToolResultConversationManager(storage=_storage(tmp_path), model=_model())
    state = {"request_state": {}}
    manager._on_before_model_call(BeforeModelCallEvent(agent=agent, invocation_state=state))
    # Another After callback can add future input while handling the response.
    # It was not part of the request the model successfully read.
    messages.extend(_tool_exchange("added_later", "web_fetch", html))
    manager._on_after_model_call(
        AfterModelCallEvent(
            agent=agent,
            invocation_state=state,
            stop_response=AfterModelCallEvent.ModelStopResponse(
                message={"role": "assistant", "content": [{"text": "Read the requested result."}]},
                stop_reason="end_turn",
            ),
        )
    )

    manager.apply_management(agent, invocation_state=state)

    assert json.loads(_result_text(messages, "in_request"))["filter"] == "html_document"
    assert _result_text(messages, "added_later") == html
